import copy
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional
import uvicorn
from fastapi import FastAPI, HTTPException
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
from core.market_lookup import search_market, get_eur_usd_rate
from core.db import get_db
from core.models import User, PortfolioPosition, InvestmentThesis, AnalysisSession
from core.assistant_delivery import (
	ASSISTANT_DELIVERY_SCHEMA_VERSION,
	DECRYPTAGE_SURFACE,
	AssistantDeliveryError,
	AssistantDeliveryNotFound,
	AssistantDeliveryOwnershipConflict,
	AssistantDeliveryUserNotFound,
	AssistantTurnIdentityCollision,
	InvalidAssistantDeliveryInput,
	UnsupportedAssistantDeliverySchema,
	acknowledge_delivery,
	bind_analysis_session,
	claim_delivery,
	decryptage_request_fingerprint,
	lock_delivery_for_ack,
	preflight_delivery,
	visible_content_fingerprint,
)
from core.decryptage_cognitive_runtime import (
	capture_delivered_response,
	capture_user_turn,
	runtime_private_metadata,
)
from core.decryptage_progress import (
	apply_construction_these_progress,
	extract_step_marker,
	find_active_analysis_session as _find_active_analysis_session,
)
from sqlalchemy.orm import Session
from fastapi import Depends

import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")


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
	"""R1-C1 : user_id, conversation_key et client_turn_id sont obligatoires.
	client_turn_id identifie UN tour utilisateur logique (identique sur un
	retry réseau du même envoi, nouveau à chaque envoi). La surface et
	l'ordinal de livraison sont imposés par le serveur."""
	ticker: str
	question: str = ""
	context: str = ""
	last_method_id: Optional[str] = None
	level: str = "debutant"
	user_id: str
	conversation_key: str
	client_turn_id: uuid.UUID

	@field_validator("ticker")
	@classmethod
	def ticker_must_not_be_empty_decryptage(cls, v):
		v = v.strip().upper()
		if not v:
			raise ValueError("ticker must not be empty")
		return v

	@field_validator("user_id", "conversation_key")
	@classmethod
	def identifier_must_not_be_empty_decryptage(cls, v):
		# Validé, jamais transformé : la valeur est persistée et hashée telle quelle.
		if not v.strip() or "\x00" in v:
			raise ValueError("identifier must be a non-empty string without NUL")
		return v


class AssistantDeliveryAckRequest(BaseModel):
	user_id: str
	conversation_key: str


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


def _get_latest_thesis(user_id, ticker):
	"""Retourne la dernière thèse enregistrée pour cet utilisateur et
	ce ticker, ou None. Ne doit jamais faire planter la réponse
	principale : toute erreur est journalisée et avalée silencieusement."""
	from core.db import SessionLocal
	from core.models import InvestmentThesis
	if not SessionLocal or not user_id:
		return None
	try:
		session = SessionLocal()
		try:
			thesis = session.query(InvestmentThesis).filter(
				InvestmentThesis.user_id == user_id, InvestmentThesis.ticker == ticker
			).order_by(InvestmentThesis.created_at.desc()).first()
			if thesis:
				return {"text": thesis.thesis_text, "date": thesis.created_at.strftime("%d/%m/%Y")}
			return None
		finally:
			session.close()
	except Exception as e:
		print(f"[DB-TRACKING ERROR] {type(e).__name__}: {e}")
		return None


def _get_analysis_progress(user_id, ticker):
	"""Retourne l'état d'une analyse construction_these EN COURS pour ce
	user+ticker, ou None s'il n'y a aucune AnalysisSession in_progress
	(_get_latest_thesis prend alors le relai).

	Source unique (T1-C1) : l'AnalysisSession in_progress retenue par
	_find_active_analysis_session. current_step vient de la session ; on
	ne lit QUE les AnalysisFact/UserStatement rattachés à son id. Aucun
	filtre temporel.

	Ne doit jamais faire planter la réponse principale : toute erreur
	est journalisée et avalée silencieusement."""
	from core.db import SessionLocal
	from core.models import AnalysisFact, UserStatement
	if not SessionLocal or not user_id:
		return None
	try:
		session = SessionLocal()
		try:
			active = _find_active_analysis_session(session, user_id, ticker)
			if not active:
				return None
			statements = session.query(UserStatement).filter(
				UserStatement.analysis_session_id == active.id
			).order_by(UserStatement.statement_date.asc(), UserStatement.id.asc()).all()
			facts = session.query(AnalysisFact).filter(
				AnalysisFact.analysis_session_id == active.id
			).order_by(AnalysisFact.fact_date.asc(), AnalysisFact.id.asc()).all()
			return {
				"current_step": active.current_step,
				"statements": [{"step": s.step, "text": s.statement_text} for s in statements if s.step],
				"facts": {f.fact_type: f.fact_value for f in facts},
			}
		finally:
			session.close()
	except Exception as e:
		print(f"[DB-TRACKING ERROR] {type(e).__name__}: {e}")
		return None


