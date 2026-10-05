"""Tests de R1-C2 : premier branchement réel de la capture cognitive T2 sur
Décrypter (core/decryptage_cognitive_runtime.py, /decryptage et ACK).

1. Tests sans base (toujours exécutés) : constantes et vocabulaires fermés
   (identiques aux CHECK de la migration 0012), API publique, aucune
   transaction possédée, aucun appel externe, aucun T3+ (évaluation,
   observation, taxonomie, longitudinal, inférence, Step 6), task_kind
   toujours NULL, stimulus et SupportTrace limités au contenu visible,
   frontière architecturale (seul ce module appelle la capture ; Web-V2
   ignore tout du cognitif).

2. Tests contre un vrai PostgreSQL : uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test". Sinon SKIPPÉS :
   rien n'est simulé avec SQLite (verrous, SAVEPOINT, contraintes).

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_r1c2_test \\
           python -m pytest tests/test_decryptage_cognitive_runtime.py

   Les parcours appellent directement api.decryptage /
   api.ack_assistant_delivery avec une Session (fixtures et faux appels
   externes de tests/test_assistant_delivery.py) ; les courses réelles
   utilisent deux connexions et pg_blocking_pids() (aucun sleep
   arbitraire).

Scénarios fonctionnels A à R de la spec R1-C2, plus concurrence et
idempotence.
"""
import ast
import inspect
import json
import logging
import uuid

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

import api
from core import assistant_delivery as ad
from core import cognitive_capture as cc
from core import decryptage_cognitive_runtime as dcr
from core.decryptage_progress import DECRYPTAGE_STEP_MARKERS
from tests.test_assistant_delivery import (  # noqa: F401 — fixtures
    FP_A,
    PAYLOAD_A,
    STEPS,
    Sessions,
    _call,
    _count,
    _product,
    _rows,
    engine,
    ext,
)
from tests.test_cognitive_capture import _run_blocked, _Statements, _writes
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import SESSION_ID, _code_tokens
from tests.test_migration_0005_cognitive_support_traces import USER

RUNTIME_PATH = REPO_ROOT / "core" / "decryptage_cognitive_runtime.py"
MIGRATION_PATH = REPO_ROOT / "alembic" / "versions" / "0012_decryptage_cognitive_links.py"
API_PATH = REPO_ROOT / "api.py"
CONV = "conv-u"
CONV_2 = "conv-u-reprise"
VERSION = "decryptage-cognitive-runtime-v1"
# Tout ce que R1-C2 ne doit JAMAIS appeler ni importer (T3+, Step 6).
T3_AND_BEYOND = (
    "observation_service", "PedagogicalObservation", "ObservationEvaluationRun", "start_evaluation_run",
    "add_observation", "taxonomy_service", "CoreCapabilityDefinition", "longitudinal_service",
    "inference_service", "CompetencyInferenceRun", "UserCompetencyState", "user_competency_states",
    "adaptation_state", "progression_view", "progress_evidence",
)
EVALUATION_WORDS = ("competenc", "capabilit", "evidence", "polarity", "correct", "score", "mastery", "percentile",
                    "streak", "xp", "rank", "support_level", "stage")


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_versions_and_constants_are_explicit():
    assert dcr.COGNITIVE_RUNTIME_VERSION == dcr.CAPTURE_VERSION == VERSION
    assert dcr.EVENT_BUILDER_VERSION == "decryptage-event-builder-v1"
    assert dcr.ADMISSION_VERSION == "decryptage-event-admission-v1"
    assert dcr.EVENT_BUILDER_VERSION != cc.EVENT_BUILDER_VERSION
    assert (dcr.EVENT_ORIGIN, dcr.SURFACE, dcr.SUPPORT_KIND) == ("decryptage", "decryptage", "assistant_response")
    assert dcr.SURFACE == ad.DECRYPTAGE_SURFACE
    assert dcr.SEGMENTATION_ORDINAL == 1
    assert (dcr.AWAITING_DELIVERY, dcr.CAPTURED) == ("awaiting_delivery", "captured")
    assert dcr.TASK_STEPS == ("business", "moat", "chiffres", "valorisation", "risques")
    assert "swot_final" not in dcr.TASK_STEPS


def test_vocabularies_are_closed_and_identical_to_the_migration_checks():
    assert dcr.INPUT_ACTIONS == ("no_open_event", "no_user_contribution", "contribution_appended",
                                 "event_closed_context_change", "event_abandoned_context_change")
    assert dcr.RESPONSE_ACTIONS == ("opened_event", "continued_event", "continued_without_boundary_signal",
                                    "transitioned_event", "closed_terminal", "no_cognitive_action")
    assert "closed_without_next_task" not in dcr.RESPONSE_ACTIONS
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    for value in (*dcr.INPUT_ACTIONS, *dcr.RESPONSE_ACTIONS, dcr.AWAITING_DELIVERY, dcr.CAPTURED):
        assert f"'{value}'" in source, value
    assert "closed_without_next_task" not in source


def test_public_api_is_exact_and_keyword_only():
    tree = ast.parse(RUNTIME_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == {
        "runtime_private_metadata", "capture_user_turn", "capture_delivered_response"}
    assert list(inspect.signature(dcr.capture_user_turn).parameters) == ["db", "delivery", "ticker", "user_text"]
    assert list(inspect.signature(dcr.capture_delivered_response).parameters) == ["db", "delivery"]
    for name in ("capture_user_turn", "capture_delivered_response"):
        params = list(inspect.signature(getattr(dcr, name)).parameters.values())[1:]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params), name
    for exc in (dcr.CognitiveLinkInvariantError, dcr.AmbiguousOpenDecryptageEvents):
        assert issubclass(exc, dcr.DecryptageCognitiveRuntimeError)
        assert not issubclass(exc, (sa.exc.SQLAlchemyError, cc.CognitiveCaptureError, ad.AssistantDeliveryError))


def _imports(tree) -> set:
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{alias.name}" if node.module == "core" else node.module
                         for alias in node.names}
    return imported


def test_runtime_owns_no_transaction_makes_no_external_call_and_ignores_fastapi():
    source = RUNTIME_PATH.read_text(encoding="utf-8")
    tokens = _code_tokens(source).split("\n")
    for forbidden in ("commit", "rollback", "begin", "begin_nested", "close", "SessionLocal", "get_db",
                      "HTTPException", "fastapi", "Depends", "anthropic", "requests", "fetch_financial_data",
                      "normalize_ticker", "delete", "merge", "expunge", "connection", "execute_raw"):
        assert forbidden not in tokens, forbidden
    assert _imports(ast.parse(source)) == {"logging", "datetime", "sqlalchemy", "core.cognitive_capture",
                                           "core.decryptage_progress", "core.models"}


