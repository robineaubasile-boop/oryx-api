"""Tests de l'Étape 6.1E : contexte pédagogique minimal de la réponse
(core/adaptation_context.py).

1. Assemblage PUR (toujours exécuté) : AdaptationStateSnapshot et
   InteractionCompetencyFocus construits à la main -> build_assumption_baseline
   (6-1C) -> build_safe_assumption_plan (6-1D) -> build_pedagogical_response_context
   -> project_pedagogical_context. Conservation exacte (ordre, rôles,
   périmètres, definition_id, stades, contraintes), trois situations
   distinctes (non résolu / aucun état / état sans stade sûr), compatibilité
   focus / plan stricte, fail closed sans contexte partiel, projection sans
   provenance ni diagnostic, déterminisme, immutabilité.

2. Chaîne RÉELLE sans base : load_current_focus_taxonomy (lecteurs T4-B
   doublés, comme tests/test_adaptation_focus.py) -> classify_focus_proposal
   (backend factice) -> validate_focus_proposal -> 6-1C -> 6-1D -> 6-1E.

3. Contrat statique : API publique, imports, aucune base, aucun modèle de
   langage, aucune persistance, aucun branchement runtime, aucune brique
   6.2+ (posture, mouvement, progression, génération).

4. Intégration contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL, sinon
   SKIPPÉS ; jamais SQLite) : SPEC V1 active -> T3 -> T4 -> T5 -> T6 réels
   -> load_adaptation_state + load_current_focus_taxonomy ->
   classify_focus_proposal -> validate_focus_proposal -> 6-1C -> 6-1D ->
   6-1E ; aucune requête pendant l'assemblage et la projection.
"""
import ast
import dataclasses
import inspect
import random
import uuid

import pytest
import sqlalchemy as sa

from core.adaptation_assumptions import build_assumption_baseline
from core.adaptation_context import (
    PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION,
    CompetencyPedagogicalContext,
    CompetencyResponseContext,
    CompetencyStateProvenance,
    IncompatibleResponseContextInputs,
    InvalidResponseContextArgument,
    InvalidResponseContextContent,
    PedagogicalProjection,
    PedagogicalResponseContext,
    PedagogicalResponseContextError,
    ResponseContextEnvelope,
    UnsupportedResponseContextVersion,
    build_pedagogical_response_context,
    project_pedagogical_context,
)
from core.adaptation_focus import (
    FocusProposal,
    InteractionCompetencyFocus,
    load_current_focus_taxonomy,
    validate_focus_proposal,
)
from core.adaptation_focus_classifier import InteractionFocusInput, classify_focus_proposal
from core.adaptation_safety import (
    VALIDATION_ACTIVATION_RULE,
    VALIDATION_WORK_RULE,
    MinimalValidationConstraint,
    SafeAssumptionCoverage,
    SafeAssumptionPlan,
    build_safe_assumption_plan,
)
from core.adaptation_state import load_adaptation_state
from core.inference_state_policies import REVISION_REASON_CODES, TENSION_REASON_CODES
from tests.test_adaptation_assumptions import (
    FINGERPRINT,
    OTHER_RELEASE,
    RELEASE,
    _pipeline,
    _v1,
    cap,
    cf,
    d,
    focus,
    snap,
    state,
    uid,
)
from tests.test_adaptation_focus import loaded, taxonomy  # noqa: F401 — fixtures (lecteurs T4-B doublés)
from tests.test_adaptation_focus_classifier import FakeBackend, _out
from tests.test_adaptation_safety import DC, DCA, DCAM, A, B, C, D, N, T, constraint, plan, s8
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "adaptation_context.py"
DISC = ("discovery",)


# --------------------------------------------------------------------------
# Constructeurs
# --------------------------------------------------------------------------

def assemble(st, fo):
    return build_pedagogical_response_context(focus=fo, plan=plan(st, fo))


def project(st, fo):
    return project_pedagogical_context(assemble(st, fo))


def safe(pedagogy):
    return [(c.capability_definition_id, c.safe_stages) for c in pedagogy.safe_assumptions]


def c7(current="application", ladder=None, **kwargs):
    ladder = ladder if ladder is not None else {s: ((d("C7_A"),), False) for s in DCA}
    return snap("C7", current, ladder, **kwargs)


def _replace_part(plan_, index, **changes):
    """Plan dont la compétence index (0 = cible) est modifiée."""
    parts = [plan_.target, *plan_.supporting]
    parts[index] = dataclasses.replace(parts[index], **changes)
    return dataclasses.replace(plan_, target=parts[0], supporting=tuple(parts[1:]))


# --------------------------------------------------------------------------
# 1. Contrats publics
# --------------------------------------------------------------------------

def test_schema_version_is_explicit():
    assert PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION == "pedagogical-response-context-v1"


def test_dataclasses_are_frozen_keyword_only_with_the_exact_fields():
    expected = {
        ResponseContextEnvelope: ["plan_schema_version", "baseline_schema_version", "focus_schema_version",
                                  "focus_policy_version", "taxonomy_release_id", "taxonomy_spec_fingerprint"],
        CompetencyStateProvenance: ["inference_run_id", "longitudinal_assessment_run_id", "state_generation",
                                    "taxonomy_release_id"],
        CompetencyPedagogicalContext: ["role", "competency_code", "scope_mode", "capability_definition_ids",
                                       "safe_assumptions", "validation_constraints"],
        CompetencyResponseContext: ["provenance", "pedagogy"],
        PedagogicalResponseContext: ["schema_version", "envelope", "resolution_status", "target", "supporting"],
        PedagogicalProjection: ["schema_version", "resolution_status", "target", "supporting"],
    }
    for cls, names in expected.items():
        assert [f.name for f in dataclasses.fields(cls)] == names, cls
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls))


def test_public_api_is_exactly_the_assembler_and_the_projection():
    from core import adaptation_context
    public = sorted(name for name, value in vars(adaptation_context).items()
                    if inspect.isfunction(value) and value.__module__ == adaptation_context.__name__
                    and not name.startswith("_"))
    assert public == ["build_pedagogical_response_context", "project_pedagogical_context"]
    signature = inspect.signature(build_pedagogical_response_context)
    assert list(signature.parameters) == ["focus", "plan"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values())
    assert list(inspect.signature(project_pedagogical_context).parameters) == ["context"]


def test_errors_are_a_dedicated_business_hierarchy():
    for error in (InvalidResponseContextArgument, UnsupportedResponseContextVersion,
                  IncompatibleResponseContextInputs, InvalidResponseContextContent):
        assert issubclass(error, PedagogicalResponseContextError)
    assert PedagogicalResponseContextError.__bases__ == (Exception,)


# --------------------------------------------------------------------------
# 2. Conservation exacte (scénarios 1-9)
# --------------------------------------------------------------------------