# --- R1-C1 : livraison idempotente des réponses Décrypter ------------------
#
# /decryptage possède ses transactions ; aucune n'est ouverte pendant les
# appels externes (résolution du ticker, données financières, Claude) :
#
#   EMPREINTE PURE de la requête (aucune I/O)
#   -> TX PREFLIGHT (User, registre de conversation, livraison existante)
#   -> COMMIT -> retry canonique : réponse immédiate, ZÉRO appel externe
#   -> sinon APPELS EXTERNES (normalize_ticker, données, Claude ; aucune
#      transaction DB ouverte)
#   -> TX CLAIM/PRODUIT (livraison, rattachement cognitif T2 du tour
#      utilisateur [R1-C2], progression construction_these, liaison
#      analysis_session) -> COMMIT -> réponse HTTP.
#
# R1-C2 : seul le worker gagnant (livraison créée par CET appel) rattache
# le tour utilisateur au CognitiveEvent Décrypter compatible et crée le lien
# decryptage_cognitive_links en awaiting_delivery ; un retry canonique ou un
# worker perdant ne touche jamais à T2. La réponse est capturée à l'ACK,
# atomiquement avec pending -> delivered. Aucun T3+ (évaluation,
# observation, inférence, Step 6). Un échec externe AVANT la livraison
# canonique (ticker, données, Claude) laisse le tour hors T2 (limite V1).
#
# Une réponse success=True n'est renvoyée qu'après le COMMIT de la
# livraison pending : une défaillance DB n'est plus avalée (l'ancienne
# doctrine « ne jamais faire planter la réponse principale » est
# abandonnée pour ce chemin) ; la transaction est annulée et le client
# reçoit une erreur retryable.

DECRYPTAGE_DELIVERY_ORDINAL = 1
DECRYPTAGE_DISCLAIMER = "Analyse éducative uniquement. Ne constitue pas un conseil en investissement."
_RETRYABLE_ERROR = "Erreur interne, réessaie dans quelques instants."

_DELIVERY_HTTP_STATUS = {
	AssistantDeliveryUserNotFound: 404,
	AssistantDeliveryNotFound: 404,
	AssistantDeliveryOwnershipConflict: 403,
	AssistantTurnIdentityCollision: 409,
	UnsupportedAssistantDeliverySchema: 409,
	InvalidAssistantDeliveryInput: 422,
}


def _delivery_http_error(exc: AssistantDeliveryError, *, phase: str) -> HTTPException:
	"""Erreur métier attendue → HTTP. Le détail ne contient que le type
	d'erreur (jamais de payload ni de texte utilisateur). Toute autre erreur
	du service est une incohérence serveur : 500 retryable."""
	status = _DELIVERY_HTTP_STATUS.get(type(exc), 500)
	print(f"[ASSISTANT-DELIVERY] {phase} refusé : {type(exc).__name__} → HTTP {status}")
	if status == 500:
		return HTTPException(status_code=500, detail={"error": _RETRYABLE_ERROR, "retryable": True})
	return HTTPException(status_code=status, detail={"error": type(exc).__name__, "retryable": False})


def _delivery_response(delivery) -> dict:
	"""Réponse HTTP d'une livraison : son payload public canonique persisté
	(jamais recalculé), assistant_turn_id et delivery_status. Jamais
	private_metadata. Construite AVANT le commit (objets ORM expirés
	ensuite)."""
	return {
		**copy.deepcopy(delivery.response_payload),
		"assistant_turn_id": str(delivery.id),
		"delivery_status": delivery.status,
	}


