"""Tests de R1-C1 : livraisons idempotentes des réponses Décrypter.

Couvre core/assistant_delivery.py (service), core/decryptage_progress.py
(progression construction_these extraite de api.py, transactionnelle) et
le branchement runtime de /decryptage + POST
/api/runtime/assistant-deliveries/{assistant_turn_id}/ack dans api.py.

1. Tests sans base (toujours exécutés) : constantes et versions, API
   publique exacte, hiérarchie d'exceptions, aucune transaction possédée
   par les services (un seul SAVEPOINT ciblé), aucun branchement T2+
   dans les services R1-C1 (capture cognitive, SupportTrace, observations,
   longitudinal, inférence, Step 6), validation structurelle avant tout
   accès à la base, empreintes (requête, contenu visible), marqueurs
   d'étape privés. Depuis R1-C2, les routes passent par l'orchestrateur T2
   Décrypter (core/decryptage_cognitive_runtime.py, testé dans
   tests/test_decryptage_cognitive_runtime.py) et toujours par aucun T3+.

2. Tests contre un vrai PostgreSQL : uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite (la
   concurrence et l'idempotence reposent sur la contrainte UNIQUE, les
   SAVEPOINT et les verrous PostgreSQL).

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_r1c1_test \\
           python -m pytest tests/test_assistant_delivery.py

   Les sessions sont créées comme core.db.SessionLocal (autoflush=False) ;
   la transaction appartient toujours à l'appelant (le test ou la route).
   La course réelle utilise deux connexions et pg_blocking_pids() (aucun
   sleep arbitraire). Les scénarios de route appellent directement
   api.decryptage / api.ack_assistant_delivery avec une Session ; seuls les
   appels externes (données financières, Claude) sont remplacés, et ils
   vérifient qu'aucune transaction n'est ouverte pendant leur exécution.
"""
import ast
import hashlib
import inspect
import itertools
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

import api
import core.db
from core import assistant_delivery as ad
from core import decryptage_progress as dp
from core.assistant_delivery import (
    AnalysisSessionBindingConflict,
    AssistantDeliveryError,
    AssistantDeliveryNotFound,
    AssistantDeliveryOwnershipConflict,
    AssistantDeliveryUserNotFound,
    AssistantTurnIdentityCollision,
    InvalidAssistantDeliveryInput,
    UnsupportedAssistantDeliverySchema,
)
from core.models import AnalysisFact, AnalysisSession, AssistantDelivery, InvestmentThesis, UserStatement
from core.ticker_resolver import normalize_ticker
from tests.test_cognitive_capture import _NoDB, _assert_utc, _run_blocked, _Statements, _writes
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import SESSION_ID, _code_tokens
from tests.test_migration_0005_cognitive_support_traces import OTHER_USER, USER, _upgrade_head_with_users

SERVICE_PATH = REPO_ROOT / "core" / "assistant_delivery.py"
PROGRESS_PATH = REPO_ROOT / "core" / "decryptage_progress.py"
API_PATH = REPO_ROOT / "api.py"
PUBLIC_API = {
    "preflight_delivery",
    "claim_delivery",
    "bind_analysis_session",
    "lock_delivery_for_ack",
    "acknowledge_delivery",
    "decryptage_request_fingerprint",
    "visible_content_fingerprint",
}
TURN = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CONV = "conv-u"
FP_A = "a" * 64
FP_B = "b" * 64
PAYLOAD_A = {"success": True, "ticker": "MC.PA", "name": "LVMH", "method_used": "construction_these",
             "analysis": "Réponse A", "price": 600.0, "currency": "EUR", "disclaimer": "d"}
PAYLOAD_B = {**PAYLOAD_A, "analysis": "Réponse B", "price": 601.5}
STEPS = ("business", "moat", "chiffres", "valorisation", "risques", "swot_final")
# Tout ce que R1-C1 ne doit JAMAIS appeler ni importer (T2+ pédagogique).
FORBIDDEN_RUNTIME_NAMES = (
    "cognitive_capture", "open_event_idempotent", "append_user_contribution", "add_support_trace",
    "finalize_event", "abandon_event", "SupportTrace", "CognitiveEvent", "observation_service",
    "longitudinal_service", "inference_service", "taxonomy_service", "adaptation_state", "progression_view",
    "start_evaluation_run", "add_observation",
)


def _identity(**overrides):
    kwargs = {"user_id": USER, "conversation_key": CONV, "surface": ad.DECRYPTAGE_SURFACE,
              "source_user_turn_id": TURN, "delivery_ordinal": 1}
    kwargs.update(overrides)
    return kwargs


def _claim_kwargs(**overrides):
    kwargs = {**_identity(), "request_fingerprint": FP_A, "visible_content_fingerprint": FP_A,
              "response_payload": PAYLOAD_A, "private_metadata": {"decryptage_step_marker": "business"},
              "delivery_schema_version": ad.ASSISTANT_DELIVERY_SCHEMA_VERSION}
    kwargs.update(overrides)
    return kwargs


def _fingerprint_kwargs(**overrides):
    kwargs = {"user_id": USER, "conversation_key": CONV, "client_turn_id": TURN, "ticker_input": "MC.PA",
              "question": "Que fait LVMH ?", "context": "", "last_method_id": None, "level": "debutant"}
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_versions_and_constants_are_explicit():
    assert ad.ASSISTANT_DELIVERY_SCHEMA_VERSION == "assistant-delivery-v1"
    assert ad.DECRYPTAGE_REQUEST_FINGERPRINT_SCHEMA_VERSION == "decryptage-request-fingerprint-v2"
    assert ad.DECRYPTAGE_SURFACE == "decryptage"
    assert ad.SUPPORTED_SURFACES == ("decryptage",)
    assert (ad.PENDING, ad.DELIVERED) == ("pending", "delivered")
    assert ad.TURN_UNIQUE_CONSTRAINT == "uq_assistant_deliveries_turn_ordinal"
    assert dp.DECRYPTAGE_STEP_MARKERS == STEPS
    assert api.DECRYPTAGE_DELIVERY_ORDINAL == 1


def test_public_api_is_exact_and_keyword_only():
    """Ni update générique, ni delete, ni listing : seules les opérations
    du runtime R1-C1 (+ lock_delivery_for_ack, verrou sans mutation de
    l'ACK R1-C2)."""
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == PUBLIC_API
    assert list(inspect.signature(ad.visible_content_fingerprint).parameters) == ["text"]
    for name in PUBLIC_API - {"visible_content_fingerprint"}:
        params = list(inspect.signature(getattr(ad, name)).parameters.values())
        if params[0].name == "db":
            assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
            params = params[1:]
        assert params and all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params), name
    assert list(inspect.signature(ad.claim_delivery).parameters) == [
        "db", "user_id", "conversation_key", "surface", "source_user_turn_id", "delivery_ordinal",
        "request_fingerprint", "visible_content_fingerprint", "response_payload", "private_metadata",
        "delivery_schema_version"]
    assert list(inspect.signature(ad.acknowledge_delivery).parameters) == [
        "db", "assistant_turn_id", "user_id", "conversation_key"]
    assert list(inspect.signature(ad.lock_delivery_for_ack).parameters) == [
        "db", "assistant_turn_id", "user_id", "conversation_key"]
    assert list(inspect.signature(ad.bind_analysis_session).parameters) == [
        "db", "delivery_id", "analysis_session_id"]
    assert list(inspect.signature(dp.apply_construction_these_progress).parameters) == [
        "db", "user_id", "ticker", "step", "thesis_text", "data", "user_contribution", "answered_step"]


def test_exceptions_are_a_small_business_hierarchy():
    for exc in (InvalidAssistantDeliveryInput, AssistantDeliveryUserNotFound, AssistantDeliveryNotFound,
                AssistantTurnIdentityCollision, AssistantDeliveryOwnershipConflict,
                UnsupportedAssistantDeliverySchema, AnalysisSessionBindingConflict):
        assert issubclass(exc, AssistantDeliveryError)
        assert not issubclass(exc, sa.exc.IntegrityError)
    assert not issubclass(AssistantDeliveryError, (LookupError, ValueError))


def _calls(tree, attr) -> list:
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr]


def _imports(tree) -> set:
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{alias.name}" if node.module == "core" else node.module
                         for alias in node.names}
    return imported


