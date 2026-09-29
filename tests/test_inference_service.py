"""Tests de T6-B : service transactionnel de l'inférence de l'état C1-C12
(core/inference_service.py).

Aucune migration dans ce chantier : le schéma testé est celui produit par
`alembic upgrade head` (= 0009_competency_inference_state, T6-A). Aucune
donnée n'est seedée : chaque test crée ses releases, définitions, événements,
runs T3, observations et dossiers T5 via les services T2-B / T3-B / T4-B /
T5-B. T6-C n'existe pas : les InferenceDecision sont construites à la main.

1. Tests sans base (toujours exécutés) : aucune migration, API publique
   exacte, hiérarchie d'exceptions, structures immuables, aucun commit /
   rollback, aucune route, aucun LLM, aucun moteur de décision, aucun score,
   formats canoniques, validation COMPLÈTE de la décision avant tout accès à
   la base.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T1-T5) :
   uniquement si ORYX_TEST_DATABASE_URL pointe vers une base DÉDIÉE dont le
   nom contient "test" (schéma public détruit et recréé). Sinon SKIPPÉS :
   rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t6b_test \\
           python -m pytest tests/test_inference_service.py

   Les tests de concurrence attendent, via pg_blocking_pids(), que la
   seconde connexion soit effectivement bloquée par la première (et sur
   quelle requête) avant de la libérer : aucun sleep() comme
   synchronisation.
"""
import ast
import dataclasses
import hashlib
import inspect
import itertools
import json
import subprocess
import threading
import uuid
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from core import inference_service as svc
from core import longitudinal_service as t5
from core import longitudinal_view as view
from core import observation_service as obs
from core.inference_service import (
    BasisRefDecision,
    DuplicateInference,
    InferenceContext,
    InferenceDecision,
    InferenceRunNotFound,
    InferenceServiceError,
    InvalidInferenceArgument,
    InvalidInferenceBasisReference,
    InvalidInferenceDecision,
    InvalidInferenceState,
    InvalidInferenceTension,
    InvalidStageClaim,
    LongitudinalParentNotUsable,
    PredecessorSnapshot,
    StageClaimDecision,
    StaleInferenceChain,
    StaleInferenceInput,
    StaleInferencePredecessor,
    TensionDecision,
    UserNotFound,
    ValidatedCompetencyState,
)
from tests.test_longitudinal_service import (  # noqa: F401 — fixture engine
    CLEANUP as T5_CLEANUP,
    _invalidate,
    _t3,
    _taxonomy,
    app,
    contra,
    engine,
    only,
    sup,
)
from tests.test_longitudinal_service import _start as _start_t5
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, _script_directory
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import T6A_TABLES, _code_tokens
from tests.test_migration_0005_cognitive_support_traces import OTHER_USER, USER
from tests.test_migration_0008_longitudinal_relations import T5A
from tests.test_migration_0009_competency_inference_state import T6A, T6A_MODELS
from tests.test_observation_service import INVALID_JSON_VALUES, _blocked, _NoDB, _reaches_db, _recording
from tests.test_observation_service import _start_kwargs as t3_kwargs
from tests.test_taxonomy_service import _advisory_locks, _pids, _rows

SERVICE_PATH = REPO_ROOT / "core" / "inference_service.py"
PUBLIC_API = {
    "start_competency_inference",
    "get_inference_context",
    "complete_competency_inference",
    "fail_competency_inference",
    "get_competency_inference",
    "get_active_competency_inference",
    "get_stage_claims",
    "get_inference_tensions",
    "get_inference_basis_refs",
    "get_user_competency_state",
    "get_validated_user_competency_state",
}
EXCEPTIONS = {
    InvalidInferenceArgument,
    UserNotFound,
    InferenceRunNotFound,
    LongitudinalParentNotUsable,
    InvalidInferenceState,
    DuplicateInference,
    StaleInferenceInput,
    StaleInferencePredecessor,
    InvalidInferenceDecision,
    InvalidStageClaim,
    InvalidInferenceTension,
    InvalidInferenceBasisReference,
    StaleInferenceChain,
}
VERSIONS = ("positive_basis_version", "confidence_profile_version", "state_decision_version", "validation_version",
            "inference_schema_version", "evaluator_version")
STAGES = ("discovery", "comprehension", "application", "mastery")
PROFILE = {"diagnosticity": {"note": "décrit par T6-C"}, "coverage": {"note": "qualitatif"}}
INVALID_UUIDS = [None, "", str(uuid.UUID(int=7)), 1, uuid.UUID(int=7).bytes]


def _start_kwargs(parent=None, **overrides):
    kwargs = {"longitudinal_assessment_run_id": uuid.uuid4() if parent is None else parent,
              "trigger": "longitudinal_completed",
              **{name: f"{name.split('_version')[0]}-1" for name in VERSIONS}}
    kwargs.update(overrides)
    return kwargs


# --- construction manuelle de décisions (T6-C n'existe pas) --------------------------

def claim(stage, status="not_established", mode=None, **overrides):
    if mode is None:
        mode = "none" if status == "not_established" else "direct"
    kwargs = {"stage": stage, "positive_basis_status": status, "basis_mode": mode,
              "confidence_profile": dict(PROFILE) if status == "established" else None}
    kwargs.update(overrides)
    return StageClaimDecision(**kwargs)


def claims(highest=None, *, direct=None):
    """Claims jusqu'à `highest` established (highest direct, inférieures
    implied sauf `direct`), supérieures not_established."""
    top = STAGES.index(highest) if highest else -1
    direct = {highest} if direct is None else set(direct)
    return tuple(claim(s, "established", "direct" if s in direct else "implied_by_higher_claim") if i <= top
                 else claim(s) for i, s in enumerate(STAGES))


def pos(stage, source):
    return BasisRefDecision(ref_role="positive_basis", source_kind="observation", source_id=source, claim_stage=stage)


def ref(role, kind, source, **target):
    return BasisRefDecision(ref_role=role, source_kind=kind, source_id=source, **target)


def tension(key="t1", stage="application", mode="whole_competency", memberships=(), **overrides):
    kwargs = {"tension_key": key, "fragilized_stage": stage, "scope_mode": mode, "summary": "contradiction récente",
              "revision_status": "unresolved", "capability_membership_ids": tuple(memberships)}
    kwargs.update(overrides)
    return TensionDecision(**kwargs)


def decision(current="application", claim_set=None, refs=(), *, cause=None, context=None, needs=(), tensions=(),
             summary="Décision fournie par T6-C."):
    return InferenceDecision(current_stage=current, transition_cause=cause, unresolved_revision_context=context,
                             validation_needs=tuple(needs), state_decision_summary=summary,
                             claims=claim_set if claim_set is not None else claims(current),
                             tensions=tuple(tensions), basis_refs=tuple(refs))


X, Y, Z = uuid.UUID(int=0x11), uuid.UUID(int=0x22), uuid.UUID(int=0x33)


def _pure(d):
    """Validation pure (sans base) : l'appel doit échouer avant la base."""
    return svc.complete_competency_inference(_NoDB(), run_id=uuid.uuid4(), decision=d)


def _pure_ok(d):
    _reaches_db(lambda db: svc.complete_competency_inference(db, run_id=uuid.uuid4(), decision=d))


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_no_migration_added_by_t6b():
    """T6-B est service-only : la tête reste 0009 (T6-A), aucun fichier 0010."""
    script = _script_directory()
    assert script.get_heads() == [T6A]
    assert script.get_revision(T6A).down_revision == T5A
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[-1] == f"{T6A}.py" and not any(name.startswith("0010") for name in files)


def test_t6a_schema_files_are_unchanged_by_t6b():
    """Ni core/models.py ni la migration 0009 ne sont modifiés par T6-B
    (comparaison avec le commit de merge de T6-A quand il est disponible)."""
    base = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "b70bfa8b9fb3c2987f4d73737cbe26705bce232c"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    if base.returncode != 0:
        pytest.skip("historique git indisponible")
    for path in ("core/models.py", f"alembic/versions/{T6A}.py", "api.py"):
        diff = subprocess.run(["git", "diff", "--quiet", base.stdout.strip(), "--", path], cwd=REPO_ROOT)
        assert diff.returncode == 0, path


def test_public_api_is_exactly_the_eleven_operations():
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == PUBLIC_API
    for name in PUBLIC_API:
        params = list(inspect.signature(getattr(svc, name)).parameters.values())
        assert params[0].name == "db" and params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:]), name


def test_exact_signatures():
    def params(fn):
        return {name: p.default for name, p in inspect.signature(fn).parameters.items() if name != "db"}

    empty = inspect.Parameter.empty
    assert params(svc.start_competency_inference) == {
        "longitudinal_assessment_run_id": empty, "trigger": empty, **{v: empty for v in VERSIONS},
        "model_id": None, "prompt_spec_version": None}
    assert params(svc.complete_competency_inference) == {"run_id": empty, "decision": empty}
    assert params(svc.fail_competency_inference) == {"run_id": empty, "failure_code": None}
    for name in ("get_inference_context", "get_competency_inference", "get_stage_claims", "get_inference_tensions",
                 "get_inference_basis_refs"):
        assert params(getattr(svc, name)) == {"run_id": empty}, name
    for name in ("get_active_competency_inference", "get_user_competency_state",
                 "get_validated_user_competency_state"):
        assert params(getattr(svc, name)) == {"user_id": empty, "competency_code": empty}, name


def test_derived_identity_and_outputs_cannot_be_supplied():
    """Le caller ne fournit jamais user_id, competency_code, predecessor,
    previous_stage, fingerprint, dédup ni sortie."""
    for field in ("user_id", "competency_code", "predecessor_inference_run_id", "previous_stage",
                  "input_fingerprint", "inference_dedup_key", "output_fingerprint", "current_stage", "id"):
        with pytest.raises(TypeError):
            svc.start_competency_inference(_NoDB(), **_start_kwargs(**{field: None}))
    for field in ("user_id", "competency_code", "previous_stage", "transition", "tension_state",
                  "input_fingerprint", "output_fingerprint"):
        with pytest.raises(TypeError):
            svc.complete_competency_inference(_NoDB(), run_id=uuid.uuid4(), decision=decision(), **{field: None})


def test_exceptions_are_a_small_business_hierarchy():
    public = {o for o in vars(svc).values()
              if isinstance(o, type) and issubclass(o, Exception) and o.__module__ == svc.__name__}
    assert public == EXCEPTIONS | {InferenceServiceError}
    for exc in EXCEPTIONS:
        assert exc.__bases__ == (InferenceServiceError,), exc
    assert InferenceServiceError.__bases__ == (Exception,)
    assert not issubclass(InferenceServiceError, (LookupError, ValueError, t5.LongitudinalServiceError,
                                                  view.LongitudinalViewError, obs.ObservationServiceError))


def test_structures_are_frozen_keyword_only_dataclasses():
    for cls in (InferenceContext, PredecessorSnapshot, InferenceDecision, StageClaimDecision, TensionDecision,
                BasisRefDecision, ValidatedCompetencyState):
        assert cls.__dataclass_params__.frozen, cls
        assert all(f.kw_only for f in dataclasses.fields(cls)), cls
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision().current_stage = "mastery"
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(InferenceDecision) == ["current_stage", "transition_cause", "unresolved_revision_context",
                                        "validation_needs", "state_decision_summary", "claims", "tensions",
                                        "basis_refs"]
    assert names(StageClaimDecision) == ["stage", "positive_basis_status", "basis_mode", "basis_summary",
                                         "scope_summary", "confidence_profile", "mastery_assessment"]
    assert names(TensionDecision) == ["tension_key", "fragilized_stage", "scope_mode", "summary",
                                      "revision_status", "capability_membership_ids"]
    assert names(BasisRefDecision) == ["ref_role", "source_kind", "source_id", "claim_stage", "tension_key",
                                       "confidence_dimension"]
    for field in ("run_id", "user_id", "competency_code", "longitudinal_assessment_run_id", "longitudinal_dossier",
                  "predecessor", "input_fingerprint", *VERSIONS, "model_id", "prompt_spec_version"):
        assert field in names(InferenceContext), field
    for field in ("inference_run_id", "current_stage", "tension_state", "unresolved_revision_context",
                  "input_fingerprint", "output_fingerprint", "longitudinal_assessment_run_id"):
        assert field in names(PredecessorSnapshot), field
    # Aucune valeur mécanique fournie par T6-C, aucun nombre de confiance.
    for cls in (InferenceDecision, StageClaimDecision, TensionDecision, BasisRefDecision):
        for field in ("previous_stage", "transition", "tension_state", "input_fingerprint", "output_fingerprint",
                      "confidence_score", "confidence_level", "overall_confidence", "percentage", "stage_claim_id",
                      "tension_id", "score"):
            assert field not in names(cls), (cls, field)


def _imports(path):
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_service_owns_no_transaction_and_has_no_framework_or_llm_dependency():
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).split("\n")
    for forbidden in ("commit", "rollback", "close", "SessionLocal", "get_db", "HTTPException", "delete",
                      "merge", "expunge", "text", "pg_advisory_lock", "pg_try_advisory_lock", "pg_advisory_unlock",
                      "create_all", "hash", "execute_raw", "sleep"):
        assert forbidden not in tokens, forbidden
    assert tokens.count("begin_nested") == 1
    assert tokens.count("pg_advisory_xact_lock") == 1
    # Seule dépendance applicative : les modèles et la vue T5-C (lecture).
    assert _imports(SERVICE_PATH) == {"hashlib", "json", "math", "uuid", "collections.abc", "dataclasses",
                                      "datetime", "types", "typing", "sqlalchemy", "sqlalchemy.exc",
                                      "core.longitudinal_view", "core.models"}


