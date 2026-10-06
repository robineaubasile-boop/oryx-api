"""Tests de T5-B : service interne transactionnel du dossier longitudinal et
des relations entre observations (core/longitudinal_service.py).

Aucune migration dans ce chantier : le schéma testé est celui produit par
`alembic upgrade head` (= 0008_longitudinal_relations, T5-A, puis
0009_competency_inference_state depuis T6-A, tables vides non utilisées par
ce service). Aucune donnée
n'est seedée : chaque test crée ses releases, définitions, événements,
runs T3 et observations via les services T2-B / T3-B / T4-B.

1. Tests sans base (toujours exécutés) : aucune migration, API publique
   exacte, hiérarchie d'exceptions, aucun commit / rollback, aucune route,
   aucun LLM, aucun stade / confiance / score / « independent », aucune
   preuve créée, validation structurelle complète AVANT tout accès à la
   base, formats canoniques input_fingerprint / scope_fingerprint V1, clé
   consultative déterministe, requêtes de verrouillage.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T1-T5-A) :
   uniquement si ORYX_TEST_DATABASE_URL pointe vers une base DÉDIÉE dont le
   nom contient "test" (schéma public détruit et recréé). Sinon SKIPPÉS :
   rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t5b_test \\
           python -m pytest tests/test_longitudinal_service.py

   Les tests de concurrence attendent, via pg_blocking_pids(), que la
   seconde connexion soit effectivement bloquée par la première — et
   vérifient sur quelle requête (pg_stat_activity) — avant de la libérer ;
   les tests à N threads utilisent une barrière et vérifient l'invariant
   final : aucun sleep() comme synchronisation.
"""
import ast
import hashlib
import inspect
import itertools
import json
import random
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from core import cognitive_capture as cc
from core import longitudinal_service as svc
from core import observation_service as obs
from core import taxonomy_service as tax
from core.longitudinal_service import (
    DuplicateLongitudinalAssessment,
    DuplicateLongitudinalRelation,
    InvalidDependency,
    InvalidLongitudinalPayload,
    InvalidLongitudinalState,
    InvalidRelationScope,
    InvalidRevalidation,
    InvalidTransfer,
    LongitudinalAssessmentNotFound,
    LongitudinalServiceError,
    ObservationNotEligible,
    RelationSourceNotFound,
    StaleLongitudinalSnapshot,
    TaxonomyReleaseNotUsable,
    UserNotFound,
)
from core.models import LongitudinalAssessmentRun, ObservationDependency
from tests.test_cognitive_capture import _contribution as _capture_contribution
from tests.test_cognitive_capture import _open_kwargs as _capture_open_kwargs
from tests.test_migration_0002_analysis_sessions import (
    BASELINE,
    REPO_ROOT,
    T1A,
    _script_directory,
    pg_url,  # noqa: F401 — fixture
)
from tests.test_migration_0003_analysis_session_links import T1B1
from tests.test_migration_0004_drop_company_analyses import T1C2, T5A_TABLES, _code_tokens
from tests.test_migration_0005_cognitive_support_traces import OTHER_USER, T2A, USER, _upgrade_head_with_users
from tests.test_migration_0006_observation_layer import T3A
from tests.test_migration_0007_pedagogical_taxonomy import T4A
from tests.test_migration_0008_longitudinal_relations import (
    DEDUP_UNIQUE,
    DEPENDENCY_TYPES,
    EXECUTION_STATUSES,
    INTERPRETATION_STATUSES,
    ONE_ACTIVE_INDEX,
    SCOPE_MODES,
    SOURCE_KINDS,
    T5A,
    T5A_MODELS,
)
from tests.test_migration_0009_competency_inference_state import T6A
from tests.test_migration_0010_r1b_event_idempotence import R1B
from tests.test_migration_0011_assistant_deliveries import R1C1
from tests.test_migration_0012_decryptage_cognitive_links import R1C2
from tests.test_migration_0004_drop_company_analyses import R1C4, R1C4_FILE
from tests.test_observation_service import (
    INVALID_JSON_VALUES,
    _blocked,
    _NoDB,
    _obs_kwargs,
    _reaches_db,
    _recording,
)
from tests.test_observation_service import _event as _finalized_event
from tests.test_observation_service import _start_kwargs as _t3_start_kwargs
from tests.test_taxonomy_service import _advisory_locks, _def_kwargs, _pids, _rows

SERVICE_PATH = REPO_ROOT / "core" / "longitudinal_service.py"
PUBLIC_API = {
    "start_longitudinal_assessment",
    "add_dependency",
    "add_transfer",
    "add_revalidation",
    "complete_longitudinal_assessment",
    "fail_longitudinal_assessment",
    "get_longitudinal_assessment",
    "get_active_longitudinal_assessment",
    "get_longitudinal_inputs",
    "get_dependencies",
    "get_transfers",
    "get_revalidations",
}
EXCEPTIONS = {
    UserNotFound,
    LongitudinalAssessmentNotFound,
    InvalidLongitudinalPayload,
    InvalidLongitudinalState,
    DuplicateLongitudinalAssessment,
    TaxonomyReleaseNotUsable,
    StaleLongitudinalSnapshot,
    ObservationNotEligible,
    RelationSourceNotFound,
    InvalidDependency,
    InvalidTransfer,
    InvalidRevalidation,
    InvalidRelationScope,
    DuplicateLongitudinalRelation,
}
START_TEXT = ["user_id", "trigger", "dependency_version", "transfer_version", "revalidation_version",
              "relation_schema_version", "assessment_dedup_key"]
INVALID_TEXT = ["", " ", "\t\n", None, 42, b"x", "a\x00b", uuid.UUID(int=1)]
INVALID_UUIDS = [None, "", str(uuid.UUID(int=7)), 1, uuid.UUID(int=7).bytes, uuid.UUID(int=7).int]


class _Mode(str, Enum):
    LOCALIZED = "localized"


def _t5_kwargs(release_id=None, **overrides):
    kwargs = {
        "user_id": USER,
        "competency_code": "C7",
        "trigger": "observation_completed",
        "pedagogical_taxonomy_release_id": uuid.uuid4() if release_id is None else release_id,
        "dependency_version": "dependency-1",
        "transfer_version": "transfer-1",
        "revalidation_version": "revalidation-1",
        "relation_schema_version": "relations-1",
        "assessment_dedup_key": f"t5-{uuid.uuid4()}",
    }
    kwargs.update(overrides)
    return kwargs


def _dep_kwargs(**overrides):
    kwargs = {
        "run_id": uuid.uuid4(),
        "target_observation_id": uuid.uuid4(),
        "source_kind": "observation",
        "source_observation_id": uuid.uuid4(),
        "dependency_type": "dependent",
        "scope_mode": "whole_observation",
        "dependency_basis": {"why": "reprend la décomposition montrée à l'épisode précédent"},
    }
    kwargs.update(overrides)
    return kwargs


def _transfer_kwargs(**overrides):
    kwargs = {
        "run_id": uuid.uuid4(),
        "source_observation_id": uuid.uuid4(),
        "target_observation_id": uuid.uuid4(),
        "scope_mode": "whole_observation",
        "transfer_basis": {"adaptation": "structure de capital différente, raisonnement réorganisé"},
    }
    kwargs.update(overrides)
    return kwargs


def _reval_kwargs(**overrides):
    kwargs = {
        "run_id": uuid.uuid4(),
        "source_contradiction_observation_id": uuid.uuid4(),
        "target_supportive_observation_id": uuid.uuid4(),
        "scope_mode": "whole_observation",
        "revalidation_basis": {"mechanism": "effet de levier sur le ROE, démontré sans aide"},
    }
    kwargs.update(overrides)
    return kwargs


RELATION_CALLS = {
    "add_dependency": (svc.add_dependency, _dep_kwargs, "dependency_basis"),
    "add_transfer": (svc.add_transfer, _transfer_kwargs, "transfer_basis"),
    "add_revalidation": (svc.add_revalidation, _reval_kwargs, "revalidation_basis"),
}


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_no_migration_added_by_t5b():
    """T5-B est service-only : aucune migration ajoutée par T5-B ; les seules
    ajoutées depuis sont 0009 (T6-A, structure de l'inférence de l'état
    C1-C12), 0010 (R1-B, identité idempotente des CognitiveEvents), 0011
    (R1-C1, livraisons des réponses assistant), 0012 (R1-C2, liens
    cognitifs Décrypter) et 0013 (R1-C4, affinité conversationnelle), qui est
    la tête."""
    script = _script_directory()
    assert script.get_heads() == [R1C4]
    assert script.get_revision(R1C4).down_revision == R1C2
    assert script.get_revision(R1C2).down_revision == R1C1
    assert script.get_revision(R1C1).down_revision == R1B
    assert script.get_revision(R1B).down_revision == T6A
    assert script.get_revision(T6A).down_revision == T5A
    revisions = (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A, T6A, R1B, R1C1, R1C2, R1C4)
    assert {rev.revision for rev in script.walk_revisions()} == set(revisions)
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [f"{rev}.py" for rev in revisions[:-1]] + [R1C4_FILE]


def test_public_api_is_exactly_the_twelve_operations():
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == PUBLIC_API
    for name in PUBLIC_API:
        params = list(inspect.signature(getattr(svc, name)).parameters.values())
        assert params[0].name == "db" and params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:]), name
    # Aucune détection, inférence, notation, ni mutation du snapshot.
    for forbidden in ("detect_transfer", "detect_dependency", "detect_revalidation", "infer_stage",
                      "score_history", "assess_mastery", "assess_dependency", "build_longitudinal_history",
                      "activate_longitudinal_run", "add_input", "remove_input", "update_snapshot",
                      "delete_dependency", "delete_transfer", "delete_revalidation", "compute_confidence",
                      "get_or_create_assessment", "add_independence"):
        assert not hasattr(svc, forbidden), forbidden


def test_exact_signatures():
    def params(fn):
        return {name: p.default for name, p in inspect.signature(fn).parameters.items() if name != "db"}

    empty = inspect.Parameter.empty
    assert params(svc.start_longitudinal_assessment) == {name: empty for name in _t5_kwargs()}
    assert params(svc.add_dependency) == {
        "run_id": empty, "target_observation_id": empty, "source_kind": empty, "source_observation_id": None,
        "source_support_trace_id": None, "dependency_type": empty, "scope_mode": empty, "dependency_basis": empty,
        "capability_membership_ids": ()}
    assert params(svc.add_transfer) == {**{name: empty for name in _transfer_kwargs()},
                                        "capability_membership_ids": ()}
    assert params(svc.add_revalidation) == {**{name: empty for name in _reval_kwargs()},
                                            "capability_membership_ids": ()}
    assert params(svc.complete_longitudinal_assessment) == {"run_id": empty}
    assert params(svc.fail_longitudinal_assessment) == {"run_id": empty, "failure_code": None}
    assert params(svc.get_active_longitudinal_assessment) == {"user_id": empty, "competency_code": empty}
    for name in ("get_longitudinal_assessment", "get_longitudinal_inputs", "get_dependencies", "get_transfers",
                 "get_revalidations"):
        assert params(getattr(svc, name)) == {"run_id": empty}, name


def test_snapshot_fingerprints_and_lifecycle_cannot_be_supplied():
    """L'appelant ne choisit ni les observations, ni input_fingerprint, ni
    scope_fingerprint, ni aucun champ de lifecycle."""
    for field in ("observation_ids", "input_fingerprint", "execution_status", "interpretation_status",
                  "started_at", "completed_at", "created_at", "failure_code", "id", "inputs"):
        with pytest.raises(TypeError):
            svc.start_longitudinal_assessment(_NoDB(), **_t5_kwargs(**{field: None}))
    for name, (fn, kwargs, _) in RELATION_CALLS.items():
        for field in ("scope_fingerprint", "id", "created_at", "event_id", "dependency_weight"):
            with pytest.raises(TypeError):
                fn(_NoDB(), **kwargs(**{field: None}))
    with pytest.raises(TypeError):
        svc.complete_longitudinal_assessment(_NoDB(), run_id=uuid.uuid4(), input_fingerprint="x")


def test_exceptions_are_a_small_business_hierarchy():
    public = {o for o in vars(svc).values()
              if isinstance(o, type) and issubclass(o, Exception) and o.__module__ == svc.__name__}
    assert public == EXCEPTIONS | {LongitudinalServiceError}
    for exc in EXCEPTIONS:
        assert exc.__bases__ == (LongitudinalServiceError,), exc
    assert LongitudinalServiceError.__bases__ == (Exception,)
    assert not issubclass(LongitudinalServiceError, (LookupError, ValueError, obs.ObservationServiceError,
                                                     tax.TaxonomyServiceError, cc.CognitiveCaptureError))


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_service_owns_no_transaction_and_has_no_framework_or_llm_dependency():
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).split("\n")
    for forbidden in ("commit", "rollback", "begin", "close", "SessionLocal", "get_db", "HTTPException",
                      "delete", "update", "merge", "expunge", "text", "pg_advisory_lock", "pg_try_advisory_lock",
                      "pg_advisory_unlock", "create_all", "hash", "execute_raw"):
        assert forbidden not in tokens, forbidden
    # Un seul savepoint : l'INSERT d'un run (traduction de la dédup).
    assert tokens.count("begin_nested") == 1
    # Verrou consultatif TRANSACTION-LEVEL uniquement.
    assert tokens.count("pg_advisory_xact_lock") == 1
    # Aucun autre service importé (frontières nettes, aucun cycle), ni
    # framework, ni client LLM / HTTP.
    assert _imports(SERVICE_PATH) == {"hashlib", "json", "math", "uuid", "datetime", "typing", "sqlalchemy",
                                      "sqlalchemy.exc", "core.models"}


def test_service_contains_no_stage_confidence_score_or_detection_vocabulary():
    """T5-B ne décide aucun stade (T6), ne note rien, ne détecte rien, ne
    construit aucun profil (T5-C), n'appelle aucun modèle (docstrings et
    commentaires exclus). local_stage n'est LU que pour exiger la cible
    application d'un transfert."""
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).lower()
    for word in ("score", "confidence", "mastery", "master", "progress", "infer", "coverage", "variety",
                 "freshness", "durab", "consistency", "percent", "weight", "coefficient", "probabilit",
                 "independen", "current_stage", "new_stage", "previous_stage", "revalidation_required",
                 "confirmation_required", "discovery", "comprehension", "non_etabli", "profile", "detect",
                 "ticker", "anthropic", "openai", "claude", "llm", "requests", "http", "prompt", "model_id",
                 "evaluator", "xp_", "level", "ratio", "evidence_strength", "closure", "transitiv"):
        assert word not in tokens, word
    stage_reads = [t for t in _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).split("\n") if "stage" in t]
    assert stage_reads and all("local_stage" in t for t in stage_reads)


def test_independent_is_never_a_dependency_type():
    assert svc.DEPENDENCY_TYPES == frozenset({"dependent", "partially_dependent"})
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).split("\n")
    assert "independent" not in tokens and "not_dependent" not in tokens
    with pytest.raises(InvalidLongitudinalPayload, match="dependency_type"):
        svc.add_dependency(_NoDB(), **_dep_kwargs(dependency_type="independent"))


T5_WRITABLE_MODELS = {"LongitudinalAssessmentRun", "LongitudinalAssessmentInput", "ObservationDependency",
                      "ObservationTransfer", "ObservationRevalidation"}
NEVER_CONSTRUCTED = {"User", "CognitiveEvent", "SupportTrace", "ObservationEvaluationRun", "PedagogicalObservation",
                     "PedagogicalTaxonomyRelease", "CoreCapabilityDefinition", "CapabilityTaxonomyMembership",
                     "ObservationCapability"}


