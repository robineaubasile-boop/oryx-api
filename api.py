import os
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from core.scoring import compute_score, get_verdict
from core.pedagogie import generate_analysis
from core.valuation import compute_valuation, valuation_verdict
from core.data_fetcher import fetch_financial_data


def _format_large_number(value, currency="USD"):
	"""Formate un grand nombre en Mds/M lisible.
	Ex: -13109000000 → '-13.1 Mds'
	    5400000 → '5.4 M'
	"""
	if value is None:
		return None
	abs_val = abs(value)
	sign = "-" if value < 0 else ""
	if abs_val >= 1_000_000_000:
		return f"{sign}{abs_val / 1_000_000_000:.1f} Mds {currency}"
	elif abs_val >= 1_000_000:
		return f"{sign}{abs_val / 1_000_000:.1f} M {currency}"
	elif abs_val >= 1_000:
		return f"{sign}{abs_val / 1_000:.1f} K {currency}"
	else:
		return f"{value} {currency}"


app = FastAPI()

app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)


class StockRequest(BaseModel):
	ticker: str

	@field_validator("ticker")
	@classmethod
	def ticker_must_not_be_empty(cls, v):
		v = v.strip().upper()
		if not v:
			raise ValueError("ticker must not be empty")
		return v


@app.get("/health")
def health():
	return {"status": "ok"}


@app.post("/analyze")
def analyze(request: StockRequest):
	ticker = request.ticker
	print(f"[ANALYZE] Received ticker: {ticker}")

	# --- Fetch data ---
	try:
		print(f"[ANALYZE] Fetching data for {ticker}...")
		result = fetch_financial_data(ticker)
	except Exception as e:
		print(f"[ANALYZE ERROR] fetch_financial_data crashed: {type(e).__name__}: {e}")
		return {"success": False, "ticker": ticker, "error": f"Internal error: {e}"}

	if not result["success"]:
		print(f"[ANALYZE] Fetch failed: {result['error']}")
		return {"success": False, "ticker": ticker, "error": result["error"]}

	data = result["data"]
	print(f"[ANALYZE] Data: {data}")

	# --- Scoring ---
	try:
		score = compute_score(data)
		verdict = get_verdict(score)
		analysis = generate_analysis(data)
		print(f"[ANALYZE] Score: {score}, Verdict: {verdict}")
	except Exception as e:
		print(f"[ANALYZE ERROR] Scoring failed: {type(e).__name__}: {e}")
		return {"success": False, "ticker": ticker, "error": f"Scoring error: {e}"}

	# --- Valuation ---
	try:
		fair_value, upside, multiple = compute_valuation(data)
		valo = valuation_verdict(upside)
		print(f"[ANALYZE] Fair value: {fair_value}, Upside: {upside}%, Multiple: {multiple}")
	except Exception as e:
		print(f"[ANALYZE ERROR] Valuation failed: {type(e).__name__}: {e}")
		return {"success": False, "ticker": ticker, "error": f"Valuation error: {e}"}

	# --- P/E réel ---
	eps = data.get("eps")
	current_price = data.get("current_price", 0)
	if eps is not None and eps > 0 and current_price > 0:
		pe_ratio = round(current_price / eps, 2)
	else:
		pe_ratio = None
	print(f"[ANALYZE] P/E ratio: {pe_ratio}")

	return {
		"success": True,
		"ticker": ticker,
		"name": data.get("name", ticker),
		"score": float(score),
		"verdict": verdict,
		"analysis": analysis,
		"multiple": float(multiple),
		"fair_value": round(fair_value, 2),
		"current_price": current_price,
		"pe_ratio": pe_ratio,
		"upside_percent": f"+{round(upside, 1)}" if upside > 0 else str(round(upside, 1)),
		"valuation_verdict": valo,
		"revenue_growth": round(data.get("revenue_growth", 0), 2),
		"operating_margin": round(data.get("operating_margin", 0), 2),
		"roe": round(data.get("roe", 0), 2),
		"fcf_per_share": round(data.get("fcf_per_share", 0), 2),
		"net_cash": _format_large_number(data.get("net_cash", 0), data.get("currency", "USD")),
		"roic": round(data.get("roic", 0), 2),
		"debt_to_equity": round(data.get("debt_to_equity", 0), 2),
		"currency": data.get("currency", "USD"),
		"sector": data.get("sector", "Unknown"),
		"pe_history_avg": data.get("pe_history_avg"),
		"revenue_growth_years": data.get("revenue_growth_years", 0),
		"margin_stability": round(data.get("margin_stability", 0), 2),
		"eps_positive_years": data.get("eps_positive_years", 0),
	}


if __name__ == "__main__":
	port = int(os.environ.get("PORT", 10000))
	uvicorn.run(app, host="0.0.0.0", port=port)
