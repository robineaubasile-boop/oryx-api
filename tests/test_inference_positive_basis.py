"""Tests de T6-C1 : Positive Basis Engine (core/inference_positive_basis.py).

1. Tests purs (toujours exécutés) : les InferenceContext sont construits à la
   main ; chaque LongitudinalDossier est dérivé par les fonctions PURES de
   T5-C (core/longitudinal_view.py : épisodes, profils, limitations) à partir
   d'observations et de relations décrites par un petit builder, afin que le
   moteur soit testé contre le contrat réel de la vue et non contre une
   imitation. Aucune base, aucun modèle.

2. Un test d'intégration contre un vrai PostgreSQL (mêmes conditions que
   T1-T6-B, sinon SKIPPÉ) : InferenceContext produit par
   start_competency_inference, évalué par T6-C1, puis décision construite
   par un adaptateur de TEST et acceptée par complete_competency_inference
   (compatibilité des positive_basis refs avec T6-B). T6-C1 lui-même ne
   construit jamais d'InferenceDecision.
"""
import ast
import dataclasses
import itertools
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace

import pytest

from core import inference_positive_basis as engine_module
from core import inference_service as svc
from core import longitudinal_view as view
from core.inference_policies import (
    POSITIVE_BASIS_V1,
    InconsistentInferenceContext,
    PositiveBasisError,
    UnsupportedInferenceContext,
    UnsupportedPositiveBasisPolicy,
    UnsupportedTaxonomySemantics,
    resolve_positive_basis_policy,
)
from core.inference_positive_basis import (
    ClaimLimitation,
    DemonstrationScope,
    DirectClaimAssessment,
    MasteryAssessment,
    MasteryPropertyAssessment,
    PositiveBasisAssessment,
    PositiveBasisClaim,
    PositiveDemonstration,
    PositiveEpisode,
    PositiveEvidence,
    StructuralRef,
    evaluate_positive_basis,
    project_positive_evidence,
)
from core.inference_service import (
    BasisRefDecision,
    CurrentTaxonomyCapability,
    CurrentTaxonomyContext,
    InferenceContext,
    InferenceDecision,
    StageClaimDecision,
)
from core.pedagogy.taxonomy_v1 import CAPABILITY_CODES
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens

ENGINE_PATH = REPO_ROOT / "core" / "inference_positive_basis.py"
POLICIES_PATH = REPO_ROOT / "core" / "inference_policies.py"
STAGES = ("discovery", "comprehension", "application", "mastery")
NS = uuid.UUID("7d9c3f5e-2f44-4a38-9a43-1c6d1f0f6c01")
RELEASE = uuid.uuid5(NS, "release:oryx-v1")
EPOCH = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
VERSIONS = ("positive_basis_version", "confidence_profile_version", "state_decision_version", "validation_version",
            "inference_schema_version", "evaluator_version")


# --------------------------------------------------------------------------
# Builder : taxonomie courante, observations, relations -> vue T5-C réelle
# --------------------------------------------------------------------------

def definition_id(code, revision=1):
    return uuid.uuid5(NS, f"definition:{code}:r{revision}")


def capability_ref(code, revision=1):
    return view.CapabilityRef(definition_id(code, revision), code, revision, f"libellé {code}")


def competency_codes(competency):
    return tuple(c for c in CAPABILITY_CODES if c.split("_")[0] == competency)


def taxonomy(competency, *, release=RELEASE, revisions=None, codes=None, extra=()):
    """CurrentTaxonomyContext (T6-C0) : revisions = {code: révision},
    codes = sous-ensemble explicite, extra = capacités supplémentaires."""
    revisions = revisions or {}
    capabilities = [CurrentTaxonomyCapability(
        membership_id=uuid.uuid5(NS, f"membership:{release}:{code}:{revisions.get(code, 1)}"),
        definition_id=definition_id(code, revisions.get(code, 1)), capability_code=code,
        semantic_revision=revisions.get(code, 1), label=f"libellé {code}")
        for code in (competency_codes(competency) if codes is None else codes)]
    capabilities.extend(extra)
    return CurrentTaxonomyContext(release_id=release, competency_code=competency, capabilities=tuple(capabilities))