def test_01_resolved_focus_with_target_only():
    st, fo = state(s8()), focus(cf("C8", A, C))
    p = plan(st, fo)
    result = build_pedagogical_response_context(focus=fo, plan=p)
    assert result.schema_version == PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION
    assert result.resolution_status == "resolved" and result.supporting == ()
    pedagogy = result.target.pedagogy
    assert (pedagogy.role, pedagogy.competency_code, pedagogy.scope_mode, pedagogy.capability_definition_ids) == (
        "target", "C8", "localized", (A, C))
    assert pedagogy.safe_assumptions is p.target.coverages
    assert pedagogy.validation_constraints is p.target.validation_constraints
    assert result.envelope == ResponseContextEnvelope(
        plan_schema_version="safe-assumption-plan-v1", baseline_schema_version="assumption-baseline-v1",
        focus_schema_version="interaction-focus-v1", focus_policy_version="focus-policy-1",
        taxonomy_release_id=RELEASE, taxonomy_spec_fingerprint=FINGERPRINT)


def _multi():
    st = state(s8(), c7())
    fo = focus(cf("C8", A, B), [cf("C2"), cf("C7", d("C7_A"), d("C7_B")), cf("C10")])
    return st, fo


def test_02_resolved_focus_with_several_supports():
    st, fo = _multi()
    p = plan(st, fo)
    result = build_pedagogical_response_context(focus=fo, plan=p)
    assert [s.pedagogy.competency_code for s in result.supporting] == ["C2", "C7", "C10"]
    for part, planned in zip(result.supporting, p.supporting):
        assert part.pedagogy.safe_assumptions is planned.coverages
        assert part.pedagogy.validation_constraints is planned.validation_constraints
    c7_part = result.supporting[1].pedagogy
    assert safe(c7_part) == [(d("C7_A"), DCA), (d("C7_B"), ())]


def test_03_order_is_preserved_never_sorted():
    # Supports C2 -> C7 -> C10 (jamais un tri lexical C10 < C2) ; definition_id
    # dans l'ordre du focus (jamais un tri d'UUID).
    st, fo = _multi()
    result = assemble(st, fo)
    assert [s.pedagogy.competency_code for s in result.supporting] == ["C2", "C7", "C10"]
    ids = (D, A, C)
    assert sorted(ids) != list(ids) or sorted(ids, reverse=True) != list(ids)
    target = assemble(state(s8()), focus(cf("C8", *ids))).target.pedagogy
    assert target.capability_definition_ids == ids
    assert [c.capability_definition_id for c in target.safe_assumptions] == list(ids)


def test_04_roles_are_preserved():
    result = assemble(*_multi())
    assert result.target.pedagogy.role == "target"
    assert {s.pedagogy.role for s in result.supporting} == {"supporting"}
    projection = project_pedagogical_context(result)
    assert [p.role for p in (projection.target, *projection.supporting)] == ["target", "supporting", "supporting",
                                                                             "supporting"]


def test_05_localized_scopes_are_never_merged_nor_broadened():
    ladder = {"discovery": ((A, B), False), "comprehension": ((B,), False), "application": ((B,), True)}
    st = state(s8(ladder=ladder, current="application"))
    localized = assemble(st, focus(cf("C8", B))).target.pedagogy
    assert safe(localized) == [(B, DCA)]
    assert A not in localized.capability_definition_ids  # acquis de A jamais importé
    global_part = assemble(st, focus(cf("C8"))).target.pedagogy
    # Une base localized ne devient jamais un acquis global : seul
    # competency_only_basis compte, et competency_only reste competency_only.
    assert (global_part.scope_mode, global_part.capability_definition_ids) == ("competency_only", ())
    assert safe(global_part) == [(None, ("application",))]
    assert "whole_competency" not in repr(assemble(st, focus(cf("C8"))))


def test_06_definition_ids_are_exact():
    other_a = uid("another-C8_A")
    caps = (cap("C8_A"), cap("C8_A", other_a, revision=2), cap("C8_B"), cap("C8_C"), cap("C8_D"))
    st = state(s8(capabilities=caps, ladder={"discovery": ((A,), False)}))
    on_other = assemble(st, focus(cf("C8", other_a))).target.pedagogy
    assert on_other.capability_definition_ids == (other_a,) and safe(on_other) == [(other_a, ())]
    on_a = assemble(st, focus(cf("C8", A))).target.pedagogy
    assert safe(on_a) == [(A, DISC)]


def test_07_safe_stages_are_kept_without_any_addition():
    # Échelle à trou (Discovery + Application, sans Comprehension) : jamais comblée.
    gap = {"discovery": ((A,), False), "application": ((A,), False), "mastery": ((A,), False)}
    st = state(s8(ladder=gap, tensions=[T("mastery", "localized", A)]))
    p = plan(st, focus(cf("C8", A)))
    result = build_pedagogical_response_context(focus=focus(cf("C8", A)), plan=p)
    assert safe(result.target.pedagogy) == [(A, ("discovery", "application"))]
    assert safe(result.target.pedagogy) == [(c.capability_definition_id, c.safe_stages) for c in p.target.coverages]


def test_08_conditional_constraints_are_kept_without_activation():
    needs = [N("revalidation", "application", "localized", A), N("confirmation", "comprehension", "localized", B)]
    st = state(s8(needs=needs))
    p = plan(st, focus(cf("C8", A, B)))
    pedagogy = build_pedagogical_response_context(focus=focus(cf("C8", A, B)), plan=p).target.pedagogy
    assert pedagogy.validation_constraints == (constraint("revalidation", "application", "localized", A),
                                               constraint("confirmation", "comprehension", "localized", B))
    assert pedagogy.validation_constraints is p.target.validation_constraints
    for item in pedagogy.validation_constraints:
        assert (item.activation_rule, item.residual_work_rule) == (VALIDATION_ACTIVATION_RULE, VALIDATION_WORK_RULE)
    # Une contrainte ne retire aucun stade et n'est jamais une obligation.
    assert safe(pedagogy) == [(A, DCAM), (B, DCAM)]
    names = {f.name for cls in (CompetencyPedagogicalContext, PedagogicalProjection) for f in dataclasses.fields(cls)}
    assert not {"active", "activated", "selected", "required", "mandatory", "question", "exercise"} & names


def test_09_capability_present_without_safe_stage_stays_represented():
    st = state(s8(ladder={"discovery": ((A,), False)}, current="discovery"))
    pedagogy = assemble(st, focus(cf("C8", A, B))).target.pedagogy
    assert safe(pedagogy) == [(A, DISC), (B, ())]


# --------------------------------------------------------------------------
# 3. Trois situations distinctes (10-15)
# --------------------------------------------------------------------------

