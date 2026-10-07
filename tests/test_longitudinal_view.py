"""Tests de T5-C : reconstruction READ-ONLY du dossier longitudinal
(core/longitudinal_view.py).

Aucune migration dans ce chantier : le schéma testé est celui produit par
`alembic upgrade head` (= 0008_longitudinal_relations, puis
0009_competency_inference_state depuis T6-A, tables vides non lues par ce
module). Les données sont
créées via les services T2-B / T3-B / T4-B / T5-B ; seules les corruptions
sont écrites en SQL direct, dans la transaction du test (annulée).

1. Tests sans base (toujours exécutés) : aucune migration, API publique
   exacte, hiérarchie d'exceptions, aucune écriture / transaction / verrou,
   aucun LLM, aucun import du service T5-B, aucun stade / confiance / score /
   « independent » / fraîcheur, dataclasses immuables, parité des formats
   canoniques et des règles avec T5-B, validation des arguments AVANT tout
   accès à la base, to_payload.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T1-T5-B) :
   uniquement si ORYX_TEST_DATABASE_URL pointe vers une base DÉDIÉE dont le
   nom contient "test". Sinon SKIPPÉS.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t5c_test \\
           python -m pytest tests/test_longitudinal_view.py
"""
import ast
import dataclasses
import inspect
import json
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace

import pytest
import sqlalchemy as sa

from core import cognitive_capture as cc
from core import longitudinal_service as svc
from core import longitudinal_view as view
from core.db import Base
from core.models import User
from core.longitudinal_view import (
    InvalidLongitudinalDossier,
    InvalidLongitudinalViewArgument,
    LongitudinalDossier,
    LongitudinalDossierNotFound,
    LongitudinalDossierNotStabilized,
    LongitudinalViewError,
)
from tests.test_longitudinal_service import (  # noqa: F401 — fixtures
    Sessions,
    _event_with_trace,
    _invalidate,
    _raw_edge,
    _rows,
    _scope_fp,
    _sql,
    _t3,
    _t5_kwargs,
    _taxonomy,
    app,
    contra,
    db,
    engine,
    only,
    sup,
)
from tests.test_cognitive_capture import capture_event
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, _script_directory
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_migration_0005_cognitive_support_traces import OTHER_USER, USER
from tests.test_migration_0008_longitudinal_relations import T5A
from tests.test_migration_0009_competency_inference_state import T6A
from tests.test_migration_0010_r1b_event_idempotence import R1B
from tests.test_migration_0011_assistant_deliveries import R1C1
from tests.test_migration_0012_decryptage_cognitive_links import R1C2
from tests.test_migration_0004_drop_company_analyses import R1C4, R1C4_FILE, R1D1, R1D1_FILE
from tests.test_observation_service import _NoDB, _reaches_db

VIEW_PATH = REPO_ROOT / "core" / "longitudinal_view.py"
PUBLIC_API = {"build_longitudinal_dossier", "build_active_longitudinal_dossier"}
EXCEPTIONS = {InvalidLongitudinalViewArgument, LongitudinalDossierNotFound, LongitudinalDossierNotStabilized,
              InvalidLongitudinalDossier}
# Champs / valeurs appartenant à T6 (ou interdits) : jamais dans la sortie.
FORBIDDEN_OUTPUT = ("current_stage", "previous_stage", "new_stage", "user_stage", "confidence", "mastery",
                    "revalidation_required", "confirmation_required", "validation_need", "transition",
                    "progress", "score", "percent", "ratio", "sufficient", "fresh", "stale", "decay",
                    "days_since", "age_days", "weight", "coefficient", "penalty", "bonus", "family_id",
                    "confirmed_cognitive_motif", "user_believes", "user_is_confused", "user_has_bias",
                    "user_does_not_understand")
INVALID_UUIDS = [None, "", str(uuid.UUID(int=7)), 1, uuid.UUID(int=7).bytes]


def _has_word(text: str, word: str) -> bool:
    """`word` comme mot (séparateurs : tout sauf lettre / chiffre ; « _ »
    compris) : « ratio » n'est pas trouvé dans « demonstration »."""
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text.lower()) is not None


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_no_migration_added_by_t5c():
    """Aucune migration ajoutée par T5-C ; les seules ajoutées depuis sont
    0009 (T6-A), 0010 (R1-B), 0011 (R1-C1), 0012 (R1-C2) et 0013 (R1-C4),
    qui est la tête."""
    script = _script_directory()
    assert script.get_heads() == [R1D1]
    assert script.get_revision(R1D1).down_revision == R1C4
    assert script.get_revision(R1C4).down_revision == R1C2
    assert script.get_revision(R1C2).down_revision == R1C1
    assert script.get_revision(R1C1).down_revision == R1B
    assert script.get_revision(R1B).down_revision == T6A
    assert script.get_revision(T6A).down_revision == T5A
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[-7:] == [f"{T5A}.py", f"{T6A}.py", f"{R1B}.py", f"{R1C1}.py", f"{R1C2}.py", R1C4_FILE,
                          R1D1_FILE]
    assert len(files) == 14


def test_no_profile_table_or_model_exists():
    for table in Base.metadata.tables:
        for fragment in ("profile", "active_history", "dossier", "coverage", "variety", "independence",
                         "consistency", "freshness", "durability"):
            assert fragment not in table, table


def test_public_api_is_exactly_two_keyword_only_builders():
    tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == PUBLIC_API
    for name in PUBLIC_API:
        params = list(inspect.signature(getattr(view, name)).parameters.values())
        assert params[0].name == "db" and params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:]), name
    empty = inspect.Parameter.empty
    assert {n: p.default for n, p in inspect.signature(view.build_longitudinal_dossier).parameters.items()
            if n != "db"} == {"run_id": empty}
    assert {n: p.default for n, p in inspect.signature(view.build_active_longitudinal_dossier).parameters.items()
            if n != "db"} == {"user_id": empty, "competency_code": empty}


def test_exceptions_are_a_small_business_hierarchy():
    public = {o for o in vars(view).values()
              if isinstance(o, type) and issubclass(o, Exception) and o.__module__ == view.__name__}
    assert public == EXCEPTIONS | {LongitudinalViewError}
    for exc in EXCEPTIONS:
        assert exc.__bases__ == (LongitudinalViewError,), exc
    assert LongitudinalViewError.__bases__ == (Exception,)
    assert not issubclass(LongitudinalViewError, svc.LongitudinalServiceError)


def _imports(path):
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_view_imports_no_service_framework_or_llm():
    """Règles T5-B réimplémentées localement (aucune dépendance vers le
    service transactionnel), aucun framework, aucun client LLM / HTTP."""
    assert _imports(VIEW_PATH) == {"hashlib", "json", "uuid", "collections.abc", "dataclasses", "datetime",
                                   "types", "sqlalchemy", "core.models"}


def test_view_never_writes_locks_or_owns_a_transaction():
    tokens = _code_tokens(VIEW_PATH.read_text(encoding="utf-8")).split("\n")
    for forbidden in ("add", "add_all", "delete", "flush", "commit", "rollback", "begin", "begin_nested",
                      "merge", "expunge", "refresh", "update", "insert", "text", "execute_raw", "with_for_update",
                      "SessionLocal", "get_db", "pg_advisory_xact_lock", "pg_advisory_lock", "create_all",
                      "populate_existing", "HTTPException"):
        assert forbidden not in tokens, forbidden
    lowered = [t.lower() for t in tokens]
    for statement in ("insert ", "update ", "delete ", "truncate", "alter "):
        assert not any(statement in t for t in lowered), statement
    # Seule forme d'exécution : db.execute(select(...)), sous no_autoflush.
    tree = ast.parse(VIEW_PATH.read_text(encoding="utf-8"))
    db_calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "db"}
    assert db_calls == {"execute"}
    assert tokens.count("no_autoflush") == 2
    # Aucune affectation d'attribut (aucun objet ORM modifié).
    assigned = [t for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets
                if isinstance(t, ast.Attribute)]
    assert all(isinstance(t.value, ast.Name) and t.value.id == "self" for t in assigned)


def test_view_contains_no_stage_confidence_score_or_llm_vocabulary():
    tokens = _code_tokens(VIEW_PATH.read_text(encoding="utf-8")).split("\n")
    joined = "\n".join(tokens)
    for word in (*FORBIDDEN_OUTPUT, "anthropic", "openai", "claude", "llm", "prompt", "model_id", "evaluator",
                 "requests", "http", "ticker", "tickers", "sector", "surface", "cache", "redis", "event_time_s"):
        assert not _has_word(joined, word), word
    # « independent » n'est jamais une valeur : seule independence_evidence.
    assert "independent" not in tokens and "not_dependent" not in tokens
    # stage n'apparaît que comme local_stage (propriété locale d'observation).
    stage_tokens = [t for t in tokens if "stage" in t.lower()]
    assert stage_tokens and all("local_stage" in t.lower() for t in stage_tokens), stage_tokens


def _view_dataclasses():
    return [o for o in vars(view).values() if isinstance(o, type) and dataclasses.is_dataclass(o)]


