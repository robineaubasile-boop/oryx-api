"""Tests de l'Étape 6.2C1 : calibration du support pédagogique
(core/adaptation_support.py).

1. Contrats : versions, vocabulaire d'allocation, dataclasses immuables,
   API publique, hiérarchie d'erreurs, proposition sans stade / score /
   rationale.
2. Préflight tâche <-> posture (6-2A / 6-2B) : appariement exact, jamais une
   posture recalculée à la place de 6-2B.
3. Préflight contexte <-> taxonomie (6-1E / 6-1B) et RestrictedSupportCatalogue :
   identité exacte, catalogue limité à la cible et aux supports de 6.1,
   safe_stages exacts, aucune contrainte de validation.
4. Statuts : no_task, resolved, neutral / ambiguous / composite.
5. Présupposés sûrs et ponts conceptuels.
6. Allocations et règles par posture.
7. Canonicalisation, fail-closed, déterminisme, immutabilité.
8. Projection de génération (aucun UUID, aucune provenance).
9. Cas frontières pédagogiques et chaîne réelle 6-1C -> 6-1D -> 6-1E -> 6-2C.
10. Contrat statique : pureté, isolation, aucune DB / migration / runtime,
    aucun score / niveau / trace de support réel.
"""
import ast
import dataclasses
import inspect
import re
import uuid
from types import MappingProxyType

import pytest

from core.adaptation_context import (
    PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION,
    CompetencyPedagogicalContext,
    CompetencyResponseContext,
    CompetencyStateProvenance,
    PedagogicalResponseContext,
    ResponseContextEnvelope,
    build_pedagogical_response_context,
)
from core.adaptation_focus import (
    FOCUS_POLICY_VERSION,
    FOCUS_SCHEMA_VERSION,
    CurrentFocusTaxonomy,
    FocusCapabilityDefinition,
    FocusCompetencyDefinition,
    FocusProposal,
    ProposedCompetencyFocus,
    validate_focus_proposal,
)
from core.adaptation_posture import (
    CHALLENGE,
    CO_REASON,
    EXPLAIN,
    GUIDE,
    InteractionPostureBaseline,
    build_posture_baseline,
)
from core.adaptation_safety import MinimalValidationConstraint, SafeAssumptionCoverage
from core.adaptation_support import (
    JOINT,
    OPERATION_ALLOCATIONS,
    ORYX,
    SUPPORT_PLAN_SCHEMA_VERSION,
    SUPPORT_POLICY_VERSION,
    USER_RESERVED,
    CatalogueCapability,
    CatalogueCompetency,
    ConceptualBridge,
    IncompatibleSupportPlanInputs,
    InteractionSupportPlan,
    InvalidSupportPlanArgument,
    InvalidSupportPlanContent,
    InvalidSupportPlanProposal,
    PlannedOperationAllocation,
    ProjectedCapability,
    ProjectedConceptualBridge,
    ProjectedSafeAssumption,
    ProposedOperationAllocation,
    ProposedPedagogicalReference,
    RelevantSafeAssumption,
    RestrictedSupportCatalogue,
    SegmentSupportPlan,
    SegmentSupportProjection,
    SegmentSupportProposal,
    SupportGenerationProjection,
    SupportPlanError,
    SupportPlanningInput,
    SupportPlanProposal,
    SupportSegmentInput,
    UnsupportedSupportPlanVersion,
    prepare_support_planning,
    project_support_plan,
    validate_support_plan_proposal,
)
from core.adaptation_task import (
    COGNITIVE_OPERATIONS,
    NO_TASK,
    TASK_POLICY_VERSION,
    TASK_REQUESTED,
    TASK_SCHEMA_VERSION,
    InteractionTaskProfile,
    TaskSegment,
)
from core.pedagogy.taxonomy_v1 import load_taxonomy_v1
from tests.test_adaptation_assumptions import FINGERPRINT, RELEASE, d, snap, state, uid
from tests.test_adaptation_safety import plan as safe_plan
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "adaptation_support.py"
D, DC, DCA, DCAM = (("discovery",), ("discovery", "comprehension"), ("discovery", "comprehension", "application"),
                    ("discovery", "comprehension", "application", "mastery"))


# --------------------------------------------------------------------------
# Constructeurs : taxonomie V1 (definition_id stables), contexte, tâche
# --------------------------------------------------------------------------

def make_taxonomy(release=RELEASE, ids=d):
    spec, fingerprint = load_taxonomy_v1()
    return CurrentFocusTaxonomy(
        taxonomy_release_id=release, taxonomy_schema_version=spec["taxonomy_schema_version"],
        taxonomy_version_key=spec["version_key"], taxonomy_spec_fingerprint=fingerprint,
        focus_policy_version=FOCUS_POLICY_VERSION,
        competencies=tuple(FocusCompetencyDefinition(competency_code=c["competency_code"], label=c["label"],
                                                     central_question=c["central_question"])
                           for c in spec["competencies"]),
        capabilities=tuple(FocusCapabilityDefinition(
            definition_id=ids(c["capability_code"]), capability_code=c["capability_code"],
            semantic_revision=c["semantic_revision"], competency_code=c["competency_code"], label=c["label"],
            definition=c["definition"], mapping_guidance=c["mapping_guidance"]) for c in spec["capabilities"]))


TAXONOMY = make_taxonomy()
SPEC = load_taxonomy_v1()[0]
SPEC_CAPABILITIES = {c["capability_code"]: c for c in SPEC["capabilities"]}
SPEC_COMPETENCIES = {c["competency_code"]: c for c in SPEC["competencies"]}


def provenance(code="C10"):
    return CompetencyStateProvenance(inference_run_id=uid(f"run-{code}"),
                                     longitudinal_assessment_run_id=uid(f"t5-{code}"), state_generation=3,
                                     taxonomy_release_id=RELEASE)


def part(role, code, *capabilities, stages=None, constraints=(), with_provenance=True):
    """capabilities : (code capacité, safe_stages) -> localized ; sinon
    competency_only avec stages."""
    if capabilities:
        ids = tuple(d(cap_code) for cap_code, _ in capabilities)
        coverages = tuple(SafeAssumptionCoverage(capability_definition_id=d(cap_code), safe_stages=tuple(st))
                          for cap_code, st in capabilities)
        mode = "localized"
    else:
        ids, mode = (), "competency_only"
        coverages = (SafeAssumptionCoverage(capability_definition_id=None, safe_stages=tuple(stages or ())),)
    return CompetencyResponseContext(
        provenance=provenance(code) if with_provenance else None,
        pedagogy=CompetencyPedagogicalContext(role=role, competency_code=code, scope_mode=mode,
                                              capability_definition_ids=ids, safe_assumptions=coverages,
                                              validation_constraints=tuple(constraints)))


def envelope(**overrides):
    kwargs = dict(plan_schema_version="safe-assumption-plan-v1", baseline_schema_version="assumption-baseline-v1",
                  focus_schema_version=FOCUS_SCHEMA_VERSION, focus_policy_version=FOCUS_POLICY_VERSION,
                  taxonomy_release_id=RELEASE, taxonomy_spec_fingerprint=FINGERPRINT)
    kwargs.update(overrides)
    return ResponseContextEnvelope(**kwargs)


def context(target=None, supporting=(), *, status="resolved", **envelope_overrides):
    return PedagogicalResponseContext(schema_version=PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION,
                                      envelope=envelope(**envelope_overrides), resolution_status=status,
                                      target=target, supporting=tuple(supporting))


def default_context():
    """Cible C10 (C10_B Discovery, C10_D sans stade), support C8
    competency_only Comprehension, support C9 localized C9_C Mastery."""
    return context(part("target", "C10", ("C10_B", D), ("C10_D", ())),
                   [part("supporting", "C8", stages=DC), part("supporting", "C9", ("C9_C", DCAM))])


def seg(excerpt="Explique-moi ce prix.", intent="unspecified", operations=("explain_mechanism",),
        characteristics=()):
    return TaskSegment(source_excerpt=excerpt, explicit_intent=intent, cognitive_operations=tuple(operations),
                       task_characteristics=tuple(characteristics))


def profile(*segments, status=TASK_REQUESTED):
    return InteractionTaskProfile(schema_version=TASK_SCHEMA_VERSION, policy_version=TASK_POLICY_VERSION,
                                  request_status=status, segments=tuple(segments))


GUIDE_SEG = seg("Aide-moi à calculer, ne me donne pas la réponse", "reasoning_reserved_for_user",
                ("apply_procedure", "calculate", "form_conclusion"))
JOINT_SEG = seg("Analyse avec moi", "joint_reasoning_requested",
                ("select_relevant_information", "interpret_evidence", "construct_reasoning", "form_conclusion"))
CHALLENGE_SEG = seg("Challenge ma thèse", "challenge_requested",
                    ("generate_hypotheses", "evaluate_existing_reasoning", "stress_test", "falsify"),
                    ("existing_reasoning_to_evaluate",))
EXPLAIN_SEG = seg("Donne-moi juste l'explication", "direct_answer_requested",
                  ("explain_mechanism", "interpret_evidence", "form_conclusion"))


def inputs(task=None, ctx=None, taxonomy=None, posture=None):
    task = task if task is not None else profile(EXPLAIN_SEG)
    return dict(task_profile=task, posture_baseline=posture if posture is not None else build_posture_baseline(task),
                context=ctx if ctx is not None else default_context(),
                taxonomy=taxonomy if taxonomy is not None else TAXONOMY)


def ref(code, *tokens, mode=None):
    mode = mode or ("localized" if tokens else "competency_only")
    return ProposedPedagogicalReference(competency_code=code, scope_mode=mode, capability_tokens=tuple(tokens))


def alloc(operation, allocation=ORYX):
    return ProposedOperationAllocation(operation=operation, allocation=allocation)


def sp(index=0, assumptions=(), bridges=(), allocations=()):
    return SegmentSupportProposal(segment_index=index, relevant_safe_assumptions=tuple(assumptions),
                                  conceptual_bridges=tuple(bridges), operation_allocations=tuple(allocations))


def proposal(*segments, **overrides):
    kwargs = dict(schema_version=SUPPORT_PLAN_SCHEMA_VERSION, policy_version=SUPPORT_POLICY_VERSION,
                  segments=tuple(segments))
    kwargs.update(overrides)
    return SupportPlanProposal(**kwargs)


def validate(prop, **kwargs):
    return validate_support_plan_proposal(prop, **inputs(**kwargs))