def test_10_resolved_focus_without_step5_state():
    result = assemble(state(), focus(cf("C8", A, B), [cf("C7")]))
    for part in (result.target, *result.supporting):
        assert part.provenance is None
        assert all(c.safe_stages == () for c in part.pedagogy.safe_assumptions)
        assert part.pedagogy.validation_constraints == ()
    assert safe(result.target.pedagogy) == [(A, ()), (B, ())]
    assert safe(result.supporting[0].pedagogy) == [(None, ())]
    assert "non_etabli" not in repr(result)


def test_11_resolved_focus_with_a_non_etabli_state():
    st = state(snap("C8", "non_etabli", {}))
    result = assemble(st, focus(cf("C8", A)))
    assert result.target.provenance == CompetencyStateProvenance(
        inference_run_id=uid("run-C8"), longitudinal_assessment_run_id=uid("t5-C8"), state_generation=1,
        taxonomy_release_id=RELEASE)
    assert safe(result.target.pedagogy) == [(A, ())]
    # Absence d'état (B) et état non_etabli : jamais le même contexte canonique.
    absent = assemble(state(), focus(cf("C8", A)))
    assert absent.target.provenance is None and absent != result
    # Le stade courant n'est jamais exposé (ni plancher, ni plafond, ni diagnostic).
    assert "non_etabli" not in repr(result) and "current_stage" not in repr(result)


def test_12_present_state_without_any_safe_stage_keeps_focus_scope_and_constraints():
    st = state(s8(tensions=[T("discovery", "whole_competency")],
                  needs=[N("revalidation", "discovery", "whole_competency")]))
    result = assemble(st, focus(cf("C8", A, B)))
    target = result.target
    assert target.provenance is not None
    assert (target.pedagogy.scope_mode, target.pedagogy.capability_definition_ids) == ("localized", (A, B))
    assert safe(target.pedagogy) == [(A, ()), (B, ())]
    assert target.pedagogy.validation_constraints == (constraint("revalidation", "discovery", "localized", A, B),)
    absent = assemble(state(), focus(cf("C8", A, B))).target
    assert absent.provenance is None and absent.pedagogy.validation_constraints == ()


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"],
                         ids=["13-neutral", "14-ambiguous", "15-composite"])
def test_13_14_15_unresolved_focus_gives_a_neutral_context_with_the_exact_status(status):
    st = state(s8(tensions=[T("application", "localized", A)], needs=[N("revalidation", "application",
                                                                          "localized", A)]), c7())
    fo = focus(status=status)
    result = assemble(st, fo)
    assert (result.resolution_status, result.target, result.supporting) == (status, None, ())
    assert result.envelope.taxonomy_release_id == RELEASE
    projection = project_pedagogical_context(result)
    assert projection == PedagogicalProjection(schema_version=PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION,
                                               resolution_status=status, target=None, supporting=())


# --------------------------------------------------------------------------
# 4. Compatibilité focus / plan (16-26)
# --------------------------------------------------------------------------

def _inputs():
    st = state(s8(needs=[N("revalidation", "application", "localized", A)]), c7())
    fo = focus(cf("C8", A, B), [cf("C2"), cf("C7", d("C7_A"))])
    return fo, plan(st, fo), st


def _refused(error, fo, p, match=None):
    with pytest.raises(error, match=match):
        build_pedagogical_response_context(focus=fo, plan=p)


@pytest.mark.parametrize("changes", [
    {"schema_version": "safe-assumption-plan-v2"}, {"schema_version": None},
    {"baseline_schema_version": "assumption-baseline-v2"}])
def test_16_unsupported_plan_schema_version(changes):
    fo, p, _ = _inputs()
    _refused(UnsupportedResponseContextVersion, fo, dataclasses.replace(p, **changes))


def test_16_focus_schema_version_unsupported_or_different_from_the_plan():
    fo, p, _ = _inputs()
    _refused(UnsupportedResponseContextVersion, dataclasses.replace(fo, schema_version="interaction-focus-v2"), p)
    _refused(IncompatibleResponseContextInputs, fo, dataclasses.replace(p, focus_schema_version="interaction-focus-v2"),
             "schema_version du focus")


def test_17_policy_version_unsupported_or_different_from_the_plan():
    fo, p, _ = _inputs()
    _refused(UnsupportedResponseContextVersion, dataclasses.replace(fo, policy_version="focus-policy-2"), p)
    _refused(IncompatibleResponseContextInputs, fo, dataclasses.replace(p, focus_policy_version="focus-policy-2"),
             "policy_version")


def test_18_taxonomy_release_mismatch():
    fo, _, st = _inputs()
    other = dataclasses.replace(fo, taxonomy_release_id=OTHER_RELEASE)
    _refused(IncompatibleResponseContextInputs, fo, plan(st, other), "taxonomy_release_id")
    _refused(IncompatibleResponseContextInputs, other, plan(st, fo), "taxonomy_release_id")


def test_19_semantic_fingerprint_mismatch():
    fo, _, st = _inputs()
    other = dataclasses.replace(fo, taxonomy_spec_fingerprint="0" * 64)
    _refused(IncompatibleResponseContextInputs, fo, plan(st, other), "taxonomy_spec_fingerprint")


def test_resolution_status_mismatch():
    fo, _, st = _inputs()
    _refused(IncompatibleResponseContextInputs, fo, plan(st, focus(status="neutral")), "resolution_status")
    _refused(IncompatibleResponseContextInputs, focus(status="ambiguous"), plan(st, focus(status="neutral")),
             "resolution_status")


def test_20_target_differs_between_focus_and_plan():
    _, _, st = _inputs()
    _refused(IncompatibleResponseContextInputs, focus(cf("C8", A)), plan(st, focus(cf("C7", d("C7_A")))),
             "target.competency_code")


def test_21_missing_support():
    _, _, st = _inputs()
    fo = focus(cf("C8", A), [cf("C2"), cf("C7")])
    _refused(IncompatibleResponseContextInputs, fo, plan(st, focus(cf("C8", A), [cf("C7")])), r"manquantes \['C2'\]")


def test_22_extra_support():
    _, _, st = _inputs()
    fo = focus(cf("C8", A), [cf("C7")])
    _refused(IncompatibleResponseContextInputs, fo, plan(st, focus(cf("C8", A), [cf("C2"), cf("C7")])),
             r"en trop \['C2'\]")


def test_23_support_order_changed():
    fo, p, _ = _inputs()
    swapped = dataclasses.replace(p, supporting=tuple(reversed(p.supporting)))
    _refused(IncompatibleResponseContextInputs, fo, swapped, "ordre")


@pytest.mark.parametrize("plan_focus", [cf("C8"), cf("C8", A), cf("C8", A, B, C)])
def test_24_scope_changed(plan_focus):
    _, _, st = _inputs()
    _refused(IncompatibleResponseContextInputs, focus(cf("C8", A, B)), plan(st, focus(plan_focus)), "target")


