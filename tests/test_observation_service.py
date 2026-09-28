"""Tests de T3-B : service interne transactionnel des observations
pédagogiques (core/observation_service.py).

Aucune migration dans ce chantier : le schéma testé est celui produit par
`alembic upgrade head` (= 0006_observation_layer, T3-A, puis
0007_pedagogical_taxonomy depuis T4-A, qui ajoute les tables de taxonomie
— non utilisées par ce service — et la FK
observation_evaluation_runs.pedagogical_taxonomy_release_id, puis
0008_longitudinal_relations depuis T5-A et
0009_competency_inference_state depuis T6-A, tables vides non utilisées par
ce service).

1. Tests sans base (toujours exécutés) : aucune migration T3-B, API publique
   exacte, petite hiérarchie d'exceptions, aucun commit/rollback ni
   dépendance FastAPI / LLM dans le service, aucun branchement applicatif,
   aucun vocabulaire d'inférence, vocabulaires identiques aux CHECK de
   0006, validation structurelle complète AVANT tout accès à la base,
   requêtes de verrouillage.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T1/T2/T3-A) :
   uniquement si ORYX_TEST_DATABASE_URL pointe vers une base DÉDIÉE dont le
   nom contient "test" (schéma public détruit et recréé). Sinon SKIPPÉS :
   rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t3b_test \\
           python -m pytest tests/test_observation_service.py

   Les sessions sont créées comme core.db.SessionLocal (autoflush=False) ;
   la transaction appartient toujours au test (l'appelant). Les tests de
   concurrence utilisent des connexions réelles et attendent, via
   pg_blocking_pids(), que la seconde soit effectivement bloquée par la
   première — et vérifient sur quelle requête (pg_stat_activity) — avant de
   la libérer : aucun sleep() arbitraire.
"""
import ast
import inspect
import itertools
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from core import cognitive_capture as cc
from core import observation_service as svc
from core.models import ObservationEvaluationRun, PedagogicalObservation, User
from core.observation_service import (
    DuplicateEvaluationRun,
    EvaluationRunNotFound,
    EventNotEvaluable,
    EventNotFound,
    InvalidEvaluationState,
    InvalidObservationPayload,
    InvalidReevaluationTarget,
    ObservationAlreadyInvalidated,
    ObservationNotFound,
    ObservationServiceError,
)
from tests.test_cognitive_capture import _backend_pid, _wait_until_blocked_by
from tests.test_migration_0002_analysis_sessions import (
    BASELINE,
    REPO_ROOT,
    T1A,
    _script_directory,
    pg_url,  # noqa: F401 — fixture
)
from tests.test_migration_0003_analysis_session_links import T1B1
from tests.test_migration_0004_drop_company_analyses import T1C2, _code_tokens
from tests.test_migration_0005_cognitive_support_traces import OTHER_USER, T2A, USER, _upgrade_head_with_users
from tests.test_migration_0006_observation_layer import ACTIVE_INDEX, OBS_VOCABULARIES, RUN_VOCABULARIES, T3A
from tests.test_migration_0007_pedagogical_taxonomy import RELEASE_FK, T4A, T4A_MODELS, T4A_TABLES

T5A = "0008_longitudinal_relations"
T6A = "0009_competency_inference_state"

SERVICE_PATH = REPO_ROOT / "core" / "observation_service.py"
DEDUP_INDEX = "uq_observation_evaluation_runs_dedup_key"
PUBLIC_API = {
    "start_evaluation_run",
    "add_observation",
    "complete_evaluation_run",
    "fail_evaluation_run",
    "invalidate_observation",
    "get_evaluation_run",
    "get_observations",
}
EXCEPTIONS = {
    EventNotFound,
    EventNotEvaluable,
    EvaluationRunNotFound,
    ObservationNotFound,
    InvalidObservationPayload,
    InvalidEvaluationState,
    InvalidReevaluationTarget,
    DuplicateEvaluationRun,
    ObservationAlreadyInvalidated,
}
START_REQUIRED_TEXT = [
    "trigger",
    "evaluation_dedup_key",
    "normalization_version",
    "local_stage_version",
    "capability_mapping_version",
    "evaluation_schema_version",
    "evaluator_version",
    "input_fingerprint",
]
CLOSED_VOCABULARIES = {
    "competency_code": svc.COMPETENCY_CODES,
    "observation_role": svc.OBSERVATION_ROLES,
    "elicitation_mode": svc.ELICITATION_MODES,
    "support_level": svc.SUPPORT_LEVELS,
    "polarity": svc.POLARITIES,
    "evidence_strength": svc.EVIDENCE_STRENGTHS,
    "local_stage": svc.LOCAL_STAGES,
    "contradiction_scope": svc.CONTRADICTION_SCOPES,
    "error_type": svc.ERROR_TYPES,
    "capability_localization": svc.CAPABILITY_LOCALIZATIONS,
}
JSON_ROOTS = {
    "primary_user_action": dict,
    "contributive_user_actions": list,
    "source_contribution_refs": list,
    "residual_cognitive_work": dict,
}


class _Stage(str, Enum):
    APPLICATION = "application"


class _NoDB:
    """Session factice : la validation structurelle doit échouer avant tout
    accès à la base."""

    def __getattr__(self, name):
        raise AssertionError(f"accès à la base inattendu : {name}")


def _reaches_db(call):
    """La validation est passée : l'appel atteint la base (_NoDB)."""
    with pytest.raises(AssertionError, match="accès à la base inattendu"):
        call(_NoDB())


INVALID_JSON_VALUES = [
    {"t": (1, 2)},
    {"s": {1, 2}},
    {"nan": float("nan")},
    {"inf": float("inf")},
    {"neg_inf": [float("-inf")]},
    {"decimal": Decimal("1.5")},
    {"date": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    {"uuid": uuid.uuid4()},
    {"bytes": b"x"},
    {"objet": object()},
    {"enum": _Stage.APPLICATION},
    {1: "clé int"},
    {None: "clé None"},
    {True: "clé bool"},
    {("a", "b"): "clé tuple"},
    {"nul": "a\x00b"},
    {"a\x00": "clé avec NUL"},
    {"nested": {"deep": [{"ok": 1}, {"ko": Decimal("2")}]}},
]


def _as_root(value, root):
    """Place une valeur invalide sous la racine attendue (dict ou list)."""
    if root is dict:
        return value if isinstance(value, dict) else {"v": value}
    return [value]


def _start_kwargs(event_id=None, **overrides):
    kwargs = {
        "event_id": uuid.uuid4() if event_id is None else event_id,
        "trigger": "event_finalized",
        "evaluation_dedup_key": f"dedup-{uuid.uuid4()}",
        "normalization_version": "norm-1",
        "local_stage_version": "stage-1",
        "capability_mapping_version": "map-1",
        "evaluation_schema_version": "schema-1",
        "evaluator_version": "evaluator-1",
        "input_fingerprint": "sha256:in",
    }
    kwargs.update(overrides)
    return kwargs


def _obs_kwargs(**overrides):
    kwargs = {
        "competency_code": "C7",
        "observation_role": "primary",
        "task_kind": "interpret_metric",
        "primary_user_action": {"work_index": 0, "kind": "explanation"},
        "contributive_user_actions": [{"work_index": 1}],
        "elicitation_mode": "prompted",
        "support_level": "hinted",
        "source_contribution_refs": [{"kind": "user_work", "index": 0}, {"kind": "support_trace", "seq": 1}],
        "residual_cognitive_work": {"description": "relier le ROE aux capitaux propres"},
        "polarity": "supportive",
        "evidence_strength": "medium",
        "local_stage": "comprehension",
        "contradiction_scope": None,
        "error_type": None,
        "observation_text": "Explique correctement ce que mesure le ROE.",
        "capability_localization": "localized",
    }
    kwargs.update(overrides)
    return kwargs


def _contradictory(**overrides):
    return _obs_kwargs(**{"polarity": "contradictory", "local_stage": None,
                          "contradiction_scope": "comprehension", "error_type": "conceptual", **overrides})


def _add(db, run_id, **overrides):
    return svc.add_observation(db, run_id=run_id, **_obs_kwargs(**overrides))


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_no_migration_added_by_t3b_head_is_0009():
    """T3-B n'a ajouté aucune migration ; les seules ajoutées depuis sont
    0007 (T4-A), 0008 (T5-A) et 0009 (T6-A), qui est la tête."""
    script = _script_directory()
    assert script.get_heads() == [T6A]
    revisions = (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A, T6A)
    assert {rev.revision for rev in script.walk_revisions()} == set(revisions)
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [f"{rev}.py" for rev in revisions]


def test_public_api_is_exactly_the_seven_operations():
    """Pas d'activate/supersede séparés, pas d'update/delete d'observation,
    pas de get_or_create, pas d'inférence."""
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == PUBLIC_API
    for name in PUBLIC_API:
        params = list(inspect.signature(getattr(svc, name)).parameters.values())
        assert params[0].name == "db" and params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:]), name
    for forbidden in ("activate_run", "supersede_run", "update_observation", "delete_observation",
                      "get_or_create_run", "find_last_open_run", "get_user_level",
                      "calculate_confidence", "infer_stage", "revalidate_observation"):
        assert not hasattr(svc, forbidden), forbidden


def test_exact_signatures():
    def params(fn):
        return {name: p.default for name, p in inspect.signature(fn).parameters.items() if name != "db"}

    empty = inspect.Parameter.empty
    assert params(svc.start_evaluation_run) == {
        **{name: empty for name in ["event_id"] + START_REQUIRED_TEXT},
        "pedagogical_taxonomy_release_id": None,
        "model_id": None,
        "prompt_spec_version": None,
        "re_evaluates_run_id": None,
    }
    # Aucun champ d'observation n'a de valeur par défaut : l'évaluateur
    # doit tout expliciter, y compris les None.
    assert params(svc.add_observation) == {name: empty for name in ["run_id"] + list(_obs_kwargs())}
    assert params(svc.complete_evaluation_run) == {"run_id": empty, "output_fingerprint": empty}
    assert params(svc.fail_evaluation_run) == {"run_id": empty, "failure_code": empty}
    assert params(svc.invalidate_observation) == {"observation_id": empty, "reason": empty}
    assert params(svc.get_evaluation_run) == {"run_id": empty}
    assert params(svc.get_observations) == {"run_id": empty}