def test_service_creates_no_evidence_and_mutates_only_t5_runs():
    """Aucune preuve créée (ni événement, aide, observation, run T3,
    localisation) ; les seuls objets construits sont les lignes T5 ; les
    seules affectations d'attribut sont les transitions du run T5."""
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    constructed = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id[:1].isupper()}
    assert not constructed & NEVER_CONSTRUCTED, constructed & NEVER_CONSTRUCTED
    models = constructed - {"InvalidLongitudinalPayload", "InvalidLongitudinalState", "InvalidRelationScope",
                            "InvalidDependency", "InvalidTransfer", "InvalidRevalidation", "UserNotFound",
                            "LongitudinalAssessmentNotFound", "DuplicateLongitudinalAssessment",
                            "TaxonomyReleaseNotUsable", "StaleLongitudinalSnapshot", "ObservationNotEligible",
                            "RelationSourceNotFound", "DuplicateLongitudinalRelation", "_Evidence", "_Scope",
                            "_Relation"}
    assert models == T5_WRITABLE_MODELS
    assigned = {t.attr for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets
                if isinstance(t, ast.Attribute)}
    assert assigned == {"execution_status", "interpretation_status", "completed_at", "failure_code"}
    # Les lignes *_capabilities sont construites par un seul helper, pour
    # les trois modèles de périmètre T5 uniquement.
    source = SERVICE_PATH.read_text(encoding="utf-8")
    for model in ("DependencyCapability", "TransferCapability", "RevalidationCapability"):
        assert f"_insert_scope(db, {model}," in source, model


def test_service_is_not_wired_to_the_application():
    """Aucune route, aucun module applicatif n'importe le service (seuls les
    tests l'utilisent) ; api.py ne parle pas de longitudinal."""
    # Étape 6.1A : lecteur Step 6 non branché, par les seules lectures du run
    # T5 parent et de son snapshot (jamais une écriture T5).
    # Étape 6.4B1 : mêmes lectures, pour vérifier l'appartenance des bases
    # positives au snapshot T5 consommé (voir tests/test_progress_evidence.py).
    step6_readers = {"core/adaptation_state.py": {"get_longitudinal_assessment", "get_longitudinal_inputs"},
                     "core/progress_evidence.py": {"get_longitudinal_assessment", "get_longitudinal_inputs"}}
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
        if rel != "core/longitudinal_service.py":
            assert "longitudinal_service" not in source, rel
            for name in PUBLIC_API - step6_readers.get(rel, set()):
                assert name not in source, (rel, name)
    assert checked > 0
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8")).lower()
    for word in ("longitudinal", "dependenc", "transfer", "revalidation"):
        assert word not in api, word


def test_only_one_t5_service_module():
    """Un seul service transactionnel T5 ; depuis T5-C, le seul autre module
    T5 est le lecteur core/longitudinal_view.py (lecture seule, voir
    tests/test_longitudinal_view.py)."""
    core = sorted(p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / "core").rglob("*.py"))
    t5 = [m for m in core if any(w in m for w in ("longitudinal", "relation", "dependenc", "transfer",
                                                  "revalidation"))]
    assert t5 == ["core/longitudinal_service.py", "core/longitudinal_view.py"]


def test_vocabularies_match_the_0008_check_constraints():
    assert svc.SOURCE_KINDS == frozenset(SOURCE_KINDS)
    assert svc.DEPENDENCY_TYPES == frozenset(DEPENDENCY_TYPES)
    assert svc.SCOPE_MODES == frozenset(SCOPE_MODES)
    assert {svc.RUNNING, svc.COMPLETED, svc.FAILED} == set(EXECUTION_STATUSES)
    assert {svc.CANDIDATE, svc.ACTIVE, svc.SUPERSEDED, svc.OBSOLETE} == set(INTERPRETATION_STATUSES)
    assert svc.COMPETENCY_CODES == obs.COMPETENCY_CODES
    assert svc.APPLICATION in obs.LOCAL_STAGES and "mastery" not in obs.LOCAL_STAGES
    assert (svc.SUPPORTIVE, svc.CONTRADICTORY) == (obs.SUPPORTIVE, obs.CONTRADICTORY)
    assert svc.VALID == obs.VALID and svc.RELEASE_ACTIVE == tax.RELEASE_ACTIVE
    assert {svc.LOCALIZED, svc.COMPETENCY_ONLY} == obs.CAPABILITY_LOCALIZATIONS
    assert svc.DEDUP_CONSTRAINT == DEDUP_UNIQUE


def test_utcnow_is_utc_aware():
    now = svc._utcnow()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


# --- validation : start -----------------------------------------------------

def test_valid_start_arguments_reach_the_database():
    _reaches_db(lambda db: svc.start_longitudinal_assessment(db, **_t5_kwargs()))


@pytest.mark.parametrize("field", START_TEXT)
@pytest.mark.parametrize("value", INVALID_TEXT)
def test_start_rejects_invalid_strings(field, value):
    with pytest.raises(InvalidLongitudinalPayload, match=field):
        svc.start_longitudinal_assessment(_NoDB(), **_t5_kwargs(**{field: value}))


@pytest.mark.parametrize("value", ["C0", "C13", "c7", "C7 ", "C07", "C7_A", "", None, 7, _Mode.LOCALIZED])
def test_start_rejects_unknown_competency_codes(value):
    with pytest.raises(InvalidLongitudinalPayload, match="competency_code"):
        svc.start_longitudinal_assessment(_NoDB(), **_t5_kwargs(competency_code=value))


@pytest.mark.parametrize("value", INVALID_UUIDS)
def test_start_rejects_invalid_release_ids(value):
    with pytest.raises(InvalidLongitudinalPayload, match="pedagogical_taxonomy_release_id"):
        svc.start_longitudinal_assessment(_NoDB(), **_t5_kwargs(pedagogical_taxonomy_release_id=value))


@pytest.mark.parametrize("competency", sorted(svc.COMPETENCY_CODES))
def test_every_competency_passes_validation(competency):
    _reaches_db(lambda db: svc.start_longitudinal_assessment(db, **_t5_kwargs(competency_code=competency)))


# --- validation : relations -------------------------------------------------

@pytest.mark.parametrize("name", sorted(RELATION_CALLS))
def test_valid_relation_arguments_reach_the_database(name):
    fn, kwargs, _ = RELATION_CALLS[name]
    _reaches_db(lambda db: fn(db, **kwargs()))
    _reaches_db(lambda db: fn(db, **kwargs(scope_mode="competency_only")))
    _reaches_db(lambda db: fn(db, **kwargs(scope_mode="localized", capability_membership_ids=[uuid.uuid4()])))
    _reaches_db(lambda db: fn(db, **kwargs(scope_mode="localized",
                                           capability_membership_ids=(uuid.uuid4(), uuid.uuid4()))))


UUID_FIELDS = {
    "add_dependency": ("run_id", "target_observation_id", "source_observation_id"),
    "add_transfer": ("run_id", "source_observation_id", "target_observation_id"),
    "add_revalidation": ("run_id", "source_contradiction_observation_id", "target_supportive_observation_id"),
}


@pytest.mark.parametrize("name, field", [(n, f) for n, fields in sorted(UUID_FIELDS.items()) for f in fields])
@pytest.mark.parametrize("value", INVALID_UUIDS[1:])
def test_relation_identifiers_must_be_uuids(name, field, value):
    fn, kwargs, _ = RELATION_CALLS[name]
    with pytest.raises(InvalidLongitudinalPayload, match=field):
        fn(_NoDB(), **kwargs(**{field: value}))


def test_support_trace_identifier_must_be_a_uuid():
    with pytest.raises(InvalidLongitudinalPayload, match="source_support_trace_id"):
        svc.add_dependency(_NoDB(), **_dep_kwargs(source_kind="support_trace", source_observation_id=None,
                                                  source_support_trace_id=str(uuid.uuid4())))


@pytest.mark.parametrize("field, value", [
    ("source_kind", "trace"), ("source_kind", "Observation"), ("source_kind", None), ("source_kind", ""),
    ("dependency_type", "independent"), ("dependency_type", "partial"), ("dependency_type", None),
    ("dependency_type", "Dependent"), ("scope_mode", "whole"), ("scope_mode", None), ("scope_mode", "global"),
    ("scope_mode", _Mode.LOCALIZED),
])
def test_dependency_vocabularies_are_closed(field, value):
    with pytest.raises(InvalidLongitudinalPayload, match=field):
        svc.add_dependency(_NoDB(), **_dep_kwargs(**{field: value}))


@pytest.mark.parametrize("kind, with_observation, with_trace", [
    ("observation", False, False), ("observation", True, True), ("observation", False, True),
    ("support_trace", False, False), ("support_trace", True, True), ("support_trace", True, False),
])
def test_dependency_source_must_be_an_exact_xor(kind, with_observation, with_trace):
    with pytest.raises(InvalidDependency, match="seul requis"):
        svc.add_dependency(_NoDB(), **_dep_kwargs(
            source_kind=kind, source_observation_id=uuid.uuid4() if with_observation else None,
            source_support_trace_id=uuid.uuid4() if with_trace else None))


def test_dependency_xor_accepts_a_support_trace_source():
    _reaches_db(lambda db: svc.add_dependency(db, **_dep_kwargs(
        source_kind="support_trace", source_observation_id=None, source_support_trace_id=uuid.uuid4())))


def test_self_relations_are_refused_before_the_database():
    same = uuid.uuid4()
    with pytest.raises(InvalidDependency, match="elle-même"):
        svc.add_dependency(_NoDB(), **_dep_kwargs(target_observation_id=same, source_observation_id=same))
    with pytest.raises(InvalidTransfer, match="identiques"):
        svc.add_transfer(_NoDB(), **_transfer_kwargs(source_observation_id=same, target_observation_id=same))
    with pytest.raises(InvalidRevalidation, match="identiques"):
        svc.add_revalidation(_NoDB(), **_reval_kwargs(source_contradiction_observation_id=same,
                                                      target_supportive_observation_id=same))


@pytest.mark.parametrize("name", sorted(RELATION_CALLS))
@pytest.mark.parametrize("mode, ids, exc, match", [
    ("localized", [], InvalidRelationScope, "au moins un"),
    ("localized", (), InvalidRelationScope, "au moins un"),
    ("whole_observation", [uuid.UUID(int=3)], InvalidRelationScope, "aucun capability_membership_id"),
    ("competency_only", [uuid.UUID(int=3)], InvalidRelationScope, "aucun capability_membership_id"),
    ("localized", [uuid.UUID(int=3), uuid.UUID(int=3)], InvalidRelationScope, "doublon"),
    ("localized", {uuid.UUID(int=3)}, InvalidLongitudinalPayload, "liste"),
    ("localized", None, InvalidLongitudinalPayload, "liste"),
    ("localized", str(uuid.UUID(int=3)), InvalidLongitudinalPayload, "liste"),
    ("localized", [str(uuid.UUID(int=3))], InvalidLongitudinalPayload, "capability_membership_ids"),
    ("localized", [uuid.UUID(int=3), None], InvalidLongitudinalPayload, "capability_membership_ids"),
])
def test_scope_structure_is_validated_before_the_database(name, mode, ids, exc, match):
    fn, kwargs, _ = RELATION_CALLS[name]
    with pytest.raises(exc, match=match):
        fn(_NoDB(), **kwargs(scope_mode=mode, capability_membership_ids=ids))


@pytest.mark.parametrize("name", sorted(RELATION_CALLS))
@pytest.mark.parametrize("value", [None, [], ["raison"], "raison", 1, True, ({"a": 1},)])
def test_basis_root_must_be_a_dict(name, value):
    fn, kwargs, basis = RELATION_CALLS[name]
    with pytest.raises(InvalidLongitudinalPayload, match=basis):
        fn(_NoDB(), **kwargs(**{basis: value}))


@pytest.mark.parametrize("name", sorted(RELATION_CALLS))
def test_basis_must_not_be_empty(name):
    """Une relation a toujours une raison explicable."""
    fn, kwargs, basis = RELATION_CALLS[name]
    for empty in ({}, OrderedDict()):
        with pytest.raises(InvalidLongitudinalPayload, match="non vide"):
            fn(_NoDB(), **kwargs(**{basis: empty}))


@pytest.mark.parametrize("name", sorted(RELATION_CALLS))
@pytest.mark.parametrize("value", INVALID_JSON_VALUES, ids=range(len(INVALID_JSON_VALUES)))
def test_basis_must_be_strictly_json_compatible(name, value):
    fn, kwargs, basis = RELATION_CALLS[name]
    with pytest.raises(InvalidLongitudinalPayload, match=basis):
        fn(_NoDB(), **kwargs(**{basis: value if isinstance(value, dict) else {"v": value}}))


def test_basis_rejects_circular_references_and_is_deep_copied():
    cyclic = {"a": []}
    cyclic["a"].append(cyclic)
    with pytest.raises(InvalidLongitudinalPayload, match="circulaire"):
        svc.add_transfer(_NoDB(), **_transfer_kwargs(transfer_basis=cyclic))
    shared = {"k": 1}
    basis = OrderedDict(s="é", i=-3, big=10 ** 30, f=0.5, t=True, n=None, nested=[{"a": [1, "b"]}],
                        twice=[shared, shared])
    copy = svc._require_basis(basis, "transfer_basis")
    assert copy == basis and type(copy) is dict
    basis["nested"][0]["a"].append("mutation")
    shared["k"] = 2
    assert copy["nested"] == [{"a": [1, "b"]}] and copy["twice"] == [{"k": 1}, {"k": 1}]


# --- validation : lifecycle et lectures -------------------------------------

UUID_CALLS = {
    "complete.run_id": lambda db, v: svc.complete_longitudinal_assessment(db, run_id=v),
    "fail.run_id": lambda db, v: svc.fail_longitudinal_assessment(db, run_id=v),
    "get.run_id": lambda db, v: svc.get_longitudinal_assessment(db, run_id=v),
    "inputs.run_id": lambda db, v: svc.get_longitudinal_inputs(db, run_id=v),
    "dependencies.run_id": lambda db, v: svc.get_dependencies(db, run_id=v),
    "transfers.run_id": lambda db, v: svc.get_transfers(db, run_id=v),
    "revalidations.run_id": lambda db, v: svc.get_revalidations(db, run_id=v),
}


@pytest.mark.parametrize("name", sorted(UUID_CALLS))
@pytest.mark.parametrize("value", INVALID_UUIDS)
def test_run_identifiers_must_be_uuids(name, value):
    with pytest.raises(InvalidLongitudinalPayload, match="run_id"):
        UUID_CALLS[name](_NoDB(), value)


@pytest.mark.parametrize("value", ["", " ", 3, b"x", "a\x00"])
def test_failure_code_is_none_or_text(value):
    with pytest.raises(InvalidLongitudinalPayload, match="failure_code"):
        svc.fail_longitudinal_assessment(_NoDB(), run_id=uuid.uuid4(), failure_code=value)
    _reaches_db(lambda db: svc.fail_longitudinal_assessment(db, run_id=uuid.uuid4(), failure_code=None))
    _reaches_db(lambda db: svc.fail_longitudinal_assessment(db, run_id=uuid.uuid4(), failure_code="stale_input"))


def test_get_active_validates_its_arguments():
    with pytest.raises(InvalidLongitudinalPayload, match="user_id"):
        svc.get_active_longitudinal_assessment(_NoDB(), user_id="", competency_code="C7")
    with pytest.raises(InvalidLongitudinalPayload, match="competency_code"):
        svc.get_active_longitudinal_assessment(_NoDB(), user_id=USER, competency_code="C7_A")


# --- formats canoniques ---------------------------------------------------------

def _uuid(n):
    return uuid.UUID(int=n)


def _ev(observation, run, event, release, compatible=(), localization="localized"):
    return svc._Evidence(observation, run, event, release, localization, "supportive", "comprehension",
                         frozenset(compatible))