def test_25_definition_id_different_or_reordered():
    _, _, st = _inputs()
    other_a = uid("another-C8_A")
    _refused(IncompatibleResponseContextInputs, focus(cf("C8", A)), plan(st, focus(cf("C8", other_a))),
             "capability_definition_ids")
    _refused(IncompatibleResponseContextInputs, focus(cf("C8", A, B)), plan(st, focus(cf("C8", B, A))),
             "capability_definition_ids")
    fo, p, _ = _inputs()
    forged = _replace_part(p, 2, focus_capability_definition_ids=(d("C7_B"),))
    _refused(IncompatibleResponseContextInputs, fo, forged, r"supporting\[1\]")


class _SubFocus(InteractionCompetencyFocus):
    pass


class _SubPlan(SafeAssumptionPlan):
    pass


def _subclass(cls, value):
    return cls(**{f.name: getattr(value, f.name) for f in dataclasses.fields(value)})


@pytest.mark.parametrize("make", [
    lambda fo, p, st: {"focus": None},
    lambda fo, p, st: {"focus": p},
    lambda fo, p, st: {"focus": dataclasses.asdict(fo)},
    lambda fo, p, st: {"focus": FocusProposal(schema_version=fo.schema_version, policy_version=fo.policy_version,
                                              resolution_status="neutral", target=None, supporting=())},
    lambda fo, p, st: {"focus": _subclass(_SubFocus, fo)},
    lambda fo, p, st: {"plan": None},
    lambda fo, p, st: {"plan": fo},
    lambda fo, p, st: {"plan": dataclasses.asdict(p)},
    lambda fo, p, st: {"plan": build_assumption_baseline(state=st, focus=fo)},
    lambda fo, p, st: {"plan": _subclass(_SubPlan, p)},
])
def test_26_wrong_input_types(make):
    fo, p, st = _inputs()
    with pytest.raises(InvalidResponseContextArgument):
        build_pedagogical_response_context(**{"focus": fo, "plan": p, **make(fo, p, st)})


def test_26_projection_refuses_a_wrong_type():
    fo, p, _ = _inputs()
    for value in (None, {}, p, fo, project_pedagogical_context(build_pedagogical_response_context(focus=fo, plan=p))):
        with pytest.raises(InvalidResponseContextArgument):
            project_pedagogical_context(value)


# --------------------------------------------------------------------------
# 5. Entrées corrompues : fail closed, jamais un contexte partiel (27)
# --------------------------------------------------------------------------

def _coverage(p, index=0, **changes):
    part = [p.target, *p.supporting][index]
    coverages = list(part.coverages)
    coverages[0] = dataclasses.replace(coverages[0], **changes)
    return _replace_part(p, index, coverages=tuple(coverages))


def _forge_constraint(p, **changes):
    (item,) = p.target.validation_constraints
    return _replace_part(p, 0, validation_constraints=(dataclasses.replace(item, **changes),))


CORRUPTIONS = {
    "stage-added-out-of-order": lambda p: _coverage(p, safe_stages=("application", "discovery")),
    "stage-duplicated": lambda p: _coverage(p, safe_stages=("discovery", "discovery")),
    "stage-out-of-vocabulary": lambda p: _coverage(p, safe_stages=("non_etabli",)),
    "stages-list": lambda p: _coverage(p, safe_stages=["discovery"]),
    "coverage-outside-scope": lambda p: _coverage(p, capability_definition_id=C),
    "coverage-missing": lambda p: _replace_part(p, 0, coverages=p.target.coverages[:1]),
    "coverage-merged-to-global": lambda p: _coverage(p, 1, capability_definition_id=A),
    "coverages-list": lambda p: _replace_part(p, 0, coverages=list(p.target.coverages)),
    "constraint-activated": lambda p: _forge_constraint(p, activation_rule="always"),
    "constraint-work-rule": lambda p: _forge_constraint(p, residual_work_rule="ask_a_question"),
    "constraint-whole-competency": lambda p: _forge_constraint(p, scope_mode="whole_competency"),
    "constraint-outside-scope": lambda p: _forge_constraint(p, capability_definition_ids=(C,)),
    "constraint-ids-list": lambda p: _forge_constraint(p, capability_definition_ids=[A]),
    "constraint-rule-list": lambda p: _forge_constraint(p, activation_rule=[VALIDATION_ACTIVATION_RULE]),
    "constraint-intent": lambda p: _forge_constraint(p, intent="quiz"),
    "constraint-stage": lambda p: _forge_constraint(p, target_stage="expert"),
    "constraint-duplicated": lambda p: _replace_part(p, 0, validation_constraints=p.target.validation_constraints * 2),
    "constraint-type": lambda p: _replace_part(p, 0, validation_constraints=({"intent": "revalidation"},)),
    "role-swapped": lambda p: _replace_part(p, 0, role="supporting"),
    "support-role": lambda p: _replace_part(p, 1, role="target"),
    "partial-provenance": lambda p: _replace_part(p, 0, source_state_generation=None),
    "provenance-type": lambda p: _replace_part(p, 0, source_inference_run_id=str(uid("run-C8"))),
    "provenance-generation": lambda p: _replace_part(p, 0, source_state_generation=0),
    "assumption-without-state": lambda p: _coverage(p, 1, safe_stages=DISC),
    "target-missing": lambda p: dataclasses.replace(p, target=None),
    "supporting-list": lambda p: dataclasses.replace(p, supporting=list(p.supporting)),
}


@pytest.mark.parametrize("name", sorted(CORRUPTIONS))
def test_27_corrupted_plan_is_refused_without_any_partial_result(name):
    fo, p, _ = _inputs()
    forged = CORRUPTIONS[name](p)
    with pytest.raises(PedagogicalResponseContextError) as error:
        build_pedagogical_response_context(focus=fo, plan=forged)
    assert type(error.value) in (InvalidResponseContextContent, IncompatibleResponseContextInputs)