class Dossier:
    """Décrit un dossier T5 ; dossier() le fait dériver par T5-C."""

    def __init__(self, competency, *, release=RELEASE, clock=EPOCH, revisions=None):
        self.competency, self.release, self.clock = competency, release, clock
        self.revisions = revisions or {}
        self.observations, self.dependencies, self.transfers, self.revalidations = [], [], [], []
        self.events = {}
        self._ids = itertools.count(1)

    # -- observations --------------------------------------------------------
    def _event(self, name):
        if name not in self.events:
            index = len(self.events)
            self.events[name] = SimpleNamespace(
                id=uuid.uuid5(NS, f"event:{name}"), run=uuid.uuid5(NS, f"t3-run:{name}"),
                started=self.clock + timedelta(hours=index), ordinals=itertools.count(1))
        return self.events[name]

    def observe(self, event, *caps, stage="application", strength="strong", polarity="supportive", support="none",
                elicitation="spontaneous", competency=None, name=None, scope="application"):
        e = self._event(event)
        observation_id = uuid.uuid5(NS, f"observation:{name or next(self._ids)}")
        contradictory = polarity == "contradictory"
        refs = tuple(sorted((capability_ref(c, self.revisions.get(c, 1)) for c in caps), key=view._capability_order))
        self.observations.append(view.HistoryObservation(
            observation_id=observation_id, evaluation_run_id=e.run, event_id=e.id, ordinal=next(e.ordinals),
            competency_code=competency or self.competency, polarity=polarity, evidence_strength=strength,
            local_stage=None if contradictory else stage,
            contradiction_scope=scope if contradictory else None,
            error_type="conceptual" if contradictory else None, observation_role="primary", task_kind="analysis",
            elicitation_mode=elicitation, support_level=support,
            capability_localization="localized" if caps else "competency_only",
            observation_text="texte libre que T6-C1 ne relit jamais",
            primary_user_action=MappingProxyType({"kind": "answer"}), contributive_user_actions=(),
            residual_cognitive_work=MappingProxyType({"left": "tout"}), source_contribution_refs=(),
            compatible_capabilities=refs, source_taxonomy_release_id=self.release, re_evaluates_run_id=None,
            event=view.EventProvenance(
                event_id=e.id, event_origin="analysis", task_kind="analysis", analysis_session_id=None,
                conversation_key=None, event_started_at_technical=e.started,
                event_closed_at_technical=e.started + timedelta(minutes=30), demonstration_time=None,
                demonstration_time_source=view.DEMONSTRATION_TIME_UNAVAILABLE),
            observation_created_at_inference=e.started + timedelta(minutes=31),
            current_integrity_status="valid", current_evaluation_run_interpretation_status="active"))
        return observation_id

    def contra(self, event, *caps, **kwargs):
        return self.observe(event, *caps, polarity="contradictory", **kwargs)

    # -- relations (contraintes structurelles de T5-B) -----------------------
    def _get(self, observation_id):
        return next(o for o in self.observations if o.observation_id == observation_id)

    def _scope(self, caps, available):
        if caps is None:
            mode, definitions = ("whole_observation", frozenset(available))
        elif caps == "competency_only":
            mode, definitions = ("competency_only", frozenset())
        else:
            mode, definitions = ("localized", frozenset(definition_id(c) for c in caps))
            assert definitions <= frozenset(available), "scope localized hors du périmètre compatible"
        refs = tuple(sorted((capability_ref(c.capability_code) for o in self.observations
                             for c in o.compatible_capabilities if c.definition_id in definitions),
                            key=view._capability_order))
        refs = tuple(dict.fromkeys(refs))
        return mode, definitions, view.RelationScope(
            scope_mode=mode, capabilities=refs,
            scope_fingerprint=view._scope_fingerprint(self.competency, mode, definitions))

    @staticmethod
    def _compatible(observation):
        return frozenset(c.definition_id for c in observation.compatible_capabilities)

    def transfer(self, source, target, caps=None):
        s, t = self._get(source), self._get(target)
        assert s.event_id != t.event_id and s.polarity == t.polarity == "supportive"
        assert t.local_stage == "application"
        mode, definitions, scope = self._scope(caps, self._compatible(s) & self._compatible(t))
        row = SimpleNamespace(id=uuid.uuid5(NS, f"transfer:{source}:{target}:{caps}"),
                              transfer_basis={"adaptation": "configuration différente"})
        self.transfers.append(view._Resolved(view.TRANSFER, row, t, s, mode, definitions, scope,
                                             (source, target, scope.scope_fingerprint)))
        return row.id

    def revalidation(self, source, target, caps=None):
        s, t = self._get(source), self._get(target)
        assert s.event_id != t.event_id and s.polarity == "contradictory" and t.polarity == "supportive"
        mode, definitions, scope = self._scope(caps, self._compatible(s) & self._compatible(t))
        row = SimpleNamespace(id=uuid.uuid5(NS, f"revalidation:{source}:{target}:{caps}"),
                              revalidation_basis={"mechanism": "redémontré sans aide"})
        self.revalidations.append(view._Resolved(view.REVALIDATION, row, t, s, mode, definitions, scope,
                                                 (source, target, scope.scope_fingerprint)))
        return row.id

    def dependency(self, target, source, *, kind="dependent", caps=None):
        t, s = self._get(target), self._get(source)
        assert s.event_id != t.event_id
        mode, definitions, scope = self._scope(caps, self._compatible(t))
        row = SimpleNamespace(id=uuid.uuid5(NS, f"dependency:{source}:{target}:{caps}"), dependency_type=kind,
                              source_kind="observation", source_observation_id=source, source_support_trace_id=None,
                              trace_event_id=None, trace_support_kind=None, dependency_basis={"why": "reprise"})
        self.dependencies.append(view._Resolved(view.DEPENDENCY, row, t, s, mode, definitions, scope,
                                                (target, "observation", source, None, scope.scope_fingerprint)))
        return row.id

    # -- vue T5-C ------------------------------------------------------------
    def dossier(self, *, reverse=False):
        observations = sorted(self.observations, key=lambda o: (o.event.event_started_at_technical, str(o.event_id),
                                                                o.ordinal, str(o.observation_id)))
        if reverse:
            observations.reverse()
        observations = tuple(observations)
        deps, transfers, revals = (list(reversed(x)) if reverse else list(x)
                                   for x in (self.dependencies, self.transfers, self.revalidations))
        view._check_no_overlap(deps, transfers + revals)
        release_refs = {definition_id(c, self.revisions.get(c, 1)): capability_ref(c, self.revisions.get(c, 1))
                        for c in CAPABILITY_CODES}
        competencies = {d: ref.capability_code.split("_")[0] for d, ref in release_refs.items()}
        episodes = view._episodes(observations)
        dependency_profile = view._dependency_profile(observations, deps, transfers, revals)
        consistency_profile = view._consistency_profile(observations, deps, revals)
        run = SimpleNamespace(competency_code=self.competency)
        return view.LongitudinalDossier(
            view_schema_version=view.VIEW_SCHEMA_VERSION, run_id=uuid.uuid5(NS, f"t5:{self.release}"),
            user_id="user-t6c1", competency_code=self.competency, pedagogical_taxonomy_release_id=self.release,
            input_fingerprint="0" * 64, execution_status="completed", interpretation_status="active",
            dependency_version="dependency-1", transfer_version="transfer-1", revalidation_version="revalidation-1",
            relation_schema_version="relation-1", run_completed_at_technical=self.clock + timedelta(days=400),
            active_history=view.ActiveHistory(observations=observations, episodes=episodes),
            dependency_profile=dependency_profile,
            coverage_profile=view._coverage_profile(run, observations, release_refs, competencies),
            variety_profile=view._variety_profile(observations, episodes, transfers),
            transfer_profile=view._transfer_profile(transfers),
            consistency_profile=consistency_profile,
            temporal_validation_profile=view._temporal_profile(observations, episodes, deps, transfers, revals),
            limitations=view._limitations(observations, (), dependency_profile, consistency_profile, deps, transfers,
                                          revals))

    def context(self, *, reverse=False, taxonomy_context=None, **overrides):
        dossier = self.dossier(reverse=reverse)
        kwargs = dict(
            run_id=uuid.uuid5(NS, "t6-run"), user_id="user-t6c1", competency_code=self.competency,
            trigger="longitudinal_completed", longitudinal_assessment_run_id=dossier.run_id,
            pedagogical_taxonomy_release_id=self.release, longitudinal_dossier=dossier, predecessor=None,
            predecessor_decision_context=None, transition_causality=None,
            current_taxonomy_context=taxonomy_context or taxonomy(self.competency, release=self.release,
                                                                  revisions=self.revisions),
            input_schema_version=2, input_fingerprint="1" * 64, inference_dedup_key="2" * 64,
            **{name: f"{name.split('_version')[0]}-1" for name in VERSIONS}, model_id=None, prompt_spec_version=None)
        kwargs.update(overrides)
        return InferenceContext(**kwargs)

    def assess(self, **kwargs):
        return evaluate_positive_basis(self.context(**kwargs))


def claim(assessment, stage):
    return assessment.claims[STAGES.index(stage)]


def statuses(assessment):
    return {c.stage: (c.status, c.basis_mode) for c in assessment.claims}


EST, NOT = "established", "not_established"
DIRECT = (EST, "direct")
IMPLIED = (EST, "implied_by_higher_claim")
NONE = (NOT, "none")


# --------------------------------------------------------------------------
# Structures, API, pureté
# --------------------------------------------------------------------------

STRUCTURES = (DemonstrationScope, PositiveDemonstration, PositiveEpisode, PositiveEvidence, StructuralRef,
              ClaimLimitation, DirectClaimAssessment, MasteryPropertyAssessment, MasteryAssessment,
              PositiveBasisClaim, PositiveBasisAssessment)


def test_structures_are_frozen_keyword_only_dataclasses():
    for cls in STRUCTURES:
        assert cls.__dataclass_params__.frozen, cls
        assert all(f.kw_only for f in dataclasses.fields(cls)), cls
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(PositiveBasisAssessment) == ["policy_version", "competency_code", "claims",
                                              "highest_established_stage"]
    assert names(PositiveBasisClaim) == [
        "stage", "status", "basis_mode", "implied_from_stage", "basis_observation_ids",
        "represented_capability_definition_ids",
        "competency_only_observation_ids", "structural_basis_refs", "representative_pattern_names",
        "representativeness_reason", "limitations", "mastery_assessment"]
    assert names(MasteryAssessment) == [
        "schema_version", "autonomy", "independent_repetition", "variety_transfer", "longitudinality",
        "robustness_revision", "represented_capability_definition_ids", "limitations"]
    assert names(MasteryPropertyAssessment) == ["status", "observation_ids", "structural_refs", "reason_codes",
                                                "limitations"]
    assert names(PositiveDemonstration) == [
        "observation_id", "event_id", "local_stage", "evidence_strength", "elicitation_mode", "support_level",
        "capability_localization", "capability_definition_ids", "scope_readings"]
    # Aucune sortie de décision finale, aucun nombre.
    for cls in STRUCTURES:
        for field in ("current_stage", "confidence_profile", "tension", "transition", "transition_cause",
                      "validation_needs", "state_decision_summary", "score", "ratio", "percentage", "points",
                      "weight", "level", "count"):
            assert field not in names(cls), (cls, field)
        for f in dataclasses.fields(cls):
            assert f.type not in (int, float, "int", "float"), (cls, f.name)


def test_public_entry_point_is_a_pure_single_argument_function():
    import inspect
    assert list(inspect.signature(evaluate_positive_basis).parameters) == ["context"]
    tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
    public = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == {"evaluate_positive_basis", "project_positive_evidence"}


def _imports(path):
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_no_database_llm_network_clock_or_randomness():
    """31-32 : aucune dépendance de base, de session, de modèle, de réseau,
    d'horloge ni de hasard ; aucune transaction."""
    assert _imports(ENGINE_PATH) == {"uuid", "dataclasses", "core.inference_policies", "core.inference_service"}
    assert _imports(POLICIES_PATH) == {"uuid", "collections.abc", "dataclasses", "types", "core.inference_service",
                                       "core.pedagogy.taxonomy_v1"}
    for path in (ENGINE_PATH, POLICIES_PATH):
        tokens = _code_tokens(path.read_text(encoding="utf-8")).lower().split("\n")
        for word in ("sqlalchemy", "session", "select", "execute", "commit", "rollback", "flush", "db", "models",
                     "anthropic", "openai", "claude", "llm", "requests", "httpx", "urllib", "socket", "random",
                     "datetime", "now", "utcnow", "time", "uuid4", "sleep", "getenv", "environ", "open"):
            assert word not in tokens, (path.name, word)


