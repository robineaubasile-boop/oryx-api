import os
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

from core.scoring import compute_score, get_verdict
from core.pedagogie import generate_analysis
from core.valuation import compute_valuation, valuation_verdict
from core.data_fetcher import fetch_financial_data, fetch_etf_data
from core.ticker_resolver import normalize_ticker
from core.pedagogie_library import lookup_method, METHODES
import anthropic
from core.decryptage_engine import build_system_prompt, build_user_message
from core.education_engine import build_system_prompt as build_education_prompt, build_user_message as build_education_user_message
from core.coach_engine import build_system_prompt as build_coach_prompt, build_user_message as build_coach_user_message
from core.portfolio_analysis_engine import build_system_prompt as build_portfolio_analysis_prompt, build_user_message as build_portfolio_analysis_user_message
from core.checklist_engine import build_system_prompt as build_checklist_prompt, build_user_message as build_checklist_user_message
from core.market_lookup import search_market


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL_DECRYPTAGE = os.getenv("CLAUDE_MODEL_DECRYPTAGE", "claude-sonnet-4-5-20251001")
CLAUDE_MODEL_EDUCATION = os.getenv("CLAUDE_MODEL_EDUCATION", "claude-sonnet-4-6")
CLAUDE_MODEL_COACH = os.getenv("CLAUDE_MODEL_COACH", "claude-sonnet-4-6")
CLAUDE_MODEL_CHECKLIST = os.getenv("CLAUDE_MODEL_CHECKLIST", "claude-sonnet-4-6")
CLAUDE_MODEL_CLASSIFIER = os.getenv("CLAUDE_MODEL_CLASSIFIER", "claude-haiku-4-5-20251001")
CLAUDE_MODEL_PORTFOLIO = os.getenv("CLAUDE_MODEL_PORTFOLIO", "claude-sonnet-4-6")


def _extract_text(response) -> str:
	"""Extrait le texte d'une réponse Claude, en ignorant les blocs thinking éventuels."""
	for block in response.content:
		if block.type == "text":
			return block.text
	return ""


def _safe(val, default=0):
	"""Retourne default si val est None."""
	return val if val is not None else default


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


