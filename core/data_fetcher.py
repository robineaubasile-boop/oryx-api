import yfinance as yf
def fetch_financial_data(ticker: str):

	stock = yf.Ticker(ticker)
	info = stock.info
	print(info)

	revenue_growth = (info.get("revenueGrowth") or 0) * 100
	operating_margin = (info.get("operatingMargins") or 0) * 100
	roe = (info.get("returnOnEquity") or 0) * 100
	total_cash = info.get("totalCash") or 0
	total_debt = info.get("totalDebt") or 0
	free_cashflow = info.get("freeCashflow") or 0
	eps = info.get("trailingEps") or 0
	shares = info.get("sharesOutstanding") or 1

	data = {
		"revenue_growth": revenue_growth,
		"operating_margin": operating_margin,
		"roe": roe,
		"net_cash": total_cash - total_debt,
		"fcf_per_share": free_cashflow / shares,
		"growth": (info.get("earningsGrowth") or 0) * 100,
		"eps": eps,
		"current_price": info.get("currentPrice") or 0
	}

	print(data)

	return data