def test_no_text_reinterpretation_of_opaque_payloads():
    """Aucune relecture de texte libre ni de payload opaque T3."""
    tokens = _code_tokens(ENGINE_PATH.read_text(encoding="utf-8")).split("\n")
    for field in ("observation_text", "primary_user_action", "contributive_user_actions", "residual_cognitive_work",
                  "source_contribution_refs", "event_started_at_technical", "event_closed_at_technical",
                  "demonstration_time", "observation_created_at_inference", "run_completed_at_technical",
                  "predecessor", "predecessor_decision_context", "transition_causality", "current_integrity_status",
                  "current_evaluation_run_interpretation_status", "limitations_of_dossier"):
        assert field not in tokens, field


def test_no_score_ratio_average_weight_or_numeric_rule():
    """33 : aucun score, ratio, moyenne, poids ni seuil ; le moteur ne
    contient aucun littéral numérique ni len() ; la policy seulement la
    version de format d'entrée exigée."""
    for path in (ENGINE_PATH, POLICIES_PATH):
        tokens = _code_tokens(path.read_text(encoding="utf-8")).lower()
        # Mots entiers des identifiants et chaînes (snake_case découpé).
        words = set(re.split(r"[^a-z0-9]+", tokens))
        for word in ("score", "scores", "scoring", "percent", "percentage", "ratio", "ratios", "average", "mean",
                     "points", "weight", "weights", "weighting", "coefficient", "threshold", "xp", "rank", "decay",
                     "sum", "count", "counter", "min", "max", "days", "freshness", "balance", "almost"):
            assert word not in words, (path.name, word)
        for compound in ("coverage_percent", "evidence_balance", "mastery_score", "competency_score",
                         "capability_ratio"):
            assert compound not in tokens, (path.name, compound)
    tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
    numbers = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) in (int, float)]
    assert numbers == []
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "len"]
    tree = ast.parse(POLICIES_PATH.read_text(encoding="utf-8"))
    numeric = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
               and type(n.value.value) in (int, float)]
    assert [ast.unparse(n) for n in numeric] == ["SUPPORTED_INPUT_SCHEMA_VERSION = 2"]
    assert len([n for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) in (int, float)]) == 1
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "len"]


def test_t6c1_builds_no_decision_and_calls_no_service():
    """T6-C1 ne construit ni InferenceDecision ni StageClaimDecision et
    n'appelle aucune opération de T6-B."""
    for path in (ENGINE_PATH, POLICIES_PATH):
        tokens = _code_tokens(path.read_text(encoding="utf-8")).split("\n")
        for name in ("InferenceDecision", "StageClaimDecision", "TensionDecision", "BasisRefDecision",
                     "start_competency_inference", "complete_competency_inference", "get_inference_context",
                     "fail_competency_inference", "current_stage", "confidence_profile", "tension_state",
                     "validation_needs", "transition_cause", "unresolved_revision_context"):
            assert name not in tokens, (path.name, name)
    assert _imports(REPO_ROOT / "core" / "inference_service.py").isdisjoint(
        {"core.inference_policies", "core.inference_positive_basis"})


# --------------------------------------------------------------------------
# Contexte V1 : fail closed
# --------------------------------------------------------------------------

def _simple():
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B")
    return d


@pytest.mark.parametrize("version", [1, 3, "2", True])
def test_input_schema_version_other_than_2_fails_closed(version):
    """26."""
    with pytest.raises(UnsupportedInferenceContext, match="input_schema_version"):
        _simple().assess(input_schema_version=version)


@pytest.mark.parametrize("field", ["model_id", "prompt_spec_version"])
def test_model_or_prompt_metadata_fails_closed(field):
    """27-28."""
    with pytest.raises(UnsupportedInferenceContext, match="déterministe"):
        _simple().assess(**{field: "x-1"})


@pytest.mark.parametrize("version", ["positive_basis-2", "positive_basis-0", "positive_basis", "", None,
                                     "POSITIVE_BASIS-1"])
def test_unknown_positive_basis_version_fails_closed(version):
    """29 : aucune policy par défaut."""
    with pytest.raises(UnsupportedPositiveBasisPolicy):
        _simple().assess(positive_basis_version=version)


def test_context_type_is_required():
    with pytest.raises(UnsupportedInferenceContext):
        evaluate_positive_basis(SimpleNamespace(**{f.name: getattr(_simple().context(), f.name)
                                                   for f in dataclasses.fields(InferenceContext)}))


def test_errors_are_a_dedicated_business_hierarchy():
    for exc in (UnsupportedInferenceContext, UnsupportedPositiveBasisPolicy, UnsupportedTaxonomySemantics,
                InconsistentInferenceContext):
        assert issubclass(exc, PositiveBasisError)
    assert not issubclass(PositiveBasisError, (svc.InferenceServiceError, view.LongitudinalViewError, ValueError))


def test_same_capability_code_with_unknown_semantic_revision_fails_closed():
    """24 : C10_B r2 n'est jamais présumée signifier C10_B r1."""
    d = Dossier("C10", revisions={"C10_B": 2})
    d.observe("e1", "C10_B", "C10_A")
    with pytest.raises(UnsupportedTaxonomySemantics, match="C10_B semantic_revision 2"):
        d.assess()


def test_future_release_reusing_v1_definitions_is_compatible():
    """25 : autre release, autres memberships, mêmes definition_id V1."""
    def build(release):
        d = Dossier("C10", release=release)
        a = d.observe("e1", "C10_B", "C10_A", name="a")
        b = d.observe("e2", "C10_B", "C10_A", name="b")
        d.transfer(a, b)
        d.observe("e3", "C10_C", stage="comprehension", strength="medium", name="c")
        return d.assess()

    future = uuid.uuid5(NS, "release:oryx-v2")
    assert build(future) == build(RELEASE)
    assert claim(build(future), "mastery").status == EST


@pytest.mark.parametrize("case", ["missing", "unexpected", "duplicate", "revision_zero"])
def test_taxonomy_mismatch_fails_closed(case):
    d = Dossier("C10")
    d.observe("e1", "C10_B", "C10_A")
    if case == "missing":
        context = taxonomy("C10", codes=("C10_A", "C10_B", "C10_C"))
    elif case == "unexpected":
        context = taxonomy("C10", extra=(CurrentTaxonomyCapability(
            membership_id=uuid.uuid4(), definition_id=definition_id("C11_A"), capability_code="C11_A",
            semantic_revision=1, label="x"),))
    elif case == "duplicate":
        context = taxonomy("C10", extra=(taxonomy("C10").capabilities[0],))
    else:
        context = taxonomy("C10", revisions={"C10_D": 0})
    with pytest.raises(UnsupportedTaxonomySemantics):
        d.assess(taxonomy_context=context)


@pytest.mark.parametrize("case", ["competency", "release", "dossier_competency"])
def test_divergent_context_fails_closed(case):
    d = _simple()
    if case == "competency":
        kwargs = {"taxonomy_context": taxonomy("C3")}
    elif case == "release":
        kwargs = {"taxonomy_context": taxonomy("C2", release=uuid.uuid5(NS, "other"))}
    else:
        kwargs = {"competency_code": "C3", "taxonomy_context": taxonomy("C3")}
    with pytest.raises(InconsistentInferenceContext):
        d.assess(**kwargs)


def test_observation_never_crosses_competencies():
    """Une observation d'une autre compétence, ou localisée sur une capacité
    d'une autre compétence, est refusée : aucune propagation."""
    d = Dossier("C7")
    d.observe("e1", "C7_A", "C7_B")
    d.observe("e2", "C8_A", "C8_B", competency="C8")
    with pytest.raises(InconsistentInferenceContext, match="jamais de traversée"):
        d.assess()
    d = Dossier("C7")
    d.observe("e1", "C7_A", "C8_A")
    with pytest.raises(InconsistentInferenceContext, match="C8_A hors de la taxonomie"):
        d.assess()


def test_predecessor_and_causality_are_never_read():
    """Le predecessor et la causalité sont historiques : même des valeurs
    opaques n'influencent rien (le moteur ne les lit jamais)."""
    d = _simple()
    baseline = d.assess()
    opaque = object()
    assert d.assess(predecessor=opaque, predecessor_decision_context=opaque, transition_causality=opaque) == baseline


# --------------------------------------------------------------------------
# Déterminisme
# --------------------------------------------------------------------------