def test_input_fingerprint_v1_exact_canonical_format():
    """Format figé : JSON canonique (clés triées, séparateurs compacts,
    UTF-8 non échappé), observations triées par observation_id, définitions
    triées, UUID en str canonique ; SHA-256 hex minuscule."""
    evidence = [
        _ev(_uuid(0x20), _uuid(0x21), _uuid(0x22), _uuid(0x9), compatible=(_uuid(0xB), _uuid(0xA))),
        _ev(_uuid(0x10), _uuid(0x11), _uuid(0x12), _uuid(0x9), localization="competency_only"),
    ]
    canonical = (
        '{"competency_code":"C7","input_schema_version":1,"observations":['
        '{"compatible_capability_definition_ids":[],'
        f'"evaluation_run_id":"{_uuid(0x11)}","event_id":"{_uuid(0x12)}","observation_id":"{_uuid(0x10)}",'
        f'"source_taxonomy_release_id":"{_uuid(0x9)}"}},'
        f'{{"compatible_capability_definition_ids":["{_uuid(0xA)}","{_uuid(0xB)}"],'
        f'"evaluation_run_id":"{_uuid(0x21)}","event_id":"{_uuid(0x22)}","observation_id":"{_uuid(0x20)}",'
        f'"source_taxonomy_release_id":"{_uuid(0x9)}"}}],'
        f'"pedagogical_taxonomy_release_id":"{_uuid(0x9)}","user_id":"utilisateur-é"}}'
    )
    fingerprint = svc._input_fingerprint(user_id="utilisateur-é", competency_code="C7", release_id=_uuid(0x9),
                                         evidence=evidence)
    assert fingerprint == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert len(fingerprint) == 64 and fingerprint == fingerprint.lower()
    # Dossier vide : représentable, fingerprint déterministe.
    empty = svc._input_fingerprint(user_id="u", competency_code="C7", release_id=_uuid(0x9), evidence=[])
    assert empty == hashlib.sha256(
        f'{{"competency_code":"C7","input_schema_version":1,"observations":[],'
        f'"pedagogical_taxonomy_release_id":"{_uuid(0x9)}","user_id":"u"}}'.encode()).hexdigest()


def test_input_fingerprint_is_independent_of_order_and_tracks_every_component():
    release = uuid.uuid4()
    base = [_ev(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), release, compatible=[uuid.uuid4() for _ in range(3)])
            for _ in range(6)]

    def fp(evidence, **overrides):
        kwargs = {"user_id": USER, "competency_code": "C7", "release_id": release, "evidence": evidence}
        kwargs.update(overrides)
        return svc._input_fingerprint(**kwargs)

    reference = fp(base)
    rng = random.Random(5)
    for _ in range(10):
        shuffled = base[:]
        rng.shuffle(shuffled)
        assert fp(shuffled) == reference
        assert fp(iter(shuffled)) == reference
    different = {
        "ajout": fp(base + [_ev(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), release)]),
        "retrait": fp(base[1:]),
        "run T3": fp([base[0]._replace(evaluation_run_id=uuid.uuid4())] + base[1:]),
        "événement": fp([base[0]._replace(event_id=uuid.uuid4())] + base[1:]),
        "release source": fp([base[0]._replace(source_release_id=uuid.uuid4())] + base[1:]),
        "compatibilité": fp([base[0]._replace(compatible=base[0].compatible - {next(iter(base[0].compatible))})]
                            + base[1:]),
        "release cible": fp(base, release_id=uuid.uuid4()),
        "user": fp(base, user_id=OTHER_USER),
        "compétence": fp(base, competency_code="C8"),
    }
    assert reference not in different.values()
    assert len(set(different.values())) == len(different)
    # Ni statut, ni horodatage, ni localisation, polarité ou stade : seules
    # l'identité et la provenance de chaque preuve comptent.
    assert fp([base[0]._replace(polarity="contradictory", local_stage=None)] + base[1:]) == reference


def test_scope_fingerprint_v1_exact_canonical_format():
    scope = svc._Scope("localized", frozenset({_uuid(0xB), _uuid(0xA)}))
    canonical = (f'{{"capability_definition_ids":["{_uuid(0xA)}","{_uuid(0xB)}"],"competency_code":"C7",'
                 '"scope_mode":"localized","scope_schema_version":1}')
    assert svc._scope_fingerprint("C7", scope) == hashlib.sha256(canonical.encode()).hexdigest()
    only = svc._scope_fingerprint("C7", svc._Scope("competency_only", frozenset()))
    assert only == hashlib.sha256(b'{"capability_definition_ids":[],"competency_code":"C7",'
                                  b'"scope_mode":"competency_only","scope_schema_version":1}').hexdigest()
    # Le mode porte la sémantique : même liste vide, fingerprints distincts.
    assert svc._scope_fingerprint("C7", svc._Scope("whole_observation", frozenset())) != only
    assert svc._scope_fingerprint("C8", scope) != svc._scope_fingerprint("C7", scope)
    assert svc._scope_fingerprint("C7", svc._Scope("whole_observation", scope.definitions)) != \
        svc._scope_fingerprint("C7", scope)


def test_activation_lock_key_is_deterministic_per_user_and_competency():
    expected = int.from_bytes(hashlib.sha256('["oryx-t5","u","C7"]'.encode()).digest()[:8], "big", signed=True)
    assert svc._activation_lock_key("u", "C7") == expected
    keys = {svc._activation_lock_key(u, c) for u in ("u", "u2", "é", "u,C7") for c in ("C7", "C8", "C12")}
    assert len(keys) == 12
    for key in keys:
        assert type(key) is int and -2 ** 63 <= key < 2 ** 63
    # Jamais la clé globale d'activation T4-B.
    assert tax.ACTIVATION_LOCK_KEY not in keys


def test_relation_set_refuses_contradictory_or_duplicate_dependency_classification():
    """Défense finale (sans base) : l'identité d'une dépendance exclut
    dependency_type."""
    scope = svc._Scope("localized", frozenset({_uuid(1)}))
    key = svc._dependency_key(_uuid(2), "observation", _uuid(3), None, "fp")
    assert svc._dependency_key(_uuid(2), "observation", _uuid(3), None, "fp") == key
    dependent = svc._Relation(_uuid(2), scope, key, "dependent")
    partial = svc._Relation(_uuid(2), scope, key, "partially_dependent")
    other_scope = svc._Relation(_uuid(2), scope, svc._dependency_key(_uuid(2), "observation", _uuid(3), None, "fp2"),
                                "partially_dependent")
    other_source = svc._Relation(_uuid(2), scope, svc._dependency_key(_uuid(2), "observation", _uuid(4), None, "fp"),
                                 "partially_dependent")
    svc._check_relation_set([dependent, other_scope, other_source], [], [])
    with pytest.raises(InvalidDependency, match="classification contradictoire"):
        svc._check_relation_set([dependent, other_scope, partial], [], [])
    with pytest.raises(DuplicateLongitudinalRelation):
        svc._check_relation_set([dependent, other_source, dependent], [], [])


def test_overlap_is_conservative():
    a, b = _uuid(1), _uuid(2)
    loc = lambda *d: svc._Scope("localized", frozenset(d))  # noqa: E731
    assert svc._overlaps(loc(a), loc(a, b)) and not svc._overlaps(loc(a), loc(b))
    for mode in ("whole_observation", "competency_only"):
        for other in (loc(a), svc._Scope("whole_observation", frozenset({b})),
                      svc._Scope("competency_only", frozenset())):
            assert svc._overlaps(svc._Scope(mode, frozenset({a})), other)
            assert svc._overlaps(other, svc._Scope(mode, frozenset()))


# --- requêtes de verrouillage -------------------------------------------------

class _RecordingDB:
    """Enregistre les requêtes ; toute ligne cherchée est absente."""

    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(" ".join(str(statement.compile(dialect=postgresql.dialect())).split()))
        return SimpleNamespace(scalar_one_or_none=lambda: None, one_or_none=lambda: None, first=lambda: None)


RUN_LOCK = ("SELECT longitudinal_assessment_runs.id,",
            "FROM longitudinal_assessment_runs WHERE longitudinal_assessment_runs.id = ", "FOR NO KEY UPDATE")


@pytest.mark.parametrize("call", [
    lambda db: svc.add_dependency(db, **_dep_kwargs()),
    lambda db: svc.add_transfer(db, **_transfer_kwargs()),
    lambda db: svc.add_revalidation(db, **_reval_kwargs()),
    lambda db: svc.fail_longitudinal_assessment(db, run_id=uuid.uuid4()),
], ids=["dependency", "transfer", "revalidation", "fail"])
def test_first_query_of_relation_and_fail_is_the_run_row_lock(call):
    db = _RecordingDB()
    with pytest.raises(LongitudinalAssessmentNotFound):
        call(db)
    assert len(db.statements) == 1
    sql = db.statements[0]
    assert sql.startswith(RUN_LOCK[0]) and RUN_LOCK[1] in sql and sql.endswith(RUN_LOCK[2])


def test_complete_reads_the_immutable_couple_before_any_lock():
    db = _RecordingDB()
    with pytest.raises(LongitudinalAssessmentNotFound):
        svc.complete_longitudinal_assessment(db, run_id=uuid.uuid4())
    assert len(db.statements) == 1
    sql = db.statements[0]
    assert sql.startswith("SELECT longitudinal_assessment_runs.user_id, longitudinal_assessment_runs.competency_code "
                          "FROM longitudinal_assessment_runs WHERE longitudinal_assessment_runs.id = ")
    assert " FOR " not in sql


def test_start_first_checks_the_user():
    db = _RecordingDB()
    with pytest.raises(UserNotFound):
        svc.start_longitudinal_assessment(db, **_t5_kwargs())
    assert len(db.statements) == 1 and db.statements[0].startswith("SELECT users.id FROM users")


def test_dedup_violation_detection_is_targeted():
    def error(constraint):
        return sa.exc.IntegrityError("INSERT", {}, SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint)))

    assert svc._is_dedup_violation(error(DEDUP_UNIQUE))
    for other in (ONE_ACTIVE_INDEX, "ck_longitudinal_assessment_runs_competency_code",
                  "longitudinal_assessment_runs_user_id_fkey", "longitudinal_assessment_runs_pkey", None):
        assert not svc._is_dedup_violation(error(other)), other
    assert not svc._is_dedup_violation(sa.exc.IntegrityError("INSERT", {}, Exception(DEDUP_UNIQUE)))


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

CLEANUP = ("revalidation_capabilities", "observation_revalidations", "transfer_capabilities",
           "observation_transfers", "dependency_capabilities", "observation_dependencies",
           "longitudinal_assessment_inputs", "longitudinal_assessment_runs", "observation_capabilities",
           "capability_taxonomy_memberships", "core_capability_definitions", "pedagogical_observations",
           "observation_evaluation_runs", "pedagogical_taxonomy_releases", "support_traces", "cognitive_events",
           "conversation_identities")
assert set(CLEANUP[:8]) == T5A_TABLES
EVIDENCE_TABLES = ("cognitive_events", "support_traces", "observation_evaluation_runs", "pedagogical_observations",
                   "observation_capabilities", "capability_taxonomy_memberships", "core_capability_definitions",
                   "pedagogical_taxonomy_releases", "users")
ON_RUN = "SELECT longitudinal_assessment_runs.id,"
ON_RELEASE = "SELECT pedagogical_taxonomy_releases.id,"
ON_ADVISORY = "SELECT pg_advisory_xact_lock("


@pytest.fixture(scope="module")
def engine(pg_url):  # noqa: F811
    """Schéma = head (0009) + deux utilisateurs ; chaque test nettoie ce
    qu'il a créé."""
    eng = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    _upgrade_head_with_users(pg_url, eng)
    yield eng
    eng.dispose()


@pytest.fixture
def Sessions(engine):
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)  # = core.db.SessionLocal
    yield factory
    with engine.begin() as conn:
        for table in CLEANUP:
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


@pytest.fixture
def clock(monkeypatch):
    """Horloge déterministe du service T5-B : chaque appel avance d'une
    seconde. `clock()` l'installe et retourne son premier instant."""
    def start():
        base = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        ticks = itertools.count()
        monkeypatch.setattr(svc, "_utcnow", lambda: base + timedelta(seconds=next(ticks)))
        return base
    return start


# --- helpers T2 / T3 / T4 (via leurs services) ----------------------------------

_REVISIONS = itertools.count(100)


def _taxonomy(Sessions, spec=("C7_A", "C7_B", "C7_C", "C8_A"), *, activate=True):
    """Release (activée par défaut) : spec = {code: None (nouvelle
    définition) | UUID (définition réutilisée)}. Chaque nouvelle définition
    reçoit une révision unique (aucun conflit entre releases d'un test)."""
    if not isinstance(spec, dict):
        spec = dict.fromkeys(spec)
    with Sessions() as session:
        release = tax.create_candidate_release(session, version_key=f"t5b-{uuid.uuid4()}",
                                               spec_fingerprint=f"sha256:{uuid.uuid4()}")
        memberships, definitions = {}, {}
        for code, value in spec.items():
            definition_id = value if isinstance(value, uuid.UUID) else tax.create_capability_definition(
                session, **_def_kwargs(code, next(_REVISIONS))).id
            memberships[code] = tax.attach_capability_to_release(
                session, release_id=release.id, capability_definition_id=definition_id).id
            definitions[code] = definition_id
        if activate:
            tax.activate_release(session, release_id=release.id)
        session.commit()
        return SimpleNamespace(id=release.id, m=memberships, d=definitions)


def sup(*caps, stage="comprehension", **overrides):
    return {"caps": caps, "local_stage": stage, **overrides}


def app(*caps, **overrides):
    return sup(*caps, stage="application", **overrides)


def contra(*caps, **overrides):
    return {"caps": caps, "polarity": "contradictory", "local_stage": None, "contradiction_scope": "comprehension",
            "error_type": "conceptual", **overrides}


def only(spec=None, **overrides):
    """Même observation, capability_localization competency_only (aucun
    mapping)."""
    return {**(spec or sup()), "caps": (), "capability_localization": "competency_only", **overrides}


def _t3(Sessions, release_id, *specs, user_id=USER, end="complete", event_id=None):
    """Run T3 commité (évalué sous release_id), une observation par spec
    (mappings T4 dans spec["caps"]), terminé par end."""
    if event_id is None:
        event_id = _finalized_event(Sessions, user_id=user_id)
    with Sessions() as session:
        run = obs.start_evaluation_run(session, **_t3_start_kwargs(
            event_id, pedagogical_taxonomy_release_id=release_id))
        ids = []
        for spec in specs:
            spec = dict(spec)
            caps = spec.pop("caps", ())
            observation = obs.add_observation(session, run_id=run.id, **_obs_kwargs(**spec))
            for membership_id in caps:
                tax.map_observation_capability(session, observation_id=observation.id,
                                               capability_membership_id=membership_id)
            ids.append(observation.id)
        if end == "complete":
            obs.complete_evaluation_run(session, run_id=run.id, output_fingerprint="sha256:out")
        elif end == "fail":
            obs.fail_evaluation_run(session, run_id=run.id, failure_code="evaluator_error")
        session.commit()
        return SimpleNamespace(run=run.id, ids=ids, id=ids[0] if ids else None, event=event_id,
                               release=release_id)


def _event_with_trace(Sessions, user_id=USER):
    """CognitiveEvent finalized avec une aide réellement montrée."""
    with Sessions() as session:
        event = cc.open_event_idempotent(session, **_capture_open_kwargs(
            user_id=user_id, task_kind="interpret_metric", stimulus_snapshot={"question": "Que mesure le ROE ?"}))
        trace = cc.add_support_trace(session, event_id=event.id, support_kind="hint",
                                     support_payload={"text": "pense aux capitaux propres"})
        cc.append_user_contribution(session, event_id=event.id, **_capture_contribution(
            session_ref=event.conversation_key, text_excerpt="rentabilité des capitaux propres"))
        cc.finalize_event(session, event_id=event.id)
        session.commit()
        return event.id, trace.id


def _invalidate(Sessions, observation_id):
    with Sessions() as session:
        obs.invalidate_observation(session, observation_id=observation_id, reason="erreur d'extraction")
        session.commit()


# --- helpers T5 ------------------------------------------------------------------