def test_delivery_service_owns_no_transaction_and_does_not_depend_on_fastapi():
    """Jamais commit / rollback / begin / close / SessionLocal ; un seul
    SAVEPOINT local (with db.begin_nested()) autour de l'INSERT de
    claim_delivery."""
    source = SERVICE_PATH.read_text(encoding="utf-8")
    tokens = _code_tokens(source).split("\n")
    for forbidden in ("commit", "rollback", "begin", "close", "SessionLocal", "get_db", "HTTPException", "fastapi",
                      "Depends", "delete", "merge", "expunge", "invalidate", "connection", "anthropic"):
        assert forbidden not in tokens, forbidden
    assert tokens.count("begin_nested") == 1
    tree = ast.parse(source)
    (call,) = _calls(tree, "begin_nested")
    owner = next(f for f in tree.body if isinstance(f, ast.FunctionDef) and any(n is call for n in ast.walk(f)))
    assert owner.name == "claim_delivery"
    withs = [w for w in ast.walk(owner) if isinstance(w, ast.With)]
    assert [w.items[0].context_expr for w in withs] == [call]
    assert [ast.unparse(stmt) for stmt in withs[0].body] == ["db.add(delivery)", "db.flush()"]
    assert _imports(tree) == {"hashlib", "json", "math", "re", "uuid", "datetime", "sqlalchemy", "sqlalchemy.exc",
                              "core.interaction_identity", "core.models"}


def test_progress_module_owns_no_transaction_and_swallows_nothing():
    source = PROGRESS_PATH.read_text(encoding="utf-8")
    tokens = _code_tokens(source).split("\n")
    for forbidden in ("commit", "rollback", "begin", "begin_nested", "close", "SessionLocal", "get_db",
                      "HTTPException", "fastapi", "Depends", "anthropic"):
        assert forbidden not in tokens, forbidden
    tree = ast.parse(source)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Try)], "aucune capture d'erreur DB"
    # typing : ProgressOutcome (NamedTuple, R1-C4), aucune I/O.
    assert _imports(tree) == {"logging", "re", "uuid", "datetime", "typing", "core.models"}


@pytest.mark.parametrize("path", [SERVICE_PATH, PROGRESS_PATH], ids=lambda p: p.name)
def test_r1c1_modules_do_not_touch_t2_and_beyond(path):
    """R1-C1 ne crée ni CognitiveEvent ni SupportTrace et n'appelle aucun
    moteur pédagogique (T3+, Step 6, Progression)."""
    tokens = _code_tokens(path.read_text(encoding="utf-8"))
    for name in FORBIDDEN_RUNTIME_NAMES:
        assert name not in tokens, name
    for module in _imports(ast.parse(path.read_text(encoding="utf-8"))):
        assert not module.startswith(("core.adaptation", "core.inference", "core.observation", "core.longitudinal",
                                      "core.progress", "core.taxonomy", "core.cognitive")), module