def _format_aum(value, currency="USD"):
	if value is None:
		return None
	abs_val = abs(value)
	if abs_val >= 1_000_000_000:
		return f"{abs_val / 1_000_000_000:.1f} Mds {currency}"
	elif abs_val >= 1_000_000:
		return f"{abs_val / 1_000_000:.0f} M {currency}"
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
	raw_ticker = request.ticker
	ticker = normalize_ticker(raw_ticker)
	print(f"[ANALYZE] Received: '{raw_ticker}' → resolved: '{ticker}'")

	# --- Fetch data ---
	try:
		print(f"[ANALYZE] Fetching data for {ticker}...")
		result = fetch_financial_data(ticker)
	except Exception as e:
		print(f"[ANALYZE ERROR] fetch_financial_data crashed: {type(e).__name__}: {e}")
		return {"success": False, "ticker": ticker, "error": f"Erreur interne : {e}"}

	if not result["success"]:
		print(f"[ANALYZE] Fetch failed: {result['error']}")
		return {"success": False, "ticker": ticker, "error": result["error"]}

	data = result["data"]
	print(f"[ANALYZE] Data: {data}")

	# --- Detect ETF and redirect ---
	if data.get("name") and not data.get("revenue_growth") and not data.get("operating_margin") and not data.get("roe"):
		return {"success": False, "ticker": ticker, "error": "Cet actif semble etre un ETF. Utilisez la commande Analyse ETF."}

	# --- Scoring ---
	try:
		score = compute_score(data)
		verdict = get_verdict(score)
		analysis = generate_analysis(data)
		print(f"[ANALYZE] Score: {score}, Verdict: {verdict}")
	except Exception as e:
		print(f"[ANALYZE ERROR] Scoring failed: {type(e).__name__}: {e}")
		return {"success": False, "ticker": ticker, "error": f"Erreur de scoring : {e}"}

	# --- Valuation ---
	try:
		valuation = compute_valuation(data)
		fair_value = valuation["fair_value"]
		upside = valuation["upside"]
		multiple = valuation["multiple"]
		valo = valuation_verdict(upside)
		print(f"[ANALYZE] Fair value: {fair_value}, Upside: {upside}%, Multiple: {multiple}")
	except Exception as e:
		print(f"[ANALYZE ERROR] Valuation failed: {type(e).__name__}: {e}")
		return {"success": False, "ticker": ticker, "error": f"Erreur de valorisation : {e}"}

	# --- P/E réel ---
	pe_ratio = round(valuation["pe_ratio"], 2) if valuation["pe_ratio"] > 0 else None
	print(f"[ANALYZE] P/E ratio: {pe_ratio}")

	if not data.get("current_price") and not data.get("revenue_growth") and not data.get("operating_margin") and not data.get("roe"):
		return {"success": False, "ticker": ticker, "error": "Cette entreprise n'est pas couverte par nos sources de données. Vérifiez le ticker ou essayez un autre actif."}

	# --- Détection données partielles ---
	_missing_count = sum(1 for k in ("roic", "debt_to_equity", "net_cash") if data.get(k) is None)
	data_warning = "⚠️ Données partielles — certaines métriques (ROIC, D/E, trésorerie) ne sont pas disponibles pour ce ticker. Le score Oryx peut être sous-estimé." if _missing_count >= 2 else None

	return {
		"success": True,
		"ticker": ticker,
		"name": data.get("name", ticker),
		"score": float(score),
		"verdict": verdict,
		"analysis": analysis,
		"multiple": float(multiple),
		"multiple_raw": float(valuation["multiple_raw"]),
		"multiple_capped": valuation["multiple_capped"],
		"fair_value": round(fair_value, 2),
		"current_price": data.get("current_price", 0),
		"pe_ratio": pe_ratio,
		"upside_percent": f"+{round(upside, 1)}" if upside and upside > 0 else str(round(_safe(upside), 1)),
		"upside_raw": round(valuation["upside_raw"], 1),
		"upside_capped": valuation["upside_capped"],
		"cap_reason": valuation["cap_reason"],
		"valuation_verdict": valo,
		"revenue_growth": round(_safe(data.get("revenue_growth")), 2),
		"operating_margin": round(_safe(data.get("operating_margin")), 2),
		"roe": round(_safe(data.get("roe")), 2),
		"fcf_per_share": round(_safe(data.get("fcf_per_share")), 2),
		"net_cash": _format_large_number(data.get("net_cash", 0), data.get("currency", "USD")),
		"roic": round(_safe(data.get("roic")), 2),
		"debt_to_equity": round(_safe(data.get("debt_to_equity")), 2),
		"currency": data.get("currency", "USD"),
		"sector": data.get("sector", "Unknown"),
		"pe_history_avg": data.get("pe_history_avg"),
		"revenue_growth_years": data.get("revenue_growth_years", 0),
		"margin_stability": round(_safe(data.get("margin_stability")), 2),
		"eps_positive_years": data.get("eps_positive_years", 0),
		"fcf_vs_net_income": data.get("fcf_vs_net_income"),
		"gross_margin_trend": data.get("gross_margin_trend"),
		"receivables_vs_revenue": data.get("receivables_vs_revenue"),
		"data_warning": data_warning,
	}