def test_no_llm_route_or_decision_engine():
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).lower()
    for word in ("anthropic", "openai", "claude", "llm", "completion", "messages", "requests", "httpx", "client",
                 "prompt(", "fastapi", "apirouter"):
        assert word not in tokens, word
    for name in ("infer_stage", "evaluate_claim", "calculate_confidence", "compute_confidence", "decide_tension",
                 "decide_validation_need", "mastery_engine", "decide_stage", "assess_mastery"):
        assert not hasattr(svc, name), name
        assert name not in tokens, name


def test_no_score_rank_or_numeric_pedagogy():
    """Aucun score, pourcentage, ratio, points, pénalité, décroissance ni
    rang (docstrings et commentaires exclus). Le seul ordre de stade est un
    tuple interne ; aucun nombre n'est dérivé d'un décompte de preuves."""
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).lower()
    for word in ("score", "percent", "ratio", "points", "penalty", "decay", "rank", "weight", "coefficient",
                 "xp", "stage_rank", "numeric_stage", "active_validation_plan", "days_since", "threshold"):
        assert word not in tokens.split("\n") and f"{word}_" not in tokens and f"_{word}" not in tokens, word
    assert svc.STAGE_SEQUENCE == ("non_etabli", "discovery", "comprehension", "application", "mastery")
    # Aucun champ d'état numérique dans les structures du service.
    for cls in (InferenceContext, PredecessorSnapshot, ValidatedCompetencyState):
        for f in dataclasses.fields(cls):
            assert f.type not in (float, "float"), (cls, f.name)


def test_vocabularies_match_the_0009_check_constraints():
    t6 = {m.__tablename__: m for m in T6A_MODELS}
    checks = {c.name: str(c.sqltext) for m in t6.values() for c in m.__table__.constraints
              if isinstance(c, sa.CheckConstraint)}

    def values(name):
        return set(re_values(checks[name]))

    def re_values(sql):
        return [part.split("'")[1] for part in sql.split(",")]

    assert values("ck_competency_inference_runs_current_stage") == svc.CURRENT_STAGES
    assert values("ck_competency_stage_claims_stage") == set(svc.CLAIM_STAGES)
    assert values("ck_competency_inference_runs_transition") == svc.TRANSITIONS
    assert values("ck_competency_inference_runs_transition_cause") == svc.TRANSITION_CAUSES
    assert values("ck_competency_inference_runs_tension_state") == svc.TENSION_STATES
    assert values("ck_competency_inference_tensions_scope_mode") == svc.TENSION_SCOPE_MODES
    assert values("ck_competency_inference_tensions_revision_status") == svc.REVISION_STATUSES
    assert values("ck_competency_inference_basis_refs_ref_role") == svc.REF_ROLES
    assert values("ck_competency_inference_basis_refs_confidence_dimension") == svc.CONFIDENCE_DIMENSIONS
    assert values("ck_competency_inference_basis_refs_source_kind") == svc.SOURCE_KINDS
    assert values("ck_competency_stage_claims_positive_basis_status") == svc.POSITIVE_BASIS_STATUSES
    assert values("ck_competency_stage_claims_basis_mode") == svc.BASIS_MODES
    assert svc.COMPETENCY_CODES == t5.COMPETENCY_CODES
    assert svc.DEDUP_CONSTRAINT == "uq_competency_inference_runs_dedup_key"


def test_service_is_the_only_writer_of_t6_tables():
    """Hors core/models.py, la migration 0009 et CE service, aucun code
    applicatif ne mentionne les modèles / tables T6-A ; aucune route."""
    allowed = {"core/models.py", f"alembic/versions/{T6A}.py", "core/inference_service.py"}
    needles = (*(m.__name__ for m in T6A_MODELS), *T6A_TABLES, "inference_service")
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
        if rel not in allowed:
            for needle in needles:
                assert needle not in source, (rel, needle)
            for name in PUBLIC_API:
                assert name not in source, (rel, name)
    assert checked > 0
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8")).lower()
    for word in ("inference", "stage_claim", "competency_state", "tension"):
        assert word not in api, word


def test_service_constructs_only_t6_rows_and_mutates_only_run_and_cache():
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    constructed = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id[:1].isupper()}
    from core import models as orm
    orm_models = {name for name, obj in vars(orm).items() if isinstance(obj, type) and hasattr(obj, "__table__")}
    models = constructed & orm_models
    assert models == {"CompetencyInferenceRun", "CompetencyStageClaim", "CompetencyInferenceTension",
                      "CompetencyInferenceTensionCapability", "CompetencyInferenceBasisRef",
                      "UserCompetencyState"}, models
    assigned = {(t.value.id, t.attr) for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)}
    assert assigned - {("self", "_values")} == {pair for pair in assigned if pair[0] in {"run", "active", "cache"}}
    assert {attr for owner, attr in assigned if owner == "active"} == {"interpretation_status"}
    assert {attr for owner, attr in assigned if owner == "cache"} == {
        "active_inference_run_id", "current_stage", "tension_state", "state_generation", "updated_at"}


def test_utcnow_is_utc_aware():
    now = svc._utcnow()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


# --- formats canoniques --------------------------------------------------------

def test_canonical_json_is_sorted_compact_utf8():
    assert svc._canonical_json({"b": 1, "a": ["é", None]}) == '{"a":["é",null],"b":1}'
    assert svc._canonical_sha256({"a": 1}) == hashlib.sha256(b'{"a":1}').hexdigest()


def _relations(**payload):
    return svc._Relations({"dependencies": [], "transfers": [], "revalidations": [], **payload}, {}, {})


def _parent(**overrides):
    values = {"user_id": "u-é", "competency_code": "C7", "pedagogical_taxonomy_release_id": uuid.UUID(int=9),
              "input_fingerprint": "t5fp", "dependency_version": "d1", "transfer_version": "t1",
              "revalidation_version": "r1", "relation_schema_version": "s1"}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_input_fingerprint_v1_exact_canonical_format():
    dependency = {"target_observation_id": str(X), "source_kind": "observation", "source_observation_id": str(Y),
                  "source_support_trace_id": None, "scope_fingerprint": "sf", "dependency_type": "dependent",
                  "scope_mode": "localized", "dependency_basis": {"why": "é"}}
    payload = svc._dossier_payload(_parent(), _relations(dependencies=[dependency]))
    predecessor = {"inference_run_id": str(Z), "current_stage": "application", "tension_state": "none",
                   "unresolved_revision_context": None, "output_fingerprint": "out"}
    expected = {"input_schema_version": 1, "user_id": "u-é", "competency_code": "C7",
                "pedagogical_taxonomy_release_id": str(uuid.UUID(int=9)), "longitudinal_input_fingerprint": "t5fp",
                "dependency_version": "d1", "transfer_version": "t1", "revalidation_version": "r1",
                "relation_schema_version": "s1", "dependencies": [dependency], "transfers": [],
                "revalidations": [], "predecessor": predecessor}
    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert svc._input_fingerprint(payload, predecessor) == hashlib.sha256(canonical.encode()).hexdigest()
    assert svc._input_fingerprint(payload, None) != svc._input_fingerprint(payload, predecessor)


def test_input_fingerprint_tracks_every_component():
    base = svc._dossier_payload(_parent(), _relations())
    reference = svc._input_fingerprint(base, None)
    variants = [svc._dossier_payload(_parent(**{k: v}), _relations()) for k, v in (
        ("user_id", "autre"), ("competency_code", "C8"), ("pedagogical_taxonomy_release_id", uuid.UUID(int=10)),
        ("input_fingerprint", "t5fp2"), ("dependency_version", "d2"), ("transfer_version", "t2"),
        ("revalidation_version", "r2"), ("relation_schema_version", "s2"))]
    variants += [svc._dossier_payload(_parent(), _relations(**{k: [{"x": 1}]}))
                 for k in ("dependencies", "transfers", "revalidations")]
    prints = {svc._input_fingerprint(v, None) for v in variants}
    assert reference not in prints and len(prints) == len(variants)


def test_dedup_key_excludes_the_trigger_and_tracks_every_specification():
    spec = {name: "1" for name in svc.SPECIFICATION_FIELDS}
    reference = svc._inference_dedup_key("fp", spec)
    assert "trigger" not in svc.SPECIFICATION_FIELDS
    assert svc._inference_dedup_key("fp", dict(spec)) == reference
    changed = {svc._inference_dedup_key("fp", {**spec, name: "2"}) for name in svc.SPECIFICATION_FIELDS}
    changed.add(svc._inference_dedup_key("fp2", spec))
    changed.add(svc._inference_dedup_key("fp", {**spec, "model_id": None}))
    assert reference not in changed and len(changed) == len(svc.SPECIFICATION_FIELDS) + 2


def test_activation_lock_key_is_deterministic_and_distinct_from_t5():
    key = svc._activation_lock_key(USER, "C7")
    encoded = json.dumps(["oryx-t6", USER, "C7"], separators=(",", ":"), ensure_ascii=False).encode()
    assert key == int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big", signed=True)
    assert -2 ** 63 <= key < 2 ** 63
    assert key == svc._activation_lock_key(USER, "C7")
    assert key != t5._activation_lock_key(USER, "C7")
    assert len({svc._activation_lock_key(u, c) for u in (USER, OTHER_USER) for c in ("C7", "C8")}) == 4


def _checked(d, *, relations=None):
    return svc._validated_decision(d)


def test_output_fingerprint_is_order_independent_and_tracks_semantics():
    rels = svc._Relations({}, {("transfer", Y): {"source_observation_id": "s", "target_observation_id": "t",
                                                 "scope_fingerprint": "sf"}}, {})
    memberships = {X: uuid.UUID(int=100), Y: uuid.UUID(int=101)}
    refs = [pos("application", X), ref("confidence", "transfer", Y, claim_stage="application",
                                       confidence_dimension="independence"),
            ref("tension", "observation", Z, tension_key="a"), ref("tension", "observation", X, tension_key="b"),
            ref("validation", "observation", Z)]
    tensions = [tension("a", mode="localized", memberships=(X, Y)), tension("b", summary="autre")]
    base = decision(refs=refs, tensions=tensions, needs=[{"need": "revalider"}])
    shuffled = decision(claim_set=tuple(reversed(claims("application"))), refs=list(reversed(refs)),
                        tensions=[tension("zz", summary="autre"), tension("yy", mode="localized", memberships=(Y, X))],
                        needs=[{"need": "revalider"}])
    shuffled = dataclasses.replace(shuffled, basis_refs=tuple(
        dataclasses.replace(r, tension_key={"a": "yy", "b": "zz"}[r.tension_key]) if r.tension_key else r
        for r in reversed(refs)))

    def fp(d, **overrides):
        kwargs = {"input_fingerprint": "in", "specification": {"state_decision_version": "1"},
                  "decision": svc._validated_decision(d), "previous_stage": None, "transition": None,
                  "tension_state": "open", "relations": rels, "membership_definitions": memberships}
        kwargs.update(overrides)
        return svc._output_fingerprint(**kwargs)

    reference = fp(base)
    assert fp(shuffled) == reference
    different = {
        fp(dataclasses.replace(base, current_stage="comprehension")),
        fp(dataclasses.replace(base, claims=claims("application", direct={"application", "comprehension"}),
                               basis_refs=(*refs, pos("comprehension", Z)))),
        fp(dataclasses.replace(base, tensions=(tension("a", mode="localized", memberships=(X,)), tensions[1]))),
        fp(dataclasses.replace(base, state_decision_summary="autre")),
        fp(base, input_fingerprint="in2"),
        fp(base, specification={"state_decision_version": "2"}),
        fp(base, previous_stage="comprehension", transition="upgraded"),
        fp(base, membership_definitions={X: uuid.UUID(int=100), Y: uuid.UUID(int=102)}),
    }
    assert reference not in different and len(different) == 8


def test_dedup_violation_detection_is_targeted():
    def error(constraint):
        return sa.exc.IntegrityError("INSERT", {}, SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint)))

    assert svc._is_dedup_violation(error("uq_competency_inference_runs_dedup_key"))
    for other in ("uq_competency_inference_runs_one_active_user_competency", "competency_inference_runs_pkey",
                  "uq_longitudinal_assessment_runs_dedup_key", None):
        assert not svc._is_dedup_violation(error(other)), other


# --- validation pure : arguments -------------------------------------------------

@pytest.mark.parametrize("field", ["trigger", *VERSIONS])
@pytest.mark.parametrize("value", ["", " ", None, 3, b"x", "a\x00b"])
def test_start_rejects_invalid_text(field, value):
    with pytest.raises(InvalidInferenceArgument, match=field):
        svc.start_competency_inference(_NoDB(), **_start_kwargs(**{field: value}))


@pytest.mark.parametrize("field", ["model_id", "prompt_spec_version"])
def test_start_optional_metadata(field):
    with pytest.raises(InvalidInferenceArgument, match=field):
        svc.start_competency_inference(_NoDB(), **_start_kwargs(**{field: ""}))
    _reaches_db(lambda db: svc.start_competency_inference(db, **_start_kwargs(**{field: "x"})))


@pytest.mark.parametrize("value", INVALID_UUIDS)
def test_identifiers_must_be_uuids(value):
    for call in (lambda db: svc.start_competency_inference(db, **{**_start_kwargs(),
                                                                  "longitudinal_assessment_run_id": value}),
                 lambda db: svc.get_inference_context(db, run_id=value),
                 lambda db: svc.complete_competency_inference(db, run_id=value, decision=decision()),
                 lambda db: svc.fail_competency_inference(db, run_id=value),
                 lambda db: svc.get_competency_inference(db, run_id=value),
                 lambda db: svc.get_stage_claims(db, run_id=value)):
        with pytest.raises(InvalidInferenceArgument):
            call(_NoDB())