def test_exceptions_are_a_small_business_hierarchy():
    public = {obj for obj in vars(svc).values()
              if isinstance(obj, type) and issubclass(obj, Exception) and obj.__module__ == svc.__name__}
    assert public == EXCEPTIONS | {ObservationServiceError}
    for exc in EXCEPTIONS:
        assert exc.__bases__ == (ObservationServiceError,), exc
    assert ObservationServiceError.__bases__ == (Exception,)
    assert not issubclass(ObservationServiceError, (LookupError, ValueError))


def test_service_owns_no_transaction_and_has_no_framework_or_llm_dependency():
    source = SERVICE_PATH.read_text(encoding="utf-8")
    tokens = set(_code_tokens(source).split("\n"))
    for forbidden in ("commit", "rollback", "begin", "close", "SessionLocal", "get_db", "HTTPException",
                      "utcnow", "delete", "update", "merge", "expunge", "execute_raw", "text"):
        assert forbidden not in tokens, forbidden
    # Seul savepoint : l'INSERT d'un run (traduction du conflit de dédup).
    assert _code_tokens(source).split("\n").count("begin_nested") == 1

    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    # core.taxonomy_service (T4-B) : seul le contrôle interne des mappings
    # avant complétion (import unidirectionnel, voir
    # test_service_delegates_the_t4_check_to_the_taxonomy_service).
    assert imported == {"math", "uuid", "datetime", "sqlalchemy", "sqlalchemy.exc", "core.models",
                        "core.taxonomy_service"}


def test_service_contains_no_inference_or_evaluator_vocabulary():
    """T3-B persiste une interprétation locale déjà produite : aucune
    notion de score, confiance, maîtrise, progression, priorité, ni appel à
    un modèle (docstrings et commentaires exclus)."""
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).lower()
    for word in ("score", "confidence", "mastery", "progress", "infer", "priorit", "coverage",
                 "transfer", "revalid", "xp_", "user_level", "user_id", "anthropic", "openai",
                 "claude", "llm", "requests", "http", "weight", "coefficient"):
        assert word not in tokens, word


def test_service_is_not_wired_to_the_application():
    """Aucune route ni module applicatif n'importe le service (seuls les
    tests l'utilisent) : l'application produit toujours zéro run."""
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
        if rel != "core/observation_service.py":
            assert "observation_service" not in source, rel
    assert checked > 0


def test_service_does_not_touch_the_t4_taxonomy():
    """Le service T3-B ne lit ni n'écrit lui-même aucune table de
    taxonomie : il transmet pedagogical_taxonomy_release_id et, depuis
    T4-B, délègue la cohérence localized / competency_only au contrôle
    interne de core/taxonomy_service."""
    tokens = set(_code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).split("\n"))
    for name in (*T4A_TABLES, *(model.__name__ for model in T4A_MODELS)):
        assert name not in tokens, name


def test_service_delegates_the_t4_check_to_the_taxonomy_service():
    """T4-B : un seul point de contact, le contrôle interne importé de
    core.taxonomy_service, appelé une seule fois (complete_evaluation_run),
    entre le verrou du run et celui de l'événement. Pas de cycle d'import :
    taxonomy_service n'importe pas ce module."""
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "core.taxonomy_service"]
    assert [[a.name for a in n.names] for n in imports] == [["_validate_run_capability_mappings"]]
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_validate_run_capability_mappings"]
    assert len(calls) == 1
    complete = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "complete_evaluation_run")
    called = [n.func.id for n in ast.walk(complete) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert (called.index("_lock_open_run") < called.index("_validate_run_capability_mappings")
            < called.index("_lock_event"))
    taxonomy = ast.parse((REPO_ROOT / "core" / "taxonomy_service.py").read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(taxonomy) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(taxonomy) if isinstance(n, ast.Import) for a in n.names}
    assert not any("observation_service" in (m or "") for m in modules)


def test_vocabularies_match_the_0006_check_constraints():
    for column, allowed in CLOSED_VOCABULARIES.items():
        assert allowed == frozenset(OBS_VOCABULARIES[column]), column
    assert {svc.VALID, svc.INVALIDATED} == set(OBS_VOCABULARIES["integrity_status"])
    assert {svc.RUNNING, svc.COMPLETED, svc.FAILED} == set(RUN_VOCABULARIES["execution_status"])
    assert {svc.CANDIDATE, svc.ACTIVE, svc.SUPERSEDED, svc.OBSOLETE} == set(
        RUN_VOCABULARIES["interpretation_status"])
    assert "mastery" not in svc.LOCAL_STAGES
    assert svc.FINALIZED == cc.FINALIZED


def test_utcnow_is_utc_aware():
    now = svc._utcnow()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


# --- start_evaluation_run : validation -------------------------------------

def test_lifecycle_fields_cannot_be_supplied():
    for field in ("execution_status", "interpretation_status", "started_at", "completed_at",
                  "created_at", "failure_code", "output_fingerprint", "id"):
        with pytest.raises(TypeError):
            svc.start_evaluation_run(_NoDB(), **_start_kwargs(**{field: None}))
    for field in ("id", "ordinal", "integrity_status", "invalidated_at", "invalidation_reason",
                  "created_at", "evaluation_run_id"):
        with pytest.raises(TypeError):
            svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **_obs_kwargs(**{field: None}))


def test_valid_start_arguments_reach_the_database():
    _reaches_db(lambda db: svc.start_evaluation_run(db, **_start_kwargs()))
    _reaches_db(lambda db: svc.start_evaluation_run(db, **_start_kwargs(
        pedagogical_taxonomy_release_id=uuid.uuid4(), model_id="model-x", prompt_spec_version="p-1",
        re_evaluates_run_id=uuid.uuid4())))


@pytest.mark.parametrize("field", START_REQUIRED_TEXT)
@pytest.mark.parametrize("value", ["", " ", "\t\n", None, 42, b"x", "a\x00b"])
def test_start_rejects_invalid_required_strings(field, value):
    with pytest.raises(InvalidObservationPayload, match=field):
        svc.start_evaluation_run(_NoDB(), **_start_kwargs(**{field: value}))


@pytest.mark.parametrize("field", ["event_id", "pedagogical_taxonomy_release_id", "re_evaluates_run_id"])
@pytest.mark.parametrize("value", ["", str(uuid.uuid4()), 1, uuid.uuid4().bytes])
def test_start_rejects_invalid_uuids(field, value):
    with pytest.raises(InvalidObservationPayload, match=field):
        svc.start_evaluation_run(_NoDB(), **_start_kwargs(**{field: value}))


def test_start_requires_event_id():
    with pytest.raises(InvalidObservationPayload, match="event_id"):
        svc.start_evaluation_run(_NoDB(), **{**_start_kwargs(), "event_id": None})


@pytest.mark.parametrize("field", ["model_id", "prompt_spec_version"])
@pytest.mark.parametrize("value", ["", "  ", 3])
def test_start_rejects_invalid_optional_strings(field, value):
    with pytest.raises(InvalidObservationPayload, match=field):
        svc.start_evaluation_run(_NoDB(), **_start_kwargs(**{field: value}))


# --- add_observation : validation ------------------------------------------

@pytest.mark.parametrize("column", sorted(CLOSED_VOCABULARIES))
def test_every_vocabulary_value_passes_validation(column):
    for value in sorted(CLOSED_VOCABULARIES[column]):
        if column == "polarity":
            kwargs = _obs_kwargs() if value == "supportive" else _contradictory()
        elif column == "contradiction_scope":
            kwargs = _contradictory(contradiction_scope=value)
        else:
            kwargs = _obs_kwargs(**{column: value})
        _reaches_db(lambda db: svc.add_observation(db, run_id=uuid.uuid4(), **kwargs))


@pytest.mark.parametrize("column", sorted(CLOSED_VOCABULARIES))
@pytest.mark.parametrize("value", ["", "inconnu", "C13", "c7", "Primary", " primary", "strong ",
                                   "mastery", "maîtrise", "mixed", 7, b"none"])
def test_unknown_vocabulary_values_are_refused_never_mapped(column, value):
    base = _contradictory() if column == "contradiction_scope" else _obs_kwargs()
    with pytest.raises(InvalidObservationPayload, match=column):
        svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **{**base, column: value})


@pytest.mark.parametrize("column", ["competency_code", "observation_role", "elicitation_mode",
                                    "support_level", "polarity", "evidence_strength",
                                    "capability_localization"])
def test_required_vocabularies_reject_none(column):
    with pytest.raises(InvalidObservationPayload, match=column):
        svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **_obs_kwargs(**{column: None}))


def test_str_enum_is_refused_not_converted():
    with pytest.raises(InvalidObservationPayload, match="local_stage"):
        svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **_obs_kwargs(local_stage=_Stage.APPLICATION))


def test_mastery_is_never_a_local_stage():
    for value in ("mastery", "Mastery", "MASTERY"):
        with pytest.raises(InvalidObservationPayload, match="local_stage"):
            svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **_obs_kwargs(local_stage=value))


@pytest.mark.parametrize("kwargs", [
    _obs_kwargs(local_stage="none"),
    _obs_kwargs(local_stage="discovery"),
    _obs_kwargs(local_stage="application", evidence_strength="strong"),
    _obs_kwargs(local_stage="none", evidence_strength="strong", support_level="answer_given"),
    _contradictory(),
    _contradictory(contradiction_scope="undetermined", error_type=None),
    _contradictory(evidence_strength="strong", contradiction_scope="application"),
    _contradictory(evidence_strength="weak", contradiction_scope="recognition", error_type="factual_premise"),
], ids=["supportive-none", "supportive-discovery", "supportive-application-strong",
        "supportive-none-strong-answer_given", "contradictory", "contradictory-undetermined",
        "contradictory-strong", "contradictory-weak"])