def _start(Sessions, release_id, **overrides) -> uuid.UUID:
    with Sessions() as session:
        run = svc.start_longitudinal_assessment(session, **_t5_kwargs(release_id, **overrides))
        session.commit()
        return run.id


def _commit(Sessions, fn, **kwargs):
    with Sessions() as session:
        result = fn(session, **kwargs)
        session.commit()
        return result.id


def _complete(Sessions, run_id):
    return _commit(Sessions, svc.complete_longitudinal_assessment, run_id=run_id)


def _count(engine, table, where="TRUE", **params):
    with engine.connect() as conn:
        return conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {where}"), params).scalar_one()


def _run_row(engine, run_id):
    rows = _rows(engine, "SELECT * FROM longitudinal_assessment_runs WHERE id = :id", id=run_id)
    return rows[0] if rows else None


def _state(engine, run_id):
    row = _run_row(engine, run_id)
    return row["execution_status"], row["interpretation_status"]


def _inputs(engine, run_id) -> set:
    return {r["observation_id"] for r in _rows(
        engine, "SELECT observation_id FROM longitudinal_assessment_inputs WHERE run_id = :r", r=run_id)}


def _evidence_state(engine) -> dict:
    """Contenu intégral des tables T2 / T3 / T4 (preuves et taxonomie)."""
    return {t: sorted(map(repr, _rows(engine, f"SELECT * FROM {t}"))) for t in EVIDENCE_TABLES}


def _fp(*items, release_id, user_id=USER, competency_code="C7"):
    """input_fingerprint attendu : items = (_t3 résultat, index, définitions
    compatibles)."""
    return svc._input_fingerprint(user_id=user_id, competency_code=competency_code, release_id=release_id,
                                  evidence=[_ev(t.ids[i], t.run, t.event, t.release, compatible=defs)
                                            for t, i, defs in items])


def _scope_fp(mode, *definitions, competency_code="C7"):
    return svc._scope_fingerprint(competency_code, svc._Scope(mode, frozenset(definitions)))


# --- A. start et snapshot ----------------------------------------------------------

def test_pg_start_creates_a_running_candidate_with_its_snapshot(engine, Sessions, db, clock):
    tx = _taxonomy(Sessions)
    first = _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    second = _t3(Sessions, tx.id, contra(tx.m["C7_A"], tx.m["C7_B"]))
    now = clock()
    run = svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id, assessment_dedup_key="u:C7:v1"))
    assert isinstance(run, LongitudinalAssessmentRun) and isinstance(run.id, uuid.UUID)
    assert (run.user_id, run.competency_code, run.pedagogical_taxonomy_release_id) == (USER, "C7", tx.id)
    assert (run.execution_status, run.interpretation_status) == ("running", "candidate")
    assert run.started_at == run.created_at == now
    assert run.completed_at is None and run.failure_code is None
    assert (run.trigger, run.dependency_version, run.transfer_version, run.revalidation_version,
            run.relation_schema_version, run.assessment_dedup_key) == (
        "observation_completed", "dependency-1", "transfer-1", "revalidation-1", "relations-1", "u:C7:v1")
    assert run.input_fingerprint == _fp((first, 0, [tx.d["C7_A"]]), (second, 0, [tx.d["C7_A"], tx.d["C7_B"]]),
                                        release_id=tx.id)
    inputs = svc.get_longitudinal_inputs(db, run_id=run.id)
    assert [i.observation_id for i in inputs] == sorted([first.id, second.id], key=str)
    # Flushé dans la transaction de l'appelant, pas commité.
    assert _run_row(engine, run.id) is None
    db.commit()
    assert _state(engine, run.id) == ("running", "candidate")
    assert _inputs(engine, run.id) == {first.id, second.id}
    assert svc.get_longitudinal_assessment(db, run_id=run.id) is run


def test_pg_snapshot_keeps_every_admissible_observation_and_nothing_else(engine, Sessions, db):
    """Inclus : supportive, contradictory, supportive local_stage none,
    competency_only de la même release, run T3 réévalué (nouvel active).
    Exclus : invalidated, run T3 superseded / obsolete (failed) /
    candidate (running), autre user, autre compétence, run T3 sans
    taxonomie (localized ou competency_only)."""
    tx = _taxonomy(Sessions)
    a, b, c8 = tx.m["C7_A"], tx.m["C7_B"], tx.m["C8_A"]
    supportive = _t3(Sessions, tx.id, sup(a), sup(c8, competency_code="C8"))
    contradictory = _t3(Sessions, tx.id, contra(a, b))
    none_stage = _t3(Sessions, tx.id, sup(b, stage="none"))
    competency_only = _t3(Sessions, tx.id, only(contra()))
    invalidated = _t3(Sessions, tx.id, sup(a))
    _invalidate(Sessions, invalidated.id)
    superseded = _t3(Sessions, tx.id, sup(a))
    reevaluation = _t3(Sessions, tx.id, sup(b), event_id=superseded.event)
    failed = _t3(Sessions, tx.id, sup(a), end="fail")
    running = _t3(Sessions, tx.id, sup(a), end=None)
    other_user = _t3(Sessions, tx.id, sup(a), user_id=OTHER_USER)
    no_taxonomy = _t3(Sessions, None, sup(), only())
    assert obs.get_evaluation_run(db, run_id=superseded.run).interpretation_status == "superseded"

    run = svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id))
    expected = {supportive.ids[0], contradictory.id, none_stage.id, competency_only.id, reevaluation.id}
    assert {i.observation_id for i in svc.get_longitudinal_inputs(db, run_id=run.id)} == expected
    excluded = {supportive.ids[1], invalidated.id, superseded.id, failed.id, running.id, other_user.id,
                *no_taxonomy.ids}
    assert not expected & excluded
    assert run.input_fingerprint == _fp(
        (supportive, 0, [tx.d["C7_A"]]), (contradictory, 0, [tx.d["C7_A"], tx.d["C7_B"]]),
        (none_stage, 0, [tx.d["C7_B"]]), (competency_only, 0, []), (reevaluation, 0, [tx.d["C7_B"]]),
        release_id=tx.id)
    # L'autre utilisateur a son propre dossier ; C8 aussi.
    other = svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id, user_id=OTHER_USER))
    assert {i.observation_id for i in svc.get_longitudinal_inputs(db, run_id=other.id)} == {other_user.id}
    c8_run = svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id, competency_code="C8"))
    assert {i.observation_id for i in svc.get_longitudinal_inputs(db, run_id=c8_run.id)} == {supportive.ids[1]}


def test_pg_empty_snapshot_is_a_valid_dossier_and_completes(engine, Sessions, db):
    tx = _taxonomy(Sessions)
    _t3(Sessions, tx.id, sup(tx.m["C7_A"]), user_id=OTHER_USER)
    before = _evidence_state(engine)
    run = svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id))
    assert svc.get_longitudinal_inputs(db, run_id=run.id) == []
    assert run.input_fingerprint == svc._input_fingerprint(user_id=USER, competency_code="C7", release_id=tx.id,
                                                           evidence=[])
    svc.complete_longitudinal_assessment(db, run_id=run.id)
    db.commit()
    assert _state(engine, run.id) == ("completed", "active")
    assert _count(engine, "longitudinal_assessment_inputs") == 0
    # Aucun faux événement ni fausse observation.
    assert _evidence_state(engine) == before


@pytest.mark.parametrize("status", ["candidate", "retired", "unknown"])
def test_pg_start_requires_the_current_active_release(engine, Sessions, db, status):
    tx = _taxonomy(Sessions, ("C7_A",), activate=status != "candidate")
    if status == "retired":
        _taxonomy(Sessions, ("C7_A",))
    release_id = uuid.uuid4() if status == "unknown" else tx.id
    with pytest.raises(TaxonomyReleaseNotUsable):
        svc.start_longitudinal_assessment(db, **_t5_kwargs(release_id))
    assert _count(engine, "longitudinal_assessment_runs") == 0


def test_pg_start_reads_the_release_status_fresh(Sessions, db):
    """Un objet release périmé dans la Session n'est jamais utilisé."""
    tx = _taxonomy(Sessions, ("C7_A",))
    tax.get_active_release(db)  # release active chargée dans la Session
    _taxonomy(Sessions, ("C7_A",))  # retire tx dans une autre transaction
    with pytest.raises(TaxonomyReleaseNotUsable):
        svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id))


def test_pg_start_unknown_user(Sessions, db):
    tx = _taxonomy(Sessions, ("C7_A",))
    with pytest.raises(UserNotFound):
        svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id, user_id="inconnu"))


def test_pg_duplicate_dedup_key_is_a_business_error_never_the_old_run(engine, Sessions, db):
    tx = _taxonomy(Sessions, ("C7_A",))
    run_id = _start(Sessions, tx.id, assessment_dedup_key="k")
    before = _run_row(engine, run_id)
    for overrides in ({}, {"user_id": OTHER_USER}, {"competency_code": "C8"}):
        with pytest.raises(DuplicateLongitudinalAssessment):
            svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id, assessment_dedup_key="k", **overrides))
    # Transaction de l'appelant toujours utilisable après la pré-vérification.
    other = svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id, assessment_dedup_key="k2"))
    db.commit()
    assert _run_row(engine, run_id) == before
    assert _count(engine, "longitudinal_assessment_runs") == 2 and _state(engine, other.id)[0] == "running"


def test_pg_dedup_race_is_translated_via_savepoint_and_the_transaction_stays_usable(engine, Sessions):
    """La pré-vérification ne voit pas l'INSERT non commité du premier ; le
    second attend sur l'UNIQUE, puis la violation de CETTE contrainte
    devient DuplicateLongitudinalAssessment après retour au savepoint : les
    écritures antérieures de l'appelant restent et il peut continuer."""
    tx = _taxonomy(Sessions, ("C7_A",))

    def racing(session):
        session.execute(sa.text("INSERT INTO users (id, level) VALUES ('pending-user', 'debutant')"))
        try:
            svc.start_longitudinal_assessment(session, **_t5_kwargs(tx.id, assessment_dedup_key="shared",
                                                                     competency_code="C8"))
        except DuplicateLongitudinalAssessment as exc:
            assert isinstance(exc.__cause__, sa.exc.IntegrityError) and DEDUP_UNIQUE in str(exc.__cause__)
            return svc.start_longitudinal_assessment(session, **_t5_kwargs(tx.id, assessment_dedup_key="after"))
        return None

    with Sessions() as first, Sessions() as second:
        run_id = svc.start_longitudinal_assessment(first, **_t5_kwargs(tx.id, assessment_dedup_key="shared")).id
        result, error = _blocked(engine, first, second, racing, waiting_on="INSERT INTO longitudinal_assessment_runs")
        assert error is None and result is not None
        after = result.id
    assert {r["assessment_dedup_key"]: r["competency_code"] for r in _rows(
        engine, "SELECT assessment_dedup_key, competency_code FROM longitudinal_assessment_runs")} == {
        "shared": "C7", "after": "C7"}
    assert _state(engine, run_id) == _state(engine, after) == ("running", "candidate")
    assert _count(engine, "users", "id = 'pending-user'") == 1


def test_pg_other_integrity_errors_are_not_masked(engine, Sessions, db, monkeypatch):
    """Seule la contrainte de dédup est traduite : une autre violation (ici
    un CHECK, en corrompant la constante du service) remonte telle quelle ;
    la transaction de l'appelant reste utilisable (savepoint)."""
    tx = _taxonomy(Sessions, ("C7_A",))
    monkeypatch.setattr(svc, "RUNNING", "bogus")
    with pytest.raises(sa.exc.IntegrityError, match="ck_longitudinal_assessment_runs_execution_status") as info:
        svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id))
    assert not isinstance(info.value, LongitudinalServiceError)
    monkeypatch.undo()
    run = svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id))
    db.commit()
    assert _state(engine, run.id) == ("running", "candidate")


# --- B. compatibility gate taxonomique ---------------------------------------------

def test_pg_cross_release_compatibility_is_by_definition_id_never_by_code(engine, Sessions, db):
    """Ancienne release {C7_A: X, C7_B: Y}, nouvelle {C7_A: X réutilisée,
    C7_B: Z}. X reste compatible ; Y n'est JAMAIS remappée vers Z (même
    code) ; une observation multi-capacité ne garde que X ; competency_only
    d'une autre release est exclue ; celles de la nouvelle release sont
    incluses. Le run candidat de l'ancienne release ne peut plus devenir
    active."""
    old = _taxonomy(Sessions, ("C7_A", "C7_B"))
    x, y = old.d["C7_A"], old.d["C7_B"]
    o_x = _t3(Sessions, old.id, sup(old.m["C7_A"]))
    o_y = _t3(Sessions, old.id, sup(old.m["C7_B"]))
    o_xy = _t3(Sessions, old.id, contra(old.m["C7_A"], old.m["C7_B"]))
    o_only = _t3(Sessions, old.id, only())
    old_run = _start(Sessions, old.id)
    assert _inputs(engine, old_run) == {o_x.id, o_y.id, o_xy.id, o_only.id}

    new = _taxonomy(Sessions, {"C7_A": x, "C7_B": None})
    z = new.d["C7_B"]
    assert new.d["C7_A"] == x and z not in (x, y)
    n_z = _t3(Sessions, new.id, sup(new.m["C7_B"]))
    n_only = _t3(Sessions, new.id, only())
    run = svc.start_longitudinal_assessment(db, **_t5_kwargs(new.id))
    assert {i.observation_id for i in svc.get_longitudinal_inputs(db, run_id=run.id)} == {
        o_x.id, o_xy.id, n_z.id, n_only.id}
    assert run.input_fingerprint == _fp((o_x, 0, [x]), (o_xy, 0, [x]), (n_z, 0, [z]), (n_only, 0, []),
                                        release_id=new.id)
    db.commit()

    # Le scope compatible d'une observation historique est celui de X seul.
    dependency = svc.add_dependency(db, run_id=run.id, target_observation_id=o_xy.id, source_kind="observation",
                                    source_observation_id=o_x.id, dependency_type="dependent",
                                    scope_mode="whole_observation", dependency_basis={"why": "reprise"})
    assert dependency.scope_fingerprint == _scope_fp("whole_observation", x)
    for membership in (new.m["C7_B"], old.m["C7_A"], old.m["C7_B"]):
        with pytest.raises(InvalidRelationScope):
            svc.add_dependency(db, run_id=run.id, target_observation_id=o_xy.id, source_kind="observation",
                               source_observation_id=o_x.id, dependency_type="partially_dependent",
                               scope_mode="localized", dependency_basis={"why": "reprise"},
                               capability_membership_ids=[membership])
    localized = svc.add_dependency(db, run_id=run.id, target_observation_id=o_xy.id, source_kind="observation",
                                   source_observation_id=o_x.id, dependency_type="partially_dependent",
                                   scope_mode="localized", dependency_basis={"why": "reprise"},
                                   capability_membership_ids=[new.m["C7_A"]])
    assert localized.scope_fingerprint == _scope_fp("localized", x)
    svc.complete_longitudinal_assessment(db, run_id=run.id)
    db.commit()
    assert _state(engine, run.id) == ("completed", "active")
    with pytest.raises(TaxonomyReleaseNotUsable):
        svc.complete_longitudinal_assessment(db, run_id=old_run)
    db.rollback()
    assert _state(engine, old_run) == ("running", "candidate")


def test_pg_localized_observation_without_any_compatible_definition_is_excluded(engine, Sessions, db):
    old = _taxonomy(Sessions, ("C7_A", "C7_B"))
    observation = _t3(Sessions, old.id, sup(old.m["C7_A"], old.m["C7_B"]))
    new = _taxonomy(Sessions, ("C7_A", "C7_B"))  # deux nouvelles définitions, mêmes codes
    run = svc.start_longitudinal_assessment(db, **_t5_kwargs(new.id))
    assert svc.get_longitudinal_inputs(db, run_id=run.id) == []
    assert observation.id not in _inputs(engine, run.id)