def test_couple_arguments_are_validated():
    for fn in (svc.get_active_competency_inference, svc.get_user_competency_state,
               svc.get_validated_user_competency_state):
        with pytest.raises(InvalidInferenceArgument, match="user_id"):
            fn(_NoDB(), user_id="", competency_code="C7")
        with pytest.raises(InvalidInferenceArgument, match="competency_code"):
            fn(_NoDB(), user_id=USER, competency_code="C7_A")
        _reaches_db(lambda db: fn(db, user_id=USER, competency_code="C12"))


def test_decision_must_be_an_inference_decision():
    for value in (None, {}, "decision", SimpleNamespace(**dataclasses.asdict(decision()))):
        with pytest.raises(InvalidInferenceArgument, match="InferenceDecision"):
            _pure(value)


def test_failure_code_is_none_or_text():
    with pytest.raises(InvalidInferenceArgument, match="failure_code"):
        svc.fail_competency_inference(_NoDB(), run_id=uuid.uuid4(), failure_code="")
    _reaches_db(lambda db: svc.fail_competency_inference(db, run_id=uuid.uuid4(), failure_code="t6c_error"))


# --- validation pure : quatre claims ---------------------------------------------

def test_valid_decisions_reach_the_database():
    _pure_ok(decision("application", refs=[pos("application", X)]))
    _pure_ok(decision("non_etabli", claims()))
    _pure_ok(decision("mastery", refs=[pos("mastery", X)]))
    _pure_ok(decision("discovery", refs=[pos("discovery", X)]))


@pytest.mark.parametrize("claim_set, match", [
    (claims("application")[:3], "exactement quatre"),
    ((*claims("application"), claim("mastery")), "en double"),
    ((claims("application")[0], claims("application")[1], claims("application")[2], claims("application")[2]),
     "en double"),
    ((claim("non_etabli"), *claims("application")[1:]), "non_etabli"),
    ((), "exactement quatre"),
    ([dict(stage="discovery")] * 4, "StageClaimDecision"),
    (None, "liste"),
])
def test_exactly_four_claims(claim_set, match):
    d = decision(refs=[pos("application", X)])
    d = dataclasses.replace(d, claims=claim_set)
    with pytest.raises(InvalidStageClaim, match=match):
        _pure(d)


def test_claims_are_accepted_in_any_order():
    _pure_ok(decision(claim_set=tuple(reversed(claims("application"))), refs=[pos("application", X)]))


@pytest.mark.parametrize("stage, status, mode, match", [
    ("discovery", "not_established", "direct", "exige basis_mode none"),
    ("discovery", "established", "none", "exige direct"),
    ("discovery", "Established", "direct", "positive_basis_status"),
    ("discovery", "established", "inferred", "basis_mode"),
])
def test_claim_status_and_mode_coherence(stage, status, mode, match):
    bad = claim(stage, status, mode)
    with pytest.raises(InvalidStageClaim, match=match):
        _pure(decision("non_etabli", (bad, *claims()[1:])))


def test_direct_established_requires_a_positive_basis_ref():
    with pytest.raises(InvalidStageClaim, match="sans ref positive_basis"):
        _pure(decision("application"))
    _pure_ok(decision("application", refs=[pos("application", X)]))


def test_implied_claims():
    # Implied valide si une claim supérieure est established.
    _pure_ok(decision("application", refs=[pos("application", X)]))
    # Implied avec une positive_basis directe : refusé.
    with pytest.raises(InvalidInferenceBasisReference, match="positive_basis vers comprehension"):
        _pure(decision("application", refs=[pos("application", X), pos("comprehension", Y)]))
    # Implied sans claim supérieure established.
    lone = (claim("discovery", "established", "implied_by_higher_claim"), *claims()[1:])
    with pytest.raises(InvalidStageClaim, match="sans claim supérieure"):
        _pure(decision("discovery", lone))
    # Mastery ne peut jamais être implied.
    mastery = (*claims("application")[:3], claim("mastery", "established", "implied_by_higher_claim"))
    with pytest.raises(InvalidStageClaim, match="mastery ne peut jamais"):
        _pure(decision("mastery", mastery, [pos("application", X)]))


def test_not_established_claims_take_no_positive_basis():
    with pytest.raises(InvalidInferenceBasisReference, match="positive_basis vers mastery not_established"):
        _pure(decision("application", refs=[pos("application", X), pos("mastery", Y)]))


def test_positive_basis_is_monotonic():
    gap = (claim("discovery"), claim("comprehension"), claim("application", "established"), claim("mastery"))
    with pytest.raises(InvalidStageClaim, match="non monotone"):
        _pure(decision("application", gap, [pos("application", X)]))
    hole = (claim("discovery", "established"), claim("comprehension"), claim("application", "established"),
            claim("mastery"))
    with pytest.raises(InvalidStageClaim, match="non monotone"):
        _pure(decision("application", hole, [pos("application", X), pos("discovery", Y)]))
    _pure_ok(decision("mastery", claims("mastery"), [pos("mastery", X)]))


def test_the_same_observation_never_supports_two_claims():
    both = claims("application", direct={"application", "comprehension"})
    _pure_ok(decision("application", both, [pos("application", X), pos("comprehension", Y)]))
    with pytest.raises(InvalidInferenceBasisReference, match="positive_basis de application et de comprehension"):
        _pure(decision("application", both, [pos("application", X), pos("comprehension", X)]))


def test_positive_basis_must_come_from_an_observation():
    for kind in ("dependency", "transfer", "revalidation"):
        with pytest.raises(InvalidInferenceBasisReference, match="jamais une preuve"):
            _pure(decision("application", refs=[pos("application", X), ref("positive_basis", kind, Y,
                                                                            claim_stage="application")]))


def test_confidence_profile():
    for bad in (None, [], "élevé", 0.8, 3):
        c = (*claims("application")[:2], claim("application", "established", confidence_profile=bad),
             claim("mastery"))
        with pytest.raises(InvalidStageClaim, match="confidence_profile"):
            _pure(decision("application", c, [pos("application", X)]))
    c = (*claims("application")[:3], claim("mastery", confidence_profile={"x": 1}))
    with pytest.raises(InvalidStageClaim, match="aucun confidence_profile"):
        _pure(decision("application", c, [pos("application", X)]))
    frozen = (*claims("application")[:2],
              claim("application", "established", confidence_profile=MappingProxyType({"coverage": ("a",)})),
              claim("mastery"))
    _pure_ok(decision("application", frozen, [pos("application", X)]))


STRICT_JSON_REFUSED = [v for v in INVALID_JSON_VALUES if not (list(v) == ["t"] and isinstance(v["t"], tuple))]


@pytest.mark.parametrize("value", STRICT_JSON_REFUSED, ids=range(len(STRICT_JSON_REFUSED)))
def test_json_payloads_are_strict(value):
    c = (*claims("application")[:2], claim("application", "established", confidence_profile=value),
         claim("mastery"))
    with pytest.raises(InvalidStageClaim):
        _pure(decision("application", c, [pos("application", X)]))
    with pytest.raises(InvalidInferenceDecision):
        _pure(decision("application", refs=[pos("application", X), ref("validation", "observation", Y)],
                       needs=[value]))
    with pytest.raises(InvalidInferenceDecision):
        _pure(decision("application", refs=[pos("application", X)], context=value))


def test_frozen_json_structures_are_accepted_as_json():
    """Mapping (dont mappingproxy) = objet, tuple = tableau : T6-C peut
    transmettre des structures figées ; elles sont copiées en dict / list."""
    d = decision("application", refs=[pos("application", X), ref("validation", "observation", Y)],
                 needs=(MappingProxyType({"k": (1, "a")}),), context=MappingProxyType({"m": ({"x": None},)}))
    checked = svc._validated_decision(d)
    assert checked.needs == [{"k": [1, "a"]}] and type(checked.needs[0]) is dict
    assert checked.context == {"m": [{"x": None}]}


def test_json_payloads_reject_cycles_and_are_deep_copied():
    cyclic = {"a": []}
    cyclic["a"].append(cyclic)
    with pytest.raises(InvalidInferenceDecision, match="circulaire"):
        _pure(decision("application", refs=[pos("application", X)], context=cyclic))
    shared = {"k": [1]}
    d = decision("application", refs=[pos("application", X), ref("validation", "observation", Y)],
                 needs=[shared], context={"motif": shared})
    checked = svc._validated_decision(d)
    shared["k"].append(2)
    assert checked.needs == [{"k": [1]}] and checked.context == {"motif": {"k": [1]}}


def test_confidence_refs():
    for dimension in sorted(svc.CONFIDENCE_DIMENSIONS):
        _pure_ok(decision("application", refs=[pos("application", X), ref(
            "confidence", "transfer", Y, claim_stage="application", confidence_dimension=dimension)]))
    assert svc.CONFIDENCE_DIMENSIONS == {"diagnosticity", "coverage", "independence", "consistency",
                                         "temporal_validation"}
    with pytest.raises(InvalidInferenceBasisReference, match="confidence_dimension obligatoire"):
        _pure(decision("application", refs=[pos("application", X), ref("confidence", "observation", Y,
                                                                        claim_stage="application")]))
    with pytest.raises(InvalidInferenceBasisReference, match="confidence_dimension"):
        _pure(decision("application", refs=[pos("application", X), ref(
            "confidence", "observation", Y, claim_stage="application", confidence_dimension="score")]))
    with pytest.raises(InvalidInferenceBasisReference, match="vers mastery not_established"):
        _pure(decision("application", refs=[pos("application", X), ref(
            "confidence", "observation", Y, claim_stage="mastery", confidence_dimension="coverage")]))


def test_mastery_assessment_and_refs_belong_to_the_mastery_claim():
    with_assessment = (*claims("application")[:3], claim("mastery", mastery_assessment={"why": "pas de transfert"}))
    _pure_ok(decision("application", with_assessment, [pos("application", X)]))
    misplaced = (*claims("application")[:2], claim("application", "established", mastery_assessment={"x": 1}),
                 claim("mastery"))
    with pytest.raises(InvalidStageClaim, match="réservé à la claim mastery"):
        _pure(decision("application", misplaced, [pos("application", X)]))
    _pure_ok(decision("application", refs=[pos("application", X), ref("mastery", "transfer", Y,
                                                                       claim_stage="mastery")]))
    with pytest.raises(InvalidInferenceBasisReference, match="rattachée à comprehension"):
        _pure(decision("application", refs=[pos("application", X), ref("mastery", "transfer", Y,
                                                                       claim_stage="comprehension")]))


@pytest.mark.parametrize("bad, match", [
    (ref("positive_basis", "observation", Y, tension_key="t1"), "claim_stage obligatoire"),
    (ref("positive_basis", "observation", Y, claim_stage="application", tension_key="t1"), "tension_key interdit"),
    (ref("confidence", "observation", Y, claim_stage="application"), "confidence_dimension obligatoire"),
    (ref("confidence", "observation", Y, tension_key="t1", confidence_dimension="coverage"),
     "claim_stage obligatoire"),
    (ref("tension", "observation", Y), "tension_key obligatoire"),
    (ref("tension", "observation", Y, tension_key="t1", claim_stage="application"), "claim_stage interdit"),
    (ref("tension", "observation", Y, tension_key="t1", confidence_dimension="coverage"),
     "confidence_dimension interdit"),
    (ref("tension", "observation", Y, tension_key="inconnue"), "inconnue"),
    (ref("transition", "observation", Y, claim_stage="application"), "claim_stage interdit"),
    (ref("transition", "observation", Y, tension_key="t1"), "tension_key interdit"),
    (ref("validation", "observation", Y, tension_key="t1"), "tension_key interdit"),
    (ref("validation", "observation", Y, confidence_dimension="coverage"), "confidence_dimension interdit"),
    (ref("mastery", "observation", Y, claim_stage="application"), "rattachée à application"),
    (ref("mastery", "observation", Y, claim_stage="mastery", confidence_dimension="coverage"), "interdit"),
    (ref("proof", "observation", Y), "ref_role"),
    (ref("transition", "support_trace", Y), "source_kind"),
    (ref("transition", "observation", str(Y)), "source_id"),
])
def test_ref_attachments(bad, match):
    base = [pos("application", X), ref("tension", "observation", Z, tension_key="t1")]
    with pytest.raises(InvalidInferenceBasisReference, match=match):
        _pure(decision("application", refs=[*base, bad], tensions=[tension()]))


def test_semantic_duplicate_refs_are_refused():
    base = [pos("application", X)]
    dup = ref("transition", "transfer", Y)
    with pytest.raises(InvalidInferenceBasisReference, match="en double"):
        _pure(decision("application", refs=[*base, dup, dup]))
    with pytest.raises(InvalidInferenceBasisReference, match="en double"):
        _pure(decision("application", refs=[*base, pos("application", X)]))
    # Même source, cibles ou dimensions différentes : provenances distinctes.
    _pure_ok(decision("application", refs=[*base, dup, ref("validation", "transfer", Y),
                                           ref("confidence", "transfer", Y, claim_stage="application",
                                               confidence_dimension="coverage"),
                                           ref("confidence", "transfer", Y, claim_stage="application",
                                               confidence_dimension="independence")]))


