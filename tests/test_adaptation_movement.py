"""Tests de l'Étape 6.3A : mouvement pédagogique marginal
(core/adaptation_movement.py).

1. Contrats : versions, vocabulaire fermé et non ordonné, dataclasses
   immuables, API publique, hiérarchie d'erreurs, proposition sans score /
   stade / rationale / surface.
2. Préflight tâche <-> posture <-> support <-> contexte <-> taxonomie :
   frontières 6-2C réutilisées, appariement exact, jamais rapproché.
3. Stay : décision positive, sans ancrage ni intention.
4. Mouvements autres que stay : ancrage et movement_intent obligatoires.
5. localized / competency_only : jamais un périmètre élargi.
6. Canonicalisation et doublons.
7. Statuts : no_task et focus non résolu => seul stay (politique V1).
8. Déterminisme, immutabilité, fail-closed (jamais un stay de repli).
9. Projection de génération (aucun UUID, aucune provenance, aucun stade).
10. Chaîne réelle 6-1B -> 6-1C -> 6-1D -> 6-1E -> 6-2C -> 6.3.
11. Contrat statique : pureté, isolation, aucune DB / migration / runtime,
    aucune table stade / posture / opération -> mouvement, aucun rang,
    aucune surface, aucune contrainte de validation.
"""
import ast
import dataclasses
import inspect
import re
import uuid

import pytest

from core import adaptation_movement as movement_module
from core.adaptation_context import build_pedagogical_response_context
from core.adaptation_focus import (
    FOCUS_POLICY_VERSION,
    FOCUS_SCHEMA_VERSION,
    FocusProposal,
    ProposedCompetencyFocus,
    validate_focus_proposal,
)
from core.adaptation_movement import (
    APPLY,
    CLARIFY,
    DEEPEN,
    GENERALIZE,
    INTEGRATE,
    MAX_MOVEMENT_INTENT_CHARS,
    MOVEMENT_POLICY_VERSION,
    MOVEMENT_SCHEMA_VERSION,
    PEDAGOGICAL_MOVEMENTS,
    STAY,
    IncompatibleMovementInputs,
    InteractionPedagogicalMovement,
    InvalidMovementArgument,
    InvalidMovementContent,
    InvalidMovementProposal,
    MovementAnchor,
    MovementCatalogueCapability,
    MovementCatalogueCompetency,
    MovementError,
    MovementGenerationProjection,
    MovementPlanningInput,
    MovementProposal,
    MovementSegmentInput,
    PlannedSegmentSupport,
    ProjectedMovementCompetency,
    UnsupportedMovementVersion,
    prepare_movement_planning,
    project_pedagogical_movement,
    validate_movement_proposal,
)
from core.adaptation_posture import CHALLENGE, CO_REASON, EXPLAIN, GUIDE, build_posture_baseline
from core.adaptation_safety import MinimalValidationConstraint
from core.adaptation_support import (
    JOINT,
    ORYX,
    SUPPORT_PLAN_SCHEMA_VERSION,
    SUPPORT_POLICY_VERSION,
    USER_RESERVED,
    InteractionSupportPlan,
    PlannedOperationAllocation,
    ProjectedCapability,
    SupportPlanError,
    SupportPlanProposal,
    validate_support_plan_proposal,
)
from core.adaptation_task import NO_TASK, TASK_REQUESTED
from tests.test_adaptation_assumptions import RELEASE, d, snap, state, uid
from tests.test_adaptation_safety import plan as safe_plan
from tests.test_adaptation_support import (
    CHALLENGE_SEG,
    DC,
    DCA,
    DCAM,
    EXPLAIN_SEG,
    GUIDE_SEG,
    JOINT_SEG,
    SPEC_CAPABILITIES,
    SPEC_COMPETENCIES,
    TAXONOMY,
    D,
    alloc,
    context,
    default_context,
    inputs,
    make_taxonomy,
    part,
    profile,
    ref,
    seg,
    sp,
)
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "adaptation_movement.py"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
NON_STAY = (CLARIFY, DEEPEN, APPLY, GENERALIZE, INTEGRATE)
INTENT = "Relier le ROIC historique au rendement du capital supplémentaire."


# --------------------------------------------------------------------------
# Constructeurs : un plan 6-2C validé par la vraie porte, puis 6.3
# --------------------------------------------------------------------------

def _default_allocations(posture, operations):
    """Allocation minimale valide sous support-policy-1."""
    if posture == GUIDE:
        return (alloc(operations[0], USER_RESERVED),)
    if posture == CO_REASON:
        return (alloc(operations[0], JOINT),)
    return ()


def support_for(base, segments=None):
    """Plan 6-2C validé pour des entrées 6-2A / 6-2B / 6-1E / 6-1B."""
    task, posture = base["task_profile"], base["posture_baseline"]
    if segments is None:
        segments = tuple(sp(i, allocations=_default_allocations(decision.posture, segment.cognitive_operations))
                         for i, (segment, decision) in enumerate(zip(task.segments, posture.segments)))
    return validate_support_plan_proposal(SupportPlanProposal(
        schema_version=SUPPORT_PLAN_SCHEMA_VERSION, policy_version=SUPPORT_POLICY_VERSION, segments=tuple(segments)),
        **base)


def movement_inputs(task=None, ctx=None, taxonomy=None, posture=None, support=None, support_segments=None):
    base = inputs(task=task, ctx=ctx, taxonomy=taxonomy, posture=posture)
    if support is None:
        support = support_for(base, support_segments)
    return dict(base, support_plan=support)


def mp(movement=STAY, indices=(), codes=(), tokens=(), intent=None, **overrides):
    kwargs = dict(schema_version=MOVEMENT_SCHEMA_VERSION, policy_version=MOVEMENT_POLICY_VERSION, movement=movement,
                  segment_indices=tuple(indices), competency_codes=tuple(codes), capability_tokens=tuple(tokens),
                  movement_intent=intent)
    kwargs.update(overrides)
    return MovementProposal(**kwargs)


def deepen(**overrides):
    kwargs = dict(movement=DEEPEN, indices=(0,), codes=("C10",), tokens=("C10_B@r1",), intent=INTENT)
    kwargs.update(overrides)
    return mp(**kwargs)


def validate(proposal, **kwargs):
    return validate_movement_proposal(proposal, **movement_inputs(**kwargs))


def refused(proposal, match=None, error=InvalidMovementProposal, **kwargs):
    with pytest.raises(error, match=match) as caught:
        validate(proposal, **kwargs)
    return caught.value


def project(movement, **kwargs):
    return project_pedagogical_movement(movement, **movement_inputs(**kwargs))