# --- C. input_fingerprint sur données réelles --------------------------------------------

def test_pg_input_fingerprint_is_reproducible_and_tracks_the_dossier(engine, Sessions):
    tx = _taxonomy(Sessions, ("C7_A",))
    a = tx.m["C7_A"]
    first = _t3(Sessions, tx.id, sup(a))

    def fingerprint(release_id=tx.id):
        with Sessions() as session:
            run = svc.start_longitudinal_assessment(session, **_t5_kwargs(release_id))
            result = run.input_fingerprint, {i.observation_id for i in svc.get_longitudinal_inputs(
                session, run_id=run.id)}
            session.rollback()
            return result

    reference = fingerprint()
    assert fingerprint() == reference and reference[1] == {first.id}
    second = _t3(Sessions, tx.id, sup(a))
    added = fingerprint()
    assert added[0] != reference[0] and added[1] == {first.id, second.id}
    _invalidate(Sessions, second.id)
    assert fingerprint() == reference  # même dossier qu'au départ
    reevaluation = _t3(Sessions, tx.id, sup(a), event_id=first.event)
    superseded = fingerprint()
    assert superseded[0] not in (reference[0], added[0]) and superseded[1] == {reevaluation.id}
    new = _taxonomy(Sessions, {"C7_A": tx.d["C7_A"]})
    moved = fingerprint(new.id)
    assert moved[1] == superseded[1] and moved[0] != superseded[0]


# --- D. dépendances ------------------------------------------------------------------------

@pytest.fixture
def world(Sessions):
    """Release active C7_A/B/C + C8_A ; observations du user sur des
    événements distincts ; trois observations d'un même événement ; aides
    d'un autre événement, de l'événement de t1 et d'un autre user ; puis un
    run T5 commité sur ce dossier."""
    tx = _taxonomy(Sessions)
    A, B, C = tx.m["C7_A"], tx.m["C7_B"], tx.m["C7_C"]
    t1_event, same_event_trace = _event_with_trace(Sessions)
    _, other_event_trace = _event_with_trace(Sessions)
    _, foreign_trace = _event_with_trace(Sessions, user_id=OTHER_USER)
    same = _t3(Sessions, tx.id, sup(A), app(A), contra(A))
    w = SimpleNamespace(
        tx=tx, A=A, B=B, C=C, dA=tx.d["C7_A"], dB=tx.d["C7_B"], dC=tx.d["C7_C"],
        s1=_t3(Sessions, tx.id, sup(A, B)).id,
        s2=_t3(Sessions, tx.id, sup(A)).id,
        t1=_t3(Sessions, tx.id, app(A, B), event_id=t1_event).id,
        t2=_t3(Sessions, tx.id, app(A)).id,
        k1=_t3(Sessions, tx.id, contra(A, B)).id,
        k2=_t3(Sessions, tx.id, contra(C)).id,
        k_only=_t3(Sessions, tx.id, only(contra())).id,
        none=_t3(Sessions, tx.id, sup(A, stage="none")).id,
        app_only=_t3(Sessions, tx.id, only(app())).id,
        s_only=_t3(Sessions, tx.id, only(sup())).id,
        tB=_t3(Sessions, tx.id, app(B)).id,
        p_sup=same.ids[0], p_app=same.ids[1], p_contra=same.ids[2],
        same_event_trace=same_event_trace, other_event_trace=other_event_trace, foreign_trace=foreign_trace,
    )
    w.run = _start(Sessions, tx.id)
    return w


def _dependency(db, w, target, source=None, *, trace=None, mode="whole_observation", caps=(), kind="dependent",
                basis=None):
    return svc.add_dependency(
        db, run_id=w.run, target_observation_id=target,
        source_kind="support_trace" if trace else "observation",
        source_observation_id=None if trace else source, source_support_trace_id=trace,
        dependency_type=kind, scope_mode=mode, dependency_basis=basis or {"why": "reprise d'un épisode antérieur"},
        capability_membership_ids=list(caps))


def _transfer(db, w, source, target, *, mode="whole_observation", caps=(), basis=None):
    return svc.add_transfer(db, run_id=w.run, source_observation_id=source, target_observation_id=target,
                            scope_mode=mode, transfer_basis=basis or {"adaptation": "raisonnement réorganisé"},
                            capability_membership_ids=list(caps))


def _revalidation(db, w, source, target, *, mode="whole_observation", caps=(), basis=None):
    return svc.add_revalidation(db, run_id=w.run, source_contradiction_observation_id=source,
                                target_supportive_observation_id=target, scope_mode=mode,
                                revalidation_basis=basis or {"mechanism": "levier démontré sans aide"},
                                capability_membership_ids=list(caps))


def test_pg_dependency_between_observations_of_distinct_events(engine, Sessions, db, world, clock):
    w = world
    now = clock()
    basis = {"why": "réutilise la décomposition DuPont vue en E1", "refs": [{"work_index": 0}]}
    dependency = _dependency(db, w, w.t1, w.s1, mode="localized", caps=[w.A], basis=basis)
    assert isinstance(dependency, ObservationDependency)
    assert (dependency.run_id, dependency.target_observation_id, dependency.source_kind,
            dependency.source_observation_id, dependency.source_support_trace_id, dependency.dependency_type,
            dependency.scope_mode) == (w.run, w.t1, "observation", w.s1, None, "dependent", "localized")
    assert dependency.dependency_basis == basis and dependency.dependency_basis is not basis
    basis["refs"].append("mutation")
    assert dependency.dependency_basis["refs"] == [{"work_index": 0}]
    assert dependency.scope_fingerprint == _scope_fp("localized", w.dA)
    assert dependency.created_at == now
    assert _count(engine, "observation_dependencies") == 0  # flushé, pas commité
    db.commit()
    assert [(d.id, caps) for d, caps in svc.get_dependencies(db, run_id=w.run)] == [(dependency.id, (w.A,))]
    assert _count(engine, "dependency_capabilities") == 1


def test_pg_dependency_on_a_support_trace_of_another_event(engine, Sessions, db, world):
    w = world
    dependency = _dependency(db, w, w.t1, trace=w.other_event_trace, kind="partially_dependent")
    assert (dependency.source_kind, dependency.source_observation_id, dependency.source_support_trace_id) == (
        "support_trace", None, w.other_event_trace)
    # whole_observation : scope compatible complet de la cible, aucune ligne.
    assert dependency.scope_fingerprint == _scope_fp("whole_observation", w.dA, w.dB)
    db.commit()
    assert _count(engine, "dependency_capabilities") == 0
    assert svc.get_dependencies(db, run_id=w.run)[0][1] == ()


def test_pg_support_trace_sources_are_checked(engine, Sessions, db, world):
    w = world
    with pytest.raises(InvalidDependency, match="autre utilisateur"):
        _dependency(db, w, w.t1, trace=w.foreign_trace)
    with pytest.raises(InvalidDependency, match="même CognitiveEvent"):
        _dependency(db, w, w.t1, trace=w.same_event_trace)
    with pytest.raises(RelationSourceNotFound):
        _dependency(db, w, w.t1, trace=uuid.uuid4())
    # L'aide de l'événement de t1 reste une source valide pour une AUTRE cible.
    _dependency(db, w, w.s1, trace=w.same_event_trace)
    assert _count(engine, "observation_dependencies") == 0


def test_pg_dependency_between_observations_of_the_same_event_is_refused(engine, db, world):
    with pytest.raises(InvalidDependency, match="même CognitiveEvent"):
        _dependency(db, world, world.p_app, world.p_sup)


def test_pg_dependency_endpoints_must_be_in_the_run_snapshot(engine, Sessions, db, world):
    w = world
    late = _t3(Sessions, w.tx.id, sup(w.A)).id  # admissible, mais apparue après start
    foreign = _t3(Sessions, w.tx.id, sup(w.A), user_id=OTHER_USER).id
    for target, source in ((late, w.s1), (foreign, w.s1), (w.t1, late), (w.t1, foreign)):
        with pytest.raises(ObservationNotEligible):
            _dependency(db, w, target, source)
    with pytest.raises(RelationSourceNotFound):
        _dependency(db, w, uuid.uuid4(), w.s1)
    with pytest.raises(RelationSourceNotFound):
        _dependency(db, w, w.t1, uuid.uuid4())


def test_pg_dependency_scope_is_checked_against_the_target(engine, Sessions, db, world):
    w = world
    other = _taxonomy(Sessions, ("C7_A",), activate=False)
    for caps in ([other.m["C7_A"]], [w.tx.m["C8_A"]], [w.C], [w.A, w.C], [uuid.uuid4()]):
        with pytest.raises(InvalidRelationScope):
            _dependency(db, w, w.t1, w.s1, mode="localized", caps=caps)
    # Cible competency_only : aucune capacité inventée.
    with pytest.raises(InvalidRelationScope):
        _dependency(db, w, w.app_only, w.s1, mode="localized", caps=[w.A])
    whole = _dependency(db, w, w.app_only, w.s1)
    only_scope = _dependency(db, w, w.t2, w.s1, mode="competency_only")
    both = _dependency(db, w, w.t1, w.s2, mode="localized", caps=[w.B, w.A])
    assert whole.scope_fingerprint == _scope_fp("whole_observation")
    assert only_scope.scope_fingerprint == _scope_fp("competency_only")
    assert both.scope_fingerprint == _scope_fp("localized", w.dA, w.dB)
    db.commit()
    assert _count(engine, "dependency_capabilities") == 2


def test_pg_duplicate_dependency_is_refused_distinct_scopes_and_sources_are_not(engine, Sessions, db, world):
    w = world
    _dependency(db, w, w.t1, w.s1, mode="localized", caps=[w.A])
    with pytest.raises(DuplicateLongitudinalRelation):
        _dependency(db, w, w.t1, w.s1, mode="localized", caps=[w.A], basis={"why": "autre formulation"})
    # Même source / cible, scope réellement distinct : autorisé.
    _dependency(db, w, w.t1, w.s1, mode="localized", caps=[w.B], kind="partially_dependent")
    _dependency(db, w, w.t1, w.s1, mode="competency_only")
    # Même cible, autres sources : autorisé (quel que soit le type).
    _dependency(db, w, w.t1, trace=w.other_event_trace, mode="localized", caps=[w.A], kind="partially_dependent")
    _dependency(db, w, w.t1, w.s2, mode="localized", caps=[w.A])
    db.commit()
    assert _count(engine, "observation_dependencies") == 5
    with pytest.raises(DuplicateLongitudinalRelation):
        _dependency(db, w, w.t1, w.s1, mode="competency_only")


@pytest.mark.parametrize("first, second", [("dependent", "partially_dependent"),
                                           ("partially_dependent", "dependent")])
def test_pg_same_dependency_cannot_be_classified_twice(engine, Sessions, db, world, first, second):
    """dependency_type qualifie UNE dépendance : même cible, même source,
    même scope_fingerprint => même dépendance ; un second type
    contradictoire est refusé (InvalidDependency), le même type est un
    doublon (DuplicateLongitudinalRelation)."""
    w = world
    same = (
        lambda kind: _dependency(db, w, w.t1, w.s1, mode="localized", caps=[w.A], kind=kind),
        lambda kind: _dependency(db, w, w.t1, w.s1, mode="competency_only", kind=kind),
        lambda kind: _dependency(db, w, w.t2, w.s1, kind=kind),
        lambda kind: _dependency(db, w, w.t1, trace=w.other_event_trace, mode="localized", caps=[w.B], kind=kind),
    )
    for add in same:
        assert add(first).dependency_type == first
        with pytest.raises(InvalidDependency, match="classification contradictoire de la même dépendance"):
            add(second)
        with pytest.raises(DuplicateLongitudinalRelation):
            add(first)
    db.commit()
    assert _count(engine, "observation_dependencies") == len(same)
    assert _count(engine, "observation_dependencies", "dependency_type = :t", t=second) == 0
    svc.complete_longitudinal_assessment(db, run_id=w.run)
    db.commit()
    assert _state(engine, w.run) == ("completed", "active")


# --- E. transferts -------------------------------------------------------------------------

def test_pg_transfer_supportive_to_application(engine, Sessions, db, world, clock):
    w = world
    now = clock()
    basis = {"adaptation": "a recalculé la marge sur une structure de coûts différente"}
    transfer = _transfer(db, w, w.s1, w.t1, mode="localized", caps=[w.A], basis=basis)
    assert (transfer.run_id, transfer.source_observation_id, transfer.target_observation_id, transfer.scope_mode,
            transfer.transfer_basis, transfer.created_at) == (w.run, w.s1, w.t1, "localized", basis, now)
    assert transfer.scope_fingerprint == _scope_fp("localized", w.dA)
    # whole_observation : intersection compatible source / cible.
    assert _transfer(db, w, w.s2, w.t1).scope_fingerprint == _scope_fp("whole_observation", w.dA)
    assert _transfer(db, w, w.s1, w.app_only, mode="competency_only").scope_fingerprint == _scope_fp(
        "competency_only")
    db.commit()
    assert [(t.id, caps) for t, caps in svc.get_transfers(db, run_id=w.run)][0] == (transfer.id, (w.A,))


@pytest.mark.parametrize("pair, match", [
    (("k1", "t1"), "source contradictory"),
    (("s1", "k1"), "cible contradictory"),
    (("s1", "s2"), "local_stage comprehension"),
    (("s1", "none"), "local_stage none"),
    (("p_sup", "p_app"), "même CognitiveEvent"),
])
def test_pg_transfer_semantics_are_enforced(engine, db, world, pair, match):
    w = world
    with pytest.raises(InvalidTransfer, match=match):
        _transfer(db, w, getattr(w, pair[0]), getattr(w, pair[1]))


@pytest.mark.parametrize("mode, caps", [("localized", "A"), ("localized", "B"), ("whole_observation", None),
                                         ("competency_only", None)])
def test_pg_transfer_between_localized_observations_without_common_scope_is_refused(engine, db, world, mode,
                                                                                    caps):
    """localized C7_A -> localized C7_B Application : intersection vide =>
    aucun raisonnement commun, refusé AVANT la résolution du scope_mode
    (aucun contournement par whole_observation ou competency_only)."""
    w = world
    with pytest.raises(InvalidTransfer, match="aucun raisonnement commun"):
        _transfer(db, w, w.s2, w.tB, mode=mode, caps=[getattr(w, caps)] if caps else [])
    db.commit()
    assert _count(engine, "observation_transfers") == 0


def test_pg_transfer_valid_granularities(engine, Sessions, db, world):
    """Restent valides : C7_A -> C7_A, C7_A/B -> C7_A, localized ->
    competency_only et competency_only -> localized (granularité capability
    inconnue : représentable au niveau compétence)."""
    w = world
    assert _transfer(db, w, w.s2, w.t2, mode="localized", caps=[w.A]).scope_fingerprint == _scope_fp(
        "localized", w.dA)
    assert _transfer(db, w, w.s1, w.t2, mode="localized", caps=[w.A]).scope_fingerprint == _scope_fp(
        "localized", w.dA)
    assert _transfer(db, w, w.s1, w.app_only, mode="competency_only").scope_fingerprint == _scope_fp(
        "competency_only")
    assert _transfer(db, w, w.s_only, w.t1).scope_fingerprint == _scope_fp("whole_observation")
    assert _transfer(db, w, w.s_only, w.tB, mode="competency_only").scope_fingerprint == _scope_fp(
        "competency_only")
    with pytest.raises(InvalidRelationScope):
        _transfer(db, w, w.s_only, w.t1, mode="localized", caps=[w.A])
    db.commit()
    svc.complete_longitudinal_assessment(db, run_id=w.run)
    db.commit()
    assert _state(engine, w.run) == ("completed", "active")
    assert len(svc.get_transfers(db, run_id=w.run)) == 5