def test_runtime_never_calls_t3_and_beyond_nor_evaluates():
    source = RUNTIME_PATH.read_text(encoding="utf-8")
    tokens = _code_tokens(source)
    for name in T3_AND_BEYOND:
        assert name not in tokens, name
    lowered = set(tokens.lower().split("\n"))
    for word in EVALUATION_WORDS:
        assert not [t for t in lowered if word in t], word
    for module in _imports(ast.parse(source)):
        assert not module.startswith(("core.adaptation", "core.inference", "core.observation", "core.longitudinal",
                                      "core.progress", "core.taxonomy", "core.pedagog")), module


def test_runtime_writes_t2_only_through_cognitive_capture_primitives():
    """Aucune écriture directe de CognitiveEvent / SupportTrace /
    AssistantDelivery : seules les primitives R1-B, et seul le lien est
    instancié ici."""
    tree = ast.parse(RUNTIME_PATH.read_text(encoding="utf-8"))
    called = {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
              and n.func.value.id == "cognitive_capture"}
    assert called == {"cognitive_capture.open_event_idempotent", "cognitive_capture.append_user_contribution",
                      "cognitive_capture.add_support_trace", "cognitive_capture.finalize_event",
                      "cognitive_capture.abandon_event"}
    constructed = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id[:1].isupper()}
    assert constructed == {"DecryptageCognitiveLink", "CognitiveLinkInvariantError", "AmbiguousOpenDecryptageEvents",
                           "DecryptageCognitiveRuntimeError"}
    # task_kind est toujours NULL et support_refs_before n'est jamais fourni.
    [opening] = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "open_event_idempotent"]
    kwargs = {k.arg: k.value for k in opening.keywords}
    assert isinstance(kwargs["task_kind"], ast.Constant) and kwargs["task_kind"].value is None
    assert ast.unparse(kwargs["source_turn_refs"]) == "(delivery.id,)"
    assert "support_refs_before" not in _code_tokens(RUNTIME_PATH.read_text(encoding="utf-8"))


def test_runtime_private_metadata_marks_the_opt_in_and_keeps_the_marker():
    for marker in (*DECRYPTAGE_STEP_MARKERS, None):
        assert dcr.runtime_private_metadata(marker) == {"decryptage_step_marker": marker,
                                                        "cognitive_runtime_version": VERSION}
    for marker in ("quality", "Moat", ""):
        with pytest.raises(dcr.DecryptageCognitiveRuntimeError):
            dcr.runtime_private_metadata(marker)


def test_stimulus_is_only_what_the_frontend_displays():
    payload = {**PAYLOAD_A, "analysis": "Texte visible."}
    assert dcr._visible_stimulus(payload) == {"visible_content": "Texte visible.", "price": 600.0, "currency": "EUR"}
    fallback = {"success": True, "ticker": "MC.PA", "name": "LVMH", "method_used": None, "analysis": "Reformule ?",
                "disclaimer": "d"}
    assert dcr._visible_stimulus(fallback) == {"visible_content": "Reformule ?"}
    assert dcr._visible_stimulus({**payload, "price": None}) == {"visible_content": "Texte visible."}


def test_api_marks_every_new_delivery_and_captures_only_for_the_winner():
    tree = ast.parse(API_PATH.read_text(encoding="utf-8"))
    (route,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "decryptage"]
    source = ast.unparse(route)
    assert "private_metadata=runtime_private_metadata(step_marker)" in source
    [guard] = [n for n in ast.walk(route) if isinstance(n, ast.If)
               and any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "capture_user_turn"
                       for c in ast.walk(n))]
    assert ast.unparse(guard.test) == "created"
    # Rattachement AVANT la progression produit, après le claim.
    assert source.index("claim_delivery(") < source.index("capture_user_turn(") < source.index(
        "apply_construction_these_progress(")
    (ack,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ack_assistant_delivery"]
    ack_source = ast.unparse(ack)
    assert (ack_source.index("lock_delivery_for_ack(") < ack_source.index("capture_delivered_response(")
            < ack_source.index("acknowledge_delivery(") < ack_source.index("db.commit()"))


def test_web_v2_knows_nothing_about_cognitive_capture_and_has_no_close_endpoint():
    html = (REPO_ROOT / "web-v2" / "index.html").read_text(encoding="utf-8")
    for name in ("cognitive_event", "CognitiveEvent", "support_trace", "SupportTrace", "capture_state",
                 "cognitive_runtime", "competenc", "/close"):
        assert name not in html, name
    assert not [r for r in api.app.routes if "close" in getattr(r, "path", "")]


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

LINK_COLUMNS = ("assistant_delivery_id, capture_version, input_action, input_event_id, abandoned_event_id, "
                "capture_state, response_action, response_event_id, support_trace_id, created_at, captured_at")


def _links(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(
            f"SELECT {LINK_COLUMNS} FROM decryptage_cognitive_links ORDER BY created_at")).mappings()]


def _link(engine, delivery_id):
    with engine.connect() as conn:
        row = conn.execute(sa.text(f"SELECT {LINK_COLUMNS} FROM decryptage_cognitive_links "
                                   "WHERE assistant_delivery_id = :d"), {"d": uuid.UUID(str(delivery_id))})
        row = row.mappings().one_or_none()
        return dict(row) if row else None


def _events(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(
            "SELECT id, status, user_id, conversation_key, event_origin, task_kind, analysis_session_id, "
            "stimulus_snapshot, user_work_snapshot, event_builder_version, admission_version, event_schema_version "
            "FROM cognitive_events ORDER BY started_at, id")).mappings()]


def _event(engine, event_id):
    [event] = [e for e in _events(engine) if e["id"] == event_id]
    return event


def _traces(engine, event_id=None):
    with engine.connect() as conn:
        query = "SELECT id, cognitive_event_id, sequence_no, support_kind, support_payload FROM support_traces"
        if event_id is not None:
            return [dict(r) for r in conn.execute(sa.text(query + " WHERE cognitive_event_id = :e ORDER BY "
                                                          "sequence_no"), {"e": event_id}).mappings()]
        return [dict(r) for r in conn.execute(sa.text(query + " ORDER BY created_at")).mappings()]


def _delivery(engine, delivery_id):
    [row] = [r for r in _rows(engine) if str(r["id"]) == str(delivery_id)]
    return row


def _t2_state(engine):
    """Empreinte complète de l'état T2 + liens + livraisons."""
    return {"events": _events(engine), "traces": _traces(engine), "links": _links(engine),
            "deliveries": [(r["id"], r["status"], r["delivered_at"]) for r in _rows(engine)]}


def _reply(label, marker=None):
    text = f"Réponse {label}."
    return text + (f"\n<!--ORYX_STEP:{marker}-->" if marker is not None else "")


def _send(Sessions, ext, reply, question="", *, context=None, conv=CONV, ticker="MC.PA", turn=None):  # noqa: F811
    """Un tour /decryptage. context vide = début de conversation (méthode
    construction_these forcée) ; sinon verrou construction_these."""
    ext.texts = [reply]
    if context is None:
        context = "h" if question else ""
    response = _call(Sessions, ext, client_turn_id=turn or str(uuid.uuid4()), question=question, context=context,
                     conversation_key=conv, ticker=ticker, last_method_id="construction_these" if context else None)
    assert response["delivery_status"] in ("pending", "delivered")
    return response