def _walk(value):
    yield value
    if dataclasses.is_dataclass(value):
        for field in dataclasses.fields(value):
            yield from _walk(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from _walk(item)


# --------------------------------------------------------------------------
# 1. Contrats
# --------------------------------------------------------------------------

def test_frozen_distinct_versions():
    assert MOVEMENT_SCHEMA_VERSION == "interaction-pedagogical-movement-v1"
    assert MOVEMENT_POLICY_VERSION == "movement-policy-1"
    assert len({MOVEMENT_SCHEMA_VERSION, MOVEMENT_POLICY_VERSION, SUPPORT_PLAN_SCHEMA_VERSION,
                SUPPORT_POLICY_VERSION}) == 4


def test_movement_vocabulary_is_closed_and_exact():
    assert (STAY, CLARIFY, DEEPEN, APPLY, GENERALIZE, INTEGRATE) == (
        "stay", "clarify", "deepen", "apply", "generalize", "integrate")
    assert PEDAGOGICAL_MOVEMENTS == (STAY, CLARIFY, DEEPEN, APPLY, GENERALIZE, INTEGRATE)
    assert type(PEDAGOGICAL_MOVEMENTS) is tuple and all(type(m) is str for m in PEDAGOGICAL_MOVEMENTS)


def test_movements_are_neither_ranked_nor_numbered():
    # Aucune valeur numérique, aucun rang, aucun « mouvement suivant ».
    for name, value in vars(movement_module).items():
        assert not (isinstance(value, dict) and set(value) & set(PEDAGOGICAL_MOVEMENTS)), name
        if name.isupper() and isinstance(value, int):
            assert name == "MAX_MOVEMENT_INTENT_CHARS", name
    for name in dir(movement_module):
        for word in ("rank", "order", "level", "next", "higher", "score", "index_of"):
            assert word not in name.lower(), name


def test_movement_bound_is_explicit():
    assert MAX_MOVEMENT_INTENT_CHARS == 500 and type(MAX_MOVEMENT_INTENT_CHARS) is int


EXPECTED_FIELDS = {
    MovementCatalogueCapability: ["capability_token", "label", "definition", "safe_stages"],
    MovementCatalogueCompetency: ["role", "competency_code", "competency_label", "central_question", "scope_mode",
                                  "capabilities", "competency_only_safe_stages"],
    PlannedSegmentSupport: ["relevant_safe_assumptions", "conceptual_bridges", "operation_allocations"],
    MovementSegmentInput: ["segment_index", "source_excerpt", "explicit_intent", "cognitive_operations",
                           "task_characteristics", "posture", "planned_support"],
    MovementPlanningInput: ["request_status", "pedagogical_resolution_status", "segments", "catalogue"],
    MovementProposal: ["schema_version", "policy_version", "movement", "segment_indices", "competency_codes",
                       "capability_tokens", "movement_intent"],
    MovementAnchor: ["segment_indices", "competency_codes", "capability_definition_ids"],
    InteractionPedagogicalMovement: ["schema_version", "policy_version", "source_task_schema_version",
                                     "source_task_policy_version", "source_posture_schema_version",
                                     "source_posture_policy_version", "source_support_schema_version",
                                     "source_support_policy_version", "source_pedagogical_context_schema_version",
                                     "request_status", "pedagogical_resolution_status", "movement", "anchor",
                                     "movement_intent"],
    ProjectedMovementCompetency: ["competency_code", "competency_label", "capabilities"],
    MovementGenerationProjection: ["schema_version", "policy_version", "movement", "movement_intent",
                                   "segment_indices", "competencies"],
}


def test_dataclasses_are_frozen_keyword_only_with_exact_fields():
    for cls, names in EXPECTED_FIELDS.items():
        assert [f.name for f in dataclasses.fields(cls)] == names, cls
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls)), cls


def test_public_api_is_preflight_validator_and_projection():
    public = sorted(name for name, value in vars(movement_module).items()
                    if inspect.isfunction(value) and value.__module__ == movement_module.__name__
                    and not name.startswith("_"))
    assert public == ["prepare_movement_planning", "project_pedagogical_movement", "validate_movement_proposal"]
    names = ["task_profile", "posture_baseline", "support_plan", "context", "taxonomy"]
    for function, expected in ((prepare_movement_planning, names),
                               (validate_movement_proposal, ["proposal", *names]),
                               (project_pedagogical_movement, ["movement", *names])):
        parameters = inspect.signature(function).parameters
        assert list(parameters) == expected
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for name, p in parameters.items() if name in names)


def test_errors_are_a_dedicated_hierarchy():
    assert MovementError.__bases__ == (Exception,)
    for error in (InvalidMovementArgument, UnsupportedMovementVersion, IncompatibleMovementInputs,
                  InvalidMovementContent, InvalidMovementProposal):
        assert error.__bases__ == (MovementError,)
    assert not issubclass(MovementError, SupportPlanError)


FORBIDDEN_FIELDS = ("confidence", "probability", "score", "value_score", "difficulty", "stage", "rationale",
                    "diagnosis", "validation_need", "validation_constraints", "next_step", "next_movement",
                    "surface", "route", "cta", "question", "exercise", "response", "answer", "rank", "level",
                    "user_id", "person_id", "inference_run_id", "state_generation", "provenance", "mapping_guidance",
                    "taxonomy_release_id", "taxonomy_spec_fingerprint", "definition_id", "autonomy")


def test_proposal_movement_and_projection_carry_no_forbidden_field():
    for cls in (MovementProposal, MovementAnchor, InteractionPedagogicalMovement, MovementGenerationProjection,
                ProjectedMovementCompetency):
        for field in dataclasses.fields(cls):
            for word in FORBIDDEN_FIELDS:
                if cls is MovementAnchor and field.name == "capability_definition_ids":
                    continue
                assert word not in field.name, (cls, field.name)


def test_planning_input_carries_no_identity_provenance_constraint_nor_mapping_guidance():
    for cls in (MovementCatalogueCapability, MovementCatalogueCompetency, PlannedSegmentSupport,
                MovementSegmentInput, MovementPlanningInput):
        for field in dataclasses.fields(cls):
            for word in ("uuid", "definition_id", "release", "fingerprint", "provenance", "validation", "constraint",
                         "mapping_guidance", "confidence", "tension", "observation", "user", "history",
                         "transaction", "activity"):
                assert word not in field.name, (cls, field.name)
    assert "safe_stages" not in [f.name for f in dataclasses.fields(MovementGenerationProjection)]


# --------------------------------------------------------------------------
# 2. Préflight
# --------------------------------------------------------------------------