def _function_source(name) -> str:
    """Code de la route SANS sa docstring (seuls les noms appelés comptent)."""
    tree = ast.parse(API_PATH.read_text(encoding="utf-8"))
    (node,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if ast.get_docstring(node) is not None:
        node.body = node.body[1:]
    return ast.unparse(node)


# R1-C2 : chaque route passe par UN point d'entrée de l'orchestrateur T2
# Décrypter (core/decryptage_cognitive_runtime.py), jamais directement par
# les primitives de capture, et n'appelle toujours aucun T3+.
R1C2_ROUTE_ENTRY = {"decryptage": "capture_user_turn", "ack_assistant_delivery": "capture_delivered_response"}


@pytest.mark.parametrize("route", ["decryptage", "ack_assistant_delivery"])
def test_runtime_routes_call_t2_only_through_the_r1c2_runtime_nor_open_hidden_sessions(route):
    source = _function_source(route)
    for name in (*FORBIDDEN_RUNTIME_NAMES, "SessionLocal", "_track_construction_these_progress",
                 "_track_session_progress"):
        assert name not in source, name
    assert source.count(R1C2_ROUTE_ENTRY[route] + "(") == 1
    params = inspect.signature(getattr(api, route)).parameters
    assert params["db"].default.dependency is core.db.get_db


def test_write_tracking_is_extracted_from_api():
    """L'ancien tracking (SessionLocal + commit + swallow) n'existe plus dans
    api.py ; seules les lectures historiques gardent leur Session courte."""
    source = API_PATH.read_text(encoding="utf-8")
    for name in ("_track_construction_these_progress", "_track_session_progress", "_STEP_MARKER_RE", "_FACT_FIELDS"):
        assert name not in source, name
        assert not hasattr(api, name), name
    assert api._find_active_analysis_session is dp.find_active_analysis_session
    assert api.apply_construction_these_progress is dp.apply_construction_these_progress


def test_decryptage_request_requires_runtime_identities():
    base = {"ticker": "MC.PA", "user_id": USER, "conversation_key": CONV, "client_turn_id": str(TURN)}
    request = api.DecryptageRequest(**base)
    assert request.client_turn_id == TURN and type(request.client_turn_id) is uuid.UUID
    assert (request.user_id, request.conversation_key) == (USER, CONV)
    for field in ("user_id", "conversation_key", "client_turn_id"):
        with pytest.raises(ValidationError):
            api.DecryptageRequest(**{k: v for k, v in base.items() if k != field})
    for field, value in (("user_id", ""), ("user_id", "   "), ("conversation_key", ""), ("conversation_key", "a\x00"),
                         ("client_turn_id", "not-a-uuid"), ("client_turn_id", ""), ("user_id", None)):
        with pytest.raises(ValidationError):
            api.DecryptageRequest(**{**base, field: value})
    # Valeurs validées sans transformation sémantique.
    assert api.DecryptageRequest(**{**base, "user_id": " u "}).user_id == " u "
    # UUID canonique quel que soit le format reçu.
    assert api.DecryptageRequest(**{**base, "client_turn_id": str(TURN).upper()}).client_turn_id == TURN
    assert set(api.DecryptageRequest.model_fields) == {
        "ticker", "question", "context", "last_method_id", "level", "user_id", "conversation_key", "client_turn_id"}
    assert set(api.AssistantDeliveryAckRequest.model_fields) == {"user_id", "conversation_key"}


# --- empreintes ------------------------------------------------------------

def test_request_fingerprint_formula_is_frozen():
    expected_payload = {
        "schema_version": "decryptage-request-fingerprint-v2", "user_id": USER, "conversation_key": CONV,
        "client_turn_id": str(TURN), "ticker_input": "MC.PA", "question": "Que fait LVMH ?", "context": "",
        "last_method_id": None, "level": "debutant",
    }
    canonical = json.dumps(expected_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    fingerprint = ad.decryptage_request_fingerprint(**_fingerprint_kwargs())
    assert fingerprint == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert len(fingerprint) == 64 and fingerprint == fingerprint.lower()
    assert all(c in "0123456789abcdef" for c in fingerprint)
    assert ad.decryptage_request_fingerprint(**_fingerprint_kwargs()) == fingerprint


@pytest.mark.parametrize("field, value", [
    ("user_id", OTHER_USER), ("conversation_key", "conv-2"), ("client_turn_id", uuid.uuid4()), ("ticker_input", "AAPL"),
    ("question", "Autre question"), ("context", "[USER] historique"), ("last_method_id", "construction_these"),
    ("level", "intermediaire"),
])
def test_request_fingerprint_changes_with_every_consumed_field(field, value):
    assert (ad.decryptage_request_fingerprint(**_fingerprint_kwargs(**{field: value}))
            != ad.decryptage_request_fingerprint(**_fingerprint_kwargs()))


def test_request_fingerprint_is_pure_and_hashes_the_ticker_input_not_its_resolution(monkeypatch):
    """Le fingerprint n'a aucun paramètre pour les données de marché, la
    réponse, le marqueur, la session, l'horodatage ou l'assistant_turn_id.
    Il hashe la SAISIE du ticker avec une normalisation locale
    (strip().upper()) et n'appelle jamais le résolveur : « LVMH » et
    « MC.PA » sont deux payloads différents, même s'ils désignent le même
    instrument."""
    assert set(inspect.signature(ad.decryptage_request_fingerprint).parameters) == {
        "user_id", "conversation_key", "client_turn_id", "ticker_input", "question", "context", "last_method_id",
        "level"}
    assert "normalize_ticker" not in _code_tokens(SERVICE_PATH.read_text(encoding="utf-8"))
    import core.ticker_resolver

    def no_io(*args, **kwargs):
        raise AssertionError("aucune I/O dans le fingerprint")

    monkeypatch.setattr(core.ticker_resolver.requests, "get", no_io)
    fingerprint = ad.decryptage_request_fingerprint(**_fingerprint_kwargs(ticker_input="MC.PA"))
    for same in (" mc.pa ", "Mc.Pa", "MC.PA\n"):
        assert ad.decryptage_request_fingerprint(**_fingerprint_kwargs(ticker_input=same)) == fingerprint
    assert normalize_ticker("LVMH") == "MC.PA"
    assert ad.decryptage_request_fingerprint(**_fingerprint_kwargs(ticker_input="LVMH")) != fingerprint


@pytest.mark.parametrize("field, value", [
    ("user_id", ""), ("conversation_key", " "), ("client_turn_id", str(TURN)), ("ticker_input", ""), ("ticker_input", " "), ("ticker_input", None),
    ("question", None),
    ("context", 3), ("level", None), ("last_method_id", 1), ("question", "a\x00b"),
])
def test_request_fingerprint_rejects_invalid_inputs(field, value):
    with pytest.raises(InvalidAssistantDeliveryInput):
        ad.decryptage_request_fingerprint(**_fingerprint_kwargs(**{field: value}))


def test_visible_content_fingerprint_is_sha256_of_exact_text():
    for text in ("Réponse.", "", "  espaces conservés  \n", "émoji 📈"):
        assert ad.visible_content_fingerprint(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    with pytest.raises(InvalidAssistantDeliveryInput):
        ad.visible_content_fingerprint(None)


# --- marqueurs privés ------------------------------------------------------

@pytest.mark.parametrize("step", STEPS)
def test_each_known_marker_is_removed_and_kept_private(step):
    visible, marker = dp.extract_step_marker(f"Texte pédagogique.\n\n<!--ORYX_STEP:{step}-->\n")
    assert (visible, marker) == ("Texte pédagogique.", step)


def test_text_without_marker_is_untouched():
    text = "Texte sans marqueur.  \n"
    assert dp.extract_step_marker(text) == (text, None)


@pytest.mark.parametrize("raw", ["inconnu", "Moat", "business ", "", "swot-final", "étape"])
def test_unknown_marker_is_removed_but_never_kept(raw, caplog):
    visible, marker = dp.extract_step_marker(f"Texte.<!--ORYX_STEP:{raw}-->")
    assert (visible, marker) == ("Texte.", None)
    assert "ORYX_STEP" not in visible
    assert "Texte." not in caplog.text


def test_first_marker_wins_and_all_markers_are_removed():
    visible, marker = dp.extract_step_marker("A<!--ORYX_STEP:moat-->B<!--ORYX_STEP:chiffres-->")
    assert (visible, marker) == ("AB", "moat")


def test_progress_refuses_steps_outside_the_closed_vocabulary_before_db():
    for step in ("inconnu", None, "Moat"):
        with pytest.raises(dp.InvalidDecryptageStep):
            dp.apply_construction_these_progress(_NoDB(), user_id=USER, ticker="MC.PA", step=step,
                                                 thesis_text=None, data=None, user_contribution=False,
                                                 answered_step=None)


# --- validation structurelle avant tout accès à la base ---------------------

INVALID_CLAIMS = [
    {"surface": "education"}, {"surface": "Decryptage"}, {"surface": ""}, {"surface": None},
    {"delivery_ordinal": 0}, {"delivery_ordinal": -1}, {"delivery_ordinal": True}, {"delivery_ordinal": 1.0},
    {"user_id": ""}, {"user_id": None}, {"conversation_key": " "}, {"conversation_key": "a\x00"},
    {"source_user_turn_id": str(TURN)}, {"source_user_turn_id": None},
    {"request_fingerprint": "A" * 64}, {"request_fingerprint": "a" * 63}, {"request_fingerprint": None},
    {"visible_content_fingerprint": "g" * 64},
    {"response_payload": None}, {"response_payload": []}, {"response_payload": {"price": Decimal("1")}},
    {"response_payload": {"price": float("nan")}}, {"response_payload": {"t": (1, 2)}},
    {"response_payload": {"analysis": "a\x00"}}, {"response_payload": {1: "x"}},
    {"response_payload": {**PAYLOAD_A, "assistant_turn_id": "x"}},
    {"response_payload": {**PAYLOAD_A, "delivery_status": "pending"}},
    {"response_payload": {**PAYLOAD_A, "private_metadata": {}}},
    {"private_metadata": None}, {"private_metadata": {"d": datetime(2026, 1, 1)}},
]


@pytest.mark.parametrize("overrides", INVALID_CLAIMS, ids=lambda o: repr(o)[:60])
def test_claim_rejects_invalid_inputs_before_db(overrides):
    with pytest.raises(InvalidAssistantDeliveryInput):
        ad.claim_delivery(_NoDB(), **_claim_kwargs(**overrides))


@pytest.mark.parametrize("version", ["assistant-delivery-v2", "", None, "ASSISTANT-DELIVERY-V1"])
def test_claim_refuses_unknown_schema_versions_before_db(version):
    with pytest.raises(UnsupportedAssistantDeliverySchema):
        ad.claim_delivery(_NoDB(), **_claim_kwargs(delivery_schema_version=version))


@pytest.mark.parametrize("overrides", [o for o in INVALID_CLAIMS if set(o) & {
    "surface", "delivery_ordinal", "user_id", "conversation_key", "source_user_turn_id", "request_fingerprint"}],
    ids=lambda o: repr(o)[:60])
def test_preflight_rejects_invalid_inputs_before_db(overrides):
    kwargs = {**_identity(), "request_fingerprint": FP_A, **overrides}
    with pytest.raises(InvalidAssistantDeliveryInput):
        ad.preflight_delivery(_NoDB(), **kwargs)


def test_ack_and_bind_reject_invalid_inputs_before_db():
    for kwargs in ({"assistant_turn_id": str(TURN), "user_id": USER, "conversation_key": CONV},
                   {"assistant_turn_id": TURN, "user_id": "", "conversation_key": CONV},
                   {"assistant_turn_id": TURN, "user_id": USER, "conversation_key": None}):
        with pytest.raises(InvalidAssistantDeliveryInput):
            ad.acknowledge_delivery(_NoDB(), **kwargs)
    for kwargs in ({"delivery_id": str(TURN), "analysis_session_id": SESSION_ID},
                   {"delivery_id": TURN, "analysis_session_id": None}):
        with pytest.raises(InvalidAssistantDeliveryInput):
            ad.bind_analysis_session(_NoDB(), **kwargs)


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL — service
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine(pg_url):  # noqa: F811
    """Schéma = head (0011) + users 'u' / 'u2' et une analysis_session de 'u'
    (SESSION_ID), créé une fois pour le module ; chaque test nettoie."""
    eng = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    _upgrade_head_with_users(pg_url, eng)
    yield eng
    eng.dispose()


@pytest.fixture
def Sessions(engine):
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)  # = core.db.SessionLocal
    yield factory
    with engine.begin() as conn:
        # R1-C2 : liens cognitifs et T2 créés par la route avant les livraisons.
        for table in ("decryptage_cognitive_links", "support_traces", "cognitive_events"):
            conn.execute(sa.text(f"DELETE FROM {table}"))
        conn.execute(sa.text("DELETE FROM assistant_deliveries"))
        for table in ("user_statements", "analysis_facts", "investment_theses"):
            conn.execute(sa.text(f"DELETE FROM {table}"))
        conn.execute(sa.text("DELETE FROM conversation_identities"))
        conn.execute(sa.text("DELETE FROM analysis_sessions WHERE id <> :s"), {"s": SESSION_ID})
        conn.execute(sa.text("UPDATE analysis_sessions SET status = 'in_progress', current_step = NULL, "
                             "completed_at = NULL WHERE id = :s"), {"s": SESSION_ID})
        conn.execute(sa.text("DELETE FROM users WHERE id NOT IN ('u', 'u2')"))


@pytest.fixture
def db(Sessions):
    session = Sessions()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def clock(monkeypatch):
    base = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    ticks = itertools.count()
    monkeypatch.setattr(ad, "_utcnow", lambda: base + timedelta(seconds=next(ticks)))
    return base


DELIVERY_COLUMNS = ("id, user_id, conversation_key, surface, source_user_turn_id, delivery_ordinal, "
                    "analysis_session_id, request_fingerprint, visible_content_fingerprint, response_payload, "
                    "private_metadata, status, delivery_schema_version, generated_at, delivered_at")


def _rows(engine):
    """État commité, lu par une connexion indépendante."""
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(
            f"SELECT {DELIVERY_COLUMNS} FROM assistant_deliveries ORDER BY generated_at, id")).mappings()]


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def _committed_claim(Sessions, **overrides):
    with Sessions() as session:
        delivery, created = ad.claim_delivery(session, **_claim_kwargs(**overrides))
        assert created
        delivery_id = delivery.id
        session.commit()
    return delivery_id