def test_every_view_structure_is_a_frozen_dataclass_without_t6_fields():
    classes = _view_dataclasses()
    assert LongitudinalDossier in classes and len(classes) >= 25
    for cls in classes:
        assert cls.__dataclass_params__.frozen, cls
        for field in dataclasses.fields(cls):
            assert field.name != "event_time", cls.__name__
            for word in FORBIDDEN_OUTPUT:
                assert not _has_word(field.name, word), (cls.__name__, field.name)
            if "stage" in field.name:
                assert "local_stage" in field.name, (cls.__name__, field.name)
    root = [f.name for f in dataclasses.fields(LongitudinalDossier)]
    for name in ("run_id", "user_id", "competency_code", "pedagogical_taxonomy_release_id", "input_fingerprint",
                 "interpretation_status", "active_history", "dependency_profile", "coverage_profile",
                 "variety_profile", "transfer_profile", "consistency_profile", "temporal_validation_profile",
                 "limitations"):
        assert name in root, name


def test_vocabularies_match_t5b():
    assert view.SCOPE_MODES == svc.SCOPE_MODES
    assert view.SOURCE_KINDS == svc.SOURCE_KINDS
    assert view.DEPENDENCY_TYPES == svc.DEPENDENCY_TYPES
    assert view.COMPETENCY_CODES == svc.COMPETENCY_CODES
    assert (view.INPUT_SCHEMA_VERSION, view.SCOPE_SCHEMA_VERSION) == (svc.INPUT_SCHEMA_VERSION,
                                                                      svc.SCOPE_SCHEMA_VERSION)
    assert view.READABLE_RUN_STATES == {(svc.COMPLETED, svc.ACTIVE), (svc.COMPLETED, svc.SUPERSEDED)}
    assert view.UNSTABILIZED_RUN_STATES == {(svc.RUNNING, svc.CANDIDATE), (svc.FAILED, svc.OBSOLETE)}
    assert view.POSITIVE_LOCAL_STAGES == ("discovery", "comprehension", "application")
    assert "independent" not in view.SCOPE_CLASSIFICATIONS


def _random_evidence(rng, release):
    return [svc._Evidence(uuid.UUID(int=rng.getrandbits(128)), uuid.uuid4(), uuid.uuid4(),
                          rng.choice([release, uuid.uuid4()]), rng.choice(["localized", "competency_only"]),
                          "supportive", "application", frozenset(uuid.uuid4() for _ in range(rng.randrange(4))))
            for _ in range(rng.randrange(8))]


def test_input_fingerprint_parity_with_t5b():
    rng = random.Random(11)
    for _ in range(50):
        release = uuid.uuid4()
        evidence = _random_evidence(rng, release)
        expected = svc._input_fingerprint(user_id="u-é", competency_code="C7", release_id=release,
                                          evidence=evidence)
        rng.shuffle(evidence)
        assert view._input_fingerprint(
            user_id="u-é", competency_code="C7", release_id=release,
            evidence=[(e.observation_id, e.evaluation_run_id, e.event_id, e.source_release_id, e.compatible)
                      for e in evidence]) == expected


def test_scope_fingerprint_overlap_and_compatibility_parity_with_t5b():
    rng = random.Random(3)
    pool = [uuid.uuid4() for _ in range(4)]
    for _ in range(200):
        modes = [rng.choice(sorted(svc.SCOPE_MODES)) for _ in range(2)]
        defs = [frozenset(rng.sample(pool, rng.randrange(3))) for _ in range(2)]
        assert view._scope_fingerprint("C7", modes[0], defs[0]) == svc._scope_fingerprint(
            "C7", svc._Scope(modes[0], defs[0]))
        assert view._overlaps(modes[0], defs[0], modes[1], defs[1]) == svc._overlaps(
            svc._Scope(modes[0], defs[0]), svc._Scope(modes[1], defs[1]))
        release = rng.choice([pool[0], None, pool[1]])
        localization = rng.choice(["localized", "competency_only"])
        item = svc._Evidence(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), release, localization, "supportive",
                             "comprehension", defs[0])
        assert view._is_compatible(localization, release, defs[0], pool[0]) == svc._is_compatible(item, pool[0])


def test_capability_natural_order_is_never_lexical():
    refs = [view.CapabilityRef(uuid.uuid4(), code, 1, code) for code in ("C10_A", "C2_B", "C1_C", "C2_A", "C1_A")]
    assert [r.capability_code for r in sorted(refs, key=view._capability_order)] == [
        "C1_A", "C1_C", "C2_A", "C2_B", "C10_A"]


def test_freeze_and_payload():
    source = {"a": [1, {"b": "é"}], "n": None}
    frozen = view._freeze(source)
    source["a"].append(2)
    assert isinstance(frozen, MappingProxyType) and frozen["a"] == (1, MappingProxyType({"b": "é"}))
    with pytest.raises(TypeError):
        frozen["x"] = 1
    ref = view.CapabilityRef(uuid.UUID(int=5), "C7_A", 2, "libellé")
    unit = view.ScopeUnit(view.CAPABILITY_GRANULARITY, ref)
    with pytest.raises(dataclasses.FrozenInstanceError):
        unit.granularity = "x"
    moment = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    event = view.EventProvenance(uuid.UUID(int=9), "education", "interpret_metric", None, None, moment, None,
                                 None, view.DEMONSTRATION_TIME_UNAVAILABLE)
    assert unit.to_payload() == {"granularity": "capability", "capability": {
        "definition_id": str(uuid.UUID(int=5)), "capability_code": "C7_A", "semantic_revision": 2,
        "label": "libellé"}}
    payload = event.to_payload()
    assert payload["event_started_at_technical"] == "2026-01-02T03:04:00+00:00"
    assert payload["demonstration_time"] is None and payload["demonstration_time_source"] == "unavailable"
    assert view._primitive({"k": (frozen,)}) == {"k": [{"a": [1, {"b": "é"}], "n": None}]}


@pytest.mark.parametrize("value", INVALID_UUIDS)
def test_run_id_is_validated_before_the_database(value):
    with pytest.raises(InvalidLongitudinalViewArgument, match="run_id"):
        view.build_longitudinal_dossier(_NoDB(), run_id=value)


@pytest.mark.parametrize("field, value", [
    ("user_id", ""), ("user_id", " "), ("user_id", None), ("user_id", 3), ("user_id", "a\x00"),
    ("competency_code", "C0"), ("competency_code", "C7_A"), ("competency_code", None), ("competency_code", "c7"),
])
def test_active_arguments_are_validated_before_the_database(field, value):
    kwargs = {"user_id": USER, "competency_code": "C7", field: value}
    with pytest.raises(InvalidLongitudinalViewArgument, match=field):
        view.build_active_longitudinal_dossier(_NoDB(), **kwargs)


def test_valid_arguments_reach_the_database():
    _reaches_db(lambda db: view.build_longitudinal_dossier(db, run_id=uuid.uuid4()))
    _reaches_db(lambda db: view.build_active_longitudinal_dossier(db, user_id=USER, competency_code="C12"))


def test_view_is_not_wired_to_the_application():
    """Aucune route, aucun module applicatif n'importe la vue ; api.py
    inchangé."""
    checked = 0
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or rel.startswith(("tests/", ".git/")) or "node_modules" in path.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            source = _code_tokens(source)
        checked += 1
        # T6-B : seul consommateur de la vue (InferenceContext), lui-même
        # non branché : tests/test_inference_service.py.
        if rel not in ("core/longitudinal_view.py", "core/inference_service.py"):
            assert "longitudinal_view" not in source, rel
            for name in PUBLIC_API:
                assert name not in source, (rel, name)
    assert checked > 0
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8")).lower()
    for word in ("longitudinal", "dossier", "coverage_profile", "competency_profile", "active_history"):
        assert word not in api, word


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

def _dossier_run(Sessions, release_id, *relations, user_id=USER, competency_code="C7") -> uuid.UUID:
    """Run T5 complet (start, relations, complete), commité."""
    with Sessions() as session:
        run = svc.start_longitudinal_assessment(session, **_t5_kwargs(release_id, user_id=user_id,
                                                                      competency_code=competency_code))
        for relation in relations:
            relation(session, run.id)
        svc.complete_longitudinal_assessment(session, run_id=run.id)
        session.commit()
        return run.id


def dep(target, source=None, *, trace=None, mode="whole_observation", caps=(), kind="dependent"):
    return lambda s, run_id: svc.add_dependency(
        s, run_id=run_id, target_observation_id=target, source_kind="support_trace" if trace else "observation",
        source_observation_id=None if trace else source, source_support_trace_id=trace, dependency_type=kind,
        scope_mode=mode, dependency_basis={"why": "reprise d'un épisode antérieur"},
        capability_membership_ids=list(caps))


def tr(source, target, *, mode="whole_observation", caps=()):
    return lambda s, run_id: svc.add_transfer(
        s, run_id=run_id, source_observation_id=source, target_observation_id=target, scope_mode=mode,
        transfer_basis={"adaptation": "raisonnement réorganisé"}, capability_membership_ids=list(caps))


def rv(source, target, *, mode="whole_observation", caps=()):
    return lambda s, run_id: svc.add_revalidation(
        s, run_id=run_id, source_contradiction_observation_id=source, target_supportive_observation_id=target,
        scope_mode=mode, revalidation_basis={"mechanism": "démontré sans aide"}, capability_membership_ids=list(caps))