def test_valid_polarity_combinations(kwargs):
    _reaches_db(lambda db: svc.add_observation(db, run_id=uuid.uuid4(), **kwargs))


@pytest.mark.parametrize("kwargs, match", [
    (_obs_kwargs(local_stage=None), "local_stage obligatoire"),
    (_obs_kwargs(contradiction_scope="comprehension"), "contradiction_scope doit être None"),
    (_obs_kwargs(local_stage=None, contradiction_scope="comprehension"), "local_stage obligatoire"),
    (_contradictory(local_stage="comprehension"), "local_stage doit être None"),
    (_contradictory(local_stage="none"), "local_stage doit être None"),
    (_contradictory(contradiction_scope=None), "contradiction_scope obligatoire"),
], ids=["supportive-without-stage", "supportive-with-scope", "supportive-scope-only",
        "contradictory-with-stage", "contradictory-with-stage-none", "contradictory-without-scope"])
def test_invalid_polarity_combinations(kwargs, match):
    with pytest.raises(InvalidObservationPayload, match=match):
        svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **kwargs)


@pytest.mark.parametrize("value", ["", "  ", 5, "a\x00"])
def test_task_kind_is_none_or_non_empty_text(value):
    with pytest.raises(InvalidObservationPayload, match="task_kind"):
        svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **_obs_kwargs(task_kind=value))
    for extensible in (None, "interpret_metric", "tache_future_42"):
        _reaches_db(lambda db: svc.add_observation(db, run_id=uuid.uuid4(), **_obs_kwargs(task_kind=extensible)))


@pytest.mark.parametrize("value", ["", " \n", None, 1, "a\x00b"])
def test_observation_text_is_required(value):
    with pytest.raises(InvalidObservationPayload, match="observation_text"):
        svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **_obs_kwargs(observation_text=value))


@pytest.mark.parametrize("field, root", sorted(JSON_ROOTS.items()))
def test_json_root_types_are_enforced(field, root):
    wrong = [None, "texte", 42, 1.5, True, ({"a": 1},), OrderedDict() if root is list else (), set()]
    wrong.append([] if root is dict else {})
    for value in wrong:
        with pytest.raises(InvalidObservationPayload, match=field):
            svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **_obs_kwargs(**{field: value}))
    _reaches_db(lambda db: svc.add_observation(db, run_id=uuid.uuid4(), **_obs_kwargs(**{field: root()})))


@pytest.mark.parametrize("field, root", sorted(JSON_ROOTS.items()))
@pytest.mark.parametrize("value", INVALID_JSON_VALUES, ids=range(len(INVALID_JSON_VALUES)))
def test_json_payloads_must_be_strictly_json_compatible(field, root, value):
    with pytest.raises(InvalidObservationPayload, match=field):
        svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **_obs_kwargs(**{field: _as_root(value, root)}))


@pytest.mark.parametrize("field, root", sorted(JSON_ROOTS.items()))
def test_json_payloads_reject_circular_references(field, root):
    cyclic = {"a": []}
    cyclic["a"].append(cyclic)
    with pytest.raises(InvalidObservationPayload, match="circulaire"):
        svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **_obs_kwargs(**{field: _as_root(cyclic, root)}))
    looping = []
    looping.append(looping)
    with pytest.raises(InvalidObservationPayload, match="circulaire"):
        svc.add_observation(_NoDB(), run_id=uuid.uuid4(), **_obs_kwargs(**{field: _as_root(looping, root)}))


def test_json_copy_accepts_json_values_and_returns_independent_copy():
    shared = {"k": 1}
    payload = OrderedDict(
        s="é", i=-3, big=10 ** 30, f=0.5, t=True, n=None, e={}, l=[], nested=[{"a": [1, "b", None]}],
        shared_twice=[shared, shared],
    )
    copy = svc._json_root(payload, "p", dict)
    assert copy == payload and type(copy) is dict
    assert copy["nested"] is not payload["nested"]
    assert copy["nested"][0] is not payload["nested"][0]
    payload["nested"][0]["a"].append("mutation")
    shared["k"] = 2
    assert copy["nested"] == [{"a": [1, "b", None]}]
    assert copy["shared_twice"] == [{"k": 1}, {"k": 1}]
    listed = [{"x": [1]}]
    listed_copy = svc._json_root(listed, "l", list)
    listed[0]["x"].append(2)
    assert listed_copy == [{"x": [1]}] and type(listed_copy) is list


# --- autres opérations : validation ----------------------------------------

UUID_CALLS = {
    "add_observation": lambda db, v: svc.add_observation(db, run_id=v, **_obs_kwargs()),
    "complete_evaluation_run": lambda db, v: svc.complete_evaluation_run(db, run_id=v, output_fingerprint="o"),
    "fail_evaluation_run": lambda db, v: svc.fail_evaluation_run(db, run_id=v, failure_code="f"),
    "invalidate_observation": lambda db, v: svc.invalidate_observation(db, observation_id=v, reason="r"),
    "get_evaluation_run": lambda db, v: svc.get_evaluation_run(db, run_id=v),
    "get_observations": lambda db, v: svc.get_observations(db, run_id=v),
}


@pytest.mark.parametrize("name", sorted(UUID_CALLS))
@pytest.mark.parametrize("value", [None, "", str(uuid.uuid4()), 1])
def test_identifiers_must_be_uuids(name, value):
    with pytest.raises(InvalidObservationPayload, match="_id"):
        UUID_CALLS[name](_NoDB(), value)


@pytest.mark.parametrize("value", ["", "   ", None, 0, "a\x00"])
def test_terminal_transitions_and_invalidation_require_non_empty_text(value):
    with pytest.raises(InvalidObservationPayload, match="output_fingerprint"):
        svc.complete_evaluation_run(_NoDB(), run_id=uuid.uuid4(), output_fingerprint=value)
    with pytest.raises(InvalidObservationPayload, match="failure_code"):
        svc.fail_evaluation_run(_NoDB(), run_id=uuid.uuid4(), failure_code=value)
    with pytest.raises(InvalidObservationPayload, match="reason"):
        svc.invalidate_observation(_NoDB(), observation_id=uuid.uuid4(), reason=value)


# --- requêtes de verrouillage ----------------------------------------------

class _RecordingDB:
    """Enregistre la première requête puis simule une ligne absente."""

    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(" ".join(str(statement.compile(dialect=postgresql.dialect())).split()))
        return SimpleNamespace(scalar_one_or_none=lambda: None)


@pytest.mark.parametrize("name, exc, table, mode", [
    ("start_evaluation_run", EventNotFound, "cognitive_events", "FOR UPDATE"),
    ("add_observation", EvaluationRunNotFound, "observation_evaluation_runs", "FOR NO KEY UPDATE"),
    ("complete_evaluation_run", EvaluationRunNotFound, "observation_evaluation_runs", "FOR NO KEY UPDATE"),
    ("fail_evaluation_run", EvaluationRunNotFound, "observation_evaluation_runs", "FOR NO KEY UPDATE"),
    # FOR NO KEY UPDATE depuis T4-B (compatible avec le FOR KEY SHARE de la
    # FK observation_capabilities -> pedagogical_observations).
    ("invalidate_observation", ObservationNotFound, "pedagogical_observations", "FOR NO KEY UPDATE"),
])
def test_first_query_of_each_mutation_is_a_row_lock(name, exc, table, mode):
    calls = {**UUID_CALLS, "start_evaluation_run": lambda db, v: svc.start_evaluation_run(
        db, **_start_kwargs(event_id=v))}
    db = _RecordingDB()
    with pytest.raises(exc):
        calls[name](db, uuid.uuid4())
    assert len(db.statements) == 1
    sql = db.statements[0]
    assert sql.startswith(f"SELECT {table}.id")
    assert f"FROM {table} WHERE {table}.id = " in sql
    assert sql.endswith(mode)


def test_dedup_violation_detection_is_targeted():
    def error(constraint):
        orig = SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint))
        return sa.exc.IntegrityError("INSERT", {}, orig)

    assert svc._is_dedup_violation(error(DEDUP_INDEX))
    for other in (ACTIVE_INDEX, "ck_observation_evaluation_runs_execution_status",
                  "observation_evaluation_runs_event_id_fkey", "observation_evaluation_runs_pkey", None):
        assert not svc._is_dedup_violation(error(other)), other
    assert not svc._is_dedup_violation(sa.exc.IntegrityError("INSERT", {}, Exception(DEDUP_INDEX)))


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine(pg_url):  # noqa: F811
    """Schéma = head (0009) + deux utilisateurs, créé une fois pour le
    module ; chaque test nettoie ce qu'il a créé."""
    eng = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    _upgrade_head_with_users(pg_url, eng)
    yield eng
    eng.dispose()


@pytest.fixture
def Sessions(engine):
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)  # = core.db.SessionLocal
    yield factory
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM pedagogical_observations"))
        conn.execute(sa.text("DELETE FROM observation_evaluation_runs"))
        conn.execute(sa.text("DELETE FROM pedagogical_taxonomy_releases"))
        conn.execute(sa.text("DELETE FROM support_traces"))
        conn.execute(sa.text("DELETE FROM cognitive_events"))
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
    """Horloge déterministe du service : chaque appel à _utcnow avance d'une
    seconde. Deux timestamps égaux prouvent un unique `now` par opération."""
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    ticks = itertools.count()
    monkeypatch.setattr(svc, "_utcnow", lambda: base + timedelta(seconds=next(ticks)))
    return base


def _event(Sessions, close="finalize_event", user_id=USER) -> uuid.UUID:
    """CognitiveEvent commité via le service T2-B (finalized par défaut)."""
    with Sessions() as session:
        event = cc.open_event(session, user_id=user_id, event_origin="education",
                              task_kind="interpret_metric", stimulus_snapshot={"question": "ROE ?"})
        cc.append_user_work(session, event_id=event.id, work={"text": "rentabilité des capitaux propres"})
        if close:
            getattr(cc, close)(session, event_id=event.id)
        session.commit()
        return event.id