@app.post("/analyze-etf")
def analyze_etf(request: StockRequest):
	raw_ticker = request.ticker
	ticker = normalize_ticker(raw_ticker)
	print(f"[ETF] Received: '{raw_ticker}' → resolved: '{ticker}'")

	try:
		result = fetch_etf_data(ticker)
	except Exception as e:
		print(f"[ETF ERROR] fetch_etf_data crashed: {type(e).__name__}: {e}")
		return {"success": False, "ticker": ticker, "error": f"Erreur interne : {e}"}

	if not result["success"]:
		return {"success": False, "ticker": ticker, "error": result["error"]}

	data = result["data"]
	print(f"[ETF] Data parsed for {ticker}")

	# Formater le top 10 en string lisible pour le prompt
	top_10_str = ""
	for h in data.get("top_10_holdings", []):
		pct = h.get("assets_pct", 0)
		top_10_str += f"• {h['name']} ({h['code']}) : {pct} %\n"

	# Formater sectors en string
	sectors_str = ""
	for sector, pct in sorted(data.get("sector_weights", {}).items(), key=lambda x: x[1], reverse=True):
		sectors_str += f"• {sector} : {pct} %\n"

	# Formater regions en string
	regions_str = ""
	for region, pct in sorted(data.get("world_regions", {}).items(), key=lambda x: x[1], reverse=True):
		regions_str += f"• {region} : {pct} %\n"

	# Formater market cap en string
	cap_str = ""
	cap_order = ["Mega", "Big", "Medium", "Small", "Micro"]
	for size in cap_order:
		pct = data.get("market_cap_breakdown", {}).get(size)
		if pct:
			cap_str += f"• {size} : {pct} %\n"

	return {
		"success": True,
		"ticker": ticker,
		"type": "ETF",
		"name": data.get("name", ticker),
		"currency": data.get("currency", "USD"),
		"current_price": data.get("current_price", 0),
		"category": data.get("category", "Unknown"),
		"isin": data.get("isin"),
		"index_name": data.get("index_name"),
		"inception_date": data.get("inception_date"),
		"aum": _format_aum(data.get("total_assets"), data.get("currency", "USD")),
		"holdings_count": data.get("holdings_count"),
		"yield_pct": data.get("yield_pct"),
		"ter": data.get("ter"),
		"returns_ytd": data.get("returns_ytd"),
		"returns_1y": data.get("returns_1y"),
		"returns_3y": data.get("returns_3y"),
		"returns_5y": data.get("returns_5y"),
		"returns_10y": data.get("returns_10y"),
		"volatility_1y": data.get("volatility_1y"),
		"volatility_3y": data.get("volatility_3y"),
		"sharpe_3y": data.get("sharpe_3y"),
		"top_10_holdings": top_10_str.strip(),
		"sector_weights": sectors_str.strip(),
		"world_regions": regions_str.strip(),
		"market_cap_breakdown": cap_str.strip(),
		"pe_portfolio": data.get("pe_portfolio"),
		"pb_portfolio": data.get("pb_portfolio"),
		"pe_category": data.get("pe_category"),
		"pb_category": data.get("pb_category"),
		"morningstar_rating": data.get("morningstar_rating"),
		"morningstar_benchmark": data.get("morningstar_benchmark"),
	}


class DecryptageRequest(BaseModel):
	ticker: str
	question: str = ""
	context: str = ""

	@field_validator("ticker")
	@classmethod
	def ticker_must_not_be_empty_decryptage(cls, v):
		v = v.strip().upper()
		if not v:
			raise ValueError("ticker must not be empty")
		return v


class PedagogieRequest(BaseModel):
	question: str
	context: str = ""


class EducationRequest(BaseModel):
	question: str
	context: str = ""


class CoachRequest(BaseModel):
	question: str
	context: str = ""
	tickers: str = ""
	portfolio: str = ""


class ChecklistRequest(BaseModel):
	ticker: str
	question: str = ""
	context: str = ""

	@field_validator("ticker")
	@classmethod
	def ticker_must_not_be_empty_checklist(cls, v):
		v = v.strip().upper()
		if not v:
			raise ValueError("ticker must not be empty")
		return v