@app.post("/decryptage")
def decryptage(request: DecryptageRequest, db: Session = Depends(get_db)):
	raw_ticker = request.ticker
	question = request.question.strip()
	context = request.context.strip()
	user_id = request.user_id
	conversation_key = request.conversation_key
	client_turn_id = request.client_turn_id

	identity = {
		"user_id": user_id,
		"conversation_key": conversation_key,
		"surface": DECRYPTAGE_SURFACE,
		"source_user_turn_id": client_turn_id,
		"delivery_ordinal": DECRYPTAGE_DELIVERY_ORDINAL,
	}

	# --- Phase A : preflight, AVANT tout appel externe ----------------------
	try:
		# Empreinte PURE du payload utilisateur logique, calculée AVANT toute
		# I/O externe : saisie du ticker (strip().upper() local, jamais
		# normalize_ticker qui peut interroger EODHD), question / context
		# strippés ; aucune donnée de marché.
		request_fingerprint = decryptage_request_fingerprint(
			user_id=user_id,
			conversation_key=conversation_key,
			client_turn_id=client_turn_id,
			ticker_input=raw_ticker,
			question=question,
			context=context,
			last_method_id=request.last_method_id,
			level=request.level,
		)
		canonical = preflight_delivery(db, **identity, request_fingerprint=request_fingerprint)
		replay = _delivery_response(canonical) if canonical is not None else None
		db.commit()
	except AssistantDeliveryError as exc:
		db.rollback()
		raise _delivery_http_error(exc, phase="preflight")
	except Exception as exc:
		db.rollback()
		print(f"[ASSISTANT-DELIVERY ERROR] preflight : {type(exc).__name__}")
		raise HTTPException(status_code=500, detail={"error": _RETRYABLE_ERROR, "retryable": True})
	if replay is not None:
		print(f"[ASSISTANT-DELIVERY] Retry : assistant_turn={replay['assistant_turn_id']}, statut={replay['delivery_status']} (aucun appel externe)")
		return replay

	# --- Appels externes : aucune transaction DB ouverte --------------------
	# Seulement pour un tour sans livraison canonique : résolution du ticker
	# (peut interroger EODHD Search), données financières, Claude.
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
	existing_thesis = None
	in_progress_analysis = None
	if not context:
		method = _force_construction_these_method()
		# T1-B2 : une tentative en cours prime sur une thèse déjà terminée
		# (ex. ancienne thèse LVMH + nouvelle session LVMH en cours).
		in_progress_analysis = _get_analysis_progress(user_id, ticker)
		if not in_progress_analysis:
			existing_thesis = _get_latest_thesis(user_id, ticker)
	else:
		method = lookup_method(lookup_text, context=context, last_method_id=request.last_method_id)
	method_id = method["method_id"] if method else None
	print(f"[DECRYPTAGE] Méthode: {method_id or 'aucune'}")

	system_prompt = build_system_prompt(data, method, request.level, existing_thesis, in_progress_analysis)
	user_message = build_user_message(
		question if question else f"Aide-moi à analyser {company_name} ({ticker}).",
		context
	)

	client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
	analysis_text = None
	last_error = None
	for attempt in range(2):
		try:
			response = client.messages.create(
				model=CLAUDE_MODEL_DECRYPTAGE,
				max_tokens=2500,
				system=system_prompt,
				messages=[{"role": "user", "content": user_message}]
			)
			analysis_text = _extract_text(response)
			print(f"[DECRYPTAGE] Claude OK — {len(analysis_text)} chars — stop_reason={response.stop_reason}")
			if analysis_text.strip():
				break
			print(f"[DECRYPTAGE WARNING] Réponse vide (tentative {attempt + 1}/2)")
			if attempt == 0:
				time.sleep(1.5)
		except Exception as e:
			last_error = e
			print(f"[DECRYPTAGE ERROR] Claude failed (tentative {attempt + 1}/2): {type(e).__name__}: {e}")
			if attempt == 0:
				time.sleep(1.5)
	if analysis_text is None:
		print(f"[DECRYPTAGE ERROR] Échec définitif après 2 tentatives: {last_error}")
		return {
			"success": False,
			"ticker": ticker,
			"error": "Je n'ai pas réussi à générer une réponse, réessaie dans quelques instants.",
		}

	# --- Payload public canonique + métadonnée privée -----------------------
	if not analysis_text.strip():
		print(f"[DECRYPTAGE WARNING] Empty response — ticker={ticker}, question='{question[:80]}'")
		step_marker = None
		response_payload = {
			"success": True,
			"ticker": ticker,
			"name": company_name,
			"method_used": method_id,
			"analysis": "Je n'ai pas bien compris, tu peux reformuler ta question ?",
			"disclaimer": DECRYPTAGE_DISCLAIMER,
		}
	else:
		# Le marqueur privé est toujours retiré du texte visible ; seul un
		# marqueur du vocabulaire fermé est conservé (private_metadata).
		visible_text, step_marker = extract_step_marker(analysis_text)
		response_payload = {
			"success": True,
			"ticker": ticker,
			"name": company_name,
			"method_used": method_id,
			"analysis": visible_text,
			"price": data.get("current_price"),
			"currency": data.get("currency", "USD"),
			"disclaimer": DECRYPTAGE_DISCLAIMER,
		}

	# --- Phase B : claim puis progression produit, une transaction ----------
	try:
		delivery, created = claim_delivery(
			db,
			**identity,
			request_fingerprint=request_fingerprint,
			visible_content_fingerprint=visible_content_fingerprint(response_payload["analysis"]),
			response_payload=response_payload,
			private_metadata=runtime_private_metadata(step_marker),
			delivery_schema_version=ASSISTANT_DELIVERY_SCHEMA_VERSION,
		)
		# Seule la livraison gagnante fait avancer le produit : la réponse
		# d'un worker perdant est jetée et ne mute jamais AnalysisSession ni
		# T2. Le rattachement cognitif lit l'AnalysisSession active AVANT la
		# progression produit (le texte du tour répond à l'étape précédente).
		if created:
			capture_user_turn(db, delivery=delivery, ticker=ticker, user_text=question)
		if created and method_id == "construction_these" and step_marker is not None:
			analysis_session = apply_construction_these_progress(
				db, user_id=user_id, ticker=ticker, step=step_marker, thesis_text=question, data=data,
			)
			if analysis_session is not None:
				bind_analysis_session(db, delivery_id=delivery.id, analysis_session_id=analysis_session.id)
		final = _delivery_response(delivery)
		db.commit()
	except (AssistantTurnIdentityCollision, AssistantDeliveryOwnershipConflict, AssistantDeliveryUserNotFound,
			UnsupportedAssistantDeliverySchema) as exc:
		db.rollback()
		raise _delivery_http_error(exc, phase="claim")
	except Exception as exc:
		db.rollback()
		print(f"[ASSISTANT-DELIVERY ERROR] claim/progression annulés : {type(exc).__name__}")
		raise HTTPException(status_code=500, detail={"error": _RETRYABLE_ERROR, "retryable": True})
	print(f"[ASSISTANT-DELIVERY] assistant_turn={final['assistant_turn_id']}, ticker={ticker}, créée={created}, statut={final['delivery_status']}")
	return final