def _committed_run(Sessions, event_id, *, observations=0, end=None, **overrides) -> uuid.UUID:
    """Run commité (en tant qu'appelant) : start, n observations, puis
    éventuellement complete / fail."""
    with Sessions() as session:
        run = svc.start_evaluation_run(session, **_start_kwargs(event_id, **overrides))
        for index in range(observations):
            _add(session, run.id, observation_text=f"observation {index + 1}")
        if end == "complete":
            svc.complete_evaluation_run(session, run_id=run.id, output_fingerprint="sha256:out")
        elif end == "fail":
            svc.fail_evaluation_run(session, run_id=run.id, failure_code="evaluator_error")
        session.commit()
        return run.id


def _run_row(engine, run_id):
    """État commité, lu par une connexion indépendante."""
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT * FROM observation_evaluation_runs WHERE id = :id"),
                           {"id": run_id}).mappings().one_or_none()
        return dict(row) if row else None


def _obs_rows(engine, run_id):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(
            "SELECT * FROM pedagogical_observations WHERE evaluation_run_id = :id ORDER BY ordinal"
        ), {"id": run_id}).mappings()]


def _states(engine, event_id):
    with engine.connect() as conn:
        return {r.id: (r.execution_status, r.interpretation_status) for r in conn.execute(sa.text(
            "SELECT id, execution_status, interpretation_status FROM observation_evaluation_runs "
            "WHERE event_id = :e"), {"e": event_id})}


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def _assert_utc(value):
    assert value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _recording(engine):
    statements = []

    def record(conn, cursor, statement, parameters, *args):
        statements.append((" ".join(statement.split()), parameters))

    return statements, record


def _blocked(engine, holder, waiter, action, *, waiting_on):
    """`holder` détient déjà ses verrous (transaction ouverte). `action(waiter)`
    est lancée dans un thread ; une fois qu'elle est réellement bloquée par
    `holder` — sur une requête commençant par `waiting_on`
    (pg_stat_activity ; le texte y est tronqué, le mode de verrou est
    vérifié par ailleurs) — holder commite. Retourne (résultat, exception) de
    l'action, dont la transaction est commitée si elle a réussi."""
    holder_pid, waiter_pid = _backend_pid(holder), _backend_pid(waiter)
    outcome = {}

    def work():
        try:
            outcome["result"] = action(waiter)
            waiter.commit()
        except Exception as exc:  # noqa: BLE001 — restitué au test
            outcome["error"] = exc
            waiter.rollback()

    thread = threading.Thread(target=work)
    thread.start()
    try:
        _wait_until_blocked_by(engine, waiter_pid, holder_pid)
        with engine.connect() as conn:
            query = " ".join(conn.execute(sa.text("SELECT query FROM pg_stat_activity WHERE pid = :p"),
                                          {"p": waiter_pid}).scalar_one().split())
        assert query.startswith(waiting_on), (waiting_on, query)
        assert "result" not in outcome and "error" not in outcome
        holder.commit()
    finally:
        if thread.is_alive() and holder.in_transaction():
            holder.rollback()
        thread.join(timeout=10)
    assert not thread.is_alive()
    return outcome.get("result"), outcome.get("error")


# Requête bloquée (préfixe). Les requêtes de verrouillage (FOR UPDATE /
# FOR NO KEY UPDATE, par id) sont vérifiées par LOCK_SQL.
ON_EVENT = "SELECT cognitive_events.id,"
ON_RUN = "SELECT observation_evaluation_runs.id,"
ON_OBSERVATION = "SELECT pedagogical_observations.id,"
LOCK_SQL = {
    "event": ("FROM cognitive_events WHERE cognitive_events.id =", "FOR UPDATE"),
    "run": ("FROM observation_evaluation_runs WHERE observation_evaluation_runs.id =", "FOR NO KEY UPDATE"),
}


# --- A. start_evaluation_run ------------------------------------------------

def test_pg_start_on_finalized_event_creates_running_candidate(engine, Sessions, db, clock):
    event_id = _event(Sessions)
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id, evaluation_dedup_key="event:v1"))
    assert isinstance(run, ObservationEvaluationRun) and isinstance(run.id, uuid.UUID)
    assert (run.event_id, run.execution_status, run.interpretation_status) == (event_id, "running", "candidate")
    assert run.started_at == run.created_at == clock
    assert run.completed_at is None and run.failure_code is None and run.output_fingerprint is None
    assert run.re_evaluates_run_id is None and run.pedagogical_taxonomy_release_id is None
    assert run.model_id is None and run.prompt_spec_version is None
    assert (run.trigger, run.evaluation_dedup_key, run.normalization_version, run.local_stage_version,
            run.capability_mapping_version, run.evaluation_schema_version, run.evaluator_version,
            run.input_fingerprint) == ("event_finalized", "event:v1", "norm-1", "stage-1", "map-1",
                                       "schema-1", "evaluator-1", "sha256:in")
    # Flushé (visible dans la transaction de l'appelant), pas commité.
    assert db.execute(sa.text("SELECT execution_status FROM observation_evaluation_runs WHERE id = :id"),
                      {"id": run.id}).scalar_one() == "running"
    assert _run_row(engine, run.id) is None
    db.commit()
    stored = _run_row(engine, run.id)
    assert (stored["execution_status"], stored["interpretation_status"]) == ("running", "candidate")
    assert stored["started_at"] == stored["created_at"] == clock
    assert svc.get_evaluation_run(db, run_id=run.id) is run


def _release(engine) -> uuid.UUID:
    """Release de taxonomie T4 minimale, créée par le test (aucune n'est
    seedée par la migration)."""
    release_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO pedagogical_taxonomy_releases (id, version_key, status, spec_fingerprint, created_at) "
            "VALUES (:id, :k, 'candidate', :f, now())"
        ), {"id": release_id, "k": f"test-{release_id}", "f": f"sha256:{release_id}"})
    return release_id


def test_pg_start_real_clock_and_optional_provenance(engine, Sessions, db):
    event_id = _event(Sessions)
    release = _release(engine)
    run = svc.start_evaluation_run(db, **_start_kwargs(
        event_id, trigger="manual_replay", pedagogical_taxonomy_release_id=release,
        model_id="model-2026-09", prompt_spec_version="prompt-3"))
    db.commit()
    stored = _run_row(engine, run.id)
    for value in (run.started_at, run.created_at, stored["started_at"], stored["created_at"]):
        _assert_utc(value)
    assert stored["started_at"] == stored["created_at"]
    assert (stored["trigger"], stored["pedagogical_taxonomy_release_id"], stored["model_id"],
            stored["prompt_spec_version"]) == ("manual_replay", release, "model-2026-09", "prompt-3")


def test_pg_start_with_unknown_taxonomy_release_is_refused_by_the_t4_fk(engine, Sessions, db):
    """Depuis T4-A, pedagogical_taxonomy_release_id est une vraie FK : une
    release inexistante est refusée par PostgreSQL. Le service (inchangé)
    propage l'IntegrityError telle quelle (ce n'est pas un conflit de
    dédup) après retour au savepoint : la transaction de l'appelant reste
    utilisable."""
    event_id = _event(Sessions)
    with pytest.raises(sa.exc.IntegrityError, match=RELEASE_FK):
        svc.start_evaluation_run(db, **_start_kwargs(event_id, pedagogical_taxonomy_release_id=uuid.uuid4()))
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id))
    db.commit()
    assert _count(engine, "observation_evaluation_runs") == 1
    assert _run_row(engine, run.id)["pedagogical_taxonomy_release_id"] is None


def test_pg_start_keeps_strings_exactly_as_provided(engine, Sessions, db):
    """Validé mais jamais normalisé (pas de strip)."""
    event_id = _event(Sessions)
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id, trigger=" Trigger ", evaluator_version="v1\n"))
    db.commit()
    stored = _run_row(engine, run.id)
    assert (stored["trigger"], stored["evaluator_version"]) == (" Trigger ", "v1\n")


@pytest.mark.parametrize("close, status", [(None, "open"), ("abandon_event", "abandoned")])
def test_pg_start_refuses_non_finalized_events(engine, Sessions, db, close, status):
    event_id = _event(Sessions, close=close)
    with pytest.raises(EventNotEvaluable, match=status):
        svc.start_evaluation_run(db, **_start_kwargs(event_id))
    db.commit()
    assert _count(engine, "observation_evaluation_runs") == 0


def test_pg_start_unknown_event(db):
    with pytest.raises(EventNotFound):
        svc.start_evaluation_run(db, **_start_kwargs(uuid.uuid4()))


def test_pg_start_does_not_modify_the_event(engine, Sessions, db):
    event_id = _event(Sessions)
    with engine.connect() as conn:
        before = conn.execute(sa.text("SELECT * FROM cognitive_events WHERE id = :id"), {"id": event_id}).one()
    svc.start_evaluation_run(db, **_start_kwargs(event_id))
    db.commit()
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT * FROM cognitive_events WHERE id = :id"),
                            {"id": event_id}).one() == before


def test_pg_start_reloads_a_stale_event_under_the_lock(Sessions, db):
    """L'événement chargé open dans la Session est rechargé sous le verrou :
    une finalisation commitée entre-temps est vue."""
    event_id = _event(Sessions, close=None)
    stale = cc.get_event(db, event_id=event_id)
    assert stale.status == "open"
    with Sessions() as other:
        cc.finalize_event(other, event_id=event_id)
        other.commit()
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id))
    assert stale.status == "finalized" and run.execution_status == "running"


def test_pg_start_waits_for_a_concurrent_finalization(engine, Sessions):
    """start attend le verrou d'une finalisation T2-B en cours puis voit
    l'événement finalized."""
    event_id = _event(Sessions, close=None)
    with Sessions() as closer, Sessions() as evaluator:
        cc.finalize_event(closer, event_id=event_id)
        result, error = _blocked(engine, closer, evaluator,
                                 lambda s: svc.start_evaluation_run(s, **_start_kwargs(event_id)).execution_status,
                                 waiting_on=ON_EVENT)
    assert error is None and result == "running"


# --- B. réévaluation --------------------------------------------------------