@pytest.mark.parametrize("forged", [
    lambda fo: dataclasses.replace(fo, target=dataclasses.replace(fo.target, capability_definition_ids=(A, A))),
    lambda fo: dataclasses.replace(fo, target=dataclasses.replace(fo.target, capability_definition_ids=[A, B])),
    lambda fo: dataclasses.replace(fo, target=dataclasses.replace(fo.target, scope_mode="whole_competency")),
    lambda fo: dataclasses.replace(fo, target=dataclasses.replace(fo.target, competency_code="C13")),
    lambda fo: dataclasses.replace(fo, taxonomy_release_id=str(RELEASE)),
    lambda fo: dataclasses.replace(fo, resolution_status="unknown"),
])
def test_27_forged_consistent_focus_and_plan_are_refused(forged):
    """Focus ET plan corrompus de la même façon (le plan reprend le focus
    forgé) : la cohérence mutuelle ne suffit jamais."""
    fo, p, _ = _inputs()
    bad = forged(fo)
    planned = p
    if bad.target is not fo.target:
        planned = _replace_part(p, 0, focus_scope_mode=bad.target.scope_mode,
                                competency_code=bad.target.competency_code,
                                focus_capability_definition_ids=bad.target.capability_definition_ids)
    if bad.taxonomy_release_id != fo.taxonomy_release_id:
        planned = dataclasses.replace(planned, focus_taxonomy_release_id=bad.taxonomy_release_id)
    if bad.resolution_status != fo.resolution_status:
        planned = dataclasses.replace(planned, resolution_status=bad.resolution_status)
    with pytest.raises(InvalidResponseContextContent):
        build_pedagogical_response_context(focus=bad, plan=planned)


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_27_corruption_is_never_converted_into_a_neutral_context(status):
    fo, p, _ = _inputs()
    with pytest.raises(InvalidResponseContextContent):
        build_pedagogical_response_context(focus=dataclasses.replace(fo, resolution_status=status),
                                           plan=dataclasses.replace(p, resolution_status=status))
    neutral = focus(status=status)
    with pytest.raises(InvalidResponseContextContent):
        build_pedagogical_response_context(focus=neutral, plan=dataclasses.replace(
            plan(state(), neutral), supporting=p.supporting))


def test_27_the_assembler_never_returns_before_every_check(monkeypatch):
    """Toute erreur survient avant le retour : aucun objet n'est publié
    (pas de cache, pas d'état global, pas de résultat partiel)."""
    from core import adaptation_context
    fo, p, _ = _inputs()
    forged = CORRUPTIONS["constraint-activated"](p)
    seen = []
    original = adaptation_context._check_context
    monkeypatch.setattr(adaptation_context, "_check_context", lambda c: seen.append(c) or original(c))
    with pytest.raises(InvalidResponseContextContent):
        build_pedagogical_response_context(focus=fo, plan=forged)
    assert len(seen) == 1  # vérifié au complet, puis refusé : jamais retourné
    assert not [name for name, value in vars(adaptation_context).items()
                if isinstance(value, (list, dict, set)) and not name.startswith("__")]


# --------------------------------------------------------------------------
# 6. Projection (28-29)
# --------------------------------------------------------------------------

def test_28_projection_has_no_internal_provenance():
    st, fo = _multi()
    result = assemble(st, fo)
    projection = project_pedagogical_context(result)
    assert projection.target is result.target.pedagogy
    assert all(a is b.pedagogy for a, b in zip(projection.supporting, result.supporting))
    text = repr(projection)
    for value in (uid("run-C8"), uid("t5-C8"), uid("run-C7"), uid("t5-C7"), RELEASE, FINGERPRINT):
        assert str(value) not in text and repr(value) not in text
    names = {f.name for cls in (PedagogicalProjection, CompetencyPedagogicalContext, SafeAssumptionCoverage,
                                MinimalValidationConstraint) for f in dataclasses.fields(cls)}
    for forbidden in ("user_id", "provenance", "envelope", "inference_run_id", "longitudinal_assessment_run_id",
                      "state_generation", "source_inference_run_id", "taxonomy_release_id",
                      "taxonomy_spec_fingerprint", "plan_schema_version", "baseline_schema_version"):
        assert forbidden not in names, forbidden
    assert "state_generation" not in text and "'u'" not in text


def test_29_projection_carries_no_diagnostic():
    st = state(s8(tensions=[T("application", "localized", A)],
                  context={"schema_version": "revision-context-v1", "origin_inference_run_id": str(uid("origin")),
                           "reason_code": REVISION_REASON_CODES[0],
                           "resolution_status": "unresolved", "motifs": [
                               {"fragilized_stage": "comprehension", "scope_mode": "localized",
                                "capability_definition_ids": [str(B)],
                                "source_contradiction_observation_ids": [str(uid("obs"))],
                                "reason_codes": [TENSION_REASON_CODES[0]]}]},
                  needs=[N("revalidation", "application", "localized", A)]))
    projection = project(st, focus(cf("C8", A, B)))
    names = {f.name for cls in (PedagogicalProjection, CompetencyPedagogicalContext) for f in dataclasses.fields(cls)}
    for forbidden in ("diagnostic", "tension", "tensions", "tension_state", "fragilized_stage", "revision_status",
                      "unresolved_revision_context", "reason_codes", "needs_revalidation", "weakness", "claims",
                      "confidence_profile", "mastery_assessment", "score", "current_stage", "established_stages",
                      "level", "difficulty", "max_difficulty", "ceiling", "prerequisite", "blocking", "posture",
                      "next_move", "progression", "message"):
        assert forbidden not in names, forbidden
    text = repr(projection)
    for forbidden in ("tension", "contradiction", "reason", "origin", "unresolved", "fragil", "non_etabli",
                      str(uid("obs")), str(uid("origin"))):
        assert forbidden not in text, forbidden
    assert safe(projection.target) == [(A, DC), (B, DISC)]


@pytest.mark.parametrize("forge, error", [
    (lambda c: dataclasses.replace(c, schema_version="pedagogical-response-context-v2"),
     UnsupportedResponseContextVersion),
    (lambda c: dataclasses.replace(c, envelope=dataclasses.replace(c.envelope, focus_policy_version="x")),
     UnsupportedResponseContextVersion),
    (lambda c: dataclasses.replace(c, envelope=None), InvalidResponseContextContent),
    (lambda c: dataclasses.replace(c, resolution_status="neutral"), InvalidResponseContextContent),
    (lambda c: dataclasses.replace(c, target=None), InvalidResponseContextContent),
    (lambda c: dataclasses.replace(c, target=c.target.pedagogy), InvalidResponseContextContent),
    (lambda c: dataclasses.replace(c, supporting=(c.supporting[1], c.supporting[0])), InvalidResponseContextContent),
    (lambda c: dataclasses.replace(c, supporting=(*c.supporting, c.supporting[0])), InvalidResponseContextContent),
    (lambda c: dataclasses.replace(c, target=dataclasses.replace(c.target, pedagogy=dataclasses.replace(
        c.target.pedagogy, validation_constraints=tuple(dataclasses.replace(
            item, activation_rule="always") for item in c.target.pedagogy.validation_constraints)))),
     InvalidResponseContextContent),
    (lambda c: dataclasses.replace(c, target=dataclasses.replace(c.target, provenance=None)),
     InvalidResponseContextContent),
])
def test_projection_of_a_forged_context_fails_closed(forge, error):
    st = state(s8(needs=[N("revalidation", "application", "localized", A)]), c7())
    result = assemble(st, focus(cf("C8", A, B), [cf("C2"), cf("C7", d("C7_A"))]))
    with pytest.raises(error):
        project_pedagogical_context(forge(result))