def test_pg_claim_creates_a_pending_versioned_delivery(engine, Sessions, clock):
    with Sessions() as session:
        delivery, created = ad.claim_delivery(session, **_claim_kwargs())
        delivery_id = delivery.id
        assert created is True
        assert type(delivery.id) is uuid.UUID and delivery.id != TURN
        assert delivery.status == "pending"
        assert delivery.generated_at == clock
        _assert_utc(delivery.generated_at)
        assert delivery.delivered_at is None
        assert delivery.analysis_session_id is None
        session.commit()
    [row] = _rows(engine)
    assert row == {
        "id": delivery_id, "user_id": USER, "conversation_key": CONV, "surface": "decryptage",
        "source_user_turn_id": TURN, "delivery_ordinal": 1, "analysis_session_id": None,
        "request_fingerprint": FP_A, "visible_content_fingerprint": FP_A, "response_payload": PAYLOAD_A,
        "private_metadata": {"decryptage_step_marker": "business"}, "status": "pending",
        "delivery_schema_version": "assistant-delivery-v1", "generated_at": clock, "delivered_at": None,
    }
    # La conversation est enregistrée dans le registre R1-B pour ce user.
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT user_id FROM conversation_identities WHERE conversation_key = :c"),
                            {"c": CONV}).scalar_one() == USER


def test_pg_payloads_are_copied_defensively(engine, Sessions):
    payload = json.loads(json.dumps(PAYLOAD_A))
    private = {"decryptage_step_marker": None}
    with Sessions() as session:
        delivery, _ = ad.claim_delivery(session, **_claim_kwargs(response_payload=payload, private_metadata=private))
        assert delivery.response_payload == PAYLOAD_A and delivery.response_payload is not payload
        assert delivery.private_metadata is not private
        payload["analysis"] = "modifié après coup"
        private["decryptage_step_marker"] = "moat"
        session.commit()
    [row] = _rows(engine)
    assert row["response_payload"] == PAYLOAD_A
    assert row["private_metadata"] == {"decryptage_step_marker": None}


def test_pg_exact_retry_returns_the_same_delivery_without_mutation(engine, Sessions):
    delivery_id = _committed_claim(Sessions)
    before = _rows(engine)
    with Sessions() as session, _Statements(engine) as statements:
        delivery, created = ad.claim_delivery(session, **_claim_kwargs())
        assert (delivery.id, created) == (delivery_id, False)
        session.commit()
    assert _writes(statements) == []
    assert _rows(engine) == before


def test_pg_concurrent_generation_with_another_text_is_not_a_collision(engine, Sessions):
    """Même tour, même requête, réponse générée différente (B) : la
    livraison canonique reste A ; aucune collision sur le texte généré."""
    delivery_id = _committed_claim(Sessions)
    with Sessions() as session:
        delivery, created = ad.claim_delivery(session, **_claim_kwargs(
            response_payload=PAYLOAD_B, visible_content_fingerprint=FP_B,
            private_metadata={"decryptage_step_marker": "moat"}))
        assert (delivery.id, created) == (delivery_id, False)
        assert delivery.response_payload == PAYLOAD_A
        session.commit()
    [row] = _rows(engine)
    assert row["response_payload"] == PAYLOAD_A and row["visible_content_fingerprint"] == FP_A
    assert row["private_metadata"] == {"decryptage_step_marker": "business"}


def test_pg_same_turn_with_another_request_is_an_identity_collision(engine, Sessions):
    _committed_claim(Sessions)
    before = _rows(engine)
    with Sessions() as session:
        with pytest.raises(AssistantTurnIdentityCollision) as failure:
            ad.claim_delivery(session, **_claim_kwargs(request_fingerprint=FP_B, response_payload=PAYLOAD_B))
        assert "Réponse" not in str(failure.value) and FP_B not in str(failure.value)
        with pytest.raises(AssistantTurnIdentityCollision):
            ad.preflight_delivery(session, **_identity(), request_fingerprint=FP_B)
        session.commit()
    assert _rows(engine) == before


def test_pg_preflight_absent_identical_and_collision_without_mutation(engine, Sessions):
    with Sessions() as session:
        assert ad.preflight_delivery(session, **_identity(), request_fingerprint=FP_A) is None
        session.commit()
    delivery_id = _committed_claim(Sessions)
    before = _rows(engine)
    with Sessions() as session, _Statements(engine) as statements:
        canonical = ad.preflight_delivery(session, **_identity(), request_fingerprint=FP_A)
        assert canonical.id == delivery_id and canonical.response_payload == PAYLOAD_A
        with pytest.raises(AssistantTurnIdentityCollision):
            ad.preflight_delivery(session, **_identity(), request_fingerprint=FP_B)
        # Autre tour, autre ordinal : identités distinctes.
        assert ad.preflight_delivery(session, **_identity(source_user_turn_id=uuid.uuid4()),
                                     request_fingerprint=FP_A) is None
        assert ad.preflight_delivery(session, **_identity(delivery_ordinal=2), request_fingerprint=FP_A) is None
        session.commit()
    assert _writes(statements) == []
    assert _rows(engine) == before


def test_pg_unknown_user_is_refused_without_any_write(engine, Sessions):
    with Sessions() as session, _Statements(engine) as statements:
        with pytest.raises(AssistantDeliveryUserNotFound):
            ad.preflight_delivery(session, **_identity(user_id="ghost"), request_fingerprint=FP_A)
        with pytest.raises(AssistantDeliveryUserNotFound):
            ad.claim_delivery(session, **_claim_kwargs(user_id="ghost"))
        session.commit()
    assert _writes(statements) == []
    assert _count(engine, "users") == 2 and _count(engine, "conversation_identities") == 0


def test_pg_conversation_of_another_user_is_refused(engine, Sessions):
    """U1 + C1 (à U1) : valide. U2 + C1 : refusé, sans contourner le
    registre R1-B ; aucune livraison ni réattribution."""
    delivery_id = _committed_claim(Sessions)
    with Sessions() as session:
        for call in (lambda: ad.preflight_delivery(session, **_identity(user_id=OTHER_USER), request_fingerprint=FP_A),
                     lambda: ad.claim_delivery(session, **_claim_kwargs(user_id=OTHER_USER))):
            with pytest.raises(AssistantDeliveryOwnershipConflict):
                call()
        session.commit()
    assert [r["id"] for r in _rows(engine)] == [delivery_id]
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT user_id FROM conversation_identities")).scalars().all() == [USER]


def test_pg_unsupported_schema_version_is_never_read_permissively(engine, Sessions):
    delivery_id = _committed_claim(Sessions)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE assistant_deliveries SET delivery_schema_version = 'assistant-delivery-v2'"))
    with Sessions() as session:
        with pytest.raises(UnsupportedAssistantDeliverySchema):
            ad.preflight_delivery(session, **_identity(), request_fingerprint=FP_A)
        with pytest.raises(UnsupportedAssistantDeliverySchema):
            ad.claim_delivery(session, **_claim_kwargs())
        with pytest.raises(UnsupportedAssistantDeliverySchema):
            ad.acknowledge_delivery(session, assistant_turn_id=delivery_id, user_id=USER, conversation_key=CONV)
        with pytest.raises(UnsupportedAssistantDeliverySchema):
            ad.bind_analysis_session(session, delivery_id=delivery_id, analysis_session_id=SESSION_ID)


