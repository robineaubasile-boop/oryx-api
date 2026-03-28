import os
import logging
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.scoring import compute_score, get_verdict
from core.pedagogie import generate_analysis
from core.valuation import compute_valuation, valuation_verdict
from core.data_fetcher import fetch_financial_data, _fmp_get, FMP_API_KEY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


@app.get("/health")
def health():
	return {"status": "ok"}


@app.get("/debug-ticker/{ticker}")
def debug_ticker(ticker: str):
	ticker = ticker.upper()
	if not FMP_API_KEY:
		return {"success": False, "ticker": ticker, "error": "FMP_API_KEY not set"}
	try:
		profile = _fmp_get("profile", ticker)
		metrics = _fmp_get("key-metrics", ticker)
		income = _fmp_get("income-statement", ticker)
		return {
			"success": True,
			"ticker": ticker,
			"profile": profile[0] if profile else None,
			"profile_keys": list(profile[0].keys()) if profile else [],
			"metrics": metrics[0] if metrics else None,
			"metrics_keys": list(metrics[0].keys()) if metrics else [],
			"income_latest": income[0] if income else None,
		}
	except Exception as e:
		return {
			"success": False,
			"ticker": ticker,
			"error": str(e),
			"error_type": type(e).__name__
		}


@app.post("/analyze")
def analyze(request: StockRequest):
	ticker = request.ticker.upper()
	print(f"[ANALYZE] Received ticker: {ticker}")

	# --- Fetch data from FMP ---
	print(f"[ANALYZE] Calling fetch_financial_data({ticker})")
	result = fetch_financial_data(ticker)

	if not result["success"]:
		print(f"[ANALYZE] Fetch failed: {result['error']}")
		return {"success": False, "ticker": ticker, "error": result["error"]}

	data_dict = result["data"]
	print(f"[ANALYZE] Data retrieved: {data_dict}")

	# --- QUALITÉ ---
	score = compute_score(data_dict)
	verdict = get_verdict(score)
	analysis = generate_analysis(data_dict)
	print(f"[ANALYZE] Score: {score}, Verdict: {verdict}")

	# --- VALORISATION ---
	fair_value, upside, multiple = compute_valuation(data_dict)
	valo = valuation_verdict(upside)
	print(f"[ANALYZE] Fair value: {fair_value}, Upside: {upside}%, Multiple: {multiple}")

	return {
		"success": True,
		"ticker": ticker,
		"score": float(score),
		"verdict": verdict,
		"analysis": analysis,
		"multiple": float(multiple),
		"fair_value": round(fair_value, 2) if fair_value is not None else None,
		"current_price": data_dict.get("current_price"),
		"upside_percent": round(upside, 1) if upside is not None else None,
		"valuation_verdict": valo,
		"revenue_growth": data_dict.get("revenue_growth"),
		"operating_margin": data_dict.get("operating_margin"),
		"roe": data_dict.get("roe"),
		"fcf_per_share": data_dict.get("fcf_per_share"),
		"net_cash": data_dict.get("net_cash")
	}


@app.post("/analyze-ticker")
def analyze_ticker(request: StockRequest):
	"""Alias for /analyze — kept for backwards compatibility."""
	return analyze(request)


if __name__ == "__main__":
	port = int(os.environ.get("PORT", 10000))
	uvicorn.run(app, host="0.0.0.0", port=port)