# --------------------------------------------------------------------------
# 7. Déterminisme, immutabilité, frontières (30-32)
# --------------------------------------------------------------------------

def test_30_determinism():
    st, fo = _multi()
    p = plan(st, fo)
    first = build_pedagogical_response_context(focus=fo, plan=p)
    for _ in range(3):
        again = build_pedagogical_response_context(focus=fo, plan=plan(st, fo))
        assert again == first and repr(again) == repr(first)
        assert project_pedagogical_context(again) == project_pedagogical_context(first)


def test_30_no_clock_random_environment_nor_uuid_generation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("appel interdit")

    st, fo = _multi()
    p = plan(st, fo)
    for target, name in ((uuid, "uuid4"), (uuid, "uuid1"), (random, "random"), (random, "shuffle")):
        monkeypatch.setattr(target, name, forbidden)
    project_pedagogical_context(build_pedagogical_response_context(focus=fo, plan=p))
    tokens = set(_code_tokens(_source()).split())
    for word in ("now", "utcnow", "time", "environ", "getenv", "random", "shuffle", "choice", "uuid4", "uuid5",
                 "sorted", "sort"):
        assert word not in tokens, word


def test_31_immutability():
    st, fo = _multi()
    result = assemble(st, fo)
    projection = project_pedagogical_context(result)
    for obj, field in ((result, "target"), (result.envelope, "taxonomy_release_id"), (result.target, "pedagogy"),
                       (result.target.provenance, "state_generation"), (result.target.pedagogy, "safe_assumptions"),
                       (projection, "supporting"), (projection.target, "role")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, field, None)
    for part in (result.target.pedagogy, *(s.pedagogy for s in result.supporting)):
        assert type(part.capability_definition_ids) is tuple
        assert type(part.safe_assumptions) is tuple and type(part.validation_constraints) is tuple
        assert all(type(c.safe_stages) is tuple for c in part.safe_assumptions)
    assert type(result.supporting) is tuple and type(projection.supporting) is tuple


def test_user_request_keeps_priority_no_ceiling_no_blocking_prerequisite():
    """Aucun plafond, aucune difficulté maximale, aucun message utilisateur
    reçu ni classé : les stades sûrs ne sont qu'un plancher ; un support
    vide ne conditionne jamais la cible."""
    assert "message" not in inspect.signature(build_pedagogical_response_context).parameters
    full = assemble(state(s8()), focus(cf("C8", A), [cf("C7")]))
    assert full.target.pedagogy.safe_assumptions == (SafeAssumptionCoverage(capability_definition_id=A,
                                                                            safe_stages=DCAM),)
    assert full.supporting[0].provenance is None  # support sans état : ne bloque rien
    alone = assemble(state(s8()), focus(cf("C8", A)))
    assert full.target.pedagogy == alone.target.pedagogy


def test_compatibility_is_not_authenticity():
    """6-1E vérifie que le plan a été construit pour CE focus ; il ne peut
    pas prouver que ce focus vient du classificateur pour cette interaction,
    ni que l'état Step 5 du plan est le plus récent (orchestrateur)."""
    stale = state(s8(run=uid("run-R42"), generation=1))
    fresh = state(s8(run=uid("run-R43"), generation=2, ladder={"discovery": ((A,), False)}))
    fo = focus(cf("C8", A))  # construit à la main, jamais passé par 6-1B
    result = build_pedagogical_response_context(focus=fo, plan=plan(stale, fo))
    assert result.target.provenance.inference_run_id == uid("run-R42")  # provenance exposée à l'orchestrateur
    assert result != build_pedagogical_response_context(focus=fo, plan=plan(fresh, fo))


# --------------------------------------------------------------------------
# 8. Chaîne réelle sans base : 6-1B1 -> 6-1B2 -> 6-1B1 -> 6-1C -> 6-1D -> 6-1E
# --------------------------------------------------------------------------

def _chain(taxonomy, output, st, message="Est-ce que le ROIC actuel justifie ce multiple ?"):  # noqa: F811
    backend = FakeBackend(output)
    proposal = classify_focus_proposal(InteractionFocusInput(current_message=message, context_turns=()), taxonomy,
                                       backend=backend)
    assert len(backend.calls) == 1
    validated = validate_focus_proposal(proposal, taxonomy)
    baseline = build_assumption_baseline(state=st, focus=validated)
    safe_plan = build_safe_assumption_plan(baseline=baseline, state=st)
    return validated, safe_plan, build_pedagogical_response_context(focus=validated, plan=safe_plan)


def _tax_ids(taxonomy):  # noqa: F811
    return {c.capability_code: c.definition_id for c in taxonomy.capabilities}


def test_real_chain_without_database(taxonomy):  # noqa: F811
    ids = _tax_ids(taxonomy)
    release = taxonomy.taxonomy_release_id
    caps = tuple(cap(f"C10_{x}", ids[f"C10_{x}"], release=release) for x in "ABCD")
    c10 = snap("C10", "application", {"discovery": ((ids["C10_A"], ids["C10_B"]), False),
                                      "comprehension": ((ids["C10_A"],), False),
                                      "application": ((ids["C10_A"],), False)},
               release=release, capabilities=caps, tension_state="open",
               tensions=(T("application", "localized", ids["C10_A"]),),
               needs=(N("revalidation", "application", "localized", ids["C10_A"]),))
    st = state(c10)
    output = _out(target=("C10", "localized", "C10_B@r1", "C10_A@r1"), supporting=[("C8", "localized", "C8_A@r1")])
    validated, safe_plan, result = _chain(taxonomy, output, st)
    assert result.envelope.taxonomy_release_id == release
    assert result.envelope.taxonomy_spec_fingerprint == taxonomy.taxonomy_spec_fingerprint
    target = result.target
    assert target.pedagogy.capability_definition_ids == validated.target.capability_definition_ids == (
        ids["C10_A"], ids["C10_B"])
    assert safe(target.pedagogy) == [(ids["C10_A"], DC), (ids["C10_B"], DISC)]
    assert target.pedagogy.validation_constraints == (
        constraint("revalidation", "application", "localized", ids["C10_A"]),)
    assert target.provenance.inference_run_id == uid("run-C10")
    (support,) = result.supporting
    assert (support.provenance, support.pedagogy.competency_code, safe(support.pedagogy)) == (
        None, "C8", [(ids["C8_A"], ())])
    projection = project_pedagogical_context(result)
    assert str(uid("run-C10")) not in repr(projection) and str(release) not in repr(projection)


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_real_chain_unresolved_status_without_database(taxonomy, status):  # noqa: F811
    _, _, result = _chain(taxonomy, _out(status), state(s8()), message="Bonjour")
    assert (result.resolution_status, result.target, result.supporting) == (status, None, ())


