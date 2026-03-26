import os
import time
import logging
import threading
import requests

logger = logging.getLogger(__name__)

FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

MAX_RETRIES = 3
REQUEST_TIMEOUT = 30
CACHE_TTL = 3600  # 1 hour
RATE_LIMIT_DELAY = 2  # seconds between FMP calls

# --- In-memory cache ---
_cache = {}  # {ticker: {"timestamp": float, "result": dict}}
_cache_lock = threading.Lock()

# --- Rate limiter ---
_last_call_time = 0.0
_rate_lock = threading.Lock()


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


def _rate_limit_wait():
	global _last_call_time
	with _rate_lock:
		now = time.time()
		elapsed = now - _last_call_time
		if elapsed < RATE_LIMIT_DELAY:
			wait = RATE_LIMIT_DELAY - elapsed
			logger.debug(f"Rate limit: waiting {wait:.1f}s")
			time.sleep(wait)
		_last_call_time = time.time()


def _fmp_get(endpoint: str, ticker: str):
	"""GET request to FMP API with retry and rate limiting."""
	url = f"{FMP_BASE_URL}/{endpoint}/{ticker}"
	params = {"apikey": FMP_API_KEY}
	last_error = None

	for attempt in range(1, MAX_RETRIES + 1):
		try:
			_rate_limit_wait()
			resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
			resp.raise_for_status()
			data = resp.json()
			return data
		except Exception as e:
			last_error = e
			logger.warning(f"FMP {endpoint}/{ticker} attempt {attempt}/{MAX_RETRIES} failed: {e}")
			if attempt < MAX_RETRIES:
				wait = 2 ** attempt
				logger.info(f"Retrying in {wait}s...")
				time.sleep(wait)

	raise last_error


def make_error(ticker: str, message: str):
	return {"success": False, "ticker": ticker, "error": message, "data": None}


def fetch_financial_data(ticker: str):
	if not FMP_API_KEY:
		return make_error(ticker, "FMP_API_KEY environment variable is not set")

	# --- Check cache ---
	cached = _get_cached(ticker)
	if cached is not None:
		return cached

	# --- Fetch profile ---
	try:
		profile_list = _fmp_get("profile", ticker)
	except Exception as e:
		logger.error(f"All {MAX_RETRIES} attempts failed for profile/{ticker}: {e}")
		return make_error(ticker, f"FMP API is unreachable after {MAX_RETRIES} attempts")

	if not profile_list or not isinstance(profile_list, list) or len(profile_list) == 0:
		return make_error(ticker, f"Ticker '{ticker}' not found on FMP")

	profile = profile_list[0]

	if profile.get("price") is None:
		return make_error(ticker, f"Ticker '{ticker}' has no market data on FMP")

	# --- Fetch key metrics ---
	try:
		metrics_list = _fmp_get("key-metrics", ticker)
	except Exception as e:
		logger.warning(f"key-metrics failed for {ticker}, using defaults: {e}")
		metrics_list = []

	metrics = metrics_list[0] if metrics_list and isinstance(metrics_list, list) and len(metrics_list) > 0 else {}

	# --- Fetch income statements (for revenue growth) ---
	try:
		income_list = _fmp_get("income-statement", ticker)
	except Exception as e:
		logger.warning(f"income-statement failed for {ticker}, using defaults: {e}")
		income_list = []

	# Calculate revenue growth from last 2 years
	revenue_growth = 0.0
	if isinstance(income_list, list) and len(income_list) >= 2:
		rev_current = income_list[0].get("revenue") or 0
		rev_previous = income_list[1].get("revenue") or 0
		if rev_previous > 0:
			revenue_growth = ((rev_current - rev_previous) / rev_previous) * 100

	# Calculate EPS growth from last 2 years
	eps_growth = 0.0
	if isinstance(income_list, list) and len(income_list) >= 2:
		eps_current = income_list[0].get("eps") or 0
		eps_previous = income_list[1].get("eps") or 0
		if eps_previous > 0:
			eps_growth = ((eps_current - eps_previous) / eps_previous) * 100

	logger.debug(f"FMP profile for {ticker}: {profile}")
	logger.debug(f"FMP metrics for {ticker}: {metrics}")

	# --- Parse fields (same output format as before) ---
	operating_margin = (profile.get("operatingMargin") or 0) * 100 if profile.get("operatingMargin") and profile.get("operatingMargin") < 1 else (profile.get("operatingMargin") or 0)
	roe = (metrics.get("roe") or 0) * 100 if metrics.get("roe") and abs(metrics.get("roe")) < 5 else (metrics.get("roe") or 0)

	total_cash = profile.get("totalCash") or metrics.get("cashPerShare", 0) * (profile.get("volAvg", 0) or 0)
	total_debt = profile.get("totalDebt") or 0
	free_cashflow = metrics.get("freeCashFlowPerShare", 0) * (profile.get("sharesOutstanding") or 1) if metrics.get("freeCashFlowPerShare") else 0
	eps = profile.get("eps") or 0
	shares = profile.get("sharesOutstanding") or 0
	fcf_per_share = metrics.get("freeCashFlowPerShare") or 0

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

	logger.debug(f"Processed data for {ticker}: {data}")

	result = {"success": True, "ticker": ticker, "error": None, "data": data}

	# --- Store in cache ---
	_set_cached(ticker, result)

	return result