@app.post("/api/runtime/assistant-deliveries/{assistant_turn_id}/ack")
def ack_assistant_delivery(assistant_turn_id: uuid.UUID, request: AssistantDeliveryAckRequest, db: Session = Depends(get_db)):
	"""Le frontend first-party confirme avoir inséré la réponse canonique
	dans l'interface : pending → delivered (idempotent). Ne prouve ni
	lecture ni compréhension. Ne renvoie ni le texte, ni l'empreinte, ni
	private_metadata, ni aucune donnée cognitive.

	R1-C2 : pour une livraison R1-C2, la capture T2 de la réponse
	(lifecycle CognitiveEvent, SupportTrace éventuel, lien -> captured) est
	faite dans la MÊME transaction, sous l'ordre de verrous livraison ->
	lien -> AnalysisSession -> CognitiveEvent, AVANT pending → delivered.
	Tout échec annule l'ensemble : la livraison reste pending (l'outbox
	frontend reste bloquante). Livraison legacy R1-C1 : ACK inchangé, aucune
	capture rétroactive. Aucun T3+."""
	try:
		delivery = lock_delivery_for_ack(
			db, assistant_turn_id=assistant_turn_id, user_id=request.user_id, conversation_key=request.conversation_key,
		)
		capture_delivered_response(db, delivery=delivery)
		delivery = acknowledge_delivery(
			db, assistant_turn_id=assistant_turn_id, user_id=request.user_id, conversation_key=request.conversation_key,
		)
		result = {"success": True, "assistant_turn_id": str(delivery.id), "delivery_status": delivery.status}
		db.commit()
	except AssistantDeliveryError as exc:
		db.rollback()
		raise _delivery_http_error(exc, phase="ack")
	except Exception as exc:
		db.rollback()
		print(f"[ASSISTANT-DELIVERY ERROR] ack : {type(exc).__name__}")
		raise HTTPException(status_code=500, detail={"error": _RETRYABLE_ERROR, "retryable": True})
	return result


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
_web_chat_last_method = {}
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
	level: str = "debutant"


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
	last_method_id = _web_chat_last_method.get(session_id)

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
				method = lookup_method(lookup_text, context=context, last_method_id=last_method_id)
			if method:
				_web_chat_last_method[session_id] = method["method_id"]
			else:
				_web_chat_last_method[session_id] = None
			system_prompt = build_system_prompt(data, method, request.level)
			user_message = build_user_message(message, context)
			model = CLAUDE_MODEL_DECRYPTAGE
			max_tokens = 2000

		elif route == "checklist" and ticker:
			ticker = normalize_ticker(ticker)
			system_prompt = build_checklist_prompt(ticker)
			user_message = build_checklist_user_message(message, context)
			model = CLAUDE_MODEL_CHECKLIST
			max_tokens = 2000

		elif route == "coach":
			method = lookup_method(message, context=context, last_method_id=last_method_id, allow_these_lock=False)
			if method:
				_web_chat_last_method[session_id] = method["method_id"]
			else:
				_web_chat_last_method[session_id] = None
			system_prompt = build_coach_prompt(ticker, "", method, request.level)
			user_message = build_coach_user_message(message, context)
			model = CLAUDE_MODEL_COACH
			max_tokens = 2500

		else:
			method = lookup_method(message, context=context, last_method_id=last_method_id, allow_these_lock=False)
			if method:
				_web_chat_last_method[session_id] = method["method_id"]
			else:
				_web_chat_last_method[session_id] = None
			system_prompt = build_education_prompt(method, request.level)
			user_message = build_education_user_message(message, context)
			model = CLAUDE_MODEL_EDUCATION
			max_tokens = 2500
			route = "education"

	except Exception as e:
		print(f"[WEB-CHAT ERROR] Engine setup failed: {type(e).__name__}: {e}")
		return {"success": False, "error": "Je n'ai pas réussi à traiter ta demande, réessaie.", "route_used": route}

	# 4. Call Claude
	client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
	response_text = None
	last_error = None
	for attempt in range(2):
		try:
			response = client.messages.create(
				model=model,
				max_tokens=max_tokens,
				system=system_prompt,
				messages=[{"role": "user", "content": user_message}]
			)
			response_text = _extract_text(response)
			print(f"[WEB-CHAT] Claude OK — route={route}, {len(response_text)} chars — stop_reason={response.stop_reason}")
			if response_text.strip():
				break
			print(f"[WEB-CHAT WARNING] Réponse vide (tentative {attempt + 1}/2)")
			if attempt == 0:
				time.sleep(1.5)
		except Exception as e:
			last_error = e
			print(f"[WEB-CHAT ERROR] Claude failed (tentative {attempt + 1}/2): {type(e).__name__}: {e}")
			if attempt == 0:
				time.sleep(1.5)
	if response_text is None:
		print(f"[WEB-CHAT ERROR] Échec définitif après 2 tentatives: {last_error}")
		return {"success": False, "error": "Je n'ai pas réussi à générer une réponse, réessaie dans quelques instants.", "route_used": route}

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


