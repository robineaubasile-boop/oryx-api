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
from core import taxonomy_service as tax
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
    IntegrityChangeContext,
    LongitudinalParentNotUsable,
    ObservationDeltaContext,
    PredecessorBasisRefContext,
    PredecessorDecisionContext,
    PredecessorHistoricalObservation,
    PredecessorLongitudinalContext,
    PredecessorSnapshot,
    PredecessorStageClaimContext,
    PredecessorTensionContext,
    CurrentTaxonomyCapability,
    CurrentTaxonomyContext,
    ReevaluationContext,
    RelationDeltaContext,
    RelationFamilyDeltaContext,
    StageClaimDecision,
    StaleInferenceChain,
    StaleInferenceInput,
    StaleInferencePredecessor,
    TensionDecision,
    TransitionCausalityContext,
    HistoricalInferenceRun,
    UserNotFound,
    ValidatedCompetencyState,
    VersionChangeContext,
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
from tests.test_migration_0010_r1b_event_idempotence import R1B
from tests.test_migration_0011_assistant_deliveries import R1C1
from tests.test_migration_0012_decryptage_cognitive_links import R1C2
from tests.test_observation_service import INVALID_JSON_VALUES, _blocked, _NoDB, _obs_kwargs, _reaches_db, _recording
from tests.test_observation_service import _event as _finalized_event
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
    # Étape 6.4C : lecture seule de la lignée adoptée (chaîne predecessor).
    "get_inference_lineage",
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
PREDECESSOR_CONTEXT_CLASSES = (PredecessorDecisionContext, PredecessorStageClaimContext, PredecessorTensionContext,
                               PredecessorBasisRefContext, PredecessorLongitudinalContext,
                               PredecessorHistoricalObservation)
CAUSALITY_CLASSES = (TransitionCausalityContext, IntegrityChangeContext, ReevaluationContext, ObservationDeltaContext,
                     RelationDeltaContext, RelationFamilyDeltaContext, VersionChangeContext)
TAXONOMY_CONTEXT_CLASSES = (CurrentTaxonomyContext, CurrentTaxonomyCapability)
# Propriétés de la vue T5-C relues au présent : jamais dans l'entrée T6-C.
LIVE_OBSERVATION_FIELDS = ("current_integrity_status", "current_evaluation_run_interpretation_status")
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
    """T6-B est service-only : aucune migration ajoutée par T6-B ; les seules
    ajoutées depuis sont 0010 (R1-B, identité idempotente des
    CognitiveEvents), 0011 (R1-C1, livraisons des réponses assistant) et
    0012 (R1-C2, liens cognitifs Décrypter), qui est la tête."""
    script = _script_directory()
    assert script.get_heads() == [R1C2]
    assert script.get_revision(R1C2).down_revision == R1C1
    assert script.get_revision(R1C1).down_revision == R1B
    assert script.get_revision(R1B).down_revision == T6A
    assert script.get_revision(T6A).down_revision == T5A
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[-4:] == [f"{T6A}.py", f"{R1B}.py", f"{R1C1}.py", f"{R1C2}.py"] and len(files) == 12


def test_t6a_schema_files_are_unchanged_by_t6b():
    """Ni core/models.py ni la migration 0009 ne sont modifiés par T6-B
    (comparaison avec le commit de merge de T6-A quand il est disponible).
    core/models.py est ensuite modifié par R1-B (0010) : il est comparé
    jusqu'au dernier commit avant R1-B (merge de Step 6.4D). api.py est
    ensuite modifié par R1-C1 (branchement runtime de /decryptage) : il est
    comparé jusqu'au dernier commit avant R1-C1 (merge de R1-B, PR #212)."""
    commits = {}
    for name, sha in (("t6a", "b70bfa8b9fb3c2987f4d73737cbe26705bce232c"),
                      ("before_r1b", "4982474436cb846d8e61b7e62c6fecc8d5a27745"),
                      ("before_r1c1", "e6d630060234edd42ba919cbbeb170367cdae0cc")):
        found = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
                               cwd=REPO_ROOT, capture_output=True, text=True)
        if found.returncode != 0:
            pytest.skip("historique git indisponible")
        commits[name] = found.stdout.strip()
    diff = subprocess.run(["git", "diff", "--quiet", commits["t6a"], commits["before_r1b"], "--", "core/models.py"],
                          cwd=REPO_ROOT)
    assert diff.returncode == 0, "core/models.py"
    diff = subprocess.run(["git", "diff", "--quiet", commits["t6a"], "--", f"alembic/versions/{T6A}.py"],
                          cwd=REPO_ROOT)
    assert diff.returncode == 0, T6A
    diff = subprocess.run(["git", "diff", "--quiet", commits["t6a"], commits["before_r1c1"], "--", "api.py"],
                          cwd=REPO_ROOT)
    assert diff.returncode == 0, "api.py"


def test_public_api_is_exactly_the_twelve_operations():
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
                 "get_inference_basis_refs", "get_inference_lineage"):
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
                BasisRefDecision, ValidatedCompetencyState, HistoricalInferenceRun, *PREDECESSOR_CONTEXT_CLASSES,
                *CAUSALITY_CLASSES, *TAXONOMY_CONTEXT_CLASSES):
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
                  "predecessor", "predecessor_decision_context", "input_fingerprint", *VERSIONS, "model_id",
                  "prompt_spec_version"):
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


def test_predecessor_context_structures():
    """T6-B.1 : identité minimale (PredecessorSnapshot, inchangé) et contenu
    décisionnel historique (PredecessorDecisionContext) restent séparés ;
    aucune identité de ligne T6 n'est exposée comme clé sémantique."""
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(PredecessorSnapshot) == [
        "inference_run_id", "longitudinal_assessment_run_id", "current_stage", "previous_stage", "transition",
        "transition_cause", "tension_state", "unresolved_revision_context", "established_claim_stages",
        "input_fingerprint", "output_fingerprint"]
    assert names(PredecessorDecisionContext) == [
        "inference_run_id", "longitudinal_assessment_run_id", "pedagogical_taxonomy_release_id",
        "historical_longitudinal_context", "claims", "tensions", "run_basis_refs", "validation_needs",
        "state_decision_summary"]
    assert names(PredecessorStageClaimContext) == [
        "stage", "positive_basis_status", "basis_mode", "basis_summary", "scope_summary", "confidence_profile",
        "mastery_assessment", "basis_refs"]
    assert names(PredecessorTensionContext) == [
        "fragilized_stage", "scope_mode", "summary", "revision_status", "capability_membership_ids",
        "capability_definition_ids", "basis_refs"]
    assert names(PredecessorBasisRefContext) == ["ref_role", "confidence_dimension", "source_kind", "source_id"]
    # Projection stable : les faits du snapshot T5-C, sans ce qui est relu au
    # présent (omis, jamais remplacé par une valeur supposée).
    assert names(PredecessorHistoricalObservation) == [
        n for n in names(view.HistoryObservation) if n not in LIVE_OBSERVATION_FIELDS]
    dossier_fields = names(view.LongitudinalDossier)
    assert names(PredecessorLongitudinalContext) == [
        *(n for n in dossier_fields if n not in ("execution_status", "interpretation_status",
                                                 "run_completed_at_technical", "active_history",
                                                 "dependency_profile", "coverage_profile", "variety_profile",
                                                 "transfer_profile", "consistency_profile",
                                                 "temporal_validation_profile", "limitations")),
        "observations", "episodes", "dependency_profile", "coverage_profile", "variety_profile",
        "transfer_profile", "consistency_profile", "temporal_validation_profile", "limitations"]
    for cls in (PredecessorLongitudinalContext, PredecessorHistoricalObservation):
        assert not any(n.startswith("current_") or n.endswith("_status") for n in names(cls)), cls
    assert svc.LIVE_LIMITATION_CODES == {view.UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT}
    fields = {f.name: f for f in dataclasses.fields(InferenceContext)}
    assert fields["predecessor_decision_context"].type == "PredecessorDecisionContext | None" or \
        fields["predecessor_decision_context"].type == PredecessorDecisionContext | None
    for cls in PREDECESSOR_CONTEXT_CLASSES:
        for field in ("stage_claim_id", "tension_id", "id", "score", "confidence_score", "weight", "rank",
                      "active_validation_plan", "tension_key"):
            assert field not in names(cls), (cls, field)
        # Aucune annotation vers un modèle ORM (seul LongitudinalDossier, T5-C).
        from core import models as orm
        orm_models = {n for n, o in vars(orm).items() if isinstance(o, type) and hasattr(o, "__table__")}
        for f in dataclasses.fields(cls):
            assert not any(model in str(f.type) for model in orm_models), (cls, f.name)
    with pytest.raises(dataclasses.FrozenInstanceError):
        PredecessorBasisRefContext(ref_role="validation", confidence_dimension=None, source_kind="observation",
                                   source_id=X).source_id = Y


T6B_MERGE = "0435aa4dd00cc5cf5cab38f686805b6d94075cb0"
# Formats canoniques, identité logique, verrous, lifecycle, activation et
# cache : strictement ceux de T6-B (PR #188).
# (T6-B.2 change volontairement INPUT_SCHEMA_VERSION, renomme
# _input_fingerprint en _input_fingerprint_v1 et étend _verified_inputs,
# _check_decision et _Inputs : voir le test T6-B.2 ci-dessous.)
UNCHANGED_BY_T6B1 = (
    "DEDUP_SCHEMA_VERSION", "OUTPUT_SCHEMA_VERSION", "ACTIVATION_LOCK_NAMESPACE",
    "SPECIFICATION_FIELDS", "VERSION_FIELDS", "REF_ATTACHMENTS", "_canonical_json", "_canonical_sha256",
    "_specification", "_dossier_payload", "_predecessor_payload", "_inference_dedup_key",
    "_activation_lock_key", "_ref_source_identity", "_ref_payload", "_output_fingerprint", "_relations",
    "_snapshot", "_membership_definitions", "_validated_decision",
    "_validated_claims", "_validated_tensions", "_validated_refs", "_parent_problems", "_share_chain",
    "_require_completed_active", "_active_and_cache", "_check_cache", "_children", "_require_pristine_candidate",
    "_lock_couple", "_lock_run", "_lock_active", "_lock_cache", "_persist_children", "_Decision",
    "PredecessorSnapshot", "InferenceDecision", "complete_competency_inference", "fail_competency_inference",
    "get_validated_user_competency_state", "get_competency_inference", "get_active_competency_inference",
    "get_stage_claims", "get_inference_tensions", "get_inference_basis_refs", "get_user_competency_state")
T6B1_ADDITIONS = {
    "PredecessorDecisionContext", "PredecessorStageClaimContext", "PredecessorTensionContext",
    "PredecessorBasisRefContext", "PredecessorLongitudinalContext", "PredecessorHistoricalObservation",
    "LIVE_LIMITATION_CODES", "_build_predecessor_decision_context", "_historical_longitudinal_context",
    "_historical_ref_order", "_historical_ref_contexts"}
T6B1_MERGE = "4635545be818a40aae9ec38288d588723b5b5045"
T6B2_ADDITIONS = {
    "INVALIDATED", "T3_SPECIFICATION_FIELDS", "T5_VERSION_FIELDS", "LEGACY_INPUT_SCHEMA_VERSION",
    "TRANSITION_CAUSALITY_SCHEMA_VERSION", "IntegrityChangeContext", "ReevaluationContext", "ObservationDeltaContext",
    "RelationFamilyDeltaContext", "RelationDeltaContext", "VersionChangeContext", "TransitionCausalityContext",
    "_InputIdentity", "_CausalFacts", "_input_fingerprint_v1", "_input_fingerprint_v2",
    "_causality_fingerprint_payload", "_detect_input_identity", "_predecessor_parent", "_sorted_ids",
    "_effective_run", "_check_reevaluation_chain", "_causal_facts", "_id_delta", "_relation_family_delta",
    "_version_changes", "_build_transition_causality", "_check_legacy_causes", "_cited_events",
    "_check_transition_causes"}
# Seules définitions T6-B.1 modifiées par T6-B.2 : identité d'entrée (version,
# détection V1 / V2, causalité), contexte, validation des causes, docstrings.
CHANGED_BY_T6B2 = {"INPUT_SCHEMA_VERSION", "InferenceContext", "_Inputs", "_verified_inputs", "_context",
                   "_build_predecessor_decision_context", "_check_decision", "start_competency_inference",
                   "get_inference_context"}
T6B2_MERGE = "6ca71a7a14cb0211edf6f96fa20201f6bf18f716"
T6C0_ADDITIONS = {"CurrentTaxonomyCapability", "CurrentTaxonomyContext", "_build_current_taxonomy_context"}
# Seules définitions T6-B.2 modifiées par T6-C0 : le champ du contexte, son
# assemblage et ses deux constructeurs (start / get).
CHANGED_BY_T6C0 = {"InferenceContext", "_context", "start_competency_inference", "get_inference_context"}
# Seule définition T6-B modifiée par T6-C3 : la garde structurelle de cause,
# qui reconnaît un changement sémantique des relations T5 (relation_delta
# added / removed) comme support de pedagogical_reinterpretation.
CHANGED_BY_T6C3 = {"_check_transition_causes"}
# Étape 6.4C : seuls ajouts, une structure et une lecture seule de la lignée
# adoptée (plus son helper de requête) ; aucune définition existante modifiée.
STEP64C_ADDITIONS = {"HistoricalInferenceRun", "_lineage_rows", "get_inference_lineage"}


def _top_level(source):
    definitions = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            definitions[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            definitions[node.targets[0].id] = ast.dump(node)
    return definitions


def test_t6b1_leaves_fingerprints_dedup_locks_lifecycle_and_cache_unchanged():
    """T6-B.1 n'est qu'une reconstruction historique : les versions et
    formats V1 (input, dédup, output), _predecessor_payload, les verrous,
    complete / fail, l'activation, le cache et la lecture validée sont
    identiques (AST) à ceux du merge de T6-B."""
    assert (svc.LEGACY_INPUT_SCHEMA_VERSION, svc.DEDUP_SCHEMA_VERSION, svc.OUTPUT_SCHEMA_VERSION) == (1, 1, 1)
    assert list(inspect.signature(svc._predecessor_payload).parameters) == ["snapshot", "raw_context"]
    snapshot = PredecessorSnapshot(
        inference_run_id=X, longitudinal_assessment_run_id=Y, current_stage="application", previous_stage=None,
        transition=None, transition_cause=None, tension_state="none", unresolved_revision_context=None,
        established_claim_stages=("discovery",), input_fingerprint="in", output_fingerprint="out")
    assert svc._predecessor_payload(snapshot, None) == {
        "inference_run_id": str(X), "current_stage": "application", "tension_state": "none",
        "unresolved_revision_context": None, "output_fingerprint": "out"}
    base = subprocess.run(["git", "show", f"{T6B_MERGE}:core/inference_service.py"], cwd=REPO_ROOT,
                          capture_output=True, text=True)
    if base.returncode != 0:
        pytest.skip("historique git indisponible")
    before, after = _top_level(base.stdout), _top_level(SERVICE_PATH.read_text(encoding="utf-8"))
    for name in UNCHANGED_BY_T6B1:
        assert name in before and after.get(name) == before[name], name
    # Seuls ajouts : les structures historiques (T6-B.1), causales (T6-B.2)
    # et la résolution taxonomique courante (T6-C0).
    assert set(after) - set(before) == T6B1_ADDITIONS | T6B2_ADDITIONS | T6C0_ADDITIONS | STEP64C_ADDITIONS
    assert set(before) - set(after) == {"_input_fingerprint"}  # renommé _input_fingerprint_v1 (T6-B.2)


def _without_docstring(node):
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
        node.body = node.body[1:]
    return node


def _function(source, name):
    return next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)


def test_t6b2_changes_only_the_input_identity_and_the_causal_checks():
    """Diff AST contre le merge de T6-B.1 : T6-B.1 (structures et projection
    historique), dédup, output, verrous, complete, fail, cache et lecture
    validée strictement inchangés ; seules l'identité d'entrée, le contexte
    et la validation des causes évoluent. _input_fingerprint_v1 est
    l'ancien _input_fingerprint, et _check_legacy_causes l'ancien bloc
    causal de _check_decision, à l'identique."""
    base = subprocess.run(["git", "show", f"{T6B1_MERGE}:core/inference_service.py"], cwd=REPO_ROOT,
                          capture_output=True, text=True)
    if base.returncode != 0:
        pytest.skip("historique git indisponible")
    current = SERVICE_PATH.read_text(encoding="utf-8")
    before, after = _top_level(base.stdout), _top_level(current)
    assert set(after) - set(before) == T6B2_ADDITIONS | T6C0_ADDITIONS | STEP64C_ADDITIONS
    assert set(before) - set(after) == {"_input_fingerprint"}
    for name in set(before) - CHANGED_BY_T6B2 - {"_input_fingerprint"}:
        assert after[name] == before[name], name
    for name in CHANGED_BY_T6B2:
        assert after[name] != before[name], name
    # V1 : même corps, seul le nom de la constante de version change.
    old = _function(base.stdout, "_input_fingerprint")
    old.name = "_input_fingerprint_v1"
    for node in ast.walk(old):
        if isinstance(node, ast.Name) and node.id == "INPUT_SCHEMA_VERSION":
            node.id = "LEGACY_INPUT_SCHEMA_VERSION"
    assert ast.dump(old) == ast.dump(_without_docstring(_function(current, "_input_fingerprint_v1")))
    # Chemin legacy : l'ancien bloc « if predecessor is not None » à l'identique.
    old_block = next(n for n in _function(base.stdout, "_check_decision").body
                     if isinstance(n, ast.If) and ast.unparse(n.test) == "predecessor is not None"
                     and "_new_user_events" in ast.unparse(n))
    legacy = _without_docstring(_function(current, "_check_legacy_causes")).body
    assert ast.unparse(legacy[0]) == "error = InvalidInferenceDecision"
    assert [ast.dump(n) for n in legacy[1:]] == [ast.dump(n) for n in old_block.body]


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
    # Seules dépendances applicatives : les modèles, la vue T5-C et la
    # lecture T4-B get_release_capabilities (T6-C0), toutes en lecture.
    assert _imports(SERVICE_PATH) == {"hashlib", "json", "math", "uuid", "collections.abc", "dataclasses",
                                      "datetime", "types", "typing", "sqlalchemy", "sqlalchemy.exc",
                                      "core.longitudinal_view", "core.models", "core.taxonomy_service"}


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
    for cls in (InferenceContext, PredecessorSnapshot, ValidatedCompetencyState, HistoricalInferenceRun,
                *PREDECESSOR_CONTEXT_CLASSES, *CAUSALITY_CLASSES):
        for f in dataclasses.fields(cls):
            assert f.type not in (float, "float"), (cls, f.name)
    # Deltas causaux : des ensembles, jamais un décompte, un solde ni un score.
    for cls in CAUSALITY_CLASSES:
        for f in dataclasses.fields(cls):
            assert not any(word in f.name for word in ("count", "net_", "score", "progress", "weight", "age",
                                                       "fresh", "decay", "duration", "_at")), (cls, f.name)


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
    # Étape 6.1A : seul lecteur autorisé, et seulement par les lectures
    # rattachées à un run (jamais une écriture, jamais le cache brut).
    # Étape 6.4B1 : lecteur non branché des refs positive_basis du run
    # capturé par 6-1A, revalidé avant / après (jamais l'active courante,
    # jamais les tensions ; voir tests/test_progress_evidence.py).
    step6_readers = {"core/adaptation_state.py": {
        "get_validated_user_competency_state", "get_competency_inference", "get_stage_claims",
        "get_inference_tensions", "get_inference_basis_refs"},
        "core/progress_evidence.py": {
        "get_validated_user_competency_state", "get_competency_inference", "get_stage_claims",
        "get_inference_basis_refs"},
        # Étape 6.4C1 : lecteur non branché de la lignée adoptée du run
        # capturé par 6-1A, revalidé avant / après (voir
        # tests/test_progress_history.py). 6-4C2 / 6-4C3 n'en lisent que
        # des vocabulaires.
        "core/progress_history.py": {"get_validated_user_competency_state", "get_inference_lineage"}}
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
            for name in PUBLIC_API - step6_readers.get(rel, set()):
                assert name not in source, (rel, name)
    for rel, reads in step6_readers.items():
        tokens = _code_tokens((REPO_ROOT / rel).read_text(encoding="utf-8")).split()
        assert reads <= set(tokens), rel
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
    assert svc._input_fingerprint_v1(payload, predecessor) == hashlib.sha256(canonical.encode()).hexdigest()
    assert svc._input_fingerprint_v1(payload, None) != svc._input_fingerprint_v1(payload, predecessor)