def test_pg_lost_race_reads_the_winner_inside_a_local_savepoint(engine, Sessions):
    """Deux transactions concurrentes, même identité, même requête, réponses
    générées différentes : une seule ligne, un seul gagnant ; le perdant
    attend le verrou de l'index UNIQUE, revient à son SAVEPOINT, relit la
    livraison canonique et sa transaction reste utilisable (ses écritures
    antérieures sont conservées et commitées)."""
    with Sessions() as first, Sessions() as second:
        winner, created = ad.claim_delivery(first, **_claim_kwargs())
        winner_id = winner.id
        assert created
        # Écriture préalable du perdant dans SA transaction, hors SAVEPOINT.
        second.add(AnalysisSession(user_id=OTHER_USER, ticker="AAPL", status="in_progress"))

        def lose(session):
            delivery, was_created = ad.claim_delivery(session, **_claim_kwargs(
                response_payload=PAYLOAD_B, visible_content_fingerprint=FP_B))
            # Transaction non empoisonnée : elle lit encore et écrit encore.
            session.execute(sa.text("SELECT 1")).scalar_one()
            return delivery.id, was_created, dict(delivery.response_payload)

        result, error = _run_blocked(engine, first, second, lose)
    assert error is None
    assert result == (winner_id, False, PAYLOAD_A)
    [row] = _rows(engine)
    assert row["id"] == winner_id and row["response_payload"] == PAYLOAD_A
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM analysis_sessions WHERE user_id = :u"),
                            {"u": OTHER_USER}).scalar_one() == 1


def test_pg_race_against_a_rolled_back_winner_creates_the_delivery(engine, Sessions):
    with Sessions() as first, Sessions() as second:
        ad.claim_delivery(first, **_claim_kwargs())
        result, error = _run_blocked(engine, first, second, lambda s: ad.claim_delivery(
            s, **_claim_kwargs(response_payload=PAYLOAD_B))[1], release="rollback")
    assert error is None and result is True
    [row] = _rows(engine)
    assert row["response_payload"] == PAYLOAD_B


def test_pg_race_with_another_request_is_a_collision(engine, Sessions):
    with Sessions() as first, Sessions() as second:
        ad.claim_delivery(first, **_claim_kwargs())
        result, error = _run_blocked(engine, first, second, lambda s: ad.claim_delivery(
            s, **_claim_kwargs(request_fingerprint=FP_B)))
    assert result is None and isinstance(error, AssistantTurnIdentityCollision)
    [row] = _rows(engine)
    assert row["request_fingerprint"] == FP_A


def test_pg_other_integrity_errors_are_propagated(engine, Sessions, monkeypatch):
    """Seule la violation de uq_assistant_deliveries_turn_ordinal est
    traduite ; une autre IntegrityError remonte telle quelle."""
    monkeypatch.setattr(ad, "_require_existing_user", lambda db, user_id: None)
    monkeypatch.setattr(ad, "_resolve_conversation", lambda db, **kw: None)
    with Sessions() as session:
        with pytest.raises(sa.exc.IntegrityError, match="assistant_deliveries_user_id_fkey"):
            ad.claim_delivery(session, **_claim_kwargs(user_id="ghost", conversation_key="never-registered"))


def test_pg_ack_pending_to_delivered_then_idempotent(engine, Sessions, clock):
    delivery_id = _committed_claim(Sessions)
    [before] = _rows(engine)
    with Sessions() as session:
        delivery = ad.acknowledge_delivery(session, assistant_turn_id=delivery_id, user_id=USER, conversation_key=CONV)
        assert delivery.status == "delivered"
        session.commit()
    [after] = _rows(engine)
    _assert_utc(after["delivered_at"])
    assert after["delivered_at"] > before["generated_at"]
    assert {k: v for k, v in after.items() if k not in ("status", "delivered_at")} == {
        k: v for k, v in before.items() if k not in ("status", "delivered_at")}
    assert after["status"] == "delivered"

    with Sessions() as session, _Statements(engine) as statements:
        again = ad.acknowledge_delivery(session, assistant_turn_id=delivery_id, user_id=USER, conversation_key=CONV)
        assert again.delivered_at == after["delivered_at"]
        session.commit()
    assert _writes(statements) == []
    assert _rows(engine) == [after]
    assert _count(engine, "assistant_deliveries") == 1


def test_pg_ack_ownership_and_not_found(engine, Sessions):
    delivery_id = _committed_claim(Sessions)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO conversation_identities VALUES ('conv-u2', :u, now()), "
                             "('conv-u-bis', :me, now())"), {"u": OTHER_USER, "me": USER})
    before = _rows(engine)
    with Sessions() as session:
        for user_id, conversation_key in ((OTHER_USER, CONV), (OTHER_USER, "conv-u2"), (USER, "conv-u-bis")):
            with pytest.raises(AssistantDeliveryOwnershipConflict) as failure:
                ad.acknowledge_delivery(session, assistant_turn_id=delivery_id, user_id=user_id,
                                        conversation_key=conversation_key)
            assert "Réponse" not in str(failure.value)
        with pytest.raises(AssistantDeliveryNotFound):
            ad.acknowledge_delivery(session, assistant_turn_id=uuid.uuid4(), user_id=USER, conversation_key=CONV)
        session.commit()
    assert _rows(engine) == before


def test_pg_bind_analysis_session_rules(engine, Sessions):
    delivery_id = _committed_claim(Sessions)
    with Sessions() as session:
        other = AnalysisSession(user_id=OTHER_USER, ticker="AAPL", status="in_progress")
        second = AnalysisSession(user_id=USER, ticker="AAPL", status="in_progress")
        session.add_all([other, second])
        session.commit()
        other_id, second_id = other.id, second.id

    with Sessions() as session:
        for analysis_session_id in (other_id, uuid.uuid4()):
            with pytest.raises(AnalysisSessionBindingConflict):
                ad.bind_analysis_session(session, delivery_id=delivery_id, analysis_session_id=analysis_session_id)
        with pytest.raises(AssistantDeliveryNotFound):
            ad.bind_analysis_session(session, delivery_id=uuid.uuid4(), analysis_session_id=SESSION_ID)
        delivery = ad.bind_analysis_session(session, delivery_id=delivery_id, analysis_session_id=SESSION_ID)
        assert delivery.analysis_session_id == SESSION_ID
        session.commit()
    [bound] = _rows(engine)
    assert bound["analysis_session_id"] == SESSION_ID

    with Sessions() as session, _Statements(engine) as statements:
        ad.bind_analysis_session(session, delivery_id=delivery_id, analysis_session_id=SESSION_ID)
        with pytest.raises(AnalysisSessionBindingConflict):
            ad.bind_analysis_session(session, delivery_id=delivery_id, analysis_session_id=second_id)
        session.commit()
    assert _writes(statements) == []
    assert _rows(engine) == [bound]

    # Liaison possible aussi sur une livraison déjà delivered.
    other_turn = _committed_claim(Sessions, source_user_turn_id=uuid.uuid4())
    with Sessions() as session:
        ad.acknowledge_delivery(session, assistant_turn_id=other_turn, user_id=USER, conversation_key=CONV)
        assert ad.bind_analysis_session(session, delivery_id=other_turn,
                                        analysis_session_id=second_id).analysis_session_id == second_id
        session.commit()


def test_pg_service_never_writes_pedagogical_tables(engine, Sessions):
    delivery_id = _committed_claim(Sessions)
    with Sessions() as session:
        ad.acknowledge_delivery(session, assistant_turn_id=delivery_id, user_id=USER, conversation_key=CONV)
        ad.bind_analysis_session(session, delivery_id=delivery_id, analysis_session_id=SESSION_ID)
        session.commit()
    assert _count(engine, "cognitive_events") == 0 and _count(engine, "support_traces") == 0


# --------------------------------------------------------------------------
# 3. Contre un vrai PostgreSQL — /decryptage et ACK
# --------------------------------------------------------------------------

DATA = {"name": "LVMH", "current_price": 600.0, "currency": "EUR", "operating_margin": 0.26, "roe": 0.24,
        "net_cash": -1.0e9}
SNAPSHOT = {"operating_margin": 0.26, "roe": 0.24, "net_cash": -1.0e9}