_DEFAULT = object()


def refused(error, prop=_DEFAULT, match=None, **kwargs):
    prop = proposal(sp()) if prop is _DEFAULT else prop
    with pytest.raises(error, match=match):
        validate(prop, **kwargs)


def catalogue_of(**kwargs):
    return prepare_support_planning(**inputs(**kwargs)).catalogue


def allocations_of(plan_, index=0):
    return [(a.operation, a.allocation) for a in plan_.segments[index].operation_allocations]


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_frozen_distinct_versions():
    assert SUPPORT_PLAN_SCHEMA_VERSION == "interaction-support-plan-v1"
    assert SUPPORT_POLICY_VERSION == "support-policy-1"
    assert SUPPORT_PLAN_SCHEMA_VERSION != SUPPORT_POLICY_VERSION


def test_allocation_vocabulary_is_closed():
    assert OPERATION_ALLOCATIONS == ("oryx", "user_reserved", "joint") == (ORYX, USER_RESERVED, JOINT)


def test_cognitive_operations_are_reused_from_6_2a_never_redefined():
    from core import adaptation_support
    assert adaptation_support.COGNITIVE_OPERATIONS is COGNITIVE_OPERATIONS
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets
                if isinstance(t, ast.Name)}
    assert not assigned & {"COGNITIVE_OPERATIONS", "POSTURES", "ASSUMPTION_STAGE_ORDER", "CHALLENGE_TASK_OPERATIONS"}
    constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is str}
    assert not constants & (set(COGNITIVE_OPERATIONS) | {"explain", "guide", "co_reason", "challenge", "discovery",
                                                         "comprehension", "application", "mastery"})


EXPECTED_FIELDS = {
    CatalogueCapability: ["definition_id", "capability_token", "label", "definition", "mapping_guidance",
                          "safe_stages"],
    CatalogueCompetency: ["role", "competency_code", "label", "central_question", "scope_mode", "capabilities",
                          "competency_only_safe_stages"],
    RestrictedSupportCatalogue: ["policy_version", "resolution_status", "competencies"],
    SupportSegmentInput: ["segment_index", "source_excerpt", "explicit_intent", "cognitive_operations",
                          "task_characteristics", "posture"],
    SupportPlanningInput: ["request_status", "segments", "catalogue"],
    ProposedPedagogicalReference: ["competency_code", "scope_mode", "capability_tokens"],
    ProposedOperationAllocation: ["operation", "allocation"],
    SegmentSupportProposal: ["segment_index", "relevant_safe_assumptions", "conceptual_bridges",
                             "operation_allocations"],
    SupportPlanProposal: ["schema_version", "policy_version", "segments"],
    RelevantSafeAssumption: ["competency_code", "capability_definition_id", "safe_stages"],
    ConceptualBridge: ["competency_code", "scope_mode", "capability_definition_ids"],
    PlannedOperationAllocation: ["operation", "allocation"],
    SegmentSupportPlan: ["segment_index", "source_excerpt", "posture", "relevant_safe_assumptions",
                         "conceptual_bridges", "operation_allocations"],
    InteractionSupportPlan: ["schema_version", "policy_version", "source_task_schema_version",
                             "source_task_policy_version", "source_posture_schema_version",
                             "source_posture_policy_version", "source_pedagogical_context_schema_version",
                             "request_status", "pedagogical_resolution_status", "segments"],
    ProjectedCapability: ["capability_token", "label", "definition"],
    ProjectedSafeAssumption: ["competency_code", "competency_label", "capability", "safe_stages"],
    ProjectedConceptualBridge: ["competency_code", "competency_label", "scope_mode", "capabilities"],
    SegmentSupportProjection: ["segment_index", "source_excerpt", "posture", "relevant_safe_assumptions",
                               "conceptual_bridges", "operation_allocations"],
    SupportGenerationProjection: ["schema_version", "policy_version", "request_status", "segments"],
}


def test_dataclasses_are_frozen_keyword_only_with_exact_fields():
    for cls, names in EXPECTED_FIELDS.items():
        assert [f.name for f in dataclasses.fields(cls)] == names, cls
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls)), cls


def test_public_api_is_preflight_validator_and_projection():
    from core import adaptation_support
    public = sorted(name for name, value in vars(adaptation_support).items()
                    if inspect.isfunction(value) and value.__module__ == adaptation_support.__name__
                    and not name.startswith("_"))
    assert public == ["prepare_support_planning", "project_support_plan", "validate_support_plan_proposal"]
    for function, names in ((prepare_support_planning, ["task_profile", "posture_baseline", "context", "taxonomy"]),
                            (validate_support_plan_proposal,
                             ["proposal", "task_profile", "posture_baseline", "context", "taxonomy"]),
                            (project_support_plan, ["plan", "catalogue"])):
        parameters = inspect.signature(function).parameters
        assert list(parameters) == names
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY
               for p in list(inspect.signature(validate_support_plan_proposal).parameters.values())[1:])


def test_errors_are_a_dedicated_hierarchy():
    for error in (InvalidSupportPlanArgument, UnsupportedSupportPlanVersion, IncompatibleSupportPlanInputs,
                  InvalidSupportPlanContent, InvalidSupportPlanProposal):
        assert error.__bases__ == (SupportPlanError,)
    assert SupportPlanError.__bases__ == (Exception,)


PROPOSAL_FORBIDDEN = ("rationale", "confidence", "score", "stage", "level", "amount", "movement", "move", "surface",
                      "validation", "diagnostic", "uuid", "definition_id", "difficulty", "gap", "weight", "rank")


def test_proposal_has_no_stage_score_rationale_nor_uuid_field():
    for cls in (ProposedPedagogicalReference, ProposedOperationAllocation, SegmentSupportProposal,
                SupportPlanProposal):
        for field in dataclasses.fields(cls):
            for word in PROPOSAL_FORBIDDEN:
                assert word not in field.name, (cls.__name__, field.name)


def test_plan_carries_no_provenance_trace_score_nor_validation():
    names = {f.name for cls in (InteractionSupportPlan, SegmentSupportPlan, RelevantSafeAssumption,
                                ConceptualBridge, PlannedOperationAllocation) for f in dataclasses.fields(cls)}
    for word in ("trace", "actual", "release", "fingerprint", "inference", "generation", "confidence", "constraint",
                 "score", "difficulty", "level", "movement", "surface", "response_text", "user_id", "provenance"):
        assert not [name for name in names if word in name], word


def test_catalogue_never_carries_validation_constraints_nor_provenance():
    names = {f.name for cls in (CatalogueCapability, CatalogueCompetency, RestrictedSupportCatalogue,
                                SupportSegmentInput, SupportPlanningInput) for f in dataclasses.fields(cls)}
    for word in ("constraint", "validation", "provenance", "inference", "generation", "release", "fingerprint",
                 "user", "tension", "confidence"):
        assert not [name for name in names if word in name], word


# --------------------------------------------------------------------------
# 2. Préflight tâche <-> posture
# --------------------------------------------------------------------------

def test_valid_task_and_posture_are_prepared_in_order():
    task = profile(EXPLAIN_SEG, GUIDE_SEG, seg("puis compare", operations=("compare",)))
    planning = prepare_support_planning(**inputs(task=task))
    assert type(planning) is SupportPlanningInput and planning.request_status == TASK_REQUESTED
    assert [(s.segment_index, s.source_excerpt, s.posture) for s in planning.segments] == [
        (0, EXPLAIN_SEG.source_excerpt, EXPLAIN), (1, GUIDE_SEG.source_excerpt, GUIDE), (2, "puis compare", EXPLAIN)]
    assert planning.segments[1].cognitive_operations == GUIDE_SEG.cognitive_operations
    assert planning.segments[1].explicit_intent == "reasoning_reserved_for_user"
    assert planning.segments[0].task_characteristics == ()


def _decision(baseline, index=0, **changes):
    decisions = list(baseline.segments)
    decisions[index] = dataclasses.replace(decisions[index], **changes)
    return dataclasses.replace(baseline, segments=tuple(decisions))


TWO = profile(EXPLAIN_SEG, GUIDE_SEG)


@pytest.mark.parametrize("mutate, error, match", [
    (lambda b: dataclasses.replace(b, request_status=NO_TASK), IncompatibleSupportPlanInputs, "request_status"),
    (lambda b: dataclasses.replace(b, segments=b.segments[:1]), IncompatibleSupportPlanInputs, "segment"),
    (lambda b: dataclasses.replace(b, segments=b.segments + b.segments[:1]), IncompatibleSupportPlanInputs,
     "segment"),
    (lambda b: _decision(b, 1, source_excerpt="autre extrait"), IncompatibleSupportPlanInputs, "source_excerpt"),
    (lambda b: _decision(b, 1, source_excerpt=GUIDE_SEG.source_excerpt + " "), IncompatibleSupportPlanInputs,
     "source_excerpt"),
    (lambda b: _decision(b, 1, segment_index=0), IncompatibleSupportPlanInputs, "segment_index"),
    (lambda b: _decision(b, 1, segment_index=True), IncompatibleSupportPlanInputs, "segment_index"),
    (lambda b: _decision(b, 0, segment_index=0.0), IncompatibleSupportPlanInputs, "segment_index"),
    (lambda b: dataclasses.replace(b, segments=tuple(reversed(b.segments))), IncompatibleSupportPlanInputs,
     "segment_index"),
    (lambda b: dataclasses.replace(b, source_task_schema_version="interaction-task-profile-v2"),
     IncompatibleSupportPlanInputs, "source_task_schema_version"),
    (lambda b: dataclasses.replace(b, source_task_policy_version="task-policy-2"), IncompatibleSupportPlanInputs,
     "source_task_policy_version"),
    (lambda b: dataclasses.replace(b, schema_version="interaction-posture-baseline-v2"),
     UnsupportedSupportPlanVersion, "posture.schema_version"),
    (lambda b: dataclasses.replace(b, policy_version="posture-policy-2"), UnsupportedSupportPlanVersion,
     "posture.policy_version"),
    (lambda b: _decision(b, 0, posture="lecture"), InvalidSupportPlanContent, "posture"),
    (lambda b: _decision(b, 0, posture="Explain"), InvalidSupportPlanContent, "posture"),
    (lambda b: _decision(b, 0, selection_basis="default"), InvalidSupportPlanContent, "selection_basis"),
    (lambda b: dataclasses.replace(b, segments=list(b.segments)), InvalidSupportPlanContent, "tuple"),
    (lambda b: dataclasses.replace(b, segments=(object(), b.segments[1])), InvalidSupportPlanContent,
     "SegmentPostureDecision"),
])
def test_task_posture_mismatch_is_refused(mutate, error, match):
    with pytest.raises(error, match=match):
        prepare_support_planning(**inputs(task=TWO, posture=mutate(build_posture_baseline(TWO))))