def test_valid_inputs_are_prepared_with_task_posture_support_and_restricted_catalogue():
    task = profile(EXPLAIN_SEG, GUIDE_SEG)
    support_segments = (sp(0, assumptions=[ref("C8")], bridges=[ref("C10", "C10_B@r1")],
                           allocations=[alloc("explain_mechanism")]),
                        sp(1, allocations=[alloc("calculate", USER_RESERVED)]))
    planning = prepare_movement_planning(**movement_inputs(task=task, support_segments=support_segments))
    assert type(planning) is MovementPlanningInput
    assert (planning.request_status, planning.pedagogical_resolution_status) == (TASK_REQUESTED, "resolved")
    assert [(s.segment_index, s.source_excerpt, s.explicit_intent, s.posture) for s in planning.segments] == [
        (0, EXPLAIN_SEG.source_excerpt, "direct_answer_requested", EXPLAIN),
        (1, GUIDE_SEG.source_excerpt, "reasoning_reserved_for_user", GUIDE)]
    assert planning.segments[1].cognitive_operations == GUIDE_SEG.cognitive_operations
    first, second = (segment.planned_support for segment in planning.segments)
    assert [(a.competency_code, a.capability, a.safe_stages) for a in first.relevant_safe_assumptions] == [
        ("C8", None, DC)]
    assert [(b.competency_code, [c.capability_token for c in b.capabilities]) for b in first.conceptual_bridges] == [
        ("C10", ["C10_B@r1"])]
    assert second.operation_allocations == (PlannedOperationAllocation(operation="calculate",
                                                                       allocation=USER_RESERVED),)
    assert [(c.role, c.competency_code, c.scope_mode) for c in planning.catalogue] == [
        ("target", "C10", "localized"), ("supporting", "C8", "competency_only"), ("supporting", "C9", "localized")]
    c10, c8, c9 = planning.catalogue
    assert c10.competency_label == SPEC_COMPETENCIES["C10"]["label"]
    assert c10.central_question == SPEC_COMPETENCIES["C10"]["central_question"]
    assert [(c.capability_token, c.label, c.definition, c.safe_stages) for c in c10.capabilities] == [
        ("C10_B@r1", SPEC_CAPABILITIES["C10_B"]["label"], SPEC_CAPABILITIES["C10_B"]["definition"], D),
        ("C10_D@r1", SPEC_CAPABILITIES["C10_D"]["label"], SPEC_CAPABILITIES["C10_D"]["definition"], ())]
    assert c8.capabilities == () and c8.competency_only_safe_stages == DC
    assert c9.capabilities[0].safe_stages == DCAM and c10.competency_only_safe_stages is None


def test_planning_input_has_no_uuid_mapping_guidance_nor_validation_constraint():
    constraint = MinimalValidationConstraint(
        intent="revalidation", target_stage="application", scope_mode="localized",
        capability_definition_ids=(d("C10_B"),),
        activation_rule="only_if_naturally_relevant_and_not_direct_answer_or_explanation",
        residual_work_rule="preserve_minimal_residual_user_work")
    ctx = context(part("target", "C10", ("C10_B", DCA), constraints=[constraint]),
                  [part("supporting", "C8", stages=DC)])
    planning = prepare_movement_planning(**movement_inputs(
        ctx=ctx, support_segments=(sp(assumptions=[ref("C10", "C10_B@r1")]),)))
    text = repr(planning)
    assert not UUID_PATTERN.search(text)
    for word in ("mapping_guidance", "validation", "constraint", "revalidation", "activation_rule", "provenance",
                 "inference_run_id", "state_generation", "release", "fingerprint", str(RELEASE)):
        assert word not in text, word
    assert not [value for value in _walk(planning) if type(value) in (uuid.UUID, MinimalValidationConstraint)]


def test_preflight_reuses_the_public_6_2c_boundaries(monkeypatch):
    calls = []
    for name in ("prepare_support_planning", "project_support_plan"):
        real = getattr(movement_module, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)
        monkeypatch.setattr(movement_module, name, spy)
    prepare_movement_planning(**movement_inputs())
    assert calls == ["prepare_support_planning", "project_support_plan"]


def test_the_support_plan_received_is_carried_never_recomputed():
    base = inputs()
    lean = support_for(base)
    rich = support_for(base, (sp(assumptions=[ref("C8")], bridges=[ref("C9", "C9_C@r1")],
                                 allocations=[alloc("form_conclusion")]),))
    lean_planning = prepare_movement_planning(**dict(base, support_plan=lean))
    rich_planning = prepare_movement_planning(**dict(base, support_plan=rich))
    assert lean_planning.segments[0].planned_support == PlannedSegmentSupport(
        relevant_safe_assumptions=(), conceptual_bridges=(), operation_allocations=())
    rich_support = rich_planning.segments[0].planned_support
    assert [b.competency_code for b in rich_support.conceptual_bridges] == ["C9"]
    assert [a.operation for a in rich_support.operation_allocations] == ["form_conclusion"]


def _replace_segment(plan, index=0, **changes):
    segments = list(plan.segments)
    segments[index] = dataclasses.replace(segments[index], **changes)
    return dataclasses.replace(plan, segments=tuple(segments))


def _other_task_support():
    other = profile(seg("Une autre demande.", "direct_answer_requested", EXPLAIN_SEG.cognitive_operations))
    return support_for(inputs(task=other))