def test_pg_transfer_scope_must_stay_within_the_intersection(engine, Sessions, db, world):
    w = world
    other = _taxonomy(Sessions, ("C7_A",), activate=False)
    for source, target, caps in ((w.s2, w.t1, [w.B]), (w.s1, w.t2, [w.B]), (w.s1, w.t1, [w.C]),
                                 (w.s1, w.t1, [other.m["C7_A"]]), (w.s1, w.app_only, [w.A])):
        with pytest.raises(InvalidRelationScope):
            _transfer(db, w, source, target, mode="localized", caps=caps)
    late = _t3(Sessions, w.tx.id, app(w.A)).id
    with pytest.raises(ObservationNotEligible):
        _transfer(db, w, w.s1, late)


def test_pg_dependency_and_transfer_cannot_share_a_scope(engine, Sessions, db, world):
    """Un transfert autonome et une dépendance directe vers la même cible
    sont incompatibles sur un même scope, quel que soit l'ordre ; des
    parties réellement distinctes (C7_A dépendante, C7_B transférée)
    coexistent ; un scope non localized chevauche toujours."""
    w = world
    _dependency(db, w, w.t1, trace=w.other_event_trace, mode="localized", caps=[w.A])
    for mode, caps in (("localized", [w.A]), ("localized", [w.A, w.B]), ("whole_observation", []),
                       ("competency_only", [])):
        with pytest.raises(InvalidTransfer, match="dépendance"):
            _transfer(db, w, w.s1, w.t1, mode=mode, caps=caps)
    _transfer(db, w, w.s1, w.t1, mode="localized", caps=[w.B])
    # Sens inverse : le transfert C7_B existe, une dépendance C7_B est refusée.
    for kind in ("dependent", "partially_dependent"):
        with pytest.raises(InvalidDependency, match="transfert"):
            _dependency(db, w, w.t1, w.s2, mode="localized", caps=[w.B], kind=kind)
    with pytest.raises(InvalidDependency, match="transfert"):
        _dependency(db, w, w.t1, w.s2, mode="competency_only")
    # Autre cible : aucun conflit.
    _dependency(db, w, w.t2, w.s1, mode="localized", caps=[w.A])
    db.commit()
    svc.complete_longitudinal_assessment(db, run_id=w.run)
    db.commit()
    assert _state(engine, w.run) == ("completed", "active")


def test_pg_transfer_is_never_detected_only_persisted(engine, Sessions, db, world):
    """Aucune heuristique : un dossier contenant deux applications sur des
    événements (tickers, dates) différents ne produit AUCUN transfert tant
    que l'évaluateur n'en propose pas ; un basis proposé est conservé tel
    quel, sans interprétation."""
    w = world
    svc.complete_longitudinal_assessment(db, run_id=w.run)
    db.commit()
    assert svc.get_transfers(db, run_id=w.run) == [] and svc.get_dependencies(db, run_id=w.run) == []
    assert svc.get_revalidations(db, run_id=w.run) == []
    run_id = _start(Sessions, w.tx.id)
    basis = {"variation": "ticker différent", "ticker_source": "MC.PA", "ticker_target": "OR.PA"}
    transfer = svc.add_transfer(db, run_id=run_id, source_observation_id=w.t2, target_observation_id=w.t1,
                                scope_mode="whole_observation", transfer_basis=basis)
    assert transfer.transfer_basis == basis
    assert svc.get_transfers(db, run_id=run_id) == [(transfer, ())]


# --- F. revalidations ------------------------------------------------------------------------

def test_pg_revalidation_contradictory_to_supportive_keeps_the_contradiction(engine, Sessions, db, world, clock):
    w = world
    before = _evidence_state(engine)
    now = clock()
    revalidation = _revalidation(db, w, w.k1, w.t1, mode="localized", caps=[w.A])
    assert (revalidation.source_contradiction_observation_id, revalidation.target_supportive_observation_id,
            revalidation.scope_mode, revalidation.created_at) == (w.k1, w.t1, "localized", now)
    assert revalidation.scope_fingerprint == _scope_fp("localized", w.dA)
    assert _revalidation(db, w, w.k1, w.s2).scope_fingerprint == _scope_fp("whole_observation", w.dA)
    # Contradiction competency_only : pas deux localized, la précision
    # causale vient du basis ; aucune capacité commune fabriquée.
    assert _revalidation(db, w, w.k_only, w.s1, mode="competency_only").scope_fingerprint == _scope_fp(
        "competency_only")
    assert _revalidation(db, w, w.k_only, w.s1).scope_fingerprint == _scope_fp("whole_observation")
    db.commit()
    svc.complete_longitudinal_assessment(db, run_id=w.run)
    db.commit()
    assert _evidence_state(engine) == before  # contradiction intacte, aucune observation ajoutée
    assert len(svc.get_revalidations(db, run_id=w.run)) == 4


@pytest.mark.parametrize("pair, match", [
    (("s1", "t1"), "source supportive"),
    (("k1", "k2"), "cible contradictory"),
    (("p_contra", "p_sup"), "même CognitiveEvent"),
    (("k2", "s1"), "aucun mécanisme commun"),
])
def test_pg_revalidation_semantics_are_enforced(engine, db, world, pair, match):
    w = world
    for mode in ("whole_observation", "competency_only"):
        with pytest.raises(InvalidRevalidation, match=match):
            _revalidation(db, w, getattr(w, pair[0]), getattr(w, pair[1]), mode=mode)


def test_pg_revalidation_scope_must_stay_within_the_intersection(engine, Sessions, db, world):
    w = world
    for source, target, caps in ((w.k1, w.s2, [w.B]), (w.k1, w.t1, [w.C]), (w.k_only, w.s1, [w.A])):
        with pytest.raises(InvalidRelationScope):
            _revalidation(db, w, source, target, mode="localized", caps=caps)
    late = _t3(Sessions, w.tx.id, contra(w.A)).id
    with pytest.raises(ObservationNotEligible):
        _revalidation(db, w, late, w.t1)


def test_pg_revalidation_is_refused_on_a_scope_with_an_identified_dependency(engine, db, world):
    """L'absence d'arête n'établit jamais l'indépendance, mais une
    dépendance identifiée de la cible sur le même scope interdit la
    revalidation (dans les deux ordres d'ajout)."""
    w = world
    _dependency(db, w, w.t1, w.s1, mode="localized", caps=[w.A], kind="partially_dependent")
    for mode, caps in (("localized", [w.A]), ("whole_observation", []), ("competency_only", [])):
        with pytest.raises(InvalidRevalidation, match="dépendance"):
            _revalidation(db, w, w.k1, w.t1, mode=mode, caps=caps)
    _revalidation(db, w, w.k1, w.t1, mode="localized", caps=[w.B])
    with pytest.raises(InvalidDependency, match="revalidation"):
        _dependency(db, w, w.t1, trace=w.other_event_trace, mode="localized", caps=[w.B])
    with pytest.raises(DuplicateLongitudinalRelation):
        _revalidation(db, w, w.k1, w.t1, mode="localized", caps=[w.B])


# --- G. complete ------------------------------------------------------------------------------

def test_pg_complete_activates_and_supersedes_the_previous_active(engine, Sessions, db, clock):
    tx = _taxonomy(Sessions, ("C7_A",))
    _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    first = _start(Sessions, tx.id)
    _complete(Sessions, first)
    first_row = _run_row(engine, first)
    failed = _start(Sessions, tx.id)
    _commit(Sessions, svc.fail_longitudinal_assessment, run_id=failed)
    second = _start(Sessions, tx.id)
    now = clock()
    run = svc.complete_longitudinal_assessment(db, run_id=second)
    assert (run.execution_status, run.interpretation_status, run.completed_at, run.failure_code) == (
        "completed", "active", now, None)
    assert _state(engine, first) == ("completed", "active")  # rien de commité
    db.commit()
    assert _state(engine, second) == ("completed", "active")
    assert _state(engine, first) == ("completed", "superseded")
    assert _state(engine, failed) == ("failed", "obsolete")
    # L'ancien active ne change que d'interpretation_status.
    assert {k: v for k, v in _run_row(engine, first).items() if k != "interpretation_status"} == {
        k: v for k, v in first_row.items() if k != "interpretation_status"}
    assert svc.get_active_longitudinal_assessment(db, user_id=USER, competency_code="C7").id == second
    assert svc.get_active_longitudinal_assessment(db, user_id=USER, competency_code="C8") is None
    assert svc.get_active_longitudinal_assessment(db, user_id=OTHER_USER, competency_code="C7") is None


def test_pg_complete_lock_order_and_flush_order(engine, Sessions, db):
    """advisory -> run candidat FOR NO KEY UPDATE -> release FOR SHARE ->
    active courant FOR NO KEY UPDATE ; l'UPDATE superseded précède l'UPDATE
    active quel que soit l'ordre des clés primaires (index unique partiel
    non différable)."""
    tx = _taxonomy(Sessions, ("C7_A",))
    _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    active = _start(Sessions, tx.id)
    _complete(Sessions, active)
    for lower in (True, False):
        candidate = _start(Sessions, tx.id)
        while (candidate < active) != lower:
            candidate = _start(Sessions, tx.id)
        statements, record = _recording(engine)
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            svc.complete_longitudinal_assessment(db, run_id=candidate)
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        assert statements[0][0].startswith("SELECT longitudinal_assessment_runs.user_id, "
                                           "longitudinal_assessment_runs.competency_code FROM")
        assert statements[1][0].startswith(ON_ADVISORY)
        assert svc._activation_lock_key(USER, "C7") in statements[1][1].values()
        locks = [(sql, p) for sql, p in statements if " FOR " in sql]
        assert [sql.rsplit(" FOR ", 1)[1] for sql, _ in locks] == ["NO KEY UPDATE", "SHARE", "NO KEY UPDATE"]
        assert locks[0][0].startswith(ON_RUN) and candidate in locks[0][1].values()
        assert locks[1][0].startswith(ON_RELEASE) and tx.id in locks[1][1].values()
        assert locks[2][0].startswith(ON_RUN) and "longitudinal_assessment_runs.user_id = " in locks[2][0]
        assert statements.index(locks[0]) == 2
        updates = [p for sql, p in statements if sql.startswith("UPDATE longitudinal_assessment_runs")]
        assert [p["interpretation_status"] for p in updates] == ["superseded", "active"]
        assert updates[0]["longitudinal_assessment_runs_id"] == active
        assert updates[1]["longitudinal_assessment_runs_id"] == candidate
        assert not [s for s, _ in statements if s.startswith(("INSERT", "DELETE"))]
        db.commit()
        active = candidate


@pytest.mark.parametrize("end", ["complete", "fail"])
def test_pg_second_transition_is_refused_never_a_no_op(engine, Sessions, db, end):
    tx = _taxonomy(Sessions, ("C7_A",))
    run_id = _start(Sessions, tx.id)
    if end == "complete":
        _complete(Sessions, run_id)
    else:
        _commit(Sessions, svc.fail_longitudinal_assessment, run_id=run_id)
    before = _run_row(engine, run_id)
    for call in (lambda: svc.complete_longitudinal_assessment(db, run_id=run_id),
                 lambda: svc.fail_longitudinal_assessment(db, run_id=run_id, failure_code="x")):
        with pytest.raises(InvalidLongitudinalState):
            call()
    assert _run_row(engine, run_id) == before


def test_pg_unknown_runs(db):
    for call in (svc.complete_longitudinal_assessment, svc.fail_longitudinal_assessment,
                 svc.get_longitudinal_assessment, svc.get_longitudinal_inputs, svc.get_dependencies,
                 svc.get_transfers, svc.get_revalidations):
        with pytest.raises(LongitudinalAssessmentNotFound):
            call(db, run_id=uuid.uuid4())
    with pytest.raises(LongitudinalAssessmentNotFound):
        svc.add_transfer(db, **_transfer_kwargs())