@pytest.mark.parametrize("changes", [
    {"posture": GUIDE}, {"posture": CO_REASON}, {"posture": CHALLENGE},
    {"selection_basis": "direct_fallback"}, {"selection_basis": "task_operation"},
])
def test_a_posture_diverging_from_6_2b_rules_is_refused_never_recomputed(changes):
    # Le baseline corrompu (vocabulaire valide) n'est jamais « réparé » par
    # la posture que 6-2B aurait produite.
    corrupted = _decision(build_posture_baseline(TWO), 0, **changes)
    with pytest.raises(IncompatibleSupportPlanInputs, match="jamais recalculée"):
        prepare_support_planning(**inputs(task=TWO, posture=corrupted))
    with pytest.raises(IncompatibleSupportPlanInputs):
        validate(proposal(sp(0, allocations=[alloc("explain_mechanism")]),
                          sp(1, allocations=[alloc("calculate", USER_RESERVED)])), task=TWO, posture=corrupted)


def test_posture_of_another_profile_with_same_shape_is_refused():
    other = profile(seg(EXPLAIN_SEG.source_excerpt, "challenge_requested"), GUIDE_SEG)
    with pytest.raises(IncompatibleSupportPlanInputs):
        prepare_support_planning(**inputs(task=TWO, posture=build_posture_baseline(other)))


def test_posture_baseline_subclass_or_enum_values_are_refused():
    class Baseline(InteractionPostureBaseline):
        pass
    baseline = build_posture_baseline(TWO)
    with pytest.raises(InvalidSupportPlanArgument):
        prepare_support_planning(**inputs(task=TWO, posture=Baseline(**{f.name: getattr(baseline, f.name)
                                                                       for f in dataclasses.fields(baseline)})))

    class Text(str):
        pass
    with pytest.raises(IncompatibleSupportPlanInputs):
        prepare_support_planning(**inputs(task=TWO, posture=dataclasses.replace(
            baseline, request_status=Text(TASK_REQUESTED))))


@pytest.mark.parametrize("name, value", [("task", object()), ("posture", None), ("ctx", "context"),
                                         ("taxonomy", {})])
def test_wrong_input_types_are_refused(name, value):
    kwargs = inputs()
    key = {"task": "task_profile", "posture": "posture_baseline", "ctx": "context", "taxonomy": "taxonomy"}[name]
    kwargs[key] = value
    with pytest.raises(InvalidSupportPlanArgument):
        prepare_support_planning(**kwargs)
    with pytest.raises(InvalidSupportPlanArgument):
        validate_support_plan_proposal(proposal(sp()), **kwargs)


def test_corrupted_task_profile_is_refused_through_the_6_2b_boundary():
    good = profile(EXPLAIN_SEG)
    posture = build_posture_baseline(good)
    bad_operations = profile(seg(EXPLAIN_SEG.source_excerpt, "direct_answer_requested",
                                 ("form_conclusion", "explain_mechanism")))
    with pytest.raises(InvalidSupportPlanContent, match="profil 6-2A") as caught:
        prepare_support_planning(**inputs(task=bad_operations, posture=posture))
    assert caught.value.__cause__ is not None
    with pytest.raises(UnsupportedSupportPlanVersion, match="profil 6-2A"):
        prepare_support_planning(**inputs(task=dataclasses.replace(good, policy_version="task-policy-0"),
                                          posture=posture))


# --------------------------------------------------------------------------
# 3. Préflight contexte <-> taxonomie, catalogue restreint
# --------------------------------------------------------------------------

def test_catalogue_contains_only_target_then_supporting_of_6_1():
    catalogue = catalogue_of()
    assert type(catalogue) is RestrictedSupportCatalogue
    assert catalogue.policy_version == SUPPORT_POLICY_VERSION and catalogue.resolution_status == "resolved"
    assert [(c.role, c.competency_code, c.scope_mode) for c in catalogue.competencies] == [
        ("target", "C10", "localized"), ("supporting", "C8", "competency_only"), ("supporting", "C9", "localized")]
    tokens = [cap.capability_token for c in catalogue.competencies for cap in c.capabilities]
    assert tokens == ["C10_B@r1", "C10_D@r1", "C9_C@r1"]


def test_catalogue_capabilities_carry_meaning_and_exact_safe_stages():
    ctx = default_context()
    catalogue = catalogue_of(ctx=ctx)
    c10 = catalogue.competencies[0]
    assert (c10.label, c10.central_question) == (SPEC_COMPETENCIES["C10"]["label"],
                                                 SPEC_COMPETENCIES["C10"]["central_question"])
    assert c10.competency_only_safe_stages is None
    b, dd = c10.capabilities
    assert (b.definition_id, b.label, b.definition) == (d("C10_B"), SPEC_CAPABILITIES["C10_B"]["label"],
                                                        SPEC_CAPABILITIES["C10_B"]["definition"])
    assert isinstance(b.mapping_guidance, MappingProxyType)
    assert b.mapping_guidance["include"] == tuple(SPEC_CAPABILITIES["C10_B"]["mapping_guidance"]["include"])
    # safe_stages : les MÊMES objets que les SafeAssumptionCoverage de 6.1.
    assert b.safe_stages is ctx.target.pedagogy.safe_assumptions[0].safe_stages
    assert dd.safe_stages == ()
    c8 = catalogue.competencies[1]
    assert c8.capabilities == () and c8.competency_only_safe_stages == DC
    assert c8.competency_only_safe_stages is ctx.supporting[0].pedagogy.safe_assumptions[0].safe_stages


def test_catalogue_never_extends_to_the_other_capabilities_or_competencies():
    ctx = context(part("target", "C10", ("C10_B", DC)), [part("supporting", "C8", ("C8_A", D))])
    catalogue = catalogue_of(ctx=ctx)
    text = repr(catalogue)
    assert [cap.capability_token for c in catalogue.competencies for cap in c.capabilities] == ["C10_B@r1",
                                                                                                "C8_A@r1"]
    for absent in ("C10_A@r1", "C10_C@r1", "C8_B@r1", "C7", "C9", "C11"):
        assert absent not in text, absent


def test_catalogue_drops_validation_constraints():
    constraint = MinimalValidationConstraint(
        intent="confirmation", target_stage="application", scope_mode="localized",
        capability_definition_ids=(d("C10_B"),),
        activation_rule="only_if_naturally_relevant_and_not_direct_answer_or_explanation",
        residual_work_rule="preserve_minimal_residual_user_work")
    ctx = context(part("target", "C10", ("C10_B", DC), constraints=[constraint]))
    planning = prepare_support_planning(**inputs(ctx=ctx))
    text = repr(planning)
    for word in ("confirmation", "only_if_naturally", "preserve_minimal", "MinimalValidationConstraint",
                 "inference_run_id", "state_generation", str(uid("run-C10")), str(RELEASE), FINGERPRINT):
        assert word not in text, word


def test_preflight_uses_the_6_1e_and_6_1b_public_boundaries(monkeypatch):
    from core import adaptation_support as module
    calls = []
    original_projection, original_focus = module.project_pedagogical_context, module.validate_focus_proposal
    monkeypatch.setattr(module, "project_pedagogical_context",
                        lambda ctx: calls.append("6-1E") or original_projection(ctx))

    def focus(prop, taxonomy):
        calls.append(("6-1B", prop.resolution_status, prop.target, prop.supporting))
        return original_focus(prop, taxonomy)
    monkeypatch.setattr(module, "validate_focus_proposal", focus)
    prepare_support_planning(**inputs())
    assert calls == ["6-1E", ("6-1B", "neutral", None, ())]


def test_taxonomy_of_another_release_is_refused_never_remapped():
    other = make_taxonomy(release=uid("release-other"))
    with pytest.raises(IncompatibleSupportPlanInputs, match="taxonomy_release_id"):
        prepare_support_planning(**inputs(taxonomy=other))


def test_context_announcing_another_fingerprint_is_refused():
    ctx = context(part("target", "C10", ("C10_B", D)), taxonomy_spec_fingerprint="0" * 64)
    with pytest.raises(IncompatibleSupportPlanInputs, match="taxonomy_spec_fingerprint"):
        prepare_support_planning(**inputs(ctx=ctx))


def test_taxonomy_with_unregistered_fingerprint_is_unsupported():
    taxonomy = dataclasses.replace(TAXONOMY, taxonomy_spec_fingerprint="f" * 64)
    with pytest.raises(UnsupportedSupportPlanVersion, match="taxonomie") as caught:
        prepare_support_planning(**inputs(taxonomy=taxonomy))
    assert caught.value.__cause__ is not None


def test_corrupted_taxonomy_is_refused():
    competencies = list(TAXONOMY.competencies)
    competencies[9] = dataclasses.replace(competencies[9], label="Valorisation (modifiée)")
    with pytest.raises(InvalidSupportPlanContent, match="taxonomie"):
        prepare_support_planning(**inputs(taxonomy=dataclasses.replace(TAXONOMY, competencies=tuple(competencies))))
    with pytest.raises(InvalidSupportPlanContent, match="taxonomie"):
        prepare_support_planning(**inputs(taxonomy=dataclasses.replace(TAXONOMY,
                                                                       capabilities=TAXONOMY.capabilities[:-1])))


def test_capability_absent_from_the_taxonomy_is_refused():
    taxonomy = make_taxonomy(ids=lambda code: uid(f"other-{code}") if code == "C10_B" else d(code))
    with pytest.raises(IncompatibleSupportPlanInputs, match="absent de la taxonomie"):
        prepare_support_planning(**inputs(taxonomy=taxonomy))


def _forged_part(role, code, ids, stages=D):
    return CompetencyResponseContext(provenance=provenance(code), pedagogy=CompetencyPedagogicalContext(
        role=role, competency_code=code, scope_mode="localized", capability_definition_ids=tuple(ids),
        safe_assumptions=tuple(SafeAssumptionCoverage(capability_definition_id=i, safe_stages=stages) for i in ids),
        validation_constraints=()))