@pytest.mark.parametrize("bad, match", [
    (tension(mode="localized"), "au moins un"),
    (tension(mode="whole_competency", memberships=(X,)), "aucun capability_membership_id"),
    (tension(mode="competency_only", memberships=(X,)), "aucun capability_membership_id"),
    (tension(mode="localized", memberships=(X, X)), "en double"),
    (tension(mode="localized", memberships=(str(X),)), "capability_membership_ids"),
    (tension(mode="global"), "scope_mode"),
    (tension(stage="non_etabli"), "fragilized_stage"),
    (tension(key=""), "tension_key"),
    (tension(summary=" "), "summary"),
    (tension(revision_status="solved"), "revision_status"),
])
def test_tension_structure(bad, match):
    with pytest.raises(InvalidInferenceTension, match=match):
        _pure(decision("application", refs=[pos("application", X), ref("tension", "observation", Y,
                                                                        tension_key=bad.tension_key or "t1")],
                       tensions=[bad]))


def test_tension_keys_are_unique_and_every_tension_has_provenance():
    refs = [pos("application", X), ref("tension", "observation", Y, tension_key="t1")]
    with pytest.raises(InvalidInferenceTension, match="en double"):
        _pure(decision("application", refs=refs, tensions=[tension("t1"), tension("t1", summary="bis")]))
    with pytest.raises(InvalidInferenceTension, match="sans ref tension"):
        _pure(decision("application", refs=[pos("application", X)], tensions=[tension("t1")]))
    _pure_ok(decision("application", refs=refs, tensions=[tension("t1")]))
    _pure_ok(decision("application", refs=refs, tensions=[tension("t1", mode="localized", memberships=(Z,))]))


def test_run_level_fields():
    for field, value, match in (("current_stage", "expert", "current_stage"), ("current_stage", None, "current_stage"),
                                ("transition_cause", "reroll", "transition_cause"),
                                ("state_decision_summary", "", "state_decision_summary"),
                                ("unresolved_revision_context", {}, "non vide"),
                                ("unresolved_revision_context", ["x"], "mapping"),
                                ("validation_needs", {"need": 1}, "liste"),
                                ("validation_needs", ["besoin"], "mapping")):
        d = dataclasses.replace(decision(refs=[pos("application", X), ref("validation", "observation", Y)]),
                                **{field: value})
        with pytest.raises(InvalidInferenceDecision, match=match):
            _pure(d)


def test_non_etabli_means_no_established_claim():
    with pytest.raises(InvalidInferenceDecision, match="aucune prétention positive"):
        _pure(decision("non_etabli", claims("discovery"), [pos("discovery", X)]))


def test_validation_needs_require_a_validation_ref():
    needs = [{"besoin": "revalider le levier sur un autre cas"}]
    with pytest.raises(InvalidInferenceDecision, match="sans ref validation"):
        _pure(decision("application", refs=[pos("application", X)], needs=needs))
    _pure_ok(decision("application", refs=[pos("application", X), ref("validation", "observation", Y)], needs=needs))
    _pure_ok(decision("application", refs=[pos("application", X)], needs=()))


# --- requêtes : ordre et verrous (sans base réelle) ------------------------------

class _RecordingDB:
    """Enregistre les requêtes ; toute ligne cherchée est absente."""

    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(" ".join(str(statement.compile(dialect=postgresql.dialect())).split()))
        return SimpleNamespace(scalar_one_or_none=lambda: None, one_or_none=lambda: None, first=lambda: None)


def test_complete_reads_the_immutable_couple_before_any_lock():
    db = _RecordingDB()
    with pytest.raises(InferenceRunNotFound):
        svc.complete_competency_inference(db, run_id=uuid.uuid4(), decision=decision(refs=[pos("application", X)]))
    assert len(db.statements) == 1
    assert db.statements[0].startswith("SELECT competency_inference_runs.user_id, "
                                       "competency_inference_runs.competency_code FROM competency_inference_runs")
    assert " FOR " not in db.statements[0]


def test_fail_first_query_is_the_candidate_row_lock():
    db = _RecordingDB()
    with pytest.raises(InferenceRunNotFound):
        svc.fail_competency_inference(db, run_id=uuid.uuid4())
    assert len(db.statements) == 1
    assert db.statements[0].startswith("SELECT competency_inference_runs.id,")
    assert db.statements[0].endswith("FOR NO KEY UPDATE")


def test_start_first_reads_the_t5_parent_without_lock():
    db = _RecordingDB()
    with pytest.raises(LongitudinalParentNotUsable, match="introuvable"):
        svc.start_competency_inference(db, **_start_kwargs())
    assert len(db.statements) == 1
    assert db.statements[0].startswith("SELECT longitudinal_assessment_runs.id,") and " FOR " not in db.statements[0]


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

T6_CLEANUP = ("competency_inference_basis_refs", "competency_inference_tension_capabilities",
              "competency_inference_tensions", "competency_stage_claims", "user_competency_states",
              "competency_inference_runs")
assert set(T6_CLEANUP) == T6A_TABLES
ON_ADVISORY = "SELECT pg_advisory_xact_lock("
ON_T5_RUN = "SELECT longitudinal_assessment_runs.id,"


@pytest.fixture
def Sessions(engine):  # noqa: F811
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)  # = core.db.SessionLocal
    yield factory
    with engine.begin() as conn:
        for table in (*T6_CLEANUP, *T5_CLEANUP):
            conn.execute(sa.text(f"DELETE FROM {table}"))
        conn.execute(sa.text("DELETE FROM users WHERE id NOT IN ('u', 'u2')"))


@pytest.fixture
def db(Sessions):
    session = Sessions()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _commit_t5(Sessions, fn, **kwargs):
    with Sessions() as session:
        result = fn(session, **kwargs)
        session.commit()
        return result.id


def _complete_t5(Sessions, run_id):
    return _commit_t5(Sessions, t5.complete_longitudinal_assessment, run_id=run_id)


def _new_t5(Sessions, w, *specs):
    """Nouvelles observations (une par spec) puis nouveau dossier T5 activé,
    qui supersede le précédent. Retourne (run T5, ids des observations)."""
    new = [_t3(Sessions, w.tx.id, spec).id for spec in specs]
    run = _start_t5(Sessions, w.tx.id)
    _complete_t5(Sessions, run)
    return run, new


@pytest.fixture
def world(Sessions):
    """Release active C7_A/B/C + C8_A ; observations d'événements distincts ;
    dossier T5 L1 completed / active avec une dépendance, un transfert et
    une revalidation."""
    tx = _taxonomy(Sessions)
    A, B, C = tx.m["C7_A"], tx.m["C7_B"], tx.m["C7_C"]
    w = SimpleNamespace(tx=tx, A=A, B=B, C=C, dA=tx.d["C7_A"], dB=tx.d["C7_B"])
    w.disc_t3 = _t3(Sessions, tx.id, sup(A, B, stage="discovery"))
    w.disc = w.disc_t3.id
    w.comp = _t3(Sessions, tx.id, sup(A, B)).id
    w.app_t3 = _t3(Sessions, tx.id, app(A))
    w.app = w.app_t3.id
    w.app_b = _t3(Sessions, tx.id, app(B)).id
    w.k = _t3(Sessions, tx.id, contra(A)).id
    w.kB = _t3(Sessions, tx.id, contra(B)).id
    w.k_only = _t3(Sessions, tx.id, only(contra())).id
    w.t5 = _start_t5(Sessions, tx.id)
    with Sessions() as s:
        w.dep = t5.add_dependency(s, run_id=w.t5, target_observation_id=w.comp, source_kind="observation",
                                  source_observation_id=w.disc, dependency_type="partially_dependent",
                                  scope_mode="localized", dependency_basis={"why": "reprend la décomposition"},
                                  capability_membership_ids=[B]).id
        w.tr = t5.add_transfer(s, run_id=w.t5, source_observation_id=w.comp, target_observation_id=w.app,
                               scope_mode="localized", transfer_basis={"adaptation": "structure différente"},
                               capability_membership_ids=[A]).id
        w.rv = t5.add_revalidation(s, run_id=w.t5, source_contradiction_observation_id=w.kB,
                                   target_supportive_observation_id=w.app_b, scope_mode="whole_observation",
                                   revalidation_basis={"mechanism": "démontré sans aide"}).id
        s.commit()
    _complete_t5(Sessions, w.t5)
    return w


def _start(Sessions, parent, **overrides) -> InferenceContext:
    with Sessions() as session:
        context = svc.start_competency_inference(session, **_start_kwargs(parent, **overrides))
        session.commit()
        return context


def _complete(Sessions, run_id, d):
    with Sessions() as session:
        run = svc.complete_competency_inference(session, run_id=run_id, decision=d)
        session.commit()
        return run.id


def _app_decision(w, **overrides):
    """Première inférence valide : Application directe (positive w.app),
    inférieures implied, Mastery not_established."""
    kwargs = {"refs": [pos("application", w.app)]}
    kwargs.update(overrides)
    return decision("application", **kwargs)


def _activate_first(Sessions, w, d=None, **start):
    context = _start(Sessions, w.t5, **start)
    _complete(Sessions, context.run_id, d or _app_decision(w))
    return context.run_id


def _run(engine, run_id):
    rows = _rows(engine, "SELECT * FROM competency_inference_runs WHERE id = :id", id=run_id)
    return rows[0] if rows else None


def _state(engine, run_id):
    row = _run(engine, run_id)
    return row["execution_status"], row["interpretation_status"]


def _cache(engine, user_id=USER, competency_code="C7"):
    rows = _rows(engine, "SELECT * FROM user_competency_states WHERE user_id = :u AND competency_code = :c",
                 u=user_id, c=competency_code)
    return rows[0] if rows else None


def _count(engine, table, where="TRUE", **params):
    with engine.connect() as conn:
        return conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {where}"), params).scalar_one()