def _exec(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(sa.text(sql), params)


def _sql(db, sql, **params):
    db.execute(sa.text(sql), params)


def _raw_edge(db, table, **values):
    """Arête insérée en contournant le service (données incohérentes)."""
    values = {"id": uuid.uuid4(), "created_at": datetime.now(timezone.utc), **values}
    basis = next(k for k in values if k.endswith("_basis"))
    values[basis] = json.dumps(values[basis])
    columns = ", ".join(values)
    placeholders = ", ".join(f"CAST(:{k} AS JSONB)" if k == basis else f":{k}" for k in values)
    _sql(db, f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", **values)
    return values["id"]


def _corrupt(db, w, case):
    """Corruption directe (hors service) d'un run cohérent, DANS la
    transaction du test (DDL compris, annulé par son rollback) ; retourne
    l'exception attendue à la complétion."""
    whole_t1 = _scope_fp("whole_observation", w.dA, w.dB)
    if case == "input_row_deleted":
        _sql(db, "DELETE FROM longitudinal_assessment_inputs WHERE run_id = :r AND observation_id = :o",
              r=w.run, o=w.s2)
        return StaleLongitudinalSnapshot
    if case == "input_row_added":
        extra = w.other_user_observation
        _sql(db, "INSERT INTO longitudinal_assessment_inputs (run_id, observation_id) VALUES (:r, :o)",
              r=w.run, o=extra)
        return StaleLongitudinalSnapshot
    if case == "input_fingerprint":
        _sql(db, "UPDATE longitudinal_assessment_runs SET input_fingerprint = 'falsifié' WHERE id = :r",
              r=w.run)
        return StaleLongitudinalSnapshot
    if case == "scope_fingerprint":
        _sql(db, "UPDATE observation_dependencies SET scope_fingerprint = :f WHERE run_id = :r",
              f=_scope_fp("localized", w.dB), r=w.run)
        return InvalidRelationScope
    if case == "localized_without_capability":
        _sql(db, "DELETE FROM dependency_capabilities")
        return InvalidRelationScope
    if case == "capability_of_another_release":
        _sql(db, "UPDATE dependency_capabilities SET capability_membership_id = :m",
              m=w.other_release.m["C7_A"])
        return InvalidRelationScope
    if case == "empty_basis":
        _sql(db, "UPDATE observation_dependencies SET dependency_basis = '{}'::jsonb")
        return InvalidDependency
    if case == "dependency_same_event":
        _raw_edge(db, "observation_dependencies", run_id=w.run, target_observation_id=w.p_app,
                  source_kind="observation", source_observation_id=w.p_sup, dependency_type="dependent",
                  scope_mode="whole_observation", dependency_basis={"why": "x"},
                  scope_fingerprint=_scope_fp("whole_observation", w.dA))
        return InvalidDependency
    if case == "dependency_foreign_trace":
        _raw_edge(db, "observation_dependencies", run_id=w.run, target_observation_id=w.s1,
                  source_kind="support_trace", source_support_trace_id=w.foreign_trace, dependency_type="dependent",
                  scope_mode="whole_observation", dependency_basis={"why": "x"},
                  scope_fingerprint=_scope_fp("whole_observation", w.dA, w.dB))
        return InvalidDependency
    if case == "dependency_independent_type":
        _sql(db, "ALTER TABLE observation_dependencies DROP CONSTRAINT ck_observation_dependencies_dependency_type")
        _sql(db, "UPDATE observation_dependencies SET dependency_type = 'independent'")
        return InvalidDependency
    if case == "transfer_target_not_application":
        _raw_edge(db, "observation_transfers", run_id=w.run, source_observation_id=w.t2,
                  target_observation_id=w.s2, scope_mode="whole_observation", transfer_basis={"a": "x"},
                  scope_fingerprint=_scope_fp("whole_observation", w.dA))
        return InvalidTransfer
    if case == "transfer_overlapping_dependency":
        _raw_edge(db, "observation_transfers", run_id=w.run, source_observation_id=w.s1,
                  target_observation_id=w.t1, scope_mode="whole_observation", transfer_basis={"a": "x"},
                  scope_fingerprint=whole_t1)
        return InvalidTransfer
    if case in ("dependency_contradictory_type", "dependency_duplicate_same_type"):
        dependency_id = _raw_edge(db, "observation_dependencies", run_id=w.run, target_observation_id=w.t1,
                                  source_kind="observation", source_observation_id=w.s1,
                                  dependency_type="partially_dependent" if case == "dependency_contradictory_type"
                                  else "dependent", scope_mode="localized", dependency_basis={"why": "copie"},
                                  scope_fingerprint=_scope_fp("localized", w.dA))
        _sql(db, "INSERT INTO dependency_capabilities (dependency_id, capability_membership_id) VALUES (:d, :m)",
             d=dependency_id, m=w.A)
        return InvalidDependency if case == "dependency_contradictory_type" else DuplicateLongitudinalRelation
    if case.startswith("transfer_localized_disjoint_"):
        mode = case.removeprefix("transfer_localized_disjoint_")
        _raw_edge(db, "observation_transfers", run_id=w.run, source_observation_id=w.s2,
                  target_observation_id=w.tB, scope_mode=mode, transfer_basis={"a": "x"},
                  scope_fingerprint=_scope_fp(mode))
        return InvalidTransfer
    if case == "duplicate_transfer":
        _raw_edge(db, "observation_transfers", run_id=w.run, source_observation_id=w.s2,
                  target_observation_id=w.t2, scope_mode="whole_observation", transfer_basis={"a": "copie"},
                  scope_fingerprint=_scope_fp("whole_observation", w.dA))
        return DuplicateLongitudinalRelation
    if case == "revalidation_polarity":
        _raw_edge(db, "observation_revalidations", run_id=w.run, source_contradiction_observation_id=w.s2,
                  target_supportive_observation_id=w.t2, scope_mode="whole_observation",
                  revalidation_basis={"m": "x"}, scope_fingerprint=_scope_fp("whole_observation", w.dA))
        return InvalidRevalidation
    if case == "revalidation_overlapping_dependency":
        _raw_edge(db, "observation_revalidations", run_id=w.run, source_contradiction_observation_id=w.k1,
                  target_supportive_observation_id=w.t1, scope_mode="competency_only",
                  revalidation_basis={"m": "x"}, scope_fingerprint=_scope_fp("competency_only"))
        return InvalidRevalidation
    if case == "edge_outside_snapshot":
        _raw_edge(db, "observation_transfers", run_id=w.run, source_observation_id=w.s2,
                  target_observation_id=w.other_user_observation, scope_mode="competency_only",
                  transfer_basis={"a": "x"}, scope_fingerprint=_scope_fp("competency_only"))
        return ObservationNotEligible
    raise AssertionError(case)


CORRUPTIONS = ["input_row_deleted", "input_row_added", "input_fingerprint", "scope_fingerprint",
               "localized_without_capability", "capability_of_another_release", "empty_basis",
               "dependency_same_event", "dependency_foreign_trace", "dependency_independent_type",
               "dependency_contradictory_type", "dependency_duplicate_same_type",
               "transfer_localized_disjoint_whole_observation", "transfer_localized_disjoint_competency_only",
               "transfer_target_not_application", "transfer_overlapping_dependency", "duplicate_transfer",
               "revalidation_polarity", "revalidation_overlapping_dependency", "edge_outside_snapshot"]


@pytest.mark.parametrize("case", CORRUPTIONS)
def test_pg_complete_revalidates_everything_persisted(engine, Sessions, db, world, case):
    """Défense contre une écriture ORM / SQL directe ayant contourné le
    service : lignes d'input, input_fingerprint, chaque relation (extrémités,
    polarités, stade, événements, aide, vocabulaire, basis, scope,
    scope_fingerprint), doublons et chevauchements. Rien n'est muté."""
    w = world
    w.other_user_observation = _t3(Sessions, w.tx.id, sup(w.A), user_id=OTHER_USER).id
    w.other_release = _taxonomy(Sessions, ("C7_A",), activate=False)
    _commit(Sessions, lambda s, **k: _dependency(s, w, w.t1, w.s1, mode="localized", caps=[w.A]))
    _commit(Sessions, lambda s, **k: _transfer(s, w, w.s2, w.t2))
    _commit(Sessions, lambda s, **k: _revalidation(s, w, w.k1, w.s2))
    before = _run_row(engine, w.run)
    expected = _corrupt(db, w, case)
    with pytest.raises(expected):
        svc.complete_longitudinal_assessment(db, run_id=w.run)
    assert svc.get_longitudinal_assessment(db, run_id=w.run).interpretation_status == "candidate"
    db.rollback()
    assert _run_row(engine, w.run) == before
    assert _count(engine, "longitudinal_assessment_runs", "interpretation_status = 'active'") == 0
    assert _count(engine, "observation_dependencies", "dependency_type = 'independent'") == 0


def test_pg_consistent_relations_complete(engine, Sessions, db, world):
    w = world
    _dependency(db, w, w.t1, w.s1, mode="localized", caps=[w.A])
    _dependency(db, w, w.t1, trace=w.other_event_trace, mode="localized", caps=[w.A], kind="partially_dependent")
    _transfer(db, w, w.s2, w.t2)
    _transfer(db, w, w.s1, w.t1, mode="localized", caps=[w.B])
    _revalidation(db, w, w.k1, w.s2)
    _revalidation(db, w, w.k1, w.t1, mode="localized", caps=[w.B])
    db.commit()
    svc.complete_longitudinal_assessment(db, run_id=w.run)
    db.commit()
    assert _state(engine, w.run) == ("completed", "active")
    assert [len(svc.get_dependencies(db, run_id=w.run)), len(svc.get_transfers(db, run_id=w.run)),
            len(svc.get_revalidations(db, run_id=w.run))] == [2, 2, 2]


# --- H. snapshot périmé ---------------------------------------------------------------------------

@pytest.mark.parametrize("change", ["new_observation", "invalidated", "t3_superseded", "t3_run_added_elsewhere"])
def test_pg_stale_snapshot_is_never_activated_nor_rewritten(engine, Sessions, db, change):
    tx = _taxonomy(Sessions, ("C7_A",))
    first = _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    run_id = _start(Sessions, tx.id)
    before, inputs = _run_row(engine, run_id), _inputs(engine, run_id)
    if change == "new_observation":
        _t3(Sessions, tx.id, contra(tx.m["C7_A"]))
    elif change == "invalidated":
        _invalidate(Sessions, first.id)
    elif change == "t3_superseded":
        _t3(Sessions, tx.id, sup(tx.m["C7_A"]), event_id=first.event)
    else:
        # Une observation d'un autre user / d'une autre compétence ne
        # périme pas le dossier.
        _t3(Sessions, tx.id, sup(tx.m["C7_A"]), user_id=OTHER_USER)
        _t3(Sessions, tx.id, sup(competency_code="C8", capability_localization="competency_only"))
        svc.complete_longitudinal_assessment(db, run_id=run_id)
        db.commit()
        assert _state(engine, run_id) == ("completed", "active")
        return
    with pytest.raises(StaleLongitudinalSnapshot):
        svc.complete_longitudinal_assessment(db, run_id=run_id)
    db.rollback()
    assert _run_row(engine, run_id) == before and _inputs(engine, run_id) == inputs
    # Aucune magie : l'orchestrateur clôt explicitement le run.
    svc.fail_longitudinal_assessment(db, run_id=run_id, failure_code="stale_input")
    db.commit()
    row = _run_row(engine, run_id)
    assert (row["execution_status"], row["interpretation_status"], row["failure_code"]) == (
        "failed", "obsolete", "stale_input")
    assert row["input_fingerprint"] == before["input_fingerprint"] and _inputs(engine, run_id) == inputs


def test_pg_an_old_computation_never_overwrites_a_newer_dossier(engine, Sessions, db):
    """A démarre sur O1 ; O2 apparaît ; B démarre sur O1 + O2 et devient
    active ; A ne peut plus compléter et B reste active."""
    tx = _taxonomy(Sessions, ("C7_A",))
    o1 = _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    a = _start(Sessions, tx.id)
    o2 = _t3(Sessions, tx.id, app(tx.m["C7_A"]))
    b = _start(Sessions, tx.id)
    assert (_inputs(engine, a), _inputs(engine, b)) == ({o1.id}, {o1.id, o2.id})
    _complete(Sessions, b)
    with pytest.raises(StaleLongitudinalSnapshot):
        svc.complete_longitudinal_assessment(db, run_id=a)
    db.rollback()
    assert (_state(engine, a), _state(engine, b)) == (("running", "candidate"), ("completed", "active"))
    # Si le dossier courant redevient exactement celui de A, A est légitime.
    _invalidate(Sessions, o2.id)
    _complete(Sessions, a)
    assert (_state(engine, a), _state(engine, b)) == (("completed", "active"), ("completed", "superseded"))


def test_pg_inconsistent_previous_active_is_a_business_error(engine, Sessions, db):
    tx = _taxonomy(Sessions, ("C7_A",))
    previous = _start(Sessions, tx.id)
    _exec(engine, "UPDATE longitudinal_assessment_runs SET interpretation_status = 'active' WHERE id = :r",
          r=previous)  # running / active : incohérent
    candidate = _start(Sessions, tx.id)
    with pytest.raises(InvalidLongitudinalState, match="running"):
        svc.complete_longitudinal_assessment(db, run_id=candidate)
    db.rollback()
    assert _state(engine, candidate) == ("running", "candidate")


def test_pg_several_actives_are_never_resolved_arbitrarily(engine, Sessions, db):
    tx = _taxonomy(Sessions, ("C7_A",))
    runs = [_start(Sessions, tx.id) for _ in range(3)]
    db.execute(sa.text(f"DROP INDEX {ONE_ACTIVE_INDEX}"))
    db.execute(sa.text("UPDATE longitudinal_assessment_runs SET execution_status = 'completed', "
                       "interpretation_status = 'active' WHERE id IN (:a, :b)"), {"a": runs[0], "b": runs[1]})
    with pytest.raises(InvalidLongitudinalState, match="2 runs actifs"):
        svc.get_active_longitudinal_assessment(db, user_id=USER, competency_code="C7")
    with pytest.raises(InvalidLongitudinalState, match="2 runs actifs"):
        svc.complete_longitudinal_assessment(db, run_id=runs[2])
    db.rollback()


def test_pg_partial_unique_index_remains_the_final_defense(engine, Sessions, db):
    tx = _taxonomy(Sessions, ("C7_A",))
    _complete(Sessions, _start(Sessions, tx.id))
    rogue = db.get(LongitudinalAssessmentRun, _start(Sessions, tx.id))
    rogue.interpretation_status = "active"
    with pytest.raises(sa.exc.IntegrityError, match=ONE_ACTIVE_INDEX):
        db.flush()


def test_pg_upstream_invalidation_never_cascades_into_t5_history(engine, Sessions, db, world):
    """Invalidation / supersession T3 après activation : inputs et relations
    du run restent (historique reconstructible), le run reste active ; le
    prochain dossier exclut simplement l'observation."""
    w = world
    _commit(Sessions, lambda s, **k: _dependency(s, w, w.t1, w.s1, mode="localized", caps=[w.A]))
    _complete(Sessions, w.run)
    snapshot = {t: sorted(map(repr, _rows(engine, f"SELECT * FROM {t}"))) for t in sorted(T5A_TABLES)}
    _invalidate(Sessions, w.s1)
    _t3(Sessions, w.tx.id, app(w.A, w.B), event_id=_rows(
        engine, "SELECT r.event_id FROM observation_evaluation_runs r JOIN pedagogical_observations o "
                "ON o.evaluation_run_id = r.id WHERE o.id = :o", o=w.t1)[0]["event_id"])
    assert {t: sorted(map(repr, _rows(engine, f"SELECT * FROM {t}"))) for t in sorted(T5A_TABLES)} == snapshot
    assert _state(engine, w.run) == ("completed", "active")
    following = svc.start_longitudinal_assessment(db, **_t5_kwargs(w.tx.id))
    inputs = {i.observation_id for i in svc.get_longitudinal_inputs(db, run_id=following.id)}
    assert w.s1 not in inputs and w.t1 not in inputs and len(inputs) == len(_inputs(engine, w.run)) - 1


def test_pg_no_evidence_is_created_or_modified_by_the_whole_lifecycle(engine, Sessions, db, world):
    w = world
    before = _evidence_state(engine)
    _dependency(db, w, w.t1, w.s1, mode="localized", caps=[w.A])
    _dependency(db, w, w.s2, trace=w.other_event_trace)
    _transfer(db, w, w.s1, w.t1, mode="localized", caps=[w.B])
    _revalidation(db, w, w.k1, w.t2)
    svc.complete_longitudinal_assessment(db, run_id=w.run)
    db.commit()
    other = _start(Sessions, w.tx.id)
    _commit(Sessions, svc.fail_longitudinal_assessment, run_id=other)
    assert _evidence_state(engine) == before
    # Aucune transitivité : s1 -> t1 et t1 -> ? ne créent rien d'autre.
    assert _count(engine, "observation_dependencies") == 2 and _count(engine, "observation_transfers") == 1


# --- I. fail -----------------------------------------------------------------------------------

def test_pg_fail_keeps_inputs_relations_and_the_current_active(engine, Sessions, db, world, clock):
    w = world
    active = _start(Sessions, w.tx.id)
    _complete(Sessions, active)
    _commit(Sessions, lambda s, **k: _transfer(s, w, w.s1, w.t1))
    inputs = _inputs(engine, w.run)
    now = clock()
    run = svc.fail_longitudinal_assessment(db, run_id=w.run, failure_code="evaluator_timeout")
    assert (run.execution_status, run.interpretation_status, run.completed_at, run.failure_code) == (
        "failed", "obsolete", now, "evaluator_timeout")
    db.commit()
    assert _state(engine, active) == ("completed", "active")
    assert _inputs(engine, w.run) == inputs and len(svc.get_transfers(db, run_id=w.run)) == 1
    for call in (lambda: _transfer(db, w, w.s2, w.t2), lambda: _dependency(db, w, w.t2, w.s1),
                 lambda: _revalidation(db, w, w.k1, w.t2)):
        with pytest.raises(InvalidLongitudinalState):
            call()
    no_code = _start(Sessions, w.tx.id)
    assert svc.fail_longitudinal_assessment(db, run_id=no_code).failure_code is None


@pytest.mark.parametrize("state", [("completed", "active"), ("completed", "superseded"), ("failed", "obsolete"),
                                   ("running", "active"), ("completed", "candidate"), ("running", "obsolete")])
def test_pg_only_running_candidate_accepts_relations_or_transitions(engine, Sessions, db, world, state):
    w = world
    _exec(engine, "UPDATE longitudinal_assessment_runs SET execution_status = :e, interpretation_status = :i "
                  "WHERE id = :r", e=state[0], i=state[1], r=w.run)
    for call in (lambda: _dependency(db, w, w.t1, w.s1), lambda: _transfer(db, w, w.s1, w.t1),
                 lambda: _revalidation(db, w, w.k1, w.t1),
                 lambda: svc.complete_longitudinal_assessment(db, run_id=w.run),
                 lambda: svc.fail_longitudinal_assessment(db, run_id=w.run)):
        with pytest.raises(InvalidLongitudinalState):
            call()
        db.rollback()


def test_pg_relation_reloads_a_stale_run_under_the_lock(Sessions, db, world):
    w = world
    stale = db.get(LongitudinalAssessmentRun, w.run)
    _complete(Sessions, w.run)
    with pytest.raises(InvalidLongitudinalState):
        _transfer(db, w, w.s1, w.t1)
    assert stale.interpretation_status == "active"  # rechargé par populate_existing


# --- J. concurrence ------------------------------------------------------------------------------

RELATION_OPERATIONS = {
    "dependency": lambda s, w: _dependency(s, w, w.t1, w.s1, mode="localized", caps=[w.A]),
    "transfer": lambda s, w: _transfer(s, w, w.s2, w.t2),
    "revalidation": lambda s, w: _revalidation(s, w, w.k1, w.t1),
    "fail": lambda s, w: svc.fail_longitudinal_assessment(s, run_id=w.run, failure_code="x"),
}


@pytest.mark.parametrize("operation", sorted(RELATION_OPERATIONS))
def test_pg_relation_first_then_complete_waits_and_sees_it(engine, Sessions, world, operation):
    w = world
    with Sessions() as first, Sessions() as second:
        RELATION_OPERATIONS[operation](first, w)
        result, error = _blocked(engine, first, second,
                                 lambda s: svc.complete_longitudinal_assessment(s, run_id=w.run).interpretation_status,
                                 waiting_on=ON_RUN)
    if operation == "fail":
        assert isinstance(error, InvalidLongitudinalState) and _state(engine, w.run) == ("failed", "obsolete")
        return
    assert error is None and result == "active"
    assert _state(engine, w.run) == ("completed", "active")
    table = {"dependency": "observation_dependencies", "transfer": "observation_transfers",
             "revalidation": "observation_revalidations"}[operation]
    assert _count(engine, table, "run_id = :r", r=w.run) == 1


@pytest.mark.parametrize("operation", sorted(RELATION_OPERATIONS))
def test_pg_complete_first_then_relation_is_refused(engine, Sessions, world, operation):
    w = world
    with Sessions() as first, Sessions() as second:
        svc.complete_longitudinal_assessment(first, run_id=w.run)
        _, error = _blocked(engine, first, second, lambda s: RELATION_OPERATIONS[operation](s, w), waiting_on=ON_RUN)
    assert isinstance(error, InvalidLongitudinalState)
    assert _state(engine, w.run) == ("completed", "active")
    for table in ("observation_dependencies", "observation_transfers", "observation_revalidations"):
        assert _count(engine, table) == 0


def test_pg_fail_first_then_relation_is_refused_and_relation_first_then_fail_keeps_it(engine, Sessions, world):
    w = world
    with Sessions() as first, Sessions() as second:
        RELATION_OPERATIONS["transfer"](first, w)
        result, error = _blocked(engine, first, second,
                                 lambda s: svc.fail_longitudinal_assessment(s, run_id=w.run).execution_status,
                                 waiting_on=ON_RUN)
    assert error is None and result == "failed"
    assert _count(engine, "observation_transfers") == 1
    run_id = _start(Sessions, w.tx.id)
    with Sessions() as first, Sessions() as second:
        svc.fail_longitudinal_assessment(first, run_id=run_id)
        _, error = _blocked(engine, first, second, lambda s: svc.add_transfer(
            s, run_id=run_id, source_observation_id=w.s1, target_observation_id=w.t1,
            scope_mode="whole_observation", transfer_basis={"a": "x"}), waiting_on=ON_RUN)
    assert isinstance(error, InvalidLongitudinalState)
    assert _count(engine, "observation_transfers", "run_id = :r", r=run_id) == 0


def test_pg_two_completions_of_the_same_run(engine, Sessions, world):
    w = world
    with Sessions() as first, Sessions() as second:
        svc.complete_longitudinal_assessment(first, run_id=w.run)
        _, error = _blocked(engine, first, second,
                            lambda s: svc.complete_longitudinal_assessment(s, run_id=w.run), waiting_on=ON_ADVISORY)
    assert isinstance(error, InvalidLongitudinalState)
    assert _state(engine, w.run) == ("completed", "active")


def test_pg_two_candidates_of_the_same_couple_are_serialized_by_the_advisory_lock(engine, Sessions):
    tx = _taxonomy(Sessions, ("C7_A",))
    _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    previous, a, b = (_start(Sessions, tx.id) for _ in range(3))
    _complete(Sessions, previous)
    with Sessions() as first, Sessions() as second:
        svc.complete_longitudinal_assessment(first, run_id=a)
        result, error = _blocked(engine, first, second, lambda s: svc.complete_longitudinal_assessment(
            s, run_id=b).interpretation_status, waiting_on=ON_ADVISORY)
    assert error is None and result == "active"
    assert [_state(engine, r)[1] for r in (previous, a, b)] == ["superseded", "superseded", "active"]


def test_pg_concurrent_old_snapshot_waits_then_is_stale(engine, Sessions):
    tx = _taxonomy(Sessions, ("C7_A",))
    _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    a = _start(Sessions, tx.id)
    _t3(Sessions, tx.id, contra(tx.m["C7_A"]))
    b = _start(Sessions, tx.id)
    with Sessions() as first, Sessions() as second:
        svc.complete_longitudinal_assessment(first, run_id=b)
        _, error = _blocked(engine, first, second, lambda s: svc.complete_longitudinal_assessment(s, run_id=a),
                            waiting_on=ON_ADVISORY)
    assert isinstance(error, StaleLongitudinalSnapshot)
    assert (_state(engine, a), _state(engine, b)) == (("running", "candidate"), ("completed", "active"))


def test_pg_without_the_advisory_lock_concurrent_activations_race(engine, Sessions, monkeypatch):
    """Test de mutation : sans le verrou consultatif, la seconde complétion
    attend l'ancien active verrouillé par la première puis — la ligne
    n'étant plus active — n'en trouve plus aucune et viole l'index unique
    partiel. C'est la course que le verrou consultatif interdit."""
    tx = _taxonomy(Sessions, ("C7_A",))
    previous, a, b = (_start(Sessions, tx.id) for _ in range(3))
    _complete(Sessions, previous)
    monkeypatch.setattr(svc, "_lock_activation", lambda db, user_id, competency_code: None)
    with Sessions() as first, Sessions() as second:
        svc.complete_longitudinal_assessment(first, run_id=a)
        _, error = _blocked(engine, first, second, lambda s: svc.complete_longitudinal_assessment(s, run_id=b),
                            waiting_on=ON_RUN)
    assert isinstance(error, sa.exc.IntegrityError) and ONE_ACTIVE_INDEX in str(error)
    assert [_state(engine, r)[1] for r in (previous, a, b)] == ["superseded", "active", "candidate"]


def test_pg_many_concurrent_completions_end_with_exactly_one_active(engine, Sessions):
    tx = _taxonomy(Sessions, ("C7_A",))
    _t3(Sessions, tx.id, sup(tx.m["C7_A"]))
    candidates = [_start(Sessions, tx.id) for _ in range(6)]
    barrier = threading.Barrier(len(candidates))
    done, errors = [], []

    def work(run_id):
        try:
            with Sessions() as session:
                session.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
                barrier.wait(timeout=10)
                svc.complete_longitudinal_assessment(session, run_id=run_id)
                session.commit()
                done.append(run_id)
        except Exception as exc:  # noqa: BLE001 — restitué au test
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(r,)) for r in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == [] and sorted(done) == sorted(candidates)
    rows = {r: _run_row(engine, r) for r in candidates}
    statuses = [row["interpretation_status"] for row in rows.values()]
    assert statuses.count("active") == 1 and statuses.count("superseded") == len(candidates) - 1
    # L'active est la dernière activation sérialisée.
    active = next(r for r, row in rows.items() if row["interpretation_status"] == "active")
    assert active == max(candidates, key=lambda r: rows[r]["completed_at"])


def test_pg_advisory_lock_is_transaction_level_and_keyed_by_couple(engine, Sessions):
    tx = _taxonomy(Sessions, ("C7_A",))
    key = svc._activation_lock_key(USER, "C7") & 0xFFFFFFFFFFFFFFFF
    expected = {"classid": key >> 32, "objid": key & 0xFFFFFFFF, "objsubid": 1, "granted": True}
    for end in ("commit", "rollback"):
        run_id = _start(Sessions, tx.id)
        with engine.connect() as conn, Session(bind=conn) as session:
            (pid,) = _pids(session)
            assert _advisory_locks(engine, pid) == []
            svc.complete_longitudinal_assessment(session, run_id=run_id)
            assert _advisory_locks(engine, pid) == [expected]
            getattr(session, end)()
            assert _pids(session) == [pid]
            assert _advisory_locks(engine, pid) == []


def test_pg_other_couples_are_not_serialized(engine, Sessions):
    """La clé est par (user_id, competency_code) : une complétion en cours
    ne bloque ni un autre user ni une autre compétence."""
    tx = _taxonomy(Sessions, ("C7_A",))
    run_id = _start(Sessions, tx.id)
    others = [_start(Sessions, tx.id, user_id=OTHER_USER), _start(Sessions, tx.id, competency_code="C8")]
    with Sessions() as first:
        svc.complete_longitudinal_assessment(first, run_id=run_id)
        for other in others:
            with Sessions() as second:
                second.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
                svc.complete_longitudinal_assessment(second, run_id=other)
                second.commit()
        first.commit()
    assert {_state(engine, r) for r in [run_id, *others]} == {("completed", "active")}


def test_pg_completion_blocks_the_retirement_of_its_release(engine, Sessions):
    """complete détient la release en FOR SHARE : une activation T4 qui
    retirerait cette release attend le COMMIT ; le dossier activé l'a été
    sous la release alors active."""
    tx = _taxonomy(Sessions, ("C7_A",))
    run_id = _start(Sessions, tx.id)
    successor = _taxonomy(Sessions, ("C7_A",), activate=False)
    with Sessions() as first, Sessions() as second:
        svc.complete_longitudinal_assessment(first, run_id=run_id)
        result, error = _blocked(engine, first, second, lambda s: tax.activate_release(
            s, release_id=successor.id).status, waiting_on=ON_RELEASE)
    assert error is None and result == "active"
    assert _state(engine, run_id) == ("completed", "active")
    with Sessions() as session:
        assert tax.get_active_release(session).id == successor.id


def test_pg_an_in_flight_retirement_makes_the_completion_refuse(engine, Sessions):
    tx = _taxonomy(Sessions, ("C7_A",))
    run_id = _start(Sessions, tx.id)
    successor = _taxonomy(Sessions, ("C7_A",), activate=False)
    with Sessions() as first, Sessions() as second:
        tax.activate_release(first, release_id=successor.id)
        _, error = _blocked(engine, first, second, lambda s: svc.complete_longitudinal_assessment(s, run_id=run_id),
                            waiting_on=ON_RELEASE)
    assert isinstance(error, TaxonomyReleaseNotUsable)
    assert _state(engine, run_id) == ("running", "candidate")


def test_pg_t5_inserts_never_block_t3_or_t4_writers(engine, Sessions, world):
    """Les INSERT T5 ne prennent que des FOR KEY SHARE (FK) : une
    invalidation T3 d'une observation du snapshot et un start T3 sous la
    release ne sont pas bloqués par un start / une relation T5 en cours."""
    w = world
    with Sessions() as t5:
        svc.start_longitudinal_assessment(t5, **_t5_kwargs(w.tx.id))
        _dependency(t5, w, w.t1, w.s1, mode="localized", caps=[w.A])
        with Sessions() as t3:
            t3.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
            obs.invalidate_observation(t3, observation_id=w.s1, reason="erreur")
            obs.start_evaluation_run(t3, **_t3_start_kwargs(_finalized_event(Sessions),
                                                            pedagogical_taxonomy_release_id=w.tx.id))
            t3.commit()
        t5.commit()
    with pytest.raises(StaleLongitudinalSnapshot):
        _complete(Sessions, w.run)


# --- K. lectures -----------------------------------------------------------------------------------

def test_pg_reads_are_deterministic(engine, Sessions, db, world, clock):
    w = world
    clock()
    deps = [_dependency(db, w, w.t1, w.s1, mode="localized", caps=[w.B, w.A]).id,
            _dependency(db, w, w.s2, w.s1).id]
    transfers = [_transfer(db, w, w.s2, w.t2).id, _transfer(db, w, w.s1, w.t2, mode="competency_only").id]
    db.commit()
    got = svc.get_dependencies(db, run_id=w.run)
    assert [d.id for d, _ in got] == deps
    assert got[0][1] == tuple(sorted((w.A, w.B), key=str)) and got[1][1] == ()
    assert [t.id for t, _ in svc.get_transfers(db, run_id=w.run)] == transfers
    inputs = [i.observation_id for i in svc.get_longitudinal_inputs(db, run_id=w.run)]
    assert inputs == sorted(inputs, key=str) and len(inputs) == len(_inputs(engine, w.run))
    assert svc.get_revalidations(db, run_id=w.run) == []


# --- L. propriété de la transaction ------------------------------------------------------------

def test_pg_service_never_commits_nor_rolls_back(engine, Sessions, db, world):
    """Aucune opération (y compris les erreurs métier) n'émet COMMIT ni
    ROLLBACK ; seul un savepoint local, pour l'INSERT d'un run."""
    w = world
    events = []
    listeners = {name: (lambda *a, _n=name: events.append(_n))
                 for name in ("commit", "rollback", "savepoint", "rollback_savepoint", "release_savepoint")}
    for name, fn in listeners.items():
        sa.event.listen(engine, name, fn)
    try:
        run = svc.start_longitudinal_assessment(db, **_t5_kwargs(w.tx.id, assessment_dedup_key="once"))
        with pytest.raises(DuplicateLongitudinalAssessment):
            svc.start_longitudinal_assessment(db, **_t5_kwargs(w.tx.id, assessment_dedup_key="once"))
        _dependency(db, w, w.t1, w.s1, mode="localized", caps=[w.A])
        with pytest.raises(InvalidTransfer):
            _transfer(db, w, w.s1, w.t1, mode="localized", caps=[w.A])
        _transfer(db, w, w.s2, w.t2)
        _revalidation(db, w, w.k1, w.s2)
        svc.fail_longitudinal_assessment(db, run_id=run.id)
        svc.complete_longitudinal_assessment(db, run_id=w.run)
        with pytest.raises(InvalidLongitudinalState):
            svc.fail_longitudinal_assessment(db, run_id=w.run)
        for read in (svc.get_longitudinal_inputs, svc.get_dependencies, svc.get_transfers, svc.get_revalidations):
            read(db, run_id=w.run)
        svc.get_active_longitudinal_assessment(db, user_id=USER, competency_code="C7")
    finally:
        for name, fn in listeners.items():
            sa.event.remove(engine, name, fn)
    assert "commit" not in events and "rollback" not in events
    assert events == ["savepoint", "release_savepoint"]
    assert _state(engine, w.run) == ("running", "candidate")
    db.rollback()
    assert _count(engine, "longitudinal_assessment_runs") == 1
    assert sum(_count(engine, t) for t in ("observation_dependencies", "observation_transfers",
                                           "observation_revalidations")) == 0


def test_pg_t5_writes_join_a_wider_caller_transaction(engine, Sessions, db):
    """Évaluation T3 + mapping T4 + dossier T5 : une seule transaction,
    commitée (ou non) par l'appelant seul."""
    tx = _taxonomy(Sessions, ("C7_A",))
    event_id = _finalized_event(Sessions)
    run = obs.start_evaluation_run(db, **_t3_start_kwargs(event_id, pedagogical_taxonomy_release_id=tx.id))
    observation = obs.add_observation(db, run_id=run.id, **_obs_kwargs())
    tax.map_observation_capability(db, observation_id=observation.id, capability_membership_id=tx.m["C7_A"])
    obs.complete_evaluation_run(db, run_id=run.id, output_fingerprint="o")
    t5 = svc.start_longitudinal_assessment(db, **_t5_kwargs(tx.id))
    assert [i.observation_id for i in svc.get_longitudinal_inputs(db, run_id=t5.id)] == [observation.id]
    svc.complete_longitudinal_assessment(db, run_id=t5.id)
    assert _count(engine, "longitudinal_assessment_runs") == 0 and _count(engine, "pedagogical_observations") == 0
    db.commit()
    assert _state(engine, t5.id) == ("completed", "active") and _inputs(engine, t5.id) == {observation.id}


def test_t5a_models_are_unchanged_by_t5b():
    """T5-B ne modifie pas les modèles T5-A (aucune colonne ajoutée)."""
    assert [m.__tablename__ for m in T5A_MODELS] == [
        "longitudinal_assessment_runs", "longitudinal_assessment_inputs", "observation_dependencies",
        "dependency_capabilities", "observation_transfers", "transfer_capabilities", "observation_revalidations",
        "revalidation_capabilities"]