def test_capability_of_another_competency_is_refused():
    with pytest.raises(IncompatibleSupportPlanInputs, match="n'appartient pas à C10"):
        prepare_support_planning(**inputs(ctx=context(_forged_part("target", "C10", [d("C8_A")]))))


def test_capabilities_out_of_taxonomic_order_are_refused_never_reordered():
    with pytest.raises(IncompatibleSupportPlanInputs, match="ordre taxonomique"):
        prepare_support_planning(**inputs(ctx=context(_forged_part("target", "C10", [d("C10_D"), d("C10_B")]))))


@pytest.mark.parametrize("ctx, error", [
    (context(CompetencyResponseContext(provenance=provenance(), pedagogy=CompetencyPedagogicalContext(
        role="target", competency_code="C10", scope_mode="localized", capability_definition_ids=(),
        safe_assumptions=(), validation_constraints=()))), InvalidSupportPlanContent),
    (context(CompetencyResponseContext(provenance=provenance(), pedagogy=CompetencyPedagogicalContext(
        role="target", competency_code="C10", scope_mode="whole_competency", capability_definition_ids=(),
        safe_assumptions=(SafeAssumptionCoverage(capability_definition_id=None, safe_stages=D),),
        validation_constraints=()))), InvalidSupportPlanContent),
    (context(part("target", "C10", ("C10_B", ("comprehension", "discovery")))), InvalidSupportPlanContent),
    (context(part("target", "C10", ("C10_B", D), with_provenance=False)), InvalidSupportPlanContent),
    (context(part("target", "C10", ("C10_B", D)), status="neutral"), InvalidSupportPlanContent),
    (context(None), InvalidSupportPlanContent),
    (dataclasses.replace(context(part("target", "C10", ("C10_B", D))),
                         schema_version="pedagogical-response-context-v2"), UnsupportedSupportPlanVersion),
    (context(part("target", "C10", ("C10_B", D)), focus_policy_version="focus-policy-2"),
     UnsupportedSupportPlanVersion),
])
def test_incoherent_or_unsupported_context_is_refused(ctx, error):
    with pytest.raises(error, match="contexte 6-1E") as caught:
        prepare_support_planning(**inputs(ctx=ctx))
    assert caught.value.__cause__ is not None


# --------------------------------------------------------------------------
# 4. Statuts
# --------------------------------------------------------------------------

def test_no_task_yields_an_empty_deterministic_plan():
    task = profile(status=NO_TASK)
    result = validate(proposal(), task=task)
    assert result.request_status == NO_TASK and result.segments == ()
    assert result == validate(proposal(), task=task)
    with pytest.raises(InvalidSupportPlanProposal, match="segment"):
        validate(proposal(sp()), task=task)


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_unresolved_focus_has_no_personalisation_but_keeps_posture_and_allocations(status):
    ctx = context(status=status)
    task = profile(GUIDE_SEG, JOINT_SEG)
    catalogue = catalogue_of(ctx=ctx, task=task)
    assert catalogue.competencies == () and catalogue.resolution_status == status
    result = validate(proposal(sp(0, allocations=[alloc("calculate", USER_RESERVED)]),
                               sp(1, allocations=[alloc("interpret_evidence", JOINT)])), ctx=ctx, task=task)
    assert result.pedagogical_resolution_status == status
    assert [s.posture for s in result.segments] == [GUIDE, CO_REASON]
    assert all(s.relevant_safe_assumptions == () and s.conceptual_bridges == () for s in result.segments)
    for refs in ({"assumptions": [ref("C10", "C10_B@r1")]}, {"bridges": [ref("C10")]}):
        with pytest.raises(InvalidSupportPlanProposal, match="catalogue restreint"):
            validate(proposal(sp(0, allocations=[alloc("calculate", USER_RESERVED)], **refs),
                              sp(1, allocations=[alloc("interpret_evidence", JOINT)])), ctx=ctx, task=task)


def test_composite_never_invents_local_focuses_per_segment():
    task = profile(seg("Explique-moi le FCF", operations=("explain_mechanism",)),
                   seg("puis le ROIC", operations=("explain_mechanism",)))
    ctx = context(status="composite")
    for code, token in (("C7", "C7_A@r1"), ("C8", "C8_A@r1")):
        with pytest.raises(InvalidSupportPlanProposal):
            validate(proposal(sp(0, bridges=[ref(code, token)]), sp(1)), ctx=ctx, task=task)
    assert validate(proposal(sp(0), sp(1)), ctx=ctx, task=task).segments[1].conceptual_bridges == ()


def test_resolved_focus_allows_assumptions_and_bridges():
    result = validate(proposal(sp(assumptions=[ref("C8")], bridges=[ref("C10", "C10_B@r1")])))
    (segment,) = result.segments
    assert segment.relevant_safe_assumptions == (RelevantSafeAssumption(
        competency_code="C8", capability_definition_id=None, safe_stages=DC),)
    assert segment.conceptual_bridges == (ConceptualBridge(competency_code="C10", scope_mode="localized",
                                                           capability_definition_ids=(d("C10_B"),)),)


# --------------------------------------------------------------------------
# 5. Présupposés sûrs et ponts
# --------------------------------------------------------------------------

def test_relevant_safe_assumption_is_one_exact_coverage_per_capability():
    ctx = context(part("target", "C10", ("C10_A", DCA), ("C10_B", D)))
    result = validate(proposal(sp(assumptions=[ref("C10", "C10_B@r1", "C10_A@r1")])), ctx=ctx)
    assert result.segments[0].relevant_safe_assumptions == (
        RelevantSafeAssumption(competency_code="C10", capability_definition_id=d("C10_A"), safe_stages=DCA),
        RelevantSafeAssumption(competency_code="C10", capability_definition_id=d("C10_B"), safe_stages=D))
    coverages = ctx.target.pedagogy.safe_assumptions
    assert [a.safe_stages for a in result.segments[0].relevant_safe_assumptions] == [c.safe_stages for c in coverages]
    assert result.segments[0].relevant_safe_assumptions[0].safe_stages is coverages[0].safe_stages


def test_irrelevant_assumption_is_simply_not_selected():
    result = validate(proposal(sp(assumptions=[ref("C9", "C9_C@r1")])))
    assert [a.competency_code for a in result.segments[0].relevant_safe_assumptions] == ["C9"]
    assert validate(proposal(sp())).segments[0].relevant_safe_assumptions == ()


@pytest.mark.parametrize("reference, match", [
    (ref("C7", "C7_A@r1"), "catalogue restreint"),                       # compétence hors 6.1
    (ref("C10", "C10_A@r1"), "absent du catalogue restreint"),           # capacité globale hors périmètre
    (ref("C10", "C10_E@r1"), "absent du catalogue restreint"),           # token inventé
    (ref("C10", "C10_B@r2"), "absent du catalogue restreint"),           # autre révision
    (ref("C10", "C10_B"), "absent du catalogue restreint"),              # révision omise
    (ref("C10", "c10_b@r1"), "absent du catalogue restreint"),           # casse
    (ref("C10", "C9_C@r1"), "appartient à C9"),                          # capacité d'une autre compétence
    (ref("C10", "C10_B@r1", "C10_B@r1"), "double"),
    (ref("C10", mode="localized"), "sans capacité"),                     # localized sans token
    (ref("C10", "C10_B@r1", mode="competency_only"), "périmètre 6.1"),   # mauvais scope
    (ref("C8", "C8_A@r1", mode="localized"), "périmètre 6.1"),
    (ref("C8", mode="whole_competency"), "périmètre 6.1"),
    (ref("C10", "C10_D@r1"), "aucun stade sûr"),                         # coverage vide : jamais présupposé
    (ProposedPedagogicalReference(competency_code="C10", scope_mode="localized", capability_tokens=["C10_B@r1"]),
     "tuple"),
    (ProposedPedagogicalReference(competency_code=None, scope_mode="localized", capability_tokens=()),
     "catalogue restreint"),
    (ProposedPedagogicalReference(competency_code="C10", scope_mode="localized", capability_tokens=(1,)),
     "absent du catalogue restreint"),
    (("C10", "localized", ("C10_B@r1",)), "ProposedPedagogicalReference"),
])
def test_invalid_assumption_reference_is_refused(reference, match):
    refused(InvalidSupportPlanProposal, proposal(sp(assumptions=[reference])), match)


@pytest.mark.parametrize("reference, match", [
    (ref("C7", "C7_A@r1"), "catalogue restreint"),
    (ref("C10", "C10_A@r1"), "absent du catalogue restreint"),
    (ref("C10", "C9_C@r1"), "appartient à C9"),
    (ref("C10", mode="localized"), "sans capacité"),
    (ref("C8", "C8_A@r1", mode="competency_only"), "aucun token inventé"),
    (ref("C9", mode="competency_only"), "périmètre 6.1"),
])
def test_invalid_bridge_reference_is_refused(reference, match):
    refused(InvalidSupportPlanProposal, proposal(sp(bridges=[reference])), match)


def test_same_competency_twice_in_a_list_is_refused():
    refused(InvalidSupportPlanProposal, proposal(sp(bridges=[ref("C10", "C10_B@r1"), ref("C10", "C10_D@r1")])),
            "C10 en double")
    refused(InvalidSupportPlanProposal, proposal(sp(assumptions=[ref("C8"), ref("C8")])), "C8 en double")


def test_needed_supporting_bridge_is_accepted_and_unneeded_support_is_never_bridged():
    result = validate(proposal(sp(bridges=[ref("C9", "C9_C@r1")])))
    assert result.segments[0].conceptual_bridges == (ConceptualBridge(
        competency_code="C9", scope_mode="localized", capability_definition_ids=(d("C9_C"),)),)
    # Aucun pont automatique : C8 (support 6.1) n'apparaît que si proposé.
    assert all(b.competency_code != "C8" for b in result.segments[0].conceptual_bridges)
    assert validate(proposal(sp())).segments[0].conceptual_bridges == ()


def test_competency_only_bridge_has_no_invented_capability():
    result = validate(proposal(sp(bridges=[ref("C8")])))
    assert result.segments[0].conceptual_bridges == (ConceptualBridge(
        competency_code="C8", scope_mode="competency_only", capability_definition_ids=()),)