def _rich():
    d = Dossier("C12")
    a = d.observe("e1", "C12_A", "C12_B", name="a")
    b = d.observe("e2", "C12_B", "C12_C", name="b", support="hinted")
    d.transfer(a, b, caps=["C12_B"])
    d.observe("e3", "C12_B", stage="comprehension", strength="medium", name="c")
    d.observe("e4", "C12_A", stage="discovery", strength="weak", name="d")
    d.observe("e5", stage="application", strength="medium", name="e")
    k = d.contra("e6", "C12_C", name="k")
    r = d.observe("e7", "C12_C", "C12_D", name="r")
    d.revalidation(k, r)
    return d


def test_same_context_same_assessment_ten_times():
    """30."""
    context = _rich().context()
    results = [evaluate_positive_basis(context) for _ in range(10)]
    assert all(result == results[0] for result in results)
    assert hash(repr(results[0])) == hash(repr(evaluate_positive_basis(context)))


def test_result_does_not_depend_on_input_collection_order():
    d = _rich()
    assert d.assess(reverse=True) == d.assess()
    taxonomy_context = taxonomy("C12")
    shuffled = dataclasses.replace(taxonomy_context, capabilities=tuple(reversed(taxonomy_context.capabilities)))
    assert d.assess(taxonomy_context=shuffled) == d.assess()


def test_canonical_orders():
    result = _rich().assess()
    assert tuple(c.stage for c in result.claims) == STAGES
    order = [definition_id(c) for c in competency_codes("C12")]
    for c in result.claims:
        assert list(c.basis_observation_ids) == sorted(c.basis_observation_ids, key=str)
        assert list(c.represented_capability_definition_ids) == sorted(c.represented_capability_definition_ids,
                                                                       key=order.index)
        assert list(c.structural_basis_refs) == sorted(c.structural_basis_refs,
                                                       key=lambda r: (r.relation_kind, str(r.relation_id)))


def test_technical_time_alone_has_no_effect():
    """21 : décaler les horodatages techniques (des années) ne change rien."""
    def build(clock):
        d = Dossier("C2", clock=clock)
        a = d.observe("e1", "C2_A", "C2_B", name="a")
        b = d.observe("e2", "C2_A", "C2_C", name="b")
        d.transfer(a, b, caps=["C2_A"])
        return d.assess()

    assert build(EPOCH) == build(EPOCH + timedelta(days=3650)) == build(EPOCH - timedelta(days=900))


# --------------------------------------------------------------------------
# Claims directes, implication, allocation
# --------------------------------------------------------------------------

def test_new_expert_first_representative_application_is_established_immediately():
    """1-3 : une première démonstration représentative établit Application
    sans progression séquentielle ; les inférieures sont implied."""
    d = Dossier("C2")
    o = d.observe("e1", "C2_A", "C2_B")
    result = d.assess()
    assert statuses(result) == {"discovery": IMPLIED, "comprehension": IMPLIED, "application": DIRECT,
                                "mastery": NONE}
    app = claim(result, "application")
    assert app.basis_observation_ids == (o,)
    assert app.representativeness_reason == "single_representative_demonstration"
    assert app.representative_pattern_names == ("c2_risk_nature_related_to_horizon_or_capacity",)
    assert app.represented_capability_definition_ids == (definition_id("C2_A"), definition_id("C2_B"))
    assert result.highest_established_stage == "application"
    for stage in ("discovery", "comprehension"):
        implied = claim(result, stage)
        assert implied.basis_observation_ids == () and implied.representativeness_reason == "implied_by_higher_claim"
        assert implied.implied_from_stage == "application"


def test_comprehension_can_be_the_first_claim_without_discovery_evidence():
    """2 : aucune progression séquentielle imposée."""
    d = Dossier("C4")
    d.observe("e1", "C4_A", "C4_B", stage="comprehension", strength="medium")
    assert statuses(d.assess()) == {"discovery": IMPLIED, "comprehension": DIRECT, "application": NONE,
                                    "mastery": NONE}


def test_distinct_direct_bases_stay_direct():
    """4 : Application directe via O42, Comprehension directe via O17,
    Discovery implied (exemple de la spécification)."""
    d = Dossier("C2")
    o17 = d.observe("e17", "C2_A", "C2_B", stage="comprehension", strength="medium", name="O17")
    o42 = d.observe("e42", "C2_A", "C2_B", name="O42")
    result = d.assess()
    assert statuses(result) == {"discovery": IMPLIED, "comprehension": DIRECT, "application": DIRECT,
                                "mastery": NONE}
    assert claim(result, "application").basis_observation_ids == (o42,)
    assert claim(result, "comprehension").basis_observation_ids == (o17,)


def test_implied_claims_keep_the_represented_scope_of_application_without_basis():
    """Application directe localisée : Comprehension / Discovery implied
    reprennent son périmètre représenté, zéro basis_observation_ids."""
    d = Dossier("C4")
    o = d.observe("e1", "C4_B", "C4_D", name="o")
    result = d.assess()
    app = claim(result, "application")
    assert app.basis_mode == "direct" and app.basis_observation_ids == (o,)
    for stage in ("discovery", "comprehension"):
        implied = claim(result, stage)
        assert (implied.status, implied.basis_mode, implied.implied_from_stage) == (
            EST, "implied_by_higher_claim", "application")
        assert implied.basis_observation_ids == () and implied.structural_basis_refs == ()
        assert implied.represented_capability_definition_ids == (definition_id("C4_B"), definition_id("C4_D"))
        assert implied.representative_pattern_names == app.representative_pattern_names
        assert implied.competency_only_observation_ids == () and implied.limitations == ()
    assert _all_basis_ids(result) == [o]


def test_claims_implied_by_mastery_keep_its_represented_scope():
    d, a, b, t = _mastery_dossier()
    result = d.assess()
    mastery = claim(result, "mastery")
    assert mastery.basis_mode == "direct"
    for stage in ("discovery", "comprehension", "application"):
        implied = claim(result, stage)
        assert implied.basis_mode == "implied_by_higher_claim" and implied.implied_from_stage == "mastery"
        assert implied.basis_observation_ids == ()
        assert implied.represented_capability_definition_ids == mastery.represented_capability_definition_ids == (
            definition_id("C2_A"), definition_id("C2_B"))
    ids = _all_basis_ids(result)
    assert sorted(ids, key=str) == sorted({a, b}, key=str) and len(ids) == len(set(ids))


def test_implied_scope_comes_from_the_nearest_direct_higher_claim():
    """Comprehension directe distincte sous Application : Discovery reprend
    le périmètre de Comprehension (la claim directe la plus proche)."""
    d = Dossier("C2")
    d.observe("e17", "C2_A", "C2_C", stage="comprehension", name="O17")
    d.observe("e42", "C2_A", "C2_B", name="O42")
    discovery = claim(d.assess(), "discovery")
    assert discovery.implied_from_stage == "comprehension"
    assert discovery.represented_capability_definition_ids == (definition_id("C2_A"), definition_id("C2_C"))


def test_competency_only_higher_claim_keeps_competency_only_nature_when_implied():
    d = Dossier("C10")
    o = d.observe("e1", name="o")
    result = d.assess()
    for stage in ("discovery", "comprehension"):
        implied = claim(result, stage)
        assert implied.basis_mode == "implied_by_higher_claim" and implied.implied_from_stage == "application"
        assert implied.represented_capability_definition_ids == ()  # aucune capacité inventée
        assert implied.competency_only_observation_ids == (o,)
        assert implied.basis_observation_ids == ()
    assert _all_basis_ids(result) == [o]


def _all_basis_ids(result):
    return [i for c in result.claims for i in c.basis_observation_ids]


@pytest.mark.parametrize("builder", [_rich, _simple])
def test_an_observation_is_positive_basis_of_one_claim_only(builder):
    """5 : jamais recopiée sur les claims implied, jamais trois claims."""
    result = builder().assess()
    ids = _all_basis_ids(result)
    assert len(ids) == len(set(ids))
    for c in result.claims:
        if c.basis_mode != "direct":
            assert c.basis_observation_ids == () and c.structural_basis_refs == ()


def test_invariants_of_every_claim():
    for builder in (_rich, _simple, lambda: Dossier("C5")):
        result = builder().assess()
        seen_established = False
        for c in reversed(result.claims):
            if c.status == NOT:
                assert c.basis_mode == "none" and c.basis_observation_ids == ()
            if c.basis_mode == "implied_by_higher_claim":
                assert seen_established and c.stage != "mastery"
                source = claim(result, c.implied_from_stage)
                assert source.basis_mode == "direct" and STAGES.index(source.stage) > STAGES.index(c.stage)
                assert c.represented_capability_definition_ids == source.represented_capability_definition_ids
                assert c.basis_observation_ids == () and c.structural_basis_refs == ()
            else:
                assert c.implied_from_stage is None
            if c.basis_mode == "direct":
                assert c.basis_observation_ids
            seen_established = seen_established or c.status == EST
            assert (c.mastery_assessment is not None) == (c.stage == "mastery")
        established = [c.stage for c in result.claims if c.status == EST]
        assert established == list(STAGES[:len(established)])  # monotone
        assert result.highest_established_stage == (established[-1] if established else None)