MISMATCHES = {
    "task / posture : posture d'un autre profil": (
        lambda: dict(movement_inputs(), posture_baseline=build_posture_baseline(profile(GUIDE_SEG))),
        IncompatibleMovementInputs),
    "task / posture : posture divergente de 6-2B": (
        lambda: dict(movement_inputs(), posture_baseline=dataclasses.replace(
            build_posture_baseline(profile(EXPLAIN_SEG)), segments=(dataclasses.replace(
                build_posture_baseline(profile(EXPLAIN_SEG)).segments[0], posture=CHALLENGE),))),
        IncompatibleMovementInputs),
    "task / support : extrait d'une autre tâche": (
        lambda: dict(movement_inputs(), support_plan=_other_task_support()), IncompatibleMovementInputs),
    "task / support : opération allouée absente du segment 6-2A": (
        lambda: dict(movement_inputs(), support_plan=_replace_segment(
            movement_inputs()["support_plan"],
            operation_allocations=(PlannedOperationAllocation(operation="calculate", allocation=ORYX),))),
        IncompatibleMovementInputs),
    "task / support : request_status": (
        lambda: dict(movement_inputs(), support_plan=support_for(inputs(task=profile(status=NO_TASK)))),
        IncompatibleMovementInputs),
    "task / support : nombre de segments": (
        lambda: dict(movement_inputs(), support_plan=support_for(inputs(task=profile(EXPLAIN_SEG, EXPLAIN_SEG)))),
        IncompatibleMovementInputs),
    "posture / support : posture du plan différente de 6-2B": (
        lambda: dict(movement_inputs(), support_plan=_replace_segment(movement_inputs()["support_plan"],
                                                                      posture=CHALLENGE)),
        IncompatibleMovementInputs),
    "support : segment_index non canonique": (
        lambda: dict(movement_inputs(), support_plan=_replace_segment(movement_inputs()["support_plan"],
                                                                      segment_index=1)),
        InvalidMovementContent),
    "support : posture du plan hors des règles 6-2C": (
        lambda: dict(movement_inputs(), support_plan=_replace_segment(movement_inputs()["support_plan"],
                                                                      posture=GUIDE)),
        InvalidMovementContent),
    "support / contexte : présupposé hors du catalogue de ce contexte": (
        lambda: dict(movement_inputs(ctx=context(part("target", "C10", ("C10_B", D)))),
                     support_plan=support_for(inputs(), (sp(assumptions=[ref("C8")]),))),
        IncompatibleMovementInputs),
    "support / contexte : statut de résolution": (
        lambda: dict(movement_inputs(), support_plan=support_for(inputs(ctx=context(status="neutral")))),
        IncompatibleMovementInputs),
    "support : version source de tâche": (
        lambda: dict(movement_inputs(), support_plan=dataclasses.replace(
            movement_inputs()["support_plan"], source_task_schema_version="interaction-task-profile-v2")),
        UnsupportedMovementVersion),
    "support : version source de posture": (
        lambda: dict(movement_inputs(), support_plan=dataclasses.replace(
            movement_inputs()["support_plan"], source_posture_policy_version="posture-policy-0")),
        UnsupportedMovementVersion),
    "support : version source du contexte": (
        lambda: dict(movement_inputs(), support_plan=dataclasses.replace(
            movement_inputs()["support_plan"], source_pedagogical_context_schema_version="ctx-v0")),
        UnsupportedMovementVersion),
    "support : version du plan": (
        lambda: dict(movement_inputs(), support_plan=dataclasses.replace(
            movement_inputs()["support_plan"], schema_version="interaction-support-plan-v2")),
        UnsupportedMovementVersion),
    "contexte / taxonomie : autre release": (
        lambda: dict(movement_inputs(), taxonomy=make_taxonomy(release=uid("release-other"))),
        IncompatibleMovementInputs),
    "contexte / taxonomie : autre fingerprint annoncé": (
        lambda: dict(movement_inputs(), context=context(part("target", "C10", ("C10_B", D)),
                                                        taxonomy_spec_fingerprint="f" * 64)),
        IncompatibleMovementInputs),
    "contexte : version non supportée": (
        lambda: dict(movement_inputs(), context=context(part("target", "C10", ("C10_B", D)),
                                                        focus_policy_version="focus-x")),
        UnsupportedMovementVersion),
    "contexte : résolu sans cible": (lambda: dict(movement_inputs(), context=context(None)), InvalidMovementContent),
    "tâche : version non supportée": (
        lambda: dict(movement_inputs(), task_profile=dataclasses.replace(profile(EXPLAIN_SEG),
                                                                         schema_version="task-v0")),
        UnsupportedMovementVersion),
}


@pytest.mark.parametrize("name", list(MISMATCHES))
def test_every_mismatch_is_refused_fail_closed_before_any_model(name):
    build, error = MISMATCHES[name]
    corrupted = build()
    for call in (lambda: prepare_movement_planning(**corrupted),
                 lambda: validate_movement_proposal(mp(), **corrupted),
                 lambda: validate_movement_proposal(deepen(), **corrupted)):
        with pytest.raises(error):
            call()


def test_upstream_errors_are_chained_never_absorbed():
    with pytest.raises(IncompatibleMovementInputs) as caught:
        prepare_movement_planning(**dict(movement_inputs(), taxonomy=make_taxonomy(release=uid("release-other"))))
    assert isinstance(caught.value.__cause__, SupportPlanError)


@pytest.mark.parametrize("name", ["task_profile", "posture_baseline", "support_plan", "context", "taxonomy"])
def test_wrong_input_types_are_refused(name):
    corrupted = dict(movement_inputs(), **{name: "objet"})
    with pytest.raises(InvalidMovementArgument, match=name):
        prepare_movement_planning(**corrupted)


def test_input_subclasses_are_refused():
    sub = type("Plan", (InteractionSupportPlan,), {})
    plan = movement_inputs()["support_plan"]
    forged = sub(**{f.name: getattr(plan, f.name) for f in dataclasses.fields(plan)})
    with pytest.raises(InvalidMovementArgument):
        prepare_movement_planning(**dict(movement_inputs(), support_plan=forged))


def test_corrupted_inputs_never_become_a_stay():
    corrupted = dict(movement_inputs(), support_plan=_other_task_support())
    with pytest.raises(MovementError):
        validate_movement_proposal(mp(), **corrupted)


# --------------------------------------------------------------------------
# 3. Stay
# --------------------------------------------------------------------------

def test_stay_is_a_valid_positive_decision_without_anchor_nor_intent():
    result = validate(mp())
    assert type(result) is InteractionPedagogicalMovement
    assert (result.movement, result.anchor, result.movement_intent) == (STAY, None, None)
    assert (result.request_status, result.pedagogical_resolution_status) == (TASK_REQUESTED, "resolved")


@pytest.mark.parametrize("proposal", [
    mp(indices=(0,)), mp(codes=("C10",)), mp(tokens=("C10_B@r1",)), mp(intent=INTENT), mp(intent=""),
    mp(intent="   "), mp(indices=(0,), codes=("C10",), tokens=("C10_B@r1",), intent=INTENT), mp(codes=("C8",)),
])
def test_stay_with_anchor_or_intent_is_refused(proposal):
    refused(proposal, "stay")


def test_movement_header_carries_the_exact_source_versions():
    base = movement_inputs()
    result = validate_movement_proposal(deepen(), **base)
    task, posture, plan, ctx = (base["task_profile"], base["posture_baseline"], base["support_plan"], base["context"])
    assert (result.schema_version, result.policy_version) == (MOVEMENT_SCHEMA_VERSION, MOVEMENT_POLICY_VERSION)
    assert (result.source_task_schema_version, result.source_task_policy_version) == (task.schema_version,
                                                                                      task.policy_version)
    assert (result.source_posture_schema_version, result.source_posture_policy_version) == (posture.schema_version,
                                                                                            posture.policy_version)
    assert (result.source_support_schema_version, result.source_support_policy_version) == (plan.schema_version,
                                                                                            plan.policy_version)
    assert result.source_pedagogical_context_schema_version == ctx.schema_version


# --------------------------------------------------------------------------
# 4. Mouvements autres que stay
# --------------------------------------------------------------------------