def test_same_perimeter_can_be_assumption_and_bridge():
    result = validate(proposal(sp(assumptions=[ref("C10", "C10_B@r1"), ref("C8")],
                                  bridges=[ref("C10", "C10_B@r1"), ref("C8")])))
    segment = result.segments[0]
    assert [a.competency_code for a in segment.relevant_safe_assumptions] == ["C10", "C8"]
    assert [b.competency_code for b in segment.conceptual_bridges] == ["C10", "C8"]


def test_bridge_without_any_safe_assumption_is_possible_without_diagnostic():
    # C10_D ne porte aucun stade sûr : il peut être un pont, jamais un présupposé.
    result = validate(proposal(sp(bridges=[ref("C10", "C10_D@r1")])))
    assert result.segments[0].relevant_safe_assumptions == ()
    assert result.segments[0].conceptual_bridges[0].capability_definition_ids == (d("C10_D"),)
    no_state = context(part("target", "C10", ("C10_B", ()), with_provenance=False))
    assert validate(proposal(sp(bridges=[ref("C10", "C10_B@r1")])), ctx=no_state).segments[0].conceptual_bridges


def test_model_cannot_provide_its_own_safe_stages():
    with pytest.raises(TypeError):
        ProposedPedagogicalReference(competency_code="C10", scope_mode="localized", capability_tokens=(),
                                     safe_stages=DCAM)
    assert "safe_stages" not in {f.name for f in dataclasses.fields(ProposedPedagogicalReference)}


def test_safe_stages_are_never_altered():
    for stages in (D, DC, DCA, DCAM, ("comprehension",), ("discovery", "application")):
        ctx = context(part("target", "C10", ("C10_B", stages)))
        result = validate(proposal(sp(assumptions=[ref("C10", "C10_B@r1")])), ctx=ctx)
        assert result.segments[0].relevant_safe_assumptions[0].safe_stages == stages


# --------------------------------------------------------------------------
# 6. Allocations et postures
# --------------------------------------------------------------------------

def test_explain_accepts_oryx_and_unallocated_operations():
    result = validate(proposal(sp(allocations=[alloc("explain_mechanism"), alloc("form_conclusion")])))
    assert allocations_of(result) == [("explain_mechanism", ORYX), ("form_conclusion", ORYX)]
    assert validate(proposal(sp())).segments[0].operation_allocations == ()


@pytest.mark.parametrize("allocation", [USER_RESERVED, JOINT])
def test_explain_refuses_user_reserved_and_joint(allocation):
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc("explain_mechanism"),
                                                                 alloc("form_conclusion", allocation)])), "explain")


def test_guide_requires_at_least_one_user_reserved_operation():
    task = profile(GUIDE_SEG)
    result = validate(proposal(sp(allocations=[alloc("apply_procedure"), alloc("calculate", USER_RESERVED)])),
                      task=task)
    assert allocations_of(result) == [("apply_procedure", ORYX), ("calculate", USER_RESERVED)]
    for allocations in ([], [alloc("apply_procedure"), alloc("calculate")]):
        refused(InvalidSupportPlanProposal, proposal(sp(allocations=allocations)), "user_reserved", task=task)
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc("calculate", USER_RESERVED),
                                                                 alloc("form_conclusion", JOINT)])),
            "joint", task=task)


def test_co_reason_requires_at_least_one_joint_operation():
    task = profile(JOINT_SEG)
    result = validate(proposal(sp(allocations=[alloc("select_relevant_information"),
                                               alloc("interpret_evidence", JOINT), alloc("construct_reasoning", JOINT),
                                               alloc("form_conclusion", USER_RESERVED)])), task=task)
    assert allocations_of(result) == [("select_relevant_information", ORYX), ("interpret_evidence", JOINT),
                                      ("construct_reasoning", JOINT), ("form_conclusion", USER_RESERVED)]
    for allocations in ([], [alloc("interpret_evidence")], [alloc("form_conclusion", USER_RESERVED)]):
        refused(InvalidSupportPlanProposal, proposal(sp(allocations=allocations)), "joint", task=task)


@pytest.mark.parametrize("operation", ["evaluate_existing_reasoning", "stress_test", "falsify"])
def test_challenge_operations_are_never_reserved_to_the_user(operation):
    task = profile(CHALLENGE_SEG)
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc(operation, USER_RESERVED)])), "challenge",
            task=task)
    for allocation in (ORYX, JOINT):
        assert allocations_of(validate(proposal(sp(allocations=[alloc(operation, allocation)])), task=task)) == [
            (operation, allocation)]


def test_challenge_allows_other_operations_to_stay_with_the_user_and_generates_nothing():
    task = profile(CHALLENGE_SEG)
    result = validate(proposal(sp(allocations=[alloc("generate_hypotheses", USER_RESERVED),
                                               alloc("evaluate_existing_reasoning"), alloc("stress_test", JOINT)])),
                      task=task)
    assert result.segments[0].posture == CHALLENGE
    assert allocations_of(result) == [("generate_hypotheses", USER_RESERVED), ("evaluate_existing_reasoning", ORYX),
                                      ("stress_test", JOINT)]
    assert validate(proposal(sp()), task=task).segments[0].operation_allocations == ()


def test_task_operation_challenge_follows_the_same_rule():
    task = profile(seg("Qu'est-ce qui réfuterait ma thèse ?", operations=("generate_hypotheses", "falsify"),
                       characteristics=("existing_reasoning_to_evaluate",)))
    assert build_posture_baseline(task).segments[0].selection_basis == "task_operation"
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc("falsify", USER_RESERVED)])), task=task)
    assert validate(proposal(sp(allocations=[alloc("falsify", JOINT)])), task=task)


@pytest.mark.parametrize("allocation, match", [
    (alloc("calculate"), "absente du segment"),                     # opération du vocabulaire hors segment
    (alloc("summarize"), "hors vocabulaire"),                       # opération inventée
    (alloc("Explain_Mechanism"), "hors vocabulaire"),
    (alloc("explain_mechanism", "user"), "hors vocabulaire"),       # allocation inconnue
    (alloc("explain_mechanism", "Oryx"), "hors vocabulaire"),
    (alloc("explain_mechanism", None), "hors vocabulaire"),
    (alloc(None), "hors vocabulaire"),
    (("explain_mechanism", "oryx"), "ProposedOperationAllocation"),
])
def test_invalid_allocation_is_refused(allocation, match):
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[allocation])), match)


def test_duplicate_operation_is_refused_even_with_the_same_allocation():
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc("explain_mechanism"),
                                                                 alloc("explain_mechanism")])), "double")


def test_str_subclass_values_are_never_converted():
    class Text(str):
        pass
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc(Text("explain_mechanism"))])))
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc("explain_mechanism", Text(ORYX))])))
    refused(InvalidSupportPlanProposal, proposal(sp(bridges=[ref(Text("C8"))])))
    refused(InvalidSupportPlanProposal, proposal(sp(bridges=[ref("C10", Text("C10_B@r1"))])))
    refused(InvalidSupportPlanProposal, proposal(sp(), schema_version=Text(SUPPORT_PLAN_SCHEMA_VERSION)))


# --------------------------------------------------------------------------
# 7. Canonicalisation, fail-closed, déterminisme
# --------------------------------------------------------------------------

def test_allocations_are_canonicalised_in_cognitive_operations_order():
    task = profile(JOINT_SEG)
    forward = [alloc("select_relevant_information"), alloc("interpret_evidence", JOINT),
               alloc("form_conclusion", USER_RESERVED)]
    assert validate(proposal(sp(allocations=forward[::-1])), task=task) == validate(
        proposal(sp(allocations=forward)), task=task)


def test_references_and_tokens_are_canonicalised_in_catalogue_order():
    shuffled = proposal(sp(assumptions=[ref("C9", "C9_C@r1"), ref("C8"), ref("C10", "C10_B@r1")],
                           bridges=[ref("C9", "C9_C@r1"), ref("C10", "C10_D@r1", "C10_B@r1")]))
    ordered = proposal(sp(assumptions=[ref("C10", "C10_B@r1"), ref("C8"), ref("C9", "C9_C@r1")],
                          bridges=[ref("C10", "C10_B@r1", "C10_D@r1"), ref("C9", "C9_C@r1")]))
    result = validate(shuffled)
    assert result == validate(ordered)
    assert [a.competency_code for a in result.segments[0].relevant_safe_assumptions] == ["C10", "C8", "C9"]
    assert result.segments[0].conceptual_bridges[0].capability_definition_ids == (d("C10_B"), d("C10_D"))


def test_capabilities_follow_the_focus_order_never_a_uuid_sort():
    # Un tri d'UUID inverserait l'ordre taxonomique C10_A -> C10_C.
    ids = {"C10_A": uuid.UUID("ffffffff-0000-4000-8000-000000000000"),
           "C10_C": uuid.UUID("00000000-0000-4000-8000-000000000000")}
    taxonomy = make_taxonomy(ids=lambda code: ids.get(code, d(code)))
    assert sorted(ids.values()) == [ids["C10_C"], ids["C10_A"]]
    ctx = context(_forged_part("target", "C10", [ids["C10_A"], ids["C10_C"]]))
    result = validate(proposal(sp(bridges=[ref("C10", "C10_C@r1", "C10_A@r1")])), ctx=ctx, taxonomy=taxonomy)
    assert result.segments[0].conceptual_bridges[0].capability_definition_ids == (ids["C10_A"], ids["C10_C"])


@pytest.mark.parametrize("prop, match", [
    (proposal(sp(), schema_version="interaction-support-plan-v2"), "schema_version"),
    (proposal(sp(), policy_version="support-policy-2"), "policy_version"),
    (proposal(sp(), schema_version=None), "schema_version"),
    (proposal(), "segment"),
    (proposal(sp(), sp(1)), "segment"),
    (proposal(sp(1)), "segment_index"),
    (proposal(sp(True)), "segment_index"),
    (proposal(sp("0")), "segment_index"),
    (proposal(segments=[sp()]), "tuple"),
    (proposal(segments=(object(),)), "SegmentSupportProposal"),
    (proposal(SegmentSupportProposal(segment_index=0, relevant_safe_assumptions=[], conceptual_bridges=(),
                                     operation_allocations=())), "tuple"),
    (proposal(SegmentSupportProposal(segment_index=0, relevant_safe_assumptions=(), conceptual_bridges=None,
                                     operation_allocations=())), "tuple"),
    (proposal(SegmentSupportProposal(segment_index=0, relevant_safe_assumptions=(), conceptual_bridges=(),
                                     operation_allocations=[])), "tuple"),
])
def test_structurally_invalid_proposal_is_refused(prop, match):
    refused(InvalidSupportPlanProposal, prop, match)