def test_repeated_local_stage_none_establishes_nothing():
    """6 : aucune accumulation de local_stage none, même strong."""
    d = Dossier("C3")
    for i in range(8):
        d.observe(f"e{i}", "C3_B", "C3_A", stage="none")
        d.observe(f"f{i}", stage="none")
    result = d.assess()
    assert result.highest_established_stage is None
    assert all(c.status == NOT for c in result.claims)
    assert claim(result, "discovery").representativeness_reason == "insufficient_positive_basis"


def test_strong_never_lifts_a_claim_above_local_stage():
    """7."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", stage="comprehension", strength="strong")
    d.observe("e2", "C2_A", "C2_C", stage="discovery", strength="strong")
    result = d.assess()
    assert statuses(result)["application"] == NONE
    assert statuses(result)["comprehension"] == DIRECT
    assert claim(result, "application").representativeness_reason == "insufficient_positive_basis"


def test_evidence_strength_is_never_an_eligibility_gate():
    """8 + correction d'audit : evidence_strength reste descriptive. Une
    démonstration représentative de profondeur suffisante établit la claim
    quelle que soit sa force (ni « weak => incapable seule », ni
    « medium / strong => capable seule ») ; la force reste projetée pour la
    confiance future."""
    for stage in ("discovery", "comprehension", "application"):
        results = {}
        for strength in ("weak", "medium", "strong"):
            d = Dossier("C3")
            o = d.observe("e1", "C3_B", stage=stage, strength=strength, name="o")
            result = d.assess()
            assert claim(result, stage).basis_mode == "direct", (stage, strength)
            assert claim(result, stage).basis_observation_ids == (o,)
            results[strength] = result
        assert results["weak"] == results["medium"] == results["strong"], stage
    d = Dossier("C10")
    d.observe("e1", strength="weak")
    assert claim(d.assess(), "application").representativeness_reason == "competency_only_representative_demonstration"
    evidence = project_positive_evidence(d.context(), resolve_positive_basis_policy(d.context()))
    assert [x.evidence_strength for x in evidence.demonstrations] == ["weak"]
    assert not hasattr(engine_module, "SELF_STANDING_STRENGTHS")


def _independent_pair(stage, *, revalidated=True):
    """C2_A puis C2_C démontrés dans deux épisodes distincts ; si
    revalidated, chacun est la cible d'une revalidation T5 sur ce périmètre
    (independence_evidence établie par T5)."""
    d = Dossier("C2")
    a = d.observe("e2", "C2_A", stage=stage, strength="weak", name="a")
    c = d.observe("e4", "C2_C", stage=stage, strength="weak", name="c")
    if revalidated:
        d.revalidation(d.contra("e1", "C2_A", name="ka"), a)
        d.revalidation(d.contra("e3", "C2_C", name="kc"), c)
    return d, a, c


@pytest.mark.parametrize("stage", ["comprehension", "application"])
def test_complementary_history_with_established_independence_can_establish(stage):
    """9 : épisodes distincts, convergents et complémentaires dont T5 établit
    l'indépendance sur les périmètres apportés : base collective."""
    d, a, c = _independent_pair(stage)
    readings = {(r.observation_id, r.classification)
                for r in d.context().longitudinal_dossier.dependency_profile.scope_readings}
    assert {(a, "independence_evidence"), (c, "independence_evidence")} <= readings
    result = d.assess()
    target = claim(result, stage)
    assert (target.status, target.basis_mode) == DIRECT
    assert target.representativeness_reason == "complementary_representative_history"
    assert set(target.basis_observation_ids) == {a, c}
    assert target.representative_pattern_names == ("c2_risk_nature_related_to_horizon_or_capacity",)
    assert "independence_not_established" not in [lim.code for lim in target.limitations]


@pytest.mark.parametrize("stage", ["comprehension", "application"])
def test_same_complementary_history_without_established_independence_does_not_establish(stage):
    """Même histoire, lectures T5 not_established (aucune arête) : aucune
    indépendance présumée ; claim non établie par cette voie, raison
    explicable, aucune fraction."""
    d, a, c = _independent_pair(stage, revalidated=False)
    target = claim(d.assess(), stage)
    assert target.status == NOT and target.basis_mode == "none"
    assert target.representativeness_reason == "independence_not_established"
    [limitation] = target.limitations
    assert limitation.code == "independence_not_established"
    assert set(limitation.observation_ids) == {a, c}
    assert limitation.pattern_names == ("c2_risk_nature_related_to_horizon_or_capacity",)
    # Une seule contribution indépendante ne suffit pas non plus.
    d, a, c = _independent_pair(stage, revalidated=False)
    d.revalidation(d.contra("e1", "C2_A", name="ka"), a)
    target = claim(d.assess(), stage)
    assert target.status == NOT and target.limitations[0].observation_ids == (c,)


def test_single_representative_application_needs_no_independence_evidence():
    """Une première démonstration unique représentative établit Application
    sans répétition ni indépendance : l'indépendance n'est jamais une
    barrière cachée de la voie A."""
    d = Dossier("C2")
    o = d.observe("e1", "C2_A", "C2_B", strength="weak")
    readings = {r.classification for r in d.context().longitudinal_dossier.dependency_profile.scope_readings}
    assert readings == {"not_established"}
    app = claim(d.assess(), "application")
    assert (app.status, app.basis_mode, app.basis_observation_ids) == (EST, "direct", (o,))
    assert app.representativeness_reason == "single_representative_demonstration"


def test_redundant_repetition_of_the_same_micro_knowledge_does_not_progress():
    """10 : répéter un même micro-savoir n'étend jamais le périmètre : même
    statut qu'une seule démonstration, jamais un stade de plus."""
    def statuses_for(repetitions, competency, caps, stage):
        d = Dossier(competency)
        for i in range(repetitions):
            d.observe(f"e{i}", *caps, stage=stage, strength="weak")
        return statuses(d.assess())

    for competency, caps, stage in (("C2", ["C2_A"], "comprehension"), ("C1", ["C1_B"], "application"),
                                    ("C10", ["C10_B"], "application")):
        once = statuses_for(1, competency, caps, stage)
        assert statuses_for(6, competency, caps, stage) == once, competency
        assert once[stage] == NONE, competency
    d = Dossier("C2")
    for i in range(6):
        d.observe(f"e{i}", "C2_A", stage="comprehension", strength="strong")
    assert claim(d.assess(), "comprehension").representativeness_reason == "localized_scope_not_representative"


def test_sibling_observations_widen_coverage_but_never_independence():
    """11 : observations sœurs d'un même CognitiveEvent (quelle que soit
    leur force) : couverture élargie, jamais indépendance, transfert ni
    durabilité."""
    for strength in ("weak", "medium"):
        d = Dossier("C2")
        a = d.observe("e1", "C2_A", strength=strength, name="a")
        b = d.observe("e1", "C2_B", strength=strength, name="b")
        result = d.assess()
        app = claim(result, "application")
        assert app.representativeness_reason == "single_representative_episode"
        assert set(app.basis_observation_ids) == {a, b}
        mastery = claim(result, "mastery").mastery_assessment
        for prop in ("independent_repetition", "variety_transfer", "longitudinality"):
            assert getattr(mastery, prop).status == "not_demonstrated", prop


def test_absence_of_dependency_edge_is_never_independence():
    """12."""
    d = Dossier("C2")
    for i in range(10):
        d.observe(f"e{i}", "C2_A", "C2_B")
    result = d.assess()
    mastery = claim(result, "mastery")
    assert mastery.status == NOT
    assert mastery.mastery_assessment.independent_repetition.status == "not_demonstrated"
    assert mastery.mastery_assessment.independent_repetition.reason_codes == ("no_t5_independent_remobilization",)
    # Histoire complémentaire sans arête : jamais établie, indépendance
    # signalée non établie.
    d = Dossier("C2")
    d.observe("e1", "C2_A")
    d.observe("e2", "C2_B")
    app = claim(d.assess(), "application")
    assert app.status == NOT and app.representativeness_reason == "independence_not_established"