class _Externals:
    """Remplace fetch_financial_data et anthropic.Anthropic. Compte les
    appels et vérifie qu'aucune transaction DB n'est ouverte pendant
    chacun : ni dans la Session de la route, ni ailleurs sur la base
    (pg_stat_activity)."""

    def __init__(self, engine):
        self.engine = engine
        self.resolver_calls = 0
        self.fetch_calls = 0
        self.claude_calls = 0
        self.texts = []
        self.default_text = "Réponse pédagogique."
        self.sessions = []
        self.on_fetch = None
        self.messages = self

    def _assert_no_open_transaction(self):
        for session in self.sessions:
            assert not session.in_transaction()
        with self.engine.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()")).scalar_one() == 0
            conn.rollback()

    def resolve(self, raw):
        """normalize_ticker réel (peut interroger EODHD Search), compté."""
        self.resolver_calls += 1
        self._assert_no_open_transaction()
        return normalize_ticker(raw)

    def fetch(self, ticker):
        self.fetch_calls += 1
        self._assert_no_open_transaction()
        if self.on_fetch:
            hook, self.on_fetch = self.on_fetch, None
            hook()
        return {"success": True, "data": dict(DATA)}

    def __call__(self, *args, **kwargs):
        return self

    def create(self, **kwargs):
        self.claude_calls += 1
        self._assert_no_open_transaction()
        text = self.texts.pop(0) if self.texts else self.default_text
        block = type("Block", (), {"type": "text", "text": text})()
        return type("Resp", (), {"content": [block], "stop_reason": "end_turn"})()


@pytest.fixture
def ext(engine, Sessions, monkeypatch):
    # La session in_progress MC.PA de la fixture de schéma n'interfère pas
    # avec les parcours (remise à in_progress par le nettoyage de Sessions).
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE analysis_sessions SET status = 'abandoned' WHERE id = :s"), {"s": SESSION_ID})
    fake = _Externals(engine)
    monkeypatch.setattr(core.db, "SessionLocal", Sessions)
    monkeypatch.setattr(api.anthropic, "Anthropic", fake)
    monkeypatch.setattr(api, "fetch_financial_data", fake.fetch)
    monkeypatch.setattr(api, "normalize_ticker", fake.resolve)
    return fake


def _payload(**overrides):
    payload = {"ticker": "MC.PA", "question": "", "context": "", "last_method_id": None, "level": "debutant",
               "user_id": USER, "conversation_key": CONV, "client_turn_id": str(TURN)}
    payload.update(overrides)
    return payload


def _call(Sessions, ext, **overrides):
    with Sessions() as session:
        ext.sessions.append(session)
        try:
            return api.decryptage(api.DecryptageRequest(**_payload(**overrides)), db=session)
        finally:
            ext.sessions.remove(session)


def _ack(Sessions, assistant_turn_id, user_id=USER, conversation_key=CONV):
    with Sessions() as session:
        return api.ack_assistant_delivery(
            uuid.UUID(assistant_turn_id), api.AssistantDeliveryAckRequest(user_id=user_id,
                                                                          conversation_key=conversation_key),
            db=session)


def _product(engine):
    """Empreinte de l'état produit T1-C1 (hors SESSION_ID de la fixture)."""
    with engine.connect() as conn:
        return {
            "sessions": [tuple(r) for r in conn.execute(sa.text(
                "SELECT id, status, current_step, completed_at FROM analysis_sessions WHERE id <> :s "
                "ORDER BY started_at"), {"s": SESSION_ID})],
            "statements": [tuple(r) for r in conn.execute(sa.text(
                "SELECT step, statement_text, analysis_session_id FROM user_statements ORDER BY id"))],
            "facts": [tuple(r) for r in conn.execute(sa.text(
                "SELECT fact_type, fact_value, analysis_session_id FROM analysis_facts ORDER BY id"))],
            "theses": [tuple(r) for r in conn.execute(sa.text(
                "SELECT thesis_text, analysis_session_id FROM investment_theses ORDER BY id"))],
        }


def _marker(step):
    return f"Réponse pédagogique.\n<!--ORYX_STEP:{step}-->"


def _route_metadata(marker):
    """private_metadata d'une livraison créée par la route depuis R1-C2 :
    marqueur privé + opt-in explicite de la capture cognitive (runtime V2
    depuis R1-C4)."""
    return {"decryptage_step_marker": marker, "cognitive_runtime_version": "decryptage-cognitive-runtime-v2"}


def test_pg_route_new_generation_persists_a_pending_delivery_before_responding(engine, Sessions, ext):
    ext.texts = [_marker("business")]
    response = _call(Sessions, ext)
    assert set(response) == {"success", "ticker", "name", "method_used", "analysis", "price", "currency",
                             "disclaimer", "assistant_turn_id", "delivery_status"}
    assert response["delivery_status"] == "pending"
    assert response["analysis"] == "Réponse pédagogique."
    assert "ORYX_STEP" not in json.dumps(response) and "decryptage_step_marker" not in response
    [row] = _rows(engine)
    assert str(row["id"]) == response["assistant_turn_id"]
    assert row["status"] == "pending" and row["delivered_at"] is None
    assert row["response_payload"] == {k: v for k, v in response.items()
                                       if k not in ("assistant_turn_id", "delivery_status")}
    assert row["response_payload"] == {
        "success": True, "ticker": "MC.PA", "name": "LVMH", "method_used": "construction_these",
        "analysis": "Réponse pédagogique.", "price": 600.0, "currency": "EUR",
        "disclaimer": "Analyse éducative uniquement. Ne constitue pas un conseil en investissement."}
    assert row["visible_content_fingerprint"] == hashlib.sha256("Réponse pédagogique.".encode()).hexdigest()
    assert row["private_metadata"] == _route_metadata("business")
    assert row["request_fingerprint"] == ad.decryptage_request_fingerprint(**_fingerprint_kwargs(question=""))
    assert ext.resolver_calls == 1
    assert (row["surface"], row["source_user_turn_id"], row["delivery_ordinal"]) == ("decryptage", TURN, 1)
    # Progression construction_these appliquée et liée dans la même transaction.
    [session] = _product(engine)["sessions"]
    assert session[1:3] == ("in_progress", "business")
    assert row["analysis_session_id"] == session[0]
    assert (ext.fetch_calls, ext.claude_calls) == (1, 1)
    assert _count(engine, "cognitive_events") == 0 and _count(engine, "support_traces") == 0


def test_pg_route_retry_replays_the_canonical_payload_without_external_calls(engine, Sessions, ext):
    ext.texts = [_marker("business")]
    first = _call(Sessions, ext)
    product = _product(engine)
    # Le marché bouge, Claude répondrait autre chose : rien n'est recalculé.
    ext.default_text = _marker("moat")
    DATA_BEFORE = dict(DATA)
    try:
        DATA["current_price"] = 999.0
        retry = _call(Sessions, ext)
    finally:
        DATA.update(DATA_BEFORE)
    assert retry == first
    assert (ext.resolver_calls, ext.fetch_calls, ext.claude_calls) == (1, 1, 1)
    assert _product(engine) == product
    assert _count(engine, "assistant_deliveries") == 1


def test_pg_route_retry_after_ack_returns_delivered(engine, Sessions, ext):
    ext.texts = [_marker("business"), _marker("moat")]
    first = _call(Sessions, ext)
    _call(Sessions, ext, client_turn_id=str(uuid.uuid4()), question="Ils vendent du luxe.", context="h")
    product = _product(engine)
    acked = _ack(Sessions, first["assistant_turn_id"])
    assert acked == {"success": True, "assistant_turn_id": first["assistant_turn_id"], "delivery_status": "delivered"}
    retry = _call(Sessions, ext)
    assert retry == {**first, "delivery_status": "delivered"}
    assert (ext.resolver_calls, ext.fetch_calls, ext.claude_calls) == (2, 2, 2)
    assert _product(engine) == product
    # Second ACK : idempotent, delivered_at inchangé.
    delivered_at = [r["delivered_at"] for r in _rows(engine) if str(r["id"]) == first["assistant_turn_id"]]
    assert _ack(Sessions, first["assistant_turn_id"])["delivery_status"] == "delivered"
    assert [r["delivered_at"] for r in _rows(engine) if str(r["id"]) == first["assistant_turn_id"]] == delivered_at