def test_input_fingerprint_tracks_every_component():
    base = svc._dossier_payload(_parent(), _relations())
    reference = svc._input_fingerprint_v2(base, None, None)
    variants = [svc._dossier_payload(_parent(**{k: v}), _relations()) for k, v in (
        ("user_id", "autre"), ("competency_code", "C8"), ("pedagogical_taxonomy_release_id", uuid.UUID(int=10)),
        ("input_fingerprint", "t5fp2"), ("dependency_version", "d2"), ("transfer_version", "t2"),
        ("revalidation_version", "r2"), ("relation_schema_version", "s2"))]
    variants += [svc._dossier_payload(_parent(), _relations(**{k: [{"x": 1}]}))
                 for k in ("dependencies", "transfers", "revalidations")]
    prints = {svc._input_fingerprint_v2(v, None, None) for v in variants}
    assert reference not in prints and len(prints) == len(variants)
    assert len({svc._input_fingerprint_v1(v, None) for v in variants}) == len(variants)


def _golden_payloads():
    dependency = {"target_observation_id": str(X), "source_kind": "observation", "source_observation_id": str(Y),
                  "source_support_trace_id": None, "scope_fingerprint": "sf", "dependency_type": "dependent",
                  "scope_mode": "localized", "dependency_basis": {"why": "é"}}
    transfer = {"source_observation_id": str(Y), "target_observation_id": str(Z), "scope_fingerprint": "sf2",
                "scope_mode": "whole_observation", "transfer_basis": {"adaptation": ["a", 1, 2.5, True, None]}}
    revalidation = {"source_contradiction_observation_id": str(Z), "target_supportive_observation_id": str(X),
                    "scope_fingerprint": "sf3", "scope_mode": "competency_only",
                    "revalidation_basis": {"mechanism": "démontré"}}
    predecessor = {"inference_run_id": str(Z), "current_stage": "application", "tension_state": "none",
                   "unresolved_revision_context": None, "output_fingerprint": "out"}
    open_tension = {"inference_run_id": str(X), "current_stage": "comprehension", "tension_state": "open",
                    "unresolved_revision_context": {"motif": "levier contredit", "détail": ["C7_A", 2]},
                    "output_fingerprint": "0" * 64}
    return {
        "empty_first": (svc._dossier_payload(_parent(), _relations()), None),
        "dependency_predecessor": (svc._dossier_payload(_parent(), _relations(dependencies=[dependency])),
                                   predecessor),
        "dependency_first": (svc._dossier_payload(_parent(), _relations(dependencies=[dependency])), None),
        "all_relations_open_tension": (svc._dossier_payload(
            _parent(competency_code="C12", user_id="u2"),
            _relations(dependencies=[dependency], transfers=[transfer], revalidations=[revalidation])), open_tension),
    }


# Empreintes calculées par core/inference_service.py AVANT T6-B.2 (main
# 4635545, _input_fingerprint) sur les payloads ci-dessus : V1 doit les
# reproduire octet pour octet, pour toujours.
GOLDEN_V1 = {
    "empty_first": "d380b82e7e2ac8c8636f47e432c1fb69ba99dc21de7213e4a7b79ed9cce61bd1",
    "dependency_predecessor": "583a9f62887f30efb6cb16ebe7ec88441d4b26b0e940d3868b2ac1f2fc2c7238",
    "dependency_first": "c2651fd607091c98ee6e8ae92230ae9cd1d39af48d9a1dbf08696a0ab2291576",
    "all_relations_open_tension": "3b0dac449a5b1eb27f99fec99202be5e0baf76fafccc0eb1f9922dd9feb5fbc6",
}


def test_input_fingerprint_v1_reproduces_pre_t6b2_golden_hashes():
    payloads = _golden_payloads()
    assert set(payloads) == set(GOLDEN_V1)
    for name, (dossier, predecessor) in payloads.items():
        assert svc._input_fingerprint_v1(dossier, predecessor) == GOLDEN_V1[name], name
        # V2 est un autre format, même sans predecessor ni causalité.
        assert svc._input_fingerprint_v2(dossier, predecessor, None) != GOLDEN_V1[name], name


def test_input_schema_versions():
    assert (svc.INPUT_SCHEMA_VERSION, svc.LEGACY_INPUT_SCHEMA_VERSION, svc.TRANSITION_CAUSALITY_SCHEMA_VERSION) == (
        2, 1, 1)
    assert (svc.DEDUP_SCHEMA_VERSION, svc.OUTPUT_SCHEMA_VERSION) == (1, 1)
    assert svc.T3_SPECIFICATION_FIELDS == (
        "normalization_version", "local_stage_version", "pedagogical_taxonomy_release_id",
        "capability_mapping_version", "evaluation_schema_version", "evaluator_version", "model_id",
        "prompt_spec_version")
    assert svc.T5_VERSION_FIELDS == ("dependency_version", "transfer_version", "revalidation_version",
                                     "relation_schema_version")
    # Aucune colonne de version d'entrée : elle se détecte par recalcul.
    from core import models as orm
    assert "input_schema_version" not in orm.CompetencyInferenceRun.__table__.c


E1, E2, O1, O2, R1, R2, D1 = (uuid.UUID(int=0x100 + i) for i in range(7))


def _facts(new=(), integrity=(), reevaluations=()):
    return SimpleNamespace(new_user_event_ids=tuple(new), integrity_changes=tuple(integrity),
                           reevaluations=tuple(reevaluations))


def _integrity(observation=O1, event=E1, run=R1, definitions=(D1,)):
    return IntegrityChangeContext(observation_id=observation, event_id=event, evaluation_run_id=run,
                                  capability_definition_ids=tuple(definitions))


def _reevaluation(**overrides):
    kwargs = {"event_id": E1, "previous_evaluation_run_id": R1, "replacement_evaluation_run_id": R2,
              "replacement_re_evaluates_run_id": R1, "previous_observation_ids": (O1,),
              "current_observation_ids": (), "previous_capability_definition_ids": (D1,),
              "current_capability_definition_ids": (), "changed_evaluation_specification_fields": ("evaluator_version",)}
    kwargs.update(overrides)
    return ReevaluationContext(**kwargs)


def test_input_fingerprint_v2_exact_canonical_format():
    dossier, predecessor = _golden_payloads()["dependency_predecessor"]
    causality = svc._causality_fingerprint_payload(_facts([E2], [_integrity()], [_reevaluation()]))
    assert causality == {
        "causality_schema_version": 1,
        "new_user_event_ids": [str(E2)],
        "integrity_changes": [{"observation_id": str(O1), "event_id": str(E1), "evaluation_run_id": str(R1),
                               "capability_definition_ids": [str(D1)]}],
        "reevaluations": [{"event_id": str(E1), "previous_evaluation_run_id": str(R1),
                           "replacement_evaluation_run_id": str(R2), "replacement_re_evaluates_run_id": str(R1),
                           "previous_observation_ids": [str(O1)], "current_observation_ids": [],
                           "previous_capability_definition_ids": [str(D1)], "current_capability_definition_ids": [],
                           "changed_evaluation_specification_fields": ["evaluator_version"]}],
    }
    expected = {"input_schema_version": 2, **dossier, "predecessor": predecessor, "transition_causality": causality}
    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert svc._input_fingerprint_v2(dossier, predecessor, causality) == hashlib.sha256(
        canonical.encode()).hexdigest()
    # Première inférence : transition_causality null.
    first = json.dumps({"input_schema_version": 2, **dossier, "predecessor": None, "transition_causality": None},
                       sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert svc._causality_fingerprint_payload(None) is None
    assert svc._input_fingerprint_v2(dossier, None, None) == hashlib.sha256(first.encode()).hexdigest()
    # Aucun horodatage, aucun décompte dans le payload causal.
    assert not any(key.endswith("_at") or "count" in key for key in canonical.replace('"', " ").split())


def test_causal_payload_distinguishes_integrity_from_reevaluation():
    """Test architectural : même dossier courant, même predecessor ; A =
    observation invalidée, B = même événement réévalué (observation ->
    zéro). V1 les confond ; V2 les distingue."""
    dossier, predecessor = _golden_payloads()["dependency_predecessor"]
    history_a = svc._causality_fingerprint_payload(_facts(integrity=[_integrity()]))
    history_b = svc._causality_fingerprint_payload(_facts(reevaluations=[_reevaluation()]))
    assert history_a != history_b
    assert svc._input_fingerprint_v2(dossier, predecessor, history_a) != svc._input_fingerprint_v2(
        dossier, predecessor, history_b)
    assert svc._input_fingerprint_v1(dossier, predecessor) == svc._input_fingerprint_v1(dossier, predecessor)
    # Chaque fait causal est engagé.
    variants = [_facts(), _facts(new=[E1]), _facts(new=[E2]), _facts(integrity=[_integrity(definitions=())]),
                _facts(reevaluations=[_reevaluation(previous_evaluation_run_id=None)]),
                _facts(reevaluations=[_reevaluation(replacement_re_evaluates_run_id=None)]),
                _facts(reevaluations=[_reevaluation(current_observation_ids=(O2,))]),
                _facts(reevaluations=[_reevaluation(changed_evaluation_specification_fields=())]),
                _facts(reevaluations=[_reevaluation(current_capability_definition_ids=(D1,))])]
    prints = {svc._input_fingerprint_v2(dossier, predecessor, svc._causality_fingerprint_payload(v))
              for v in variants + [_facts(integrity=[_integrity()]), _facts(reevaluations=[_reevaluation()])]}
    assert len(prints) == len(variants) + 2


def test_causal_payload_is_canonical_whatever_the_order():
    integrity = [_integrity(O1, E1, R1, (D1, X)), _integrity(O2, E2, R2, (Y,))]
    reevaluations = [_reevaluation(), _reevaluation(event_id=E2, previous_observation_ids=(O2, O1),
                                                    changed_evaluation_specification_fields=("model_id", "evaluator_version"))]
    a = svc._causality_fingerprint_payload(_facts([E1, E2], integrity, reevaluations))
    b = svc._causality_fingerprint_payload(_facts([E2, E1], reversed(integrity), reversed(reevaluations)))
    b_shuffled = svc._causality_fingerprint_payload(_facts(
        [E2, E1], [_integrity(O2, E2, R2, (Y,)), _integrity(O1, E1, R1, (X, D1))],
        [dataclasses.replace(reevaluations[1], previous_observation_ids=(O1, O2),
                             changed_evaluation_specification_fields=("evaluator_version", "model_id")),
         reevaluations[0]]))
    assert a == b == b_shuffled
    assert svc._canonical_json(a) == svc._canonical_json(b_shuffled)


def test_detect_input_identity_is_cryptographic_never_by_date():
    dossier, predecessor = _golden_payloads()["dependency_predecessor"]
    causality = _facts([E2])
    calls = []

    def build():
        calls.append(1)
        return causality

    v1 = svc._input_fingerprint_v1(dossier, predecessor)
    v2 = svc._input_fingerprint_v2(dossier, predecessor, svc._causality_fingerprint_payload(causality))
    assert svc._detect_input_identity(v1, dossier, predecessor, build) == (1, None, v1)
    assert calls == []  # un candidat V1 n'est jamais soumis à une causalité qu'il n'a pas signée
    assert svc._detect_input_identity(v2, dossier, predecessor, build) == (2, causality, v2)
    assert svc._detect_input_identity("0" * 64, dossier, predecessor, build) is None
    assert "date" not in inspect.signature(svc._detect_input_identity).parameters


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


# Empreinte de sortie calculée par core/inference_service.py sur main e9fd90a
# (AVANT confidence-profile-v2) pour une décision dont le profil de confiance
# est persisté au format historique confidence-profile-v1 : elle doit rester
# reproductible octet pour octet depuis ce JSON v1 stocké, sans conversion
# en v2 ni changement de OUTPUT_SCHEMA_VERSION.
GOLDEN_V1_PROFILE_OUTPUT_FINGERPRINT = "8a7a2d6ca58adbfa1047d833c704be546b3d27814bbcc3d3072fdfb8d288cd64"


def _historical_v1_profile_decision():
    ns = uuid.UUID("6f1c2b0e-0000-4000-8000-000000000000")

    def dim(codes, letters):
        return {"fact_codes": codes, "capability_definition_ids": [str(uuid.uuid5(ns, x)) for x in letters],
                "limitations": []}

    profile = {"schema_version": "confidence-profile-v1",
               "diagnosticity": dim(["direct_representative_basis", "single_representative_episode"], ["A", "B"]),
               "coverage": dim(["localized_representative_scope", "coverage_concentrated_on_claim_scope",
                                "unobserved_capabilities_present"], ["A", "B", "D"]),
               "independence": dim(["independence_not_established"], []),
               "consistency": dim(["no_observed_current_tension"], []),
               "temporal_validation": dim(["single_episode_only", "exact_demonstration_time_unavailable"], [])}
    claims = {s: svc._Claim(s, "not_established", "none", "not_established:x", "none", None, None)
              for s in svc.CLAIM_STAGES}
    claims["application"] = svc._Claim("application", "established", "direct", "direct:x", "localized:x", profile,
                                       None)
    observation = uuid.uuid5(ns, "obs")
    return profile, svc._Decision("application", None, None, [], "summary", claims, (),
                                  (svc._Ref("positive_basis", "application", None, None, "observation", observation),))


def test_output_fingerprint_of_a_historical_v1_profile_is_reproduced_without_conversion():
    profile, decision = _historical_v1_profile_decision()
    kwargs = {"input_fingerprint": "in", "specification": {"confidence_profile_version": "confidence_profile-1"},
              "previous_stage": None, "transition": None, "tension_state": "none",
              "relations": svc._Relations({}, {}, {}), "membership_definitions": {}}
    assert svc.OUTPUT_SCHEMA_VERSION == 1
    assert svc._output_fingerprint(decision=decision, **kwargs) == GOLDEN_V1_PROFILE_OUTPUT_FINGERPRINT
    assert profile["schema_version"] == "confidence-profile-v1"  # jamais converti
    # Le format fait partie de la décision engagée : un même contenu en v2
    # a naturellement une autre empreinte (aucune équivalence implicite).
    v2 = {"schema_version": "confidence-profile-v2", **{name: {"facts": [
        {"code": code, "capability_definition_ids": []} for code in profile[name]["fact_codes"]],
        "limitations": []} for name in svc.CONFIDENCE_DIMENSIONS}}
    claims = {**decision.claims, "application": decision.claims["application"]._replace(confidence_profile=v2)}
    assert svc._output_fingerprint(decision=decision._replace(claims=claims), **kwargs) != \
        GOLDEN_V1_PROFILE_OUTPUT_FINGERPRINT


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
    assert context.predecessor_decision_context is None
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


@pytest.mark.parametrize("cause, claim_set, with_transition_ref, ok", [
    ("new_user_evidence", claims(), True, False),
    ("new_user_evidence", claims(), False, False),
    ("evidence_integrity_change", claims(), True, True),
    ("evidence_integrity_change", claims(), False, True),
    ("pedagogical_reinterpretation", claims(), True, True),
    ("pedagogical_reinterpretation", claims(), False, True),
    ("pedagogical_reinterpretation", claims("discovery"), True, False),
    ("evidence_integrity_change", claims("discovery"), True, False),
])
def test_pg_revision_to_non_etabli(engine, Sessions, db, world, cause, claim_set, with_transition_ref, ok):
    """revised_down -> non_etabli : jamais par new_user_evidence, jamais
    avec une claim established ; ref transition facultative (exception
    structurelle) par intégrité ou réinterprétation. Dossier corrigé
    (observation du predecessor invalidée) + nouvel événement, autre
    spécification : chaque cause a son support structurel."""
    w = world
    _activate_first(Sessions, w)
    _invalidate(Sessions, w.k)
    l2, (new,) = _new_t5(Sessions, w, contra(w.A))
    context = _start(Sessions, l2, state_decision_version="state-2")
    refs = [ref("transition", "observation", new)] if with_transition_ref else []
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


def _event_ids(engine, t5_run):
    return {r["event_id"] for r in _rows(
        engine, "SELECT r.event_id FROM longitudinal_assessment_inputs i JOIN pedagogical_observations o"
                " ON o.id = i.observation_id JOIN observation_evaluation_runs r ON r.id = o.evaluation_run_id"
                " WHERE i.run_id = :t", t=t5_run)}


def test_pg_same_dossier_is_neither_new_user_evidence_nor_integrity_change(engine, Sessions, db, world):
    """Même dossier logique que le predecessor (seule la spécification
    change) : ni nouvelle démonstration, ni changement d'intégrité ; seule
    une réinterprétation est recevable."""
    w = world
    _, context, _ = _second(Sessions, w)  # même dossier, autre spécification
    for cause, match in (("new_user_evidence", "new_user_evidence sans aucun CognitiveEvent"),
                         ("evidence_integrity_change", "sans aucune observation du dossier du predecessor invalidée")):
        with pytest.raises(InvalidInferenceDecision, match=match):
            svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w, cause=cause))
        db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id,
                                      decision=_app_decision(w, cause="pedagogical_reinterpretation"))


def test_pg_revision_to_non_etabli_without_invalidation_is_not_an_integrity_change(engine, Sessions, db, world):
    w = world
    _, context, _ = _second(Sessions, w)  # même dossier, autre spécification
    with pytest.raises(InvalidInferenceDecision, match="sans aucune observation du dossier du predecessor"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=decision(
            "non_etabli", claims(), cause="evidence_integrity_change"))


def test_pg_t3_reevaluation_is_never_new_user_evidence(engine, Sessions, db, world):
    """Predecessor : E1 / run T3 R1 / O1. Réévaluation du MÊME E1 : R2
    active, O2 remplace O1. Nouveau T5 avec O2 : O2 != O1 mais même
    événement => aucune nouvelle démonstration utilisateur ; sans
    invalidation, ce n'est pas une correction d'intégrité : c'est une
    réinterprétation (dossier logique changé, aucun nouvel événement)."""
    w = world
    first = _activate_first(Sessions, w)
    o1, e1 = w.app, w.app_t3.event
    reevaluation = _t3(Sessions, w.tx.id, app(w.A), event_id=e1)
    o2 = reevaluation.id
    assert o2 != o1 and reevaluation.event == e1 and reevaluation.run != w.app_t3.run
    assert _rows(engine, "SELECT interpretation_status FROM observation_evaluation_runs WHERE id = :r",
                 r=w.app_t3.run)[0]["interpretation_status"] == "superseded"
    l2 = _start_t5(Sessions, w.tx.id)
    _complete_t5(Sessions, l2)
    assert _event_ids(engine, l2) == _event_ids(engine, w.t5)  # aucun événement nouveau
    context = _start(Sessions, l2)
    assert context.predecessor.inference_run_id == first
    assert o2 in {o.observation_id for o in context.longitudinal_dossier.active_history.observations}
    reeval = decision("application", refs=[pos("application", o2)], cause="new_user_evidence")
    with pytest.raises(InvalidInferenceDecision, match="new_user_evidence sans aucun CognitiveEvent"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=reeval)
    db.rollback()
    # Aucune observation du predecessor invalidée : pas une correction d'intégrité.
    with pytest.raises(InvalidInferenceDecision, match="sans aucune observation du dossier du predecessor"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=dataclasses.replace(
            reeval, transition_cause="evidence_integrity_change"))
    db.rollback()
    # Mêmes spécifications T6 mais dossier réinterprété sans nouvelle démonstration.
    svc.complete_competency_inference(db, run_id=context.run_id, decision=dataclasses.replace(
        reeval, transition_cause="pedagogical_reinterpretation"))
    db.commit()
    assert _run(engine, context.run_id)["transition_cause"] == "pedagogical_reinterpretation"


