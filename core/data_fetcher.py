import time
import logging
import yfinance as yf

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
TIMEOUT_SECONDS = 30


def _fetch_with_retry(ticker: str):
	"""Fetch yfinance info with retry and timeout."""
	last_error = None

	for attempt in range(1, MAX_RETRIES + 1):
		try:
			stock = yf.Ticker(ticker)
			info = stock.info
			return info
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
	# --- Fetch with retry ---
	try:
		info = _fetch_with_retry(ticker)
	except Exception as e:
		logger.error(f"All {MAX_RETRIES} attempts failed for {ticker}: {e}")
		return make_error(ticker, f"Yahoo Finance is unreachable after {MAX_RETRIES} attempts")

	# --- Empty/None data ---
	if not info or not isinstance(info, dict):
		return make_error(ticker, f"Yahoo Finance returned empty data for '{ticker}'")

	if info.get("regularMarketPrice") is None:
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

	data = {
		"revenue_growth": revenue_growth,
		"operating_margin": operating_margin,
		"roe": roe,
		"net_cash": total_cash - total_debt,
		"fcf_per_share": fcf_per_share,
		"growth": (info.get("earningsGrowth") or 0) * 100,
		"eps": eps,
		"current_price": info.get("currentPrice") or 0
	}

	logger.debug(f"Processed data for {ticker}: {data}")

	return {"success": True, "ticker": ticker, "error": None, "data": data}
