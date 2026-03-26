import os
import time
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

REQUEST_TIMEOUT = 8  # seconds — fail fast
CACHE_TTL = 3600  # 1 hour

# --- In-memory cache ---
_cache = {}  # {ticker: {"timestamp": float, "result": dict}}
_cache_lock = threading.Lock()


def _get_cached(ticker: str):
	with _cache_lock:
		entry = _cache.get(ticker)
		if entry and (time.time() - entry["timestamp"]) < CACHE_TTL:
			logger.info(f"Cache hit for {ticker}")
			return entry["result"]
	return None


def _set_cached(ticker: str, result: dict):
	with _cache_lock:
		_cache[ticker] = {"timestamp": time.time(), "result": result}


def _fmp_get(endpoint: str, ticker: str):
	"""Single FMP GET request — no retry, fail fast."""
	url = f"{FMP_BASE_URL}/{endpoint}/{ticker}"
	params = {"apikey": FMP_API_KEY}

	start = time.time()
	try:
		resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
		elapsed = round(time.time() - start, 2)
		resp.raise_for_status()
		data = resp.json()
		logger.info(f"FMP {endpoint}/{ticker} OK in {elapsed}s")
		return data
	except requests.exceptions.Timeout:
		elapsed = round(time.time() - start, 2)
		logger.error(f"FMP {endpoint}/{ticker} TIMEOUT after {elapsed}s")
		return None
	except Exception as e:
		elapsed = round(time.time() - start, 2)
		logger.error(f"FMP {endpoint}/{ticker} FAILED in {elapsed}s: {e}")
		return None


def make_error(ticker: str, message: str):
	return {"success": False, "ticker": ticker, "error": message, "data": None}


def fetch_financial_data(ticker: str):
	total_start = time.time()

	if not FMP_API_KEY:
		return make_error(ticker, "FMP_API_KEY environment variable is not set")

	# --- Check cache ---
	cached = _get_cached(ticker)
	if cached is not None:
		return cached

	# --- Fetch all 3 endpoints in parallel ---
	results = {}
	with ThreadPoolExecutor(max_workers=3) as executor:
		futures = {
			executor.submit(_fmp_get, "profile", ticker): "profile",
			executor.submit(_fmp_get, "key-metrics", ticker): "metrics",
			executor.submit(_fmp_get, "income-statement", ticker): "income",
		}
		for future in as_completed(futures):
			key = futures[future]
			results[key] = future.result()

	profile_list = results.get("profile")
	metrics_list = results.get("metrics")
	income_list = results.get("income")

	# --- Profile is required (price, EPS) ---
	if not profile_list or not isinstance(profile_list, list) or len(profile_list) == 0:
		logger.error(f"Profile unavailable for {ticker}")
		return make_error(ticker, "timeout_or_unavailable")

	profile = profile_list[0]

	if profile.get("price") is None:
		return make_error(ticker, f"Ticker '{ticker}' not found or has no market data")

	# --- Metrics: optional, use defaults ---
	metrics = {}
	if metrics_list and isinstance(metrics_list, list) and len(metrics_list) > 0:
		metrics = metrics_list[0]
	else:
		logger.warning(f"Metrics unavailable for {ticker}, using defaults")

	# --- Income: optional, use defaults ---
	revenue_growth = 0.0
	eps_growth = 0.0
	if income_list and isinstance(income_list, list) and len(income_list) >= 2:
		rev_current = income_list[0].get("revenue") or 0
		rev_previous = income_list[1].get("revenue") or 0
		if rev_previous > 0:
			revenue_growth = ((rev_current - rev_previous) / rev_previous) * 100

		eps_current = income_list[0].get("eps") or 0
		eps_previous = income_list[1].get("eps") or 0
		if eps_previous > 0:
			eps_growth = ((eps_current - eps_previous) / eps_previous) * 100
	else:
		logger.warning(f"Income data unavailable for {ticker}, using defaults")

	# --- Parse fields (same output format) ---
	operating_margin = (profile.get("operatingMargin") or 0) * 100 if profile.get("operatingMargin") and profile.get("operatingMargin") < 1 else (profile.get("operatingMargin") or 0)
	roe = (metrics.get("roe") or 0) * 100 if metrics.get("roe") and abs(metrics.get("roe")) < 5 else (metrics.get("roe") or 0)

	total_cash = profile.get("totalCash") or metrics.get("cashPerShare", 0) * (profile.get("volAvg", 0) or 0)
	total_debt = profile.get("totalDebt") or 0
	fcf_per_share = metrics.get("freeCashFlowPerShare") or 0
	eps = profile.get("eps") or 0

	data = {
		"revenue_growth": round(revenue_growth, 2),
		"operating_margin": round(operating_margin, 2),
		"roe": round(roe, 2),
		"net_cash": total_cash - total_debt,
		"fcf_per_share": round(fcf_per_share, 4),
		"growth": round(eps_growth, 2),
		"eps": eps,
		"current_price": profile.get("price") or 0
	}

	total_elapsed = round(time.time() - total_start, 2)
	logger.info(f"fetch_financial_data({ticker}) completed in {total_elapsed}s")

	result = {"success": True, "ticker": ticker, "error": None, "data": data}

	# --- Store in cache ---
	_set_cached(ticker, result)

	return result