@pytest.mark.parametrize("kind", ["dependent", "partially_dependent"])
def test_established_dependency_never_counts_as_a_complementary_contribution(kind):
    """dependent / partially_dependent ne suffisent jamais pour la voie B ;
    sur une claim directe (voie A), la dépendance est seulement signalée."""
    d = Dossier("C2")
    a = d.observe("e1", "C2_A", name="a")
    b = d.observe("e2", "C2_B", name="b")
    d.revalidation(d.contra("e0", "C2_A", name="ka"), a)
    d.dependency(b, a, kind=kind)
    app = claim(d.assess(), "application")
    assert app.status == NOT and app.representativeness_reason == "independence_not_established"
    assert app.limitations[0].observation_ids == (b,)
    d = Dossier("C2")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    b = d.observe("e2", "C2_A", "C2_B", stage="comprehension", strength="medium", name="b")
    d.dependency(b, a, kind=kind)
    comprehension = claim(d.assess(), "comprehension")
    assert comprehension.basis_mode == "direct"  # jamais une barrière cachée à une claim directe
    assert [lim.code for lim in comprehension.limitations] == ["dependency_limited_demonstration"]


def test_c10b_local_application_is_not_global_c10_application():
    """13-14 : C10_B strong en Application locale : jamais C10 Application
    globale ; elle peut porter une Comprehension globale."""
    d = Dossier("C10")
    o = d.observe("e1", "C10_B", strength="strong")
    result = d.assess()
    assert statuses(result) == {"discovery": IMPLIED, "comprehension": DIRECT, "application": NONE,
                                "mastery": NONE}
    app = claim(result, "application")
    assert app.representativeness_reason == "localized_scope_not_representative"
    [limitation] = app.limitations
    assert limitation.pattern_names == ("c10_implied_assumptions_only",)
    assert limitation.generalization_limits == ("c10_local_valuation_step_does_not_generalize_valuation",)
    assert limitation.observation_ids == (o,)
    comprehension = claim(result, "comprehension")
    assert comprehension.basis_observation_ids == (o,)
    assert comprehension.representative_pattern_names == ("c10_price_depends_on_assumptions",)


def test_competency_only_establishes_without_inventing_capabilities():
    """15."""
    d = Dossier("C10")
    o = d.observe("e1", strength="strong")
    result = d.assess()
    app = claim(result, "application")
    assert app.basis_mode == "direct"
    assert app.representativeness_reason == "competency_only_representative_demonstration"
    assert app.represented_capability_definition_ids == ()
    assert app.competency_only_observation_ids == (o,)
    assert [lim.code for lim in app.limitations] == ["competency_only_scope_not_localizable"]


def test_competency_only_observations_never_accumulate():
    """16 : plusieurs competency_only (weak) ne s'additionnent jamais : même
    statut qu'une seule, jamais un stade au-delà de leur profondeur."""
    def build(repetitions, stage):
        d = Dossier("C10")
        for i in range(repetitions):
            d.observe(f"e{i}", stage=stage, strength="weak")
        return statuses(d.assess())

    for stage in ("discovery", "comprehension"):
        assert build(7, stage) == build(1, stage)
    assert build(7, "discovery") == {"discovery": DIRECT, "comprehension": NONE, "application": NONE,
                                     "mastery": NONE}


def test_adding_only_contradictions_leaves_positive_basis_identical():
    """17 : contradictions localisées, competency_only, dans les mêmes
    épisodes ou non : résultat strictement identique."""
    d = _rich()
    baseline = d.assess()
    d.contra("e1", "C12_A")
    d.contra("e2", "C12_B", "C12_C", strength="strong")
    d.contra("e20", scope="recognition")
    d.contra("e21", "C12_D", strength="weak")
    d.contra("e22", "C12_B", scope="comprehension")
    assert d.assess() == baseline
    d = _simple()
    baseline = d.assess()
    for i in range(5):
        d.contra(f"k{i}", "C2_A", "C2_B")
    assert d.assess() == baseline


# --------------------------------------------------------------------------
# Mastery
# --------------------------------------------------------------------------

def _mastery_dossier(competency="C2", caps=("C2_A", "C2_B"), **target):
    d = Dossier(competency)
    a = d.observe("e1", *caps, name="m-a")
    b = d.observe("e2", *caps, name="m-b", **target)
    t = d.transfer(a, b)
    return d, a, b, t


def test_single_brilliant_observation_is_never_mastery():
    """18."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", "C2_C", strength="strong", elicitation="spontaneous")
    mastery = claim(d.assess(), "mastery")
    assert mastery.status == NOT and mastery.representativeness_reason == "insufficient_longitudinal_structure"


def test_mastery_without_prior_contradiction_is_possible():
    """19 + 22 : aucune contradiction historique, aucun horodatage de
    démonstration exact : les cinq propriétés sont soutenues par la
    structure T5 (transfert = remobilisation indépendante, variation,
    durabilité, adaptation)."""
    d, a, b, t = _mastery_dossier()
    context = d.context()
    assert context.longitudinal_dossier.temporal_validation_profile.exact_demonstration_time_available is False
    assert not context.longitudinal_dossier.consistency_profile.historical_contradictions
    result = evaluate_positive_basis(context)
    assert statuses(result) == {"discovery": IMPLIED, "comprehension": IMPLIED, "application": IMPLIED,
                                "mastery": DIRECT}
    mastery = claim(result, "mastery")
    assert mastery.representativeness_reason == "longitudinal_mastery_history"
    assert set(mastery.basis_observation_ids) == {a, b}
    assert mastery.structural_basis_refs == (StructuralRef(relation_kind="transfer", relation_id=t),)
    assessment = mastery.mastery_assessment
    assert assessment.schema_version == "mastery-assessment-v1"
    assert assessment.robustness_revision.reason_codes == ("t5_transfer_adaptation",)
    assert assessment.represented_capability_definition_ids == (definition_id("C2_A"), definition_id("C2_B"))
    for prop in engine_module.MASTERY_PROPERTIES:
        assert getattr(assessment, prop).status == "supported", prop
    assert result.highest_established_stage == "mastery"
    # Anti-double-comptage : a et b ne sont base que de Mastery.
    assert _all_basis_ids(result) == list(mastery.basis_observation_ids)


def test_mastery_requires_every_property_and_no_partial_mastery():
    """Une propriété manquante => not_established, sans stade intermédiaire
    (ici autonomy : la cible remobilisée est answer_given)."""
    d, *_ = _mastery_dossier(support="answer_given")
    result = d.assess()
    mastery = claim(result, "mastery")
    assert mastery.status == NOT and mastery.basis_mode == "none"
    assert mastery.representativeness_reason == "insufficient_longitudinal_structure"
    assert mastery.mastery_assessment.autonomy.status == "not_demonstrated"
    assert mastery.mastery_assessment.autonomy.reason_codes == ("autonomy_not_demonstrated_on_representative_scope",)
    for prop in ("independent_repetition", "variety_transfer", "longitudinality", "robustness_revision"):
        assert getattr(mastery.mastery_assessment, prop).status == "supported", prop
    assert claim(result, "application").basis_mode == "direct"


@pytest.mark.parametrize("support", ["none", "hinted", "guided"])
def test_guided_is_never_an_automatic_autonomy_ceiling(support):
    """support_level n'est ni un coefficient ni un plafond : une cible guided
    sur un périmètre remobilisé sans dépendance T5 peut porter l'autonomie."""
    d, a, b, t = _mastery_dossier(support=support)
    mastery = claim(d.assess(), "mastery")
    assert mastery.mastery_assessment.autonomy.status == "supported"
    assert mastery.mastery_assessment.autonomy.observation_ids == tuple(sorted((a, b), key=str))
    assert mastery.status == EST


def test_answer_given_alone_does_not_demonstrate_autonomy():
    d, *_ = _mastery_dossier(support="answer_given")
    assert claim(d.assess(), "mastery").mastery_assessment.autonomy.status == "not_demonstrated"


@pytest.mark.parametrize("kind", ["dependent", "partially_dependent"])
def test_dependency_on_the_remobilized_scope_blocks_autonomy_on_that_scope(kind):
    """Dépendance T5 d'une extrémité positive de la relation sur le
    périmètre remobilisé : autonomie non démontrée sur ce périmètre (ici la
    source du transfert dépend d'une démonstration antérieure)."""
    d = Dossier("C2")
    z = d.observe("e0", "C2_A", "C2_B", name="z")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    b = d.observe("e2", "C2_A", "C2_B", name="b")
    d.dependency(a, z, kind=kind)
    d.transfer(a, b)
    mastery = claim(d.assess(), "mastery")
    assert mastery.mastery_assessment.autonomy.status == "not_demonstrated"
    assert mastery.status == NOT
    # Dépendance limitée à C2_B : le périmètre autonome restant (C2_A) n'est
    # plus représentatif de C2.
    d = Dossier("C2")
    z = d.observe("e0", "C2_A", "C2_B", name="z")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    b = d.observe("e2", "C2_A", "C2_B", name="b")
    d.dependency(a, z, kind=kind, caps=["C2_B"])
    d.transfer(a, b)
    assert claim(d.assess(), "mastery").mastery_assessment.autonomy.status == "not_demonstrated"