def _closed_at(engine, event_id):
    return _rows(engine, "SELECT closed_at FROM cognitive_events WHERE id = :e", e=event_id)[0]["closed_at"]


def _started_at(engine, t5_run):
    return _rows(engine, "SELECT started_at FROM longitudinal_assessment_runs WHERE id = :r",
                 r=t5_run)[0]["started_at"]


def test_pg_an_old_event_newly_interpreted_in_the_competency_is_not_new_user_evidence(engine, Sessions, db):
    """E_old finalisé AVANT le snapshot T5 du predecessor, mais sa première
    évaluation T3 n'a produit aucune observation C7 : absent du dossier C7
    du predecessor. Réévalué ensuite (observation C7), il entre dans le
    nouveau dossier : ce n'est PAS une nouvelle démonstration (closed_at <=
    started_at du T5 predecessor). Contrôle : E_new finalisé APRÈS."""
    tx = _taxonomy(Sessions, ("C7_A", "C8_A"))
    A = tx.m["C7_A"]
    old_event = _finalized_event(Sessions)
    first_eval = _t3(Sessions, tx.id, sup(tx.m["C8_A"], competency_code="C8"), event_id=old_event)
    base = _t3(Sessions, tx.id, sup(A))
    l1 = _start_t5(Sessions, tx.id)
    _complete_t5(Sessions, l1)
    assert old_event not in _event_ids(engine, l1) and first_eval.event == old_event
    r1 = _start(Sessions, l1)
    _complete(Sessions, r1.run_id, decision("comprehension", refs=[pos("comprehension", base.id)]))
    reinterpreted = _t3(Sessions, tx.id, app(A), event_id=old_event)  # nouveau run T3 du même événement
    l2 = _start_t5(Sessions, tx.id)
    _complete_t5(Sessions, l2)
    assert old_event in _event_ids(engine, l2)
    assert _closed_at(engine, old_event) <= _started_at(engine, l1)
    r2 = _start(Sessions, l2)
    upgrade = decision("application", refs=[pos("application", reinterpreted.id),
                                             ref("transition", "observation", reinterpreted.id)],
                       cause="new_user_evidence")
    with pytest.raises(InvalidInferenceDecision, match="new_user_evidence sans aucun CognitiveEvent finalisé"):
        svc.complete_competency_inference(db, run_id=r2.run_id, decision=upgrade)
    db.rollback()
    with pytest.raises(InvalidInferenceDecision, match="sans aucune observation du dossier du predecessor"):
        svc.complete_competency_inference(db, run_id=r2.run_id, decision=dataclasses.replace(
            upgrade, transition_cause="evidence_integrity_change"))
    db.rollback()
    # Réinterprétation d'un événement déjà capturé : structurellement recevable.
    svc.complete_competency_inference(db, run_id=r2.run_id, decision=dataclasses.replace(
        upgrade, transition_cause="pedagogical_reinterpretation"))
    db.rollback()
    # Contrôle : E_new finalisé après la capture du snapshot précédent.
    fresh = _t3(Sessions, tx.id, app(A))
    l3 = _start_t5(Sessions, tx.id)
    _complete_t5(Sessions, l3)
    assert _closed_at(engine, fresh.event) > _started_at(engine, l1)
    svc.fail_competency_inference(db, run_id=r2.run_id, failure_code="stale_input")
    db.commit()
    r3 = _start(Sessions, l3)
    svc.complete_competency_inference(db, run_id=r3.run_id, decision=decision(
        "application", refs=[pos("application", fresh.id), ref("transition", "observation", fresh.id)],
        cause="new_user_evidence"))
    db.commit()
    assert (_run(engine, r3.run_id)["transition"], _run(engine, r3.run_id)["transition_cause"]) == (
        "upgraded", "new_user_evidence")


def test_pg_finalized_event_without_closed_at_is_never_guessed(engine, Sessions, db, world):
    w = world
    _, context, _ = _second(Sessions, w)
    _exec(engine, "UPDATE cognitive_events SET closed_at = NULL WHERE id = :e", e=w.app_t3.event)
    with pytest.raises(InvalidInferenceState, match="sans closed_at"):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="pedagogical_reinterpretation"))


def test_pg_a_new_cognitive_event_is_new_user_evidence(engine, Sessions, db, world):
    """Contrôle : predecessor E1..En ; nouveau T5 = mêmes événements + E2,
    finalisé après la capture du snapshot précédent."""
    w = world
    _activate_first(Sessions, w)
    fresh = _t3(Sessions, w.tx.id, sup(w.A))
    l2 = _start_t5(Sessions, w.tx.id)
    _complete_t5(Sessions, l2)
    assert _event_ids(engine, l2) - _event_ids(engine, w.t5) == {fresh.event}
    assert _closed_at(engine, fresh.event) > _started_at(engine, w.t5)
    context = _start(Sessions, l2)
    assert context.transition_causality.new_user_event_ids == (fresh.event,)
    # T6-B.2 : la nouvelle démonstration doit toucher une provenance de la
    # décision ; « un nouvel événement existe quelque part » ne suffit pas.
    with pytest.raises(InvalidInferenceDecision, match="aucune ref de la décision ne cite"):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="new_user_evidence"))
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(
        w, cause="new_user_evidence", refs=[pos("application", w.app), ref(
            "confidence", "observation", fresh.id, claim_stage="application", confidence_dimension="consistency")]))
    db.commit()
    assert _run(engine, context.run_id)["transition"] == "maintained"


def test_pg_integrity_change_after_an_invalidation(engine, Sessions, db, world):
    """Une correction d'intégrité RETIRE une observation : le dossier change
    sans nouvelle démonstration ; evidence_integrity_change recevable,
    new_user_evidence non."""
    w = world
    _activate_first(Sessions, w)
    _invalidate(Sessions, w.k)
    l2 = _start_t5(Sessions, w.tx.id)
    _complete_t5(Sessions, l2)
    assert _event_ids(engine, l2) < _event_ids(engine, w.t5)
    context = _start(Sessions, l2)
    with pytest.raises(InvalidInferenceDecision, match="new_user_evidence sans aucun CognitiveEvent"):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="new_user_evidence"))
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id,
                                      decision=_app_decision(w, cause="evidence_integrity_change"))
    db.commit()
    assert _run(engine, context.run_id)["transition_cause"] == "evidence_integrity_change"


def test_pg_last_positive_evidence_invalidated_revises_down_to_non_etabli_on_an_empty_dossier(
        engine, Sessions, db):
    """E1 supportive Discovery -> L1 -> R1 discovery ; invalidation de
    l'observation (service T3) -> L2 VIDE -> R2 non_etabli par
    evidence_integrity_change, sans aucune ref (aucune source n'existe)."""
    tx = _taxonomy(Sessions, ("C7_A",))
    e1 = _t3(Sessions, tx.id, sup(tx.m["C7_A"], stage="discovery"))
    l1 = _start_t5(Sessions, tx.id)
    _complete_t5(Sessions, l1)
    r1 = _start(Sessions, l1)
    _complete(Sessions, r1.run_id, decision("discovery", refs=[pos("discovery", e1.id)]))
    _invalidate(Sessions, e1.id)
    l2 = _start_t5(Sessions, tx.id)
    _complete_t5(Sessions, l2)
    assert _count(engine, "longitudinal_assessment_inputs", "run_id = :r", r=l2) == 0
    r2 = _start(Sessions, l2)
    assert r2.longitudinal_dossier.active_history.observations == ()
    reset = decision("non_etabli", claims(), cause="evidence_integrity_change")
    # Une ref transition reste soumise aux règles normales : jamais vers
    # une source du T5 du predecessor.
    with pytest.raises(InvalidInferenceBasisReference, match="hors du snapshot"):
        svc.complete_competency_inference(db, run_id=r2.run_id, decision=dataclasses.replace(
            reset, basis_refs=(ref("transition", "observation", e1.id),)))
    db.rollback()
    with pytest.raises(InvalidInferenceDecision, match="new_user_evidence"):
        svc.complete_competency_inference(db, run_id=r2.run_id, decision=dataclasses.replace(
            reset, transition_cause="new_user_evidence"))
    db.rollback()
    svc.complete_competency_inference(db, run_id=r2.run_id, decision=reset)
    db.commit()
    row = _run(engine, r2.run_id)
    assert (row["previous_stage"], row["transition"], row["current_stage"], row["transition_cause"]) == (
        "discovery", "revised_down", "non_etabli", "evidence_integrity_change")
    assert _state(engine, r1.run_id) == ("completed", "superseded") and _state(engine, r2.run_id) == (
        "completed", "active")
    cache = _cache(engine)
    assert (cache["active_inference_run_id"], cache["current_stage"], cache["state_generation"]) == (
        r2.run_id, "non_etabli", 2)
    assert _count(engine, "competency_inference_basis_refs", "inference_run_id = :r", r=r2.run_id) == 0
    assert [c.positive_basis_status for c in svc.get_stage_claims(db, run_id=r2.run_id)] == ["not_established"] * 4
    assert svc.get_validated_user_competency_state(db, user_id=USER, competency_code="C7").current_stage == \
        "non_etabli"


def test_pg_other_downward_revisions_still_require_a_transition_ref(engine, Sessions, db, world):
    w = world
    _, context, _ = _second(Sessions, w)
    for cause in ("evidence_integrity_change", "pedagogical_reinterpretation"):
        with pytest.raises(InvalidInferenceDecision, match="sans ref transition"):
            svc.complete_competency_inference(db, run_id=context.run_id, decision=decision(
                "comprehension", refs=[pos("comprehension", w.comp)], cause=cause))
        db.rollback()


def test_pg_pedagogical_reinterpretation_is_not_a_cover_for_new_evidence(engine, Sessions, db, world):
    """Mêmes spécifications T6, dossier changé PAR un nouvel événement :
    la cause plausible est new_user_evidence, pas une réinterprétation ;
    et un dossier identique sous mêmes spécifications est un reroll."""
    w = world
    # Nouveau dossier (événement nouveau), mêmes spécifications et relations
    # T5 recréées à l'identique (relation_delta : retained seulement) : aucun
    # fait d'évolution contrôlée (depuis T6-C3, un changement sémantique des
    # relations T5 en serait un).
    _activate_first(Sessions, w)
    new = _t3(Sessions, w.tx.id, sup(w.A)).id
    l2 = _start_t5(Sessions, w.tx.id)
    with Sessions() as s:
        t5.add_dependency(s, run_id=l2, target_observation_id=w.comp, source_kind="observation",
                          source_observation_id=w.disc, dependency_type="partially_dependent",
                          scope_mode="localized", dependency_basis={"why": "reprend la décomposition"},
                          capability_membership_ids=[w.B])
        t5.add_transfer(s, run_id=l2, source_observation_id=w.comp, target_observation_id=w.app,
                        scope_mode="localized", transfer_basis={"adaptation": "structure différente"},
                        capability_membership_ids=[w.A])
        t5.add_revalidation(s, run_id=l2, source_contradiction_observation_id=w.kB,
                            target_supportive_observation_id=w.app_b, scope_mode="whole_observation",
                            revalidation_basis={"mechanism": "démontré sans aide"})
        s.commit()
    _complete_t5(Sessions, l2)
    context = _start(Sessions, l2)
    delta = context.transition_causality.relation_delta
    assert all(not f.added and not f.removed and f.retained
               for f in (delta.dependencies, delta.transfers, delta.revalidations))
    with pytest.raises(InvalidInferenceDecision, match="sans spécification T6 différente"):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(
        w, cause="new_user_evidence", refs=[pos("application", w.app), ref("transition", "observation", new)]))


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
    with pytest.raises(InvalidInferenceDecision, match="anciennes démonstrations"):
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
    with pytest.raises(InvalidInferenceDecision, match="new_user_evidence sans aucun CognitiveEvent"):
        svc.complete_competency_inference(db, run_id=again.run_id, decision=decision(
            "application", refs=[pos("application", w.app), ref("transition", "observation", k_new)],
            cause="new_user_evidence"))


def test_pg_anti_oscillation_counts_cognitive_events_not_reevaluated_observations(engine, Sessions, db, world):
    """Faiblesse non résolue ; E(app) est réévalué (O1 -> O2) et un vrai
    nouvel événement E3 existe. Citer O2 (même événement réévalué) ne
    suffit pas ; citer E3, directement ou via une relation T5 dont E3 est
    une extrémité, est structurellement recevable."""
    w = world
    first, revise, (k_new,) = _second(Sessions, w, new=[contra(w.A)])
    _complete(Sessions, revise.run_id, decision(
        "comprehension", refs=[pos("comprehension", w.comp), ref("transition", "observation", k_new),
                               ref("tension", "observation", k_new, tension_key="levier")],
        cause="new_user_evidence", context={"motif": "levier"}, tensions=[tension("levier", stage="application")]))
    o2 = _t3(Sessions, w.tx.id, app(w.A), event_id=w.app_t3.event).id
    e3 = _t3(Sessions, w.tx.id, app(w.A)).id
    l3 = _start_t5(Sessions, w.tx.id)
    with Sessions() as s:
        transfer = t5.add_transfer(s, run_id=l3, source_observation_id=w.comp, target_observation_id=e3,
                                   scope_mode="localized", transfer_basis={"adaptation": "nouveau contexte"},
                                   capability_membership_ids=[w.A]).id
        s.commit()
    _complete_t5(Sessions, l3)
    rebound = _start(Sessions, l3)
    reevaluated = decision("application", refs=[pos("application", o2), ref("transition", "observation", o2)],
                           cause="new_user_evidence")
    with pytest.raises(InvalidInferenceDecision, match="CognitiveEvent nouveau"):
        svc.complete_competency_inference(db, run_id=rebound.run_id, decision=reevaluated)
    db.rollback()
    svc.complete_competency_inference(db, run_id=rebound.run_id, decision=dataclasses.replace(
        reevaluated, basis_refs=(pos("application", o2), ref("transition", "transfer", transfer))))
    db.rollback()
    svc.complete_competency_inference(db, run_id=rebound.run_id, decision=dataclasses.replace(
        reevaluated, basis_refs=(pos("application", o2), ref("transition", "observation", e3))))
    db.commit()
    assert _run(engine, rebound.run_id)["transition"] == "upgraded"


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


# --- L. T6-B.1 : contexte décisionnel historique du predecessor --------------------------

RICH_NEEDS = [{"besoin": "revalider le levier", "portée": ["C7_A"], "détail": {"ordre": 1, "réel": 2.5}},
              {"besoin": "observer un transfert"}]
RICH_SUMMARY = "Application établie ; levier C7_A fragilisé."


def _rich_decision(w, **overrides):
    """Predecessor riche : Discovery / Comprehension implied, Application
    direct, Mastery not_established (mastery_assessment) ; refs de chaque
    rôle ; tensions localized / whole_competency / competency_only."""
    claim_set = (
        claim("discovery", "established", "implied_by_higher_claim", basis_summary="impliquée par Application",
              confidence_profile={"coverage": {"note": "impliquée"}}),
        claim("comprehension", "established", "implied_by_higher_claim", scope_summary="C7_A, C7_B",
              confidence_profile={"coverage": {"note": "deux capacités"}}),
        claim("application", "established", "direct", basis_summary="application démontrée",
              scope_summary="C7_A", confidence_profile={"diagnosticity": {"note": "élevée"},
                                                        "independence": {"sources": ["transfert", 1, 2.5, True,
                                                                                     None]}}),
        claim("mastery", mastery_assessment={"transfert": ["un seul"], "verdict": "insuffisant"}),
    )
    refs = [pos("application", w.app),
            ref("confidence", "transfer", w.tr, claim_stage="application", confidence_dimension="independence"),
            ref("confidence", "observation", w.comp, claim_stage="comprehension", confidence_dimension="coverage"),
            ref("mastery", "transfer", w.tr, claim_stage="mastery"),
            ref("tension", "observation", w.k, tension_key="levier"),
            ref("tension", "observation", w.k_only, tension_key="global"),
            ref("tension", "dependency", w.dep, tension_key="seul"),
            ref("transition", "observation", w.app),
            ref("validation", "revalidation", w.rv),
            ref("validation", "observation", w.kB)]
    tensions = [tension("levier", mode="localized", memberships=(w.A,)),
                tension("global", mode="whole_competency", summary="fragilité globale",
                        revision_status="revalidation_needed"),
                tension("seul", stage="comprehension", mode="competency_only", summary="compétence seule")]
    kwargs = {"claim_set": claim_set, "refs": refs, "tensions": tensions, "needs": RICH_NEEDS,
              "summary": RICH_SUMMARY}
    kwargs.update(overrides)
    return decision("application", **kwargs)


def _assert_stable_projection(history, live):
    """history = vue T5-C `live` du même run T5, moins ce qu'elle relit au
    présent : statut du run, current_* des observations, limitation
    upstream_evidence_changed_since_snapshot."""
    assert history.run_id == live.run_id
    for f in dataclasses.fields(PredecessorLongitudinalContext):
        if f.name not in ("observations", "episodes", "limitations"):
            assert getattr(history, f.name) == getattr(live, f.name), f.name
    assert history.episodes == live.active_history.episodes
    assert len(history.observations) == len(live.active_history.observations)
    for projected, observed in zip(history.observations, live.active_history.observations):
        assert isinstance(projected, PredecessorHistoricalObservation)
        for f in dataclasses.fields(PredecessorHistoricalObservation):
            assert getattr(projected, f.name) == getattr(observed, f.name), f.name
        for name in LIVE_OBSERVATION_FIELDS:
            assert not hasattr(projected, name)
    assert history.limitations == tuple(lim for lim in live.limitations
                                        if lim.code != view.UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT)
    assert not hasattr(history, "interpretation_status") and not hasattr(history, "execution_status")


def _refs_view(refs):
    return [(r.ref_role, r.confidence_dimension, r.source_kind, r.source_id) for r in refs]


def _rich_predecessor(Sessions, w):
    """R1 riche actif sur L1, puis L2 (nouvelle observation) qui supersede L1
    et candidat R2. Retourne (R1, contexte de R2, observation nouvelle)."""
    first = _activate_first(Sessions, w, _rich_decision(w))
    l2, (new,) = _new_t5(Sessions, w, sup(w.A))
    return first, _start(Sessions, l2), new


def test_pg_first_inference_has_neither_predecessor_nor_decision_context(engine, Sessions, db, world):
    context = _start(Sessions, world.t5)
    assert context.predecessor is None and context.predecessor_decision_context is None
    assert svc.get_inference_context(db, run_id=context.run_id).predecessor_decision_context is None


