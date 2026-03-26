import logging
import yfinance as yf

logger = logging.getLogger(__name__)


class TickerNotFoundError(Exception):
	"""Raised when a ticker returns no data from Yahoo Finance."""
	pass


class YahooFinanceError(Exception):
	"""Raised when Yahoo Finance is unreachable or returns an error."""
	pass


def fetch_financial_data(ticker: str):
	try:
		stock = yf.Ticker(ticker)
		info = stock.info
	except Exception as e:
		logger.error(f"Yahoo Finance request failed for {ticker}: {e}")
		raise YahooFinanceError(f"Could not fetch data from Yahoo Finance for '{ticker}'")

	if not info or info.get("regularMarketPrice") is None:
		raise TickerNotFoundError(f"Ticker '{ticker}' not found or returned no data")

	logger.debug(f"Raw yfinance data for {ticker}: {info}")

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

	return data