def test_real_chain_mixed_with_another_interaction_is_refused(taxonomy):  # noqa: F811
    """Plan d'une interaction C10, focus d'une interaction C7 : refusé."""
    st = state()
    _, c10_plan, _ = _chain(taxonomy, _out(target=("C10", "competency_only")), st)
    c7_focus, _, _ = _chain(taxonomy, _out(target=("C7", "competency_only")), st)
    with pytest.raises(IncompatibleResponseContextInputs):
        build_pedagogical_response_context(focus=c7_focus, plan=c10_plan)


# --------------------------------------------------------------------------
# 9. Constitution statique (32)
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


def _accessed_names():
    """Attributs lus + identifiants + chaînes littérales du code."""
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    return names | set(_code_tokens(_source()).split())


def test_imports_are_exactly_public_step6_contracts():
    by_module = {}
    for node in _tree().body:
        if isinstance(node, ast.ImportFrom):
            by_module.setdefault(node.module, set()).update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            by_module.setdefault(None, set()).update(a.name for a in node.names)
    assert by_module == {
        None: {"uuid"},
        "dataclasses": {"dataclass"},
        "core.adaptation_assumptions": {"ASSUMPTION_BASELINE_SCHEMA_VERSION", "ASSUMPTION_STAGE_ORDER",
                                        "SUPPORTING", "TARGET"},
        "core.adaptation_focus": {"COMPETENCY_ONLY", "FOCUS_POLICY_VERSION", "FOCUS_SCHEMA_VERSION", "LOCALIZED",
                                  "RESOLUTION_STATUSES", "RESOLVED", "SCOPE_MODES", "CompetencyFocus",
                                  "InteractionCompetencyFocus"},
        "core.adaptation_safety": {"SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION", "VALIDATION_ACTIVATION_RULE",
                                   "VALIDATION_WORK_RULE", "CompetencySafeAssumptions", "MinimalValidationConstraint",
                                   "SafeAssumptionCoverage", "SafeAssumptionPlan"},
        "core.adaptation_state": {"COMPETENCY_ORDER"},
        # Vocabulaire propriétaire Step 5 : constante seulement.
        "core.inference_final_policies": {"VALIDATION_INTENTS"},
    }
    imported = set().union(*by_module.values())
    assert not any(name.startswith("_") for name in imported)
    for reader in ("load_adaptation_state", "load_current_focus_taxonomy", "validate_focus_proposal",
                   "classify_focus_proposal", "build_assumption_baseline", "build_safe_assumption_plan",
                   "FocusProposal", "AdaptationStateSnapshot"):
        assert reader not in imported, reader


def test_32_no_database_access():
    imported = _imports()
    assert not {"sqlalchemy", "core.models", "core.db", "core.taxonomy_service", "core.observation_service",
                "core.longitudinal_service", "core.inference_service", "alembic", "psycopg2"} & imported
    names = _accessed_names()
    for forbidden in ("Session", "session", "db", "get_db", "execute", "query", "commit", "flush", "add", "add_all",
                      "rollback", "no_autoflush", "select", "text", "merge", "delete", "refresh", "SessionLocal",
                      "get_validated_user_competency_state", "get_release_capabilities", "get_active_release"):
        assert forbidden not in names, forbidden


def test_32_no_llm_no_network_no_persistence():
    assert not {"anthropic", "openai", "requests", "httpx", "urllib", "socket", "json", "pickle", "shelve", "os",
                "pathlib", "logging"} & _imports()
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "backend", "classif", "model_call", "llm", "complete(",
                 "open(", "write", "save", "persist", "cache"):
        assert word not in tokens, word


def test_32_no_step5_state_read_nor_re_inference():
    names = _accessed_names()
    for forbidden in ("claims", "current_stage", "tensions", "tension_state", "unresolved_revision_context",
                      "validation_needs", "reason_codes", "confidence_profile", "mastery_assessment",
                      "established_stages", "user_id", "competencies", "infer_competency",
                      "evaluate_inference_state", "evaluate_validation_needs"):
        assert forbidden not in names, forbidden


def test_32_no_later_step6_brick_nor_generation():
    tokens = _code_tokens(_source()).lower()
    for word in ("posture", "next_useful_move", "next_move", "mouvement", "movement", "progression", "progress",
                 "exercise", "exercice", "quiz", "question", "generate", "generator", "response_text", "support_trace",
                 "observation", "activate", "priority", "score", "difficulty", "ceiling", "plafond"):
        assert word not in tokens, word
    parts = {part for token in tokens.split() for part in token.split("_")}
    for word in ("explain", "guide", "clarify", "deepen", "generalize", "integrate", "route", "surface", "academy",
                 "select", "max", "min", "level", "rank"):
        assert word not in parts, word


def test_32_not_wired_to_api_nor_web_chat_nor_migrated():
    assert "api" not in _imports()
    # Étape 6.2C : calibration du support, contexte canonique revérifié et
    # projeté par la frontière publique (jamais build_pedagogical_response_context,
    # jamais les contraintes de validation) ; non branché : tests/test_adaptation_support.py.
    step6_consumers = {"core/adaptation_support.py": {
        "PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION", "PedagogicalResponseContext", "PedagogicalResponseContextError",
        "UnsupportedResponseContextVersion", "project_pedagogical_context"}}
    for rel, names in step6_consumers.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if "adaptation_context" in f"{getattr(n, 'module', '')}.{a.name}"]
        assert sorted(imported) == sorted(("core.adaptation_context", name) for name in names), rel
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if (rel != "core/adaptation_context.py" and rel not in step6_consumers
                and "adaptation_context" in path.read_text(encoding="utf-8", errors="replace")):
            users.append(rel)
    assert users == []
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("build_pedagogical_response_context", "project_pedagogical_context", "PedagogicalResponseContext",
                 "PedagogicalProjection", "PEDAGOGICAL_RESPONSE_CONTEXT_SCHEMA_VERSION"):
        assert name not in api, name
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0013_decryptage_conversation_affinity.py" and len(versions) == 13
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    # R1-C4 : decryptage_cognitive_links.response_context_event_id (FK T2 vers
    # cognitive_events : event cible de la réponse assistant, provenance) n'est
    # PAS un contexte de réponse pédagogique Step 6 ; seul cet identifiant
    # exact est toléré, et il reste une simple FK vers cognitive_events.
    from core.models import DecryptageCognitiveLink
    [fk] = DecryptageCognitiveLink.__table__.c["response_context_event_id"].foreign_keys
    assert fk.target_fullname == "cognitive_events.id"
    models = models.replace("response_context_event_id", "")
    for word in ("ResponseContext", "response_context", "PedagogicalProjection"):
        assert word not in models, word
    for word in ("__tablename__", "Column", "Base", "mapped_column", "relationship"):
        assert word not in _code_tokens(_source()).split(), word