def test_pg_predecessor_decision_context_is_the_persisted_historical_decision(engine, Sessions, db, world):
    w = world
    first, context, _ = _rich_predecessor(Sessions, w)
    p, rich = context.predecessor, context.predecessor_decision_context
    assert isinstance(rich, PredecessorDecisionContext)
    assert (p.inference_run_id, p.longitudinal_assessment_run_id) == (first, w.t5)
    assert (rich.inference_run_id, rich.longitudinal_assessment_run_id) == (first, w.t5)
    assert rich.pedagogical_taxonomy_release_id == w.tx.id
    # Dossier HISTORIQUE (L1, superseded : normal), distinct du dossier courant,
    # exposé comme projection stable de la vue T5-C.
    history = rich.historical_longitudinal_context
    assert isinstance(history, PredecessorLongitudinalContext)
    assert history.run_id == w.t5 and context.longitudinal_dossier.run_id != w.t5
    live = view.build_longitudinal_dossier(db, run_id=w.t5)
    assert live.interpretation_status == "superseded"
    _assert_stable_projection(history, live)
    # Quatre claims, ordre conceptuel, contenus exacts.
    assert [c.stage for c in rich.claims] == list(STAGES)
    assert [(c.positive_basis_status, c.basis_mode, c.basis_summary, c.scope_summary) for c in rich.claims] == [
        ("established", "implied_by_higher_claim", "impliquée par Application", None),
        ("established", "implied_by_higher_claim", None, "C7_A, C7_B"),
        ("established", "direct", "application démontrée", "C7_A"),
        ("not_established", "none", None, None)]
    discovery, comprehension, application, mastery = rich.claims
    assert application.confidence_profile == {"diagnosticity": {"note": "élevée"},
                                              "independence": {"sources": ("transfert", 1, 2.5, True, None)}}
    assert mastery.confidence_profile is None and application.mastery_assessment is None
    assert mastery.mastery_assessment == {"transfert": ("un seul",), "verdict": "insuffisant"}
    # Refs rattachées à leur claim (aucun stage_claim_id exposé).
    assert _refs_view(application.basis_refs) == [
        ("confidence", "independence", "transfer", w.tr), ("positive_basis", None, "observation", w.app)]
    assert _refs_view(comprehension.basis_refs) == [("confidence", "coverage", "observation", w.comp)]
    assert _refs_view(mastery.basis_refs) == [("mastery", None, "transfer", w.tr)]
    assert discovery.basis_refs == ()
    # Refs run-level : transition + validation, et seulement elles.
    assert sorted(_refs_view(rich.run_basis_refs), key=str) == sorted([
        ("transition", None, "observation", w.app), ("validation", None, "observation", w.kB),
        ("validation", None, "revalidation", w.rv)], key=str)
    assert [r.ref_role for r in rich.run_basis_refs] == ["transition", "validation", "validation"]
    # Tensions, scopes (membership historique + définition), provenance.
    by_mode = {t.scope_mode: t for t in rich.tensions}
    assert set(by_mode) == {"localized", "whole_competency", "competency_only"} and len(rich.tensions) == 3
    localized = by_mode["localized"]
    assert (localized.fragilized_stage, localized.summary, localized.revision_status) == (
        "application", "contradiction récente", "unresolved")
    assert localized.capability_membership_ids == (w.A,) and localized.capability_definition_ids == (w.dA,)
    assert _refs_view(localized.basis_refs) == [("tension", None, "observation", w.k)]
    whole, only_c = by_mode["whole_competency"], by_mode["competency_only"]
    assert (whole.summary, whole.revision_status, only_c.fragilized_stage) == (
        "fragilité globale", "revalidation_needed", "comprehension")
    for t in (whole, only_c):
        assert t.capability_membership_ids == () and t.capability_definition_ids == ()
    assert _refs_view(whole.basis_refs) == [("tension", None, "observation", w.k_only)]
    assert _refs_view(only_c.basis_refs) == [("tension", None, "dependency", w.dep)]
    # validation_needs et state_decision_summary tels que persistés.
    assert rich.validation_needs == (
        {"besoin": "revalider le levier", "portée": ("C7_A",), "détail": {"ordre": 1, "réel": 2.5}},
        {"besoin": "observer un transfert"})
    assert rich.state_decision_summary == RICH_SUMMARY
    # La vérification repose sur l'empreinte persistée, jamais réécrite.
    assert p.output_fingerprint == _run(engine, first)["output_fingerprint"]


def test_pg_historical_ref_and_tension_order_is_deterministic(engine, Sessions, db, world):
    """Même décision fournie dans un autre ordre (refs, tensions, clés
    locales) : contexte historique identique ; ordre des refs = (ref_role,
    confidence_dimension, source_kind, source_id)."""
    w = world
    d = _rich_decision(w)
    shuffled = dataclasses.replace(d, claims=tuple(reversed(d.claims)), tensions=tuple(reversed(d.tensions)),
                                   basis_refs=tuple(reversed(d.basis_refs)))
    contexts = []
    for i, variant in enumerate((d, shuffled)):
        context = _start(Sessions, w.t5, evaluator_version=f"e-{i}")
        run = svc.complete_competency_inference(db, run_id=context.run_id, decision=variant)
        contexts.append(svc._build_predecessor_decision_context(db, svc._run_row(db, run.id)))
        db.rollback()
    one, two = contexts
    assert (one.claims, one.tensions, one.run_basis_refs) == (two.claims, two.tensions, two.run_basis_refs)
    for refs in (one.run_basis_refs, *(c.basis_refs for c in one.claims), *(t.basis_refs for t in one.tensions)):
        keys = [(r.ref_role, r.confidence_dimension or "", r.source_kind, str(r.source_id)) for r in refs]
        assert keys == sorted(keys)


def _event_of(engine, observation_id):
    return _rows(engine, "SELECT r.event_id FROM pedagogical_observations o JOIN observation_evaluation_runs r"
                         " ON r.id = o.evaluation_run_id WHERE o.id = :o", o=observation_id)[0]["event_id"]


def test_pg_start_and_resume_expose_the_same_rich_context(engine, Sessions, db, world):
    """start == get_inference_context, immédiatement ET malgré une mutation
    LIVE d'un amont historique qui ne touche pas le T5 courant : w.kB (citée
    par une ref validation de R1) est invalidée avant L2, donc absente de
    L2 ; APRÈS le start de R2, son événement est réévalué par les services
    T3 normaux (run T3 de w.kB superseded)."""
    w = world
    first = _activate_first(Sessions, w, _rich_decision(w))
    _invalidate(Sessions, w.kB)
    l2, _ = _new_t5(Sessions, w)
    started = _start(Sessions, l2)
    assert w.kB not in {o.observation_id for o in started.longitudinal_dossier.active_history.observations}
    history = started.predecessor_decision_context.historical_longitudinal_context
    assert w.kB in {o.observation_id for o in history.observations}
    assert svc.get_inference_context(db, run_id=started.run_id) == started
    db.rollback()
    _t3(Sessions, w.tx.id, contra(w.B), event_id=_event_of(engine, w.kB))
    live = view.build_longitudinal_dossier(db, run_id=w.t5)
    live_kb = {o.observation_id: o for o in live.active_history.observations}[w.kB]
    assert (live_kb.current_integrity_status, live_kb.current_evaluation_run_interpretation_status) == (
        "invalidated", "superseded")
    resumed = svc.get_inference_context(db, run_id=started.run_id)
    assert resumed == started
    a, b = started.predecessor_decision_context, resumed.predecessor_decision_context
    assert a is not b and a == b and a.inference_run_id == first
    assert (a.historical_longitudinal_context, a.claims, a.tensions, a.run_basis_refs, a.validation_needs,
            a.state_decision_summary) == (b.historical_longitudinal_context, b.claims, b.tensions,
                                          b.run_basis_refs, b.validation_needs, b.state_decision_summary)
    assert resumed.predecessor == started.predecessor
    assert resumed.longitudinal_dossier == started.longitudinal_dossier
    _assert_stable_projection(b.historical_longitudinal_context, live)
    assert _count(engine, "competency_inference_runs") == 2 and not db.new and not db.dirty


def _walk(value, path="context"):
    """Chaque valeur atteignable : aucun dict / list / set mutable, aucun
    objet ORM ni ligne SQLAlchemy."""
    assert not isinstance(value, (dict, list, set, bytearray)), path
    assert not hasattr(type(value), "__table__") and not isinstance(value, sa.engine.Row), path
    if dataclasses.is_dataclass(value):
        assert type(value).__dataclass_params__.frozen, path
        for f in dataclasses.fields(value):
            _walk(getattr(value, f.name), f"{path}.{f.name}")
    elif isinstance(value, MappingProxyType):
        for key, item in value.items():
            _walk(item, f"{path}[{key!r}]")
    elif isinstance(value, tuple):
        for i, item in enumerate(value):
            _walk(item, f"{path}[{i}]")
    else:
        assert value is None or isinstance(value, (str, int, float, bool, uuid.UUID, type(svc._utcnow()))), (
            path, type(value))


