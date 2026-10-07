"""Tests de R1-D1A : contrat d'entrée de l'évaluation
(core/evaluation_input.py).

1. Sans base : module en lecture seule (aucune écriture, aucun LLM, aucune
   inférence), versions supportées recopiées et identiques au runtime
   Décrypter courant, canonicalisation.

2. Contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL vers une base DÉDIÉE
   dont le nom contient "test" ; sinon SKIPPÉS). Les events sont produits par
   le VRAI parcours Décrypter (api.decryptage + ACK, faux appels externes de
   tests/test_assistant_delivery.py) : provenance exacte, projection
   évaluateur sans identifiant interne, support_refs_before autoritaire,
   refus (fail closed) des events non évaluables ou corrompus, fingerprint
   stable, AUCUNE écriture.

Les fixtures et parcours de ce module sont réutilisés par
tests/test_r1d1_runtime.py.
"""
import ast
import json
import re
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from core import decryptage_cognitive_runtime as dcr
from core import evaluation_input as ei
from core import taxonomy_service as tax
from core.evaluation_input import (
    EventNotEvaluable,
    InvalidEvaluationInput,
    UnsupportedEvaluationInputVersion,
    build_evaluation_input,
)
from core.models import PedagogicalTaxonomyRelease
from core.pedagogy.taxonomy_bootstrap import activate_taxonomy_v1, bootstrap_taxonomy_v1
from tests.test_assistant_delivery import Sessions, engine, ext  # noqa: F401 — fixtures
from tests.test_cognitive_capture import _Statements, _writes
from tests.test_decryptage_cognitive_runtime import _events, _reply, _traces, _turn
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "evaluation_input.py"
CONV_A, CONV_B = "conv-a", "conv-b"
ANSWER_1 = "Ils vendent du luxe."
ANSWER_2 = "Les créances progressent : ça consomme du cash."
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
T3_TABLES = ("observation_capabilities", "pedagogical_observations", "observation_evaluation_runs")
T4_TABLES = ("capability_taxonomy_memberships", "core_capability_definitions", "pedagogical_taxonomy_releases")
T5_T6_TABLES = ("longitudinal_assessment_runs", "longitudinal_assessment_inputs", "observation_dependencies",
                "observation_transfers", "observation_revalidations", "competency_inference_runs",
                "competency_stage_claims", "competency_inference_tensions", "competency_inference_basis_refs",
                "user_competency_states")


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    return imported | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}


def test_module_is_read_only_and_never_interprets():
    tokens = set(_code_tokens(MODULE_PATH.read_text(encoding="utf-8")).split("\n"))
    for forbidden in ("commit", "rollback", "flush", "add", "add_all", "delete", "update", "merge", "insert",
                      "begin", "begin_nested", "with_for_update", "SessionLocal", "anthropic", "observation_service",
                      "taxonomy_service", "start_evaluation_run", "PedagogicalObservation", "ObservationEvaluationRun",
                      "competency_code", "polarity", "evidence_strength", "local_stage", "support_level",
                      "capability_localization", "UserStatement", "current_step", "decryptage_step_marker"):
        assert forbidden not in tokens, forbidden
    assert _imports(MODULE_PATH) == {"hashlib", "json", "uuid", "dataclasses", "datetime", "types", "sqlalchemy",
                                     "core.models"}


def test_supported_versions_are_frozen_copies_of_the_current_decryptage_runtime():
    """Recopiées (et non importées) : un futur builder / runtime ne devient
    jamais évaluable implicitement ; aujourd'hui elles coïncident."""
    assert ei.SUPPORTED_EVENT_ORIGIN == dcr.EVENT_ORIGIN
    assert ei.SUPPORTED_EVENT_BUILDER_VERSION == dcr.EVENT_BUILDER_VERSION
    assert ei.SUPPORTED_ADMISSION_VERSION == dcr.ADMISSION_VERSION
    assert ei.SUPPORTED_CAPTURE_VERSION == dcr.COGNITIVE_RUNTIME_VERSION_V2 == dcr.COGNITIVE_RUNTIME_VERSION
    assert ei.LEGACY_CAPTURE_VERSIONS == {dcr.COGNITIVE_RUNTIME_VERSION_V1}
    assert ei.SUPPORTED_SURFACE == dcr.SURFACE and ei.SUPPORTED_SUPPORT_KIND == dcr.SUPPORT_KIND
    assert ei.SUPPORTED_EVENT_SCHEMA_VERSION == "cognitive-event-v2"


def test_canonical_json_is_strict_sorted_compact_and_unicode():
    assert ei.canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode("utf-8")
    assert ei.sha256_hex({"b": 1, "a": 2}) == ei.sha256_hex({"a": 2, "b": 1})
    with pytest.raises(ValueError):
        ei.canonical_json({"x": float("nan")})