def _valid(movement):
    if movement == INTEGRATE:
        return mp(INTEGRATE, (0,), ("C10", "C8"), ("C10_B@r1",), INTENT)
    return mp(movement, (0,), ("C10",), ("C10_B@r1",), INTENT)


@pytest.mark.parametrize("movement", NON_STAY)
def test_every_non_stay_movement_is_accepted_with_a_complete_anchor(movement):
    result = validate(_valid(movement))
    assert result.movement == movement and result.movement_intent == INTENT
    assert result.anchor.segment_indices == (0,)
    assert result.anchor.capability_definition_ids == (d("C10_B"),)


@pytest.mark.parametrize("movement", NON_STAY)
@pytest.mark.parametrize("change, match", [
    (dict(segment_indices=()), "au moins un segment"),
    (dict(competency_codes=(), capability_tokens=()), "au moins une compétence"),
    (dict(movement_intent=None), "movement_intent"),
    (dict(movement_intent=""), "movement_intent"),
    (dict(movement_intent=" \n\t "), "movement_intent"),
    (dict(movement_intent=7), "movement_intent"),
    (dict(movement_intent="x" * (MAX_MOVEMENT_INTENT_CHARS + 1)), "jamais tronqué"),
])
def test_non_stay_requires_segment_competency_and_bounded_intent(movement, change, match):
    refused(dataclasses.replace(_valid(movement), **change), match)


@pytest.mark.parametrize("movement", NON_STAY)
def test_intent_at_the_bound_is_kept_exactly(movement):
    intent = "  É" + "x" * (MAX_MOVEMENT_INTENT_CHARS - 4) + "\n"
    assert len(intent) == MAX_MOVEMENT_INTENT_CHARS
    result = validate(dataclasses.replace(_valid(movement), movement_intent=intent))
    assert result.movement_intent == intent  # ni strip, ni reformulation, ni troncature


@pytest.mark.parametrize("value", ["progress", "Stay", "STAY", "deepen ", "", None, 1, "advance", "next",
                                   type("Text", (str,), {})("stay")])
def test_unknown_movement_is_refused(value):
    refused(mp(value), "movement")


def test_integrate_requires_two_distinct_competencies():
    refused(mp(INTEGRATE, (0,), (), (), INTENT), "au moins une compétence")
    refused(mp(INTEGRATE, (0,), ("C10",), ("C10_B@r1",), INTENT), "deux compétences")
    refused(mp(INTEGRATE, (0,), ("C10", "C10"), ("C10_B@r1",), INTENT), "double")
    result = validate(mp(INTEGRATE, (0,), ("C10", "C8"), ("C10_B@r1",), INTENT))
    assert result.anchor.competency_codes == ("C10", "C8")
    three = validate(mp(INTEGRATE, (0,), ("C9", "C8", "C10"), ("C9_C@r1", "C10_D@r1"), INTENT))
    assert three.anchor.competency_codes == ("C10", "C8", "C9")


@pytest.mark.parametrize("indices", [(1,), (-1,), (True,), (0.0,), ("0",), (None,), (0, 1)])
def test_segment_index_must_reference_an_existing_segment(indices):
    refused(deepen(indices=indices), "segment_indices")


@pytest.mark.parametrize("field", ["segment_indices", "competency_codes", "capability_tokens"])
def test_anchor_collections_must_be_tuples(field):
    value = {"segment_indices": [0], "competency_codes": ["C10"], "capability_tokens": ["C10_B@r1"]}[field]
    refused(dataclasses.replace(deepen(), **{field: value}), "tuple attendu")


@pytest.mark.parametrize("change", [dict(schema_version="interaction-pedagogical-movement-v2"),
                                    dict(policy_version="movement-policy-0"), dict(schema_version=None),
                                    dict(policy_version=type("Text", (str,), {})(MOVEMENT_POLICY_VERSION))])
def test_proposal_versions_are_exact(change):
    refused(dataclasses.replace(deepen(), **change), "version")


def test_wrong_proposal_type_is_an_argument_error():
    with pytest.raises(InvalidMovementArgument):
        validate({"movement": STAY})
    with pytest.raises(InvalidMovementArgument):
        validate(SupportPlanProposal(schema_version=MOVEMENT_SCHEMA_VERSION, policy_version=MOVEMENT_POLICY_VERSION,
                                     segments=()))


def test_python_never_judges_the_semantics_of_a_movement():
    # Une tâche déjà applicative (calculate) : stay est valide ; apply reste
    # structurellement valide. Aucune règle calculate -> apply, ni l'inverse.
    task = profile(seg("Calcule le ROIC.", "unspecified", ("apply_procedure", "calculate"),
                       ("user_specific_application",)))
    assert validate(mp(), task=task).movement == STAY
    assert validate(_valid(APPLY), task=task).movement == APPLY
    # Une tâche multi-compétences : stay est valide ; integrate n'est jamais
    # imposé par la présence de deux compétences.
    assert validate(mp(), ctx=default_context()).movement == STAY


# --------------------------------------------------------------------------
# 5. localized / competency_only
# --------------------------------------------------------------------------

@pytest.mark.parametrize("codes, tokens, match", [
    (("C10",), (), "localized sans capacité"),
    (("C10",), ("C10_A@r1",), "absent du catalogue restreint"),
    (("C10",), ("C9_C@r1",), "appartient à C9, compétence non déclarée"),
    (("C10",), ("C7_A@r1",), "absent du catalogue restreint"),
    (("C10", "C9"), ("C10_B@r1",), "C9 localized sans capacité"),
    (("C8",), ("C8_A@r1",), "absent du catalogue restreint"),
    (("C8",), ("C8@r1",), "absent du catalogue restreint"),
    (("C10",), ("c10_b@r1",), "absent du catalogue restreint"),
    (("C10",), ("C10_B@r01",), "absent du catalogue restreint"),
    (("C10",), ("C10_B",), "absent du catalogue restreint"),
    (("C10",), (str(d("C10_B")),), "absent du catalogue restreint"),
    (("C10",), (d("C10_B"),), "absent du catalogue restreint"),
    (("C10",), ("C10_B@r1", "C10_B@r1"), "double"),
    (("C7",), ("C7_A@r1",), "hors du catalogue restreint"),
    (("c10",), ("C10_B@r1",), "hors du catalogue restreint"),
])
def test_scope_is_never_widened_nor_invented(codes, tokens, match):
    refused(deepen(codes=codes, tokens=tokens), match)


def test_localized_authorized_tokens_and_competency_only_without_token_are_accepted():
    result = validate(deepen(codes=("C10", "C8"), tokens=("C10_D@r1",)))
    assert result.anchor.competency_codes == ("C10", "C8")
    assert result.anchor.capability_definition_ids == (d("C10_D"),)
    only = validate(deepen(codes=("C8",), tokens=()))
    assert only.anchor.competency_codes == ("C8",) and only.anchor.capability_definition_ids == ()