def test_pg_rich_context_is_deeply_immutable_and_orm_free(engine, Sessions, db, world):
    w = world
    _, context, _ = _rich_predecessor(Sessions, w)
    rich = context.predecessor_decision_context
    _walk(context)
    application = rich.claims[2]
    for mapping, key in ((application.confidence_profile, "x"), (application.confidence_profile["independence"], "x"),
                         (rich.claims[3].mastery_assessment, "verdict"), (rich.validation_needs[0], "besoin"),
                         (rich.validation_needs[0]["détail"], "ordre")):
        with pytest.raises(TypeError):
            mapping[key] = "muté"
    for collection in (rich.claims, rich.tensions, rich.run_basis_refs, rich.validation_needs,
                       application.basis_refs, rich.tensions[0].capability_membership_ids,
                       rich.tensions[0].capability_definition_ids,
                       application.confidence_profile["independence"]["sources"]):
        assert isinstance(collection, tuple)
    for target, field in ((rich, "claims"), (rich, "validation_needs"), (application, "confidence_profile"),
                          (rich.tensions[0], "capability_membership_ids"), (rich.run_basis_refs[0], "source_id"),
                          (context, "predecessor_decision_context")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(target, field, None)


def test_pg_historical_t5_superseded_and_t3_reevaluated_stay_readable(engine, Sessions, db, world):
    """Après activation du predecessor : réévaluation T3 de l'événement de
    sa positive_basis (run T3 superseded), puis nouveau T5 qui supersede L1.
    Le contexte historique L1 garde l'ancienne observation comme fait du
    snapshot, sans statut relu au présent ; la vue T5-C live l'expose et le
    signale (audit)."""
    w = world
    first = _activate_first(Sessions, w, _rich_decision(w))
    before = _run(engine, first)
    o2 = _t3(Sessions, w.tx.id, app(w.A), event_id=w.app_t3.event).id
    l2 = _start_t5(Sessions, w.tx.id)
    _complete_t5(Sessions, l2)
    context = _start(Sessions, l2)
    rich = context.predecessor_decision_context
    history = rich.historical_longitudinal_context
    assert w.app in {o.observation_id for o in history.observations}
    assert o2 not in {o.observation_id for o in history.observations}
    live = view.build_longitudinal_dossier(db, run_id=w.t5)
    live_app = {o.observation_id: o for o in live.active_history.observations}[w.app]
    assert live_app.current_evaluation_run_interpretation_status == "superseded"
    assert view.UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT in {lim.code for lim in live.limitations}
    assert view.UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT not in {lim.code for lim in history.limitations}
    _assert_stable_projection(history, live)
    assert ("positive_basis", None, "observation", w.app) in _refs_view(rich.claims[2].basis_refs)
    # Le dossier courant contient la réévaluation ; l'historique reste l'ancien.
    assert o2 in {o.observation_id for o in context.longitudinal_dossier.active_history.observations}
    assert _run(engine, first) == before  # predecessor jamais recalculé ni réécrit


def test_pg_invalidated_observation_never_erases_the_historical_decision(engine, Sessions, db, world):
    """O (positive_basis du predecessor) invalidée ensuite : le contexte
    historique la contient toujours comme fait du snapshot (la vue T5-C live
    montre invalidated), la positive_basis historique est toujours exposée ;
    aucun recalcul ni réécriture du predecessor."""
    w = world
    first = _activate_first(Sessions, w, _rich_decision(w))
    t6_before = _t6_snapshot(engine)
    _invalidate(Sessions, w.app)
    l2, _ = _new_t5(Sessions, w)
    context = _start(Sessions, l2)
    rich = context.predecessor_decision_context
    assert w.app not in {o.observation_id for o in context.longitudinal_dossier.active_history.observations}
    assert w.app in {o.observation_id for o in rich.historical_longitudinal_context.observations}
    live = view.build_longitudinal_dossier(db, run_id=w.t5)
    assert {o.observation_id: o for o in live.active_history.observations}[w.app].current_integrity_status == \
        "invalidated"
    _assert_stable_projection(rich.historical_longitudinal_context, live)
    assert ("positive_basis", None, "observation", w.app) in _refs_view(rich.claims[2].basis_refs)
    assert ("transition", None, "observation", w.app) in _refs_view(rich.run_basis_refs)
    assert [c.positive_basis_status for c in rich.claims] == ["established"] * 3 + ["not_established"]
    assert context.predecessor.current_stage == "application"
    # Predecessor et ses lignes enfants intacts (seul le candidat est nouveau).
    after = _t6_snapshot(engine)
    assert {t: v for t, v in after.items() if t != "competency_inference_runs"} == {
        t: v for t, v in t6_before.items() if t != "competency_inference_runs"}
    assert _run(engine, first)["output_fingerprint"] == context.predecessor.output_fingerprint


def test_pg_cross_release_scope_stays_historical(engine, Sessions, db, world):
    """Predecessor sur R1 (localized C7_A = membership M1 de R1) ; R2 réutilise
    la même définition sous un autre membership, R1 est retirée. Le contexte
    courant est sur R2 ; l'historique expose M1 (R1) et la définition, sans
    aucun remapping vers R2."""
    w = world
    first = _activate_first(Sessions, w, _rich_decision(w))
    r2 = _taxonomy(Sessions, {"C7_A": w.dA, "C7_B": w.dB, "C7_C": w.tx.d["C7_C"]})
    assert r2.m["C7_A"] != w.A and r2.d["C7_A"] == w.dA
    assert _rows(engine, "SELECT status FROM pedagogical_taxonomy_releases WHERE id = :i",
                 i=w.tx.id)[0]["status"] == "retired"
    _t3(Sessions, r2.id, sup(r2.m["C7_A"]))
    l2 = _start_t5(Sessions, r2.id)
    _complete_t5(Sessions, l2)
    context = _start(Sessions, l2)
    rich = context.predecessor_decision_context
    assert context.pedagogical_taxonomy_release_id == r2.id
    assert rich.inference_run_id == first and rich.pedagogical_taxonomy_release_id == w.tx.id
    assert rich.historical_longitudinal_context.pedagogical_taxonomy_release_id == w.tx.id
    (localized,) = [t for t in rich.tensions if t.scope_mode == "localized"]
    assert localized.capability_membership_ids == (w.A,) and localized.capability_definition_ids == (w.dA,)
    assert r2.m["C7_A"] not in localized.capability_membership_ids


def _corrupt_ref(engine, first, role, sql_set, **params):
    _exec(engine, f"UPDATE competency_inference_basis_refs SET {sql_set} WHERE inference_run_id = :r"
                  f" AND ref_role = '{role}'", r=first, **params)


def _foreign_revalidation(Sessions, w):
    other = _start_t5(Sessions, w.tx.id)
    with Sessions() as s:
        foreign = t5.add_revalidation(s, run_id=other, source_contradiction_observation_id=w.k,
                                      target_supportive_observation_id=w.app, scope_mode="competency_only",
                                      revalidation_basis={"w": 1}).id
        s.commit()
    return foreign


def _apply_historical_corruption(Sessions, engine, w, first, case):
    run = "UPDATE competency_inference_runs SET {} WHERE id = :r"
    claim_sql = "UPDATE competency_stage_claims SET {} WHERE inference_run_id = :r AND stage = '{}'"
    tension_sql = "UPDATE competency_inference_tensions SET {} WHERE inference_run_id = :r AND scope_mode = '{}'"
    scope_sql = ("UPDATE competency_inference_tension_capabilities SET capability_membership_id = :m WHERE"
                 " tension_id IN (SELECT id FROM competency_inference_tensions WHERE inference_run_id = :r)")
    corruption = {
        "claim_basis_summary": (claim_sql.format("basis_summary = 'réécrit'", "application"), {}),
        "claim_scope_summary": (claim_sql.format("scope_summary = 'C7_B'", "application"), {}),
        "claim_confidence_profile": (claim_sql.format(
            "confidence_profile = '{\"diagnosticity\": {\"note\": \"faible\"}}'", "application"), {}),
        "claim_mastery_assessment": (claim_sql.format("mastery_assessment = NULL", "mastery"), {}),
        "claim_status": (claim_sql.format("positive_basis_status = 'not_established', basis_mode = 'none',"
                                          " confidence_profile = NULL", "discovery"), {}),
        "missing_claim": ("DELETE FROM competency_stage_claims WHERE inference_run_id = :r AND stage = 'discovery'",
                          {}),
        "tension_summary": (tension_sql.format("summary = 'autre motif'", "whole_competency"), {}),
        "tension_revision_status": (tension_sql.format("revision_status = 'unresolved'", "whole_competency"), {}),
        "tension_stage": (tension_sql.format("fragilized_stage = 'discovery'", "localized"), {}),
        "tension_scope_mode": (tension_sql.format("scope_mode = 'whole_competency'", "competency_only"), {}),
        "tension_membership_swapped": (scope_sql, {"m": w.B}),
        "tension_membership_removed": ("DELETE FROM competency_inference_tension_capabilities", {}),
        "tension_membership_other_competency": (scope_sql, {"m": w.tx.m["C8_A"]}),
        "missing_tension": ("DELETE FROM competency_inference_basis_refs WHERE inference_run_id = :r AND tension_id"
                            " IN (SELECT id FROM competency_inference_tensions WHERE scope_mode = 'competency_only');"
                            " DELETE FROM competency_inference_tensions WHERE inference_run_id = :r"
                            " AND scope_mode = 'competency_only'", {}),
        "validation_needs": (run.format("validation_needs = '[{\"besoin\": \"autre\"}]'"), {}),
        "validation_needs_null": (run.format("validation_needs = NULL"), {}),
        "state_decision_summary": (run.format("state_decision_summary = 'réécrit'"), {}),
        "transition_cause": (run.format("transition_cause = 'new_user_evidence'"), {}),
        "specification": (run.format("evaluator_version = 'autre'"), {}),
        "input_fingerprint": (run.format("input_fingerprint = 'x'"), {}),
        "output_fingerprint": (run.format("output_fingerprint = repeat('0', 64)"), {}),
        "output_fingerprint_null": (run.format("output_fingerprint = NULL"), {}),
        "completed_at_null": (run.format("completed_at = NULL"), {}),
    }
    if case in corruption:
        sql, params = corruption[case]
        for statement in sql.split("; "):
            _exec(engine, statement, r=first, **params)
    elif case == "tension_membership_other_release":
        other = _taxonomy(Sessions, ("C7_A",), activate=False)
        _exec(engine, scope_sql, r=first, m=other.m["C7_A"])
    elif case == "ref_source_swapped":
        _corrupt_ref(engine, first, "positive_basis", "source_observation_id = :o", o=w.comp)
    elif case == "ref_moved_to_another_claim":
        _corrupt_ref(engine, first, "mastery", "stage_claim_id = (SELECT id FROM competency_stage_claims"
                                               " WHERE inference_run_id = :r AND stage = 'application')")
    elif case == "ref_deleted":
        _exec(engine, "DELETE FROM competency_inference_basis_refs WHERE inference_run_id = :r"
                      " AND source_kind = 'revalidation'", r=first)
    elif case == "ref_added":
        _exec(engine, "INSERT INTO competency_inference_basis_refs (id, inference_run_id, ref_role, source_kind,"
                      " source_observation_id, created_at) VALUES (:i, :r, 'validation', 'observation', :o, now())",
              i=uuid.uuid4(), r=first, o=w.comp)
    elif case == "ref_observation_outside_snapshot":
        _corrupt_ref(engine, first, "positive_basis", "source_observation_id = :o",
                     o=_t3(Sessions, w.tx.id, app(w.A)).id)
    elif case == "ref_relation_outside_parent":
        _exec(engine, "UPDATE competency_inference_basis_refs SET source_revalidation_id = :v WHERE"
                      " inference_run_id = :r AND source_kind = 'revalidation'", r=first,
              v=_foreign_revalidation(Sessions, w))
    elif case in ("ref_missing_observation", "ref_missing_relation"):
        # Une FK PostgreSQL rend une source absente impossible en temps
        # normal ; on la contourne (superuser, triggers de FK désactivés
        # localement) pour simuler une corruption réelle.
        column = "source_observation_id" if case == "ref_missing_observation" else "source_revalidation_id"
        with engine.begin() as conn:
            conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
            conn.execute(sa.text(f"UPDATE competency_inference_basis_refs SET {column} = :v WHERE"
                                 f" inference_run_id = :r AND {column} IS NOT NULL"), {"v": uuid.uuid4(), "r": first})
    elif case == "parent_other_couple":
        _t3(Sessions, w.tx.id, sup(w.tx.m["C8_A"], competency_code="C8"))
        c8 = _start_t5(Sessions, w.tx.id, competency_code="C8")
        _complete_t5(Sessions, c8)
        _exec(engine, run.format("longitudinal_assessment_run_id = :t"), r=first, t=c8)
    else:
        raise AssertionError(case)


HISTORICAL_CORRUPTIONS = [
    "claim_basis_summary", "claim_scope_summary", "claim_confidence_profile", "claim_mastery_assessment",
    "claim_status", "missing_claim", "tension_summary", "tension_revision_status", "tension_stage",
    "tension_scope_mode", "tension_membership_swapped", "tension_membership_removed",
    "tension_membership_other_competency", "tension_membership_other_release", "missing_tension",
    "ref_source_swapped", "ref_moved_to_another_claim", "ref_deleted", "ref_added",
    "ref_observation_outside_snapshot", "ref_relation_outside_parent", "ref_missing_observation",
    "ref_missing_relation", "validation_needs", "validation_needs_null", "state_decision_summary",
    "transition_cause", "specification", "input_fingerprint", "output_fingerprint", "output_fingerprint_null",
    "completed_at_null", "parent_other_couple",
]
# Motif attendu ; par défaut, le contenu modifié ne reproduit plus
# output_fingerprint (jamais recalculé ni réécrit).
CORRUPTION_REASONS = {
    "missing_claim": "exactement quatre attendues",
    "tension_membership_removed": "localized avec 0 membership",
    "tension_membership_other_competency": "hors de C7",
    "tension_membership_other_release": "hors de la release historique",
    "ref_observation_outside_snapshot": "absente du snapshot",
    "ref_missing_observation": "absente du snapshot",
    "ref_relation_outside_parent": "absente ou hors du run T5",
    "ref_missing_relation": "absente ou hors du run T5",
    "validation_needs_null": "sorties absentes : validation_needs",
    "output_fingerprint_null": "sorties absentes : output_fingerprint",
    "completed_at_null": "sorties absentes : completed_at",
    "input_fingerprint": "input_fingerprint non reproductible",
    "parent_other_couple": "d'un autre couple",
}
# Champs du payload predecessor de input_fingerprint (T6-B, inchangé) : leur
# corruption rend d'abord l'entrée du candidat non reproductible.
PAYLOAD_CORRUPTIONS = {"output_fingerprint", "output_fingerprint_null"}


@pytest.mark.parametrize("case", HISTORICAL_CORRUPTIONS)
def test_pg_corrupted_predecessor_history_is_refused_never_repaired(engine, Sessions, db, world, case):
    """Historique corrompu (SQL direct, output_fingerprint jamais mis à
    jour) : InvalidInferenceState, ni candidat créé, ni reprise, ni
    réparation. Jamais StaleInferenceInput pour la reconstruction elle-même."""
    w = world
    first = _activate_first(Sessions, w, _rich_decision(w))
    candidate = _start(Sessions, w.t5, state_decision_version="state-2")
    _apply_historical_corruption(Sessions, engine, w, first, case)
    before = _t6_snapshot(engine)
    reason = CORRUPTION_REASONS.get(case, "output_fingerprint non reproductible depuis la décision persistée")
    with pytest.raises(InvalidInferenceState, match=f"historique corrompu .*{reason}"):
        svc._build_predecessor_decision_context(db, svc._run_row(db, first))
    db.rollback()
    with pytest.raises(InvalidInferenceState):
        svc.start_competency_inference(db, **_start_kwargs(w.t5, state_decision_version="state-3"))
    db.rollback()
    with pytest.raises(StaleInferenceInput if case in PAYLOAD_CORRUPTIONS else InvalidInferenceState):
        svc.get_inference_context(db, run_id=candidate.run_id)
    db.rollback()
    assert _t6_snapshot(engine) == before
    assert _count(engine, "competency_inference_runs") == 2
    assert _state(engine, first) == ("completed", "active")


def test_pg_duplicate_or_unknown_claims_are_prevented_by_the_schema(engine, Sessions, db, world):
    """Claim dupliquée / inconnue : impossibles à persister (UNIQUE
    (inference_run_id, stage), CHECK stage) ; la reconstruction les refuse
    néanmoins (défense en profondeur, testée par les chemins manquants)."""
    first = _activate_first(Sessions, world, _rich_decision(world))
    for stage in ("mastery", "non_etabli"):
        with pytest.raises(sa.exc.IntegrityError):
            _exec(engine, "INSERT INTO competency_stage_claims (id, inference_run_id, stage, positive_basis_status,"
                          " basis_mode, created_at) VALUES (:i, :r, :s, 'not_established', 'none', now())",
                  i=uuid.uuid4(), r=first, s=stage)
    assert svc._build_predecessor_decision_context(db, svc._run_row(db, first)).inference_run_id == first


def test_pg_rich_context_changes_neither_input_fingerprint_nor_dedup_key(engine, Sessions, db, world):
    """Le contexte riche n'entre ni dans input_fingerprint ni dans
    inference_dedup_key : recalcul T6-B V1 depuis le dossier et le seul
    payload predecessor ; aucune nouvelle identité logique."""
    w = world
    first, context, _ = _rich_predecessor(Sessions, w)
    row = _run(engine, context.run_id)
    parent = svc._parent_row(db, context.longitudinal_assessment_run_id, lock=False)
    predecessor = svc._run_row(db, first)
    expected = svc._input_fingerprint_v2(svc._dossier_payload(parent, svc._relations(db, parent.id)),
                                         svc._predecessor_payload(context.predecessor,
                                                                  predecessor.unresolved_revision_context),
                                         svc._causality_fingerprint_payload(context.transition_causality))
    assert row["input_fingerprint"] == context.input_fingerprint == expected
    assert row["inference_dedup_key"] == svc._inference_dedup_key(expected, svc._specification(row))
    # Même entrée logique : toujours un doublon, jamais un nouveau run.
    with pytest.raises(DuplicateInference):
        svc.start_competency_inference(db, **_start_kwargs(context.longitudinal_assessment_run_id,
                                                           trigger="manual_audit"))
    db.rollback()
    assert _count(engine, "competency_inference_runs") == 2


def test_pg_rich_context_reconstruction_is_select_only(engine, Sessions, db, world):
    w = world
    first = _activate_first(Sessions, w, _rich_decision(w))
    tables = (*T6_CLEANUP, "longitudinal_assessment_runs", "longitudinal_assessment_inputs",
              "observation_dependencies", "observation_transfers", "observation_revalidations",
              "pedagogical_observations", "observation_evaluation_runs", "cognitive_events",
              "pedagogical_taxonomy_releases", "capability_taxonomy_memberships")
    before = {t: sorted(map(repr, _rows(engine, f"SELECT * FROM {t}"))) for t in tables}
    statements, record = _recording(engine)
    events = []
    listeners = {name: (lambda *a, _n=name: events.append(_n)) for name in ("commit", "rollback", "savepoint")}
    listeners["before_cursor_execute"] = record
    for name, fn in listeners.items():
        sa.event.listen(engine, name, fn)
    try:
        rich = svc._build_predecessor_decision_context(db, svc._run_row(db, first))
    finally:
        for name, fn in listeners.items():
            sa.event.remove(engine, name, fn)
    assert rich.inference_run_id == first
    assert statements and all(sql.startswith("SELECT") and " FOR " not in sql for sql, _ in statements)
    assert events == [] and not db.new and not db.dirty and not db.deleted
    db.commit()
    assert {t: sorted(map(repr, _rows(engine, f"SELECT * FROM {t}"))) for t in tables} == before


def test_pg_rich_context_query_count_is_constant(engine, Sessions, db, world):
    """Anti-N+1 : même nombre de requêtes pour un predecessor minimal et pour
    un predecessor à dix refs et trois tensions (même dossier T5) ; une
    seule requête par famille T6."""
    w = world
    _activate_first(Sessions, w)
    rich_context = _start(Sessions, w.t5, state_decision_version="state-2")
    rich = _complete(Sessions, rich_context.run_id, _rich_decision(w, cause="pedagogical_reinterpretation"))
    minimal_context = _start(Sessions, w.t5, state_decision_version="state-3")
    minimal = _complete(Sessions, minimal_context.run_id, _app_decision(w, cause="pedagogical_reinterpretation"))
    counts = {}
    for run_id in (rich, minimal):
        statements, record = _recording(engine)
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            svc._build_predecessor_decision_context(db, svc._run_row(db, run_id))
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        counts[run_id] = len(statements)
        for table in ("competency_inference_basis_refs", "competency_inference_tension_capabilities"):
            assert sum(f"FROM {table}" in sql for sql, _ in statements) == 1, table
    assert _count(engine, "competency_inference_basis_refs", "inference_run_id = :r", r=rich) == 10
    assert counts[rich] == counts[minimal]


def test_pg_historical_upstream_mutation_after_start_never_changes_the_candidate_input(engine, Sessions, db, world):
    """R1 fondé sur O1 ; réévaluation du même événement (O2, T3 de O1
    superseded) ; L2 avec O2 ; start R2. APRÈS le start, O1 (hors de L2) est
    invalidée : l'entrée courante de R2 reste valide et son contexte
    historique ne change pas. La vue T5-C live, elle, expose la mutation."""
    w = world
    o1 = w.app
    first = _activate_first(Sessions, w, _rich_decision(w))
    o2 = _t3(Sessions, w.tx.id, app(w.A), event_id=w.app_t3.event).id
    l2 = _start_t5(Sessions, w.tx.id)
    _complete_t5(Sessions, l2)
    started = _start(Sessions, l2)
    current = {o.observation_id for o in started.longitudinal_dossier.active_history.observations}
    assert o2 in current and o1 not in current
    assert started.predecessor.inference_run_id == first
    _invalidate(Sessions, o1)
    resumed = svc.get_inference_context(db, run_id=started.run_id)
    assert resumed.predecessor_decision_context == started.predecessor_decision_context
    assert resumed == started
    # Audit T5-C live : la mutation y est visible (séparation voulue).
    live = view.build_longitudinal_dossier(db, run_id=w.t5)
    live_o1 = {o.observation_id: o for o in live.active_history.observations}[o1]
    assert (live_o1.current_integrity_status, live_o1.current_evaluation_run_interpretation_status) == (
        "invalidated", "superseded")
    upstream = [lim for lim in live.limitations if lim.code == view.UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT]
    assert upstream and o1 in upstream[0].observation_ids


# --------------------------------------------------------------------------
# T6-B.2 : causalité de transition versionnée (input_fingerprint V2)
# --------------------------------------------------------------------------

def test_transition_causality_structures():
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(TransitionCausalityContext) == [
        "causality_schema_version", "new_user_event_ids", "integrity_changes", "reevaluations",
        "observation_delta", "relation_delta", "taxonomy_release_changed", "previous_taxonomy_release_id",
        "current_taxonomy_release_id", "t5_version_changes", "t6_specification_changes"]
    assert names(IntegrityChangeContext) == ["observation_id", "event_id", "evaluation_run_id",
                                             "capability_definition_ids"]
    assert names(ReevaluationContext) == [
        "event_id", "previous_evaluation_run_id", "replacement_evaluation_run_id", "replacement_re_evaluates_run_id",
        "previous_observation_ids", "current_observation_ids", "previous_capability_definition_ids",
        "current_capability_definition_ids", "changed_evaluation_specification_fields"]
    assert names(ObservationDeltaContext) == ["added_observation_ids", "removed_observation_ids",
                                              "retained_observation_ids", "added_event_ids", "removed_event_ids",
                                              "retained_event_ids"]
    assert names(RelationDeltaContext) == ["dependencies", "transfers", "revalidations"]
    assert names(RelationFamilyDeltaContext) == ["added", "removed", "retained"]
    assert names(VersionChangeContext) == ["field_name", "previous_value", "current_value"]
    context_fields = names(InferenceContext)
    assert context_fields.index("transition_causality") == context_fields.index("predecessor_decision_context") + 1
    assert "input_schema_version" in context_fields
    # Aucune cause choisie, aucun horodatage exposé.
    for cls in CAUSALITY_CLASSES:
        assert not any(n in ("transition_cause", "cause", "selected_cause") for n in names(cls)), cls
    # Aucun engine T6-C.
    for name in ("infer_competency", "StageBasisEngine", "ConfidenceProfileEngine", "TensionEngine",
                 "ValidationNeedEngine"):
        assert not hasattr(svc, name), name


def test_version_and_relation_deltas_are_pure_derivations():
    assert svc._version_changes({"b": "1", "a": "1", "c": None}, {"b": "2", "a": "1", "c": "x"}, ("b", "a", "c")) == (
        VersionChangeContext(field_name="b", previous_value="1", current_value="2"),
        VersionChangeContext(field_name="c", previous_value=None, current_value="x"))
    kept, gone, new = {"id": "k", "basis": {"é": [1]}}, {"id": "g"}, {"id": "n"}
    delta = svc._relation_family_delta([gone, kept], [dict(kept), new])
    assert (delta.added, delta.removed, delta.retained) == ((new,), (gone,), (svc._freeze(kept),))
    assert isinstance(delta.retained[0], MappingProxyType) and isinstance(delta.retained[0]["basis"]["é"], tuple)
    assert svc._id_delta([Y, X], [Z, Y]) == ((Z,), (X,), (Y,))


def _reevaluate(Sessions, release_id, prior, *specs, **overrides):
    """Réévaluation T3 DÉCLARÉE (re_evaluates_run_id = prior.run) du même
    CognitiveEvent, via les services T3-B / T4-B ; zéro spec = run completed
    sans observation."""
    with Sessions() as session:
        run = obs.start_evaluation_run(session, **t3_kwargs(
            prior.event, pedagogical_taxonomy_release_id=release_id, re_evaluates_run_id=prior.run, **overrides))
        ids = []
        for spec in specs:
            spec = dict(spec)
            caps = spec.pop("caps", ())
            observation = obs.add_observation(session, run_id=run.id, **_obs_kwargs(**spec))
            for membership_id in caps:
                tax.map_observation_capability(session, observation_id=observation.id,
                                               capability_membership_id=membership_id)
            ids.append(observation.id)
        obs.complete_evaluation_run(session, run_id=run.id, output_fingerprint="sha256:out")
        session.commit()
        return SimpleNamespace(run=run.id, ids=ids, id=ids[0] if ids else None, event=prior.event)


def _t3_of(engine, observation_id):
    row = _rows(engine, "SELECT o.evaluation_run_id, r.event_id FROM pedagogical_observations o JOIN"
                        " observation_evaluation_runs r ON r.id = o.evaluation_run_id WHERE o.id = :o",
                o=observation_id)[0]
    return SimpleNamespace(run=row["evaluation_run_id"], event=row["event_id"])


def _t5_now(Sessions, w, release_id=None, **overrides):
    run = _start_t5(Sessions, release_id or w.tx.id, **overrides)
    _complete_t5(Sessions, run)
    return run


def _ids(*values):
    return tuple(sorted(values, key=str))


def _as_legacy_v1(engine, db, run_id):
    """Réécrit un candidat vierge EXACTEMENT comme T6-B / T6-B.1 l'auraient
    créé (input_fingerprint V1 + inference_dedup_key correspondante) : aucun
    nouveau run V1 ne peut plus être créé par le service."""
    run = svc._run_row(db, run_id)
    parent = svc._parent_row(db, run.longitudinal_assessment_run_id, lock=False)
    predecessor = None if run.predecessor_inference_run_id is None else svc._run_row(
        db, run.predecessor_inference_run_id)
    v1 = svc._input_fingerprint_v1(
        svc._dossier_payload(parent, svc._relations(db, parent.id)),
        svc._predecessor_payload(None if predecessor is None else svc._snapshot(db, predecessor),
                                 None if predecessor is None else predecessor.unresolved_revision_context))
    db.rollback()
    _exec(engine, "UPDATE competency_inference_runs SET input_fingerprint = :f, inference_dedup_key = :k"
                  " WHERE id = :r", f=v1, k=svc._inference_dedup_key(v1, svc._specification(run)), r=run_id)
    return v1


def test_pg_first_inference_is_v2_without_causality(engine, Sessions, db, world):
    w = world
    context = _start(Sessions, w.t5)
    assert (context.input_schema_version, context.transition_causality, context.predecessor,
            context.predecessor_decision_context) == (2, None, None, None)
    parent = svc._parent_row(db, w.t5, lock=False)
    dossier = svc._dossier_payload(parent, svc._relations(db, w.t5))
    assert context.input_fingerprint == svc._input_fingerprint_v2(dossier, None, None) == _run(
        engine, context.run_id)["input_fingerprint"]
    assert context.input_fingerprint != svc._input_fingerprint_v1(dossier, None)
    assert context.inference_dedup_key == svc._inference_dedup_key(context.input_fingerprint,
                                                                  svc._specification(_run(engine, context.run_id)))
    assert svc.get_inference_context(db, run_id=context.run_id) == context
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=_app_decision(w))
    db.commit()
    assert _state(engine, context.run_id) == ("completed", "active")