@app.post("/pedagogie/lookup")
def pedagogie_lookup(request: PedagogieRequest):
	question = request.question
	context = request.context or ""
	print(f"[PEDAGOGIE] Received question: '{question}'")

	result = lookup_method(question, context)

	if result is None:
		return {
			"has_method": False,
			"method_id": None,
			"method_title": None,
			"method_content": None,
			"method_keywords_matched": [],
			"example_company": None,
		}

	return {
		"has_method": True,
		"method_id": result["method_id"],
		"method_title": result["title"],
		"method_content": result["method_content"],
		"method_keywords_matched": result["keywords_matched"],
		"example_company": result["example_company"],
	}


def _force_construction_these_method() -> dict:
	"""Retourne directement la méthode construction_these (Méthode 8), sans
	passer par le matching par mot-clé de lookup_method(). Utilisé pour
	garantir cette méthode sur le message déclencheur initial d'une analyse
	decryptage (context vide = pas d'historique de conversation)."""
	m = METHODES["construction_these"]
	return {
		"method_id": "construction_these",
		"title": m["title"],
		"method_content": m["method_content"],
		"example_company": m["example_company"],
		"keywords_matched": [],
	}


@app.post("/decryptage")
def decryptage(request: DecryptageRequest):
	raw_ticker = request.ticker
	question = request.question.strip()
	context = request.context.strip()
	ticker = normalize_ticker(raw_ticker)
	print(f"[DECRYPTAGE] '{raw_ticker}' → '{ticker}' | question: '{question or '(none)'}'")

	try:
		result = fetch_financial_data(ticker)
	except Exception as e:
		print(f"[DECRYPTAGE ERROR] fetch crashed: {type(e).__name__}: {e}")
		return {"success": False, "ticker": ticker, "error": str(e)}

	if not result["success"]:
		return {"success": False, "ticker": ticker, "error": result["error"]}

	data = result["data"]
	company_name = data.get("name", ticker)
	print(f"[DECRYPTAGE] Data OK pour {company_name}")

	lookup_text = question if question else f"analyser bilan états financiers {company_name}"
	if not context:
		method = _force_construction_these_method()
	else:
		method = lookup_method(lookup_text, context=context)
	print(f"[DECRYPTAGE] Méthode: {method['method_id'] if method else 'aucune'}")

	system_prompt = build_system_prompt(data, method)
	user_message = build_user_message(
		question if question else f"Aide-moi à analyser {company_name} ({ticker}).",
		context
	)

	try:
		client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
		response = client.messages.create(
			model=CLAUDE_MODEL_DECRYPTAGE,
			max_tokens=1200,
			system=system_prompt,
			messages=[{"role": "user", "content": user_message}]
		)
		analysis_text = _extract_text(response)
		print(f"[DECRYPTAGE] Claude OK — {len(analysis_text)} chars")
	except Exception as e:
		print(f"[DECRYPTAGE ERROR] Claude failed: {type(e).__name__}: {e}")
		return {"success": False, "ticker": ticker, "error": f"Erreur Claude : {e}"}

	return {
		"success": True,
		"ticker": ticker,
		"name": company_name,
		"method_used": method["method_id"] if method else None,
		"analysis": analysis_text,
		"disclaimer": "Analyse éducative uniquement. Ne constitue pas un conseil en investissement.",
	}


@app.post("/education")
def education(request: EducationRequest):
	question = request.question.strip()
	context = request.context.strip()
	print(f"[EDUCATION] question: '{question[:80]}...' " if len(question) > 80 else f"[EDUCATION] question: '{question}'")

	# 1. Détection méthode pédagogique
	method = lookup_method(question, context=context)
	print(f"[EDUCATION] Méthode: {method['method_id'] if method else 'aucune'}")

	# 2. Construction prompts
	system_prompt = build_education_prompt(method)
	user_message = build_education_user_message(question, context)

	# 3. Appel Claude
	try:
		client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
		response = client.messages.create(
			model=CLAUDE_MODEL_EDUCATION,
			max_tokens=1500,
			system=system_prompt,
			messages=[{"role": "user", "content": user_message}]
		)
		response_text = _extract_text(response)
		print(f"[EDUCATION] Claude OK — {len(response_text)} chars")
	except Exception as e:
		print(f"[EDUCATION ERROR] Claude failed: {type(e).__name__}: {e}")
		return {"success": False, "error": f"Erreur Claude : {e}"}

	return {
		"success": True,
		"response": response_text,
	}