def _custom_event(Sessions, *, event_origin="education", task_kind="interpret_metric",
                  analysis_session_id=None, conversation_key=None, user_id=USER):
    with Sessions() as session:
        event = capture_event(session, user_id=user_id, event_origin=event_origin, task_kind=task_kind,
                              stimulus_snapshot={"question": "Que mesure le ROE ?"},
                              analysis_session_id=analysis_session_id,
                              conversation_key=conversation_key or f"conv-{user_id}", close="finalize_event")
        session.commit()
        return event.id


def _analysis_session(engine, ticker):
    session_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO analysis_sessions (id, user_id, ticker, status, started_at, updated_at) "
                             "VALUES (:id, :u, :t, 'in_progress', now(), now())"), {"id": session_id, "u": USER,
                                                                                   "t": ticker})
    return session_id


def _build(db, run_id):
    return view.build_longitudinal_dossier(db, run_id=run_id)


def _cap(unit):
    return None if unit.capability is None else unit.capability.capability_code


def _by_code(coverage):
    return {entry.capability.capability_code: entry for entry in coverage.capabilities}


def _readings(profile, observation_id):
    return {_cap(r.unit): r for r in profile.scope_readings if r.observation_id == observation_id}


def _tensions(consistency, observation_id):
    return {_cap(r.unit): r for r in consistency.tension_readings if r.contradiction_observation_id == observation_id}