def test_event_id_type_is_checked_before_any_database_access():
    with pytest.raises(InvalidEvaluationInput):
        build_evaluation_input(object(), event_id=str(uuid.uuid4()))


# --------------------------------------------------------------------------
# 2. PostgreSQL — fixtures et parcours partagés
# --------------------------------------------------------------------------

def ensure_active_v1(Sessions):
    """oryx-v1 bootstrapée et ACTIVE, seule release (réinitialise T4 si un
    test a créé / activé une autre release)."""
    with Sessions() as session:
        releases = session.execute(select(PedagogicalTaxonomyRelease)).scalars().all()
        if [(r.version_key, r.status) for r in releases] == [("oryx-v1", "active")]:
            return
    with Sessions() as session:
        for table in T3_TABLES + T4_TABLES:
            session.execute(sa.text(f"DELETE FROM {table}"))
        bootstrap_taxonomy_v1(session)
        activate_taxonomy_v1(session)
        session.commit()


@pytest.fixture
def r1d1(engine, Sessions, ext):  # noqa: F811
    """Taxonomie oryx-v1 active ; T3 vidé APRÈS le test (avant le nettoyage
    T2 de Sessions, dont il dépend)."""
    ensure_active_v1(Sessions)
    yield
    with engine.begin() as conn:
        for table in T3_TABLES:
            conn.execute(sa.text(f"DELETE FROM {table}"))


def finalized_event(Sessions, ext, engine, *, conv=CONV_A, ticker="MC.PA", other_conversation=False):  # noqa: F811
    """VRAI parcours Décrypter : business ouvert (stimulus), contribution 1,
    continuation (aide S1 rendue dans conv), [reprise dans une AUTRE
    conversation : aide S2 rendue seulement là], contribution 2 (moat) qui
    finalise l'event business. Retourne l'id de l'event finalized."""
    _turn(Sessions, ext, _reply("business", "business"), "", context="", conv=conv, ticker=ticker)
    _turn(Sessions, ext, _reply("business-2", "business"), ANSWER_1, conv=conv, ticker=ticker)
    if other_conversation:
        _turn(Sessions, ext, _reply("business-B", "business"), "", context="", conv=CONV_B, ticker=ticker)
    _turn(Sessions, ext, _reply("moat", "moat"), ANSWER_2, conv=conv, ticker=ticker)
    finalized = [e for e in _events(engine) if e["status"] == "finalized"]
    return finalized[-1]["id"]


def bundle(Sessions, event_id):  # noqa: F811
    with Sessions() as session:
        return build_evaluation_input(session, event_id=event_id)


def execute(engine, statement, **params):  # noqa: F811
    with engine.begin() as conn:
        conn.execute(sa.text(statement), params)


def counts(engine, tables):  # noqa: F811
    with engine.connect() as conn:
        return {t: conn.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables}


def _keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


# --------------------------------------------------------------------------
# 2. PostgreSQL — provenance et projection
# --------------------------------------------------------------------------