def _exec(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(sa.text(sql), params)


def _t6_snapshot(engine):
    return {t: sorted(map(repr, _rows(engine, f"SELECT * FROM {t}"))) for t in T6_CLEANUP}


# --- A. start ------------------------------------------------------------------------

def test_pg_start_creates_a_running_candidate_with_derived_identity(engine, Sessions, db, world):
    w = world
    context = svc.start_competency_inference(db, **_start_kwargs(w.t5, model_id="m", prompt_spec_version="p"))
    # L'appelant possède la transaction : rien n'est visible avant son COMMIT.
    assert _count(engine, "competency_inference_runs") == 0
    db.commit()
    row = _run(engine, context.run_id)
    assert (row["execution_status"], row["interpretation_status"]) == ("running", "candidate")
    assert (row["user_id"], row["competency_code"], row["longitudinal_assessment_run_id"]) == (USER, "C7", w.t5)
    assert row["predecessor_inference_run_id"] is None and row["trigger"] == "longitudinal_completed"
    for output in ("previous_stage", "current_stage", "transition", "transition_cause", "tension_state",
                   "unresolved_revision_context", "validation_needs", "state_decision_summary",
                   "output_fingerprint", "completed_at", "failure_code"):
        assert row[output] is None, output
    for table in T6_CLEANUP[:5]:
        assert _count(engine, table) == 0, table
    assert len(row["input_fingerprint"]) == 64 and len(row["inference_dedup_key"]) == 64
    assert (row["model_id"], row["prompt_spec_version"]) == ("m", "p")
    # Contexte immuable, unique entrée de T6-C.
    assert isinstance(context, InferenceContext) and context.predecessor is None
    assert context.longitudinal_dossier.run_id == w.t5 and context.user_id == USER
    assert context.input_fingerprint == row["input_fingerprint"]
    assert {o.observation_id for o in context.longitudinal_dossier.active_history.observations} == {
        w.disc, w.comp, w.app, w.app_b, w.k, w.kB, w.k_only}
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.predecessor = None


@pytest.mark.parametrize("case", ["running", "failed", "superseded", "unknown"])
def test_pg_start_refuses_a_t5_parent_that_is_not_completed_active(engine, Sessions, db, world, case):
    w = world
    if case == "running":
        parent = _start_t5(Sessions, w.tx.id)
    elif case == "failed":
        parent = _start_t5(Sessions, w.tx.id)
        _commit_t5(Sessions, t5.fail_longitudinal_assessment, run_id=parent)
    elif case == "superseded":
        parent = w.t5
        _new_t5(Sessions, w, sup(w.A))
    else:
        parent = uuid.uuid4()
    with pytest.raises(LongitudinalParentNotUsable):
        svc.start_competency_inference(db, **_start_kwargs(parent))
    assert _count(engine, "competency_inference_runs") == 0


def _break_chain(Sessions, engine, w, case):
    if case == "observation_invalidated":
        _invalidate(Sessions, w.app)
    elif case == "t3_superseded":
        _t3(Sessions, w.tx.id, app(w.A), event_id=w.app_t3.event)
    elif case == "event_not_finalized":
        _exec(engine, "UPDATE cognitive_events SET status = 'open', closed_at = NULL WHERE id = :e",
              e=w.app_t3.event)
    elif case == "release_retired":
        _taxonomy(Sessions, ("C7_A",))
    elif case == "t5_superseded":
        _new_t5(Sessions, w, sup(w.A))


CHAIN_BREAKS = ["observation_invalidated", "t3_superseded", "event_not_finalized", "release_retired"]


@pytest.mark.parametrize("case", CHAIN_BREAKS)
def test_pg_start_refuses_a_non_current_upstream_chain(engine, Sessions, db, world, case):
    """Plus strict que T5-C (qui relit ces dossiers pour l'audit) : jamais un
    nouvel état courant depuis une chaîne déjà invalidée / superseded."""
    w = world
    _break_chain(Sessions, engine, w, case)
    with pytest.raises(LongitudinalParentNotUsable):
        svc.start_competency_inference(db, **_start_kwargs(w.t5))
    assert _count(engine, "competency_inference_runs") == 0
    if case in ("observation_invalidated", "t3_superseded"):
        # T5-C sait encore relire ce dossier (audit), mais le signale.
        dossier = view.build_longitudinal_dossier(db, run_id=w.t5)
        assert view.UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT in {lim.code for lim in dossier.limitations}


def test_pg_start_derives_the_predecessor(engine, Sessions, world):
    w = world
    first = _activate_first(Sessions, w)
    context = _start(Sessions, w.t5, state_decision_version="state-2")
    assert _run(engine, context.run_id)["predecessor_inference_run_id"] == first
    p = context.predecessor
    assert isinstance(p, PredecessorSnapshot)
    assert (p.inference_run_id, p.current_stage, p.tension_state, p.longitudinal_assessment_run_id) == (
        first, "application", "none", w.t5)
    assert p.established_claim_stages == ("discovery", "comprehension", "application")
    assert p.output_fingerprint == _run(engine, first)["output_fingerprint"]
    assert p.input_fingerprint == _run(engine, first)["input_fingerprint"]


def test_pg_predecessor_may_reference_a_superseded_t5(engine, Sessions, world):
    """Le predecessor est une référence historique : son T5 peut être
    superseded ; seul le nouveau parent doit être courant."""
    w = world
    first = _activate_first(Sessions, w)
    l2, _ = _new_t5(Sessions, w, sup(w.A))
    context = _start(Sessions, l2)
    assert context.predecessor.inference_run_id == first
    assert context.predecessor.longitudinal_assessment_run_id == w.t5
    assert _rows(engine, "SELECT interpretation_status FROM longitudinal_assessment_runs WHERE id = :i",
                 i=w.t5)[0]["interpretation_status"] == "superseded"


# --- B. identité logique, déduplication, reroll --------------------------------------------

def test_pg_same_dossier_same_versions_is_a_duplicate_whatever_the_trigger(engine, Sessions, db, world):
    w = world
    first = _start(Sessions, w.t5)
    for trigger in ("longitudinal_completed", "manual_audit", "retry"):
        with pytest.raises(DuplicateInference):
            svc.start_competency_inference(db, **_start_kwargs(w.t5, trigger=trigger))
    # Transaction de l'appelant toujours utilisable.
    other = svc.start_competency_inference(db, **_start_kwargs(w.t5, evaluator_version="evaluator-2"))
    db.commit()
    assert _count(engine, "competency_inference_runs") == 2
    assert other.input_fingerprint == first.input_fingerprint
    assert other.inference_dedup_key != first.inference_dedup_key


@pytest.mark.parametrize("field", [*VERSIONS, "model_id", "prompt_spec_version"])
def test_pg_changing_a_specification_is_a_new_identity(engine, Sessions, db, world, field):
    w = world
    _start(Sessions, w.t5)
    context = svc.start_competency_inference(db, **_start_kwargs(w.t5, **{field: "autre-spécification"}))
    db.commit()
    assert _state(engine, context.run_id) == ("running", "candidate")


def test_pg_input_fingerprint_is_the_logical_dossier_not_the_t5_uuid(engine, Sessions, db, world):
    """Un T5 recalculé à l'identique (autre UUID, même dossier logique) donne
    la même empreinte ; un dossier réellement changé, une autre."""
    w = world
    first = _start(Sessions, w.t5)
    same = _start_t5(Sessions, w.tx.id)
    with Sessions() as s:
        t5.add_dependency(s, run_id=same, target_observation_id=w.comp, source_kind="observation",
                          source_observation_id=w.disc, dependency_type="partially_dependent", scope_mode="localized",
                          dependency_basis={"why": "reprend la décomposition"}, capability_membership_ids=[w.B])
        t5.add_transfer(s, run_id=same, source_observation_id=w.comp, target_observation_id=w.app,
                        scope_mode="localized", transfer_basis={"adaptation": "structure différente"},
                        capability_membership_ids=[w.A])
        t5.add_revalidation(s, run_id=same, source_contradiction_observation_id=w.kB,
                            target_supportive_observation_id=w.app_b, scope_mode="whole_observation",
                            revalidation_basis={"mechanism": "démontré sans aide"})
        s.commit()
    _complete_t5(Sessions, same)
    assert same != w.t5
    with pytest.raises(DuplicateInference):
        svc.start_competency_inference(db, **_start_kwargs(same))
    db.rollback()
    # Relation différente (basis) => dossier logique différent.
    changed = _start_t5(Sessions, w.tx.id)
    with Sessions() as s:
        t5.add_transfer(s, run_id=changed, source_observation_id=w.comp, target_observation_id=w.app,
                        scope_mode="localized", transfer_basis={"adaptation": "autre raison"},
                        capability_membership_ids=[w.A])
        s.commit()
    _complete_t5(Sessions, changed)
    context = svc.start_competency_inference(db, **_start_kwargs(changed))
    assert context.input_fingerprint != first.input_fingerprint


def test_pg_reinterpreting_the_active_dossier_identically_is_a_reroll(engine, Sessions, db, world):
    w = world
    _activate_first(Sessions, w)
    with pytest.raises(DuplicateInference, match="réinterprétation à l'identique"):
        svc.start_competency_inference(db, **_start_kwargs(w.t5, trigger="manual_audit"))
    context = svc.start_competency_inference(db, **_start_kwargs(w.t5, state_decision_version="state-2"))
    assert context.predecessor is not None


def test_pg_dedup_race_is_translated_via_savepoint_and_the_transaction_stays_usable(engine, Sessions, world):
    """Deux workers : la pré-vérification du second ne voit pas l'INSERT non
    commité du premier ; il attend sur l'UNIQUE, puis la violation de CETTE
    contrainte devient DuplicateInference après retour au savepoint ; ses
    écritures antérieures restent, il peut continuer."""
    w = world

    def racing(session):
        session.execute(sa.text("INSERT INTO users (id, level) VALUES ('pending-user', 'debutant')"))
        try:
            svc.start_competency_inference(session, **_start_kwargs(w.t5, trigger="worker-2"))
        except DuplicateInference as exc:
            assert isinstance(exc.__cause__, sa.exc.IntegrityError)
            assert "uq_competency_inference_runs_dedup_key" in str(exc.__cause__)
            return svc.start_competency_inference(session, **_start_kwargs(w.t5, evaluator_version="e-2"))
        return None

    with Sessions() as first, Sessions() as second:
        winner = svc.start_competency_inference(first, **_start_kwargs(w.t5, trigger="worker-1"))
        result, error = _blocked(engine, first, second, racing, waiting_on="INSERT INTO competency_inference_runs")
    assert error is None and result is not None
    assert {r["trigger"] for r in _rows(engine, "SELECT trigger FROM competency_inference_runs")} == {"worker-1"} | {
        "longitudinal_completed"}
    assert _count(engine, "competency_inference_runs", "inference_dedup_key = :k",
                  k=winner.inference_dedup_key) == 1
    assert _count(engine, "users", "id = 'pending-user'") == 1


def test_pg_other_integrity_errors_are_not_masked(engine, Sessions, db, world, monkeypatch):
    w = world
    monkeypatch.setattr(svc, "RUNNING", "bogus")
    with pytest.raises(sa.exc.IntegrityError, match="ck_competency_inference_runs_execution_status") as info:
        svc.start_competency_inference(db, **_start_kwargs(w.t5))
    assert not isinstance(info.value, InferenceServiceError)
    monkeypatch.undo()
    context = svc.start_competency_inference(db, **_start_kwargs(w.t5))
    db.commit()
    assert _state(engine, context.run_id) == ("running", "candidate")


# --- C. reprise et fail ---------------------------------------------------------------

def test_pg_resume_rebuilds_exactly_the_same_context(engine, Sessions, db, world):
    w = world
    _activate_first(Sessions, w)
    started = _start(Sessions, w.t5, state_decision_version="state-2")
    resumed = svc.get_inference_context(db, run_id=started.run_id)
    assert resumed == started
    assert resumed.predecessor == started.predecessor and resumed.longitudinal_dossier == started.longitudinal_dossier
    # Lecture seule : rien n'a changé, aucun second candidat.
    assert _count(engine, "competency_inference_runs") == 2
    assert not db.new and not db.dirty


@pytest.mark.parametrize("case", [*CHAIN_BREAKS, "t5_superseded"])
def test_pg_resume_of_a_stale_candidate_is_refused(engine, Sessions, db, world, case):
    w = world
    context = _start(Sessions, w.t5)
    _break_chain(Sessions, engine, w, case)
    with pytest.raises(StaleInferenceInput):
        svc.get_inference_context(db, run_id=context.run_id)
    assert _state(engine, context.run_id) == ("running", "candidate")
    assert _count(engine, "competency_inference_runs") == 1


def test_pg_resume_after_another_activation_is_stale_predecessor(engine, Sessions, db, world):
    w = world
    a = _start(Sessions, w.t5)
    b = _start(Sessions, w.t5, evaluator_version="e-2")
    _complete(Sessions, a.run_id, _app_decision(w))
    with pytest.raises(StaleInferencePredecessor):
        svc.get_inference_context(db, run_id=b.run_id)


def test_pg_fail_keeps_the_active_and_the_cache(engine, Sessions, db, world):
    w = world
    first = _activate_first(Sessions, w)
    cache = _cache(engine)
    context = _start(Sessions, w.t5, evaluator_version="e-2")
    svc.fail_competency_inference(db, run_id=context.run_id, failure_code="t6c_timeout")
    db.commit()
    row = _run(engine, context.run_id)
    assert (row["execution_status"], row["interpretation_status"], row["failure_code"]) == (
        "failed", "obsolete", "t6c_timeout")
    assert row["completed_at"] is not None and row["current_stage"] is None
    assert _state(engine, first) == ("completed", "active") and _cache(engine) == cache
    with pytest.raises(InvalidInferenceState):
        svc.fail_competency_inference(db, run_id=context.run_id)
    with pytest.raises(InvalidInferenceState):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w))
    with pytest.raises(InvalidInferenceState):
        svc.get_inference_context(db, run_id=context.run_id)


def test_pg_terminal_runs_accept_no_second_transition(engine, Sessions, db, world):
    w = world
    first = _activate_first(Sessions, w)
    for call in (lambda: svc.fail_competency_inference(db, run_id=first),
                 lambda: svc.complete_competency_inference(db, run_id=first, decision=_app_decision(w))):
        with pytest.raises(InvalidInferenceState):
            call()
        db.rollback()
    with pytest.raises(InferenceRunNotFound):
        svc.fail_competency_inference(db, run_id=uuid.uuid4())
    with pytest.raises(InferenceRunNotFound):
        svc.complete_competency_inference(db, run_id=uuid.uuid4(), decision=_app_decision(w))


def test_pg_a_candidate_with_children_is_corrupt_never_repaired(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    _exec(engine, "INSERT INTO competency_stage_claims (id, inference_run_id, stage, positive_basis_status, "
                  "basis_mode, created_at) VALUES (:i, :r, 'discovery', 'not_established', 'none', now())",
          i=uuid.uuid4(), r=context.run_id)
    for call in (lambda: svc.fail_competency_inference(db, run_id=context.run_id),
                 lambda: svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w)),
                 lambda: svc.get_inference_context(db, run_id=context.run_id)):
        with pytest.raises(InvalidInferenceState, match="lignes enfants"):
            call()
        db.rollback()
    assert _state(engine, context.run_id) == ("running", "candidate")
    assert _count(engine, "competency_stage_claims") == 1


# --- D. complete : première activation --------------------------------------------------

def test_pg_first_activation_persists_everything_and_creates_the_cache(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    d = decision("application", refs=[
        pos("application", w.app),
        ref("confidence", "transfer", w.tr, claim_stage="application", confidence_dimension="independence"),
        ref("confidence", "observation", w.comp, claim_stage="comprehension", confidence_dimension="coverage"),
        ref("mastery", "transfer", w.tr, claim_stage="mastery"),
        ref("tension", "observation", w.k, tension_key="levier"),
        ref("validation", "revalidation", w.rv),
    ], tensions=[tension("levier", mode="localized", memberships=(w.A,))],
        needs=[{"besoin": "revalider le levier"}],
        claim_set=(*claims("application")[:3], claim("mastery", mastery_assessment={"transfert": "un seul"})))
    run = svc.complete_competency_inference(db, run_id=context.run_id, decision=d)
    assert _state(engine, context.run_id) == ("running", "candidate")  # rien avant COMMIT
    db.commit()
    row = _run(engine, context.run_id)
    assert (row["execution_status"], row["interpretation_status"]) == ("completed", "active")
    assert (row["previous_stage"], row["current_stage"], row["transition"], row["transition_cause"]) == (
        None, "application", None, None)
    assert row["tension_state"] == "open" and row["validation_needs"] == [{"besoin": "revalider le levier"}]
    assert row["completed_at"] is not None and len(row["output_fingerprint"]) == 64 and row["failure_code"] is None
    assert run.output_fingerprint == row["output_fingerprint"]
    cache = _cache(engine)
    assert (cache["active_inference_run_id"], cache["current_stage"], cache["tension_state"],
            cache["state_generation"]) == (context.run_id, "application", "open", 1)
    claims_rows = svc.get_stage_claims(db, run_id=context.run_id)
    assert [c.stage for c in claims_rows] == list(STAGES)
    assert [(c.positive_basis_status, c.basis_mode) for c in claims_rows] == [
        ("established", "implied_by_higher_claim"), ("established", "implied_by_higher_claim"),
        ("established", "direct"), ("not_established", "none")]
    assert claims_rows[3].mastery_assessment == {"transfert": "un seul"} and claims_rows[3].confidence_profile is None
    assert claims_rows[2].confidence_profile == PROFILE
    tensions = svc.get_inference_tensions(db, run_id=context.run_id)
    assert len(tensions) == 1 and tensions[0][1] == (w.A,) and tensions[0][0].fragilized_stage == "application"
    refs = svc.get_inference_basis_refs(db, run_id=context.run_id)
    by_role = {r.ref_role: r for r in refs}
    ids = {c.stage: c.id for c in claims_rows}
    assert by_role["positive_basis"].stage_claim_id == ids["application"]
    assert by_role["positive_basis"].source_observation_id == w.app
    assert by_role["mastery"].stage_claim_id == ids["mastery"] and by_role["mastery"].source_transfer_id == w.tr
    assert by_role["tension"].tension_id == tensions[0][0].id and by_role["tension"].stage_claim_id is None
    assert by_role["validation"].source_revalidation_id == w.rv and by_role["validation"].stage_claim_id is None
    assert len(refs) == 6


def test_pg_first_inference_non_etabli_and_cause_rules(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    with pytest.raises(InvalidInferenceDecision, match="première inférence"):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="new_user_evidence"))
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=decision("non_etabli", claims()))
    db.commit()
    assert _cache(engine)["current_stage"] == "non_etabli" and _cache(engine)["state_generation"] == 1