def _walk(payload):
    """(clés, valeurs str) d'un payload JSON."""
    keys, values = set(), set()
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            keys |= set(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            values.add(item)
    return keys, values


def _assert_no_t6_output(dossier):
    payload = dossier.to_payload()
    json.dumps(payload)  # primitives JSON uniquement
    keys, values = _walk(payload)
    for word in FORBIDDEN_OUTPUT:
        assert not any(_has_word(k, word) for k in keys), word
    assert "event_time" not in keys
    assert not any("stage" in k and "local_stage" not in k for k in keys)
    assert "independent" not in values and "mastery" not in values


# --- A. historique actif et snapshot ---------------------------------------------

def test_pg_active_history_restores_exactly_the_frozen_snapshot(engine, Sessions, db):
    tx = _taxonomy(Sessions)
    A, B = tx.m["C7_A"], tx.m["C7_B"]
    session_id = _analysis_session(engine, "MC.PA")
    event = _custom_event(Sessions, event_origin="coach", task_kind="compare_options",
                          analysis_session_id=session_id, conversation_key="conv-1")
    s = _t3(Sessions, tx.id, sup(A, B, stage="discovery"), event_id=event)
    k = _t3(Sessions, tx.id, contra(A, error_type="procedural", evidence_strength="strong"))
    n = _t3(Sessions, tx.id, sup(B, stage="none"))
    c = _t3(Sessions, tx.id, only(sup()))
    other_user = _t3(Sessions, tx.id, sup(A), user_id=OTHER_USER)
    other_competency = _t3(Sessions, tx.id, sup(tx.m["C8_A"], competency_code="C8"))
    run_id = _dossier_run(Sessions, tx.id)
    later = _t3(Sessions, tx.id, sup(A))  # apparue APRÈS le run

    dossier = _build(db, run_id)
    history = dossier.active_history
    ids = {o.observation_id for o in history.observations}
    assert ids == {s.id, k.id, n.id, c.id}
    assert not ids & {other_user.id, other_competency.id, later.id}
    assert (dossier.run_id, dossier.user_id, dossier.competency_code, dossier.pedagogical_taxonomy_release_id,
            dossier.execution_status, dossier.interpretation_status) == (run_id, USER, "C7", tx.id, "completed",
                                                                         "active")
    run_row = _rows(engine, "SELECT * FROM longitudinal_assessment_runs WHERE id = :r", r=run_id)[0]
    assert dossier.input_fingerprint == run_row["input_fingerprint"]
    assert dossier.run_completed_at_technical == run_row["completed_at"]

    by_id = {o.observation_id: o for o in history.observations}
    for row in _rows(engine, "SELECT o.*, r.event_id, r.re_evaluates_run_id FROM pedagogical_observations o "
                             "JOIN observation_evaluation_runs r ON r.id = o.evaluation_run_id "
                             "WHERE o.id = ANY(CAST(:ids AS uuid[]))", ids=[str(i) for i in ids]):
        o = by_id[row["id"]]
        for field in ("evaluation_run_id", "event_id", "ordinal", "competency_code", "polarity",
                      "evidence_strength", "local_stage", "contradiction_scope", "error_type", "observation_role",
                      "task_kind", "elicitation_mode", "support_level", "capability_localization",
                      "observation_text", "re_evaluates_run_id"):
            assert getattr(o, field) == row[field], field
        assert view._primitive(o.primary_user_action) == row["primary_user_action"]
        assert view._primitive(o.contributive_user_actions) == row["contributive_user_actions"]
        assert view._primitive(o.residual_cognitive_work) == row["residual_cognitive_work"]
        assert view._primitive(o.source_contribution_refs) == row["source_contribution_refs"]
        assert o.observation_created_at_inference == row["created_at"]
        assert o.source_taxonomy_release_id == tx.id
        assert (o.current_integrity_status, o.current_evaluation_run_interpretation_status) == ("valid", "active")
    assert by_id[k.id].polarity == "contradictory" and by_id[k.id].error_type == "procedural"
    assert by_id[n.id].local_stage == "none"
    assert [ref.capability_code for ref in by_id[s.id].compatible_capabilities] == ["C7_A", "C7_B"]
    assert [ref.definition_id for ref in by_id[s.id].compatible_capabilities] == [tx.d["C7_A"], tx.d["C7_B"]]
    assert by_id[c.id].compatible_capabilities == ()
    provenance = by_id[s.id].event
    event_row = _rows(engine, "SELECT * FROM cognitive_events WHERE id = :e", e=event)[0]
    assert (provenance.event_id, provenance.event_origin, provenance.task_kind, provenance.analysis_session_id,
            provenance.conversation_key) == (event, "coach", "compare_options", session_id, "conv-1")
    assert provenance.event_started_at_technical == event_row["started_at"]
    assert provenance.event_closed_at_technical == event_row["closed_at"]
    assert provenance.demonstration_time is None and provenance.demonstration_time_source == "unavailable"
    _assert_no_t6_output(dossier)


def test_pg_sibling_observations_are_grouped_by_event(Sessions, db):
    tx = _taxonomy(Sessions)
    A, B, C = tx.m["C7_A"], tx.m["C7_B"], tx.m["C7_C"]
    same = _t3(Sessions, tx.id, sup(A), app(B), contra(C))
    alone = _t3(Sessions, tx.id, sup(A))
    dossier = _build(db, _dossier_run(Sessions, tx.id))
    episodes = {e.event.event_id: e for e in dossier.active_history.episodes}
    assert set(episodes) == {same.event, alone.event}
    assert episodes[same.event].observation_ids == tuple(same.ids)  # ordinal
    assert episodes[same.event].evaluation_run_id == same.run
    assert episodes[alone.event].observation_ids == (alone.id,)
    # Même événement : une seule occasion (aucune variation, aucune durabilité).
    assert dossier.variety_profile.demonstrated_cognitive_variation == ()
    assert dossier.temporal_validation_profile.durability_evidence_events == ()


def test_pg_superseded_run_is_historically_reproducible(engine, Sessions, db):
    tx = _taxonomy(Sessions)
    A = tx.m["C7_A"]
    o1 = _t3(Sessions, tx.id, sup(A))
    o2 = _t3(Sessions, tx.id, contra(A))
    l1 = _dossier_run(Sessions, tx.id)
    first = _build(db, l1).to_payload()
    o3 = _t3(Sessions, tx.id, app(A))
    # L1 reste active tant qu'aucun nouveau run : jamais élargie à O3.
    assert {o.observation_id for o in view.build_active_longitudinal_dossier(
        db, user_id=USER, competency_code="C7").active_history.observations} == {o1.id, o2.id}
    l2 = _dossier_run(Sessions, tx.id, rv(o2.id, o3.id))
    old = _build(db, l1)
    assert old.interpretation_status == "superseded"
    assert {o.observation_id for o in old.active_history.observations} == {o1.id, o2.id}
    assert old.consistency_profile.historical_revalidations == ()
    new = _build(db, l2)
    assert new.interpretation_status == "active"
    assert {o.observation_id for o in new.active_history.observations} == {o1.id, o2.id, o3.id}
    active = view.build_active_longitudinal_dossier(db, user_id=USER, competency_code="C7")
    assert active.run_id == l2 and active == new
    # Reconstruction déterministe : L1 identique hors statut d'interprétation.
    again = _build(db, l1).to_payload()
    assert {k: v for k, v in again.items() if k != "interpretation_status"} == {
        k: v for k, v in first.items() if k != "interpretation_status"}
    assert _build(db, l1) == old


def test_pg_upstream_changes_are_exposed_never_absorbed(engine, Sessions, db):
    """Une invalidation ou une réévaluation T3 après le run ne retire ni
    n'ajoute rien au snapshot : l'état courant est exposé et signalé."""
    tx = _taxonomy(Sessions)
    A = tx.m["C7_A"]
    kept = _t3(Sessions, tx.id, sup(A))
    reevaluated = _t3(Sessions, tx.id, sup(A))
    run_id = _dossier_run(Sessions, tx.id)
    _invalidate(Sessions, kept.id)
    newer = _t3(Sessions, tx.id, app(A), event_id=reevaluated.event)
    dossier = _build(db, run_id)
    by_id = {o.observation_id: o for o in dossier.active_history.observations}
    assert set(by_id) == {kept.id, reevaluated.id} and newer.id not in by_id
    assert by_id[kept.id].current_integrity_status == "invalidated"
    assert by_id[reevaluated.id].current_evaluation_run_interpretation_status == "superseded"
    limitation = {lim.code: lim for lim in dossier.limitations}[view.UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT]
    assert set(limitation.observation_ids) == {kept.id, reevaluated.id}


def test_pg_empty_dossier_is_representable(Sessions, db):
    tx = _taxonomy(Sessions)
    dossier = _build(db, _dossier_run(Sessions, tx.id))
    assert dossier.active_history.observations == () and dossier.active_history.episodes == ()
    assert [lim.code for lim in dossier.limitations] == [view.EMPTY_SNAPSHOT]
    assert all(entry.positive_observation_status == "not_observed_positive"
               for entry in dossier.coverage_profile.capabilities)
    assert dossier.temporal_validation_profile.exact_demonstration_time_available is False


# --- B. statuts de run ----------------------------------------------------------------

def test_pg_only_completed_active_or_superseded_runs_are_readable(engine, Sessions, db):
    tx = _taxonomy(Sessions)
    _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    with Sessions() as session:
        running = svc.start_longitudinal_assessment(session, **_t5_kwargs(tx.id)).id
        failed = svc.start_longitudinal_assessment(session, **_t5_kwargs(tx.id)).id
        svc.fail_longitudinal_assessment(session, run_id=failed, failure_code="stale_input")
        session.commit()
    for run_id in (running, failed):
        with pytest.raises(LongitudinalDossierNotStabilized):
            _build(db, run_id)
    with pytest.raises(LongitudinalDossierNotFound):
        _build(db, uuid.uuid4())
    assert view.build_active_longitudinal_dossier(db, user_id=USER, competency_code="C7") is None
    for execution, interpretation in (("completed", "candidate"), ("running", "active"), ("failed", "superseded"),
                                      ("completed", "obsolete")):
        _sql(db, "UPDATE longitudinal_assessment_runs SET execution_status = :e, interpretation_status = :i, "
                 "completed_at = now() WHERE id = :r", e=execution, i=interpretation, r=running)
        with pytest.raises(InvalidLongitudinalDossier, match="incohérent"):
            _build(db, running)
    db.rollback()


def test_pg_active_run_must_be_completed_and_unique(engine, Sessions, db):
    tx = _taxonomy(Sessions)
    _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    first = _dossier_run(Sessions, tx.id)
    second = _dossier_run(Sessions, tx.id)
    assert view.build_active_longitudinal_dossier(db, user_id=USER, competency_code="C7").run_id == second
    assert view.build_active_longitudinal_dossier(db, user_id=OTHER_USER, competency_code="C7") is None
    assert view.build_active_longitudinal_dossier(db, user_id=USER, competency_code="C8") is None
    _sql(db, "UPDATE longitudinal_assessment_runs SET execution_status = 'running' WHERE id = :r", r=second)
    with pytest.raises(InvalidLongitudinalDossier, match="completed attendu"):
        view.build_active_longitudinal_dossier(db, user_id=USER, competency_code="C7")
    db.rollback()
    _sql(db, "DROP INDEX uq_longitudinal_assessment_runs_one_active_user_competency")
    _sql(db, "UPDATE longitudinal_assessment_runs SET interpretation_status = 'active' WHERE id = :r", r=first)
    with pytest.raises(InvalidLongitudinalDossier, match="2 runs actifs"):
        view.build_active_longitudinal_dossier(db, user_id=USER, competency_code="C7")
    db.rollback()


# --- C. coverage --------------------------------------------------------------------------

def test_pg_coverage_describes_positive_depths_without_any_ratio(Sessions, db):
    tx = _taxonomy(Sessions, ("C7_A", "C7_B", "C7_C", "C7_D", "C8_A"))
    A, B, C = tx.m["C7_A"], tx.m["C7_B"], tx.m["C7_C"]
    discovery = _t3(Sessions, tx.id, sup(A, stage="discovery"))
    application = _t3(Sessions, tx.id, app(A, B))
    contradictory = _t3(Sessions, tx.id, contra(A))
    none_stage = _t3(Sessions, tx.id, sup(C, stage="none"))
    contra_c = _t3(Sessions, tx.id, contra(C, evidence_strength="strong"))
    only_sup = _t3(Sessions, tx.id, only(app()))
    only_none = _t3(Sessions, tx.id, only(sup(stage="none")))
    only_contra = _t3(Sessions, tx.id, only(contra()))
    dossier = _build(db, _dossier_run(Sessions, tx.id))
    coverage = dossier.coverage_profile
    assert [e.capability.capability_code for e in coverage.capabilities] == ["C7_A", "C7_B", "C7_C", "C7_D"]
    by = _by_code(coverage)
    assert by["C7_A"].discovery == (discovery.id,) and by["C7_A"].application == (application.id,)
    assert by["C7_A"].comprehension == () and by["C7_A"].contradictory == (contradictory.id,)
    assert by["C7_A"].positive_depths == ("discovery", "application")
    assert set(by["C7_A"].positive_event_ids) == {discovery.event, application.event}
    assert by["C7_B"].application == (application.id,) and by["C7_B"].positive_depths == ("application",)
    # contradictory et local_stage none : visibles, jamais une couverture.
    assert by["C7_C"].supportive_local_stage_none == (none_stage.id,)
    assert by["C7_C"].contradictory == (contra_c.id,)
    assert by["C7_C"].positive_depths == () and by["C7_C"].positive_observation_status == "not_observed_positive"
    assert by["C7_D"].positive_observation_status == "not_observed_positive"
    assert (by["C7_D"].discovery, by["C7_D"].contradictory) == ((), ())
    assert by["C7_A"].positive_observation_status == "observed_positive"
    # competency_only : section séparée, aucune capacité cochée.
    assert coverage.competency_only_supportive == (only_sup.id,)
    assert coverage.competency_only_supportive_local_stage_none == (only_none.id,)
    assert coverage.competency_only_contradictory == (only_contra.id,)
    competency_only = {only_sup.id, only_none.id, only_contra.id}
    for entry in coverage.capabilities:
        cited = {*entry.discovery, *entry.comprehension, *entry.application, *entry.supportive_local_stage_none,
                 *entry.contradictory}
        assert not cited & competency_only
    keys, values = _walk(coverage.to_payload())
    assert not {"weak", "failed", "not_mastered", "insufficient"} & values
    _assert_no_t6_output(dossier)


def test_pg_cross_release_keeps_only_compatible_definition_ids(Sessions, db):
    old = _taxonomy(Sessions, ("C7_A", "C7_B"))
    x, y = old.d["C7_A"], old.d["C7_B"]
    historical = _t3(Sessions, old.id, app(old.m["C7_A"], old.m["C7_B"]))
    new = _taxonomy(Sessions, {"C7_A": x, "C7_B": None})
    z = new.d["C7_B"]
    newer = _t3(Sessions, new.id, sup(new.m["C7_B"]))
    dossier = _build(db, _dossier_run(Sessions, new.id))
    by_id = {o.observation_id: o for o in dossier.active_history.observations}
    assert [c.definition_id for c in by_id[historical.id].compatible_capabilities] == [x]
    assert y not in {c.definition_id for o in by_id.values() for c in o.compatible_capabilities}
    by = _by_code(dossier.coverage_profile)
    assert by["C7_A"].capability.definition_id == x and by["C7_A"].application == (historical.id,)
    # Même code C7_B, autre définition : jamais identifiée à Y.
    assert by["C7_B"].capability.definition_id == z
    assert by["C7_B"].comprehension == (newer.id,) and historical.id not in by["C7_B"].application
    limitation = {lim.code: lim for lim in dossier.limitations}[view.CROSS_RELEASE_MAPPINGS_NOT_RETAINED]
    assert limitation.observation_ids == (historical.id,)


# --- D. dépendances et indépendance ---------------------------------------------------------

def test_pg_dependency_profile_never_infers_independence_from_absence(Sessions, db):
    tx = _taxonomy(Sessions)
    A, B = tx.m["C7_A"], tx.m["C7_B"]
    source = _t3(Sessions, tx.id, sup(A, B))
    dependent = _t3(Sessions, tx.id, app(A, B))
    partial = _t3(Sessions, tx.id, sup(A))
    transfer_target = _t3(Sessions, tx.id, app(A, B))
    contradiction = _t3(Sessions, tx.id, contra(A))
    revalidated = _t3(Sessions, tx.id, sup(A))
    lonely = _t3(Sessions, tx.id, sup(A, B))
    only_obs = _t3(Sessions, tx.id, only(sup()))
    run_id = _dossier_run(
        Sessions, tx.id,
        dep(dependent.id, source.id, mode="localized", caps=[A]),
        dep(partial.id, source.id, kind="partially_dependent"),
        # Dépendance sur C7_A : n'enlève pas la preuve d'indépendance C7_B.
        dep(transfer_target.id, source.id, mode="localized", caps=[A]),
        tr(source.id, transfer_target.id, mode="localized", caps=[B]),
        rv(contradiction.id, revalidated.id))
    dossier = _build(db, run_id)
    profile = dossier.dependency_profile
    assert [d.dependency_type for d in profile.dependencies] == ["dependent", "partially_dependent", "dependent"]
    first = profile.dependencies[0]
    assert (first.target_observation_id, first.source_kind, first.source_observation_id, first.source_event_id,
            first.target_event_id) == (dependent.id, "observation", source.id, source.event, dependent.event)
    assert first.scope.scope_mode == "localized" and [c.capability_code for c in first.scope.capabilities] == ["C7_A"]
    assert first.scope.scope_fingerprint == _scope_fp("localized", tx.d["C7_A"])
    assert dict(first.dependency_basis) == {"why": "reprise d'un épisode antérieur"}

    assert {k: r.classification for k, r in _readings(profile, dependent.id).items()} == {
        "C7_A": "dependent", "C7_B": "not_established"}
    assert {k: r.classification for k, r in _readings(profile, partial.id).items()} == {"C7_A": "partially_dependent"}
    readings = _readings(profile, transfer_target.id)
    assert (readings["C7_A"].classification, readings["C7_B"].classification) == ("dependent",
                                                                                  "independence_evidence")
    assert readings["C7_B"].independence_evidence_relation_ids == (dossier.transfer_profile.transfers[0].relation_id,)
    assert {k: r.classification for k, r in _readings(profile, revalidated.id).items()} == {
        "C7_A": "independence_evidence"}
    # Aucune arête : jamais « independent ».
    for observation in (lonely, source, contradiction, only_obs):
        assert {r.classification for r in _readings(profile, observation.id).values()} == {"not_established"}
    assert set(_readings(profile, only_obs.id)) == {None}
    assert {r.observation_id for r in profile.independence_evidence_scopes} == {transfer_target.id, revalidated.id}
    assert {r.observation_id for r in profile.dependent_scopes} == {dependent.id, transfer_target.id}
    assert {r.observation_id for r in profile.partially_dependent_scopes} == {partial.id}
    limitation = {lim.code: lim for lim in dossier.limitations}[
        view.INDEPENDENCE_NOT_ESTABLISHED_FOR_SOME_OBSERVATIONS]
    assert lonely.id in limitation.observation_ids and revalidated.id not in limitation.observation_ids
    _assert_no_t6_output(dossier)


def test_pg_competency_level_relations_are_never_distributed_on_capabilities(Sessions, db):
    tx = _taxonomy(Sessions)
    A = tx.m["C7_A"]
    only_source = _t3(Sessions, tx.id, only(sup()))
    target = _t3(Sessions, tx.id, app(A))
    only_target = _t3(Sessions, tx.id, only(app()))
    source = _t3(Sessions, tx.id, sup(A))
    run_id = _dossier_run(Sessions, tx.id, tr(only_source.id, target.id), tr(source.id, only_target.id,
                                                                             mode="competency_only"))
    dossier = _build(db, run_id)
    profile = dossier.dependency_profile
    # Cible localized, périmètre sans capacité : aucune capacité inventée.
    assert _readings(profile, target.id)["C7_A"].classification == "not_established"
    assert [(u.observation_id, u.relation_kind) for u in profile.unlocalized_independence_evidence] == [
        (target.id, "transfer")]
    # Cible competency_only : preuve au niveau compétence.
    assert _readings(profile, only_target.id)[None].classification == "independence_evidence"
    assert {lim.code for lim in dossier.limitations} >= {view.COMPETENCY_ONLY_SCOPE_NOT_LOCALIZABLE,
                                                         view.UNLOCALIZED_RELATION_SCOPE}


def test_pg_dependency_on_a_support_trace_keeps_its_provenance(Sessions, db):
    tx = _taxonomy(Sessions)
    trace_event, trace = _event_with_trace(Sessions)
    target = _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    dossier = _build(db, _dossier_run(Sessions, tx.id, dep(target.id, trace=trace, kind="partially_dependent")))
    (dependency,) = dossier.dependency_profile.dependencies
    assert (dependency.source_kind, dependency.source_observation_id, dependency.source_support_trace_id,
            dependency.source_event_id, dependency.source_support_kind) == ("support_trace", None, trace,
                                                                            trace_event, "hint")
    assert trace_event not in {e.event.event_id for e in dossier.active_history.episodes}
    (relation,) = dossier.temporal_validation_profile.directional_relations
    assert (relation.relation_kind, relation.source_support_trace_id, relation.source_event_id) == (
        "dependency", trace, trace_event)


# --- E. variété et transferts -------------------------------------------------------------------

def test_pg_nominal_differences_never_prove_cognitive_variety(engine, Sessions, db):
    tx = _taxonomy(Sessions)
    A = tx.m["C7_A"]
    e1 = _custom_event(Sessions, event_origin="education", task_kind="interpret_metric",
                       analysis_session_id=_analysis_session(engine, "MC.PA"))
    e2 = _custom_event(Sessions, event_origin="decryptage", task_kind="build_thesis",
                       analysis_session_id=_analysis_session(engine, "AI.PA"), conversation_key="autre")
    first = _t3(Sessions, tx.id, sup(A), event_id=e1)
    second = _t3(Sessions, tx.id, app(A), event_id=e2)
    dossier = _build(db, _dossier_run(Sessions, tx.id))
    variety = dossier.variety_profile
    assert variety.demonstrated_cognitive_variation == ()
    assert set(variety.unclassified_context_event_ids) == {e1, e2}
    descriptors = {d.event.event_id: d for d in variety.nominal_context_descriptors}
    assert (descriptors[e2].event.event_origin, descriptors[e2].event.task_kind) == ("decryptage", "build_thesis")
    assert descriptors[e1].observation_ids == (first.id,) and descriptors[e2].local_stages == ("application",)
    assert [c.capability_code for c in descriptors[e1].capabilities] == ["C7_A"]
    assert descriptors[e1].polarities == ("supportive",)
    assert view.COGNITIVE_CONTEXT_FAMILY_NOT_PERSISTED in {lim.code for lim in dossier.limitations}
    keys, _ = _walk(variety.to_payload())
    assert not any(word in k for k in keys for word in ("ticker", "sector", "family", "count"))
    # Avec un transfert validé : variation positivement établie.
    dossier = _build(db, _dossier_run(Sessions, tx.id, tr(first.id, second.id)))
    (variation,) = dossier.variety_profile.demonstrated_cognitive_variation
    assert (variation.source_event_id, variation.target_event_id, variation.source_observation_id,
            variation.target_observation_id) == (e1, e2, first.id, second.id)
    assert dossier.variety_profile.unclassified_context_event_ids == ()


def test_pg_transfer_profile_is_exact_and_never_transitive(Sessions, db):
    tx = _taxonomy(Sessions)
    A, B = tx.m["C7_A"], tx.m["C7_B"]
    a = _t3(Sessions, tx.id, sup(A, B))
    b = _t3(Sessions, tx.id, app(A, B))
    c = _t3(Sessions, tx.id, app(A))
    dossier = _build(db, _dossier_run(Sessions, tx.id, tr(a.id, b.id, mode="localized", caps=[B]), tr(b.id, c.id)))
    transfers = dossier.transfer_profile.transfers
    assert [(t.source_observation_id, t.target_observation_id) for t in transfers] == [(a.id, b.id), (b.id, c.id)]
    first, second = transfers
    assert (first.source_event_id, first.target_event_id, first.target_local_stage) == (a.event, b.event,
                                                                                         "application")
    assert [cap.definition_id for cap in first.scope.capabilities] == [tx.d["C7_B"]]
    assert second.scope.scope_mode == "whole_observation"
    assert [cap.capability_code for cap in second.scope.capabilities] == ["C7_A"]
    assert second.scope.scope_fingerprint == _scope_fp("whole_observation", tx.d["C7_A"])
    assert dict(first.transfer_basis) == {"adaptation": "raisonnement réorganisé"}
    assert not any(v.source_observation_id == a.id and v.target_observation_id == c.id
                   for v in dossier.variety_profile.demonstrated_cognitive_variation)
    assert len(dossier.temporal_validation_profile.evidence_of_independent_remobilization) == 2
    _assert_no_t6_output(dossier)


# --- F. cohérence, tensions, récurrences ----------------------------------------------------------

def test_pg_contradiction_without_comparable_positive_is_only_historical(Sessions, db):
    tx = _taxonomy(Sessions)
    k = _t3(Sessions, tx.id, contra(tx.m["C7_A"]))
    none_stage = _t3(Sessions, tx.id, sup(tx.m["C7_A"], stage="none"))  # pas une base positive
    consistency = _build(db, _dossier_run(Sessions, tx.id)).consistency_profile
    assert [c.observation_id for c in consistency.historical_contradictions] == [k.id]
    reading = _tensions(consistency, k.id)["C7_A"]
    assert reading.status == "historical_contradiction_without_comparable_positive"
    assert none_stage.id not in reading.comparable_positive_observation_ids
    assert consistency.open_historical_tensions == ()


def test_pg_tensions_are_scoped_by_definition_and_revalidation_is_partial(Sessions, db):
    tx = _taxonomy(Sessions)
    A, B, C = tx.m["C7_A"], tx.m["C7_B"], tx.m["C7_C"]
    positive_ab = _t3(Sessions, tx.id, app(A, B))
    k_ab = _t3(Sessions, tx.id, contra(A, B, error_type="factual_premise", evidence_strength="strong"))
    k_c = _t3(Sessions, tx.id, contra(C))
    target = _t3(Sessions, tx.id, sup(A, B))
    dossier = _build(db, _dossier_run(Sessions, tx.id, rv(k_ab.id, target.id, mode="localized", caps=[A])))
    consistency = dossier.consistency_profile
    tensions = _tensions(consistency, k_ab.id)
    assert tensions["C7_A"].status == "historically_revalidated"
    assert tensions["C7_A"].revalidation_ids == (consistency.historical_revalidations[0].relation_id,)
    assert tensions["C7_B"].status == "open_historical_tension"
    assert set(tensions["C7_B"].comparable_positive_observation_ids) == {positive_ab.id, target.id}
    # Contradiction C7_C : aucune contamination par la preuve positive C7_A / C7_B.
    assert _tensions(consistency, k_c.id)["C7_C"].status == "historical_contradiction_without_comparable_positive"
    # La contradiction revalidée reste intégralement visible.
    record = {c.observation_id: c for c in consistency.historical_contradictions}[k_ab.id]
    assert (record.contradiction_scope, record.error_type, record.evidence_strength) == (
        "comprehension", "factual_premise", "strong")
    assert [cap.capability_code for cap in record.capabilities] == ["C7_A", "C7_B"]
    assert record.observation_text
    (revalidation,) = consistency.historical_revalidations
    assert (revalidation.source_contradiction_observation_id, revalidation.target_supportive_observation_id,
            revalidation.source_event_id, revalidation.target_event_id) == (k_ab.id, target.id, k_ab.event,
                                                                            target.event)
    assert [cap.capability_code for cap in revalidation.scope.capabilities] == ["C7_A"]
    assert {(t.contradiction_observation_id, _cap(t.unit)) for t in consistency.open_historical_tensions} == {
        (k_ab.id, "C7_B")}
    assert {(t.contradiction_observation_id, _cap(t.unit))
            for t in consistency.historically_revalidated_tensions} == {(k_ab.id, "C7_A")}
    _assert_no_t6_output(dossier)


def test_pg_positive_and_contradiction_on_the_same_scope_open_a_tension(Sessions, db):
    tx = _taxonomy(Sessions)
    A, B = tx.m["C7_A"], tx.m["C7_B"]
    positive = _t3(Sessions, tx.id, sup(A))
    k_a = _t3(Sessions, tx.id, contra(A))
    k_b = _t3(Sessions, tx.id, contra(B))
    consistency = _build(db, _dossier_run(Sessions, tx.id)).consistency_profile
    assert _tensions(consistency, k_a.id)["C7_A"].status == "open_historical_tension"
    assert _tensions(consistency, k_a.id)["C7_A"].comparable_positive_observation_ids == (positive.id,)
    assert _tensions(consistency, k_b.id)["C7_B"].status == "historical_contradiction_without_comparable_positive"


def test_pg_full_revalidation_closes_its_scope_only(Sessions, db):
    tx = _taxonomy(Sessions)
    A = tx.m["C7_A"]
    k = _t3(Sessions, tx.id, contra(A))
    target = _t3(Sessions, tx.id, app(A))
    consistency = _build(db, _dossier_run(Sessions, tx.id, rv(k.id, target.id))).consistency_profile
    assert _tensions(consistency, k.id)["C7_A"].status == "historically_revalidated"
    assert [c.observation_id for c in consistency.historical_contradictions] == [k.id]


def test_pg_competency_only_contradiction_stays_competency_level(Sessions, db):
    tx = _taxonomy(Sessions)
    k = _t3(Sessions, tx.id, only(contra()))
    dossier = _build(db, _dossier_run(Sessions, tx.id))
    assert set(_tensions(dossier.consistency_profile, k.id)) == {None}
    assert _tensions(dossier.consistency_profile, k.id)[None].status == \
        "historical_contradiction_without_comparable_positive"
    positive = _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    target = _t3(Sessions, tx.id, only(sup()))
    dossier = _build(db, _dossier_run(Sessions, tx.id))
    (reading,) = _tensions(dossier.consistency_profile, k.id).values()
    assert reading.unit.granularity == "competency_only" and reading.unit.capability is None
    assert reading.status == "open_historical_tension"
    assert set(reading.comparable_positive_observation_ids) == {positive.id, target.id}
    closed = _build(db, _dossier_run(Sessions, tx.id, rv(k.id, target.id)))
    assert _tensions(closed.consistency_profile, k.id)[None].status == "historically_revalidated"
    # Contradiction localized revalidée par une cible competency_only :
    # périmètre non localisé, la tension reste ouverte sur la capacité.
    k_a = _t3(Sessions, tx.id, contra(tx.m["C7_A"]))
    unlocalized = _build(db, _dossier_run(Sessions, tx.id, rv(k_a.id, target.id)))
    reading = _tensions(unlocalized.consistency_profile, k_a.id)["C7_A"]
    assert reading.status == "open_historical_tension" and reading.revalidation_ids == ()
    assert len(reading.unlocalized_revalidation_ids) == 1


def test_pg_structural_recurrence_candidates(Sessions, db):
    tx = _taxonomy(Sessions)
    A, B = tx.m["C7_A"], tx.m["C7_B"]
    same_event = _t3(Sessions, tx.id, contra(A), contra(A, error_type="execution"))
    consistency = _build(db, _dossier_run(Sessions, tx.id)).consistency_profile
    # Même événement : jamais une répétition longitudinale.
    assert consistency.structural_recurrence_candidates == ()
    other = _t3(Sessions, tx.id, contra(A, B, contradiction_scope="application", error_type="procedural"))
    consistency = _build(db, _dossier_run(Sessions, tx.id)).consistency_profile
    (candidate,) = consistency.structural_recurrence_candidates
    assert candidate.kind == "structural_recurrence_candidate"
    assert _cap(candidate.common_scope) == "C7_A"
    assert set(candidate.observation_ids) == {*same_event.ids, other.id}
    assert set(candidate.event_ids) == {same_event.event, other.event}
    assert candidate.occurrence_groups == (tuple(same_event.ids), (other.id,))
    assert candidate.contradiction_scopes == ("application", "comprehension")
    assert candidate.error_types == ("conceptual", "execution", "procedural")
    assert candidate.dependency_linked_observation_ids == ()
    # Dépendance directe sur ce périmètre : une seule lignée, aucun candidat.
    linked = _build(db, _dossier_run(Sessions, tx.id, dep(other.id, same_event.ids[0], mode="localized",
                                                          caps=[A])))
    assert linked.consistency_profile.structural_recurrence_candidates == ()
    # Dépendance sur un AUTRE périmètre (C7_B) : ne relie pas C7_A.
    third = _t3(Sessions, tx.id, contra(B))
    unrelated = _build(db, _dossier_run(Sessions, tx.id, dep(other.id, third.id, mode="localized", caps=[B])))
    by_scope = {_cap(c.common_scope): c for c in unrelated.consistency_profile.structural_recurrence_candidates}
    assert set(by_scope) == {"C7_A"}  # C7_B : other dépend de third sur C7_B
    assert by_scope["C7_A"].dependency_linked_observation_ids == ()
    assert view.SEMANTIC_CONTRADICTION_MOTIF_NOT_PERSISTED in {lim.code for lim in unrelated.limitations}
    _, values = _walk(unrelated.to_payload())
    assert "confirmed_cognitive_motif" not in values


def _candidates(dossier) -> dict:
    return {_cap(c.common_scope): c for c in dossier.consistency_profile.structural_recurrence_candidates}


def test_pg_contradictions_dependent_on_the_same_support_trace_are_not_a_recurrence(Sessions, db):
    """A. Deux contradictions C7_A d'événements distincts, toutes deux
    dependent de la MÊME aide antérieure : aucune occurrence autonome."""
    tx = _taxonomy(Sessions)
    A = tx.m["C7_A"]
    _, trace = _event_with_trace(Sessions)
    first = _t3(Sessions, tx.id, contra(A))
    second = _t3(Sessions, tx.id, contra(A))
    unlinked = _build(db, _dossier_run(Sessions, tx.id))
    assert set(_candidates(unlinked)) == {"C7_A"}  # sans dépendance connue : candidat
    dossier = _build(db, _dossier_run(Sessions, tx.id, dep(first.id, trace=trace), dep(second.id, trace=trace)))
    assert _candidates(dossier) == {}


@pytest.mark.parametrize("kind", ["dependent", "partially_dependent"])
def test_pg_one_autonomous_and_one_trace_dependent_contradiction_are_not_a_recurrence(Sessions, db, kind):
    """B / C. Une contradiction sans dépendance + une contradiction
    dependent / partially_dependent d'une aide : une seule occasion
    autonome, aucun candidat (partially_dependent ne suffit jamais)."""
    tx = _taxonomy(Sessions)
    A = tx.m["C7_A"]
    _, trace = _event_with_trace(Sessions)
    _t3(Sessions, tx.id, contra(A))
    dependent = _t3(Sessions, tx.id, contra(A))
    dossier = _build(db, _dossier_run(Sessions, tx.id, dep(dependent.id, trace=trace, kind=kind)))
    assert _candidates(dossier) == {}


def test_pg_contradictions_dependent_on_the_same_supportive_source_are_not_a_recurrence(Sessions, db):
    """D. Source supportive hors du groupe des contradictions : la lignée
    compte quand même."""
    tx = _taxonomy(Sessions)
    A = tx.m["C7_A"]
    source = _t3(Sessions, tx.id, sup(A))
    first = _t3(Sessions, tx.id, contra(A))
    second = _t3(Sessions, tx.id, contra(A))
    dossier = _build(db, _dossier_run(Sessions, tx.id, dep(first.id, source.id),
                                      dep(second.id, source.id, kind="partially_dependent")))
    assert _candidates(dossier) == {}
    # Une troisième contradiction autonome ne suffit pas seule non plus.
    third = _t3(Sessions, tx.id, contra(A))
    dossier = _build(db, _dossier_run(Sessions, tx.id, dep(first.id, source.id), dep(second.id, source.id)))
    assert third.id not in {d.target_observation_id for d in dossier.dependency_profile.dependencies}
    assert _candidates(dossier) == {}


def test_pg_localized_dependency_neutralizes_only_its_scope(Sessions, db):
    """E. Dépendance localized C7_A sur une contradiction C7_A+C7_B : plus
    de candidat C7_A ; C7_B, non couverte, reste candidate."""
    tx = _taxonomy(Sessions)
    A, B = tx.m["C7_A"], tx.m["C7_B"]
    _, trace = _event_with_trace(Sessions)
    linked = _t3(Sessions, tx.id, contra(A, B))
    other = _t3(Sessions, tx.id, contra(A, B))
    dossier = _build(db, _dossier_run(Sessions, tx.id, dep(linked.id, trace=trace, mode="localized", caps=[A])))
    candidates = _candidates(dossier)
    assert set(candidates) == {"C7_B"}
    assert set(candidates["C7_B"].observation_ids) == {linked.id, other.id}
    assert candidates["C7_B"].dependency_linked_observation_ids == ()
    assert candidates["C7_B"].kind == "structural_recurrence_candidate"


@pytest.mark.parametrize("mode", ["whole_observation", "competency_only"])
def test_pg_non_localized_dependency_neutralizes_every_scope_of_its_target(Sessions, db, mode):
    """F. whole_observation / competency_only : chevauchement conservateur,
    tous les périmètres de la cible sont neutralisés."""
    tx = _taxonomy(Sessions)
    A, B = tx.m["C7_A"], tx.m["C7_B"]
    source = _t3(Sessions, tx.id, sup(A, B))
    linked = _t3(Sessions, tx.id, contra(A, B))
    _t3(Sessions, tx.id, contra(A, B))
    dossier = _build(db, _dossier_run(Sessions, tx.id, dep(linked.id, source.id, mode=mode)))
    assert _candidates(dossier) == {}


def test_pg_dependency_linked_contradictions_stay_visible_in_a_candidate(Sessions, db):
    """Trois contradictions C7_A : deux autonomes (candidat), une dépendante
    listée à part, jamais comptée comme occasion."""
    tx = _taxonomy(Sessions)
    A = tx.m["C7_A"]
    _, trace = _event_with_trace(Sessions)
    first = _t3(Sessions, tx.id, contra(A))
    second = _t3(Sessions, tx.id, contra(A))
    dependent = _t3(Sessions, tx.id, contra(A))
    (candidate,) = _build(db, _dossier_run(Sessions, tx.id, dep(dependent.id, trace=trace))
                          ).consistency_profile.structural_recurrence_candidates
    assert candidate.observation_ids == (first.id, second.id)
    assert candidate.event_ids == (first.event, second.event)
    assert candidate.occurrence_groups == ((first.id,), (second.id,))
    assert candidate.dependency_linked_observation_ids == (dependent.id,)


@pytest.mark.parametrize("mode", ["whole_observation", "competency_only"])
def test_pg_competency_only_dependent_contradiction_is_not_an_autonomous_occurrence(Sessions, db, mode):
    """G. Niveau compétence : une contradiction competency_only ciblée par
    une dépendance ne crée pas d'occasion autonome."""
    tx = _taxonomy(Sessions)
    _, trace = _event_with_trace(Sessions)
    linked = _t3(Sessions, tx.id, only(contra()))
    _t3(Sessions, tx.id, only(contra()))
    assert set(_candidates(_build(db, _dossier_run(Sessions, tx.id)))) == {None}
    dossier = _build(db, _dossier_run(Sessions, tx.id, dep(linked.id, trace=trace, mode=mode)))
    assert _candidates(dossier) == {}


def test_pg_competency_only_recurrence_is_separate(Sessions, db):
    tx = _taxonomy(Sessions)
    first = _t3(Sessions, tx.id, only(contra()))
    second = _t3(Sessions, tx.id, only(contra()))
    localized = _t3(Sessions, tx.id, contra(tx.m["C7_A"]))
    (candidate,) = _build(db, _dossier_run(Sessions, tx.id)).consistency_profile.structural_recurrence_candidates
    assert candidate.common_scope.capability is None
    assert set(candidate.observation_ids) == {first.id, second.id} and localized.id not in candidate.observation_ids


# --- G. temporalité -------------------------------------------------------------------------------

def test_pg_temporal_profile_uses_technical_timestamps_only(engine, Sessions, db):
    tx = _taxonomy(Sessions)
    A = tx.m["C7_A"]
    k = _t3(Sessions, tx.id, contra(A))
    source = _t3(Sessions, tx.id, sup(A))
    target = _t3(Sessions, tx.id, app(A))
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE cognitive_events SET started_at = :t, closed_at = :c WHERE id = :e"),
                     {"t": old, "c": old + timedelta(minutes=5), "e": source.event})
    run_id = _dossier_run(Sessions, tx.id, tr(source.id, target.id), rv(k.id, target.id))
    dossier = _build(db, run_id)
    temporal = dossier.temporal_validation_profile
    assert temporal.exact_demonstration_time_available is False
    assert temporal.temporal_precision == "technical_timestamps_only"
    events = {e.event_id: e for e in temporal.events}
    assert events[source.event].event_started_at_technical == old
    assert events[source.event].event_closed_at_technical == old + timedelta(minutes=5)
    assert all(e.demonstration_time is None and e.demonstration_time_source == "unavailable"
               for e in temporal.events)
    # Observation interprétée récemment d'un épisode ancien : aucun « récent ».
    times = {t.observation_id: t for t in temporal.observation_inference_times}
    assert times[source.id].observation_created_at_inference > old + timedelta(days=365)
    assert set(temporal.positive_observation_ids) == {source.id, target.id}
    assert set(temporal.positive_event_ids) == {source.event, target.event}
    assert temporal.transfer_target_event_ids == (target.event,)
    assert temporal.revalidation_target_event_ids == (target.event,)
    assert [(r.relation_kind, r.source_event_id, r.target_event_id)
            for r in temporal.evidence_of_independent_remobilization] == [
        ("transfer", source.event, target.event), ("revalidation", k.event, target.event)]
    assert [(d.relation_kind, d.source_event_id, d.target_event_id) for d in temporal.durability_evidence_events] == [
        ("transfer", source.event, target.event), ("revalidation", k.event, target.event)]
    limitation = {lim.code: lim for lim in dossier.limitations}[view.EXACT_DEMONSTRATION_TIME_UNAVAILABLE]
    assert set(limitation.observation_ids) == {k.id, source.id, target.id}
    keys, values = _walk(dossier.to_payload())
    for word in ("fresh", "stale", "days_since", "decay", "age", "event_time", "recent", "old", "long", "short"):
        assert not any(k == word or k.startswith(word + "_") or k.endswith("_" + word) for k in keys), word
        assert word not in values, word
    _assert_no_t6_output(dossier)