def _ack(Sessions, response, conv=CONV):  # noqa: F811
    with Sessions() as session:
        return api.ack_assistant_delivery(uuid.UUID(response["assistant_turn_id"]),
                                          api.AssistantDeliveryAckRequest(user_id=USER, conversation_key=conv),
                                          db=session)


def _turn(Sessions, ext, reply, question="", **kwargs):  # noqa: F811
    """Tour + ACK (rendu confirmé). Retourne la réponse /decryptage."""
    response = _send(Sessions, ext, reply, question, **kwargs)
    assert _ack(Sessions, response, conv=kwargs.get("conv", CONV))["delivery_status"] == "delivered"
    return response


def _id(response):
    return uuid.UUID(response["assistant_turn_id"])


def _session_id(engine, ticker="MC.PA"):
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT id FROM analysis_sessions WHERE ticker = :t AND id <> :s "
                                    "ORDER BY started_at DESC LIMIT 1"), {"t": ticker, "s": SESSION_ID}).scalar_one()


def _contribution_ids(engine):
    return [c["contribution_id"] for e in _events(engine) for c in e["user_work_snapshot"]]


def _no_t3(engine):
    for table in ("observation_evaluation_runs", "pedagogical_observations", "observation_capabilities",
                  "longitudinal_assessment_runs", "competency_inference_runs", "competency_stage_claims",
                  "user_competency_states"):
        assert _count(engine, table) == 0, table


# --- A. premier tour -------------------------------------------------------

@pytest.mark.parametrize("trigger", ["", "Analyse LVMH"])
def test_pg_a_first_turn_opens_business_at_ack_without_contribution(engine, Sessions, ext, trigger):
    first = _send(Sessions, ext, _reply("A1", "business"), trigger, context="")
    link = _link(engine, first["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"], link["abandoned_event_id"]) == ("no_open_event", None, None)
    assert (link["capture_state"], link["response_action"], link["captured_at"]) == ("awaiting_delivery", None, None)
    assert link["capture_version"] == VERSION
    assert _events(engine) == []  # rien n'est ouvert avant le rendu confirmé

    _ack(Sessions, first)
    [event] = _events(engine)
    session_id = _session_id(engine)
    assert event["status"] == "open" and event["event_origin"] == "decryptage" and event["task_kind"] is None
    assert (event["user_id"], event["conversation_key"], event["analysis_session_id"]) == (USER, CONV, session_id)
    assert event["stimulus_snapshot"] == {"visible_content": "Réponse A1.", "price": 600.0, "currency": "EUR"}
    assert event["user_work_snapshot"] == []  # le déclencheur n'est jamais une contribution
    assert (event["event_builder_version"], event["admission_version"], event["event_schema_version"]) == (
        "decryptage-event-builder-v1", "decryptage-event-admission-v1", "cognitive-event-v2")
    link = _link(engine, first["assistant_turn_id"])
    assert (link["capture_state"], link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "captured", "opened_event", event["id"], None)
    assert link["captured_at"] is not None
    assert _delivery(engine, first["assistant_turn_id"])["status"] == "delivered"
    assert _traces(engine) == []
    # Le stimulus ne contient rien d'invisible.
    dumped = json.dumps(event["stimulus_snapshot"])
    for hidden in ("ORYX_STEP", "business", "decryptage_step_marker", "cognitive_runtime_version", "method"):
        assert hidden not in dumped, hidden
    _no_t3(engine)


def test_pg_a_event_identity_is_the_opening_delivery(engine, Sessions, ext):
    """source_turn_refs = (assistant_delivery.id,), ordinal 1, conversation
    d'origine : l'event_dedup_key est reproductible depuis la livraison."""
    first = _turn(Sessions, ext, _reply("A1", "business"))
    [event] = _events(engine)
    with engine.connect() as conn:
        key = conn.execute(sa.text("SELECT event_dedup_key FROM cognitive_events")).scalar_one()
    assert key == cc._event_dedup_key(user_id=USER, conversation_key=CONV, source_turn_refs=(_id(first),),
                                      segmentation_ordinal=1, event_builder_version="decryptage-event-builder-v1")


# --- B. business -> business -------------------------------------------------