def test_wrong_proposal_type_is_an_argument_error():
    for value in (None, {"segments": []}, FocusProposal):
        refused(InvalidSupportPlanArgument, value)


def test_segments_must_follow_the_6_2a_order():
    task = profile(EXPLAIN_SEG, GUIDE_SEG)
    good = (sp(0, allocations=[alloc("explain_mechanism")]), sp(1, allocations=[alloc("calculate", USER_RESERVED)]))
    assert validate(proposal(*good), task=task)
    refused(InvalidSupportPlanProposal, proposal(*good[::-1]), "segment_index", task=task)


def test_no_partial_plan_when_a_later_segment_is_invalid():
    task = profile(EXPLAIN_SEG, GUIDE_SEG)
    refused(InvalidSupportPlanProposal, proposal(sp(0, allocations=[alloc("explain_mechanism")]), sp(1)),
            r"segments\[1\]", task=task)


def test_errors_are_never_turned_into_an_explain_or_neutral_fallback():
    for prop in (proposal(sp(allocations=[alloc("explain_mechanism", USER_RESERVED)])),
                 proposal(sp(bridges=[ref("C7")]))):
        with pytest.raises(SupportPlanError):
            validate(prop)


def test_deterministic_and_inputs_are_not_mutated():
    kwargs = inputs(task=profile(GUIDE_SEG, CHALLENGE_SEG))
    snapshot = {key: repr(value) for key, value in kwargs.items()}
    prop = proposal(sp(0, assumptions=[ref("C10", "C10_B@r1")], bridges=[ref("C8")],
                       allocations=[alloc("calculate", USER_RESERVED)]),
                    sp(1, allocations=[alloc("stress_test", JOINT)]))
    prop_repr = repr(prop)
    plans = {validate_support_plan_proposal(prop, **kwargs) for _ in range(3)}
    assert len(plans) == 1
    assert {key: repr(value) for key, value in kwargs.items()} == snapshot and repr(prop) == prop_repr
    assert prepare_support_planning(**kwargs) == prepare_support_planning(**kwargs)


def test_same_inputs_rebuilt_independently_give_the_same_plan():
    prop = proposal(sp(bridges=[ref("C9", "C9_C@r1")], allocations=[alloc("form_conclusion")]))
    assert validate(prop) == validate(prop, ctx=default_context(), taxonomy=make_taxonomy())


def test_outputs_are_immutable():
    result = validate(proposal(sp(assumptions=[ref("C8")], allocations=[alloc("explain_mechanism")])))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.segments = ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.segments[0].posture = GUIDE
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.segments[0].operation_allocations[0].allocation = USER_RESERVED
    catalogue = catalogue_of()
    with pytest.raises(dataclasses.FrozenInstanceError):
        catalogue.competencies = ()
    with pytest.raises(TypeError):
        catalogue.competencies[0].capabilities[0].mapping_guidance["include"] = ()
    assert all(type(value) is tuple for value in (result.segments, catalogue.competencies,
                                                   result.segments[0].relevant_safe_assumptions))


def test_plan_header_carries_the_exact_source_versions():
    result = validate(proposal(sp()))
    assert (result.schema_version, result.policy_version, result.source_task_schema_version,
            result.source_task_policy_version, result.source_posture_schema_version,
            result.source_posture_policy_version, result.source_pedagogical_context_schema_version,
            result.request_status, result.pedagogical_resolution_status) == (
        SUPPORT_PLAN_SCHEMA_VERSION, SUPPORT_POLICY_VERSION, TASK_SCHEMA_VERSION, TASK_POLICY_VERSION,
        "interaction-posture-baseline-v1", "posture-policy-1", PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION,
        TASK_REQUESTED, "resolved")


def test_plan_keeps_segment_anchor_and_posture_from_6_2b():
    task = profile(EXPLAIN_SEG, GUIDE_SEG, JOINT_SEG, CHALLENGE_SEG)
    posture = build_posture_baseline(task)
    result = validate(proposal(sp(0), sp(1, allocations=[alloc("calculate", USER_RESERVED)]),
                               sp(2, allocations=[alloc("form_conclusion", JOINT)]), sp(3)), task=task)
    assert [(s.segment_index, s.source_excerpt, s.posture) for s in result.segments] == [
        (d_.segment_index, d_.source_excerpt, d_.posture) for d_ in posture.segments]


# --------------------------------------------------------------------------
# 8. Projection de génération
# --------------------------------------------------------------------------

def _plan_and_catalogue(prop=None, **kwargs):
    prop = prop or proposal(sp(assumptions=[ref("C10", "C10_B@r1"), ref("C8")],
                               bridges=[ref("C10", "C10_B@r1", "C10_D@r1"), ref("C8")],
                               allocations=[alloc("explain_mechanism")]))
    return validate(prop, **kwargs), catalogue_of(**kwargs)


def _walk(value):
    yield value
    if dataclasses.is_dataclass(value):
        for field in dataclasses.fields(value):
            yield from _walk(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from _walk(item)


def test_projection_exposes_meaning_only():
    plan_, catalogue = _plan_and_catalogue()
    projection = project_support_plan(plan_, catalogue=catalogue)
    assert type(projection) is SupportGenerationProjection
    assert (projection.schema_version, projection.policy_version, projection.request_status) == (
        SUPPORT_PLAN_SCHEMA_VERSION, SUPPORT_POLICY_VERSION, TASK_REQUESTED)
    (segment,) = projection.segments
    assert (segment.segment_index, segment.source_excerpt, segment.posture) == (0, EXPLAIN_SEG.source_excerpt,
                                                                                EXPLAIN)
    c10_b = ProjectedCapability(capability_token="C10_B@r1", label=SPEC_CAPABILITIES["C10_B"]["label"],
                                definition=SPEC_CAPABILITIES["C10_B"]["definition"])
    c10_d = ProjectedCapability(capability_token="C10_D@r1", label=SPEC_CAPABILITIES["C10_D"]["label"],
                                definition=SPEC_CAPABILITIES["C10_D"]["definition"])
    assert segment.relevant_safe_assumptions == (
        ProjectedSafeAssumption(competency_code="C10", competency_label="Valorisation", capability=c10_b,
                                safe_stages=D),
        ProjectedSafeAssumption(competency_code="C8", competency_label="Efficacité du capital / ROIC",
                                capability=None, safe_stages=DC))
    assert segment.conceptual_bridges == (
        ProjectedConceptualBridge(competency_code="C10", competency_label="Valorisation", scope_mode="localized",
                                  capabilities=(c10_b, c10_d)),
        ProjectedConceptualBridge(competency_code="C8", competency_label="Efficacité du capital / ROIC",
                                  scope_mode="competency_only", capabilities=()))
    assert segment.operation_allocations == plan_.segments[0].operation_allocations


def test_projection_contains_no_uuid_nor_technical_provenance():
    plan_, catalogue = _plan_and_catalogue()
    projection = project_support_plan(plan_, catalogue=catalogue)
    assert not [value for value in _walk(projection) if isinstance(value, uuid.UUID)]
    text = repr(projection)
    for word in (str(RELEASE), FINGERPRINT, "mapping_guidance", "include", "central_question", "definition_id",
                 "inference", "state_generation", "provenance", "constraint", "confirmation", "score"):
        assert word not in text, word
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text)


def test_projection_is_deterministic_and_immutable():
    plan_, catalogue = _plan_and_catalogue()
    first = project_support_plan(plan_, catalogue=catalogue)
    assert first == project_support_plan(plan_, catalogue=catalogue)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.segments[0].posture = GUIDE


def test_projection_of_no_task_and_of_unresolved_plans():
    task = profile(status=NO_TASK)
    plan_, catalogue = _plan_and_catalogue(proposal(), task=task)
    assert project_support_plan(plan_, catalogue=catalogue).segments == ()
    ctx = context(status="ambiguous")
    plan_, catalogue = _plan_and_catalogue(proposal(sp(allocations=[alloc("explain_mechanism")])), ctx=ctx)
    (segment,) = project_support_plan(plan_, catalogue=catalogue).segments
    assert segment.relevant_safe_assumptions == () and segment.conceptual_bridges == ()
    assert [(a.operation, a.allocation) for a in segment.operation_allocations] == [("explain_mechanism", ORYX)]


def test_projection_refuses_a_catalogue_of_another_preflight():
    plan_, _ = _plan_and_catalogue()
    other = catalogue_of(ctx=context(part("target", "C10", ("C10_B", DCA)), [part("supporting", "C8", stages=DC)]))
    with pytest.raises(IncompatibleSupportPlanInputs):
        project_support_plan(plan_, catalogue=other)
    unresolved = catalogue_of(ctx=context(status="neutral"))
    with pytest.raises(IncompatibleSupportPlanInputs, match="pedagogical_resolution_status"):
        project_support_plan(plan_, catalogue=unresolved)
    without_c8 = catalogue_of(ctx=context(part("target", "C10", ("C10_B", D), ("C10_D", ()))))
    with pytest.raises(IncompatibleSupportPlanInputs):
        project_support_plan(plan_, catalogue=without_c8)


def _segment(plan_, **changes):
    return dataclasses.replace(plan_, segments=(dataclasses.replace(plan_.segments[0], **changes),))