# --- H. lecture seule, performance, déterminisme -----------------------------------------------------

def _all_tables(engine):
    return {t: sorted(map(repr, _rows(engine, f"SELECT * FROM {t}"))) for t in Base.metadata.tables}


def _rich_dossier(Sessions, tx, size):
    A, B = tx.m["C7_A"], tx.m["C7_B"]
    sources = [_t3(Sessions, tx.id, sup(A, B), contra(B)) for _ in range(size)]
    targets = [_t3(Sessions, tx.id, app(A, B)) for _ in range(size)]
    trace_event, trace = _event_with_trace(Sessions)
    extra = _t3(Sessions, tx.id, only(contra()), sup(A), event_id=trace_event)
    relations = [tr(s.ids[0], t.id, mode="localized", caps=[A]) for s, t in zip(sources, targets)]
    relations += [rv(s.ids[1], t.id, mode="localized", caps=[B]) for s, t in zip(sources, targets)]
    relations += [dep(s.ids[1], trace=trace) for s in sources]
    return _dossier_run(Sessions, tx.id, *relations), extra


def test_pg_build_is_read_only_and_never_flushes_the_caller(engine, Sessions):
    tx = _taxonomy(Sessions)
    run_id, _ = _rich_dossier(Sessions, tx, 2)
    before = _all_tables(engine)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    # Session en autoflush=True avec une écriture en attente de l'appelant :
    # la vue (no_autoflush) ne la flushe jamais.
    session = sa.orm.Session(bind=engine, autoflush=True)
    pending = User(id="pending-user", level="debutant")
    session.add(pending)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        dossier = view.build_longitudinal_dossier(session, run_id=run_id)
        active = view.build_active_longitudinal_dossier(session, user_id=USER, competency_code="C7")
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert active == dossier
    assert statements and all(s.startswith("SELECT") for s in statements), statements
    assert not any(" FOR " in s for s in statements)
    assert pending in session.new and not session.dirty and not session.deleted
    assert list(session.identity_map.values()) == []  # aucun objet ORM chargé
    session.rollback()
    session.close()
    assert _all_tables(engine) == before