def test_pg_b_same_marker_continues_the_event_with_a_support_trace(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    [event] = _events(engine)
    turn_2 = str(uuid.uuid4())
    second = _send(Sessions, ext, _reply("A2", "business"), "Ils vendent du luxe.", turn=turn_2)
    link = _link(engine, second["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"]) == ("contribution_appended", event["id"])
    [contribution] = _event(engine, event["id"])["user_work_snapshot"]
    assert {k: v for k, v in contribution.items() if k != "occurred_at"} == {
        "contribution_id": turn_2, "source_turn_ref": turn_2, "phase": 1, "surface": "decryptage",
        "session_ref": CONV, "text": "Ils vendent du luxe.", "support_refs_before": []}

    _ack(Sessions, second)
    [trace] = _traces(engine, event["id"])
    assert (trace["support_kind"], trace["support_payload"], trace["sequence_no"]) == (
        "assistant_response", {"visible_content": "Réponse A2."}, 1)
    link = _link(engine, second["assistant_turn_id"])
    assert (link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "continued_event", event["id"], trace["id"])
    assert _event(engine, event["id"])["status"] == "open"
    assert len(_events(engine)) == 1

    # U3 hérite de S_A2 via support_refs_before (capturé par R1-B).
    _send(Sessions, ext, _reply("A3", "business"), "Et une marque forte.")
    work = _event(engine, event["id"])["user_work_snapshot"]
    assert [c["support_refs_before"] for c in work] == [[], [str(trace["id"])]]
    assert [c["phase"] for c in work] == [1, 2]


# --- C. business -> moat -----------------------------------------------------

def test_pg_c_next_marker_finalizes_and_opens_with_the_response_as_stimulus(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    [business] = _events(engine)
    second = _send(Sessions, ext, _reply("A2", "moat"), "Ils vendent du luxe.")
    assert _event(engine, business["id"])["status"] == "open"  # rien avant l'ACK
    _ack(Sessions, second)
    business_after, moat = _events(engine)
    assert business_after["status"] == "finalized"
    assert [c["text"] for c in business_after["user_work_snapshot"]] == ["Ils vendent du luxe."]
    assert moat["status"] == "open" and moat["user_work_snapshot"] == [] and moat["task_kind"] is None
    assert moat["stimulus_snapshot"]["visible_content"] == "Réponse A2."
    assert moat["analysis_session_id"] == business["analysis_session_id"]
    assert _traces(engine) == []  # A2 n'est PAS un SupportTrace de business
    link = _link(engine, second["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"]) == ("contribution_appended", business["id"])
    assert (link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "transitioned_event", moat["id"], None)


# --- D. risques -> swot_final ------------------------------------------------

def test_pg_d_swot_final_closes_risques_without_a_swot_event(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("business", "business"))
    for previous, step in zip(STEPS, STEPS[1:]):
        last = _turn(Sessions, ext, _reply(step, step), f"réponse {previous}")
    events = _events(engine)
    assert len(events) == 5 and {e["status"] for e in events} == {"finalized"}
    assert [e["stimulus_snapshot"]["visible_content"] for e in events] == [f"Réponse {s}." for s in STEPS[:-1]]
    assert [[c["text"] for c in e["user_work_snapshot"]] for e in events] == [[f"réponse {s}"] for s in STEPS[:-1]]
    link = _link(engine, last["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"]) == ("contribution_appended", events[-1]["id"])
    assert (link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "closed_terminal", None, None)
    assert _traces(engine) == []
    product = _product(engine)
    [session] = product["sessions"]
    assert session[1:3] == ("completed", "swot_final")  # comme avant R1-C2
    assert product["theses"] == [("réponse risques", session[0])]
    _no_t3(engine)


def test_pg_d_swot_final_without_open_event_never_opens_a_swot_event(engine, Sessions, ext):
    """Retour sur LVMH après un détour NVDA (event risques quitté) : la
    réponse swot_final complète l'AnalysisSession mais n'ouvre rien."""
    _turn(Sessions, ext, _reply("business", "business"))
    for previous, step in zip(STEPS[:4], STEPS[1:5]):
        _turn(Sessions, ext, _reply(step, step), f"réponse {previous}")
    _turn(Sessions, ext, _reply("N1"), "Analyse NVDA", ticker="NVDA")
    closed = [e["status"] for e in _events(engine)]
    assert closed == ["finalized"] * 4 + ["abandoned"]  # risques quitté sans travail
    final = _turn(Sessions, ext, _reply("bilan", "swot_final"), "Ma conclusion.")
    link = _link(engine, final["assistant_turn_id"])
    assert (link["input_action"], link["response_action"], link["response_event_id"]) == (
        "no_open_event", "no_cognitive_action", None)
    assert [e["status"] for e in _events(engine)] == closed
    assert _delivery(engine, final["assistant_turn_id"])["analysis_session_id"] is not None


# --- E / F. marqueur absent ou inconnu avec event open ------------------------

@pytest.mark.parametrize("reply", [_reply("A2"), "Réponse A2.\n<!--ORYX_STEP:quality-->"], ids=["absent", "inconnu"])
def test_pg_e_f_unusable_marker_keeps_the_event_open_with_a_support_trace(engine, Sessions, ext, reply):
    _turn(Sessions, ext, _reply("A1", "business"))
    [event] = _events(engine)
    second = _send(Sessions, ext, reply, "Ils vendent du luxe.")
    _ack(Sessions, second)
    [after] = _events(engine)  # aucun nouvel event, aucune transition inventée
    assert after["status"] == "open"
    assert [c["text"] for c in after["user_work_snapshot"]] == ["Ils vendent du luxe."]
    [trace] = _traces(engine, event["id"])
    assert trace["support_payload"] == {"visible_content": "Réponse A2."}
    link = _link(engine, second["assistant_turn_id"])
    assert (link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "continued_without_boundary_signal", event["id"], trace["id"])
    assert _delivery(engine, second["assistant_turn_id"])["private_metadata"]["decryptage_step_marker"] is None


def test_pg_f_unknown_marker_in_metadata_is_never_repaired(engine, Sessions, ext, caplog):
    """Même si private_metadata portait une valeur hors vocabulaire
    (« quality »), elle n'est jamais mappée (ni vers moat, ni ailleurs)."""
    _turn(Sessions, ext, _reply("A1", "business"))
    second = _send(Sessions, ext, _reply("A2", "moat"), "Ils vendent du luxe.")
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE assistant_deliveries SET private_metadata = jsonb_set(private_metadata, "
                             "'{decryptage_step_marker}', '\"quality\"') WHERE id = :d"), {"d": _id(second)})
    with caplog.at_level(logging.INFO, logger="core.decryptage_cognitive_runtime"):
        _ack(Sessions, second)
    assert len(_events(engine)) == 1 and _events(engine)[0]["status"] == "open"
    assert _link(engine, second["assistant_turn_id"])["response_action"] == "continued_without_boundary_signal"
    assert "anomaly=unknown_marker" in caplog.text


# --- G. marqueur absent sans event -------------------------------------------

def test_pg_g_unusable_marker_without_event_is_no_cognitive_action(engine, Sessions, ext):
    first = _send(Sessions, ext, _reply("A1"), "", context="")
    _ack(Sessions, first)
    assert _events(engine) == [] and _traces(engine) == []
    link = _link(engine, first["assistant_turn_id"])
    assert (link["input_action"], link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "no_open_event", "no_cognitive_action", None, None)


def test_pg_g_swot_final_without_event_or_session_opens_nothing(engine, Sessions, ext):
    first = _send(Sessions, ext, _reply("bilan", "swot_final"), "Ma thèse ?")
    _ack(Sessions, first)
    assert _events(engine) == []
    assert _link(engine, first["assistant_turn_id"])["response_action"] == "no_cognitive_action"


def test_pg_g_marker_outside_construction_these_is_not_a_boundary(engine, Sessions, ext, monkeypatch):
    """Marqueur reconnu mais progression produit non appliquée (autre
    méthode) : jamais d'event sans AnalysisSession."""
    monkeypatch.setattr(api, "lookup_method", lambda *a, **k: None)
    first = _send(Sessions, ext, _reply("A1", "moat"), "Et la dette ?")
    _ack(Sessions, first)
    assert _events(engine) == []
    assert _link(engine, first["assistant_turn_id"])["response_action"] == "no_cognitive_action"


# --- H. marqueur rétrograde --------------------------------------------------

def test_pg_h_retrograde_marker_keeps_the_event_open_with_a_diagnostic(engine, Sessions, ext, caplog):
    _turn(Sessions, ext, _reply("business", "business"))
    _turn(Sessions, ext, _reply("moat", "moat"), "réponse business")
    _turn(Sessions, ext, _reply("chiffres", "chiffres"), "réponse moat")
    chiffres = _events(engine)[-1]
    with caplog.at_level(logging.INFO, logger="core.decryptage_cognitive_runtime"):
        back = _turn(Sessions, ext, _reply("retour", "business"), "réponse chiffres")
    assert len(_events(engine)) == 3
    assert _event(engine, chiffres["id"])["status"] == "open"
    [trace] = _traces(engine, chiffres["id"])
    link = _link(engine, back["assistant_turn_id"])
    assert (link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "continued_without_boundary_signal", chiffres["id"], trace["id"])
    assert "anomaly=retrograde_marker from=chiffres to=business" in caplog.text


def test_pg_forward_skip_is_a_transition_with_a_diagnostic(engine, Sessions, ext, caplog):
    _turn(Sessions, ext, _reply("business", "business"))
    with caplog.at_level(logging.INFO, logger="core.decryptage_cognitive_runtime"):
        skip = _turn(Sessions, ext, _reply("chiffres", "chiffres"), "réponse business")
    business, chiffres = _events(engine)
    assert (business["status"], chiffres["status"]) == ("finalized", "open")
    assert _link(engine, skip["assistant_turn_id"])["response_action"] == "transitioned_event"
    assert "anomaly=step_skip from=business to=chiffres" in caplog.text


# --- I / J. changement de ticker ---------------------------------------------

def test_pg_i_ticker_change_finalizes_an_event_with_work(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    _turn(Sessions, ext, _reply("A2", "business"), "Ils vendent du luxe.")
    [lvmh] = _events(engine)
    lvmh_session = lvmh["analysis_session_id"]
    nvda = _send(Sessions, ext, _reply("N1", "business"), "Analyse NVDA", ticker="NVDA")
    link = _link(engine, nvda["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"], link["abandoned_event_id"]) == (
        "event_closed_context_change", None, lvmh["id"])
    after = _event(engine, lvmh["id"])
    assert after["status"] == "finalized"
    assert [c["text"] for c in after["user_work_snapshot"]] == ["Ils vendent du luxe."]  # NVDA jamais ajouté
    with engine.connect() as conn:  # l'AnalysisSession LVMH n'est pas abandonnée
        assert conn.execute(sa.text("SELECT status FROM analysis_sessions WHERE id = :s"),
                            {"s": lvmh_session}).scalar_one() == "in_progress"
    _ack(Sessions, nvda)
    new = _events(engine)[-1]
    assert new["analysis_session_id"] == _session_id(engine, "NVDA") != lvmh_session
    assert (new["status"], new["user_work_snapshot"]) == ("open", [])
    assert _link(engine, nvda["assistant_turn_id"])["response_action"] == "opened_event"


def test_pg_j_ticker_change_abandons_an_empty_event(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    [lvmh] = _events(engine)
    nvda = _send(Sessions, ext, _reply("N1"), "Analyse NVDA", ticker="NVDA")
    link = _link(engine, nvda["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"], link["abandoned_event_id"]) == (
        "event_abandoned_context_change", None, lvmh["id"])
    assert _event(engine, lvmh["id"])["status"] == "abandoned"
    _ack(Sessions, nvda)  # NVDA sans marqueur : aucun nouvel event
    assert len(_events(engine)) == 1
    assert _link(engine, nvda["assistant_turn_id"])["response_action"] == "no_cognitive_action"


# --- K. nouvelle conversation / reprise --------------------------------------

def test_pg_k_resume_in_a_new_conversation_continues_the_same_event(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    _turn(Sessions, ext, _reply("A2", "business"), "Ils vendent du luxe.")
    [event] = _events(engine)
    # « Nouvelle conversation » seule : aucune requête, rien n'est fermé.
    assert _event(engine, event["id"])["status"] == "open"
    # Reprise Web-V2 : nouvelle conversation_key, question vide, contexte vide.
    resume = _send(Sessions, ext, _reply("R1", "business"), "", context="", conv=CONV_2)
    link = _link(engine, resume["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"], link["abandoned_event_id"]) == (
        "no_user_contribution", None, None)
    _ack(Sessions, resume, conv=CONV_2)
    [after] = _events(engine)
    assert after["status"] == "open" and after["conversation_key"] == CONV  # origine conservée
    resume_trace = _traces(engine, event["id"])[-1]
    assert resume_trace["support_payload"] == {"visible_content": "Réponse R1."}
    assert _link(engine, resume["assistant_turn_id"])["response_action"] == "continued_event"
    # La contribution suivante va dans le même event, session_ref = nouvelle conversation.
    _send(Sessions, ext, _reply("R2", "business"), "Une marque très forte.", conv=CONV_2)
    work = _event(engine, event["id"])["user_work_snapshot"]
    assert [(c["session_ref"], c["text"]) for c in work] == [(CONV, "Ils vendent du luxe."),
                                                            (CONV_2, "Une marque très forte.")]
    assert str(resume_trace["id"]) in work[-1]["support_refs_before"]


def test_pg_k_text_typed_in_a_new_conversation_is_appended_to_the_open_event(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    [event] = _events(engine)
    typed = _send(Sessions, ext, _reply("A2", "business"), "Je reprends : ils vendent du luxe.", context="",
                  conv=CONV_2)
    assert _link(engine, typed["assistant_turn_id"])["input_event_id"] == event["id"]
    [contribution] = _event(engine, event["id"])["user_work_snapshot"]
    assert contribution["session_ref"] == CONV_2


# --- L. nouvelle AnalysisSession ---------------------------------------------

def test_pg_l_new_analysis_session_never_resumes_an_old_event(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    _turn(Sessions, ext, _reply("A2", "business"), "Ils vendent du luxe.")
    [old] = _events(engine)
    # La tentative produit précédente n'est plus en cours (hors R1-C2).
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE analysis_sessions SET status = 'abandoned' WHERE id = :s"),
                     {"s": old["analysis_session_id"]})
    fresh = _send(Sessions, ext, _reply("B1", "business"), "Analyse LVMH", context="", conv=CONV_2)
    link = _link(engine, fresh["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"], link["abandoned_event_id"]) == (
        "event_closed_context_change", None, old["id"])
    assert [c["text"] for c in _event(engine, old["id"])["user_work_snapshot"]] == ["Ils vendent du luxe."]
    _ack(Sessions, fresh, conv=CONV_2)
    new = _events(engine)[-1]
    assert new["analysis_session_id"] == _session_id(engine) != old["analysis_session_id"]
    assert new["id"] != old["id"] and new["status"] == "open"


def test_pg_l_completed_attempt_then_new_attempt_starts_fresh(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("business", "business"))
    for previous, step in zip(STEPS, STEPS[1:]):
        _turn(Sessions, ext, _reply(step, step), f"réponse {previous}")
    before = _events(engine)
    fresh = _turn(Sessions, ext, _reply("B1", "business"), "", context="", conv=CONV_2)
    assert _link(engine, fresh["assistant_turn_id"])["input_action"] == "no_open_event"
    assert _events(engine)[:5] == before
    assert _events(engine)[-1]["analysis_session_id"] not in {e["analysis_session_id"] for e in before}


# --- M. ACK idempotent et concurrent -------------------------------------------

def test_pg_m_ack_retry_is_a_strict_no_op(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    second = _turn(Sessions, ext, _reply("A2", "moat"), "Ils vendent du luxe.")
    state = _t2_state(engine)
    with _Statements(engine) as statements:
        assert _ack(Sessions, second)["delivery_status"] == "delivered"
    assert _writes(statements) == []
    assert _t2_state(engine) == state


def test_pg_m_concurrent_acks_capture_once(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    second = _send(Sessions, ext, _reply("A2", "business"), "Ils vendent du luxe.")
    request = api.AssistantDeliveryAckRequest(user_id=USER, conversation_key=CONV)
    with Sessions() as holder, Sessions() as waiter:
        delivery = ad.lock_delivery_for_ack(holder, assistant_turn_id=_id(second), user_id=USER,
                                            conversation_key=CONV)
        dcr.capture_delivered_response(holder, delivery=delivery)
        ad.acknowledge_delivery(holder, assistant_turn_id=_id(second), user_id=USER, conversation_key=CONV)
        result, error = _run_blocked(engine, holder, waiter,
                                     lambda s: api.ack_assistant_delivery(_id(second), request, db=s))
    assert error is None and result["delivery_status"] == "delivered"
    assert len(_events(engine)) == 1 and len(_traces(engine)) == 1
    assert _link(engine, second["assistant_turn_id"])["response_action"] == "continued_event"


def test_pg_m_concurrent_acks_of_a_transition_finalize_and_open_once(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    second = _send(Sessions, ext, _reply("A2", "moat"), "Ils vendent du luxe.")
    request = api.AssistantDeliveryAckRequest(user_id=USER, conversation_key=CONV)
    with Sessions() as holder, Sessions() as waiter:
        delivery = ad.lock_delivery_for_ack(holder, assistant_turn_id=_id(second), user_id=USER,
                                            conversation_key=CONV)
        dcr.capture_delivered_response(holder, delivery=delivery)
        ad.acknowledge_delivery(holder, assistant_turn_id=_id(second), user_id=USER, conversation_key=CONV)
        result, error = _run_blocked(engine, holder, waiter,
                                     lambda s: api.ack_assistant_delivery(_id(second), request, db=s))
    assert error is None and result["delivery_status"] == "delivered"
    assert [e["status"] for e in _events(engine)] == ["finalized", "open"]


# --- N. retry /decryptage et worker perdant ------------------------------------

def test_pg_n_canonical_retry_never_touches_t2(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    turn = str(uuid.uuid4())
    second = _send(Sessions, ext, _reply("A2", "business"), "Ils vendent du luxe.", turn=turn)
    state = _t2_state(engine)
    with _Statements(engine) as statements:
        assert _send(Sessions, ext, _reply("autre", "moat"), "Ils vendent du luxe.", turn=turn) == second
    assert not [s for s in _writes(statements) if "cognitive" in s or "support_traces" in s]
    assert _t2_state(engine) == state
    _ack(Sessions, second)
    acked = _t2_state(engine)
    assert _send(Sessions, ext, _reply("autre", "moat"), "Ils vendent du luxe.", turn=turn)["delivery_status"] == (
        "delivered")
    assert _t2_state(engine) == acked
    assert _contribution_ids(engine) == [turn]
    assert _count(engine, "decryptage_cognitive_links") == 2
    assert (ext.fetch_calls, ext.claude_calls) == (2, 2)


def test_pg_n_two_workers_same_client_turn_capture_once(engine, Sessions, ext):
    """Deux workers passent le preflight du même tour ; le gagnant capture,
    le perdant renvoie la réponse canonique sans contribution, ni lien, ni
    progression."""
    _turn(Sessions, ext, _reply("A1", "business"))
    turn = str(uuid.uuid4())
    results = {}

    def concurrent_worker():
        results["winner"] = _send(Sessions, ext, _reply("gagnant", "moat"), "Ils vendent du luxe.", turn=turn)

    ext.on_fetch = concurrent_worker  # le perdant attend ses données pendant que le gagnant commite
    loser = _call(Sessions, ext, client_turn_id=turn, question="Ils vendent du luxe.", context="h",
                  last_method_id="construction_these")
    assert loser == results["winner"] and loser["analysis"] == "Réponse gagnant."
    assert _contribution_ids(engine) == [turn]
    assert _count(engine, "decryptage_cognitive_links") == 2
    assert _link(engine, loser["assistant_turn_id"])["capture_state"] == "awaiting_delivery"


def test_pg_n_contribution_collision_fails_closed(engine, Sessions, ext):
    """Le même client_turn_id réutilisé dans une autre conversation (bug
    client) : même contribution_id, session_ref différente -> collision R1-B,
    rollback complet du tour."""
    _turn(Sessions, ext, _reply("A1", "business"))
    turn = str(uuid.uuid4())
    _send(Sessions, ext, _reply("A2", "business"), "Ils vendent du luxe.", turn=turn)
    state, product = _t2_state(engine), _product(engine)
    with pytest.raises(HTTPException) as failure:
        _send(Sessions, ext, _reply("A3", "business"), "Ils vendent du luxe.", turn=turn, conv=CONV_2)
    assert failure.value.status_code == 500 and failure.value.detail["retryable"] is True
    assert (_t2_state(engine), _product(engine)) == (state, product)


# --- O. plusieurs events compatibles -------------------------------------------

def _second_open_event(Sessions, analysis_session_id):
    with Sessions() as session:
        event = cc.open_event_idempotent(
            session, user_id=USER, conversation_key=CONV, source_turn_refs=(uuid.uuid4(),), segmentation_ordinal=1,
            event_origin="decryptage", stimulus_snapshot={"visible_content": "x"},
            event_builder_version=dcr.EVENT_BUILDER_VERSION, admission_version=dcr.ADMISSION_VERSION,
            analysis_session_id=analysis_session_id)
        session.commit()
        return event.id


def test_pg_o_several_compatible_open_events_fail_closed_on_the_turn(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    [event] = _events(engine)
    _second_open_event(Sessions, event["analysis_session_id"])
    state, product = _t2_state(engine), _product(engine)
    with pytest.raises(HTTPException) as failure:
        _send(Sessions, ext, _reply("A2", "business"), "Ils vendent du luxe.")
    assert failure.value.status_code == 500
    assert (_t2_state(engine), _product(engine)) == (state, product)  # ni livraison, ni lien, ni progression
    with Sessions() as session:  # cause exacte : ambiguïté, jamais un choix heuristique
        with pytest.raises(dcr.AmbiguousOpenDecryptageEvents):
            dcr.capture_user_turn(session, delivery=_claim_runtime(session), ticker="MC.PA", user_text="x")


def test_pg_o_several_compatible_open_events_fail_closed_on_the_ack(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"))
    second = _send(Sessions, ext, _reply("A2", "business"), "Ils vendent du luxe.")
    _second_open_event(Sessions, _events(engine)[0]["analysis_session_id"])
    state = _t2_state(engine)
    with pytest.raises(HTTPException) as failure:
        _ack(Sessions, second)
    assert failure.value.status_code == 500
    assert _t2_state(engine) == state
    assert _delivery(engine, second["assistant_turn_id"])["status"] == "pending"
    with Sessions() as session:
        delivery = ad.lock_delivery_for_ack(session, assistant_turn_id=_id(second), user_id=USER,
                                            conversation_key=CONV)
        with pytest.raises(dcr.AmbiguousOpenDecryptageEvents):
            dcr.capture_delivered_response(session, delivery=delivery)


def test_pg_other_surfaces_and_legacy_events_are_never_touched(engine, Sessions, ext):
    """Events d'une autre politique / surface (même AnalysisSession) :
    jamais vus comme compatibles, jamais fermés."""
    _turn(Sessions, ext, _reply("A1", "business"))
    with Sessions() as session:
        other = cc.open_event_idempotent(
            session, user_id=USER, conversation_key=CONV, source_turn_refs=(uuid.uuid4(),), segmentation_ordinal=1,
            event_origin="education", stimulus_snapshot={}, event_builder_version=cc.EVENT_BUILDER_VERSION,
            admission_version=cc.ADMISSION_VERSION, analysis_session_id=_session_id(engine)).id
        session.commit()
    _turn(Sessions, ext, _reply("A2", "business"), "Ils vendent du luxe.")
    _send(Sessions, ext, _reply("N1"), "Analyse NVDA", ticker="NVDA")
    assert _event(engine, other)["status"] == "open" and _event(engine, other)["user_work_snapshot"] == []


# --- P. échec T2 pendant l'ACK -------------------------------------------------

@pytest.mark.parametrize("primitive, reply", [
    ("add_support_trace", _reply("A2", "business")),
    ("open_event_idempotent", _reply("A2", "moat")),
])
def test_pg_p_t2_failure_on_ack_rolls_everything_back(engine, Sessions, ext, monkeypatch, primitive, reply):
    _turn(Sessions, ext, _reply("A1", "business"))
    second = _send(Sessions, ext, reply, "Ils vendent du luxe.")
    state = _t2_state(engine)
    real = getattr(cc, primitive)

    def failing(db, **kwargs):
        real(db, **kwargs)  # mutation flushée, puis échec
        raise sa.exc.OperationalError("INSERT", {}, Exception("connexion perdue"))

    monkeypatch.setattr(cc, primitive, failing)
    with pytest.raises(HTTPException) as failure:
        _ack(Sessions, second)
    assert failure.value.status_code == 500 and failure.value.detail["retryable"] is True
    assert _t2_state(engine) == state  # livraison pending, lien awaiting, event inchangé, aucune trace
    assert _delivery(engine, second["assistant_turn_id"])["status"] == "pending"
    assert _link(engine, second["assistant_turn_id"])["capture_state"] == "awaiting_delivery"

    monkeypatch.setattr(cc, primitive, real)
    assert _ack(Sessions, second)["delivery_status"] == "delivered"
    assert _link(engine, second["assistant_turn_id"])["capture_state"] == "captured"


def test_pg_p_t2_failure_on_decryptage_rolls_back_the_delivery(engine, Sessions, ext, monkeypatch):
    _turn(Sessions, ext, _reply("A1", "business"))
    state, product = _t2_state(engine), _product(engine)

    def failing(db, **kwargs):
        raise sa.exc.OperationalError("UPDATE cognitive_events", {}, Exception("connexion perdue"))

    monkeypatch.setattr(cc, "append_user_contribution", failing)
    with pytest.raises(HTTPException):
        _send(Sessions, ext, _reply("A2", "moat"), "Ils vendent du luxe.")
    assert (_t2_state(engine), _product(engine)) == (state, product)


# --- Q / R. livraisons legacy et invariants -----------------------------------

def _claim(Sessions, private_metadata):
    with Sessions() as session:
        delivery, created = ad.claim_delivery(
            session, user_id=USER, conversation_key=CONV, surface="decryptage", source_user_turn_id=uuid.uuid4(),
            delivery_ordinal=1, request_fingerprint=FP_A, visible_content_fingerprint=FP_A,
            response_payload=PAYLOAD_A, private_metadata=private_metadata,
            delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION)
        assert created
        session.commit()
        return {"assistant_turn_id": str(delivery.id)}


def test_pg_q_legacy_r1c1_delivery_ack_still_works_without_capture(engine, Sessions, ext):
    legacy = _claim(Sessions, {"decryptage_step_marker": "business"})
    assert _ack(Sessions, legacy)["delivery_status"] == "delivered"
    assert _links(engine) == [] and _events(engine) == []
    assert _ack(Sessions, legacy)["delivery_status"] == "delivered"


def test_pg_q_legacy_delivery_with_a_link_fails_closed(engine, Sessions, ext):
    legacy = _claim(Sessions, {"decryptage_step_marker": "business"})
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO decryptage_cognitive_links (assistant_delivery_id, capture_version, "
                             "input_action, capture_state, created_at) VALUES (:d, :v, 'no_open_event', "
                             "'awaiting_delivery', now())"), {"d": _id(legacy), "v": VERSION})
    with pytest.raises(HTTPException) as failure:
        _ack(Sessions, legacy)
    assert failure.value.status_code == 500
    assert _delivery(engine, legacy["assistant_turn_id"])["status"] == "pending"


def test_pg_r_r1c2_delivery_without_link_fails_closed(engine, Sessions, ext):
    orphan = _claim(Sessions, dcr.runtime_private_metadata("business"))
    with pytest.raises(HTTPException) as failure:
        _ack(Sessions, orphan)
    assert failure.value.status_code == 500 and failure.value.detail["retryable"] is True
    assert _delivery(engine, orphan["assistant_turn_id"])["status"] == "pending"
    assert _events(engine) == [] and _links(engine) == []


@pytest.mark.parametrize("corruption", [
    "UPDATE decryptage_cognitive_links SET capture_state = 'captured', response_action = 'no_cognitive_action', "
    "captured_at = now() WHERE assistant_delivery_id = :d",
    "UPDATE assistant_deliveries SET status = 'delivered', delivered_at = now() WHERE id = :d",
    "UPDATE decryptage_cognitive_links SET capture_version = 'decryptage-cognitive-runtime-v2' "
    "WHERE assistant_delivery_id = :d",
    "UPDATE assistant_deliveries SET private_metadata = jsonb_set(private_metadata, '{cognitive_runtime_version}', "
    "'\"decryptage-cognitive-runtime-v2\"') WHERE id = :d",
], ids=["pending+captured", "delivered+awaiting", "capture_version", "runtime_version"])
def test_pg_r_forbidden_state_pairs_and_versions_fail_closed(engine, Sessions, ext, corruption):
    first = _send(Sessions, ext, _reply("A1", "business"))
    with engine.begin() as conn:
        conn.execute(sa.text(corruption), {"d": _id(first)})
    state = _t2_state(engine)
    with pytest.raises(HTTPException) as failure:
        _ack(Sessions, first)
    assert failure.value.status_code == 500
    assert _t2_state(engine) == state


def test_pg_capture_user_turn_refuses_a_second_link_or_a_legacy_delivery(engine, Sessions, ext):
    first = _send(Sessions, ext, _reply("A1", "business"))
    legacy = _claim(Sessions, {"decryptage_step_marker": None})
    with Sessions() as session:
        for delivery_id in (first["assistant_turn_id"], legacy["assistant_turn_id"]):
            delivery = session.get(ad.AssistantDelivery, uuid.UUID(delivery_id))
            with pytest.raises(dcr.CognitiveLinkInvariantError):
                dcr.capture_user_turn(session, delivery=delivery, ticker="MC.PA", user_text="x")
            session.rollback()


# --- concurrence : lecture / mutation et ordre des verrous ----------------------

def _claim_runtime(session, turn=None):
    delivery, created = ad.claim_delivery(
        session, user_id=USER, conversation_key=CONV, surface="decryptage", source_user_turn_id=turn or uuid.uuid4(),
        delivery_ordinal=1, request_fingerprint=FP_A, visible_content_fingerprint=FP_A,
        response_payload=PAYLOAD_A, private_metadata=dcr.runtime_private_metadata(None),
        delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION)
    assert created
    return delivery


def test_pg_event_closed_between_read_and_mutation_is_never_appended(engine, Sessions, ext):
    """Une transaction ferme l'event pendant que le tour suivant attend son
    verrou : le tour voit l'état commité (event fermé) et ne l'enrichit
    pas (SELECT ... FOR UPDATE réévalué en READ COMMITTED)."""
    _turn(Sessions, ext, _reply("A1", "business"))
    [event] = _events(engine)
    with Sessions() as holder, Sessions() as waiter:
        cc.finalize_event(holder, event_id=event["id"])

        def turn(session):
            delivery = _claim_runtime(session)
            link = dcr.capture_user_turn(session, delivery=delivery, ticker="MC.PA", user_text="Ils vendent du luxe.")
            return link.input_action, link.input_event_id, link.abandoned_event_id

        result, error = _run_blocked(engine, holder, waiter, turn)
    assert error is None and result == ("no_open_event", None, None)
    after = _event(engine, event["id"])
    assert after["status"] == "finalized" and after["user_work_snapshot"] == []


def test_pg_ack_and_next_turn_serialize_on_the_analysis_session(engine, Sessions, ext):
    """ACK (livraison -> lien -> AnalysisSession -> event) et tour suivant
    (AnalysisSession -> events) suivent le même ordre : pas d'interblocage,
    et le tour voit l'event ouvert par l'ACK commité."""
    first = _send(Sessions, ext, _reply("A1", "business"))
    with Sessions() as holder, Sessions() as waiter:
        delivery = ad.lock_delivery_for_ack(holder, assistant_turn_id=_id(first), user_id=USER, conversation_key=CONV)
        dcr.capture_delivered_response(holder, delivery=delivery)
        ad.acknowledge_delivery(holder, assistant_turn_id=_id(first), user_id=USER, conversation_key=CONV)

        def turn(session):
            link = dcr.capture_user_turn(session, delivery=_claim_runtime(session), ticker="MC.PA",
                                         user_text="Ils vendent du luxe.")
            return link.input_action

        result, error = _run_blocked(engine, holder, waiter, turn)
    assert error is None and result == "contribution_appended"
    [event] = _events(engine)
    assert [c["text"] for c in event["user_work_snapshot"]] == ["Ils vendent du luxe."]


def test_pg_two_pending_openings_on_one_session_never_create_two_open_events(engine, Sessions, ext):
    """Deux onglets : deux premières réponses business en attente d'ACK sur
    la même AnalysisSession. Les ACK se sérialisent sur l'AnalysisSession ;
    le second voit l'event ouvert par le premier (unique event compatible)
    et le continue au lieu d'en ouvrir un second."""
    first = _send(Sessions, ext, _reply("onglet 1", "business"), "", context="")
    second = _send(Sessions, ext, _reply("onglet 2", "business"), "", context="", conv=CONV_2)
    assert _link(engine, second["assistant_turn_id"])["input_action"] == "no_open_event"
    request = api.AssistantDeliveryAckRequest(user_id=USER, conversation_key=CONV_2)
    with Sessions() as holder, Sessions() as waiter:
        delivery = ad.lock_delivery_for_ack(holder, assistant_turn_id=_id(first), user_id=USER, conversation_key=CONV)
        dcr.capture_delivered_response(holder, delivery=delivery)
        ad.acknowledge_delivery(holder, assistant_turn_id=_id(first), user_id=USER, conversation_key=CONV)
        result, error = _run_blocked(engine, holder, waiter,
                                     lambda s: api.ack_assistant_delivery(_id(second), request, db=s))
    assert error is None and result["delivery_status"] == "delivered"
    [event] = _events(engine)
    assert event["status"] == "open"
    assert _link(engine, second["assistant_turn_id"])["response_action"] == "continued_event"
    [trace] = _traces(engine, event["id"])
    assert trace["support_payload"] == {"visible_content": "Réponse onglet 2."}


def test_pg_rolled_back_ack_leaves_the_next_ack_free(engine, Sessions, ext):
    first = _send(Sessions, ext, _reply("A1", "business"))
    request = api.AssistantDeliveryAckRequest(user_id=USER, conversation_key=CONV)
    with Sessions() as holder, Sessions() as waiter:
        delivery = ad.lock_delivery_for_ack(holder, assistant_turn_id=_id(first), user_id=USER, conversation_key=CONV)
        dcr.capture_delivered_response(holder, delivery=delivery)
        result, error = _run_blocked(engine, holder, waiter,
                                     lambda s: api.ack_assistant_delivery(_id(first), request, db=s),
                                     release="rollback")
    assert error is None and result["delivery_status"] == "delivered"
    [event] = _events(engine)
    assert _link(engine, first["assistant_turn_id"])["response_event_id"] == event["id"]


# --- doctrine : journaux et frontières -------------------------------------------

def test_pg_logs_carry_ids_and_actions_but_no_text(engine, Sessions, ext, caplog):
    with caplog.at_level(logging.INFO, logger="core.decryptage_cognitive_runtime"):
        _turn(Sessions, ext, _reply("texte assistant secret", "business"))
        second = _turn(Sessions, ext, _reply("autre texte assistant", "moat"), "phrase utilisateur privée")
    records = [r for r in caplog.records if r.name == "core.decryptage_cognitive_runtime"]
    assert records and all(r.getMessage().startswith("[R1-C2] ") for r in records)
    text = caplog.text
    for secret in ("texte assistant secret", "autre texte assistant", "phrase utilisateur privée", "ORYX_STEP"):
        assert secret not in text, secret
    assert f"assistant_turn={second['assistant_turn_id']} action=transitioned_event marker=moat from=business" in text


def test_pg_full_attempt_writes_t2_but_never_t3(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("business", "business"))
    for previous, step in zip(STEPS, STEPS[1:]):
        _turn(Sessions, ext, _reply(step, step), f"réponse {previous}")
    assert {r["status"] for r in _rows(engine)} == {"delivered"}
    assert {link["capture_state"] for link in _links(engine)} == {"captured"}
    assert all(e["task_kind"] is None for e in _events(engine))
    _no_t3(engine)