@pytest.mark.parametrize("mutate, error", [
    (lambda p: dataclasses.replace(p, schema_version="interaction-support-plan-v0"), UnsupportedSupportPlanVersion),
    (lambda p: dataclasses.replace(p, policy_version="support-policy-0"), UnsupportedSupportPlanVersion),
    (lambda p: dataclasses.replace(p, source_task_policy_version="task-policy-0"), UnsupportedSupportPlanVersion),
    (lambda p: dataclasses.replace(p, source_posture_policy_version="posture-policy-0"),
     UnsupportedSupportPlanVersion),
    (lambda p: dataclasses.replace(p, source_pedagogical_context_schema_version="x"), UnsupportedSupportPlanVersion),
    (lambda p: dataclasses.replace(p, request_status=NO_TASK), InvalidSupportPlanContent),
    (lambda p: dataclasses.replace(p, request_status="maybe"), InvalidSupportPlanContent),
    (lambda p: dataclasses.replace(p, segments=()), InvalidSupportPlanContent),
    (lambda p: _segment(p, segment_index=1), InvalidSupportPlanContent),
    (lambda p: _segment(p, posture="lecture"), InvalidSupportPlanContent),
    (lambda p: _segment(p, source_excerpt=" "), InvalidSupportPlanContent),
    (lambda p: _segment(p, operation_allocations=(PlannedOperationAllocation(operation="explain_mechanism",
                                                                             allocation=USER_RESERVED),)),
     InvalidSupportPlanContent),
    (lambda p: _segment(p, posture=GUIDE), InvalidSupportPlanContent),
    (lambda p: _segment(p, operation_allocations=(PlannedOperationAllocation(operation="form_conclusion",
                                                                             allocation=ORYX),
                                                  PlannedOperationAllocation(operation="explain_mechanism",
                                                                             allocation=ORYX))),
     InvalidSupportPlanContent),
    (lambda p: _segment(p, relevant_safe_assumptions=(RelevantSafeAssumption(
        competency_code="C10", capability_definition_id=d("C10_B"), safe_stages=DCAM),)),
     IncompatibleSupportPlanInputs),
    (lambda p: _segment(p, relevant_safe_assumptions=(RelevantSafeAssumption(
        competency_code="C10", capability_definition_id=d("C10_D"), safe_stages=()),)),
     IncompatibleSupportPlanInputs),
    (lambda p: _segment(p, relevant_safe_assumptions=(RelevantSafeAssumption(
        competency_code="C7", capability_definition_id=d("C7_A"), safe_stages=D),)), IncompatibleSupportPlanInputs),
    (lambda p: _segment(p, relevant_safe_assumptions=p.segments[0].relevant_safe_assumptions[::-1]),
     IncompatibleSupportPlanInputs),
    (lambda p: _segment(p, conceptual_bridges=p.segments[0].conceptual_bridges[::-1]),
     IncompatibleSupportPlanInputs),
    (lambda p: _segment(p, conceptual_bridges=(ConceptualBridge(
        competency_code="C10", scope_mode="localized", capability_definition_ids=(d("C10_A"),)),)),
     IncompatibleSupportPlanInputs),
    (lambda p: _segment(p, conceptual_bridges=(ConceptualBridge(
        competency_code="C10", scope_mode="localized", capability_definition_ids=(d("C10_D"), d("C10_B"))),)),
     IncompatibleSupportPlanInputs),
    (lambda p: _segment(p, conceptual_bridges=(ConceptualBridge(
        competency_code="C8", scope_mode="localized", capability_definition_ids=()),)), IncompatibleSupportPlanInputs),
    (lambda p: _segment(p, conceptual_bridges=[]), InvalidSupportPlanContent),
])
def test_projection_refuses_a_forged_plan(mutate, error):
    plan_, catalogue = _plan_and_catalogue()
    with pytest.raises(error):
        project_support_plan(mutate(plan_), catalogue=catalogue)


@pytest.mark.parametrize("mutate", [
    lambda c: dataclasses.replace(c, policy_version="support-policy-0"),
    lambda c: dataclasses.replace(c, resolution_status="neutral"),
    lambda c: dataclasses.replace(c, competencies=c.competencies[1:]),
    lambda c: dataclasses.replace(c, competencies=list(c.competencies)),
    lambda c: dataclasses.replace(c, competencies=(c.competencies[0], c.competencies[0])),
    lambda c: dataclasses.replace(c, competencies=(dataclasses.replace(c.competencies[0], scope_mode="whole"),
                                                   *c.competencies[1:])),
    lambda c: dataclasses.replace(c, competencies=(dataclasses.replace(c.competencies[0], capabilities=()),
                                                   *c.competencies[1:])),
    lambda c: dataclasses.replace(c, competencies=(c.competencies[0], dataclasses.replace(
        c.competencies[1], competency_only_safe_stages=None), *c.competencies[2:])),
    lambda c: dataclasses.replace(c, competencies=(c.competencies[0], dataclasses.replace(
        c.competencies[1], competency_only_safe_stages=("mastery", "discovery")), *c.competencies[2:])),
    lambda c: dataclasses.replace(c, competencies=(dataclasses.replace(c.competencies[0], capabilities=(
        dataclasses.replace(c.competencies[0].capabilities[0], safe_stages=("non_etabli",)),
        c.competencies[0].capabilities[1])), *c.competencies[1:])),
    lambda c: dataclasses.replace(c, competencies=(dataclasses.replace(c.competencies[0], capabilities=(
        c.competencies[0].capabilities[0], c.competencies[0].capabilities[0])), *c.competencies[1:])),
])
def test_projection_refuses_a_forged_catalogue(mutate):
    plan_, catalogue = _plan_and_catalogue()
    with pytest.raises(SupportPlanError):
        project_support_plan(plan_, catalogue=mutate(catalogue))


def test_projection_argument_types():
    plan_, catalogue = _plan_and_catalogue()
    with pytest.raises(InvalidSupportPlanArgument):
        project_support_plan(catalogue, catalogue=catalogue)
    with pytest.raises(InvalidSupportPlanArgument):
        project_support_plan(plan_, catalogue=prepare_support_planning(**inputs()))


# --------------------------------------------------------------------------
# 9. Cas frontières pédagogiques et chaîne réelle
# --------------------------------------------------------------------------

def test_case_1_just_the_explanation_reserves_nothing_to_the_user():
    task = profile(seg("Donne-moi juste l'explication.", "direct_answer_requested",
                       ("explain_mechanism", "form_conclusion")))
    assert build_posture_baseline(task).segments[0].posture == EXPLAIN
    for allocation in (USER_RESERVED, JOINT):
        refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc("form_conclusion", allocation)])),
                task=task)
    assert allocations_of(validate(proposal(sp(allocations=[alloc("form_conclusion")])), task=task)) == [
        ("form_conclusion", ORYX)]


def test_case_2_do_not_give_me_the_answer_keeps_an_operation_for_the_user():
    task = profile(seg("Ne me donne pas la réponse, aide-moi à la trouver.", "reasoning_reserved_for_user",
                       ("construct_reasoning", "form_conclusion")))
    assert build_posture_baseline(task).segments[0].posture == GUIDE
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc("construct_reasoning"),
                                                                 alloc("form_conclusion")])), task=task)
    result = validate(proposal(sp(allocations=[alloc("construct_reasoning"),
                                               alloc("form_conclusion", USER_RESERVED)])), task=task)
    assert ("form_conclusion", USER_RESERVED) in allocations_of(result)


def test_case_3_analyse_with_me_shares_at_least_one_operation():
    task = profile(seg("Analyse avec moi.", "joint_reasoning_requested", ("interpret_evidence", "form_conclusion")))
    assert build_posture_baseline(task).segments[0].posture == CO_REASON
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc("interpret_evidence")])), task=task)
    assert JOINT in [a for _, a in allocations_of(validate(proposal(sp(allocations=[
        alloc("interpret_evidence", JOINT)])), task=task))]


def test_case_4_challenge_my_thesis_keeps_oryx_in_the_challenge():
    task = profile(seg("Challenge ma thèse.", "challenge_requested", ("evaluate_existing_reasoning", "stress_test"),
                       ("existing_reasoning_to_evaluate",)))
    assert build_posture_baseline(task).segments[0].posture == CHALLENGE
    refused(InvalidSupportPlanProposal, proposal(sp(allocations=[alloc("evaluate_existing_reasoning",
                                                                       USER_RESERVED)])), task=task)
    result = validate(proposal(sp(allocations=[alloc("evaluate_existing_reasoning"), alloc("stress_test", JOINT)])),
                      task=task)
    assert allocations_of(result) == [("evaluate_existing_reasoning", ORYX), ("stress_test", JOINT)]


def test_case_5_high_assumptions_and_specialised_task_never_downgrade():
    task = profile(seg("Lis ce bilan bancaire.", operations=("select_relevant_information", "interpret_evidence"),
                       characteristics=("domain_specialized",)))
    ctx = context(part("target", "C9", stages=DCAM))
    result = validate(proposal(sp(assumptions=[ref("C9")], allocations=[alloc("select_relevant_information"),
                                                                        alloc("interpret_evidence")])),
                      task=task, ctx=ctx)
    assert result.segments[0].relevant_safe_assumptions[0].safe_stages == DCAM
    assert allocations_of(result) == [("select_relevant_information", ORYX), ("interpret_evidence", ORYX)]


def test_case_6_low_assumptions_and_complex_explicit_task_is_still_treated():
    task = profile(seg("Fais la reverse valuation complète.", operations=("apply_procedure", "calculate",
                                                                          "construct_reasoning", "form_conclusion"),
                       characteristics=("multi_step", "domain_specialized")))
    ctx = context(part("target", "C10", ("C10_B", D)), [part("supporting", "C7", ("C7_C", ()))])
    result = validate(proposal(sp(bridges=[ref("C10", "C10_B@r1"), ref("C7", "C7_C@r1")],
                                  allocations=[alloc(o) for o in task.segments[0].cognitive_operations])),
                      task=task, ctx=ctx)
    segment = result.segments[0]
    assert [b.competency_code for b in segment.conceptual_bridges] == ["C10", "C7"]
    assert len(segment.operation_allocations) == 4 and segment.posture == EXPLAIN


def test_case_7_unresolved_focus_keeps_posture_and_allocation():
    task = profile(GUIDE_SEG)
    result = validate(proposal(sp(allocations=[alloc("calculate", USER_RESERVED)])), task=task,
                      ctx=context(status="ambiguous"))
    assert result.segments[0].posture == GUIDE and allocations_of(result) == [("calculate", USER_RESERVED)]


def test_case_8_multi_segments_are_planned_separately_in_order():
    task = profile(EXPLAIN_SEG, GUIDE_SEG, CHALLENGE_SEG)
    result = validate(proposal(
        sp(0, assumptions=[ref("C8")], allocations=[alloc("explain_mechanism")]),
        sp(1, bridges=[ref("C10", "C10_B@r1")], allocations=[alloc("calculate", USER_RESERVED)]),
        sp(2, bridges=[ref("C9", "C9_C@r1")], allocations=[alloc("stress_test", JOINT)])), task=task)
    assert [s.posture for s in result.segments] == [EXPLAIN, GUIDE, CHALLENGE]
    assert [[a.competency_code for a in s.relevant_safe_assumptions] for s in result.segments] == [["C8"], [], []]
    assert [[b.competency_code for b in s.conceptual_bridges] for s in result.segments] == [[], ["C10"], ["C9"]]
    assert [allocations_of(result, i) for i in range(3)] == [
        [("explain_mechanism", ORYX)], [("calculate", USER_RESERVED)], [("stress_test", JOINT)]]