def test_a_capability_without_safe_stage_can_anchor_a_movement_without_diagnostic():
    # C10_D n'a aucun stade sûr : ni dette, ni diagnostic ; un mouvement peut
    # s'y ancrer si l'incrément est utile (décision sémantique de 6-3B).
    assert validate(deepen(tokens=("C10_D@r1",))).anchor.capability_definition_ids == (d("C10_D"),)


# --------------------------------------------------------------------------
# 6. Canonicalisation
# --------------------------------------------------------------------------

THREE = profile(EXPLAIN_SEG, seg("Puis compare.", operations=("compare",)), seg("Et conclus.",
                                                                              operations=("form_conclusion",)))


def test_anchor_is_canonicalised_whatever_the_json_order():
    result = validate(mp(INTEGRATE, (2, 0), ("C9", "C10"), ("C9_C@r1", "C10_D@r1", "C10_B@r1"), INTENT), task=THREE)
    assert result.anchor == MovementAnchor(segment_indices=(0, 2), competency_codes=("C10", "C9"),
                                           capability_definition_ids=(d("C10_B"), d("C10_D"), d("C9_C")))
    same = validate(mp(INTEGRATE, (0, 2), ("C10", "C9"), ("C10_B@r1", "C10_D@r1", "C9_C@r1"), INTENT), task=THREE)
    assert result == same


def test_definition_ids_follow_the_catalogue_never_a_uuid_sort():
    result = validate(deepen(tokens=("C10_D@r1", "C10_B@r1")))
    assert result.anchor.capability_definition_ids == (d("C10_B"), d("C10_D"))


@pytest.mark.parametrize("proposal, match", [
    (deepen(indices=(0, 0)), "double"),
    (deepen(codes=("C10", "C10")), "double"),
    (deepen(tokens=("C10_B@r1", "C10_B@r1")), "double"),
])
def test_duplicates_are_refused(proposal, match):
    refused(proposal, match)


# --------------------------------------------------------------------------
# 7. Statuts : no_task, focus non résolu
# --------------------------------------------------------------------------

def test_no_task_admits_only_a_deterministic_stay():
    task = profile(status=NO_TASK)
    planning = prepare_movement_planning(**movement_inputs(task=task))
    assert planning.request_status == NO_TASK and planning.segments == ()
    result = validate(mp(), task=task)
    assert (result.movement, result.anchor, result.movement_intent) == (STAY, None, None)
    refused(deepen(), "no_task : seul stay", task=task)


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_unresolved_focus_admits_only_a_deterministic_stay(status):
    task = profile(EXPLAIN_SEG, CHALLENGE_SEG)
    planning = prepare_movement_planning(**movement_inputs(task=task, ctx=context(status=status)))
    # Aucune compétence locale réinférée : catalogue vide, support sans
    # personnalisation, postures conservées.
    assert planning.pedagogical_resolution_status == status and planning.catalogue == ()
    assert [s.posture for s in planning.segments] == [EXPLAIN, CHALLENGE]
    assert all(s.planned_support.relevant_safe_assumptions == () == s.planned_support.conceptual_bridges
               for s in planning.segments)
    result = validate(mp(), task=task, ctx=context(status=status))
    assert (result.movement, result.anchor, result.pedagogical_resolution_status) == (STAY, None, status)
    for movement in NON_STAY:
        refused(_valid(movement), f"focus {status} : seul stay", task=task, ctx=context(status=status))


def test_no_task_with_a_resolved_focus_still_admits_only_stay():
    task = profile(status=NO_TASK)
    assert prepare_movement_planning(**movement_inputs(task=task)).catalogue  # focus résolu, aucun segment
    refused(_valid(CLARIFY), "no_task", task=task)


# --------------------------------------------------------------------------
# 8. Déterminisme, immutabilité, fail-closed
# --------------------------------------------------------------------------

def test_deterministic_and_inputs_are_not_mutated():
    base = movement_inputs(task=THREE)
    snapshot = movement_inputs(task=THREE)
    proposal = mp(INTEGRATE, (2, 0), ("C9", "C10"), ("C9_C@r1", "C10_B@r1"), INTENT)
    first = validate_movement_proposal(proposal, **base)
    second = validate_movement_proposal(proposal, **base)
    assert first == second and base == snapshot
    assert prepare_movement_planning(**base) == prepare_movement_planning(**base)


def test_same_inputs_rebuilt_independently_give_the_same_movement():
    assert validate(deepen()) == validate(deepen(), ctx=default_context(), taxonomy=make_taxonomy())


def test_outputs_are_immutable():
    result = validate(deepen())
    planning = prepare_movement_planning(**movement_inputs())
    projection = project(result)
    for value in (result, result.anchor, planning, planning.segments[0], planning.catalogue[0],
                  planning.segments[0].planned_support, projection, projection.competencies[0]):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, dataclasses.fields(value)[0].name, None)
    assert all(type(v) is not list and type(v) is not dict for v in _walk(result))
    assert all(type(v) is not list and type(v) is not dict for v in _walk(planning))


def test_a_refused_proposal_never_falls_back_to_stay():
    with pytest.raises(InvalidMovementProposal):
        validate(deepen(tokens=("C7_A@r1",)))


# --------------------------------------------------------------------------
# 9. Projection de génération
# --------------------------------------------------------------------------

def test_projection_exposes_meaning_only():
    result = validate(mp(INTEGRATE, (0,), ("C8", "C10"), ("C10_D@r1", "C10_B@r1"), INTENT))
    projection = project(result)
    assert type(projection) is MovementGenerationProjection
    assert (projection.schema_version, projection.policy_version) == (MOVEMENT_SCHEMA_VERSION,
                                                                      MOVEMENT_POLICY_VERSION)
    assert (projection.movement, projection.movement_intent, projection.segment_indices) == (INTEGRATE, INTENT, (0,))
    assert projection.competencies == (
        ProjectedMovementCompetency(competency_code="C10", competency_label=SPEC_COMPETENCIES["C10"]["label"],
                                    capabilities=tuple(ProjectedCapability(
                                        capability_token=f"{code}@r1", label=SPEC_CAPABILITIES[code]["label"],
                                        definition=SPEC_CAPABILITIES[code]["definition"])
                                        for code in ("C10_B", "C10_D"))),
        ProjectedMovementCompetency(competency_code="C8", competency_label=SPEC_COMPETENCIES["C8"]["label"],
                                    capabilities=()))