def test_pg_reevaluation_of_the_same_event_is_accepted_and_leaves_target_untouched(engine, Sessions, db):
    event_id = _event(Sessions)
    active = _committed_run(Sessions, event_id, observations=1, end="complete")
    failed = _committed_run(Sessions, event_id, end="fail")
    before_active, before_failed = _run_row(engine, active), _run_row(engine, failed)
    obs_before = _obs_rows(engine, active)

    first = svc.start_evaluation_run(db, **_start_kwargs(event_id, trigger="evaluator_upgrade",
                                                         re_evaluates_run_id=active))
    # L'historique (pas seulement l'active) reste réévaluable.
    second = svc.start_evaluation_run(db, **_start_kwargs(event_id, re_evaluates_run_id=failed))
    db.commit()
    assert _run_row(engine, first.id)["re_evaluates_run_id"] == active
    assert _run_row(engine, second.id)["re_evaluates_run_id"] == failed
    assert _run_row(engine, active) == before_active
    assert _run_row(engine, failed) == before_failed
    assert _obs_rows(engine, active) == obs_before
    assert before_active["interpretation_status"] == "active"


def test_pg_reevaluation_of_a_superseded_run_is_accepted(engine, Sessions, db):
    event_id = _event(Sessions)
    old = _committed_run(Sessions, event_id, end="complete")
    _committed_run(Sessions, event_id, end="complete", re_evaluates_run_id=old)
    assert _run_row(engine, old)["interpretation_status"] == "superseded"
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id, re_evaluates_run_id=old))
    assert run.re_evaluates_run_id == old


def test_pg_reevaluation_of_another_event_is_refused(engine, Sessions, db):
    event_id, other_event = _event(Sessions), _event(Sessions, user_id=OTHER_USER)
    foreign = _committed_run(Sessions, other_event, end="complete")
    with pytest.raises(InvalidReevaluationTarget, match=str(other_event)):
        svc.start_evaluation_run(db, **_start_kwargs(event_id, re_evaluates_run_id=foreign))
    db.commit()
    assert set(_states(engine, event_id)) == set()


def test_pg_reevaluation_of_an_unknown_run_is_refused(engine, Sessions, db):
    event_id = _event(Sessions)
    with pytest.raises(EvaluationRunNotFound):
        svc.start_evaluation_run(db, **_start_kwargs(event_id, re_evaluates_run_id=uuid.uuid4()))
    db.commit()
    assert _count(engine, "observation_evaluation_runs") == 0


# --- C. déduplication -------------------------------------------------------

def test_pg_duplicate_dedup_key_is_a_business_error_never_the_old_run(engine, Sessions, db):
    event_id, other_event = _event(Sessions), _event(Sessions)
    existing = _committed_run(Sessions, event_id, evaluation_dedup_key="event:v1", end="fail")
    before = _run_row(engine, existing)
    for target in (event_id, other_event):
        with pytest.raises(DuplicateEvaluationRun, match="event:v1"):
            svc.start_evaluation_run(db, **_start_kwargs(target, evaluation_dedup_key="event:v1"))
    # La transaction de l'appelant reste utilisable après le refus.
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id, evaluation_dedup_key="event:v2"))
    db.commit()
    assert _run_row(engine, existing) == before
    assert set(_states(engine, event_id)) == {existing, run.id}
    assert _states(engine, other_event) == {}


def test_pg_dedup_race_on_the_same_event_is_serialized_by_the_event_lock(engine, Sessions):
    """Deux starts du même événement avec la même clé : le second attend le
    verrou de l'événement, puis la pré-vérification voit le premier."""
    event_id = _event(Sessions)
    with Sessions() as first, Sessions() as second:
        run_id = svc.start_evaluation_run(first, **_start_kwargs(event_id, evaluation_dedup_key="k")).id
        _, error = _blocked(engine, first, second, lambda s: svc.start_evaluation_run(
            s, **_start_kwargs(event_id, evaluation_dedup_key="k")), waiting_on=ON_EVENT)
    assert isinstance(error, DuplicateEvaluationRun)
    assert set(_states(engine, event_id)) == {run_id}


def test_pg_dedup_race_across_events_is_caught_by_the_unique_constraint(engine, Sessions):
    """Race résiduelle : même clé pour deux événements différents (verrous
    parents distincts). La pré-vérification ne voit pas l'INSERT non commité
    du premier ; le second attend sur l'UNIQUE puis la violation de CETTE
    contrainte devient DuplicateEvaluationRun, sans rendre inutilisable la
    transaction de l'appelant (savepoint)."""
    first_event, second_event = _event(Sessions), _event(Sessions)

    def racing(session):
        session.add(User(id="pending-user", level="debutant"))
        session.flush()
        try:
            svc.start_evaluation_run(session, **_start_kwargs(second_event, evaluation_dedup_key="shared"))
        except DuplicateEvaluationRun as exc:
            assert isinstance(exc.__cause__, sa.exc.IntegrityError)
            assert DEDUP_INDEX in str(exc.__cause__)
            return "duplicate"
        return "created"

    with Sessions() as first, Sessions() as second:
        run_id = svc.start_evaluation_run(first, **_start_kwargs(first_event, evaluation_dedup_key="shared")).id
        result, error = _blocked(engine, first, second, racing,
                                 waiting_on="INSERT INTO observation_evaluation_runs")
    assert error is None and result == "duplicate"
    assert set(_states(engine, first_event)) == {run_id}
    assert _states(engine, second_event) == {}
    with engine.connect() as conn:
        # L'écriture de l'appelant antérieure au conflit a bien été commitée.
        assert conn.execute(sa.text("SELECT count(*) FROM users WHERE id = 'pending-user'")).scalar_one() == 1


def test_pg_other_integrity_errors_are_not_masked(engine, Sessions, db, monkeypatch):
    """Seule la contrainte de dédup est traduite : une autre violation (ici
    un CHECK, provoqué en corrompant la constante du service) remonte
    telle quelle, et la transaction de l'appelant reste utilisable."""
    event_id = _event(Sessions)
    monkeypatch.setattr(svc, "RUNNING", "bogus")
    with pytest.raises(sa.exc.IntegrityError, match="ck_observation_evaluation_runs_execution_status") as info:
        svc.start_evaluation_run(db, **_start_kwargs(event_id))
    assert not isinstance(info.value, ObservationServiceError)
    monkeypatch.undo()
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id))
    db.commit()
    assert set(_states(engine, event_id)) == {run.id}


# --- D. add_observation -----------------------------------------------------

def test_pg_add_observation_on_running_candidate(engine, Sessions, db, clock):
    event_id = _event(Sessions)
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id))
    other = svc.start_evaluation_run(db, **_start_kwargs(event_id))
    observations = [
        _add(db, run.id, competency_code="C7"),
        svc.add_observation(db, run_id=run.id, **_contradictory(competency_code="C7")),
        _add(db, run.id, competency_code="C2", observation_role="secondary", task_kind=None),
    ]
    for ordinal, observation in enumerate(observations, start=1):
        assert isinstance(observation, PedagogicalObservation) and isinstance(observation.id, uuid.UUID)
        assert (observation.evaluation_run_id, observation.ordinal) == (run.id, ordinal)
        assert observation.integrity_status == "valid"
        assert observation.invalidated_at is None and observation.invalidation_reason is None
        _assert_utc(observation.created_at)
    # Ordinals propres à chaque run.
    assert _add(db, other.id).ordinal == 1
    # Le run lui-même n'est pas modifié par un ajout.
    assert (run.execution_status, run.interpretation_status, run.completed_at) == ("running", "candidate", None)
    db.commit()
    stored = _obs_rows(engine, run.id)
    assert [(o["ordinal"], o["competency_code"], o["polarity"]) for o in stored] == [
        (1, "C7", "supportive"), (2, "C7", "contradictory"), (3, "C2", "supportive")]
    first = stored[0]
    assert {k: first[k] for k in _obs_kwargs()} == _obs_kwargs()
    assert stored[1]["local_stage"] is None and stored[1]["contradiction_scope"] == "comprehension"
    assert stored[2]["task_kind"] is None


def test_pg_add_observation_continues_ordinals_across_transactions(engine, Sessions):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, observations=2)
    with Sessions() as session:
        assert _add(session, run_id).ordinal == 3
        session.commit()
    assert [o["ordinal"] for o in _obs_rows(engine, run_id)] == [1, 2, 3]


@pytest.mark.parametrize("end, states", [
    ("complete", ("completed", "active")),
    ("fail", ("failed", "obsolete")),
    ("superseded", ("completed", "superseded")),
])
def test_pg_add_observation_refused_on_terminal_runs(engine, Sessions, db, end, states):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, observations=1, end="fail" if end == "fail" else "complete")
    if end == "superseded":
        _committed_run(Sessions, event_id, end="complete")
    before, obs_before = _run_row(engine, run_id), _obs_rows(engine, run_id)
    assert (before["execution_status"], before["interpretation_status"]) == states
    with pytest.raises(InvalidEvaluationState, match="running / candidate requis"):
        _add(db, run_id)
    db.commit()
    assert _run_row(engine, run_id) == before and _obs_rows(engine, run_id) == obs_before


@pytest.mark.parametrize("states", [("running", "obsolete"), ("running", "active"), ("completed", "candidate")])
def test_pg_both_statuses_are_required_for_mutations(engine, Sessions, db, states):
    """État incohérent (impossible via le service, forcé en SQL) : add,
    complete et fail exigent running ET candidate."""
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE observation_evaluation_runs SET execution_status = :e, "
                             "interpretation_status = :i WHERE id = :id"),
                     {"e": states[0], "i": states[1], "id": run_id})
    for call in (lambda: _add(db, run_id),
                 lambda: svc.complete_evaluation_run(db, run_id=run_id, output_fingerprint="o"),
                 lambda: svc.fail_evaluation_run(db, run_id=run_id, failure_code="f")):
        with pytest.raises(InvalidEvaluationState):
            call()
    db.rollback()


def test_pg_add_observation_unknown_run(db):
    with pytest.raises(EvaluationRunNotFound):
        _add(db, uuid.uuid4())


