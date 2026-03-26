import os
import logging
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from core.scoring import compute_score, get_verdict
from core.pedagogie import generate_analysis
from core.valuation import compute_valuation, valuation_verdict
from core.data_fetcher import fetch_financial_data

import yfinance as yf

logging.basicConfig(level=logging.INFO)

app = FastAPI()

app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)


class StockInput(BaseModel):
	revenue_growth: float = Field(ge=-100, le=500, description="Revenue growth (%)")
	operating_margin: float = Field(ge=-100, le=100, description="Operating margin (%)")
	roe: float = Field(ge=-200, le=200, description="Return on equity (%)")
	net_cash: float = Field(description="Net cash (total cash - total debt)")
	fcf_per_share: float = Field(description="Free cash flow per share")
	growth: float = Field(ge=-100, le=500, description="EPS growth (%)")
	current_price: float = Field(gt=0, description="Current stock price")
	eps: float = Field(default=0.0, description="Earnings per share")


class TickerInput(BaseModel):
	ticker: str


@app.get("/health")
def health():
	return {"status": "ok"}


@app.get("/debug-ticker/{ticker}")
def debug_ticker(ticker: str):
	ticker = ticker.upper()
	try:
		stock = yf.Ticker(ticker)
		info = stock.info
		return {
			"success": True,
			"ticker": ticker,
			"keys": list(info.keys()) if info else [],
			"raw": info
		}
	except Exception as e:
		return {
			"success": False,
			"ticker": ticker,
			"error": str(e),
			"error_type": type(e).__name__
		}


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

	result = fetch_financial_data(ticker)

	if not result["success"]:
		return {"success": False, "ticker": ticker, "error": result["error"]}

	data_dict = result["data"]

	# --- QUALITÉ ---
	score = compute_score(data_dict)
	verdict = get_verdict(score)
	analysis = generate_analysis(data_dict)

	# --- VALORISATION ---
	fair_value, upside, multiple = compute_valuation(data_dict)
	valo = valuation_verdict(upside)

	return {
		"success": True,
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