def test_projection_of_stay_is_empty():
    projection = project(validate(mp()))
    assert (projection.movement, projection.movement_intent, projection.segment_indices,
            projection.competencies) == (STAY, None, (), ())
    for status in ("neutral", "composite"):
        unresolved = project(validate(mp(), ctx=context(status=status)), ctx=context(status=status))
        assert unresolved.movement == STAY and unresolved.competencies == ()
    assert project(validate(mp(), task=profile(status=NO_TASK)), task=profile(status=NO_TASK)).movement == STAY


def test_projection_contains_no_uuid_provenance_stage_constraint_nor_score():
    constraint = MinimalValidationConstraint(
        intent="revalidation", target_stage="application", scope_mode="localized",
        capability_definition_ids=(d("C10_B"),),
        activation_rule="only_if_naturally_relevant_and_not_direct_answer_or_explanation",
        residual_work_rule="preserve_minimal_residual_user_work")
    ctx = context(part("target", "C10", ("C10_B", DCA), constraints=[constraint]),
                  [part("supporting", "C8", stages=DC)])
    projection = project(validate(deepen(), ctx=ctx), ctx=ctx)
    text = repr(projection)
    assert not UUID_PATTERN.search(text)
    for word in ("safe_stages", "discovery", "comprehension", "application", "mastery", "provenance", "validation",
                 "release", "fingerprint", "score", "confidence", "surface", "inference_run_id", "source_"):
        assert word not in text, word
    assert not [v for v in _walk(projection) if type(v) is uuid.UUID]


def test_projection_is_deterministic_and_pairs_movement_with_its_inputs():
    result = validate(deepen())
    assert project(result) == project(result)
    # Mouvement validé pour un catalogue, projeté avec un autre : refusé.
    with pytest.raises(IncompatibleMovementInputs):
        project(result, ctx=context(part("target", "C10", ("C10_D", ())), [part("supporting", "C8", stages=DC)]))
    # Mouvement ancré sur le segment 2, projeté avec une tâche d'un segment.
    later = validate(deepen(indices=(2,)), task=THREE)
    with pytest.raises(InvalidMovementContent, match="segment_indices"):
        project(later)


def _anchor(result, **changes):
    return dataclasses.replace(result, anchor=dataclasses.replace(result.anchor, **changes))


FORGED = {
    "type": (lambda m: dataclasses.asdict(m), InvalidMovementArgument),
    "schema_version": (lambda m: dataclasses.replace(m, schema_version="interaction-pedagogical-movement-v0"),
                       UnsupportedMovementVersion),
    "policy_version": (lambda m: dataclasses.replace(m, policy_version="movement-policy-2"),
                       UnsupportedMovementVersion),
    "source_task_schema_version": (lambda m: dataclasses.replace(m, source_task_schema_version="x"),
                                   IncompatibleMovementInputs),
    "source_support_policy_version": (lambda m: dataclasses.replace(m, source_support_policy_version="x"),
                                      IncompatibleMovementInputs),
    "request_status": (lambda m: dataclasses.replace(m, request_status=NO_TASK), IncompatibleMovementInputs),
    "resolution_status": (lambda m: dataclasses.replace(m, pedagogical_resolution_status="neutral"),
                          IncompatibleMovementInputs),
    "str subclass header": (lambda m: dataclasses.replace(m, request_status=type("T", (str,), {})(TASK_REQUESTED)),
                            InvalidMovementContent),
    "movement inconnu": (lambda m: dataclasses.replace(m, movement="advance"), InvalidMovementContent),
    "stay avec ancrage": (lambda m: dataclasses.replace(m, movement=STAY), InvalidMovementContent),
    "non-stay sans ancrage": (lambda m: dataclasses.replace(m, anchor=None), InvalidMovementContent),
    "intention vide": (lambda m: dataclasses.replace(m, movement_intent=""), InvalidMovementContent),
    "intention trop longue": (lambda m: dataclasses.replace(m, movement_intent="x" * 501), InvalidMovementContent),
    "ancrage d'un autre type": (lambda m: dataclasses.replace(m, anchor=("C10",)), InvalidMovementContent),
    "definition_id inconnu": (lambda m: _anchor(m, capability_definition_ids=(uid("autre"),)),
                              IncompatibleMovementInputs),
    "definition_id en str": (lambda m: _anchor(m, capability_definition_ids=(str(d("C10_B")),)),
                             InvalidMovementContent),
    "definition_id en liste": (lambda m: _anchor(m, capability_definition_ids=[d("C10_B")]),
                               InvalidMovementContent),
    "definition_id hors compétence déclarée": (lambda m: _anchor(m, capability_definition_ids=(d("C9_C"),)),
                                               InvalidMovementContent),
    "ordre non canonique": (lambda m: _anchor(m, capability_definition_ids=(d("C10_D"), d("C10_B"))),
                            IncompatibleMovementInputs),
    "segment hors tâche": (lambda m: _anchor(m, segment_indices=(3,)), InvalidMovementContent),
    "segment non hachable": (lambda m: _anchor(m, segment_indices=([0],)), InvalidMovementContent),
    "compétence non hachable": (lambda m: _anchor(m, competency_codes=(["C10"],)), InvalidMovementContent),
    "compétence hors catalogue": (lambda m: _anchor(m, competency_codes=("C7",)), InvalidMovementContent),
}


@pytest.mark.parametrize("name", list(FORGED))
def test_projection_refuses_a_forged_movement(name):
    mutate, error = FORGED[name]
    result = validate(deepen(tokens=("C10_B@r1", "C10_D@r1")))
    with pytest.raises(error):
        project(mutate(result))


# --------------------------------------------------------------------------
# 10. Chaîne réelle
# --------------------------------------------------------------------------