@app.post("/coach")
def coach(request: CoachRequest):
	question = request.question.strip()
	context = request.context.strip()
	tickers = request.tickers.strip()
	portfolio = request.portfolio.strip()
	print(f"[COACH] question: '{question[:80]}...' " if len(question) > 80 else f"[COACH] question: '{question}'")

	# 1. Construction prompts
	method = lookup_method(question, context=context)
	system_prompt = build_coach_prompt(tickers, portfolio, method)
	user_message = build_coach_user_message(question, context)

	# 2. Appel Claude
	try:
		client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
		response = client.messages.create(
			model=CLAUDE_MODEL_COACH,
			max_tokens=1500,
			system=system_prompt,
			messages=[{"role": "user", "content": user_message}]
		)
		response_text = _extract_text(response)
		print(f"[COACH] Claude OK — {len(response_text)} chars")
	except Exception as e:
		print(f"[COACH ERROR] Claude failed: {type(e).__name__}: {e}")
		return {"success": False, "error": f"Erreur Claude : {e}"}

	return {
		"success": True,
		"response": response_text,
	}


@app.post("/checklist")
def checklist(request: ChecklistRequest):
	raw_ticker = request.ticker
	question = request.question.strip()
	context = request.context.strip()
	ticker = normalize_ticker(raw_ticker)
	print(f"[CHECKLIST] '{raw_ticker}' → '{ticker}' | question: '{question or '(none)'}'")

	system_prompt = build_checklist_prompt(ticker)
	user_message = build_checklist_user_message(
		question if question else f"Génère la checklist Oryx pour {ticker}.",
		context
	)

	try:
		client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
		response = client.messages.create(
			model=CLAUDE_MODEL_CHECKLIST,
			max_tokens=1500,
			system=system_prompt,
			messages=[{"role": "user", "content": user_message}]
		)
		response_text = _extract_text(response)
		print(f"[CHECKLIST] Claude OK — {len(response_text)} chars")
	except Exception as e:
		print(f"[CHECKLIST ERROR] Claude failed: {type(e).__name__}: {e}")
		return {"success": False, "ticker": ticker, "error": f"Erreur Claude : {e}"}

	return {
		"success": True,
		"ticker": ticker,
		"analysis": response_text,
	}


# --- Web Chat ---

import json
from collections import defaultdict

_web_chat_history = defaultdict(list)
MAX_HISTORY_TURNS = 20


def _build_context_string(history: list) -> str:
	"""Build context string from history (most recent first)."""
	if not history:
		return ""
	lines = []
	for turn in reversed(history):
		lines.append(f"[{turn['role'].upper()}] {turn['text']}")
	return "\n".join(lines)


class WebChatRequest(BaseModel):
	session_id: str
	message: str


