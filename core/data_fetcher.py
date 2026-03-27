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

	print(f"[FMP DEBUG] Calling: {url}?apikey={FMP_API_KEY[:8]}...")

	start = time.time()
	try:
		resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
		elapsed = round(time.time() - start, 2)
		print(f"[FMP DEBUG] {endpoint}/{ticker} status_code={resp.status_code} elapsed={elapsed}s")
		print(f"[FMP DEBUG] {endpoint}/{ticker} response (first 500 chars): {resp.text[:500]}")
		resp.raise_for_status()
		data = resp.json()
		print(f"[FMP DEBUG] {endpoint}/{ticker} parsed OK — type={type(data).__name__}, length={len(data) if isinstance(data, list) else 'N/A'}")
		return data
	except requests.exceptions.Timeout:
		elapsed = round(time.time() - start, 2)
		print(f"[FMP ERROR] {endpoint}/{ticker} TIMEOUT after {elapsed}s")
		logger.error(f"FMP {endpoint}/{ticker} TIMEOUT after {elapsed}s")
		return None
	except requests.exceptions.ProxyError as e:
		elapsed = round(time.time() - start, 2)
		print(f"[FMP ERROR] {endpoint}/{ticker} PROXY BLOCKED after {elapsed}s: {e}")
		logger.error(f"FMP {endpoint}/{ticker} PROXY BLOCKED: {e}")
		return None
	except requests.exceptions.ConnectionError as e:
		elapsed = round(time.time() - start, 2)
		print(f"[FMP ERROR] {endpoint}/{ticker} CONNECTION ERROR after {elapsed}s: {e}")
		logger.error(f"FMP {endpoint}/{ticker} CONNECTION ERROR: {e}")
		return None
	except requests.exceptions.HTTPError as e:
		elapsed = round(time.time() - start, 2)
		print(f"[FMP ERROR] {endpoint}/{ticker} HTTP ERROR {resp.status_code} after {elapsed}s: {resp.text[:300]}")
		logger.error(f"FMP {endpoint}/{ticker} HTTP {resp.status_code}: {e}")
		return None
	except Exception as e:
		elapsed = round(time.time() - start, 2)
		print(f"[FMP ERROR] {endpoint}/{ticker} UNEXPECTED ERROR after {elapsed}s: {type(e).__name__}: {e}")
		logger.error(f"FMP {endpoint}/{ticker} FAILED in {elapsed}s: {e}")
		return None


def _safe_get(source, key, default=None):
	"""Safely get a value from a dict, returning default if source is None."""
	if source is None:
		return default
	return source.get(key, default)


def _parse_profile(profile):
	"""Extract data from profile, returns dict with None for missing values."""
	if not profile:
		logger.warning("PROFILE: None")
		return {}

	logger.info(f"PROFILE: price={profile.get('price')}, eps={profile.get('eps')}, margin={profile.get('operatingMargin')}")

	result = {}
	result["current_price"] = profile.get("price")
	result["eps"] = profile.get("eps")

	margin_raw = profile.get("operatingMargin")
	if margin_raw is not None:
		result["operating_margin"] = round(margin_raw * 100, 2) if abs(margin_raw) < 1 else round(margin_raw, 2)
	else:
		result["operating_margin"] = None

	result["total_cash"] = profile.get("totalCash")
	result["total_debt"] = profile.get("totalDebt")
	result["shares"] = profile.get("sharesOutstanding")

	return result


def _parse_metrics(metrics):
	"""Extract data from key-metrics, returns dict with None for missing values."""
	if not metrics:
		logger.warning("METRICS: None")
		return {}

	logger.info(f"METRICS: roe={metrics.get('roe')}, fcf/share={metrics.get('freeCashFlowPerShare')}, cashPerShare={metrics.get('cashPerShare')}")

	result = {}

	roe_raw = metrics.get("roe")
	if roe_raw is not None:
		result["roe"] = round(roe_raw * 100, 2) if abs(roe_raw) < 5 else round(roe_raw, 2)
	else:
		result["roe"] = None

	result["fcf_per_share"] = metrics.get("freeCashFlowPerShare")
	result["cash_per_share"] = metrics.get("cashPerShare")

	return result