def test_real_chain_6_1b_6_1c_6_1d_6_1e_6_2c_then_6_3():
    focus = validate_focus_proposal(FocusProposal(
        schema_version=FOCUS_SCHEMA_VERSION, policy_version=FOCUS_POLICY_VERSION, resolution_status="resolved",
        target=ProposedCompetencyFocus(competency_code="C8", scope_mode="localized",
                                       capability_tokens=("C8_C@r1", "C8_A@r1")),
        supporting=(ProposedCompetencyFocus(competency_code="C10", scope_mode="competency_only",
                                            capability_tokens=()),)), TAXONOMY)
    st = state(snap("C8", "application", {"discovery": ((d("C8_A"), d("C8_C")), False),
                                          "comprehension": ((d("C8_A"),), False),
                                          "application": ((d("C8_A"),), False)}))
    ctx = build_pedagogical_response_context(focus=focus, plan=safe_plan(st, focus))
    task = profile(seg("Avec un ROIC de 25 % depuis dix ans, sa croissance créera autant de valeur ?", "unspecified",
                       ("interpret_evidence", "form_conclusion"), ("user_specific_application",)))
    base = movement_inputs(task=task, ctx=ctx, support_segments=(sp(
        assumptions=[ref("C8", "C8_A@r1")], allocations=[alloc("interpret_evidence"), alloc("form_conclusion")]),))
    planning = prepare_movement_planning(**base)
    assert [(c.competency_code, [k.capability_token for k in c.capabilities]) for c in planning.catalogue] == [
        ("C8", ["C8_A@r1", "C8_C@r1"]), ("C10", [])]
    result = validate_movement_proposal(mp(DEEPEN, (0,), ("C8",), ("C8_C@r1",), INTENT), **base)
    assert result.anchor.capability_definition_ids == (d("C8_C"),)
    projection = project_pedagogical_movement(result, **base)
    assert projection.competencies[0].capabilities[0].label == SPEC_CAPABILITIES["C8_C"]["label"]
    # Un focus localisé n'est jamais élargi à la compétence entière.
    with pytest.raises(InvalidMovementProposal, match="localized sans capacité"):
        validate_movement_proposal(mp(DEEPEN, (0,), ("C8",), (), INTENT), **base)
    assert validate_movement_proposal(mp(INTEGRATE, (0,), ("C8", "C10"), ("C8_A@r1",), INTENT), **base).anchor
    with pytest.raises(InvalidMovementProposal, match="absent du catalogue restreint"):
        validate_movement_proposal(mp(DEEPEN, (0,), ("C8",), ("C8_B@r1",), INTENT), **base)


def test_every_posture_can_stay_and_no_posture_forces_a_movement():
    task = profile(EXPLAIN_SEG, GUIDE_SEG, JOINT_SEG, CHALLENGE_SEG)
    base = movement_inputs(task=task)
    assert [s.posture for s in prepare_movement_planning(**base).segments] == [EXPLAIN, GUIDE, CO_REASON, CHALLENGE]
    assert validate_movement_proposal(mp(), **base).movement == STAY
    # Un seul mouvement pour toute l'interaction, ancré sur un segment.
    result = validate_movement_proposal(mp(DEEPEN, (3,), ("C10",), ("C10_B@r1",), INTENT), **base)
    assert result.anchor.segment_indices == (3,)


# --------------------------------------------------------------------------
# 11. Contrat statique
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


def _identifiers():
    names = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.name)
    return names


def test_imports_are_only_stdlib_and_the_public_6_2c_surface():
    assert _imports() == {"uuid", "dataclasses", "core.adaptation_support"}
    names = {a.name for n in ast.walk(_tree()) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not [name for name in names if name.startswith("_")]
    for name in ("validate_support_plan_proposal", "build_posture_baseline", "project_pedagogical_context",
                 "validate_focus_proposal", "build_pedagogical_response_context", "classify_support_plan",
                 "ASSUMPTION_STAGE_ORDER", "POSTURES", "EXPLAIN", "GUIDE", "CO_REASON", "CHALLENGE",
                 "COGNITIVE_OPERATIONS", "OPERATION_ALLOCATIONS", "ORYX", "USER_RESERVED", "JOINT"):
        assert name not in names, name


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
        raise AssertionError("accès base interdit en 6.3")

    for name in dir(db):
        if callable(getattr(db, name)) and not name.startswith("__"):
            monkeypatch.setattr(db, name, _forbidden, raising=False)
    assert project(validate(deepen())).movement == DEEPEN


def test_no_stage_posture_or_operation_rule_decides_a_movement():
    code = _code_tokens(_source())
    for value in ("discovery", "comprehension", "application", "mastery", "explain", "guide", "co_reason",
                  "challenge", "calculate", "apply_procedure", "user_specific_application", "joint", "user_reserved"):
        assert value not in code.split(), value
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            raise AssertionError("aucune table littérale (stade / posture / opération -> mouvement)")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "index":
            raise AssertionError("aucune position calculée")
    # Aucun calcul ni comparaison d'ordre, sauf la borne technique de
    # movement_intent.
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                                                 for op in node.ops):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert names == {"len", "value", "MAX_MOVEMENT_INTENT_CHARS"}, ast.dump(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)):
            raise AssertionError(ast.dump(node))
    floats = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is float]
    ints = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is int
            and type(n.value) is not bool}
    assert floats == [] and ints <= {0, 1, MAX_MOVEMENT_INTENT_CHARS}
    # safe_stages n'est jamais lu pour décider : uniquement recopié.
    readers = [n for n in ast.walk(tree) if isinstance(n, (ast.If, ast.Compare, ast.BoolOp))
               and "safe_stages" in ast.dump(n)]
    assert readers == []


def test_no_score_rank_level_surface_market_activity_nor_step5_notion():
    identifiers = {name.lower() for name in _identifiers()}
    parts = {part for name in identifiers for part in name.split("_")}
    for word in ("score", "rank", "level", "next", "progress", "progression", "difficulty", "confidence", "autonomy",
                 "surface", "route", "cta", "redirect", "observation", "claim", "tension", "trace", "trade",
                 "transaction", "performance", "turnover", "position", "portfolio", "frequency", "kpi", "reward",
                 "curriculum", "validation", "constraints", "current_stage", "inference", "generation"):
        assert word not in identifiers and word not in parts, word
    text = _source().lower()
    for word in ("academy", "rallye", "coach", "decrypt", "décrypt", "portfolio", "education", "checklist",
                 "actual_support_trace", "support_trace", "cognitive_event", "validation_constraints"):
        assert word not in text, word


def test_not_wired_to_api_web_chat_nor_migrated():
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if rel != "core/adaptation_movement.py" and "adaptation_movement" in path.read_text(
                encoding="utf-8", errors="replace"):
            users.append(rel)
    # Seul consommateur : le classificateur 6-3B, lui-même non branché.
    assert users == ["core/adaptation_movement_classifier.py"]
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("validate_movement_proposal", "prepare_movement_planning", "project_pedagogical_movement",
                 "InteractionPedagogicalMovement", "MovementGenerationProjection", "MOVEMENT_POLICY_VERSION"):
        assert name not in api, name
    for word in ("web_chat", "fastapi", "request_handler", "api"):
        assert word not in _code_tokens(_source()).split(), word
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0009_competency_inference_state.py" and len(versions) == 9
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    for word in ("movement", "Movement", "movement_intent", "pedagogical_movement", "clarify", "deepen",
                 "generalize", "integrate"):
        assert word not in models, word


def test_received_objects_are_never_mutated():
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assert not [t for t in targets if isinstance(t, (ast.Attribute, ast.Subscript))], ast.dump(node)
    assert "__setattr__" not in _code_tokens(_source())


def test_no_module_level_mutable_state():
    for name, value in vars(movement_module).items():
        if not name.startswith("__"):
            assert not isinstance(value, (list, dict, set)), name