def test_mastery_scope_must_remain_representative():
    """Transfert limité au micro-périmètre C10_B : pas de Mastery C10, même
    avec une Application représentative établie."""
    d = Dossier("C10")
    a = d.observe("e1", "C10_B", "C10_A", name="a")
    b = d.observe("e2", "C10_B", "C10_C", name="b")
    d.transfer(a, b, caps=["C10_B"])
    result = d.assess()
    assert claim(result, "application").status == EST
    mastery = claim(result, "mastery")
    assert mastery.status == NOT
    assert mastery.representativeness_reason == "longitudinal_scope_not_representative"
    assert "longitudinal_scope_not_representative" in [lim.code for lim in mastery.limitations]


def test_mastery_requires_an_established_representative_application():
    d = Dossier("C10")
    a = d.observe("e1", "C10_B", name="a")
    b = d.observe("e2", "C10_B", name="b")
    d.transfer(a, b)
    mastery = claim(d.assess(), "mastery")
    assert mastery.status == NOT and mastery.representativeness_reason == "representative_application_not_established"


def test_no_number_of_observations_triggers_mastery():
    """20 : dix analyses excellentes sans structure T5 : Application solide,
    jamais Mastery."""
    d = Dossier("C4")
    for i in range(12):
        d.observe(f"e{i}", "C4_A", "C4_B", "C4_C", "C4_D", strength="strong", elicitation="spontaneous")
    result = d.assess()
    assert statuses(result)["application"] == DIRECT
    assert statuses(result)["mastery"] == NONE


def test_revalidation_contributes_to_robustness_and_repetition():
    d = Dossier("C2")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    k = d.contra("e2", "C2_A", "C2_B", name="k")
    r = d.observe("e3", "C2_A", "C2_B", name="r")
    rv = d.revalidation(k, r)
    mastery = claim(d.assess(), "mastery").mastery_assessment
    assert mastery.robustness_revision.reason_codes == ("t5_revalidation",)
    assert mastery.independent_repetition.reason_codes == ("t5_revalidation_remobilization",)
    assert mastery.longitudinality.structural_refs == (StructuralRef(relation_kind="revalidation", relation_id=rv),)
    assert mastery.variety_transfer.status == "not_demonstrated"  # aucun transfert : pas de Mastery
    assert k not in mastery.robustness_revision.observation_ids  # une contradiction n'est jamais base positive
    assert set(mastery.independent_repetition.observation_ids) == {a, r}


def test_c11d_rational_revision_contributes_to_robustness():
    """23 : C11_D (vraie révision de thèse) contribue à robustness_revision
    pour C11 ; une capacité non désignée par la policy ne le fait pas."""
    d = Dossier("C11")
    o = d.observe("e1", "C11_D", strength="strong")
    result = d.assess()
    assert claim(result, "application").representative_pattern_names == ("c11_rational_thesis_revision",)
    robustness = claim(result, "mastery").mastery_assessment.robustness_revision
    assert robustness.status == "supported"
    assert robustness.reason_codes == ("thesis_revision_demonstrated",)
    assert robustness.observation_ids == (o,)
    assert claim(result, "mastery").status == NOT
    d = Dossier("C10")
    d.observe("e1", "C10_D", "C10_B", strength="strong")
    assert claim(d.assess(), "mastery").mastery_assessment.robustness_revision.status == "not_demonstrated"


def test_c11_mastery_through_revision_and_transfer():
    d = Dossier("C11")
    a = d.observe("e1", "C11_A", "C11_B", name="a")
    b = d.observe("e2", "C11_A", "C11_B", "C11_D", name="b")
    d.transfer(a, b)
    result = d.assess()
    mastery = claim(result, "mastery")
    assert mastery.status == EST
    assert mastery.mastery_assessment.robustness_revision.reason_codes == ("t5_transfer_adaptation",
                                                                           "thesis_revision_demonstrated")


def test_competency_only_longitudinal_history_keeps_its_nature():
    d = Dossier("C9")
    a = d.observe("e1", name="a")
    b = d.observe("e2", name="b")
    d.transfer(a, b)
    mastery = claim(d.assess(), "mastery")
    assert mastery.status == EST
    assert mastery.represented_capability_definition_ids == ()
    assert set(mastery.competency_only_observation_ids) == {a, b}
    assert "competency_only_scope_not_localizable" in [lim.code for lim in mastery.limitations]


def test_later_inactivity_does_not_erase_a_demonstrated_mastery_basis():
    """Aucune décroissance : ajouter des épisodes sans rapport (même
    beaucoup plus tard) ne retire rien."""
    d, *_ = _mastery_dossier()
    before = claim(d.assess(), "mastery")
    d.clock = EPOCH + timedelta(days=2000)
    d.observe("late", "C2_C", stage="discovery", strength="medium")
    assert claim(d.assess(), "mastery") == before


# --------------------------------------------------------------------------
# Patterns C1-C12 (représentatif / trop étroit / aucune traversée)
# --------------------------------------------------------------------------

PATTERNS = {
    # competency: (comprehension rep, comprehension narrow, application rep, application narrow)
    "C1": ([["C1_A"], ["C1_C"]], [["C1_B"]], [["C1_A"], ["C1_C"]], [["C1_B"]]),
    "C2": ([["C2_A", "C2_B"], ["C2_A", "C2_C"]], [["C2_B"], ["C2_C"], ["C2_A"], ["C2_B", "C2_C"]],
           [["C2_A", "C2_B"], ["C2_A", "C2_C"]], [["C2_B"], ["C2_C"], ["C2_B", "C2_C"]]),
    "C3": ([["C3_B"]], [["C3_A"], ["C3_C"], ["C3_A", "C3_C"]], [["C3_B"]], [["C3_A"], ["C3_C"], ["C3_A", "C3_C"]]),
    "C4": ([["C4_A", "C4_B"]], [["C4_D"], ["C4_C"], ["C4_A"], ["C4_A", "C4_C", "C4_D"]],
           [["C4_B", "C4_A"], ["C4_B", "C4_C"], ["C4_B", "C4_D"]], [["C4_D"], ["C4_B"], ["C4_A", "C4_C", "C4_D"]]),
    "C5": ([["C5_A"]], [["C5_B"], ["C5_C"], ["C5_D"], ["C5_B", "C5_C", "C5_D"]],
           [["C5_B", "C5_A"], ["C5_B", "C5_C"], ["C5_B", "C5_D"]],
           [["C5_A"], ["C5_C", "C5_D"], ["C5_A", "C5_C", "C5_D"]]),
    "C6": ([["C6_A"]], [["C6_B"], ["C6_C"], ["C6_D"], ["C6_B", "C6_C", "C6_D"]],
           [["C6_B"]], [["C6_A"], ["C6_C"], ["C6_D"], ["C6_A", "C6_C", "C6_D"]]),
    "C7": ([["C7_A", "C7_C"], ["C7_A", "C7_B"]], [["C7_B"], ["C7_C"], ["C7_A", "C7_D"], ["C7_B", "C7_C", "C7_D"]],
           [["C7_A", "C7_B"], ["C7_A", "C7_C"], ["C7_A", "C7_D"]], [["C7_B"], ["C7_C"], ["C7_B", "C7_C", "C7_D"]]),
    "C8": ([["C8_A", "C8_B"]], [["C8_A"], ["C8_D"], ["C8_A", "C8_C", "C8_D"]],
           [["C8_B", "C8_A"], ["C8_B", "C8_C"]], [["C8_A"], ["C8_D"], ["C8_B", "C8_D"], ["C8_A", "C8_C", "C8_D"]]),
    "C9": ([["C9_A", "C9_B"]], [["C9_A"], ["C9_D"], ["C9_A", "C9_C", "C9_D"]],
           [["C9_B", "C9_C"]], [["C9_A", "C9_B"], ["C9_D"], ["C9_B", "C9_D"], ["C9_A"]]),
    "C10": ([["C10_B"]], [["C10_A"], ["C10_C"], ["C10_D"], ["C10_A", "C10_C", "C10_D"]],
            [["C10_B", "C10_A"], ["C10_B", "C10_C"], ["C10_B", "C10_D"]],
            [["C10_B"], ["C10_C"], ["C10_A", "C10_C", "C10_D"]]),
    "C11": ([["C11_A", "C11_B"]], [["C11_C"], ["C11_A"], ["C11_D"], ["C11_C", "C11_D"]],
            [["C11_A", "C11_B"], ["C11_D"]], [["C11_C"], ["C11_A"], ["C11_B", "C11_C"]]),
    "C12": ([["C12_B"]], [["C12_A"], ["C12_D"], ["C12_A", "C12_C", "C12_D"]],
            [["C12_A", "C12_B"], ["C12_B", "C12_C"], ["C12_C", "C12_D"]],
            [["C12_A"], ["C12_D"], ["C12_A", "C12_D"], ["C12_A", "C12_C"]]),
}
CASES = [(competency, stage, caps, expected)
         for competency, (comp_rep, comp_narrow, app_rep, app_narrow) in PATTERNS.items()
         for stage, rep, narrow in (("comprehension", comp_rep, comp_narrow), ("application", app_rep, app_narrow))
         for caps, expected in [*((c, True) for c in rep), *((c, False) for c in narrow)]]