def _parse_income(income_list):
	"""Extract revenue/EPS growth from income statements."""
	if not income_list or not isinstance(income_list, list) or len(income_list) < 2:
		logger.warning(f"INCOME: None or insufficient data (got {len(income_list) if isinstance(income_list, list) else 0} entries)")
		return {}

	logger.info(f"INCOME: latest revenue={income_list[0].get('revenue')}, eps={income_list[0].get('eps')}")

	result = {}

	rev_current = income_list[0].get("revenue") or 0
	rev_previous = income_list[1].get("revenue") or 0
	if rev_previous > 0:
		result["revenue_growth"] = round(((rev_current - rev_previous) / rev_previous) * 100, 2)
	else:
		result["revenue_growth"] = None

	eps_current = income_list[0].get("eps") or 0
	eps_previous = income_list[1].get("eps") or 0
	if eps_previous > 0:
		result["growth"] = round(((eps_current - eps_previous) / eps_previous) * 100, 2)
	else:
		result["growth"] = None

	return result


def fetch_financial_data(ticker: str):
	total_start = time.time()

	print(f"[FETCH DEBUG] Starting fetch for {ticker}")
	print(f"[FETCH DEBUG] FMP_API_KEY set: {bool(FMP_API_KEY)} (length: {len(FMP_API_KEY)})")

	if not FMP_API_KEY:
		print(f"[FETCH ERROR] FMP_API_KEY is empty!")
		return {"success": False, "ticker": ticker, "error": "FMP_API_KEY environment variable is not set", "data": None}

	# --- Check cache ---
	cached = _get_cached(ticker)
	if cached is not None:
		return cached

	# --- Fetch all 3 endpoints in parallel (each independent) ---
	raw = {}
	with ThreadPoolExecutor(max_workers=3) as executor:
		futures = {
			executor.submit(_fmp_get, "profile", ticker): "profile",
			executor.submit(_fmp_get, "key-metrics", ticker): "metrics",
			executor.submit(_fmp_get, "income-statement", ticker): "income",
		}
		for future in as_completed(futures):
			key = futures[future]
			raw[key] = future.result()

	# --- Parse each source independently ---
	profile_list = raw.get("profile")
	metrics_list = raw.get("metrics")
	income_list = raw.get("income")

	print(f"[FETCH DEBUG] Raw results — profile: {type(profile_list).__name__}({len(profile_list) if isinstance(profile_list, list) else 'None'}), metrics: {type(metrics_list).__name__}({len(metrics_list) if isinstance(metrics_list, list) else 'None'}), income: {type(income_list).__name__}({len(income_list) if isinstance(income_list, list) else 'None'})")

	profile = profile_list[0] if profile_list and isinstance(profile_list, list) and len(profile_list) > 0 else None
	metrics = metrics_list[0] if metrics_list and isinstance(metrics_list, list) and len(metrics_list) > 0 else None

	profile_data = _parse_profile(profile)
	metrics_data = _parse_metrics(metrics)
	income_data = _parse_income(income_list)

	# --- Build output: each field independent, None if unavailable ---
	current_price = profile_data.get("current_price")
	eps = profile_data.get("eps")
	operating_margin = profile_data.get("operating_margin")
	total_cash = profile_data.get("total_cash")
	total_debt = profile_data.get("total_debt")

	roe = metrics_data.get("roe")
	fcf_per_share = metrics_data.get("fcf_per_share")

	revenue_growth = income_data.get("revenue_growth")
	eps_growth = income_data.get("growth")

	# net_cash: compute if both available, else None
	if total_cash is not None and total_debt is not None:
		net_cash = total_cash - total_debt
	elif total_cash is not None:
		net_cash = total_cash
	else:
		net_cash = None

	data = {
		"revenue_growth": revenue_growth,
		"operating_margin": operating_margin,
		"roe": roe,
		"net_cash": net_cash,
		"fcf_per_share": fcf_per_share,
		"growth": eps_growth,
		"eps": eps,
		"current_price": current_price
	}

	# --- Count available fields ---
	available = sum(1 for v in data.values() if v is not None)
	total_fields = len(data)

	total_elapsed = round(time.time() - total_start, 2)
	logger.info(f"fetch_financial_data({ticker}) completed in {total_elapsed}s — {available}/{total_fields} fields available")

	print(f"[FETCH DEBUG] Final data: {data}")
	print(f"[FETCH DEBUG] Available fields: {available}/{total_fields}")

	# --- Only fail if ZERO data available ---
	if available == 0:
		print(f"[FETCH ERROR] ZERO fields available for {ticker} — returning error")
		return {"success": False, "ticker": ticker, "error": "All data sources failed, no data available", "data": None}

	result = {"success": True, "ticker": ticker, "error": None, "data": data}

	# --- Cache only if we got data ---
	_set_cached(ticker, result)

	return result