def test_pg_add_observation_reloads_a_stale_run(Sessions, db):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id)
    stale = svc.get_evaluation_run(db, run_id=run_id)
    assert stale.execution_status == "running"
    with Sessions() as other:
        svc.complete_evaluation_run(other, run_id=run_id, output_fingerprint="o")
        other.commit()
    with pytest.raises(InvalidEvaluationState):
        _add(db, run_id)
    assert (stale.execution_status, stale.interpretation_status) == ("completed", "active")


def test_pg_concurrent_add_observation_gets_distinct_ordinals(engine, Sessions):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id)
    with Sessions() as first, Sessions() as second:
        assert _add(first, run_id, observation_text="first").ordinal == 1
        ordinal, error = _blocked(engine, first, second,
                                  lambda s: _add(s, run_id, observation_text="second").ordinal,
                                  waiting_on=ON_RUN)
    assert error is None and ordinal == 2
    assert [(o["ordinal"], o["observation_text"]) for o in _obs_rows(engine, run_id)] == [
        (1, "first"), (2, "second")]


def test_pg_add_observation_waiting_on_a_completion_is_refused(engine, Sessions):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, observations=1)
    with Sessions() as closer, Sessions() as evaluator:
        svc.complete_evaluation_run(closer, run_id=run_id, output_fingerprint="o")
        _, error = _blocked(engine, closer, evaluator, lambda s: _add(s, run_id), waiting_on=ON_RUN)
    assert isinstance(error, InvalidEvaluationState)
    assert len(_obs_rows(engine, run_id)) == 1


# --- E/F/G. vocabulaires, polarité, JSON persistés ---------------------------

def test_pg_every_vocabulary_value_is_persisted(engine, Sessions, db):
    event_id = _event(Sessions)
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id))
    expected = []
    for column, values in sorted(CLOSED_VOCABULARIES.items()):
        for value in sorted(values):
            if column == "polarity":
                kwargs = _obs_kwargs() if value == "supportive" else _contradictory()
            elif column == "contradiction_scope":
                kwargs = _contradictory(contradiction_scope=value)
            else:
                kwargs = _obs_kwargs(**{column: value})
            svc.add_observation(db, run_id=run.id, **kwargs)
            expected.append((column, value))
    db.commit()
    stored = _obs_rows(engine, run.id)
    assert [row[column] for (column, _), row in zip(expected, stored)] == [value for _, value in expected]


def test_pg_supportive_none_and_strong_contradictory_are_valid(engine, Sessions, db):
    """Force de preuve != niveau."""
    event_id = _event(Sessions)
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id))
    _add(db, run.id, local_stage="none", evidence_strength="strong")
    svc.add_observation(db, run_id=run.id, **_contradictory(evidence_strength="strong"))
    db.commit()
    assert [(o["polarity"], o["local_stage"], o["contradiction_scope"], o["evidence_strength"])
            for o in _obs_rows(engine, run.id)] == [
        ("supportive", "none", None, "strong"), ("contradictory", None, "comprehension", "strong")]


def test_pg_json_payloads_are_copied_and_stored_with_their_root_type(engine, Sessions, db):
    event_id = _event(Sessions)
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id))
    primary = {"work_index": 0, "quote": "rentabilité", "spans": [[0, 11]]}
    contributive = [{"work_index": 1, "flags": [True, None, 1.5]}]
    refs = [{"kind": "user_work", "index": 0}]
    residual = {"description": "rien", "steps": []}
    observation = _add(db, run.id, primary_user_action=primary, contributive_user_actions=contributive,
                       source_contribution_refs=refs, residual_cognitive_work=residual)
    empty = _add(db, run.id, primary_user_action={}, contributive_user_actions=[],
                 source_contribution_refs=[], residual_cognitive_work={})
    primary["quote"] = "modifiée"
    primary["spans"].append([1, 2])
    contributive[0]["flags"].append("x")
    contributive.append({})
    refs.clear()
    residual["steps"].append("ajout")
    expected = {
        "primary_user_action": {"work_index": 0, "quote": "rentabilité", "spans": [[0, 11]]},
        "contributive_user_actions": [{"work_index": 1, "flags": [True, None, 1.5]}],
        "source_contribution_refs": [{"kind": "user_work", "index": 0}],
        "residual_cognitive_work": {"description": "rien", "steps": []},
    }
    assert {k: getattr(observation, k) for k in expected} == expected
    db.commit()
    stored = _obs_rows(engine, run.id)
    assert {k: stored[0][k] for k in expected} == expected
    assert {k: stored[1][k] for k in expected} == {k: JSON_ROOTS[k]() for k in expected}
    with engine.connect() as conn:
        types = conn.execute(sa.text(
            "SELECT jsonb_typeof(primary_user_action), jsonb_typeof(contributive_user_actions), "
            "jsonb_typeof(source_contribution_refs), jsonb_typeof(residual_cognitive_work) "
            "FROM pedagogical_observations WHERE id = :id"), {"id": empty.id}).one()
    assert tuple(types) == ("object", "array", "array", "object")


@pytest.mark.parametrize("value", INVALID_JSON_VALUES[:6], ids=range(6))
def test_pg_invalid_json_never_reaches_the_database(engine, Sessions, db, value):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id)
    with pytest.raises(InvalidObservationPayload):
        _add(db, run_id, residual_cognitive_work=value)
    db.commit()
    assert _obs_rows(engine, run_id) == []


# --- H. complete_evaluation_run ---------------------------------------------

def test_pg_complete_with_zero_observation_is_a_success(engine, Sessions, db, clock):
    event_id = _event(Sessions)
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id))
    completed = svc.complete_evaluation_run(db, run_id=run.id, output_fingerprint="sha256:empty")
    assert completed is run
    assert (run.execution_status, run.interpretation_status) == ("completed", "active")
    assert run.completed_at == clock + timedelta(seconds=1) and run.started_at == clock
    assert (run.output_fingerprint, run.failure_code) == ("sha256:empty", None)
    db.commit()
    stored = _run_row(engine, run.id)
    assert (stored["execution_status"], stored["interpretation_status"]) == ("completed", "active")
    _assert_utc(stored["completed_at"])
    assert svc.get_observations(db, run_id=run.id) == []
    assert _count(engine, "pedagogical_observations") == 0


def test_pg_complete_with_observations(engine, Sessions, db):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, observations=3)
    obs_before = _obs_rows(engine, run_id)
    svc.complete_evaluation_run(db, run_id=run_id, output_fingerprint="sha256:three")
    db.commit()
    assert _states(engine, event_id) == {run_id: ("completed", "active")}
    assert _obs_rows(engine, run_id) == obs_before


def test_pg_complete_supersedes_the_previous_active_and_keeps_its_observations(engine, Sessions, db):
    event_id = _event(Sessions)
    old = _committed_run(Sessions, event_id, observations=2, end="complete")
    old_before, old_obs = _run_row(engine, old), _obs_rows(engine, old)
    new = svc.start_evaluation_run(db, **_start_kwargs(event_id, re_evaluates_run_id=old))
    _add(db, new.id, polarity="contradictory", local_stage=None, contradiction_scope="application")
    # Tant que le nouveau run n'est pas complété, l'ancien reste active.
    assert svc.get_evaluation_run(db, run_id=old).interpretation_status == "active"
    svc.complete_evaluation_run(db, run_id=new.id, output_fingerprint="sha256:new")
    db.commit()
    assert _states(engine, event_id) == {old: ("completed", "superseded"), new.id: ("completed", "active")}
    old_after = _run_row(engine, old)
    # Seul interpretation_status change sur l'ancien run ; ses observations
    # restent intactes (valid).
    assert {k: v for k, v in old_after.items() if k != "interpretation_status"} == {
        k: v for k, v in old_before.items() if k != "interpretation_status"}
    assert _obs_rows(engine, old) == old_obs
    assert all(o["integrity_status"] == "valid" for o in old_obs)


def test_pg_complete_flushes_the_supersession_before_the_activation(engine, Sessions, db):
    """L'index unique partiel est vérifié ligne par ligne : l'UPDATE
    superseded doit précéder l'UPDATE active, quel que soit l'ordre des
    clés primaires (le flush SQLAlchemy trie les UPDATE par PK). Les deux
    ordres sont forcés ici."""
    event_id = _event(Sessions)
    active = _committed_run(Sessions, event_id, end="complete")
    for lower in (True, False, True, False):
        candidate = _committed_run(Sessions, event_id)
        while (candidate < active) != lower:
            candidate = _committed_run(Sessions, event_id)
        statements, record = _recording(engine)
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            svc.complete_evaluation_run(db, run_id=candidate, output_fingerprint="o")
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        db.commit()
        updates = [params for sql, params in statements if sql.startswith("UPDATE observation_evaluation_runs")]
        assert [p["interpretation_status"] for p in updates] == ["superseded", "active"]
        assert updates[0]["observation_evaluation_runs_id"] == active
        assert updates[1]["observation_evaluation_runs_id"] == candidate
        active = candidate
    states = _states(engine, event_id)
    assert [s for s in states.values() if s[1] == "active"] == [("completed", "active")]
    assert states[active] == ("completed", "active")


def test_pg_complete_lock_order_is_run_then_event_then_active(engine, Sessions, db):
    event_id = _event(Sessions)
    previous = _committed_run(Sessions, event_id, end="complete")
    run_id = _committed_run(Sessions, event_id)
    statements, record = _recording(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        svc.complete_evaluation_run(db, run_id=run_id, output_fingerprint="o")
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    locks = [(sql, params) for sql, params in statements if "FOR " in sql and sql.startswith("SELECT")]
    assert len(locks) == 3
    assert LOCK_SQL["run"][0] in locks[0][0] and locks[0][0].endswith(LOCK_SQL["run"][1])
    assert LOCK_SQL["event"][0] in locks[1][0] and locks[1][0].endswith(LOCK_SQL["event"][1])
    assert "observation_evaluation_runs.interpretation_status = " in locks[2][0]
    assert locks[2][0].endswith("FOR NO KEY UPDATE")
    assert event_id in locks[2][1].values() and "active" in locks[2][1].values()
    # Aucune requête avant le premier verrou.
    assert statements[0][0] == locks[0][0]
    assert _states(engine, event_id)[previous] == ("completed", "active")  # rien de commité


@pytest.mark.parametrize("end", ["complete", "fail", "superseded"])
def test_pg_second_completion_is_refused(engine, Sessions, db, end):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, end="fail" if end == "fail" else "complete")
    if end == "superseded":
        _committed_run(Sessions, event_id, end="complete")
    before = _run_row(engine, run_id)
    with pytest.raises(InvalidEvaluationState):
        svc.complete_evaluation_run(db, run_id=run_id, output_fingerprint="again")
    db.commit()
    assert _run_row(engine, run_id) == before