def test_pg_finalized_event_gives_exact_provenance_and_a_tokenized_payload(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    result = bundle(Sessions, event_id)
    [trace] = _traces(engine, event_id)
    snapshot = result.source_snapshot
    assert snapshot["schema_version"] == "evaluation-source-v1"
    assert snapshot["event"]["event_id"] == str(event_id) and snapshot["event"]["status"] == "finalized"
    assert snapshot["event"]["task_kind"] is None
    assert [c["text"] for c in snapshot["contributions"]] == [ANSWER_1, ANSWER_2]
    assert [c["capture_version"] for c in snapshot["contributions"]] == [dcr.COGNITIVE_RUNTIME_VERSION_V2] * 2
    assert [c["support_refs_before"] for c in snapshot["contributions"]] == [[], [str(trace["id"])]]
    assert [s["support_trace_id"] for s in snapshot["supports"]] == [str(trace["id"])]
    assert snapshot["stimulus_snapshot"]["visible_content"] == "Réponse business."

    payload = result.evaluator_payload
    assert payload == {
        "event_token": "event_1",
        "stimulus": snapshot["stimulus_snapshot"],
        "contributions": [
            {"contribution_token": "contribution_1", "phase": 1, "text": ANSWER_1, "support_before": []},
            {"contribution_token": "contribution_2", "phase": 2, "text": ANSWER_2, "support_before": ["support_1"]},
        ],
        "support_catalog": [{"support_token": "support_1", "sequence_no": 1, "support_kind": "assistant_response",
                             "visible_content": "Réponse business-2."}],
    }
    assert dict(result.contribution_ids) == {
        "contribution_1": snapshot["contributions"][0]["contribution_id"],
        "contribution_2": snapshot["contributions"][1]["contribution_id"]}
    assert dict(result.support_ids) == {"support_1": str(trace["id"])}
    assert dict(result.contribution_support_before) == {"contribution_1": (), "contribution_2": ("support_1",)}


def test_pg_payload_never_exposes_internal_identifiers_or_product_context(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    result = bundle(Sessions, event_id)
    text = json.dumps(result.evaluator_payload, ensure_ascii=False)
    assert not UUID_PATTERN.search(text)
    assert _keys(result.evaluator_payload) == {
        "event_token", "stimulus", "visible_content", "price", "currency", "contributions", "contribution_token",
        "phase", "text", "support_before", "support_catalog", "support_token", "sequence_no", "support_kind"}
    for forbidden in ("conv-a", "user_id", "conversation", "analysis_session", "occurred_at", "created_at",
                      "dedup", "ORYX_STEP", "step_marker", "current_step", "task_kind"):
        assert forbidden not in text, forbidden


def test_pg_two_builds_give_the_same_fingerprint_whatever_the_session_timezone(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    first = bundle(Sessions, event_id)
    with Sessions() as session:
        session.execute(sa.text("SET TIME ZONE 'Asia/Tokyo'"))
        second = build_evaluation_input(session, event_id=event_id)
    assert second.source_snapshot == first.source_snapshot
    assert second.evaluator_payload == first.evaluator_payload
    assert second.source_fingerprint == first.source_fingerprint == ei.sha256_hex(first.source_snapshot)
    assert len(first.source_fingerprint) == 64


def test_pg_build_is_strictly_read_only(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    flushes = []
    with Sessions() as session:
        sa.event.listen(session, "before_flush", lambda *a: flushes.append(a))
        with _Statements(engine) as statements:
            build_evaluation_input(session, event_id=event_id)
        assert not session.new and not session.dirty and not session.deleted
    assert flushes == [] and _writes(statements) == []
    assert all(" FOR " not in s.upper() for s in statements)


def test_pg_support_refs_before_is_authoritative_not_timestamps(engine, Sessions, ext, r1d1):
    """S2 a été rendue dans une AUTRE conversation AVANT la contribution 2
    (created_at antérieur) : elle n'était pas disponible pour cette
    contribution et n'est jamais référencée, donc absente du catalogue."""
    event_id = finalized_event(Sessions, ext, engine, other_conversation=True)
    s1, s2 = _traces(engine, event_id)
    result = bundle(Sessions, event_id)
    contribution_2 = result.source_snapshot["contributions"][1]
    assert contribution_2["support_refs_before"] == [str(s1["id"])]
    assert [s["support_trace_id"] for s in result.source_snapshot["supports"]] == [str(s1["id"])]
    assert dict(result.support_ids) == {"support_1": str(s1["id"])}
    assert str(s2["id"]) not in json.dumps(result.source_snapshot)


# --------------------------------------------------------------------------
# 2. PostgreSQL — refus (fail closed, aucune réparation)
# --------------------------------------------------------------------------

def test_pg_open_unknown_and_abandoned_events_are_not_evaluable(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    [open_event] = [e for e in _events(engine) if e["status"] == "open"]
    with pytest.raises(EventNotEvaluable, match="open"):
        bundle(Sessions, open_event["id"])
    with pytest.raises(EventNotEvaluable, match="introuvable"):
        bundle(Sessions, uuid.uuid4())
    execute(engine, "UPDATE cognitive_events SET status = 'abandoned' WHERE id = :id", id=event_id)
    with pytest.raises(EventNotEvaluable, match="abandoned"):
        bundle(Sessions, event_id)


def test_pg_empty_finalized_event_is_not_evaluable(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    execute(engine, "UPDATE cognitive_events SET user_work_snapshot = '[]'::jsonb WHERE id = :id", id=event_id)
    with pytest.raises(EventNotEvaluable, match="sans contribution"):
        bundle(Sessions, event_id)


@pytest.mark.parametrize("stimulus", ['{}', '{"visible_content": ""}', '{"visible_content": "x", "prompt": "p"}',
                                      '{"visible_content": "x", "price": "600", "currency": "EUR"}',
                                      '{"visible_content": 1}'])
def test_pg_invalid_stimulus_is_refused(engine, Sessions, ext, r1d1, stimulus):
    event_id = finalized_event(Sessions, ext, engine)
    execute(engine, "UPDATE cognitive_events SET stimulus_snapshot = CAST(:s AS jsonb) WHERE id = :id",
            s=stimulus, id=event_id)
    with pytest.raises(InvalidEvaluationInput, match="stimulus_snapshot"):
        bundle(Sessions, event_id)


def _rewrite_contribution(engine, event_id, index, **changes):
    with engine.connect() as conn:
        work = conn.execute(sa.text("SELECT user_work_snapshot FROM cognitive_events WHERE id = :id"),
                            {"id": event_id}).scalar_one()
    work[index].update(changes)
    execute(engine, "UPDATE cognitive_events SET user_work_snapshot = CAST(:w AS jsonb) WHERE id = :id",
            w=json.dumps(work), id=event_id)
    return work


def test_pg_invalid_contribution_refs_are_refused(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    [trace] = _traces(engine, event_id)
    for refs, match in (([str(uuid.uuid4())], "hors de cet event"), ([str(trace["id"])] * 2, "doublon"),
                        (["not-a-uuid"], "UUID canonique"), ([str(trace["id"]).upper()], "UUID canonique"),
                        (str(trace["id"]), "liste")):
        _rewrite_contribution(engine, event_id, 1, support_refs_before=refs)
        with pytest.raises(InvalidEvaluationInput, match=match):
            bundle(Sessions, event_id)


def test_pg_support_of_another_event_is_refused(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    other_id = finalized_event(Sessions, ext, engine, conv="conv-c", ticker="NVDA")
    [foreign] = _traces(engine, other_id)
    _rewrite_contribution(engine, event_id, 1, support_refs_before=[str(foreign["id"])])
    with pytest.raises(InvalidEvaluationInput, match="hors de cet event"):
        bundle(Sessions, event_id)


@pytest.mark.parametrize("changes, match", [
    ({"phase": 3}, "phase"), ({"text": ""}, "text"), ({"contribution_id": "x"}, "contribution_id"),
    ({"session_ref": ""}, "session_ref"), ({"extra": 1}, "clés"),
])
def test_pg_invalid_contribution_is_refused(engine, Sessions, ext, r1d1, changes, match):
    event_id = finalized_event(Sessions, ext, engine)
    _rewrite_contribution(engine, event_id, 0, **changes)
    with pytest.raises(InvalidEvaluationInput, match=match):
        bundle(Sessions, event_id)


def test_pg_contribution_without_its_capture_link_is_refused(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    _rewrite_contribution(engine, event_id, 0, contribution_id=str(uuid.uuid4()))
    with pytest.raises(InvalidEvaluationInput, match="lien"):
        bundle(Sessions, event_id)


def test_pg_contribution_captured_by_the_v1_runtime_is_unsupported(engine, Sessions, ext, r1d1):
    """Une contribution V1 (aides non scoppées par conversation) n'a pas les
    garanties causales requises : jamais évaluée."""
    event_id = finalized_event(Sessions, ext, engine)
    execute(engine, "UPDATE decryptage_cognitive_links SET capture_version = 'decryptage-cognitive-runtime-v1', "
            "input_context_event_id = NULL, response_context_event_id = NULL, product_progress_action = NULL "
            "WHERE input_event_id = :id AND response_action = 'continued_event'", id=event_id)
    with pytest.raises(UnsupportedEvaluationInputVersion, match="runtime-v1"):
        bundle(Sessions, event_id)


@pytest.mark.parametrize("column, value", [("event_origin", "education"), ("event_builder_version", "builder-v2"),
                                           ("admission_version", "admission-v2"), ("event_schema_version", None)])
def test_pg_unsupported_event_versions_fail_closed(engine, Sessions, ext, r1d1, column, value):
    event_id = finalized_event(Sessions, ext, engine)
    execute(engine, f"UPDATE cognitive_events SET {column} = :v WHERE id = :id", v=value, id=event_id)
    with pytest.raises(UnsupportedEvaluationInputVersion):
        bundle(Sessions, event_id)


def test_pg_t2_task_kind_must_stay_null(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    execute(engine, "UPDATE cognitive_events SET task_kind = 'analysis' WHERE id = :id", id=event_id)
    with pytest.raises(InvalidEvaluationInput, match="task_kind"):
        bundle(Sessions, event_id)


def test_pg_invalid_support_payload_is_refused(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    execute(engine, "UPDATE support_traces SET support_payload = '{\"visible_content\": \"x\", \"secret\": 1}'"
            "::jsonb WHERE cognitive_event_id = :id", id=event_id)
    with pytest.raises(InvalidEvaluationInput, match="support_payload"):
        bundle(Sessions, event_id)


def test_pg_no_t3_t4_or_t5_t6_write(engine, Sessions, ext, r1d1):
    event_id = finalized_event(Sessions, ext, engine)
    before = counts(engine, T3_TABLES + T4_TABLES + T5_T6_TABLES)
    bundle(Sessions, event_id)
    assert counts(engine, T3_TABLES + T4_TABLES + T5_T6_TABLES) == before
    with Sessions() as session:
        assert tax.get_active_release(session).version_key == "oryx-v1"