def test_pg_first_inference_cannot_hold_a_stage_above_its_claims(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    d = decision("application", claims("comprehension"), [pos("comprehension", w.comp),
                                                           ref("tension", "observation", w.k, tension_key="t1")],
                 context={"motif": "levier"}, tensions=[tension(stage="comprehension")])
    with pytest.raises(InvalidInferenceDecision, match="au-dessus de la plus haute claim"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=d)


# --- E. provenance ------------------------------------------------------------------------

def test_pg_positive_basis_must_be_a_supportive_observation_of_the_snapshot(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    outside = _t3(Sessions, w.tx.id, app(w.A)).id  # jamais dans le snapshot de L1
    for source, match in ((w.k, "contradictory"), (w.k_only, "contradictory"), (outside, "hors du snapshot"),
                          (uuid.uuid4(), "hors du snapshot")):
        with pytest.raises(InvalidInferenceBasisReference, match=match):
            svc.complete_competency_inference(db, run_id=context.run_id,
                                              decision=decision("application", refs=[pos("application", source)]))
        db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w))


@pytest.mark.parametrize("kind", ["dependency", "transfer", "revalidation"])
def test_pg_relation_refs_must_belong_to_the_parent_t5(engine, Sessions, db, world, kind):
    w = world
    context = _start(Sessions, w.t5)
    other = _start_t5(Sessions, w.tx.id)  # autre run T5 (candidat) avec ses propres relations
    with Sessions() as s:
        foreign = {
            "dependency": lambda: t5.add_dependency(
                s, run_id=other, target_observation_id=w.comp, source_kind="observation",
                source_observation_id=w.disc, dependency_type="dependent", scope_mode="localized",
                dependency_basis={"why": "x"}, capability_membership_ids=[w.B]),
            "transfer": lambda: t5.add_transfer(s, run_id=other, source_observation_id=w.comp,
                                                target_observation_id=w.app, scope_mode="competency_only",
                                                transfer_basis={"why": "x"}),
            "revalidation": lambda: t5.add_revalidation(s, run_id=other, source_contradiction_observation_id=w.k,
                                                        target_supportive_observation_id=w.app,
                                                        scope_mode="competency_only", revalidation_basis={"w": 1}),
        }[kind]().id
        s.commit()
    own = {"dependency": w.dep, "transfer": w.tr, "revalidation": w.rv}[kind]
    with pytest.raises(InvalidInferenceBasisReference, match="n'appartient pas au run T5"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(
            w, refs=[pos("application", w.app), ref("transition", kind, foreign)]))
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(
        w, refs=[pos("application", w.app), ref("validation", kind, own)], needs=[{"besoin": "x"}]))
    db.commit()
    stored = svc.get_inference_basis_refs(db, run_id=context.run_id)
    assert {getattr(r, f"source_{kind}_id") for r in stored if r.ref_role == "validation"} == {own}


def test_pg_relations_stay_provenance_never_extra_evidence(engine, Sessions, db, world):
    """Une relation T5 ne soutient jamais directement une claim."""
    w = world
    context = _start(Sessions, w.t5)
    with pytest.raises(InvalidInferenceBasisReference, match="jamais une preuve"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=decision(
            "mastery", refs=[pos("mastery", w.app), ref("positive_basis", "transfer", w.tr, claim_stage="mastery")]))


# --- F. tensions -------------------------------------------------------------------------

def _tension_decision(w, t, sources, *, highest="application"):
    refs = [pos(highest, {"application": w.app, "comprehension": w.comp}[highest])]
    refs += [ref("tension", kind, source, tension_key=t.tension_key) for kind, source in sources]
    return decision(highest, refs=refs, tensions=[t])


@pytest.mark.parametrize("memberships, sources, ok", [
    (("A",), [("observation", "k")], True),
    (("A",), [("transfer", "tr")], True),
    (("B",), [("revalidation", "rv")], True),
    (("B",), [("dependency", "dep")], True),
    (("A", "B"), [("observation", "k"), ("observation", "kB")], True),
    (("A",), [("observation", "kB")], False),
    (("A",), [("observation", "k_only")], False),
    (("A",), [("revalidation", "rv")], False),
    (("A", "B"), [("observation", "k")], False),
])
def test_pg_localized_tension_scope_must_be_covered_by_its_sources(engine, Sessions, db, world, memberships,
                                                                    sources, ok):
    w = world
    context = _start(Sessions, w.t5)
    t = tension(mode="localized", memberships=tuple(getattr(w, m) for m in memberships))
    d = _tension_decision(w, t, [(kind, getattr(w, name)) for kind, name in sources])
    if ok:
        svc.complete_competency_inference(db, run_id=context.run_id, decision=d)
        db.commit()
        assert _run(engine, context.run_id)["tension_state"] == "open"
    else:
        with pytest.raises(InvalidInferenceTension, match="hors du périmètre réel"):
            svc.complete_competency_inference(db, run_id=context.run_id, decision=d)


def test_pg_tension_memberships_must_belong_to_the_release_and_competency(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    other_release = _taxonomy(Sessions, ("C7_A",), activate=False)
    for membership, match in ((other_release.m["C7_A"], "autre release"), (w.tx.m["C8_A"], "hors de C7"),
                              (uuid.uuid4(), "inconnu")):
        t = tension(mode="localized", memberships=(membership,))
        with pytest.raises(InvalidInferenceTension, match=match):
            svc.complete_competency_inference(db, run_id=context.run_id,
                                              decision=_tension_decision(w, t, [("observation", w.k)]))
        db.rollback()


@pytest.mark.parametrize("mode", ["whole_competency", "competency_only"])
def test_pg_non_localized_tensions_accept_any_dossier_source(engine, Sessions, db, world, mode):
    w = world
    context = _start(Sessions, w.t5)
    svc.complete_competency_inference(db, run_id=context.run_id, decision=_tension_decision(
        w, tension(mode=mode), [("observation", w.k_only)]))
    db.commit()
    assert svc.get_inference_tensions(db, run_id=context.run_id)[0][1] == ()


def test_pg_a_tension_needs_a_defensible_positive_claim(engine, Sessions, db, world):
    """Première inférence, Application not_established : aucune tension sur
    Application. Avec un predecessor Application établi : recevable."""
    w = world
    context = _start(Sessions, w.t5)
    d = decision("comprehension", refs=[pos("comprehension", w.comp),
                                         ref("tension", "observation", w.k, tension_key="t1")],
                 tensions=[tension(stage="application")])
    with pytest.raises(InvalidInferenceTension, match="aucune prétention à fragiliser"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=d)
    db.rollback()
    _complete(Sessions, context.run_id, _app_decision(w))
    second = _start(Sessions, w.t5, state_decision_version="state-2")
    revised = dataclasses.replace(d, transition_cause="pedagogical_reinterpretation",
                                  basis_refs=(*d.basis_refs, ref("transition", "observation", w.k)))
    svc.complete_competency_inference(db, run_id=second.run_id, decision=revised)
    db.commit()
    assert _run(engine, second.run_id)["transition"] == "revised_down"


def test_pg_tension_state_is_derived(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w))
    db.commit()
    assert _run(engine, context.run_id)["tension_state"] == "none" and _cache(engine)["tension_state"] == "none"
    assert "tension_state" not in {f.name for f in dataclasses.fields(InferenceDecision)}


# --- G. transitions avec predecessor ---------------------------------------------------

def _second(Sessions, w, first_decision=None, *, new=None, **start):
    """Active une première inférence puis démarre un candidat successeur :
    sur un nouveau dossier (observations `new`) ou sous une autre
    spécification."""
    first = _activate_first(Sessions, w, first_decision)
    parent, created = (w.t5, []) if new is None else _new_t5(Sessions, w, *new)
    if new is None and not start:
        start = {"state_decision_version": "state-2"}
    return first, _start(Sessions, parent, **start), created


def test_pg_maintained_increments_the_cache_generation(engine, Sessions, db, world):
    w = world
    first, context, _ = _second(Sessions, w)
    with pytest.raises(InvalidInferenceDecision, match="transition_cause obligatoire"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w))
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id,
                                      decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.commit()
    row = _run(engine, context.run_id)
    assert (row["previous_stage"], row["current_stage"], row["transition"], row["transition_cause"]) == (
        "application", "application", "maintained", "pedagogical_reinterpretation")
    assert _cache(engine)["state_generation"] == 2 and _cache(engine)["active_inference_run_id"] == context.run_id
    assert _state(engine, first) == ("completed", "superseded")


def test_pg_upgraded_requires_the_arrival_claim_and_a_transition_ref(engine, Sessions, db, world):
    w = world
    first_decision = decision("comprehension", refs=[pos("comprehension", w.comp)])
    _, context, (new,) = _second(Sessions, w, first_decision, new=[app(w.A)])
    up = decision("application", refs=[pos("application", new)], cause="new_user_evidence")
    with pytest.raises(InvalidInferenceDecision, match="sans ref transition"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=up)
    db.rollback()
    held = decision("application", claims("comprehension"), [pos("comprehension", w.comp),
                                                              ref("transition", "observation", new)],
                    cause="new_user_evidence")
    with pytest.raises(InvalidInferenceDecision, match="upgraded vers application sans claim application"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=held)
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=dataclasses.replace(
        up, basis_refs=(*up.basis_refs, ref("transition", "observation", new))))
    db.commit()
    assert (_run(engine, context.run_id)["previous_stage"], _run(engine, context.run_id)["transition"]) == (
        "comprehension", "upgraded")


def test_pg_a_jump_is_structurally_allowed(engine, Sessions, db, world):
    w = world
    _, context, (new,) = _second(Sessions, w, decision("discovery", refs=[pos("discovery", w.disc)]),
                                 new=[app(w.A)])
    svc.complete_competency_inference(db, run_id=context.run_id, decision=decision(
        "application", refs=[pos("application", new), ref("transition", "observation", new)],
        cause="new_user_evidence"))
    db.commit()
    row = _run(engine, context.run_id)
    assert (row["previous_stage"], row["current_stage"], row["transition"]) == ("discovery", "application",
                                                                                 "upgraded")
    assert _count(engine, "competency_inference_runs") == 2  # aucun passage intermédiaire créé


def test_pg_revised_down_requires_a_positive_basis_for_the_arrival_stage(engine, Sessions, db, world):
    w = world
    _, context, (new,) = _second(Sessions, w, new=[contra(w.A)])
    transition = ref("transition", "observation", new)
    by_elimination = decision("comprehension", claims(), [transition], cause="new_user_evidence")
    with pytest.raises(InvalidInferenceDecision):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=by_elimination)
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=decision(
        "comprehension", refs=[pos("comprehension", w.comp), transition], cause="new_user_evidence"))
    db.commit()
    assert _run(engine, context.run_id)["transition"] == "revised_down"


@pytest.mark.parametrize("cause, claim_set, ok", [
    ("new_user_evidence", claims(), False),
    ("evidence_integrity_change", claims(), True),
    ("pedagogical_reinterpretation", claims(), True),
    ("pedagogical_reinterpretation", claims("discovery"), False),
])
def test_pg_revision_to_non_etabli(engine, Sessions, db, world, cause, claim_set, ok):
    w = world
    _, context, (new,) = _second(Sessions, w, new=[contra(w.A)], state_decision_version="state-2")
    refs = [ref("transition", "observation", new)]
    if claim_set != claims():
        refs.append(pos("discovery", w.disc))
    d = decision("non_etabli", claim_set, refs, cause=cause)
    if ok:
        svc.complete_competency_inference(db, run_id=context.run_id, decision=d)
        db.commit()
        assert (_run(engine, context.run_id)["transition"], _cache(engine)["current_stage"]) == (
            "revised_down", "non_etabli")
    else:
        with pytest.raises(InvalidInferenceDecision):
            svc.complete_competency_inference(db, run_id=context.run_id, decision=d)


def _held(w, new, **overrides):
    """Maintien d'Application sous incertitude : base positive la plus haute
    = Comprehension, tension ouverte sur Application, contexte non résolu."""
    kwargs = {"claim_set": claims("comprehension"),
              "refs": [pos("comprehension", w.comp), ref("tension", "observation", new, tension_key="levier")],
              "cause": "new_user_evidence", "context": {"motif": "levier contredit, non encore résolu"},
              "tensions": [tension("levier", stage="application")]}
    kwargs.update(overrides)
    return decision("application", **kwargs)