def test_pg_route_same_turn_with_another_request_is_refused_before_external_calls(engine, Sessions, ext):
    first = _call(Sessions, ext)
    before = (_rows(engine), _product(engine))
    for overrides in ({"question": "Autre question"}, {"context": "h"}, {"ticker": "AAPL"}, {"level": "avance"},
                      {"last_method_id": "construction_these"}):
        with pytest.raises(HTTPException) as failure:
            _call(Sessions, ext, **overrides)
        assert failure.value.status_code == 409
        assert "Réponse" not in json.dumps(failure.value.detail)
    assert (ext.resolver_calls, ext.fetch_calls, ext.claude_calls) == (1, 1, 1)
    assert (_rows(engine), _product(engine)) == before
    assert first["delivery_status"] == "pending"


def test_pg_route_replay_never_resolves_the_ticker(engine, Sessions, ext):
    """« LVMH » (résolu en MC.PA au premier passage) + même client_turn_id +
    même payload : replay canonique sans normalize_ticker, données ni
    Claude. Une saisie locale équivalente (casse, espaces) est le même
    payload."""
    first = _call(Sessions, ext, ticker="LVMH")
    assert first["ticker"] == "MC.PA"
    assert (ext.resolver_calls, ext.fetch_calls, ext.claude_calls) == (1, 1, 1)
    assert _call(Sessions, ext, ticker="LVMH") == first
    assert _call(Sessions, ext, ticker=" lvmh ") == first
    assert (ext.resolver_calls, ext.fetch_calls, ext.claude_calls) == (1, 1, 1)


def test_pg_route_other_ticker_input_with_same_turn_collides_before_resolution(engine, Sessions, ext):
    """« LVMH » puis « MC.PA » avec le même client_turn_id : collision 409
    AVANT normalize_ticker, même si les deux saisies désignent le même
    instrument (la résolution externe ne fait pas partie de l'identité)."""
    _call(Sessions, ext, ticker="LVMH")
    before = (_rows(engine), _product(engine))
    with pytest.raises(HTTPException) as failure:
        _call(Sessions, ext, ticker="MC.PA")
    assert failure.value.status_code == 409
    assert (ext.resolver_calls, ext.fetch_calls, ext.claude_calls) == (1, 1, 1)
    assert (_rows(engine), _product(engine)) == before


def test_pg_route_replay_works_when_the_resolver_is_down(engine, Sessions, ext, monkeypatch):
    first = _call(Sessions, ext, ticker="LVMH")

    def resolver_down(raw):
        raise RuntimeError("EODHD Search indisponible")

    monkeypatch.setattr(api, "normalize_ticker", resolver_down)
    assert _call(Sessions, ext, ticker="LVMH") == first
    assert (ext.fetch_calls, ext.claude_calls) == (1, 1)


def test_pg_route_replay_on_a_cold_worker_makes_no_resolver_call(engine, Sessions, ext, monkeypatch):
    """Worker A résout un nom libre via EODHD Search et crée la livraison ;
    retry du même tour sur un worker B au cache vide : aucun appel au
    résolveur réel (ni cache, ni HTTP)."""
    import core.ticker_resolver
    monkeypatch.setattr(api, "normalize_ticker", lambda raw: "RMS.PA")  # résolution de A (EODHD simulé)
    first = _call(Sessions, ext, ticker="Hermes")
    assert first["ticker"] == "RMS.PA"

    http_calls = []
    monkeypatch.setattr(core.ticker_resolver, "_RESOLUTION_CACHE", {})
    monkeypatch.setattr(core.ticker_resolver.requests, "get", lambda *a, **k: http_calls.append(a) or 1 / 0)
    monkeypatch.setattr(api, "normalize_ticker", ext.resolve)  # résolveur réel, cache vide
    assert _call(Sessions, ext, ticker="Hermes") == first
    assert ext.resolver_calls == 0 and http_calls == []
    assert core.ticker_resolver._RESOLUTION_CACHE == {}
    assert (ext.fetch_calls, ext.claude_calls) == (1, 1)


def test_pg_route_unknown_user_fails_closed_before_anything(engine, Sessions, ext):
    with pytest.raises(HTTPException) as failure:
        _call(Sessions, ext, user_id="ghost")
    assert failure.value.status_code == 404
    assert (ext.resolver_calls, ext.fetch_calls, ext.claude_calls) == (0, 0, 0)
    assert _count(engine, "users") == 2
    assert _count(engine, "conversation_identities") == 0 and _count(engine, "assistant_deliveries") == 0


def test_pg_route_conversation_of_another_user_is_refused_before_external_calls(engine, Sessions, ext):
    _call(Sessions, ext)
    with pytest.raises(HTTPException) as failure:
        _call(Sessions, ext, user_id=OTHER_USER, client_turn_id=str(uuid.uuid4()))
    assert failure.value.status_code == 403
    assert "LVMH" not in json.dumps(failure.value.detail) and "Réponse" not in json.dumps(failure.value.detail)
    assert (ext.fetch_calls, ext.claude_calls) == (1, 1)
    assert _count(engine, "assistant_deliveries") == 1


def test_pg_route_lost_race_never_mutates_the_product(engine, Sessions, ext):
    """Deux workers passent le preflight du même tour : chacun appelle les
    données et Claude (réponses différentes). Le premier à revendiquer
    gagne ; le perdant renvoie la réponse canonique du gagnant et ne fait
    jamais avancer AnalysisSession (ni déclaration, ni fait, ni thèse)."""
    ext.texts = [_marker("business")]
    first = _call(Sessions, ext, client_turn_id=str(uuid.uuid4()))  # tour 1 : business
    # R1-C3 : la réponse rendue est acquittée (ouvre l'event business) ; le
    # tour suivant est alors une vraie contribution, seule source de
    # UserStatement.
    _ack(Sessions, first["assistant_turn_id"])
    turn = str(uuid.uuid4())
    results = {}

    def concurrent_worker():
        results["winner"] = _call(Sessions, ext, client_turn_id=turn, question="Ils vendent du luxe.", context="h",
                                  last_method_id="construction_these")

    # Worker perdant : a passé le preflight et attend les données ; pendant
    # ce temps, le worker concurrent fait tout son tour et commite.
    ext.on_fetch = concurrent_worker
    ext.texts = ["Réponse du gagnant.\n<!--ORYX_STEP:moat-->", "Réponse du perdant.\n<!--ORYX_STEP:chiffres-->"]
    loser = _call(Sessions, ext, client_turn_id=turn, question="Ils vendent du luxe.", context="h",
                  last_method_id="construction_these")
    assert loser == results["winner"]
    assert loser["analysis"] == "Réponse du gagnant."
    assert (ext.fetch_calls, ext.claude_calls) == (3, 3)
    product = _product(engine)
    [session] = product["sessions"]
    assert session[2] == "moat"
    assert [s[:2] for s in product["statements"]] == [("business", "Ils vendent du luxe.")]
    assert len(product["facts"]) == len(SNAPSHOT) and product["theses"] == []
    assert [r["private_metadata"] for r in _rows(engine)][-1] == _route_metadata("moat")
    assert _count(engine, "assistant_deliveries") == 2


def test_pg_route_product_failure_rolls_back_the_delivery(engine, Sessions, ext, monkeypatch):
    real = dp.apply_construction_these_progress

    def failing(db, **kwargs):
        real(db, **kwargs)  # mutations flushées, puis échec
        raise sa.exc.OperationalError("UPDATE analysis_sessions", {}, Exception("connexion perdue"))

    monkeypatch.setattr(api, "apply_construction_these_progress", failing)
    ext.texts = [_marker("business")]
    with pytest.raises(HTTPException) as failure:
        _call(Sessions, ext)
    assert failure.value.status_code == 500 and failure.value.detail["retryable"] is True
    assert _count(engine, "assistant_deliveries") == 0
    assert _product(engine) == {"sessions": [], "statements": [], "facts": [], "theses": []}

    # Retry ultérieur du même tour : repart proprement.
    monkeypatch.setattr(api, "apply_construction_these_progress", real)
    ext.texts = [_marker("business")]
    response = _call(Sessions, ext)
    assert response["delivery_status"] == "pending"
    assert len(_product(engine)["sessions"]) == 1
    assert (ext.fetch_calls, ext.claude_calls) == (2, 2)


