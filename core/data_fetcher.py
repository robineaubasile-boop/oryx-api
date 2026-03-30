import os
import requests

FMP_API_KEY = os.getenv("FMP_API_KEY")


def _safe_get(url):
	try:
		response = requests.get(url, timeout=5)
		if response.status_code == 200:
			data = response.json()
			if isinstance(data, list) and len(data) > 0:
				return data[0]
		return {}
	except Exception:
		return {}


def _num(value):
	try:
		return float(value)
	except:
		return 0.0


def fetch_financial_data(ticker: str):
	ticker = ticker.upper()

	# 🔗 Endpoints FMP
	profile_url = f"https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={FMP_API_KEY}"
	ratios_url = f"https://financialmodelingprep.com/api/v3/ratios/{ticker}?apikey={FMP_API_KEY}"
	growth_url = f"https://financialmodelingprep.com/api/v3/financial-growth/{ticker}?apikey={FMP_API_KEY}"

	profile = _safe_get(profile_url)
	ratios = _safe_get(ratios_url)
	growth = _safe_get(growth_url)

	# 📊 Extraction sécurisée
	current_price = _num(profile.get("price"))
	eps = _num(profile.get("eps"))

	revenue_growth = _num(growth.get("revenueGrowth")) * 100
	earnings_growth = _num(growth.get("epsgrowth")) * 100

	operating_margin = _num(ratios.get("operatingProfitMargin")) * 100
	roe = _num(ratios.get("returnOnEquity")) * 100
	fcf_per_share = _num(ratios.get("freeCashFlowPerShare"))

	# Net cash pas dispo direct → fallback simple
	net_cash = 0

	data = {
		"current_price": current_price,
		"revenue_growth": revenue_growth,
		"operating_margin": operating_margin,
		"roe": roe,
		"net_cash": net_cash,
		"fcf_per_share": fcf_per_share,
		"growth": earnings_growth,
		"eps": eps
	}

	# ✅ Success logique (même partiel)
	success = any(value != 0 for value in data.values())

	return {
	"success": success,
	"ticker": ticker,
	**data
	}