@app.get("/web-v2/fx-rate")
def web_v2_fx_rate():
	rate = get_eur_usd_rate()
	if rate is None:
		return {"success": False}
	return {"success": True, "eur_usd": rate}


class UserLevelRequest(BaseModel):
	level: str


class PortfolioPositionRequest(BaseModel):
	ticker: str
	quantity: float
	purchase_price: Optional[float] = None
	target_percent: Optional[float] = None
	envelope: Optional[str] = None


@app.get("/api/user/{user_id}")
def get_user(user_id: str, db: Session = Depends(get_db)):
	user = db.query(User).filter(User.id == user_id).first()
	if not user:
		user = User(id=user_id, level="debutant")
		db.add(user)
		db.commit()
	positions = db.query(PortfolioPosition).filter(PortfolioPosition.user_id == user_id).all()
	return {
		"level": user.level,
		"portfolio": [
			{
				"id": p.id,
				"ticker": p.ticker,
				"quantite": p.quantity,
				"prixAchat": p.purchase_price,
				"cible": p.target_percent,
				"enveloppe": p.envelope,
			}
			for p in positions
		],
	}


@app.put("/api/user/{user_id}/level")
def set_user_level(user_id: str, request: UserLevelRequest, db: Session = Depends(get_db)):
	user = db.query(User).filter(User.id == user_id).first()
	if not user:
		user = User(id=user_id, level=request.level)
		db.add(user)
	else:
		user.level = request.level
	db.commit()
	return {"success": True}