def test_pg_query_count_is_constant_whatever_the_dossier_size(engine, Sessions, db):
    counts = []
    for size in (1, 6):
        tx = _taxonomy(Sessions)
        run_id, _ = _rich_dossier(Sessions, tx, size)
        statements = []

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            dossier = _build(db, run_id)
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        assert len(dossier.transfer_profile.transfers) == size
        counts.append(len(statements))
        db.rollback()
    assert counts[0] == counts[1] <= 12, counts


def test_pg_build_is_deterministic_and_json_serializable(Sessions, db):
    tx = _taxonomy(Sessions)
    run_id, _ = _rich_dossier(Sessions, tx, 3)
    first = _build(db, run_id)
    db.expire_all()
    second = _build(db, run_id)
    assert first == second
    assert json.dumps(first.to_payload(), sort_keys=True) == json.dumps(second.to_payload(), sort_keys=True)
    payload = first.to_payload()
    assert payload["run_id"] == str(run_id) and payload["view_schema_version"] == 1
    observations = first.active_history.observations
    keys = [(o.event.event_started_at_technical, str(o.event_id), o.ordinal) for o in observations]
    assert keys == sorted(keys)
    for profile in (first.dependency_profile.dependencies, first.transfer_profile.transfers,
                    first.consistency_profile.historical_revalidations):
        assert len({r.relation_id for r in profile}) == len(profile)
    _assert_no_t6_output(first)