# --------------------------------------------------------------------------
# 10. Intégration PostgreSQL : T3 / T4 / T5 / T6 réels -> 6-1A + 6-1B -> 6-1C -> 6-1D -> 6-1E
# --------------------------------------------------------------------------

def _pg_chain(Sessions, output, message="Le bénéfice monte mais le cash baisse : pourquoi ?"):  # noqa: N803
    """Chaîne amont RÉELLE : 6-1A (lecture validée) + 6-1B1 (taxonomie
    courante) + 6-1B2 (backend factice) + 6-1B1 (validation) + 6-1C + 6-1D."""
    with Sessions() as session:
        snapshot = load_adaptation_state(session, user_id="u")
        current = load_current_focus_taxonomy(session)
        assert not session.new and not session.dirty and not session.deleted
    backend = FakeBackend(output)
    proposal = classify_focus_proposal(InteractionFocusInput(current_message=message, context_turns=()), current,
                                       backend=backend)
    validated = validate_focus_proposal(proposal, current)
    baseline = build_assumption_baseline(state=snapshot, focus=validated)
    return snapshot, current, validated, build_safe_assumption_plan(baseline=baseline, state=snapshot)


def _assemble_without_any_statement(engine, validated, safe_plan):  # noqa: F811
    from tests.test_adaptation_assumptions import _dump
    statements = []

    def record(conn, cursor, sql, *args):
        statements.append(sql)

    before = _dump(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        result = build_pedagogical_response_context(focus=validated, plan=safe_plan)
        projection = project_pedagogical_context(result)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements == []  # aucune lecture, aucune écriture pendant l'assemblage et la projection
    assert _dump(engine) == before
    return result, projection


def test_pg_real_chain_6_1b_to_6_1e(Sessions, engine):  # noqa: F811
    """Application C7_A / C7_B, puis contradiction Application sur C7_A :
    tension + revalidation réelles. Focus C7 (C7_A, C7_B, C7_C) + support C2
    sans état Step 5."""
    release = _v1(Sessions)
    p = _pipeline(Sessions, release)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    p.t3(p.contra("C7_A", contradiction_scope="application"))
    context, _ = p.infer()
    output = _out(target=("C7", "localized", "C7_C@r1", "C7_A@r1", "C7_B@r1"), supporting=[("C2", "competency_only")])
    snapshot, current, validated, safe_plan = _pg_chain(Sessions, output)
    ids = _tax_ids(current)
    (c7_state,) = snapshot.competencies
    assert c7_state.tensions and c7_state.validation_needs

    result, projection = _assemble_without_any_statement(engine, validated, safe_plan)
    assert result.envelope.taxonomy_release_id == release == validated.taxonomy_release_id
    target = result.target
    assert target.provenance == CompetencyStateProvenance(
        inference_run_id=context.run_id, longitudinal_assessment_run_id=context.longitudinal_assessment_run_id,
        state_generation=c7_state.state_generation, taxonomy_release_id=c7_state.taxonomy_release_id)
    assert target.pedagogy.capability_definition_ids == (ids["C7_A"], ids["C7_B"], ids["C7_C"])
    assert safe(target.pedagogy) == [(ids["C7_A"], DC), (ids["C7_B"], DCA), (ids["C7_C"], ())]
    assert target.pedagogy.safe_assumptions is safe_plan.target.coverages
    assert target.pedagogy.validation_constraints == safe_plan.target.validation_constraints
    assert constraint("revalidation", "application", "localized", ids["C7_A"]) in \
        target.pedagogy.validation_constraints
    (c2,) = result.supporting
    assert c2.provenance is None and safe(c2.pedagogy) == [(None, ())] and c2.pedagogy.validation_constraints == ()

    assert projection.target is target.pedagogy and projection.supporting == (c2.pedagogy,)
    text = repr(projection)
    for value in (context.run_id, context.longitudinal_assessment_run_id, release):
        assert str(value) not in text
    assert "'u'" not in repr(result) and "user_id" not in repr(result)
    assert build_pedagogical_response_context(focus=validated, plan=safe_plan) == result


def test_pg_present_state_without_safe_stage_versus_absent_state(Sessions, engine):  # noqa: F811
    p = _pipeline(Sessions, _v1(Sessions))
    p.t3(p.app("C7_A", evidence_strength="strong"))
    p.infer()
    _, current, validated, safe_plan = _pg_chain(
        Sessions, _out(target=("C7", "localized", "C7_C@r1"), supporting=[("C9", "localized", "C9_A@r1")]))
    result, projection = _assemble_without_any_statement(engine, validated, safe_plan)
    ids = _tax_ids(current)
    assert result.target.provenance is not None  # C : état présent, aucun stade sûr sur C7_C
    assert safe(result.target.pedagogy) == [(ids["C7_C"], ())]
    assert result.target.pedagogy.validation_constraints == safe_plan.target.validation_constraints
    (c9,) = result.supporting
    assert c9.provenance is None and safe(c9.pedagogy) == [(ids["C9_A"], ())]  # B : aucun état
    assert result.target != c9


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_pg_unresolved_focus_gives_a_neutral_context_despite_a_fragile_state(Sessions, engine, status):  # noqa: F811
    p = _pipeline(Sessions, _v1(Sessions))
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    p.t3(p.contra("C7_A", contradiction_scope="application"))
    p.infer()
    snapshot, _, validated, safe_plan = _pg_chain(Sessions, _out(status), message="Bonjour")
    assert snapshot.competencies[0].tensions
    result, projection = _assemble_without_any_statement(engine, validated, safe_plan)
    assert (result.resolution_status, result.target, result.supporting) == (status, None, ())
    assert (projection.resolution_status, projection.target, projection.supporting) == (status, None, ())


def test_pg_plan_of_another_state_release_is_never_remapped(Sessions, engine):  # noqa: F811
    """Focus validé sous la release active ; plan construit pour un focus
    rattaché à une autre release : refusé (aucun remapping)."""
    p = _pipeline(Sessions, _v1(Sessions))
    p.t3(p.app("C7_A", evidence_strength="strong"))
    p.infer()
    snapshot, _, validated, _ = _pg_chain(Sessions, _out(target=("C7", "localized", "C7_A@r1")))
    other = dataclasses.replace(validated, taxonomy_release_id=uuid.uuid5(uuid.NAMESPACE_URL, "other-release"))
    foreign_plan = build_safe_assumption_plan(baseline=build_assumption_baseline(state=snapshot, focus=other),
                                              state=snapshot)
    with pytest.raises(IncompatibleResponseContextInputs):
        build_pedagogical_response_context(focus=validated, plan=foreign_plan)