@app.get("/api/user/{user_id}/portfolio")
def get_portfolio(user_id: str, db: Session = Depends(get_db)):
	positions = db.query(PortfolioPosition).filter(PortfolioPosition.user_id == user_id).all()
	return [
		{
			"id": p.id,
			"ticker": p.ticker,
			"quantite": p.quantity,
			"prixAchat": p.purchase_price,
			"cible": p.target_percent,
			"enveloppe": p.envelope,
		}
		for p in positions
	]


@app.post("/api/user/{user_id}/portfolio")
def add_portfolio_position(user_id: str, request: PortfolioPositionRequest, db: Session = Depends(get_db)):
	user = db.query(User).filter(User.id == user_id).first()
	if not user:
		user = User(id=user_id, level="debutant")
		db.add(user)
		db.commit()
	position = PortfolioPosition(
		user_id=user_id,
		ticker=request.ticker,
		quantity=request.quantity,
		purchase_price=request.purchase_price,
		target_percent=request.target_percent,
		envelope=request.envelope,
	)
	db.add(position)
	db.commit()
	db.refresh(position)
	return {
		"id": position.id,
		"ticker": position.ticker,
		"quantite": position.quantity,
		"prixAchat": position.purchase_price,
		"cible": position.target_percent,
		"enveloppe": position.envelope,
	}


@app.delete("/api/user/{user_id}/portfolio/{position_id}")
def delete_portfolio_position(user_id: str, position_id: int, db: Session = Depends(get_db)):
	position = db.query(PortfolioPosition).filter(
		PortfolioPosition.id == position_id, PortfolioPosition.user_id == user_id
	).first()
	if position:
		db.delete(position)
		db.commit()
	return {"success": True}


@app.put("/api/user/{user_id}/portfolio/{position_id}")
def update_portfolio_position(user_id: str, position_id: int, request: PortfolioPositionRequest, db: Session = Depends(get_db)):
	position = db.query(PortfolioPosition).filter(
		PortfolioPosition.id == position_id, PortfolioPosition.user_id == user_id
	).first()
	if not position:
		raise HTTPException(status_code=404, detail="Position introuvable.")
	position.ticker = request.ticker
	position.quantity = request.quantity
	position.purchase_price = request.purchase_price
	position.target_percent = request.target_percent
	position.envelope = request.envelope
	db.commit()
	db.refresh(position)
	return {
		"id": position.id,
		"ticker": position.ticker,
		"quantite": position.quantity,
		"prixAchat": position.purchase_price,
		"cible": position.target_percent,
		"enveloppe": position.envelope,
	}


def _as_utc(dt):
	"""Timestamps naïfs historiques (datetime.utcnow) → UTC aware."""
	return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@app.get("/api/user/{user_id}/theses")