def test_real_chain_6_1b_6_1c_6_1d_6_1e_then_6_2c():
    # 6-1B (validation réelle d'une proposition sur la taxonomie) -> 6-1C ->
    # 6-1D -> 6-1E, puis préflight, validation et projection 6-2C.
    focus = validate_focus_proposal(FocusProposal(
        schema_version=FOCUS_SCHEMA_VERSION, policy_version=FOCUS_POLICY_VERSION, resolution_status="resolved",
        target=ProposedCompetencyFocus(competency_code="C8", scope_mode="localized",
                                       capability_tokens=("C8_D@r1", "C8_A@r1")),
        supporting=(ProposedCompetencyFocus(competency_code="C7", scope_mode="competency_only",
                                            capability_tokens=()),)), TAXONOMY)
    st = state(snap("C8", "application", {"discovery": ((d("C8_A"), d("C8_D")), False),
                                          "comprehension": ((d("C8_A"),), False),
                                          "application": ((d("C8_A"),), False)}))
    ctx = build_pedagogical_response_context(focus=focus, plan=safe_plan(st, focus))
    task = profile(GUIDE_SEG)
    catalogue = catalogue_of(task=task, ctx=ctx)
    assert [(c.competency_code, [(k.capability_token, k.safe_stages) for k in c.capabilities],
             c.competency_only_safe_stages) for c in catalogue.competencies] == [
        ("C8", [("C8_A@r1", DCA), ("C8_D@r1", D)], None), ("C7", [], ())]
    plan_ = validate(proposal(sp(assumptions=[ref("C8", "C8_A@r1")], bridges=[ref("C7"), ref("C8", "C8_D@r1")],
                                 allocations=[alloc("apply_procedure"), alloc("calculate", USER_RESERVED)])),
                     task=task, ctx=ctx)
    assert plan_.segments[0].relevant_safe_assumptions == (RelevantSafeAssumption(
        competency_code="C8", capability_definition_id=d("C8_A"), safe_stages=DCA),)
    assert [b.competency_code for b in plan_.segments[0].conceptual_bridges] == ["C8", "C7"]
    projection = project_support_plan(plan_, catalogue=catalogue)
    assert projection.segments[0].conceptual_bridges[1].capabilities == ()
    # Compétence C7 sans état Step 5 : pont possible, jamais un présupposé.
    with pytest.raises(InvalidSupportPlanProposal, match="aucun stade sûr"):
        validate(proposal(sp(assumptions=[ref("C7")], allocations=[alloc("calculate", USER_RESERVED)])),
                 task=task, ctx=ctx)


# --------------------------------------------------------------------------
# 10. Contrat statique : pureté, isolation, absence de valeur probante
# --------------------------------------------------------------------------

def _source():
    return MODULE_PATH.read_text(encoding="utf-8")


def _tree():
    return ast.parse(_source())


def _imports():
    imported = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_imports_are_only_the_public_upstream_contracts():
    assert _imports() == {"uuid", "collections.abc", "dataclasses", "core.adaptation_assumptions",
                          "core.adaptation_context", "core.adaptation_focus", "core.adaptation_posture",
                          "core.adaptation_task"}


def test_no_private_upstream_name_nor_builder_nor_classifier_is_imported():
    names = {a.name for n in ast.walk(_tree()) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not [name for name in names if name.startswith("_")]
    for name in ("build_pedagogical_response_context", "build_safe_assumption_plan", "build_assumption_baseline",
                 "load_current_focus_taxonomy", "validate_task_profile_proposal", "classify_interaction_task",
                 "classify_focus_proposal", "InteractionTaskInput", "TaskProfileProposal"):
        assert name not in names, name
    text = _source()
    for module in ("adaptation_state", "adaptation_safety", "adaptation_task_classifier",
                   "adaptation_focus_classifier", "taxonomy_service"):
        assert module not in text, module
    for module in _imports():
        for word in ("inference", "longitudinal", "observation", "cognitive_capture", "taxonomy"):
            assert word not in module, module


def test_no_db_clock_env_network_llm_nor_persistence():
    assert not {"os", "sys", "random", "time", "datetime", "json", "sqlalchemy", "core.db", "core.models",
                "requests", "httpx", "socket", "anthropic", "openai", "pathlib", "io", "secrets"} & _imports()
    tokens = _code_tokens(_source()).split()
    for word in ("db", "session", "commit", "flush", "execute", "query", "now", "random", "environ", "getenv",
                 "open", "__tablename__", "Column", "Base", "complete", "backend", "llm", "save", "persist", "cache",
                 "lru_cache", "global", "uuid4", "uuid5"):
        assert word not in tokens, word


def test_db_is_never_touched_at_runtime(monkeypatch):
    import core.db as db

    def _forbidden(*args, **kwargs):
        raise AssertionError("accès base interdit en 6-2C")

    for name in dir(db):
        if callable(getattr(db, name)) and not name.startswith("__"):
            monkeypatch.setattr(db, name, _forbidden, raising=False)
    plan_, catalogue = _plan_and_catalogue()
    assert project_support_plan(plan_, catalogue=catalogue).segments


FORBIDDEN_NOTIONS = ("support_score", "difficulty_score", "gap_score", "low_support", "medium_support",
                     "high_support", "beginner", "advanced", "weak_user", "strong_user", "autonomy_score",
                     "actual_support_trace", "support_trace", "confidence", "score", "observation", "claim",
                     "current_stage", "validation_constraints", "activation_rule", "inference_run_id",
                     "state_generation", "user_id", "next_move", "movement", "surface", "route", "academy",
                     "clarify", "deepen", "generalize", "integrate", "stay", "easy", "medium", "hard", "level")


def test_no_score_level_trace_movement_surface_nor_step5_notion():
    tokens = _code_tokens(_source()).lower()
    words = set(tokens.split())
    parts = {part for token in words for part in re.split(r"[^a-z0-9]+|_", token)}
    for notion in FORBIDDEN_NOTIONS:
        assert notion not in words, notion
        if "_" not in notion:
            assert notion not in parts, notion


def test_no_numeric_computation_on_stages():
    floats = [n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and type(n.value) is float]
    assert floats == []
    ints = {n.value for n in ast.walk(_tree()) if isinstance(n, ast.Constant) and type(n.value) is int}
    assert ints <= {0, 1}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "index":
            raise AssertionError("aucune position de stade calculée")
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert not names & {"ASSUMPTION_STAGE_ORDER", "safe_stages", "stages"}, names
        if isinstance(node, ast.Compare) and any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                                                 for op in node.ops):
            raise AssertionError("aucune comparaison d'ordre (stade, posture, quantité)")


def test_no_operation_to_stage_or_capability_mapping_is_coded():
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            assert not keys, keys
    text = _code_tokens(_source())
    assert not re.search(r"C\d+_[A-Z]", text)  # aucune capacité nommée en dur


def test_not_wired_to_api_web_chat_nor_migrated():
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/adaptation_support.py" and "adaptation_support" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    # Consommateurs : le classificateur 6-2C2 et, Étape 6.3A, le mouvement
    # pédagogique (préflight et projection 6-2C réutilisés, jamais le
    # validateur ni le classificateur), eux-mêmes non branchés.
    assert sorted(users) == ["core/adaptation_movement.py", "core/adaptation_support_classifier.py"]
    tree = ast.parse((REPO_ROOT / "core" / "adaptation_movement.py").read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                and n.module == "core.adaptation_support" for a in n.names}
    assert imported == {
        "COMPETENCY_ONLY", "LOCALIZED", "NO_TASK", "RESOLVED", "CurrentFocusTaxonomy", "IncompatibleSupportPlanInputs",
        "InteractionPostureBaseline", "InteractionSupportPlan", "InteractionTaskProfile", "InvalidSupportPlanArgument",
        "PedagogicalResponseContext", "PlannedOperationAllocation", "ProjectedCapability", "ProjectedConceptualBridge",
        "ProjectedSafeAssumption", "RestrictedSupportCatalogue", "SupportPlanError", "UnsupportedSupportPlanVersion",
        "prepare_support_planning", "project_support_plan"}
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("validate_support_plan_proposal", "prepare_support_planning", "project_support_plan",
                 "InteractionSupportPlan", "SupportGenerationProjection", "SUPPORT_POLICY_VERSION"):
        assert name not in api, name
    tokens = _code_tokens(_source())
    for word in ("web_chat", "fastapi", "request_handler", "api"):
        assert word not in tokens.split(), word
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0013_decryptage_conversation_affinity.py" and len(versions) == 13
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    for word in ("support_plan", "SupportPlan", "operation_allocation", "conceptual_bridge", "posture"):
        assert word not in models, word


def test_step5_and_upstream_modules_are_never_mutated():
    # 6-2C n'écrit jamais dans un objet reçu : aucune affectation d'attribut
    # hors des constructeurs, aucun object.__setattr__.
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assert not [t for t in targets if isinstance(t, ast.Attribute)], ast.dump(node)
    assert "__setattr__" not in _code_tokens(_source())


@pytest.mark.parametrize("mutate", [
    lambda p: _segment(p, conceptual_bridges=(ConceptualBridge(competency_code=["C10"], scope_mode="localized",
                                                               capability_definition_ids=(d("C10_B"),)),)),
    lambda p: _segment(p, conceptual_bridges=(ConceptualBridge(competency_code="C10", scope_mode="localized",
                                                               capability_definition_ids=([d("C10_B")],)),)),
    lambda p: _segment(p, relevant_safe_assumptions=(RelevantSafeAssumption(
        competency_code=["C8"], capability_definition_id=None, safe_stages=DC),)),
    lambda p: _segment(p, relevant_safe_assumptions=(RelevantSafeAssumption(
        competency_code="C10", capability_definition_id=str(d("C10_B")), safe_stages=D),)),
    lambda p: _segment(p, operation_allocations=(PlannedOperationAllocation(operation=["explain_mechanism"],
                                                                            allocation=ORYX),)),
    lambda p: dataclasses.replace(p, pedagogical_resolution_status=type("Text", (str,), {})("resolved")),
])
def test_projection_refuses_unhashable_or_untyped_forged_values_without_crashing(mutate):
    plan_, catalogue = _plan_and_catalogue()
    with pytest.raises(SupportPlanError):
        project_support_plan(mutate(plan_), catalogue=catalogue)