def test_pg_route_claim_failure_returns_no_success(engine, Sessions, ext, monkeypatch):
    def broken_claim(db, **kwargs):
        raise sa.exc.OperationalError("INSERT", {}, Exception("base indisponible"))

    monkeypatch.setattr(api, "claim_delivery", broken_claim)
    ext.texts = [_marker("business")]
    with pytest.raises(HTTPException) as failure:
        _call(Sessions, ext)
    assert failure.value.status_code == 500
    assert _count(engine, "assistant_deliveries") == 0 and _product(engine)["sessions"] == []


def test_pg_route_unknown_marker_is_stripped_and_never_tracked(engine, Sessions, ext):
    ext.texts = ["Réponse.\n<!--ORYX_STEP:inventee-->"]
    response = _call(Sessions, ext)
    assert response["analysis"] == "Réponse."
    [row] = _rows(engine)
    assert row["private_metadata"] == _route_metadata(None)
    assert row["analysis_session_id"] is None
    assert _product(engine)["sessions"] == []


def test_pg_route_marker_outside_construction_these_is_private_but_not_tracked(engine, Sessions, ext, monkeypatch):
    monkeypatch.setattr(api, "lookup_method", lambda *a, **k: None)
    ext.texts = [_marker("moat")]
    response = _call(Sessions, ext, question="Et la dette ?", context="h")
    assert response["method_used"] is None and response["analysis"] == "Réponse pédagogique."
    [row] = _rows(engine)
    assert row["private_metadata"] == _route_metadata("moat")
    assert row["analysis_session_id"] is None and _product(engine)["sessions"] == []


def test_pg_route_swot_final_without_active_session_is_delivered_unbound(engine, Sessions, ext):
    ext.texts = [_marker("swot_final")]
    _call(Sessions, ext, question="Ma thèse ?", context="h", last_method_id="construction_these")
    [row] = _rows(engine)
    assert row["private_metadata"] == _route_metadata("swot_final")
    assert row["analysis_session_id"] is None
    assert _product(engine) == {"sessions": [], "statements": [], "facts": [], "theses": []}


def test_pg_route_full_attempt_binds_every_delivery_to_the_session(engine, Sessions, ext):
    turns = [("business", "", "")] + [(s, f"réponse avant {s}", "h") for s in STEPS[1:]]
    for step, question, context in turns:
        ext.texts = [_marker(step)]
        response = _call(Sessions, ext, client_turn_id=str(uuid.uuid4()), question=question, context=context,
                         last_method_id="construction_these" if context else None)
        # R1-C3 : ACK de chaque réponse rendue (comme le frontend) : les
        # réponses suivantes sont des contributions réelles R1-C2.
        _ack(Sessions, response["assistant_turn_id"])
    product = _product(engine)
    [session] = product["sessions"]
    assert session[1:3] == ("completed", "swot_final")
    assert {r["analysis_session_id"] for r in _rows(engine)} == {session[0]}
    assert product["theses"] == [("réponse avant swot_final", session[0])]
    assert [s[0] for s in product["statements"]] == list(STEPS[:-1])


def test_pg_route_empty_claude_response_uses_the_fallback_delivery(engine, Sessions, ext):
    ext.texts = ["   ", "  \n"]
    ext_sleep = []
    original_sleep = api.time.sleep
    api.time.sleep = ext_sleep.append
    try:
        response = _call(Sessions, ext)
    finally:
        api.time.sleep = original_sleep
    assert response["analysis"] == "Je n'ai pas bien compris, tu peux reformuler ta question ?"
    assert response["delivery_status"] == "pending"
    [row] = _rows(engine)
    assert row["private_metadata"] == _route_metadata(None)
    assert "price" not in row["response_payload"]  # comportement fallback historique conservé
    assert _product(engine)["sessions"] == []


def test_pg_route_errors_before_visible_content_create_no_delivery(engine, Sessions, ext, monkeypatch):
    monkeypatch.setattr(api, "fetch_financial_data", lambda t: {"success": False, "error": "inconnu"})
    assert _call(Sessions, ext)["success"] is False

    def boom(**kwargs):
        raise RuntimeError("Claude indisponible")

    monkeypatch.setattr(api, "fetch_financial_data", ext.fetch)
    monkeypatch.setattr(ext, "create", boom)
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    response = _call(Sessions, ext)
    assert response["success"] is False and "assistant_turn_id" not in response
    assert _count(engine, "assistant_deliveries") == 0
    # Le registre de conversation (preflight commité) est la seule écriture.
    assert _count(engine, "conversation_identities") == 1


def test_pg_ack_route_ownership_not_found_and_minimal_response(engine, Sessions, ext):
    response = _call(Sessions, ext)
    turn = response["assistant_turn_id"]
    for user_id, conversation_key, status in ((OTHER_USER, CONV, 403), (USER, "autre-conv", 403)):
        with pytest.raises(HTTPException) as failure:
            _ack(Sessions, turn, user_id=user_id, conversation_key=conversation_key)
        assert failure.value.status_code == status
    with pytest.raises(HTTPException) as failure:
        _ack(Sessions, str(uuid.uuid4()))
    assert failure.value.status_code == 404
    [row] = _rows(engine)
    assert row["status"] == "pending"
    acked = _ack(Sessions, turn)
    assert acked == {"success": True, "assistant_turn_id": turn, "delivery_status": "delivered"}
    assert _count(engine, "assistant_deliveries") == 1
    assert _count(engine, "cognitive_events") == 0 and _count(engine, "support_traces") == 0


def test_pg_many_turns_and_acks_never_write_t3_and_beyond(engine, Sessions, ext):
    """Depuis R1-C2, les tours Décrypter ACKés écrivent T2 (CognitiveEvents,
    liens ; testé en détail dans tests/test_decryptage_cognitive_runtime.py)
    mais jamais T3+ (observations, inférence, états de compétence)."""
    for n in range(3):
        ext.texts = [_marker(STEPS[n])]
        response = _call(Sessions, ext, client_turn_id=str(uuid.uuid4()), question=f"q{n}" if n else "",
                         context="h" if n else "", last_method_id="construction_these" if n else None)
        _ack(Sessions, response["assistant_turn_id"])
    assert {r["status"] for r in _rows(engine)} == {"delivered"}
    assert _count(engine, "decryptage_cognitive_links") == 3
    assert _count(engine, "cognitive_events") == 3 and _count(engine, "support_traces") == 0
    for table in ("observation_evaluation_runs", "pedagogical_observations", "competency_inference_runs",
                  "user_competency_states"):
        assert _count(engine, table) == 0, table


def test_pg_http_layer_maps_errors_and_requires_identities(engine, Sessions, ext):
    """Au niveau HTTP (application ASGI réelle + get_db) : 422 sans
    identités, 404 utilisateur inconnu, 409 collision ; jamais de payload
    dans l'erreur."""
    def override_get_db():
        session = Sessions()
        try:
            yield session
        finally:
            session.close()

    api.app.dependency_overrides[core.db.get_db] = override_get_db
    try:
        client = _AsgiClient()
        assert client.post("/decryptage", json={"ticker": "MC.PA"}).status_code == 422
        assert client.post("/decryptage", json=_payload(user_id="ghost")).status_code == 404
        ok = client.post("/decryptage", json=_payload())
        assert ok.status_code == 200 and ok.json()["delivery_status"] == "pending"
        collision = client.post("/decryptage", json=_payload(question="autre"))
        assert collision.status_code == 409 and "Réponse" not in collision.text
        ack = client.post(f"/api/runtime/assistant-deliveries/{ok.json()['assistant_turn_id']}/ack",
                          json={"user_id": USER, "conversation_key": CONV})
        assert ack.status_code == 200 and ack.json()["delivery_status"] == "delivered"
        assert "analysis" not in ack.json() and "fingerprint" not in ack.text
        assert client.post("/api/runtime/assistant-deliveries/not-a-uuid/ack",
                           json={"user_id": USER, "conversation_key": CONV}).status_code == 422
    finally:
        api.app.dependency_overrides.clear()


class _AsgiClient:
    """Requêtes HTTP réelles contre l'application ASGI (httpx, dépendance
    d'anthropic), sans serveur."""

    def post(self, url, json):
        import asyncio
        import httpx

        async def go():
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(url, json=json)

        return asyncio.run(go())