def test_pg_complete_unknown_run(db):
    with pytest.raises(EvaluationRunNotFound):
        svc.complete_evaluation_run(db, run_id=uuid.uuid4(), output_fingerprint="o")


def test_pg_partial_unique_index_remains_the_final_defense(Sessions, db):
    """Contourner le service (écriture ORM directe) ne permet pas deux
    active : PostgreSQL refuse."""
    event_id = _event(Sessions)
    _committed_run(Sessions, event_id, end="complete")
    rogue = svc.get_evaluation_run(db, run_id=_committed_run(Sessions, event_id))
    rogue.interpretation_status = "active"
    with pytest.raises(sa.exc.IntegrityError, match=ACTIVE_INDEX):
        db.flush()


# --- I. concurrence de complete ---------------------------------------------

@pytest.mark.parametrize("with_previous_active", [False, True])
def test_pg_concurrent_completions_are_serialized_by_the_event_lock(engine, Sessions, with_previous_active):
    """Deux candidats du même événement complétés concurremment : le second
    est bloqué sur le verrou du CognitiveEvent parent (pas sur l'index
    unique), puis, une fois le premier commité, le supersede légitimement.
    Sans verrou parent et sans ancien active, le second échouerait sur
    l'index unique au lieu d'être sérialisé."""
    event_id = _event(Sessions)
    previous = _committed_run(Sessions, event_id, observations=1, end="complete") if with_previous_active else None
    first_id = _committed_run(Sessions, event_id, observations=1)
    second_id = _committed_run(Sessions, event_id, observations=2)
    obs_before = {r: _obs_rows(engine, r) for r in (first_id, second_id)}
    with Sessions() as first, Sessions() as second:
        if previous:
            stale = svc.get_evaluation_run(second, run_id=previous)  # référence conservée
            assert stale.interpretation_status == "active"
        svc.complete_evaluation_run(first, run_id=first_id, output_fingerprint="one")
        result, error = _blocked(
            engine, first, second,
            lambda s: svc.complete_evaluation_run(s, run_id=second_id, output_fingerprint="two").interpretation_status,
            waiting_on=ON_EVENT)
    assert error is None and result == "active"
    expected = {first_id: ("completed", "superseded"), second_id: ("completed", "active")}
    if previous:
        expected[previous] = ("completed", "superseded")
    assert _states(engine, event_id) == expected
    assert {r: _obs_rows(engine, r) for r in (first_id, second_id)} == obs_before


def test_pg_many_concurrent_completions_end_with_exactly_one_active(engine, Sessions):
    """N candidats complétés en parallèle (barrière) : aucun deadlock, aucune
    erreur, un seul active, tous les autres superseded."""
    event_id = _event(Sessions)
    _committed_run(Sessions, event_id, end="complete")
    candidates = [_committed_run(Sessions, event_id, observations=1) for _ in range(6)]
    barrier = threading.Barrier(len(candidates))
    errors, done = [], []

    def work(run_id):
        try:
            with Sessions() as session:
                session.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
                barrier.wait(timeout=10)
                svc.complete_evaluation_run(session, run_id=run_id, output_fingerprint=str(run_id))
                session.commit()
                done.append(run_id)
        except Exception as exc:  # noqa: BLE001 — restitué au test
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(run_id,)) for run_id in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == [] and sorted(done) == sorted(candidates)
    states = _states(engine, event_id)
    assert list(states.values()).count(("completed", "active")) == 1
    assert list(states.values()).count(("completed", "superseded")) == len(states) - 1
    # L'active est le dernier à avoir obtenu le verrou parent : completed_at
    # est pris sous ce verrou, donc strictement croissant dans l'ordre de
    # sérialisation.
    active = next(r for r, s in states.items() if s[1] == "active")
    completed = sorted(candidates, key=lambda r: _run_row(engine, r)["completed_at"])
    assert active == completed[-1]


def test_pg_concurrent_completion_of_the_same_run_is_refused(engine, Sessions):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id)
    with Sessions() as first, Sessions() as second:
        svc.complete_evaluation_run(first, run_id=run_id, output_fingerprint="one")
        _, error = _blocked(engine, first, second, lambda s: svc.complete_evaluation_run(
            s, run_id=run_id, output_fingerprint="two"), waiting_on=ON_RUN)
    assert isinstance(error, InvalidEvaluationState)
    assert _run_row(engine, run_id)["output_fingerprint"] == "one"


def test_pg_fk_share_lock_conflicts_with_for_update_but_not_no_key_update(engine, Sessions):
    """Justification de FOR NO KEY UPDATE : l'INSERT d'un run qui en réévalue
    un autre prend un FOR KEY SHARE (FK) sur le run réévalué. Bloquant
    contre FOR UPDATE, non bloquant contre FOR NO KEY UPDATE."""
    event_id = _event(Sessions)
    target = _committed_run(Sessions, event_id)
    for mode, blocks in (("FOR UPDATE", True), ("FOR NO KEY UPDATE", False)):
        with engine.connect() as holder, Sessions() as other:
            holder.execute(sa.text(f"SELECT id FROM observation_evaluation_runs WHERE id = :id {mode}"),
                           {"id": target})
            other.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
            if blocks:
                with pytest.raises(sa.exc.OperationalError, match="lock timeout"):
                    svc.start_evaluation_run(other, **_start_kwargs(event_id, re_evaluates_run_id=target))
            else:
                svc.start_evaluation_run(other, **_start_kwargs(event_id, re_evaluates_run_id=target))
            other.rollback()
            holder.rollback()


def test_pg_reevaluation_during_an_in_flight_evaluation_does_not_deadlock(engine, Sessions):
    """Orchestration réaliste : une transaction ajoute des observations à C
    (verrou du run) puis complète C (verrou de l'événement) ; entre-temps une
    autre démarre une réévaluation de C (verrou de l'événement + KEY SHARE
    FK sur C). Avec FOR UPDATE sur les runs : cycle. Ici : start ne bloque
    pas, complete attend simplement le verrou de l'événement."""
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id)
    with Sessions() as evaluator, Sessions() as replayer:
        _add(evaluator, run_id)
        replayer.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        replay = svc.start_evaluation_run(replayer, **_start_kwargs(event_id, re_evaluates_run_id=run_id))
        replay_id = replay.id
        result, error = _blocked(
            engine, replayer, evaluator,
            lambda s: svc.complete_evaluation_run(s, run_id=run_id, output_fingerprint="o").interpretation_status,
            waiting_on=ON_EVENT)
    assert error is None and result == "active"
    assert _states(engine, event_id) == {run_id: ("completed", "active"), replay_id: ("running", "candidate")}
    assert len(_obs_rows(engine, run_id)) == 1


# --- J. fail_evaluation_run -------------------------------------------------

def test_pg_fail_running_candidate(engine, Sessions, db, clock):
    event_id = _event(Sessions)
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id))
    failed = svc.fail_evaluation_run(db, run_id=run.id, failure_code="evaluator_timeout")
    assert failed is run
    assert (run.execution_status, run.interpretation_status) == ("failed", "obsolete")
    assert run.completed_at == clock + timedelta(seconds=1)
    assert (run.failure_code, run.output_fingerprint) == ("evaluator_timeout", None)
    db.commit()
    stored = _run_row(engine, run.id)
    assert (stored["execution_status"], stored["interpretation_status"], stored["failure_code"],
            stored["output_fingerprint"]) == ("failed", "obsolete", "evaluator_timeout", None)
    _assert_utc(stored["completed_at"])


def test_pg_fail_never_supersedes_the_active_and_keeps_observations(engine, Sessions, db):
    event_id = _event(Sessions)
    active = _committed_run(Sessions, event_id, observations=1, end="complete")
    active_before = _run_row(engine, active)
    candidate = _committed_run(Sessions, event_id, observations=2, re_evaluates_run_id=active)
    obs_before = _obs_rows(engine, candidate)
    statements, record = _recording(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        svc.fail_evaluation_run(db, run_id=candidate, failure_code="schema_mismatch")
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    db.commit()
    assert _states(engine, event_id) == {active: ("completed", "active"), candidate: ("failed", "obsolete")}
    assert _run_row(engine, active) == active_before
    assert _obs_rows(engine, candidate) == obs_before  # conservées pour l'audit
    assert _run_row(engine, candidate)["output_fingerprint"] is None
    # Un seul verrou (le run), jamais celui de l'événement.
    assert not any("cognitive_events" in sql for sql, _ in statements)
    assert not any(sql.startswith("DELETE") for sql, _ in statements)


@pytest.mark.parametrize("end", ["complete", "fail", "superseded"])
def test_pg_fail_refused_on_terminal_runs(engine, Sessions, db, end):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, end="fail" if end == "fail" else "complete")
    if end == "superseded":
        _committed_run(Sessions, event_id, end="complete")
    before = _run_row(engine, run_id)
    with pytest.raises(InvalidEvaluationState):
        svc.fail_evaluation_run(db, run_id=run_id, failure_code="late")
    db.commit()
    assert _run_row(engine, run_id) == before


def test_pg_fail_unknown_run(db):
    with pytest.raises(EvaluationRunNotFound):
        svc.fail_evaluation_run(db, run_id=uuid.uuid4(), failure_code="f")