def test_pg_legacy_v1_runs_stay_readable_and_never_get_retroactive_causality(engine, Sessions, db, world):
    """Candidat V1 (tel que créé avant T6-B.2) : lisible, empreinte V1
    reproduite, transition_causality None, chemin de validation legacy ;
    predecessor V1 accepté par un nouveau candidat V2."""
    w = world
    first = _start(Sessions, w.t5)
    v1 = _as_legacy_v1(engine, db, first.run_id)
    legacy = svc.get_inference_context(db, run_id=first.run_id)
    assert (legacy.input_schema_version, legacy.transition_causality, legacy.input_fingerprint) == (1, None, v1)
    db.rollback()
    _complete(Sessions, first.run_id, _app_decision(w))
    assert _run(engine, first.run_id)["input_fingerprint"] == v1

    # Predecessor V1 -> nouveau candidat : V2 uniquement.
    l2, (fresh,) = _new_t5(Sessions, w, sup(w.A))
    second = _start(Sessions, l2)
    assert second.input_schema_version == 2 and second.predecessor.input_fingerprint == v1
    assert second.predecessor_decision_context.inference_run_id == first.run_id
    assert second.transition_causality.new_user_event_ids == (_event_of(engine, fresh),)
    assert svc.get_inference_context(db, run_id=second.run_id) == second
    db.rollback()

    # Candidat legacy AVEC predecessor : aucune causalité non signée exposée,
    # validation T6-B inchangée (un nouvel événement dans le dossier suffit).
    _as_legacy_v1(engine, db, second.run_id)
    legacy_second = svc.get_inference_context(db, run_id=second.run_id)
    assert (legacy_second.input_schema_version, legacy_second.transition_causality) == (1, None)
    assert legacy_second.predecessor_decision_context == second.predecessor_decision_context
    db.rollback()
    svc.complete_competency_inference(db, run_id=second.run_id, decision=_app_decision(w, cause="new_user_evidence"))
    db.commit()

    # Predecessor V1 (lui-même successeur d'un V1) -> candidat V2.
    third = _start(Sessions, l2, state_decision_version="state-2")
    assert third.input_schema_version == 2 and third.predecessor.inference_run_id == second.run_id
    assert third.transition_causality.t6_specification_changes == (VersionChangeContext(
        field_name="state_decision_version", previous_value="state_decision-1", current_value="state-2"),)
    assert svc.get_inference_context(db, run_id=third.run_id) == third


def test_pg_true_new_event_integrity_change_and_observation_delta(engine, Sessions, db, world):
    w = world
    _activate_first(Sessions, w)
    _invalidate(Sessions, w.k)
    l2, (fresh,) = _new_t5(Sessions, w, sup(w.A))
    context = _start(Sessions, l2)
    c = context.transition_causality
    fresh_event, k = _event_of(engine, fresh), _t3_of(engine, w.k)
    assert _closed_at(engine, fresh_event) > _started_at(engine, w.t5)
    assert c.causality_schema_version == 1
    assert c.new_user_event_ids == (fresh_event,)
    assert c.integrity_changes == (IntegrityChangeContext(observation_id=w.k, event_id=k.event,
                                                          evaluation_run_id=k.run, capability_definition_ids=(w.dA,)),)
    assert c.reevaluations == () and c.t5_version_changes == () and c.t6_specification_changes == ()
    assert not c.taxonomy_release_changed and c.previous_taxonomy_release_id == c.current_taxonomy_release_id
    retained = [w.disc, w.comp, w.app, w.app_b, w.kB, w.k_only]
    assert c.observation_delta == ObservationDeltaContext(
        added_observation_ids=(fresh,), removed_observation_ids=(w.k,), retained_observation_ids=_ids(*retained),
        added_event_ids=(fresh_event,), removed_event_ids=(k.event,),
        retained_event_ids=_ids(*(_event_of(engine, o) for o in retained)))
    # Mêmes ensembles que les deux dossiers exposés à T6-C.
    history = context.predecessor_decision_context.historical_longitudinal_context
    delta = c.observation_delta
    assert {o.observation_id for o in history.observations} == set(delta.removed_observation_ids) | set(
        delta.retained_observation_ids)
    assert {o.observation_id for o in context.longitudinal_dossier.active_history.observations} == set(
        delta.added_observation_ids) | set(delta.retained_observation_ids)
    # L2 n'a aucune relation : les trois relations de L1 sont sorties.
    for family in ("dependencies", "transfers", "revalidations"):
        assert (getattr(c.relation_delta, family).added, getattr(c.relation_delta, family).retained) == ((), ())
        assert len(getattr(c.relation_delta, family).removed) == 1
    _walk(context)


def test_pg_old_event_reevaluated_is_never_new_user_evidence(engine, Sessions, db, world):
    """E1 finalisé avant L1 ; R1 -> O1 ; réévaluation déclarée R2 -> O2
    (autre évaluateur) ; L2 : réévaluation observation -> observation."""
    w = world
    _activate_first(Sessions, w)
    o2 = _reevaluate(Sessions, w.tx.id, w.app_t3, app(w.A, w.B), evaluator_version="evaluator-2")
    l2 = _t5_now(Sessions, w)
    context = _start(Sessions, l2)
    c = context.transition_causality
    assert _closed_at(engine, w.app_t3.event) <= _started_at(engine, w.t5)
    assert c.new_user_event_ids == () and c.integrity_changes == ()
    assert c.reevaluations == (ReevaluationContext(
        event_id=w.app_t3.event, previous_evaluation_run_id=w.app_t3.run, replacement_evaluation_run_id=o2.run,
        replacement_re_evaluates_run_id=w.app_t3.run, previous_observation_ids=(w.app,),
        current_observation_ids=(o2.id,), previous_capability_definition_ids=(w.dA,),
        current_capability_definition_ids=_ids(w.dA, w.dB),
        changed_evaluation_specification_fields=("evaluator_version",)),)
    assert c.observation_delta.retained_event_ids.count(w.app_t3.event) == 1
    reeval = decision("application", refs=[pos("application", o2.id)], cause="new_user_evidence")
    with pytest.raises(InvalidInferenceDecision, match="new_user_evidence sans aucun CognitiveEvent"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=reeval)
    db.rollback()
    with pytest.raises(InvalidInferenceDecision, match="sans aucune observation du dossier du predecessor invalidée"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=dataclasses.replace(
            reeval, transition_cause="evidence_integrity_change"))
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id, decision=dataclasses.replace(
        reeval, transition_cause="pedagogical_reinterpretation"))
    db.commit()


def test_pg_reevaluation_from_zero_to_an_observation(engine, Sessions, db):
    """E_old finalisé avant L1, évalué (C8 seulement) : absent du dossier C7.
    Réévalué ensuite (C7) : zéro -> observation, jamais une nouvelle
    démonstration. E_late : finalisé avant L1 mais évalué pour la première
    fois après : aucune interprétation -> une interprétation."""
    tx = _taxonomy(Sessions, ("C7_A", "C8_A"))
    A = tx.m["C7_A"]
    old_event, late_event = _finalized_event(Sessions), _finalized_event(Sessions)
    c8 = _t3(Sessions, tx.id, sup(tx.m["C8_A"], competency_code="C8"), event_id=old_event)
    base = _t3(Sessions, tx.id, sup(A))
    l1 = _start_t5(Sessions, tx.id)
    _complete_t5(Sessions, l1)
    r1 = _start(Sessions, l1)
    _complete(Sessions, r1.run_id, decision("comprehension", refs=[pos("comprehension", base.id)]))
    o2 = _reevaluate(Sessions, tx.id, c8, app(A))
    late = _t3(Sessions, tx.id, sup(A), event_id=late_event)
    l2 = _start_t5(Sessions, tx.id)
    _complete_t5(Sessions, l2)
    c = _start(Sessions, l2).transition_causality
    assert c.new_user_event_ids == ()
    expected = sorted([
        ReevaluationContext(event_id=old_event, previous_evaluation_run_id=c8.run, replacement_evaluation_run_id=o2.run,
                            replacement_re_evaluates_run_id=c8.run, previous_observation_ids=(),
                            current_observation_ids=(o2.id,), previous_capability_definition_ids=(),
                            current_capability_definition_ids=(tx.d["C7_A"],),
                            changed_evaluation_specification_fields=()),
        ReevaluationContext(event_id=late_event, previous_evaluation_run_id=None,
                            replacement_evaluation_run_id=late.run, replacement_re_evaluates_run_id=None,
                            previous_observation_ids=(), current_observation_ids=(late.id,),
                            previous_capability_definition_ids=(), current_capability_definition_ids=(tx.d["C7_A"],),
                            changed_evaluation_specification_fields=()),
    ], key=lambda r: str(r.event_id))
    assert c.reevaluations == tuple(expected)
    assert c.observation_delta.added_event_ids == _ids(old_event, late_event)