def get_theses(user_id: str, db: Session = Depends(get_db)):
	"""Une entrée par ticker (T1-C1 : AnalysisSession + InvestmentThesis) :
	- session in_progress → current_step de la session, theses=[] (le
	  frontend affiche « En cours », même si une ancienne thèse existe) ;
	- sinon, thèse(s) → current_step="swot_final", thèses de la plus
	  récente à la plus ancienne ;
	- sinon (ex. uniquement des sessions abandoned) → absent."""
	all_theses = db.query(InvestmentThesis).filter(
		InvestmentThesis.user_id == user_id
	).order_by(InvestmentThesis.created_at.desc(), InvestmentThesis.id.desc()).all()
	theses_by_ticker = {}
	for t in all_theses:
		theses_by_ticker.setdefault(t.ticker, []).append(t)

	active_tickers = {row.ticker for row in db.query(AnalysisSession.ticker).filter(
		AnalysisSession.user_id == user_id, AnalysisSession.status == "in_progress"
	).distinct()}

	entries = []
	for ticker in sorted(active_tickers | set(theses_by_ticker)):
		active = _find_active_analysis_session(db, user_id, ticker) if ticker in active_tickers else None
		if active:
			entries.append((_as_utc(active.updated_at), {
				"ticker": ticker,
				"current_step": active.current_step,
				"updated_at": _as_utc(active.updated_at).isoformat(),
				"theses": [],
			}))
		else:
			theses = theses_by_ticker[ticker]
			latest = _as_utc(theses[0].created_at)
			entries.append((latest, {
				"ticker": ticker,
				"current_step": "swot_final",
				"updated_at": latest.isoformat(),
				"theses": [{"text": t.thesis_text, "created_at": t.created_at.isoformat()} for t in theses],
			}))
	entries.sort(key=lambda e: e[0], reverse=True)
	return [entry for _, entry in entries]


@app.delete("/api/user/{user_id}/theses/{ticker}")
def delete_thesis(user_id: str, ticker: str, db: Session = Depends(get_db)):
	"""Supprime les thèses du user+ticker et passe ses sessions in_progress
	à abandoned (T1-C1). AnalysisSession, AnalysisFact et UserStatement
	sont conservés : une analyse ultérieure crée une nouvelle session."""
	db.query(InvestmentThesis).filter(InvestmentThesis.user_id == user_id, InvestmentThesis.ticker == ticker).delete()
	now_aware = datetime.now(timezone.utc)
	for active in db.query(AnalysisSession).filter(
		AnalysisSession.user_id == user_id, AnalysisSession.ticker == ticker,
		AnalysisSession.status == "in_progress",
	).all():
		active.status = "abandoned"
		active.updated_at = now_aware
	db.commit()
	return {"success": True}


class PortfolioAnalyzeRequest(BaseModel):
	portfolio_summary: str = ""


@app.post("/web-v2/portfolio-analyze")
def portfolio_analyze(request: PortfolioAnalyzeRequest):
	portfolio_summary = request.portfolio_summary.strip()
	system_prompt = build_portfolio_analysis_prompt()
	user_message = build_portfolio_analysis_user_message(portfolio_summary)

	client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
	response_text = ""
	for attempt in range(2):
		try:
			response = client.messages.create(
				model=CLAUDE_MODEL_PORTFOLIO,
				max_tokens=2500,
				system=system_prompt,
				messages=[{"role": "user", "content": user_message}]
			)
			response_text = _extract_text(response)
			print(f"[PORTFOLIO-ANALYZE] Claude OK — {len(response_text)} chars, stop_reason={response.stop_reason}")
			if response_text.strip():
				break
			print(f"[PORTFOLIO-ANALYZE] Réponse vide, tentative {attempt + 1}/2")
		except Exception as e:
			print(f"[PORTFOLIO-ANALYZE ERROR] Claude failed (tentative {attempt + 1}/2): {type(e).__name__}: {e}")
			if attempt == 0:
				time.sleep(1.5)
				continue
			return {"success": False, "error": f"Erreur Claude : {e}"}

	if not response_text.strip():
		return {"success": False, "error": "Réponse vide de Claude après 2 tentatives."}

	return {"success": True, "response": response_text}


if __name__ == "__main__":
	port = int(os.environ.get("PORT", 10000))
	uvicorn.run(app, host="0.0.0.0", port=port)