def _classify_intent(message: str, context: str = "") -> dict:
	"""Classify user message intent using Claude Haiku, aware of session context."""
	client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
	context_block = f"\n\nContexte de la session (messages précédents, du plus récent au plus ancien) :\n{context}" if context else ""
	response = client.messages.create(
		model=CLAUDE_MODEL_CLASSIFIER,
		max_tokens=300,
		system=f"""Tu es un classificateur de messages pour un bot d'investissement.
Analyse le message utilisateur et retourne UNIQUEMENT un JSON avec deux champs :
- "route": une des valeurs suivantes : "decryptage", "checklist", "coach", "education"
- "ticker": le ticker boursier mentionné (en majuscules), ou "" si aucun

Règles de classification (appliquées dans cet ordre de priorité) :

1. CHECKLIST : le message contient "checklist" ou "check" + un ticker ou nom d'entreprise → route "checklist"

2. DECRYPTAGE : le message mentionne un ticker (AAPL, MSFT, LVMH.PA, etc.) OU un nom d'entreprise reconnaissable (Microsoft, Apple, LVMH, Tesla, Nvidia, Amazon, etc.) avec une intention d'analyse (analyser, décrypter, regarder, étudier, que penses-tu de, parle-moi de) → route "decryptage". Extrais le ticker correspondant au nom (Microsoft → MSFT, Apple → AAPL, LVMH → MC.PA, Tesla → TSLA, Nvidia → NVDA, Amazon → AMZN, etc.).

3. CONTINUITÉ DE SESSION : si le contexte de session ci-dessous mentionne déjà un ticker et que le message actuel est une question de suivi sans nouveau ticker explicite (ex: "et niveau dette ?", "et la marge ?", "explique-moi ça"), garde route "decryptage" et réutilise le ticker déjà établi dans le contexte.

4. COACH : le message parle de décision d'investissement, d'argent à placer, ou de stratégie. Mots-clés : portefeuille, allocation, diversification, PEA, CTO, stratégie, investir, budget, j'ai X€, combien, répartir, commencer, premier investissement, comment réfléchir, que faire avec, construire un portefeuille, renforcer, arbitrage, vendre, acheter → route "coach"

5. EDUCATION : le message pose une question purement théorique ou pédagogique sur l'investissement (c'est quoi un ETF, comment fonctionne le P/E, qu'est-ce qu'un moat, etc.) → route "education"

En cas de doute entre coach et education : si le message parle d'argent concret ou de décision d'investissement → "coach". Sinon → "education".

Retourne UNIQUEMENT le JSON, rien d'autre. Exemple : {{"route": "decryptage", "ticker": "MSFT"}}{context_block}""",
		messages=[{"role": "user", "content": message}]
	)
	raw = _extract_text(response).strip()
	print(f"[WEB-CHAT] Classifier raw output: {raw!r}")
	if raw.startswith("```"):
		raw = raw.strip("`").replace("json", "", 1).strip()
	try:
		return json.loads(raw)
	except json.JSONDecodeError as e:
		print(f"[WEB-CHAT ERROR] Classifier JSON invalide: {e} | raw={raw!r}")
		return {"route": "education", "ticker": ""}