def test_pg_reevaluation_from_an_observation_to_zero(engine, Sessions, db, world):
    """R1 -> O1 (C7) ; réévaluation R2 -> zéro observation : E1 sort de L2,
    la réévaluation reste détectée (jamais déduite du seul dossier courant)."""
    w = world
    _activate_first(Sessions, w)
    zero = _reevaluate(Sessions, w.tx.id, w.disc_t3)
    l2 = _t5_now(Sessions, w)
    context = _start(Sessions, l2)
    c = context.transition_causality
    assert w.disc_t3.event not in {o.event_id for o in context.longitudinal_dossier.active_history.observations}
    assert c.reevaluations == (ReevaluationContext(
        event_id=w.disc_t3.event, previous_evaluation_run_id=w.disc_t3.run, replacement_evaluation_run_id=zero.run,
        replacement_re_evaluates_run_id=w.disc_t3.run, previous_observation_ids=(w.disc,),
        current_observation_ids=(), previous_capability_definition_ids=_ids(w.dA, w.dB),
        current_capability_definition_ids=(), changed_evaluation_specification_fields=()),)
    assert c.integrity_changes == () and c.new_user_event_ids == ()
    assert c.observation_delta.removed_observation_ids == (w.disc,)
    with pytest.raises(InvalidInferenceDecision, match="sans aucune observation du dossier du predecessor invalidée"):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="evidence_integrity_change"))
    db.rollback()
    svc.complete_competency_inference(db, run_id=context.run_id,
                                      decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.commit()


def test_pg_post_snapshot_mutations_never_change_the_causality(engine, Sessions, db, world):
    """start == get == complete malgré, APRÈS la capture de L2, une
    invalidation et une réévaluation historiques hors de L2 (L2 reste
    courant) ; l'intégrité de la fenêtre (kB) reste, celle d'après (O1)
    n'entre jamais."""
    w = world
    _activate_first(Sessions, w)
    o2 = _reevaluate(Sessions, w.tx.id, w.app_t3, app(w.A))
    _invalidate(Sessions, w.kB)
    l2 = _t5_now(Sessions, w)
    started = _start(Sessions, l2)
    c = started.transition_causality
    assert [i.observation_id for i in c.integrity_changes] == [w.kB]
    assert [r.event_id for r in c.reevaluations] == [w.app_t3.event]
    _invalidate(Sessions, w.app)  # O1 : remplacée par O2, absente de L2
    _reevaluate(Sessions, w.tx.id, _t3_of(engine, w.kB), contra(w.B))  # ancien événement absent de L2
    resumed = svc.get_inference_context(db, run_id=started.run_id)
    assert resumed == started
    assert (resumed.input_schema_version, resumed.transition_causality, resumed.input_fingerprint,
            resumed.inference_dedup_key) == (2, c, started.input_fingerprint, started.inference_dedup_key)
    db.rollback()
    svc.complete_competency_inference(db, run_id=started.run_id, decision=_app_decision(
        w, refs=[pos("application", o2.id)], cause="evidence_integrity_change"))
    db.commit()
    row = _run(engine, started.run_id)
    assert (row["input_fingerprint"], row["execution_status"], row["interpretation_status"]) == (
        started.input_fingerprint, "completed", "active")


def test_pg_an_invalidation_after_the_current_snapshot_never_supports_an_integrity_cause(engine, Sessions, db,
                                                                                          world):
    w = world
    _activate_first(Sessions, w)
    o2 = _reevaluate(Sessions, w.tx.id, w.app_t3, app(w.A))
    l2 = _t5_now(Sessions, w)
    started = _start(Sessions, l2)
    _invalidate(Sessions, w.app)
    assert svc.get_inference_context(db, run_id=started.run_id).transition_causality.integrity_changes == ()
    db.rollback()
    d = _app_decision(w, refs=[pos("application", o2.id)], cause="evidence_integrity_change")
    with pytest.raises(InvalidInferenceDecision, match="sans aucune observation du dossier du predecessor invalidée"):
        svc.complete_competency_inference(db, run_id=started.run_id, decision=d)
    db.rollback()
    svc.complete_competency_inference(db, run_id=started.run_id, decision=dataclasses.replace(
        d, transition_cause="pedagogical_reinterpretation"))
    db.commit()


def test_pg_multiple_causal_families_coexist_and_no_cause_is_chosen(engine, Sessions, db, world):
    """Même fenêtre : nouvel événement + observation invalidée + ancien
    événement réévalué. Les trois familles coexistent ; chaque cause passe
    ses gardes structurelles selon la décision ; T6-B n'en choisit aucune."""
    w = world
    _activate_first(Sessions, w)
    _invalidate(Sessions, w.k)
    o2 = _reevaluate(Sessions, w.tx.id, w.app_t3, app(w.A))
    l2, (fresh,) = _new_t5(Sessions, w, sup(w.A))
    context = _start(Sessions, l2)
    c = context.transition_causality
    assert c.new_user_event_ids == (_event_of(engine, fresh),)
    assert [i.observation_id for i in c.integrity_changes] == [w.k]
    assert [r.event_id for r in c.reevaluations] == [w.app_t3.event]
    assert not hasattr(c, "transition_cause")
    _walk(context)
    refs = [pos("application", o2.id), ref("transition", "observation", fresh)]
    for cause in ("new_user_evidence", "evidence_integrity_change", "pedagogical_reinterpretation"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=decision(
            "application", refs=refs, cause=cause))
        assert _run(engine, context.run_id)["transition_cause"] is None  # rien de commité
        db.rollback()


def test_pg_relation_delta_is_semantic(engine, Sessions, db, world):
    """L2 = mêmes observations ; même dépendance recréée (autre UUID) =>
    conservée ; transfert au basis différent => sorti + entré ; revalidation
    non recréée => sortie. Aucune comparaison d'UUID de ligne."""
    w = world
    _activate_first(Sessions, w)
    l2 = _start_t5(Sessions, w.tx.id)
    with Sessions() as s:
        dep = t5.add_dependency(s, run_id=l2, target_observation_id=w.comp, source_kind="observation",
                                source_observation_id=w.disc, dependency_type="partially_dependent",
                                scope_mode="localized", dependency_basis={"why": "reprend la décomposition"},
                                capability_membership_ids=[w.B]).id
        t5.add_transfer(s, run_id=l2, source_observation_id=w.comp, target_observation_id=w.app,
                        scope_mode="localized", transfer_basis={"adaptation": "autre justification"},
                        capability_membership_ids=[w.A])
        s.commit()
    _complete_t5(Sessions, l2)
    assert dep != w.dep
    context = _start(Sessions, l2)
    delta = context.transition_causality.relation_delta
    before, after = svc._relations(db, w.t5).payload, svc._relations(db, l2).payload
    freeze = svc._freeze
    assert delta.dependencies == RelationFamilyDeltaContext(added=(), removed=(), retained=(
        freeze(after["dependencies"][0]),))
    assert after["dependencies"] == before["dependencies"]
    assert delta.transfers == RelationFamilyDeltaContext(added=(freeze(after["transfers"][0]),),
                                                         removed=(freeze(before["transfers"][0]),), retained=())
    assert delta.revalidations == RelationFamilyDeltaContext(added=(), removed=(freeze(before["revalidations"][0]),),
                                                             retained=())
    c = context.transition_causality
    assert c.observation_delta.added_observation_ids == () == c.observation_delta.removed_observation_ids
    _walk(c)
    # Depuis T6-C3 : un changement SÉMANTIQUE des relations T5 (ici transfert
    # entré / sorti, revalidation sortie), sans autre fait, supporte
    # structurellement pedagogical_reinterpretation (jamais new_user_evidence).
    assert (c.new_user_event_ids, c.integrity_changes, c.reevaluations, c.t5_version_changes,
            c.t6_specification_changes, c.taxonomy_release_changed) == ((), (), (), (), (), False)
    with pytest.raises(InvalidInferenceDecision, match="new_user_evidence"):
        svc.complete_competency_inference(db, run_id=context.run_id,
                                          decision=_app_decision(w, cause="new_user_evidence"))
    svc.complete_competency_inference(db, run_id=context.run_id,
                                      decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.commit()


def _relation_only_causality(**families):
    empty = RelationFamilyDeltaContext(added=(), removed=(), retained=())
    release = uuid.uuid5(uuid.NAMESPACE_URL, "oryx:t6c3:relation-only")
    return svc.TransitionCausalityContext(
        causality_schema_version=1, new_user_event_ids=(), integrity_changes=(), reevaluations=(),
        observation_delta=ObservationDeltaContext(added_observation_ids=(), removed_observation_ids=(),
                                                  retained_observation_ids=(), added_event_ids=(),
                                                  removed_event_ids=(), retained_event_ids=()),
        relation_delta=RelationDeltaContext(**{f: families.get(f, empty)
                                               for f in ("dependencies", "transfers", "revalidations")}),
        taxonomy_release_changed=False, previous_taxonomy_release_id=release, current_taxonomy_release_id=release,
        t5_version_changes=(), t6_specification_changes=())


@pytest.mark.parametrize("family", ["dependencies", "transfers", "revalidations"])
@pytest.mark.parametrize("change", ["added", "removed", "retained"])
def test_relation_delta_alone_supports_reinterpretation_but_retained_never_counts(family, change):
    """_check_transition_causes (V2) : added / removed d'une famille de
    relations T5 suffit à pedagogical_reinterpretation ; retained seul n'est
    jamais un changement ; aucune autre cause n'en devient possible."""
    item = svc._freeze({"source_observation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "s")),
                        "target_observation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "t")),
                        "scope_fingerprint": "sha256:x"})
    delta = RelationFamilyDeltaContext(**{name: (item,) if name == change else ()
                                          for name in ("added", "removed", "retained")})
    inputs = SimpleNamespace(transition_causality=_relation_only_causality(**{family: delta}), relations=None)
    predecessor = SimpleNamespace(id=uuid.uuid5(uuid.NAMESPACE_URL, "p"), unresolved_revision_context=None,
                                  tension_state="none", transition=None)

    def check(cause):
        decision = svc._Decision("application", cause, None, [], "résumé", {}, (), ())
        svc._check_transition_causes(inputs, predecessor, decision, "maintained", {})

    if change == "retained":
        with pytest.raises(InvalidInferenceDecision, match="aucune évolution contrôlée"):
            check("pedagogical_reinterpretation")
    else:
        check("pedagogical_reinterpretation")
    for cause in ("new_user_evidence", "evidence_integrity_change"):
        with pytest.raises(InvalidInferenceDecision):
            check(cause)


@pytest.mark.parametrize("field, value", [("dependency_version", "dependency-2"), ("transfer_version", "transfer-2"),
                                          ("revalidation_version", "revalidation-2"),
                                          ("relation_schema_version", "relations-2")])
def test_pg_t5_version_change_is_exposed_alone(engine, Sessions, db, world, field, value):
    w = world
    _activate_first(Sessions, w)
    context = _start(Sessions, _t5_now(Sessions, w, **{field: value}))
    c = context.transition_causality
    default = {"dependency_version": "dependency-1", "transfer_version": "transfer-1",
               "revalidation_version": "revalidation-1", "relation_schema_version": "relations-1"}
    assert c.t5_version_changes == (VersionChangeContext(field_name=field, previous_value=default[field],
                                                         current_value=value),)
    assert (c.taxonomy_release_changed, c.t6_specification_changes, c.reevaluations, c.new_user_event_ids,
            c.integrity_changes) == (False, (), (), (), ())
    svc.complete_competency_inference(db, run_id=context.run_id,
                                      decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.commit()


def test_pg_taxonomy_release_change_is_exposed_alone(engine, Sessions, db, world):
    w = world
    _activate_first(Sessions, w)
    release = _taxonomy(Sessions, {code: w.tx.d[code] for code in ("C7_A", "C7_B", "C7_C", "C8_A")})
    context = _start(Sessions, _t5_now(Sessions, w, release_id=release.id))
    c = context.transition_causality
    assert (c.taxonomy_release_changed, c.previous_taxonomy_release_id, c.current_taxonomy_release_id) == (
        True, w.tx.id, release.id)
    assert (c.t5_version_changes, c.t6_specification_changes, c.reevaluations, c.new_user_event_ids,
            c.integrity_changes) == ((), (), (), (), ())
    svc.complete_competency_inference(db, run_id=context.run_id,
                                      decision=_app_decision(w, cause="pedagogical_reinterpretation"))
    db.commit()


@pytest.mark.parametrize("field", [*VERSIONS, "model_id", "prompt_spec_version"])
def test_pg_t6_specification_change_is_exposed_alone(engine, Sessions, db, world, field):
    w = world
    _activate_first(Sessions, w)
    previous = svc._specification(_start_kwargs())[field] if field in VERSIONS else None
    context = _start(Sessions, w.t5, **{field: "autre-2"})
    c = context.transition_causality
    assert c.t6_specification_changes == (VersionChangeContext(field_name=field, previous_value=previous,
                                                               current_value="autre-2"),)
    assert (c.t5_version_changes, c.taxonomy_release_changed, c.reevaluations, c.new_user_event_ids,
            c.integrity_changes, c.observation_delta.added_observation_ids,
            c.observation_delta.removed_observation_ids) == ((), False, (), (), (), (), ())
    assert svc.get_inference_context(db, run_id=context.run_id).transition_causality == c


def test_pg_v2_causes_are_read_from_the_frozen_causality_never_live(engine, Sessions, db, world, monkeypatch):
    """complete d'un candidat V2 : aucune relecture live des causes (ni le
    chemin legacy ni _new_user_events) ; anti-oscillation sur les
    new_user_event_ids figés."""
    w = world
    _, revise, (k_new,) = _second(Sessions, w, new=[contra(w.A)])
    _complete(Sessions, revise.run_id, decision(
        "comprehension", refs=[pos("comprehension", w.comp), ref("transition", "observation", k_new),
                               ref("tension", "observation", k_new, tension_key="levier")],
        cause="new_user_evidence", context={"motif": "levier"}, tensions=[tension("levier", stage="application")]))
    l3, (fresh,) = _new_t5(Sessions, w, app(w.A))
    rebound = _start(Sessions, l3)
    assert rebound.transition_causality.new_user_event_ids == (_event_of(engine, fresh),)

    def forbidden(*args, **kwargs):
        raise AssertionError("relecture live des causes pour un candidat V2")

    monkeypatch.setattr(svc, "_check_legacy_causes", forbidden)
    monkeypatch.setattr(svc, "_new_user_events", forbidden)
    # Ref vers le nouvel événement mais seulement en confidence : la
    # nouvelle démonstration est citée (cause recevable), pas la remontée.
    only_confidence = decision("application", refs=[
        pos("application", w.app), ref("transition", "observation", w.app),
        ref("confidence", "observation", fresh, claim_stage="application", confidence_dimension="consistency")],
        cause="new_user_evidence")
    with pytest.raises(InvalidInferenceDecision, match="remontée après une révision non résolue"):
        svc.complete_competency_inference(db, run_id=rebound.run_id, decision=only_confidence)
    db.rollback()
    svc.complete_competency_inference(db, run_id=rebound.run_id, decision=dataclasses.replace(
        only_confidence, basis_refs=(pos("application", w.app), ref("transition", "observation", fresh))))
    db.commit()
    assert _run(engine, rebound.run_id)["transition"] == "upgraded"


def test_pg_an_impossible_reevaluation_chain_is_refused(engine, Sessions, db, world):
    w = world
    first = _activate_first(Sessions, w)
    o2 = _reevaluate(Sessions, w.tx.id, w.app_t3, app(w.A))
    l2 = _t5_now(Sessions, w)
    _exec(engine, "UPDATE observation_evaluation_runs SET re_evaluates_run_id = :other WHERE id = :r",
          other=w.disc_t3.run, r=o2.run)
    with pytest.raises(InvalidInferenceState, match="hors de l'événement"):
        svc.start_competency_inference(db, **_start_kwargs(l2))
    db.rollback()
    assert _count(engine, "competency_inference_runs") == 1 and _state(engine, first) == ("completed", "active")


def test_pg_causal_reconstruction_query_count_is_constant(engine, Sessions, db, world):
    """Anti-N+1 : trois SELECT pour les faits causaux, quel que soit le
    nombre d'observations, d'événements, de réévaluations ou
    d'invalidations."""
    w = world
    _activate_first(Sessions, w)
    small = _t5_now(Sessions, w)
    for observation in (w.k, w.kB):
        _invalidate(Sessions, observation)
    for prior in (w.app_t3, w.disc_t3):
        _reevaluate(Sessions, w.tx.id, prior, app(w.A))
    _t3(Sessions, w.tx.id, sup(w.A), sup(w.B))
    _t3(Sessions, w.tx.id, contra(w.C))
    large = _t5_now(Sessions, w)
    counts = {}
    previous = svc._parent_row(db, w.t5, lock=False)
    for current in (small, large):
        parent = svc._parent_row(db, current, lock=False)
        statements, record = _recording(engine)
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            facts = svc._causal_facts(db, previous, parent)
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        counts[current] = len(statements)
        assert all(sql.startswith("SELECT") and " FOR " not in sql for sql, _ in statements)
    assert len(facts.reevaluations) == 2 and len(facts.integrity_changes) == 2 and len(facts.new_user_event_ids) == 2
    assert counts[small] == counts[large] == 3


# --------------------------------------------------------------------------
# T6-C0 : résolution taxonomique courante (current_taxonomy_context)
# --------------------------------------------------------------------------

def test_t6c0_changes_only_the_inference_context_surface():
    """Diff AST contre le merge de T6-B.2 : trois ajouts (deux structures,
    un helper) ; seuls InferenceContext, _context, start et get changent.
    Empreintes V1 / V2, payload causal, payload predecessor, dédup, output,
    _Inputs, _verified_inputs, validation, verrous, complete, fail, cache et
    lecture validée strictement identiques."""
    base = subprocess.run(["git", "show", f"{T6B2_MERGE}:core/inference_service.py"], cwd=REPO_ROOT,
                          capture_output=True, text=True)
    if base.returncode != 0:
        pytest.skip("historique git indisponible")
    before, after = _top_level(base.stdout), _top_level(SERVICE_PATH.read_text(encoding="utf-8"))
    assert set(after) - set(before) == T6C0_ADDITIONS | STEP64C_ADDITIONS
    assert set(before) - set(after) == set()
    for name in set(before) - CHANGED_BY_T6C0 - CHANGED_BY_T6C3:
        assert after[name] == before[name], name
    for name in CHANGED_BY_T6C0 | CHANGED_BY_T6C3:
        assert after[name] != before[name], name
    assert "relation_delta" in after["_check_transition_causes"]
    for name in ("_input_fingerprint_v1", "_input_fingerprint_v2", "_causality_fingerprint_payload",
                 "_predecessor_payload", "_dossier_payload", "_detect_input_identity", "_inference_dedup_key",
                 "_output_fingerprint", "_Inputs", "_verified_inputs", "_membership_definitions",
                 "_check_decision", "_build_transition_causality", "_build_predecessor_decision_context",
                 "complete_competency_inference", "TensionDecision", "SPECIFICATION_FIELDS"):
        assert name not in CHANGED_BY_T6C0 and after[name] == before[name], name


def test_t6c0_leaves_every_schema_version_unchanged():
    assert (svc.INPUT_SCHEMA_VERSION, svc.LEGACY_INPUT_SCHEMA_VERSION, svc.TRANSITION_CAUSALITY_SCHEMA_VERSION,
            svc.DEDUP_SCHEMA_VERSION, svc.OUTPUT_SCHEMA_VERSION) == (2, 1, 1, 1, 1)


def test_current_taxonomy_structures_expose_identity_only():
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(CurrentTaxonomyCapability) == ["membership_id", "definition_id", "capability_code",
                                                "semantic_revision", "label"]
    assert names(CurrentTaxonomyContext) == ["release_id", "competency_code", "capabilities"]
    context_fields = names(InferenceContext)
    assert context_fields.index("current_taxonomy_context") == context_fields.index("transition_causality") + 1
    assert dataclasses.fields(InferenceContext)[context_fields.index("current_taxonomy_context")].type in (
        "CurrentTaxonomyContext", CurrentTaxonomyContext)
    # Ni texte de définition, ni mapping_guidance (T3 / T4), ni propriété de
    # release relue au présent, ni valeur pédagogique.
    for cls in TAXONOMY_CONTEXT_CLASSES:
        for field in ("definition", "mapping_guidance", "created_at", "activated_at", "status", "release_status",
                      "weight", "priority", "rank", "order", "score", "level", "progression", "acquisition_status",
                      "stage", "confidence"):
            assert field not in names(cls), (cls, field)
    # TensionDecision inchangée : memberships, jamais de definition_ids.
    assert names(TensionDecision)[-1] == "capability_membership_ids"
    assert "capability_definition_ids" not in names(TensionDecision)


def test_current_taxonomy_context_uses_only_the_t4_read_api():
    """Aucune mutation T4 ni transaction : seule la lecture
    get_release_capabilities (et son exception de base) est importée."""
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    imported = [alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
                and node.module == "core.taxonomy_service" for alias in node.names]
    assert sorted(imported) == ["TaxonomyServiceError", "get_release_capabilities"]
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).split("\n")
    for forbidden in ("create_candidate_release", "create_capability_definition", "attach_capability_to_release",
                      "activate_release", "map_observation_capability", "mapping_guidance"):
        assert forbidden not in tokens, forbidden
    helper = _function(SERVICE_PATH.read_text(encoding="utf-8"), "_build_current_taxonomy_context")
    on_db = {node.func.attr for node in ast.walk(helper) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) == "db"}
    assert on_db == set()  # la session n'est passée qu'à get_release_capabilities
    calls = {node.func.attr for node in ast.walk(helper)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & {"add_all", "flush", "delete", "commit", "rollback", "begin_nested", "merge",
                        "with_for_update"}
    # complete ne construit pas le contexte taxonomique (lectures inutiles).
    complete = _function(SERVICE_PATH.read_text(encoding="utf-8"), "complete_competency_inference")
    assert "_build_current_taxonomy_context" not in ast.unparse(complete)
    for name in ("start_competency_inference", "get_inference_context"):
        assert ast.unparse(_function(SERVICE_PATH.read_text(encoding="utf-8"), name)).count(
            "_build_current_taxonomy_context(") == 1, name


def _fake_rows(*specs, release=X):
    """(membership, définition) factices, dans l'ordre fourni ; spec = (membership,
    définition, code, compétence[, release du membership[, définition du
    membership]])."""
    rows = []
    for membership_id, definition_id, code, competency, *rest in specs:
        mapped_release = rest[0] if rest else release
        mapped_definition = rest[1] if len(rest) > 1 else definition_id
        rows.append((SimpleNamespace(id=membership_id, taxonomy_release_id=mapped_release,
                                     capability_definition_id=mapped_definition),
                     SimpleNamespace(id=definition_id, capability_code=code, competency_code=competency,
                                     semantic_revision=1, label=f"label {code}")))
    return rows


TM1, TM2, TM3, TD1, TD2, TD3 = (uuid.UUID(int=0x200 + i) for i in range(6))


@pytest.mark.parametrize("rows, match", [
    (_fake_rows((TM1, TD1, "C8_A", "C8"), (TM2, TD2, "C8_C", "C8", Y)), "incohérent avec la release"),
    (_fake_rows((TM1, TD1, "C8_A", "C8"), (TM2, TD2, "C8_C", "C8", X, TD3)), "incohérent avec la release"),
    (_fake_rows((TM1, TD1, "C8_A", "C8"), (TM1, TD2, "C8_C", "C8")), "membership .* en double"),
    (_fake_rows((TM1, TD1, "C8_A", "C8"), (TM2, TD1, "C8_A", "C8")), "definition .* en double"),
    (_fake_rows((TM1, TD1, "C8_C", "C8"), (TM2, TD2, "C8_C", "C8")), "capability_code C8_C en double"),
])
def test_current_taxonomy_context_refuses_incoherent_rows(monkeypatch, rows, match):
    """Jamais une ligne choisie arbitrairement : membership hors release ou
    vers une autre définition, doublons => InvalidInferenceState."""
    monkeypatch.setattr(svc, "get_release_capabilities", lambda db, *, release_id: rows)
    with pytest.raises(InvalidInferenceState, match=match):
        svc._build_current_taxonomy_context(_NoDB(), release_id=X, competency_code="C8")


def test_current_taxonomy_context_filters_on_the_persisted_competency_and_keeps_t4_order(monkeypatch):
    """Filtre sur definition.competency_code (la définition persistée est la
    source, jamais le préfixe du code) ; ordre T4-B conservé ; doublons
    d'autres compétences hors du contexte."""
    rows = _fake_rows((TM1, TD1, "C8_B", "C8"), (TM2, TD2, "C8_A", "C7"), (TM3, TD3, "C7_A", "C8"),
                      (uuid.uuid4(), uuid.uuid4(), "C9_A", "C9"), (uuid.uuid4(), uuid.uuid4(), "C9_A", "C9"))
    monkeypatch.setattr(svc, "get_release_capabilities", lambda db, *, release_id: rows)
    context = svc._build_current_taxonomy_context(_NoDB(), release_id=X, competency_code="C8")
    assert context == CurrentTaxonomyContext(release_id=X, competency_code="C8", capabilities=(
        CurrentTaxonomyCapability(membership_id=TM1, definition_id=TD1, capability_code="C8_B", semantic_revision=1,
                                  label="label C8_B"),
        CurrentTaxonomyCapability(membership_id=TM3, definition_id=TD3, capability_code="C7_A", semantic_revision=1,
                                  label="label C7_A")))


def test_current_taxonomy_context_translates_a_missing_release(monkeypatch):
    def missing(db, *, release_id):
        raise tax.TaxonomyReleaseNotFound(str(release_id))

    monkeypatch.setattr(svc, "get_release_capabilities", missing)
    with pytest.raises(InvalidInferenceState, match="illisible"):
        svc._build_current_taxonomy_context(_NoDB(), release_id=X, competency_code="C8")


def _capability_rows(engine, release_id):
    return {row["capability_code"]: row for row in _rows(
        engine, "SELECT m.id AS membership_id, d.id AS definition_id, d.capability_code, d.semantic_revision,"
                " d.label, d.competency_code FROM capability_taxonomy_memberships m JOIN core_capability_definitions d"
                " ON d.id = m.capability_definition_id WHERE m.taxonomy_release_id = :r", r=release_id)}


def _expected_capability(row):
    return CurrentTaxonomyCapability(membership_id=row["membership_id"], definition_id=row["definition_id"],
                                     capability_code=row["capability_code"],
                                     semantic_revision=row["semantic_revision"], label=row["label"])


def _assert_deeply_immutable_without_orm(value, path="context"):
    from core import models as orm
    mapped = tuple(o for o in vars(orm).values() if isinstance(o, type) and hasattr(o, "__table__"))
    assert not isinstance(value, mapped), path
    if dataclasses.is_dataclass(value):
        assert type(value).__dataclass_params__.frozen, path
        for f in dataclasses.fields(value):
            _assert_deeply_immutable_without_orm(getattr(value, f.name), f"{path}.{f.name}")
    elif isinstance(value, tuple):
        for i, item in enumerate(value):
            _assert_deeply_immutable_without_orm(item, f"{path}[{i}]")
    else:
        assert type(value) in (uuid.UUID, str, int), (path, type(value))


def test_pg_first_inference_exposes_the_current_taxonomy_of_its_competency(engine, Sessions, db, world):
    """Release C7_A/B/C + C8_A, run C7 : exactement les trois memberships C7
    tels que persistés (membership -> définition), ordre naturel ; start ==
    get ; aucun objet ORM, immuable en profondeur."""
    w = world
    context = _start(Sessions, w.t5)
    taxonomy = context.current_taxonomy_context
    rows = _capability_rows(engine, w.tx.id)
    assert taxonomy == CurrentTaxonomyContext(
        release_id=w.tx.id, competency_code="C7",
        capabilities=tuple(_expected_capability(rows[code]) for code in ("C7_A", "C7_B", "C7_C")))
    assert taxonomy.release_id == context.pedagogical_taxonomy_release_id == \
        context.longitudinal_dossier.pedagogical_taxonomy_release_id
    assert taxonomy.competency_code == context.competency_code == context.longitudinal_dossier.competency_code
    assert [(c.membership_id, c.definition_id) for c in taxonomy.capabilities] == [
        (w.tx.m[code], w.tx.d[code]) for code in ("C7_A", "C7_B", "C7_C")]
    assert w.tx.m["C8_A"] not in {c.membership_id for c in taxonomy.capabilities}
    resumed = svc.get_inference_context(db, run_id=context.run_id)
    assert resumed.current_taxonomy_context == taxonomy and resumed == context
    _assert_deeply_immutable_without_orm(taxonomy)
    assert isinstance(taxonomy.capabilities, tuple)
    for target, field in ((taxonomy, "release_id"), (taxonomy, "competency_code"), (taxonomy, "capabilities"),
                          (taxonomy.capabilities[0], "membership_id"), (taxonomy.capabilities[0], "definition_id"),
                          (taxonomy.capabilities[0], "semantic_revision")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(target, field, None)
    with pytest.raises(TypeError):
        taxonomy.capabilities[0] = None


def test_pg_current_taxonomy_filters_the_competency_in_natural_order(engine, Sessions, db):
    """Release multi-compétences : seules les capacités de la compétence
    demandée, ordre naturel T4-B (C2 avant C10, jamais l'ordre lexical) ;
    compétence absente => capabilities vide (aucune cardinalité imposée) ;
    release inconnue => InvalidInferenceState, jamais un contexte vide."""
    codes = ("C10_D", "C1_A", "C2_A", "C8_B", "C10_A", "C8_D", "C9_A", "C10_C", "C8_A", "C1_B", "C10_B", "C8_C",
             "C12_A")
    release = _taxonomy(Sessions, codes, activate=False)
    rows = _capability_rows(engine, release.id)
    for competency, expected in (("C8", ("C8_A", "C8_B", "C8_C", "C8_D")),
                                 ("C10", ("C10_A", "C10_B", "C10_C", "C10_D")), ("C1", ("C1_A", "C1_B"))):
        context = svc._build_current_taxonomy_context(db, release_id=release.id, competency_code=competency)
        assert context == CurrentTaxonomyContext(release_id=release.id, competency_code=competency,
                                                 capabilities=tuple(_expected_capability(rows[c]) for c in expected))
    everything = [c.capability_code for i in range(1, 13) for c in svc._build_current_taxonomy_context(
        db, release_id=release.id, competency_code=f"C{i}").capabilities]
    assert everything == [d.capability_code for _, d in tax.get_release_capabilities(db, release_id=release.id)]
    assert everything.index("C2_A") < everything.index("C10_A") and sorted(everything) != everything
    assert svc._build_current_taxonomy_context(db, release_id=release.id, competency_code="C3") == \
        CurrentTaxonomyContext(release_id=release.id, competency_code="C3", capabilities=())
    with pytest.raises(InvalidInferenceState, match="illisible"):
        svc._build_current_taxonomy_context(db, release_id=uuid.uuid4(), competency_code="C8")


def test_pg_current_taxonomy_context_is_select_only_with_a_constant_query_count(engine, Sessions):
    """Anti-N+1 : release puis memberships + définitions en une jointure,
    quel que soit le nombre de capacités ; aucune écriture ni verrou."""
    small = _taxonomy(Sessions, ("C10_A",), activate=False)
    large = _taxonomy(Sessions, ("C10_A", "C10_B", "C10_C", "C10_D", "C8_A", "C8_B"), activate=False)
    counts = {}
    for release, size in ((small, 1), (large, 4)):
        with Sessions() as session:
            statements, record = _recording(engine)
            sa.event.listen(engine, "before_cursor_execute", record)
            try:
                context = svc._build_current_taxonomy_context(session, release_id=release.id, competency_code="C10")
            finally:
                sa.event.remove(engine, "before_cursor_execute", record)
            assert len(context.capabilities) == size
            assert all(sql.startswith("SELECT") and " FOR " not in sql for sql, _ in statements)
            assert not session.new and not session.dirty and not session.deleted
            counts[size] = len(statements)
    assert counts[1] == counts[4] == 2


def test_pg_v2_successor_exposes_the_current_taxonomy_next_to_its_history(engine, Sessions, db, world):
    """Inférence V2 avec predecessor : predecessor, contexte historique et
    causalité présents et inchangés par T6-C0 ; taxonomie courante =
    release du nouveau parent T5 ; start == get."""
    w = world
    first = _start(Sessions, w.t5)
    _complete(Sessions, first.run_id, _app_decision(w))
    l2, _ = _new_t5(Sessions, w, sup(w.A))
    second = _start(Sessions, l2)
    assert second.input_schema_version == 2 and second.transition_causality is not None
    assert second.predecessor.inference_run_id == first.run_id
    assert second.predecessor_decision_context.inference_run_id == first.run_id
    assert second.current_taxonomy_context == first.current_taxonomy_context
    assert second.current_taxonomy_context.release_id == second.pedagogical_taxonomy_release_id == w.tx.id
    resumed = svc.get_inference_context(db, run_id=second.run_id)
    assert resumed == second and resumed.current_taxonomy_context == second.current_taxonomy_context


def test_pg_legacy_v1_candidate_also_exposes_the_derived_taxonomy(engine, Sessions, db, world):
    """Candidat V1 legacy : input_schema_version 1, transition_causality
    None, empreinte V1 reproduite à l'identique ET current_taxonomy_context
    présent (pur développement de la release déjà engagée)."""
    w = world
    first = _start(Sessions, w.t5)
    v1 = _as_legacy_v1(engine, db, first.run_id)
    legacy = svc.get_inference_context(db, run_id=first.run_id)
    assert (legacy.input_schema_version, legacy.transition_causality, legacy.input_fingerprint) == (1, None, v1)
    assert legacy.current_taxonomy_context == first.current_taxonomy_context
    assert len(legacy.current_taxonomy_context.capabilities) == 3
    parent = svc._parent_row(db, w.t5, lock=False)
    assert svc._input_fingerprint_v1(svc._dossier_payload(parent, svc._relations(db, w.t5)), None) == v1 == _run(
        engine, first.run_id)["input_fingerprint"]


def test_pg_cross_release_resolution_goes_through_definition_ids_never_codes(engine, Sessions, db, world):
    """R1 : C7_A = (TM1, TD1), C7_B = (M1b, D1b). R2 réutilise D1 sous M2 et
    crée une NOUVELLE révision de C7_B (TD2). Le contexte R2 expose (TM2, TD1)
    et (M2b, TD2) : aucun membership de R1, aucune correspondance D1b -> M2b
    par capability_code. Une tension localized résolue par definition_id
    via ce contexte est acceptée par T6-B ; le membership historique M1 est
    refusé (validation T6-B inchangée)."""
    w = world
    r2 = _taxonomy(Sessions, {"C7_A": w.dA, "C7_B": None, "C7_C": w.tx.d["C7_C"]})
    assert _rows(engine, "SELECT status FROM pedagogical_taxonomy_releases WHERE id = :i",
                 i=w.tx.id)[0]["status"] == "retired"
    positive = _t3(Sessions, r2.id, app(r2.m["C7_A"])).id
    contradiction = _t3(Sessions, r2.id, contra(r2.m["C7_A"])).id
    l2 = _t5_now(Sessions, w, release_id=r2.id)
    context = _start(Sessions, l2)
    taxonomy = context.current_taxonomy_context
    assert taxonomy.release_id == r2.id == context.pedagogical_taxonomy_release_id
    by_code = {c.capability_code: c for c in taxonomy.capabilities}
    assert list(by_code) == ["C7_A", "C7_B", "C7_C"]
    # Même définition réutilisée : même sens, membership propre à R2.
    assert (by_code["C7_A"].definition_id, by_code["C7_A"].membership_id) == (w.dA, r2.m["C7_A"])
    assert r2.m["C7_A"] != w.A
    # Nouvelle révision : autre définition, jamais assimilée à l'ancienne.
    old_b, new_b = _capability_rows(engine, w.tx.id)["C7_B"], _capability_rows(engine, r2.id)["C7_B"]
    assert (by_code["C7_B"].definition_id, by_code["C7_B"].membership_id) == (r2.d["C7_B"], r2.m["C7_B"])
    assert by_code["C7_B"].definition_id != w.dB
    assert by_code["C7_B"].semantic_revision == new_b["semantic_revision"] != old_b["semantic_revision"]
    assert [c for c in taxonomy.capabilities if c.definition_id == w.dB] == []
    assert not {c.membership_id for c in taxonomy.capabilities} & set(w.tx.m.values())

    # Résolution definition_id -> membership courant, sans base (futur T6-C).
    (current,) = [c for c in taxonomy.capabilities if c.definition_id == w.dA]
    refs = [pos("application", positive), ref("tension", "observation", contradiction, tension_key="t1")]
    historical = decision("application", refs=refs, tensions=[tension("t1", mode="localized", memberships=(w.A,))])
    with pytest.raises(InvalidInferenceTension, match="autre release"):
        svc.complete_competency_inference(db, run_id=context.run_id, decision=historical)
    db.rollback()
    resolved = decision("application", refs=refs, tensions=[tension(
        "t1", mode="localized", memberships=(current.membership_id,))])
    svc.complete_competency_inference(db, run_id=context.run_id, decision=resolved)
    db.commit()
    assert _state(engine, context.run_id) == ("completed", "active")
    ((_, memberships),) = svc.get_inference_tensions(db, run_id=context.run_id)
    assert tuple(memberships) == (r2.m["C7_A"],)


def test_pg_retiring_the_release_after_start_keeps_the_existing_stale_rule(engine, Sessions, db, world):
    """Aucun maintien artificiel : release retirée après start =>
    StaleInferenceInput (règle T6-B inchangée), avant toute résolution
    taxonomique."""
    w = world
    context = _start(Sessions, w.t5)
    _taxonomy(Sessions, ("C7_A",))
    with pytest.raises(StaleInferenceInput, match="retired"):
        svc.get_inference_context(db, run_id=context.run_id)


def test_pg_duplicate_capability_code_in_the_release_is_invalid_state(engine, Sessions, db, world):
    """Corruption SQL (la base ne peut pas l'imposer, T4-B l'interdit) : la
    release active contient deux révisions de C7_A. start => aucun
    candidat ; get d'un candidat existant => InvalidInferenceState ; jamais
    une ligne choisie arbitrairement."""
    w = world
    existing = _start(Sessions, w.t5)
    other_revision = _taxonomy(Sessions, ("C7_A",), activate=False).d["C7_A"]
    _exec(engine, "INSERT INTO capability_taxonomy_memberships (id, taxonomy_release_id, capability_definition_id,"
                  " created_at) VALUES (:i, :r, :d, now())", i=uuid.uuid4(), r=w.tx.id, d=other_revision)
    with pytest.raises(InvalidInferenceState, match="capability_code C7_A en double"):
        svc._build_current_taxonomy_context(db, release_id=w.tx.id, competency_code="C7")
    with pytest.raises(InvalidInferenceState, match="capability_code C7_A en double"):
        svc.get_inference_context(db, run_id=existing.run_id)
    db.rollback()
    with pytest.raises(InvalidInferenceState, match="capability_code C7_A en double"):
        svc.start_competency_inference(db, **_start_kwargs(w.t5, state_decision_version="state-2"))
    db.rollback()
    assert _count(engine, "competency_inference_runs") == 1
    # Une autre compétence de la même release n'est pas concernée.
    assert [c.capability_code for c in svc._build_current_taxonomy_context(
        db, release_id=w.tx.id, competency_code="C8").capabilities] == ["C8_A"]


# --------------------------------------------------------------------------
# Étape 6.4C : lignée adoptée (get_inference_lineage)
# --------------------------------------------------------------------------

def test_historical_inference_run_is_a_minimal_frozen_structure():
    """Identité technique (validation des liens) + sorties de stade : ni
    tension, ni contexte de révision, ni besoin de validation, ni claim, ni
    horodatage, ni confiance."""
    names = [f.name for f in dataclasses.fields(HistoricalInferenceRun)]
    assert names == ["inference_run_id", "user_id", "competency_code", "predecessor_inference_run_id",
                     "previous_stage", "current_stage", "transition", "transition_cause"]
    assert HistoricalInferenceRun.__dataclass_params__.frozen
    assert all(f.kw_only for f in dataclasses.fields(HistoricalInferenceRun))
    for field in ("tension_state", "unresolved_revision_context", "validation_needs", "claims", "confidence",
                  "started_at", "completed_at", "created_at", "output_fingerprint", "state_decision_summary"):
        assert field not in names, field


def test_lineage_argument_is_validated_before_the_database():
    for value in INVALID_UUIDS:
        with pytest.raises(InvalidInferenceArgument):
            svc.get_inference_lineage(_NoDB(), run_id=value)


def _lineage_source():
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in ("_lineage_rows", "get_inference_lineage")]
    assert len(nodes) == 2
    return "\n".join(_code_tokens(ast.unparse(_without_docstring(n))) for n in nodes)


def test_lineage_follows_predecessor_links_only_never_a_timestamp_order():
    """Une requête récursive (UNION : un cycle termine) sur
    predecessor_inference_run_id ; aucun ORDER BY, aucun horodatage, aucun
    verrou, aucune écriture, aucune lecture de claim, tension, ref,
    observation ni dossier T5."""
    tokens = set(_lineage_source().split())
    assert {"cte", "union", "predecessor_inference_run_id", "reverse"} <= tokens
    for forbidden in ("order_by", "union_all", "started_at", "completed_at", "created_at", "updated_at",
                      "with_for_update", "flush", "unresolved_revision_context", "tension_state",
                      "validation_needs", "CompetencyStageClaim", "CompetencyInferenceTension",
                      "CompetencyInferenceBasisRef", "PedagogicalObservation", "LongitudinalAssessmentRun",
                      "build_longitudinal_dossier", "get_validated_user_competency_state", "_children"):
        assert forbidden not in tokens, forbidden
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    used = {a.attr for n in tree.body if isinstance(n, ast.FunctionDef)
            and n.name in ("_lineage_rows", "get_inference_lineage")
            for a in ast.walk(n) if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name)
            and a.value.id == "db"}
    assert used == {"execute", "no_autoflush"}


def _lineage_world(Sessions, w):  # noqa: N803
    """R1 application (première inférence) -> R2 maintained -> R3
    revised_down vers comprehension -> R4 upgraded vers application (active).
    Chaque run successeur change une spécification T6 :
    pedagogical_reinterpretation a son support structurel."""
    r1 = _activate_first(Sessions, w)
    r2 = _start(Sessions, w.t5, state_decision_version="state-2")
    _complete(Sessions, r2.run_id, _app_decision(w, cause="pedagogical_reinterpretation"))
    r3 = _start(Sessions, w.t5, state_decision_version="state-3")
    _complete(Sessions, r3.run_id, decision("comprehension", refs=[pos("comprehension", w.comp),
                                                                   ref("transition", "observation", w.k)],
                                            cause="pedagogical_reinterpretation"))
    r4 = _start(Sessions, w.t5, state_decision_version="state-4")
    _complete(Sessions, r4.run_id, _app_decision(w, refs=[pos("application", w.app),
                                                          ref("transition", "observation", w.app)],
                                                 cause="pedagogical_reinterpretation"))
    return r1, r2.run_id, r3.run_id, r4.run_id


def _lineage(Sessions, run_id):  # noqa: N803
    with Sessions() as session:
        result = svc.get_inference_lineage(session, run_id=run_id)
        assert not session.new and not session.dirty and not session.deleted
        return result


def test_pg_lineage_is_oldest_to_current_with_derived_transitions(engine, Sessions, world):
    w = world
    r1, r2, r3, r4 = _lineage_world(Sessions, w)
    before = _t6_snapshot(engine)
    statements = []

    def record(conn, cursor, sql, *args):
        statements.append(" ".join(sql.split()))

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        lineage = _lineage(Sessions, r4)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert _t6_snapshot(engine) == before
    assert len(statements) == 1 and statements[0].startswith("WITH RECURSIVE")
    assert " FOR " not in statements[0] and "ORDER BY" not in statements[0]
    assert "UNION ALL" not in statements[0] and " UNION " in statements[0]
    assert type(lineage) is tuple and all(type(run) is HistoricalInferenceRun for run in lineage)
    assert lineage == (
        HistoricalInferenceRun(inference_run_id=r1, user_id=USER, competency_code="C7",
                               predecessor_inference_run_id=None, previous_stage=None,
                               current_stage="application", transition=None, transition_cause=None),
        HistoricalInferenceRun(inference_run_id=r2, user_id=USER, competency_code="C7",
                               predecessor_inference_run_id=r1, previous_stage="application",
                               current_stage="application", transition="maintained",
                               transition_cause="pedagogical_reinterpretation"),
        HistoricalInferenceRun(inference_run_id=r3, user_id=USER, competency_code="C7",
                               predecessor_inference_run_id=r2, previous_stage="application",
                               current_stage="comprehension", transition="revised_down",
                               transition_cause="pedagogical_reinterpretation"),
        HistoricalInferenceRun(inference_run_id=r4, user_id=USER, competency_code="C7",
                               predecessor_inference_run_id=r3, previous_stage="comprehension",
                               current_stage="application", transition="upgraded",
                               transition_cause="pedagogical_reinterpretation"))
    # Le service est générique : la lignée d'un run superseded est son passé.
    assert [run.inference_run_id for run in _lineage(Sessions, r2)] == [r1, r2]
    assert [run.inference_run_id for run in _lineage(Sessions, r1)] == [r1]


def test_pg_lineage_order_comes_from_links_never_from_timestamps(engine, Sessions, world):
    """Horodatages inversés : l'ordre reste celui des liens predecessor."""
    w = world
    r1, r2, r3, r4 = _lineage_world(Sessions, w)
    reference = _lineage(Sessions, r4)
    for run_id, offset in ((r1, 30), (r2, 20), (r3, 10), (r4, 0)):
        _exec(engine, "UPDATE competency_inference_runs SET started_at = now() + make_interval(days => :o),"
                      " completed_at = now() + make_interval(days => :o) WHERE id = :r", o=offset, r=run_id)
    assert _lineage(Sessions, r4) == reference


def test_pg_lineage_excludes_failed_running_and_foreign_runs(engine, Sessions, world):
    """Un candidat failed / obsolete et un candidat running ont pour
    predecessor l'active, mais n'en sont pas des ancêtres ; aucun run hors
    de la chaîne n'est jamais retourné."""
    w = world
    r1, r2, r3, r4 = _lineage_world(Sessions, w)
    failed = _start(Sessions, w.t5, state_decision_version="state-5").run_id
    with Sessions() as session:
        svc.fail_competency_inference(session, run_id=failed, failure_code="timeout")
        session.commit()
    running = _start(Sessions, w.t5, state_decision_version="state-6").run_id
    assert {_run(engine, failed)["predecessor_inference_run_id"],
            _run(engine, running)["predecessor_inference_run_id"]} == {r4}
    assert [run.inference_run_id for run in _lineage(Sessions, r4)] == [r1, r2, r3, r4]
    for candidate in (failed, running):
        with pytest.raises(InvalidInferenceState, match="completed requis"):
            _lineage(Sessions, candidate)
    with pytest.raises(InferenceRunNotFound):
        _lineage(Sessions, uuid.uuid4())


def test_pg_lineage_is_history_not_evidence(engine, Sessions, world):
    """Une preuve ancienne invalidée depuis, un dossier T5 superseded : la
    lignée reste un fait historique, identique (aucune preuve relue)."""
    w = world
    r1, r2, r3, r4 = _lineage_world(Sessions, w)
    reference = _lineage(Sessions, r4)
    _invalidate(Sessions, w.app)
    _new_t5(Sessions, w, sup(w.A))
    assert _rows(engine, "SELECT interpretation_status FROM longitudinal_assessment_runs WHERE id = :i",
                 i=w.t5)[0]["interpretation_status"] == "superseded"
    assert _lineage(Sessions, r4) == reference


@pytest.mark.parametrize("case", ["cycle", "missing_predecessor", "other_user", "other_competency"])
def test_pg_lineage_link_corruption_fails_closed(engine, Sessions, world, case):
    w = world
    r1, r2, r3, r4 = _lineage_world(Sessions, w)
    update = "UPDATE competency_inference_runs SET {} WHERE id = :r"
    if case == "cycle":
        _exec(engine, update.format("predecessor_inference_run_id = :p"), r=r1, p=r3)
        match = "cycle"
    elif case == "missing_predecessor":
        # FK contournée (superuser, triggers de FK désactivés localement).
        with engine.begin() as conn:
            conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
            conn.execute(sa.text(update.format("predecessor_inference_run_id = :p")), {"r": r2, "p": uuid.uuid4()})
        match = "introuvable"
    elif case == "other_user":
        _exec(engine, update.format("user_id = :u"), r=r2, u=OTHER_USER)
        match = "autre couple"
    else:
        _exec(engine, update.format("competency_code = 'C8'"), r=r2)
        match = "autre couple"
    with pytest.raises(InvalidInferenceState, match=match):
        _lineage(Sessions, r4)


@pytest.mark.parametrize("target, assignment, match", [
    ("r1", "previous_stage = 'discovery'", "première inférence"),
    ("r1", "transition = 'upgraded'", "première inférence"),
    ("r1", "transition_cause = 'new_user_evidence'", "première inférence"),
    ("r2", "previous_stage = 'comprehension'", "previous_stage"),
    ("r2", "previous_stage = NULL", "previous_stage"),
    ("r2", "transition = 'upgraded'", "maintained dérivée"),
    ("r3", "transition = 'maintained'", "revised_down dérivée"),
    ("r4", "transition = 'revised_down'", "upgraded dérivée"),
    ("r4", "transition = NULL", "upgraded dérivée"),
    ("r3", "transition_cause = NULL", "transition_cause"),
    ("r2", "current_stage = NULL", "current_stage"),
    ("r1", "interpretation_status = 'obsolete'", "superseded attendu"),
    ("r2", "execution_status = 'failed'", "completed requis"),
    ("r4", "interpretation_status = 'obsolete'", "active / superseded attendu"),
])
def test_pg_lineage_transition_corruption_fails_closed(engine, Sessions, world, target, assignment, match):
    """Transition et cause persistées validées, jamais redécidées ni
    réparées : première inférence sans transition ; ensuite previous_stage
    = stade du predecessor, transition dérivée, cause obligatoire ;
    predecessors superseded ; tous completed."""
    w = world
    runs = dict(zip(("r1", "r2", "r3", "r4"), _lineage_world(Sessions, w)))
    _exec(engine, f"UPDATE competency_inference_runs SET {assignment} WHERE id = :r", r=runs[target])
    with pytest.raises(InvalidInferenceState, match=match):
        _lineage(Sessions, runs["r4"])