def test_pg_compatible_scopes_match_the_t5b_snapshot(Sessions, db):
    old = _taxonomy(Sessions, ("C7_A", "C7_B"))
    _t3(Sessions, old.id, app(old.m["C7_A"], old.m["C7_B"]))
    new = _taxonomy(Sessions, {"C7_A": old.d["C7_A"], "C7_B": None, "C7_C": None})
    _t3(Sessions, new.id, sup(new.m["C7_B"], new.m["C7_C"]), only(contra()))
    run_id = _dossier_run(Sessions, new.id)
    evidence = svc._run_evidence(db, svc.get_longitudinal_assessment(db, run_id=run_id))
    dossier = _build(db, run_id)
    assert {o.observation_id: frozenset(c.definition_id for c in o.compatible_capabilities)
            for o in dossier.active_history.observations} == {k: v.compatible for k, v in evidence.items()}


# --- I. corruption : refus, jamais de réparation ---------------------------------------------------

@pytest.fixture
def corruptible(Sessions):
    tx = _taxonomy(Sessions)
    A, B = tx.m["C7_A"], tx.m["C7_B"]
    w = SimpleNamespace(tx=tx, A=A, B=B)
    w.s = _t3(Sessions, tx.id, sup(A, B))
    w.t = _t3(Sessions, tx.id, app(A, B))
    w.k = _t3(Sessions, tx.id, contra(A))
    w.plain = _t3(Sessions, tx.id, sup(A))
    w.other_user = _t3(Sessions, tx.id, sup(A), user_id=OTHER_USER)
    w.c8 = _t3(Sessions, tx.id, sup(tx.m["C8_A"], competency_code="C8"))
    w.other_release = _taxonomy(Sessions, ("C7_A",), activate=False)
    w.run = _dossier_run(Sessions, tx.id, dep(w.t.id, w.s.id, mode="localized", caps=[A]))
    return w