@app.post("/web-chat")
def web_chat(request: WebChatRequest):
	session_id = request.session_id.strip()
	message = request.message.strip()
	if not message:
		return {"success": False, "error": "Message vide"}

	print(f"[WEB-CHAT] session={session_id[:8]}... | message: '{message[:80]}'")

	# 1. Get conversation context
	history = _web_chat_history[session_id]
	context = _build_context_string(history)

	# 2. Classify intent (context-aware)
	try:
		intent = _classify_intent(message, context=context)
		route = intent.get("route", "education")
		ticker = intent.get("ticker", "").strip().upper()
		print(f"[WEB-CHAT] Classified → route={route}, ticker={ticker}")
	except Exception as e:
		print(f"[WEB-CHAT ERROR] Classifier failed: {type(e).__name__}: {e}")
		return {"success": False, "error": f"Erreur classification : {e}"}

	# 3. Route to the appropriate engine
	try:
		if route == "decryptage" and ticker:
			ticker = normalize_ticker(ticker)
			result = fetch_financial_data(ticker)
			if not result["success"]:
				return {"success": False, "error": result["error"], "route_used": "decryptage"}
			data = result["data"]
			company_name = data.get("name", ticker)
			lookup_text = message if message else f"analyser bilan états financiers {company_name}"
			if not context:
				method = _force_construction_these_method()
			else:
				method = lookup_method(lookup_text, context=context)
			system_prompt = build_system_prompt(data, method)
			user_message = build_user_message(message, context)
			model = CLAUDE_MODEL_DECRYPTAGE
			max_tokens = 1200

		elif route == "checklist" and ticker:
			ticker = normalize_ticker(ticker)
			system_prompt = build_checklist_prompt(ticker)
			user_message = build_checklist_user_message(message, context)
			model = CLAUDE_MODEL_CHECKLIST
			max_tokens = 1500

		elif route == "coach":
			method = lookup_method(message, context=context)
			system_prompt = build_coach_prompt(ticker, "", method)
			user_message = build_coach_user_message(message, context)
			model = CLAUDE_MODEL_COACH
			max_tokens = 1500

		else:
			method = lookup_method(message, context=context)
			system_prompt = build_education_prompt(method)
			user_message = build_education_user_message(message, context)
			model = CLAUDE_MODEL_EDUCATION
			max_tokens = 1500
			route = "education"

	except Exception as e:
		print(f"[WEB-CHAT ERROR] Engine setup failed: {type(e).__name__}: {e}")
		return {"success": False, "error": str(e), "route_used": route}

	# 4. Call Claude
	try:
		client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
		response = client.messages.create(
			model=model,
			max_tokens=max_tokens,
			system=system_prompt,
			messages=[{"role": "user", "content": user_message}]
		)
		response_text = _extract_text(response)
		print(f"[WEB-CHAT] Claude OK — route={route}, {len(response_text)} chars")
	except Exception as e:
		print(f"[WEB-CHAT ERROR] Claude failed: {type(e).__name__}: {e}")
		return {"success": False, "error": f"Erreur Claude : {e}", "route_used": route}

	# 5. Save to history (skip empty responses — don't pollute context)
	if not response_text.strip():
		print(f"[WEB-CHAT WARNING] Empty response — route={route}, message='{message[:80]}'")
		return {
			"success": True,
			"response": "Je n'ai pas bien compris ta question, tu peux la reformuler ?",
			"route_used": route,
			"ticker": ticker if ticker else None,
		}

	history.append({"role": "user", "text": message})
	history.append({"role": "assistant", "text": response_text})
	if len(history) > MAX_HISTORY_TURNS * 2:
		_web_chat_history[session_id] = history[-(MAX_HISTORY_TURNS * 2):]

	return {
		"success": True,
		"response": response_text,
		"route_used": route,
		"ticker": ticker if ticker else None,
	}


@app.get("/web")
def serve_web():
	return FileResponse("web/index.html")


# --- Web v2 ---

@app.get("/web-v2")
def serve_web_v2():
	return FileResponse("web-v2/index.html")


@app.get("/web-v2/search")
def web_v2_search(q: str = ""):
	return search_market(q)


class PortfolioAnalyzeRequest(BaseModel):
	portfolio_summary: str = ""


@app.post("/web-v2/portfolio-analyze")
def portfolio_analyze(request: PortfolioAnalyzeRequest):
	portfolio_summary = request.portfolio_summary.strip()
	system_prompt = build_portfolio_analysis_prompt()
	user_message = build_portfolio_analysis_user_message(portfolio_summary)

	try:
		client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
		response = client.messages.create(
			model=CLAUDE_MODEL_PORTFOLIO,
			max_tokens=1500,
			system=system_prompt,
			messages=[{"role": "user", "content": user_message}]
		)
		response_text = _extract_text(response)
		print(f"[PORTFOLIO-ANALYZE] Claude OK — {len(response_text)} chars")
	except Exception as e:
		print(f"[PORTFOLIO-ANALYZE ERROR] Claude failed: {type(e).__name__}: {e}")
		return {"success": False, "error": f"Erreur Claude : {e}"}

	return {"success": True, "response": response_text}


if __name__ == "__main__":
	port = int(os.environ.get("PORT", 10000))
	uvicorn.run(app, host="0.0.0.0", port=port)