def test_pg_maintenance_under_tension(engine, Sessions, db, world):
    w = world
    _, context, (new,) = _second(Sessions, w, new=[contra(w.A)])
    for bad, match in ((_held(w, new, tensions=[], refs=[pos("comprehension", w.comp)]), "tension ouverte"),
                       (_held(w, new, context=None), "unresolved_revision_context")):
        with pytest.raises(InvalidInferenceDecision, match=match):
            svc.complete_competency_inference(db, run_id=context.run_id, decision=bad)
        db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=_held(w, new))
    db.commit()
    row = _run(engine, context.run_id)
    assert (row["current_stage"], row["transition"], row["tension_state"]) == ("application", "maintained", "open")
    assert row["unresolved_revision_context"] == {"motif": "levier contredit, non encore résolu"}


def test_pg_new_user_evidence_requires_a_new_observation(engine, Sessions, db, world):
    w = world
    _, context, _ = _second(Sessions, w)  # même dossier, autre spécification
    with pytest.raises(InvalidInferenceDecision, match="new_user_evidence sans aucune observation"):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="new_user_evidence"))
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id,
                                      decision=_app_decision(w, cause="evidence_integrity_change"))


def test_pg_pedagogical_reinterpretation_requires_a_specification_change(engine, Sessions, db, world):
    w = world
    _, context, _ = _second(Sessions, w, new=[sup(w.A)])  # nouveau dossier, mêmes spécifications
    with pytest.raises(InvalidInferenceDecision, match="aucune spécification différente"):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w, cause="new_user_evidence"))


def test_pg_anti_oscillation(engine, Sessions, db, world):
    """Predecessor maintenu sous révision non résolue ; la remontée par
    new_user_evidence exige une observation nouvelle citée par une ref
    positive_basis / transition. Garde STRUCTURELLE : elle ne dit pas que la
    preuve résout le motif (T6-C)."""
    w = world
    # R1 : Application ; R2 : révision vers Comprehension, tension ouverte.
    first, revise, (k_new,) = _second(Sessions, w, new=[contra(w.A)])
    _complete(Sessions, revise.run_id, decision(
        "comprehension", refs=[pos("comprehension", w.comp), ref("transition", "observation", k_new),
                               ref("tension", "observation", k_new, tension_key="levier")],
        cause="new_user_evidence", context={"motif": "levier"}, tensions=[tension("levier", stage="application")]))
    # R3 sur un dossier avec une observation nouvelle.
    l3, (fresh,) = _new_t5(Sessions, w, app(w.A))
    rebound = _start(Sessions, l3)
    old_only = decision("application", refs=[pos("application", w.app), ref("transition", "observation", w.app)],
                        cause="new_user_evidence")
    with pytest.raises(InvalidInferenceDecision, match="anciennes preuves seules"):
        svc.complete_competency_inference(db, run_id=rebound.run_id, decision=old_only)
    db.rollback()
    svc.complete_competency_inference(db, run_id=rebound.run_id, decision=decision(
        "application", refs=[pos("application", w.app), ref("transition", "observation", fresh)],
        cause="new_user_evidence"))
    db.commit()
    assert (_run(engine, rebound.run_id)["transition"], _cache(engine)["state_generation"]) == ("upgraded", 3)


def test_pg_anti_oscillation_without_any_new_observation(engine, Sessions, db, world):
    w = world
    first, revise, (k_new,) = _second(Sessions, w, new=[contra(w.A)])
    _complete(Sessions, revise.run_id, decision(
        "comprehension", refs=[pos("comprehension", w.comp), ref("transition", "observation", k_new),
                               ref("tension", "observation", k_new, tension_key="levier")],
        cause="new_user_evidence", context={"motif": "levier"}, tensions=[tension("levier", stage="application")]))
    parent = _run(engine, revise.run_id)["longitudinal_assessment_run_id"]
    again = _start(Sessions, parent, evaluator_version="e-2")
    with pytest.raises(InvalidInferenceDecision, match="new_user_evidence sans aucune observation"):
        svc.complete_competency_inference(db, run_id=again.run_id, decision=decision(
            "application", refs=[pos("application", w.app), ref("transition", "observation", k_new)],
            cause="new_user_evidence"))


# --- H. revérification sous verrou -------------------------------------------------------

@pytest.mark.parametrize("case", [*CHAIN_BREAKS, "t5_superseded"])
def test_pg_complete_refuses_a_stale_input_without_any_mutation(engine, Sessions, db, world, case):
    w = world
    first = _activate_first(Sessions, w)
    context = _start(Sessions, w.t5, state_decision_version="state-2")
    before = _t6_snapshot(engine)
    _break_chain(Sessions, engine, w, case)
    with pytest.raises(StaleInferenceInput):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.rollback()
    assert _t6_snapshot(engine) == before
    assert _state(engine, first) == ("completed", "active") and _state(engine, context.run_id) == (
        "running", "candidate")