def test_pg_fail_waiting_on_a_completion_sees_the_terminal_state(engine, Sessions):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id)
    with Sessions() as closer, Sessions() as other:
        stale = svc.get_evaluation_run(other, run_id=run_id)  # référence conservée
        svc.complete_evaluation_run(closer, run_id=run_id, output_fingerprint="o")
        _, error = _blocked(engine, closer, other,
                            lambda s: svc.fail_evaluation_run(s, run_id=run_id, failure_code="f"),
                            waiting_on=ON_RUN)
        assert isinstance(error, InvalidEvaluationState)
        assert stale.execution_status == "completed"
    assert _states(engine, event_id) == {run_id: ("completed", "active")}


# --- K. invalidate_observation ----------------------------------------------

def test_pg_invalidate_observation(engine, Sessions, db):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, observations=2, end="complete")
    run_before = _run_row(engine, run_id)
    target, untouched = _obs_rows(engine, run_id)
    observation = svc.invalidate_observation(db, observation_id=target["id"], reason="citation inexacte")
    assert isinstance(observation, PedagogicalObservation)
    assert observation.integrity_status == "invalidated"
    assert observation.invalidation_reason == "citation inexacte"
    assert observation.invalidated_at > target["created_at"]
    db.commit()
    after, untouched_after = _obs_rows(engine, run_id)
    _assert_utc(after["invalidated_at"])
    changed = {"integrity_status", "invalidated_at", "invalidation_reason"}
    assert {k: v for k, v in after.items() if k not in changed} == {
        k: v for k, v in target.items() if k not in changed}
    assert (after["integrity_status"], after["invalidation_reason"]) == ("invalidated", "citation inexacte")
    assert untouched_after == untouched
    assert _run_row(engine, run_id) == run_before  # l'invalidation n'affecte pas le run


def test_pg_second_invalidation_is_refused_never_a_no_op(engine, Sessions, db):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, observations=1)
    observation_id = _obs_rows(engine, run_id)[0]["id"]
    svc.invalidate_observation(db, observation_id=observation_id, reason="première")
    db.commit()
    before = _obs_rows(engine, run_id)
    with pytest.raises(ObservationAlreadyInvalidated):
        svc.invalidate_observation(db, observation_id=observation_id, reason="seconde")
    db.commit()
    assert _obs_rows(engine, run_id) == before
    assert before[0]["invalidation_reason"] == "première"


def test_pg_invalidate_unknown_observation(db):
    with pytest.raises(ObservationNotFound):
        svc.invalidate_observation(db, observation_id=uuid.uuid4(), reason="r")


@pytest.mark.parametrize("state", ["running", "superseded", "obsolete"])
def test_pg_invalidation_is_independent_of_the_run_lifecycle(engine, Sessions, db, state):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, observations=1, end="fail" if state == "obsolete" else None)
    if state == "superseded":
        with Sessions() as session:
            svc.complete_evaluation_run(session, run_id=run_id, output_fingerprint="o")
            session.commit()
        _committed_run(Sessions, event_id, end="complete")
    run_before = _run_row(engine, run_id)
    svc.invalidate_observation(db, observation_id=_obs_rows(engine, run_id)[0]["id"], reason="doublon")
    db.commit()
    assert _obs_rows(engine, run_id)[0]["integrity_status"] == "invalidated"
    assert _run_row(engine, run_id) == run_before


def test_pg_concurrent_invalidations_are_serialized(engine, Sessions):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, observations=1)
    observation_id = _obs_rows(engine, run_id)[0]["id"]
    with Sessions() as first, Sessions() as second:
        svc.invalidate_observation(first, observation_id=observation_id, reason="first")
        _, error = _blocked(engine, first, second, lambda s: svc.invalidate_observation(
            s, observation_id=observation_id, reason="second"), waiting_on=ON_OBSERVATION)
    assert isinstance(error, ObservationAlreadyInvalidated)
    assert _obs_rows(engine, run_id)[0]["invalidation_reason"] == "first"


# --- L. lectures ------------------------------------------------------------

def test_pg_get_evaluation_run_and_observations_unknown(db):
    with pytest.raises(EvaluationRunNotFound):
        svc.get_evaluation_run(db, run_id=uuid.uuid4())
    with pytest.raises(EvaluationRunNotFound):
        svc.get_observations(db, run_id=uuid.uuid4())


def test_pg_get_observations_orders_by_ordinal_and_includes_invalidated(engine, Sessions, db):
    event_id = _event(Sessions)
    run_id = _committed_run(Sessions, event_id, observations=4)
    other_run = _committed_run(Sessions, event_id, observations=1)
    # created_at inversés et lignes réécrites dans le désordre : l'ordre
    # restitué ne dépend que d'ordinal.
    with engine.begin() as conn:
        for ordinal in (2, 4, 1, 3):
            conn.execute(sa.text(
                "UPDATE pedagogical_observations SET created_at = now() - make_interval(mins => :m) "
                "WHERE evaluation_run_id = :r AND ordinal = :o"), {"m": ordinal, "r": run_id, "o": ordinal})
    svc.invalidate_observation(db, observation_id=_obs_rows(engine, run_id)[1]["id"], reason="r")
    db.commit()

    statements, record = _recording(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        observations = svc.get_observations(db, run_id=run_id)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert [o.ordinal for o in observations] == [1, 2, 3, 4]
    assert [o.integrity_status for o in observations] == ["valid", "invalidated", "valid", "valid"]
    assert all(isinstance(o, PedagogicalObservation) and o.evaluation_run_id == run_id for o in observations)
    assert statements[-1][0].endswith("ORDER BY pedagogical_observations.ordinal ASC")
    assert not any("FOR " in sql for sql, _ in statements)
    assert [o.ordinal for o in svc.get_observations(db, run_id=other_run)] == [1]


# --- M. propriété de la transaction -----------------------------------------

def test_pg_service_never_commits_nor_rolls_back(engine, Sessions, db):
    """Aucune opération (y compris le chemin de dédup et les erreurs
    métier) n'émet COMMIT ni ROLLBACK de la transaction de l'appelant ; seul
    un savepoint local est utilisé pour l'INSERT d'un run."""
    event_id = _event(Sessions)
    previous = _committed_run(Sessions, event_id, observations=1, end="complete")
    events = []
    listeners = {name: (lambda *a, _n=name: events.append(_n))
                 for name in ("commit", "rollback", "savepoint", "rollback_savepoint", "release_savepoint")}
    for name, fn in listeners.items():
        sa.event.listen(engine, name, fn)
    try:
        run = svc.start_evaluation_run(db, **_start_kwargs(event_id, evaluation_dedup_key="once"))
        with pytest.raises(DuplicateEvaluationRun):
            svc.start_evaluation_run(db, **_start_kwargs(event_id, evaluation_dedup_key="once"))
        observation = _add(db, run.id)
        with pytest.raises(InvalidObservationPayload):
            _add(db, run.id, polarity="mixed")
        svc.invalidate_observation(db, observation_id=observation.id, reason="r")
        svc.complete_evaluation_run(db, run_id=run.id, output_fingerprint="o")
        with pytest.raises(InvalidEvaluationState):
            svc.fail_evaluation_run(db, run_id=run.id, failure_code="f")
        svc.get_evaluation_run(db, run_id=run.id)
        svc.get_observations(db, run_id=run.id)
    finally:
        for name, fn in listeners.items():
            sa.event.remove(engine, name, fn)
    assert "commit" not in events and "rollback" not in events
    assert events == ["savepoint", "release_savepoint"]  # un seul INSERT de run réussi
    # Rien n'est visible hors de la transaction de l'appelant.
    assert _states(engine, event_id) == {previous: ("completed", "active")}
    db.rollback()
    assert _states(engine, event_id) == {previous: ("completed", "active")}
    assert len(_obs_rows(engine, previous)) == 1
    assert _count(engine, "pedagogical_observations") == 1


def test_pg_caller_rollback_discards_a_whole_evaluation(engine, Sessions, db):
    event_id = _event(Sessions)
    previous = _committed_run(Sessions, event_id, observations=1, end="complete")
    candidate = _committed_run(Sessions, event_id, observations=1)
    before = {r: _run_row(engine, r) for r in (previous, candidate)}
    obs_before = _obs_rows(engine, previous)
    run = svc.start_evaluation_run(db, **_start_kwargs(event_id, re_evaluates_run_id=previous))
    _add(db, run.id)
    svc.complete_evaluation_run(db, run_id=run.id, output_fingerprint="o")
    svc.fail_evaluation_run(db, run_id=candidate, failure_code="f")
    svc.invalidate_observation(db, observation_id=obs_before[0]["id"], reason="r")
    db.rollback()
    assert {r: _run_row(engine, r) for r in (previous, candidate)} == before
    assert _run_row(engine, run.id) is None
    assert _obs_rows(engine, previous) == obs_before
    # Toujours utilisable dans une nouvelle transaction.
    assert _add(db, candidate).ordinal == 2


def test_pg_observation_writes_join_a_wider_caller_transaction(engine, Sessions, db):
    """Écriture métier + capture T2-B + évaluation T3-B : une seule
    transaction, commitée (ou non) par l'appelant seul."""
    db.add(User(id="wider-user", level="debutant"))
    db.flush()
    event = cc.open_event(db, user_id="wider-user", event_origin="education", task_kind="interpret_metric",
                          stimulus_snapshot={"question": "ROE ?"})
    cc.append_user_work(db, event_id=event.id, work={"text": "réponse"})
    cc.finalize_event(db, event_id=event.id)
    run = svc.start_evaluation_run(db, **_start_kwargs(event.id))
    _add(db, run.id)
    svc.complete_evaluation_run(db, run_id=run.id, output_fingerprint="o")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users WHERE id = 'wider-user'")).scalar_one() == 0
    assert _count(engine, "cognitive_events") == 0 and _count(engine, "observation_evaluation_runs") == 0
    db.commit()
    assert _states(engine, event.id) == {run.id: ("completed", "active")}
    assert len(_obs_rows(engine, run.id)) == 1
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT status FROM cognitive_events WHERE id = :id"),
                            {"id": event.id}).scalar_one() == "finalized"