def _corruption(db, w, case):
    if case == "relation_outside_snapshot":
        _raw_edge(db, "observation_transfers", run_id=w.run, source_observation_id=w.s.id,
                  target_observation_id=w.other_user.id, scope_mode="competency_only", transfer_basis={"a": "x"},
                  scope_fingerprint=_scope_fp("competency_only"))
        return "hors du snapshot"
    if case == "membership_of_another_release":
        _sql(db, "UPDATE dependency_capabilities SET capability_membership_id = :m",
             m=w.other_release.m["C7_A"])
        return "autre release"
    if case == "scope_fingerprint":
        _sql(db, "UPDATE observation_dependencies SET scope_fingerprint = :f", f=_scope_fp("localized", w.tx.d["C7_B"]))
        return "scope_fingerprint"
    if case == "input_of_another_competency":
        _sql(db, "INSERT INTO longitudinal_assessment_inputs (run_id, observation_id) VALUES (:r, :o)",
             r=w.run, o=w.c8.id)
        return "compétence"
    if case == "input_of_another_user":
        _sql(db, "INSERT INTO longitudinal_assessment_inputs (run_id, observation_id) VALUES (:r, :o)",
             r=w.run, o=w.other_user.id)
        return "autre utilisateur"
    if case == "input_row_deleted":
        _sql(db, "DELETE FROM longitudinal_assessment_inputs WHERE run_id = :r AND observation_id = :o",
             r=w.run, o=w.plain.id)
        return "input_fingerprint"
    if case == "input_fingerprint":
        _sql(db, "UPDATE longitudinal_assessment_runs SET input_fingerprint = 'falsifié' WHERE id = :r", r=w.run)
        return "input_fingerprint"
    if case == "missing_input_observation":
        _sql(db, "ALTER TABLE longitudinal_assessment_inputs "
                 "DROP CONSTRAINT longitudinal_assessment_inputs_observation_id_fkey")
        _sql(db, "INSERT INTO longitudinal_assessment_inputs (run_id, observation_id) VALUES (:r, :o)",
             r=w.run, o=uuid.uuid4())
        return "introuvables"
    if case == "t3_run_not_completed":
        _sql(db, "UPDATE observation_evaluation_runs SET execution_status = 'failed' WHERE id = :r", r=w.plain.run)
        return "run T3"
    if case == "contradictory_dependency_classification":
        dependency_id = _raw_edge(db, "observation_dependencies", run_id=w.run, target_observation_id=w.t.id,
                                  source_kind="observation", source_observation_id=w.s.id,
                                  dependency_type="partially_dependent", scope_mode="localized",
                                  dependency_basis={"why": "copie"},
                                  scope_fingerprint=_scope_fp("localized", w.tx.d["C7_A"]))
        _sql(db, "INSERT INTO dependency_capabilities (dependency_id, capability_membership_id) VALUES (:d, :m)",
             d=dependency_id, m=w.A)
        return "déjà classée"
    if case == "transfer_target_not_application":
        _raw_edge(db, "observation_transfers", run_id=w.run, source_observation_id=w.t.id,
                  target_observation_id=w.plain.id, scope_mode="whole_observation", transfer_basis={"a": "x"},
                  scope_fingerprint=_scope_fp("whole_observation", w.tx.d["C7_A"]))
        return "local_stage"
    if case == "transfer_overlapping_dependency":
        _raw_edge(db, "observation_transfers", run_id=w.run, source_observation_id=w.s.id,
                  target_observation_id=w.t.id, scope_mode="whole_observation", transfer_basis={"a": "x"},
                  scope_fingerprint=_scope_fp("whole_observation", w.tx.d["C7_A"], w.tx.d["C7_B"]))
        return "chevauchant"
    if case == "revalidation_polarity":
        _raw_edge(db, "observation_revalidations", run_id=w.run, source_contradiction_observation_id=w.plain.id,
                  target_supportive_observation_id=w.t.id, scope_mode="whole_observation",
                  revalidation_basis={"m": "x"}, scope_fingerprint=_scope_fp("whole_observation", w.tx.d["C7_A"]))
        return "polarités"
    if case == "empty_basis":
        _sql(db, "UPDATE observation_dependencies SET dependency_basis = '{}'::jsonb")
        return "non vide"
    raise AssertionError(case)


CORRUPTIONS = ["relation_outside_snapshot", "membership_of_another_release", "scope_fingerprint",
               "input_of_another_competency", "input_of_another_user", "input_row_deleted", "input_fingerprint",
               "missing_input_observation", "t3_run_not_completed", "contradictory_dependency_classification",
               "transfer_target_not_application", "transfer_overlapping_dependency", "revalidation_polarity",
               "empty_basis"]


@pytest.mark.parametrize("case", CORRUPTIONS)
def test_pg_corrupted_dossier_is_refused_never_repaired(engine, Sessions, db, corruptible, case):
    w = corruptible
    _build(db, w.run)  # dossier cohérent
    match = _corruption(db, w, case)
    corrupted = _all_tables_in(db)
    with pytest.raises(InvalidLongitudinalDossier, match=match):
        _build(db, w.run)
    with pytest.raises(InvalidLongitudinalDossier):
        view.build_active_longitudinal_dossier(db, user_id=USER, competency_code="C7")
    # Aucune réparation : l'état corrompu est intact dans la transaction.
    assert _all_tables_in(db) == corrupted
    db.rollback()
    _build(db, w.run)


def _all_tables_in(db):
    return {t: sorted(map(repr, db.execute(sa.text(f"SELECT * FROM {t}")).mappings().all()))
            for t in ("longitudinal_assessment_runs", "longitudinal_assessment_inputs", "observation_dependencies",
                      "dependency_capabilities", "observation_transfers", "observation_revalidations",
                      "pedagogical_observations", "observation_evaluation_runs")}