def test_pg_an_older_candidate_never_overwrites_a_newer_state(engine, Sessions, db, world):
    w = world
    first = _activate_first(Sessions, w)
    a = _start(Sessions, w.t5, state_decision_version="state-A")
    b = _start(Sessions, w.t5, state_decision_version="state-B")
    _complete(Sessions, a.run_id, _app_decision(w, cause="pedagogical_reinterpretation"))
    before = _t6_snapshot(engine)
    with pytest.raises(StaleInferencePredecessor):
        svc.complete_competency_inference(db, run_id=b.run_id,
                                          decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.rollback()
    assert _t6_snapshot(engine) == before
    assert [_state(engine, r)[1] for r in (first, a.run_id, b.run_id)] == ["superseded", "active", "candidate"]


def test_pg_first_candidates_race(engine, Sessions, db, world):
    """Deux premiers candidats : le second voit un active là où il
    attendait aucun predecessor."""
    w = world
    a = _start(Sessions, w.t5)
    b = _start(Sessions, w.t5, evaluator_version="e-2")
    _complete(Sessions, a.run_id, _app_decision(w))
    with pytest.raises(StaleInferencePredecessor):
        svc.complete_competency_inference(db, run_id=b.run_id, decision=_app_decision(w))


def test_pg_rewritten_input_fingerprint_is_stale(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    _exec(engine, "UPDATE competency_inference_runs SET input_fingerprint = 'x' WHERE id = :i", i=context.run_id)
    with pytest.raises(StaleInferenceInput, match="input_fingerprint"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w))


# --- I. activation atomique, rollback, cache ------------------------------------------------

def test_pg_activation_is_atomic(engine, Sessions, db, world):
    w = world
    first, context, _ = _second(Sessions, w)
    svc.complete_competency_inference(db, run_id=context.run_id,
                                      decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    # Avant COMMIT : l'état commité est intégralement l'ancien.
    assert _state(engine, first) == ("completed", "active") and _cache(engine)["active_inference_run_id"] == first
    db.commit()
    assert _state(engine, first) == ("completed", "superseded")
    assert _state(engine, context.run_id) == ("completed", "active")
    assert _cache(engine)["active_inference_run_id"] == context.run_id and _cache(engine)["state_generation"] == 2
    assert _count(engine, "competency_inference_runs", "interpretation_status = 'active'") == 1
    # Historique conservé : aucune suppression.
    assert _count(engine, "competency_stage_claims", "inference_run_id = :r", r=first) == 4


def test_pg_caller_rollback_undoes_everything(engine, Sessions, db, world):
    w = world
    first, context, _ = _second(Sessions, w)
    before = _t6_snapshot(engine)
    svc.complete_competency_inference(db, run_id=context.run_id,
                                      decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.rollback()  # erreur tardive de l'appelant
    assert _t6_snapshot(engine) == before
    assert _state(engine, first) == ("completed", "active") and _state(engine, context.run_id) == (
        "running", "candidate")


def test_pg_late_failure_inside_complete_leaves_nothing(engine, Sessions, db, world, monkeypatch):
    """Échec APRÈS les écritures des enfants et la supersession (au moment
    d'activer) : l'appelant annule, aucune ligne partielle ne subsiste."""
    w = world
    first, context, _ = _second(Sessions, w)
    before = _t6_snapshot(engine)
    calls = itertools.count()
    real = svc._utcnow

    def failing():
        if next(calls) == 1:
            raise RuntimeError("panne tardive")
        return real()

    monkeypatch.setattr(svc, "_utcnow", failing)
    with pytest.raises(RuntimeError):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    assert db.execute(sa.text("SELECT interpretation_status FROM competency_inference_runs WHERE id = :i"),
                      {"i": first}).scalar_one() == "superseded"  # déjà flushé dans la transaction
    db.rollback()
    assert _t6_snapshot(engine) == before


@pytest.mark.parametrize("corruption", ["wrong_run", "stage", "tension", "generation", "missing", "orphan"])
def test_pg_cache_corruption_is_never_repaired(engine, Sessions, db, world, corruption):
    w = world
    first = _activate_first(Sessions, w)
    if corruption == "wrong_run":
        other = _start(Sessions, w.t5, evaluator_version="e-2").run_id
        _exec(engine, "UPDATE user_competency_states SET active_inference_run_id = :r", r=other)
    elif corruption == "stage":
        _exec(engine, "UPDATE user_competency_states SET current_stage = 'mastery'")
    elif corruption == "tension":
        _exec(engine, "UPDATE user_competency_states SET tension_state = 'open'")
    elif corruption == "generation":
        _exec(engine, "UPDATE user_competency_states SET state_generation = 0")
    elif corruption == "missing":
        _exec(engine, "DELETE FROM user_competency_states")
    else:
        _exec(engine, "DELETE FROM user_competency_states")
        _exec(engine, "UPDATE competency_inference_runs SET interpretation_status = 'superseded'")
        _exec(engine, "INSERT INTO user_competency_states VALUES (:u, 'C7', :r, 'application', 'none', 1, now())",
              u=USER, r=first)
    before = _t6_snapshot(engine)
    candidate = None
    if corruption != "wrong_run":
        with Sessions() as s:
            try:
                candidate = svc.start_competency_inference(s, **_start_kwargs(w.t5, evaluator_version="e-3"))
                pytest.fail("start aurait dû refuser le cache incohérent")
            except InvalidInferenceState:
                pass
    with pytest.raises(InvalidInferenceState):
        svc.get_validated_user_competency_state(db, user_id=USER, competency_code="C7")
    assert candidate is None and _t6_snapshot(engine) == before


def test_pg_complete_refuses_a_corrupt_cache(engine, Sessions, db, world):
    w = world
    _, context, _ = _second(Sessions, w)
    _exec(engine, "UPDATE user_competency_states SET current_stage = 'discovery'")
    before = _t6_snapshot(engine)
    with pytest.raises(InvalidInferenceState, match="divergent"):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.rollback()
    assert _t6_snapshot(engine) == before


# --- J. lectures -----------------------------------------------------------------------

def test_pg_validated_state(engine, Sessions, db, world):
    w = world
    assert svc.get_validated_user_competency_state(db, user_id=USER, competency_code="C7") is None
    assert svc.get_user_competency_state(db, user_id=USER, competency_code="C7") is None
    first = _activate_first(Sessions, w)
    state = svc.get_validated_user_competency_state(db, user_id=USER, competency_code="C7")
    assert state == ValidatedCompetencyState(user_id=USER, competency_code="C7", current_stage="application",
                                             tension_state="none", active_inference_run_id=first,
                                             longitudinal_assessment_run_id=w.t5, state_generation=1)
    assert svc.get_validated_user_competency_state(db, user_id=USER, competency_code="C8") is None
    with pytest.raises(UserNotFound):
        svc.get_validated_user_competency_state(db, user_id="inconnu", competency_code="C7")


@pytest.mark.parametrize("case", [*CHAIN_BREAKS, "t5_superseded"])
def test_pg_stale_chain_is_reported_never_repaired(engine, Sessions, db, world, case):
    w = world
    _activate_first(Sessions, w)
    _break_chain(Sessions, engine, w, case)
    before = _t6_snapshot(engine)
    if case == "release_retired":
        # Choix documenté : la lecture validée ne revérifie pas la release
        # (la chaîne pédagogique elle-même est intacte).
        assert svc.get_validated_user_competency_state(db, user_id=USER, competency_code="C7") is not None
        return
    with pytest.raises(StaleInferenceChain):
        svc.get_validated_user_competency_state(db, user_id=USER, competency_code="C7")
    # La ligne brute reste lisible (inspection technique) ; rien n'a changé.
    raw = svc.get_user_competency_state(db, user_id=USER, competency_code="C7")
    assert raw is not None and raw.current_stage == "application"
    assert not db.new and not db.dirty and not db.deleted
    assert _t6_snapshot(engine) == before


def test_pg_read_order_is_deterministic(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    refs = [pos("application", w.app), ref("validation", "revalidation", w.rv), ref("transition", "observation", w.k),
            ref("tension", "observation", w.k, tension_key="b"), ref("tension", "observation", w.kB, tension_key="a"),
            ref("confidence", "observation", w.comp, claim_stage="application", confidence_dimension="coverage")]
    svc.complete_competency_inference(db, run_id=context.run_id, decision=decision(
        "application", claim_set=tuple(reversed(claims("application"))), refs=list(reversed(refs)),
        needs=[{"b": 1}], tensions=[tension("b"), tension("a", summary="autre")]))
    db.commit()
    assert [c.stage for c in svc.get_stage_claims(db, run_id=context.run_id)] == list(STAGES)
    listed = svc.get_inference_basis_refs(db, run_id=context.run_id)
    assert [r.ref_role for r in listed] == sorted(r.ref_role for r in listed)
    assert listed == svc.get_inference_basis_refs(db, run_id=context.run_id)
    tensions = svc.get_inference_tensions(db, run_id=context.run_id)
    assert [t.id for t, _ in tensions] == [t.id for t, _ in sorted(tensions, key=lambda x: (x[0].created_at,
                                                                                               str(x[0].id)))]
    assert svc.get_active_competency_inference(db, user_id=USER, competency_code="C7").id == context.run_id
    assert svc.get_competency_inference(db, run_id=context.run_id).id == context.run_id
    for fn in (svc.get_stage_claims, svc.get_inference_tensions, svc.get_inference_basis_refs):
        with pytest.raises(InferenceRunNotFound):
            fn(db, run_id=uuid.uuid4())


def test_pg_output_fingerprint_ignores_order_and_generated_uuids(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    refs = [pos("application", w.app), ref("tension", "observation", w.k, tension_key="x"),
            ref("tension", "transfer", w.tr, tension_key="y"),
            ref("confidence", "transfer", w.tr, claim_stage="application", confidence_dimension="independence")]
    one = decision("application", refs=refs, tensions=[tension("x", mode="localized", memberships=(w.A,)),
                                                       tension("y", summary="autre")])
    two = decision("application", claim_set=tuple(reversed(claims("application"))), refs=[
        refs[3], ref("tension", "transfer", w.tr, tension_key="q"), refs[0],
        ref("tension", "observation", w.k, tension_key="p")],
        tensions=[tension("q", summary="autre"), tension("p", mode="localized", memberships=(w.A,))])
    prints = []
    for d in (one, two, one):
        prints.append(svc.complete_competency_inference(db, run_id=context.run_id, decision=d).output_fingerprint)
        db.rollback()
    assert len(set(prints)) == 1
    changed = dataclasses.replace(one, tensions=(tension("x", mode="localized", memberships=(w.A,)),
                                                 tension("y", mode="competency_only", summary="autre")))
    assert svc.complete_competency_inference(db, run_id=context.run_id,
                                             decision=changed).output_fingerprint != prints[0]


# --- K. transactions, verrous, concurrence ---------------------------------------------------

def test_pg_service_never_commits_nor_rolls_back(engine, Sessions, db, world):
    w = world
    events = []
    listeners = {name: (lambda *a, _n=name: events.append(_n))
                 for name in ("commit", "rollback", "savepoint", "rollback_savepoint", "release_savepoint")}
    for name, fn in listeners.items():
        sa.event.listen(engine, name, fn)
    try:
        context = svc.start_competency_inference(db, **_start_kwargs(w.t5))
        with pytest.raises(DuplicateInference):
            svc.start_competency_inference(db, **_start_kwargs(w.t5))
        svc.get_inference_context(db, run_id=context.run_id)
        with pytest.raises(InvalidStageClaim):
            svc.complete_competency_inference(db, run_id=context.run_id, decision=decision("application"))
        svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w))
        other = svc.start_competency_inference(db, **_start_kwargs(w.t5, evaluator_version="e-2"))
        svc.fail_competency_inference(db, run_id=other.run_id)
        svc.get_validated_user_competency_state(db, user_id=USER, competency_code="C7")
        for read in (svc.get_stage_claims, svc.get_inference_tensions, svc.get_inference_basis_refs):
            read(db, run_id=context.run_id)
    finally:
        for name, fn in listeners.items():
            sa.event.remove(engine, name, fn)
    assert "commit" not in events and "rollback" not in events
    assert events == ["savepoint", "release_savepoint", "savepoint", "release_savepoint"]
    db.rollback()
    assert _count(engine, "competency_inference_runs") == 0


def test_pg_complete_lock_order(engine, Sessions, db, world):
    w = world
    first, context, _ = _second(Sessions, w)
    statements, record = _recording(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    locks = []
    for sql, _ in statements:
        if sql.startswith("SELECT pg_advisory_xact_lock("):
            locks.append(("advisory", None))
        elif " FOR " in sql:
            table = sql.split(" FROM ", 1)[1].split()[0]
            locks.append((table, sql.rsplit(" FOR ", 1)[1]))
    assert locks == [
        ("advisory", None),
        ("competency_inference_runs", "NO KEY UPDATE"),
        ("longitudinal_assessment_runs", "SHARE"),
        ("pedagogical_taxonomy_releases", "SHARE"),
        ("cognitive_events", "SHARE"),
        ("observation_evaluation_runs", "SHARE"),
        ("pedagogical_observations", "SHARE"),
        ("competency_inference_runs", "NO KEY UPDATE"),
        ("user_competency_states", "UPDATE"),
    ]
    # Première mutation APRÈS toutes les lectures verrouillées ; ancien
    # active superseded avant l'activation, cache en dernier.
    writes = [sql for sql, _ in statements if sql.startswith(("INSERT", "UPDATE"))]
    assert [s.split()[0] + " " + s.split()[1 if s.startswith("UPDATE") else 2] for s in writes] == [
        "INSERT competency_stage_claims", "INSERT competency_inference_basis_refs",
        "UPDATE competency_inference_runs", "UPDATE competency_inference_runs", "UPDATE competency_inference_runs",
        "UPDATE user_competency_states"]
    first_write = next(i for i, (sql, _) in enumerate(statements) if sql.startswith(("INSERT", "UPDATE")))
    last_lock = max(i for i, (sql, _) in enumerate(statements) if " FOR " in sql)
    assert last_lock < first_write
    db.rollback()


def test_pg_advisory_lock_is_transaction_level_and_namespaced(engine, Sessions, world):
    w = world
    context = _start(Sessions, w.t5)
    key = svc._activation_lock_key(USER, "C7") & 0xFFFFFFFFFFFFFFFF
    expected = {"classid": key >> 32, "objid": key & 0xFFFFFFFF, "objsubid": 1, "granted": True}
    with Sessions() as session:
        svc.complete_competency_inference(session, run_id=context.run_id, decision=_app_decision(w))
        (pid,) = _pids(session)
        assert _advisory_locks(engine, pid) == [expected]
        session.commit()
        assert _advisory_locks(engine, pid) == []


def test_pg_concurrent_completions_on_the_same_predecessor(engine, Sessions, world):
    """B attend le verrou consultatif détenu par A ; après le COMMIT de A,
    B voit un autre active : StaleInferencePredecessor, jamais d'écrasement."""
    w = world
    first = _activate_first(Sessions, w)
    a = _start(Sessions, w.t5, state_decision_version="state-A")
    b = _start(Sessions, w.t5, state_decision_version="state-B")
    reinterpretation = _app_decision(w, cause="pedagogical_reinterpretation")
    with Sessions() as holder, Sessions() as waiter:
        svc.complete_competency_inference(holder, run_id=a.run_id, decision=reinterpretation)
        _, error = _blocked(engine, holder, waiter, lambda s: svc.complete_competency_inference(
            s, run_id=b.run_id, decision=reinterpretation), waiting_on=ON_ADVISORY)
    assert isinstance(error, StaleInferencePredecessor)
    assert [_state(engine, r)[1] for r in (first, a.run_id, b.run_id)] == ["superseded", "active", "candidate"]
    assert _cache(engine)["active_inference_run_id"] == a.run_id and _cache(engine)["state_generation"] == 2


def test_pg_many_concurrent_completions_end_with_exactly_one_active(engine, Sessions, world):
    w = world
    candidates = [_start(Sessions, w.t5, evaluator_version=f"e-{i}").run_id for i in range(5)]
    barrier = threading.Barrier(len(candidates))
    outcomes = []

    def work(run_id):
        with Sessions() as session:
            barrier.wait()
            try:
                svc.complete_competency_inference(session, run_id=run_id, decision=_app_decision(w))
                session.commit()
                outcomes.append("ok")
            except StaleInferencePredecessor:
                session.rollback()
                outcomes.append("stale")

    threads = [threading.Thread(target=work, args=(r,)) for r in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["ok"] + ["stale"] * 4
    assert _count(engine, "competency_inference_runs", "interpretation_status = 'active'") == 1
    active = _rows(engine, "SELECT id FROM competency_inference_runs WHERE interpretation_status = 'active'")[0]
    assert _cache(engine)["active_inference_run_id"] == active["id"] and _cache(engine)["state_generation"] == 1


def test_pg_t5_activation_waits_for_an_in_flight_t6_completion(engine, Sessions, db, world):
    """T6 détient le parent T5 en FOR SHARE : une activation T5 concurrente
    attend le COMMIT T6 (puis supersede L1 : l'état T6 devient stale)."""
    w = world
    context = _start(Sessions, w.t5)
    _t3(Sessions, w.tx.id, sup(w.A))
    l2 = _start_t5(Sessions, w.tx.id)
    with Sessions() as holder, Sessions() as waiter:
        svc.complete_competency_inference(holder, run_id=context.run_id, decision=_app_decision(w))
        _, error = _blocked(engine, holder, waiter,
                            lambda s: t5.complete_longitudinal_assessment(s, run_id=l2), waiting_on=ON_T5_RUN)
    assert error is None
    assert _state(engine, context.run_id) == ("completed", "active")
    with pytest.raises(StaleInferenceChain):
        svc.get_validated_user_competency_state(db, user_id=USER, competency_code="C7")


def test_pg_t6_completion_waits_for_an_in_flight_t5_activation_then_is_stale(engine, Sessions, world):
    w = world
    context = _start(Sessions, w.t5)
    _t3(Sessions, w.tx.id, sup(w.A))
    l2 = _start_t5(Sessions, w.tx.id)
    with Sessions() as holder, Sessions() as waiter:
        t5.complete_longitudinal_assessment(holder, run_id=l2)
        _, error = _blocked(engine, holder, waiter, lambda s: svc.complete_competency_inference(
            s, run_id=context.run_id, decision=_app_decision(w)), waiting_on=ON_T5_RUN)
    assert isinstance(error, StaleInferenceInput)
    assert _state(engine, context.run_id) == ("running", "candidate") and _cache(engine) is None


def test_pg_t6_completion_waits_for_an_in_flight_invalidation_then_is_stale(engine, Sessions, world):
    w = world
    context = _start(Sessions, w.t5)
    with Sessions() as holder, Sessions() as waiter:
        obs.invalidate_observation(holder, observation_id=w.app, reason="erreur d'extraction")
        _, error = _blocked(engine, holder, waiter, lambda s: svc.complete_competency_inference(
            s, run_id=context.run_id, decision=_app_decision(w)), waiting_on="SELECT pedagogical_observations.id")
    assert isinstance(error, StaleInferenceInput)
    assert _state(engine, context.run_id) == ("running", "candidate")


def test_pg_t3_reevaluation_waits_for_an_in_flight_t6_completion(engine, Sessions, world):
    """Événement AVANT run T3 dans l'ordre T6 : une réévaluation T3
    concurrente (run candidat -> événement -> run active) attend, sans cycle."""
    w = world
    context = _start(Sessions, w.t5)
    with Sessions() as session:
        rerun = obs.start_evaluation_run(session, **t3_kwargs(w.app_t3.event,
                                                              pedagogical_taxonomy_release_id=w.tx.id)).id
        session.commit()
    with Sessions() as holder, Sessions() as waiter:
        svc.complete_competency_inference(holder, run_id=context.run_id, decision=_app_decision(w))
        _, error = _blocked(engine, holder, waiter, lambda s: obs.complete_evaluation_run(
            s, run_id=rerun, output_fingerprint="o"), waiting_on="SELECT cognitive_events.id")
    assert error is None
    assert _state(engine, context.run_id) == ("completed", "active")
    # La réévaluation commitée ensuite rend l'état stale, jamais réécrit.
    with Sessions() as session:
        with pytest.raises(StaleInferenceChain):
            svc.get_validated_user_competency_state(session, user_id=USER, competency_code="C7")


def test_pg_other_couples_are_not_serialized(engine, Sessions, world):
    w = world
    c7 = _start(Sessions, w.t5)
    c8_obs = _t3(Sessions, w.tx.id, sup(w.tx.m["C8_A"], competency_code="C8")).id
    c8_parent = _start_t5(Sessions, w.tx.id, competency_code="C8")
    _complete_t5(Sessions, c8_parent)
    c8 = _start(Sessions, c8_parent)
    with Sessions() as first, Sessions() as second:
        svc.complete_competency_inference(first, run_id=c7.run_id, decision=_app_decision(w))
        svc.complete_competency_inference(second, run_id=c8.run_id, decision=decision(
            "comprehension", refs=[pos("comprehension", c8_obs)]))
        second.commit()
        first.commit()
    assert _cache(engine, competency_code="C8")["current_stage"] == "comprehension"
    assert _cache(engine, competency_code="C7")["current_stage"] == "application"


def test_pg_t6_writes_join_a_wider_caller_transaction(engine, Sessions, db, world):
    w = world
    context = svc.start_competency_inference(db, **_start_kwargs(w.t5))
    svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w))
    assert _count(engine, "competency_inference_runs") == 0 and _cache(engine) is None
    db.commit()
    assert _state(engine, context.run_id) == ("completed", "active") and _cache(engine)["state_generation"] == 1


def test_pg_upstream_evidence_is_never_modified(engine, Sessions, db, world):
    w = world
    tables = ("cognitive_events", "support_traces", "observation_evaluation_runs", "pedagogical_observations",
              "observation_capabilities", "longitudinal_assessment_runs", "longitudinal_assessment_inputs",
              "observation_dependencies", "observation_transfers", "observation_revalidations")
    before = {t: sorted(map(repr, _rows(engine, f"SELECT * FROM {t}"))) for t in tables}
    _second(Sessions, w)
    svc.get_validated_user_competency_state(db, user_id=USER, competency_code="C7")
    assert {t: sorted(map(repr, _rows(engine, f"SELECT * FROM {t}"))) for t in tables} == before