def test_pattern_table_covers_every_competency_and_capability():
    assert sorted(PATTERNS, key=lambda c: int(c[1:])) == [f"C{i}" for i in range(1, 13)]
    for competency, groups in PATTERNS.items():
        used = {cap for group in groups for caps in group for cap in caps}
        assert used == set(competency_codes(competency)), competency


@pytest.mark.parametrize("competency,stage,caps,representative", CASES,
                         ids=[f"{c}-{s}-{'+'.join(x)}-{'rep' if r else 'narrow'}" for c, s, x, r in CASES])
def test_competency_patterns(competency, stage, caps, representative):
    """Un cas représentatif et un cas volontairement trop étroit par stade et
    par compétence : une démonstration strong à la profondeur du stade."""
    d = Dossier(competency)
    o = d.observe("e1", *caps, stage=stage, strength="strong")
    result = d.assess()
    target = claim(result, stage)
    if representative:
        assert (target.status, target.basis_mode) == DIRECT
        assert target.basis_observation_ids == (o,)
        assert target.representative_pattern_names
    else:
        assert target.status == NOT
        assert target.representativeness_reason == "localized_scope_not_representative"
        # Toujours une vraie reconnaissance : Discovery reste directement établie.
        assert claim(result, "discovery").status == EST


@pytest.mark.parametrize("competency", list(PATTERNS))
def test_no_cross_competency_propagation(competency):
    """La preuve représentative d'une compétence n'établit rien ailleurs :
    le dossier (et la policy résolue) d'une compétence ne voit jamais les
    preuves d'une autre, et une observation étrangère est refusée."""
    other = f"C{int(competency[1:]) % 12 + 1}"
    rep = PATTERNS[competency][2][0]
    d = Dossier(other)
    d.observe("e1", *rep, competency=competency)
    with pytest.raises(InconsistentInferenceContext):
        d.assess()
    d = Dossier(other)
    d.observe("e1", competency_codes(other)[0], stage="none")
    assert d.assess().highest_established_stage is None


def test_capability_count_never_decides():
    """Trois ou quatre capacités d'une même compétence ne suffisent pas là
    où une seule capacité structurante suffit (aucun comptage)."""
    d = Dossier("C4")
    d.observe("e1", "C4_A", "C4_C", "C4_D")
    assert claim(d.assess(), "application").status == NOT
    d = Dossier("C6")
    d.observe("e1", "C6_B")
    assert claim(d.assess(), "application").basis_mode == "direct"


# --------------------------------------------------------------------------
# Compatibilité avec la validation T6-B (adaptateur de TEST uniquement)
# --------------------------------------------------------------------------

def _decision_from(assessment):
    """Adaptateur de test : T6-C1 ne produit que la base positive ; les
    champs de T6-C2 / T6-C3 reçoivent ici des valeurs de remplissage."""
    claims, refs = [], []
    for c in assessment.claims:
        claims.append(StageClaimDecision(
            stage=c.stage, positive_basis_status=c.status, basis_mode=c.basis_mode,
            confidence_profile={"placeholder": "T6-C2"} if c.status == EST else None,
            mastery_assessment=None if c.stage != "mastery"
            else {"schema_version": c.mastery_assessment.schema_version}))
        refs.extend(BasisRefDecision(ref_role="positive_basis", source_kind="observation", source_id=i,
                                     claim_stage=c.stage) for i in c.basis_observation_ids)
        refs.extend(BasisRefDecision(ref_role="mastery", source_kind=r.relation_kind, source_id=r.relation_id,
                                     claim_stage="mastery") for r in c.structural_basis_refs)
    return InferenceDecision(current_stage=assessment.highest_established_stage or "non_etabli",
                             transition_cause=None, unresolved_revision_context=None, validation_needs=(),
                             state_decision_summary="adaptateur de test", claims=tuple(claims), tensions=(),
                             basis_refs=tuple(refs))


@pytest.mark.parametrize("builder", [_rich, _simple, lambda: _mastery_dossier()[0], lambda: Dossier("C1")])
def test_positive_basis_refs_satisfy_t6b_structural_validation(builder):
    """Une observation = une claim, direct => au moins une ref, implied =>
    aucune : _validated_decision (pure) de T6-B accepte la projection."""
    decision = _decision_from(builder().assess())
    svc._validated_decision(decision)


# --------------------------------------------------------------------------
# 2. Intégration PostgreSQL : InferenceContext réel -> T6-C1 -> T6-B
# --------------------------------------------------------------------------

def test_pg_real_inference_context_is_evaluated_and_accepted_by_t6b(Sessions):  # noqa: F811
    from core import longitudinal_service as t5
    from core import models
    from core import taxonomy_service as tax
    from tests.test_longitudinal_service import _t3, app, contra
    from tests.test_longitudinal_service import _start as start_t5
    from tests.test_taxonomy_service import _def_kwargs

    with Sessions() as session:
        release = tax.create_candidate_release(session, version_key=f"t6c1-{uuid.uuid4()}",
                                               spec_fingerprint=f"sha256:{uuid.uuid4()}")
        m = {}
        for code in competency_codes("C7"):
            existing = session.query(models.CoreCapabilityDefinition).filter_by(
                capability_code=code, semantic_revision=1).one_or_none()
            definition = existing or tax.create_capability_definition(session, **_def_kwargs(code, 1))
            m[code] = tax.attach_capability_to_release(session, release_id=release.id,
                                                       capability_definition_id=definition.id).id
        tax.activate_release(session, release_id=release.id)
        session.commit()
        release_id = release.id
    first = _t3(Sessions, release_id, app(m["C7_A"], m["C7_B"])).id
    second = _t3(Sessions, release_id, app(m["C7_A"], m["C7_C"])).id
    _t3(Sessions, release_id, contra(m["C7_A"]))
    parent = start_t5(Sessions, release_id)
    with Sessions() as s:
        t5.add_transfer(s, run_id=parent, source_observation_id=first, target_observation_id=second,
                        scope_mode="localized", transfer_basis={"adaptation": "configuration différente"},
                        capability_membership_ids=[m["C7_A"]])
        t5.complete_longitudinal_assessment(s, run_id=parent)
        s.commit()
    with Sessions() as session:
        context = svc.start_competency_inference(
            session, longitudinal_assessment_run_id=parent, trigger="longitudinal_completed",
            **{name: f"{name.split('_version')[0]}-1" for name in VERSIONS})
        session.commit()
    assert context.positive_basis_version == POSITIVE_BASIS_V1
    result = evaluate_positive_basis(context)
    assert result == evaluate_positive_basis(context)
    mastery = claim(result, "mastery")
    # C7_A reliée à C7_B / C7_C : Application représentative, transfert
    # localisé sur C7_A seul : périmètre remobilisé non représentatif.
    assert claim(result, "application").basis_mode == "direct"
    assert mastery.status == NOT
    assert mastery.representativeness_reason == "longitudinal_scope_not_representative"
    with Sessions() as session:
        svc.complete_competency_inference(session, run_id=context.run_id, decision=_decision_from(result))
        session.commit()
    with Sessions() as session:
        state = svc.get_validated_user_competency_state(session, user_id=context.user_id, competency_code="C7")
        assert state.current_stage == "application"
