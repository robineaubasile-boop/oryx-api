import os
import logging
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from core.scoring import compute_score, get_verdict
from core.pedagogie import generate_analysis
from core.valuation import compute_valuation, valuation_verdict
from core.data_fetcher import fetch_financial_data, TickerNotFoundError, YahooFinanceError

logging.basicConfig(level=logging.INFO)

app = FastAPI()


class StockInput(BaseModel):
	revenue_growth: float
	operating_margin: float
	roe: float
	net_cash: float
	fcf_per_share: float
	growth: float
	current_price: float
	eps: float = 0.0


class TickerInput(BaseModel):
	ticker: str


@app.get("/health")
def health():
	return {"status": "ok"}


@app.post("/analyze")
def analyze_stock(data: StockInput):
	data_dict = data.model_dump()

	# --- QUALITÉ ---
	score = compute_score(data_dict)
	verdict = get_verdict(score)
	analysis = generate_analysis(data_dict)

	# --- VALORISATION ---
	fair_value, upside, multiple = compute_valuation(data_dict)
	valo = valuation_verdict(upside)

	return {
		"score": score,
		"verdict": verdict,
		"analysis": analysis,
		"multiple": multiple,
		"fair_value": round(fair_value, 2),
		"current_price": data_dict["current_price"],
		"upside_percent": round(upside, 1),
		"valuation_verdict": valo
	}


@app.post("/analyze-ticker")
def analyze_from_ticker(input: TickerInput):
	ticker = input.ticker.upper()

	try:
		data_dict = fetch_financial_data(ticker)
	except TickerNotFoundError:
		raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' not found on Yahoo Finance")
	except YahooFinanceError:
		raise HTTPException(status_code=502, detail="Yahoo Finance is unavailable, please try again later")

	# --- QUALITÉ ---
	score = compute_score(data_dict)
	verdict = get_verdict(score)
	analysis = generate_analysis(data_dict)

	# --- VALORISATION ---
	fair_value, upside, multiple = compute_valuation(data_dict)
	valo = valuation_verdict(upside)

	return {
		"ticker": str(ticker),
		"score": float(score),
		"verdict": str(verdict),
		"analysis": analysis,
		"multiple": float(multiple),
		"fair_value": float(round(fair_value, 2)),
		"current_price": float(data_dict["current_price"]),
		"upside_percent": float(round(upside, 1)),
		"valuation_verdict": str(valo),
		"revenue_growth": float(data_dict["revenue_growth"]),
		"operating_margin": float(data_dict["operating_margin"]),
		"roe": float(data_dict["roe"]),
		"fcf_per_share": float(data_dict["fcf_per_share"]),
		"net_cash": float(data_dict["net_cash"])
	}


if __name__ == "__main__":
	port = int(os.environ.get("PORT", 10000))
	uvicorn.run(app, host="0.0.0.0", port=port)
