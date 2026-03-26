import time
import logging
import threading
import yfinance as yf

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
TIMEOUT_SECONDS = 30
CACHE_TTL = 3600  # 1 hour in seconds
RATE_LIMIT_DELAY = 2  # seconds between yfinance calls

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


def _fetch_with_retry(ticker: str):
	"""Fetch yfinance info with retry, rate limiting, and fast_info priority for price."""
	last_error = None

	for attempt in range(1, MAX_RETRIES + 1):
		try:
			_rate_limit_wait()
			stock = yf.Ticker(ticker)

			# Use fast_info first for price data (lighter call)
			fast_price = None
			try:
				fast_info = stock.fast_info
				fast_price = getattr(fast_info, "last_price", None)
				logger.debug(f"fast_info.last_price for {ticker}: {fast_price}")
			except Exception as e:
				logger.debug(f"fast_info unavailable for {ticker}: {e}")

			info = stock.info
			return info, fast_price
		except Exception as e:
			last_error = e
			logger.warning(f"Attempt {attempt}/{MAX_RETRIES} failed for {ticker}: {e}")
			if attempt < MAX_RETRIES:
				wait = 2 ** attempt
				logger.info(f"Retrying in {wait}s...")
				time.sleep(wait)

	raise last_error


def make_error(ticker: str, message: str):
	return {"success": False, "ticker": ticker, "error": message, "data": None}


def fetch_financial_data(ticker: str):
	# --- Check cache ---
	cached = _get_cached(ticker)
	if cached is not None:
		return cached

	# --- Fetch with retry ---
	try:
		info, fast_price = _fetch_with_retry(ticker)
	except Exception as e:
		logger.error(f"All {MAX_RETRIES} attempts failed for {ticker}: {e}")
		return make_error(ticker, f"Yahoo Finance is unreachable after {MAX_RETRIES} attempts")

	# --- Empty/None data ---
	if not info or not isinstance(info, dict):
		return make_error(ticker, f"Yahoo Finance returned empty data for '{ticker}'")

	if info.get("regularMarketPrice") is None and fast_price is None:
		return make_error(ticker, f"Ticker '{ticker}' not found or has no market data")

	logger.debug(f"Raw yfinance data for {ticker}: {info}")

	# --- Parse fields ---
	revenue_growth = (info.get("revenueGrowth") or 0) * 100
	operating_margin = (info.get("operatingMargins") or 0) * 100
	roe = (info.get("returnOnEquity") or 0) * 100
	total_cash = info.get("totalCash") or 0
	total_debt = info.get("totalDebt") or 0
	free_cashflow = info.get("freeCashflow") or 0
	eps = info.get("trailingEps") or 0
	shares = info.get("sharesOutstanding") or 0

	if shares == 0:
		fcf_per_share = 0.0
	else:
		fcf_per_share = free_cashflow / shares

	# Prefer fast_info price, fallback to info
	current_price = fast_price or info.get("currentPrice") or info.get("regularMarketPrice") or 0

	data = {
		"revenue_growth": revenue_growth,
		"operating_margin": operating_margin,
		"roe": roe,
		"net_cash": total_cash - total_debt,
		"fcf_per_share": fcf_per_share,
		"growth": (info.get("earningsGrowth") or 0) * 100,
		"eps": eps,
		"current_price": current_price
	}

	logger.debug(f"Processed data for {ticker}: {data}")

	result = {"success": True, "ticker": ticker, "error": None, "data": data}

	# --- Store in cache ---
	_set_cached(ticker, result)

	return result
