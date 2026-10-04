"""Tests de l'Étape 6.1D : présupposés sûrs et contraintes minimales de
validation (core/adaptation_safety.py).

1. Politique PURE (toujours exécutée) : AdaptationStateSnapshot et
   InteractionCompetencyFocus construits à la main -> build_assumption_baseline
   (6-1C) -> build_safe_assumption_plan. Contrats, preflight canonique du
   baseline, statuts non résolus, cutoff, matrice de portée des fragilités
   (tensions courantes et revision-context-v1), parser strict, identités
   historiques, structure des tensions / besoins, projection des besoins sur
   le focus, déduplication, cible / supports, provenance, minimisation,
   déterminisme.

2. Contrat statique : API publique, imports, aucune base, aucun modèle de
   langage, aucune persistance, aucun branchement runtime, aucune brique
   6-1E / 6.2+, aucune relecture des claims, aucun diagnostic en sortie.

3. Intégration contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL, sinon
   SKIPPÉS ; jamais SQLite) : SPEC V1 active -> T3 -> T4 -> T5 -> T6 réels
   -> load_adaptation_state -> focus validé (6-1B1) -> build_assumption_baseline
   -> build_safe_assumption_plan ; aucune requête pendant le plan ; tension
   réelle + revalidation réelle ; revision-context-v1 réellement persisté.
"""
import ast
import dataclasses
import inspect
import random
import uuid
from types import MappingProxyType

import pytest
import sqlalchemy as sa

from core.adaptation_assumptions import (
    ASSUMPTION_BASELINE_SCHEMA_VERSION,
    ASSUMPTION_STAGE_ORDER,
    AssumptionCoverage,
    InvalidAssumptionBaselineState,
    InvalidAssumptionFocus,
    build_assumption_baseline,
)
from core.adaptation_safety import (
    SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION,
    VALIDATION_ACTIVATION_RULE,
    VALIDATION_WORK_RULE,
    CompetencySafeAssumptions,
    InvalidSafeAssumptionArgument,
    InvalidSafeAssumptionBaseline,
    InvalidSafeAssumptionRevisionContext,
    InvalidSafeAssumptionState,
    MinimalValidationConstraint,
    SafeAssumptionCoverage,
    SafeAssumptionPlan,
    SafeAssumptionPlanError,
    build_safe_assumption_plan,
)
from core.adaptation_state import (
    COMPETENCY_ORDER,
    AdaptationStateSnapshot,
    AdaptationTension,
    AdaptationValidationNeed,
    load_adaptation_state,
)
from core.inference_final_policies import VALIDATION_REASON_CODES
from core.inference_service import CLAIM_STAGES
from core.inference_state_policies import REVISION_REASON_CODES, TENSION_REASON_CODES
from tests.test_adaptation_assumptions import (
    OTHER_RELEASE,
    RELEASE,
    STAGES,
    _ids,
    _pipeline,
    _proposed,
    _upstream,
    _v1,
    cap,
    cf,
    d,
    focus,
    snap,
    state,
    uid,
)
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "adaptation_safety.py"

A, B, C, D = (d(f"C8_{x}") for x in "ABCD")
ALL = (A, B, C, D)
DCAM = STAGES
DCA = STAGES[:3]
DC = STAGES[:2]
DISC = STAGES[:1]
# C8 Mastery : chaque capacité ET la base competency_only portent les quatre
# stades (même release) : tout cutoff est visible.
FULL = {stage: (ALL, True) for stage in STAGES}
OBS = uid("obs-1")
REVAL_REASON = ("current_stage_under_tension",)
CONF_REASON = ("independence_not_established_on_current_basis",)


# --------------------------------------------------------------------------
# Constructeurs
# --------------------------------------------------------------------------

def frozen(value):
    """Forme figée produite par 6-1A (objet -> mappingproxy, tableau -> tuple)."""
    if isinstance(value, dict):
        return MappingProxyType({key: frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(frozen(item) for item in value)
    return value


def T(stage, scope, *ids, status="unresolved"):  # noqa: N802
    return AdaptationTension(fragilized_stage=stage, scope_mode=scope, capability_definition_ids=tuple(ids),
                             revision_status=status)


def N(intent, stage, scope, *ids, reasons=None):  # noqa: N802
    if reasons is None:
        reasons = REVAL_REASON if intent == "revalidation" else CONF_REASON
    return AdaptationValidationNeed(intent=intent, target_stage=stage, scope_mode=scope,
                                    capability_definition_ids=tuple(ids), reason_codes=tuple(reasons))


def M(stage, scope, *ids, sources=(OBS,), reasons=("claim_relevant_contradiction",)):  # noqa: N802
    return {"fragilized_stage": stage, "scope_mode": scope, "capability_definition_ids": [str(i) for i in ids],
            "source_contradiction_observation_ids": [str(s) for s in sources], "reason_codes": list(reasons)}


def ctx(*motifs, **overrides):
    data = {"schema_version": "revision-context-v1", "origin_inference_run_id": str(uid("origin")),
            "reason_code": REVISION_REASON_CODES[0], "resolution_status": "unresolved", "motifs": list(motifs)}
    data.update(overrides)
    return data


def s8(*, tensions=(), context=None, needs=(), ladder=None, current="mastery", tension_state=None, code="C8",
       **kwargs):
    if tension_state is None:
        tension_state = "open" if tensions else "none"
    return snap(code, current, FULL if ladder is None else ladder, tensions=tuple(tensions), context=context,
                needs=tuple(needs), tension_state=tension_state, **kwargs)


def plan(st, fo):
    return build_safe_assumption_plan(baseline=build_assumption_baseline(state=st, focus=fo), state=st)


def safe(part):
    return [(c.capability_definition_id, c.safe_stages) for c in part.coverages]


def target_of(st, *ids):
    return plan(st, focus(cf("C8", *ids))).target


def constraint(intent, stage, scope, *ids):
    return MinimalValidationConstraint(intent=intent, target_stage=stage, scope_mode=scope,
                                       capability_definition_ids=tuple(ids),
                                       activation_rule=VALIDATION_ACTIVATION_RULE,
                                       residual_work_rule=VALIDATION_WORK_RULE)


# --------------------------------------------------------------------------
# 1. Contrats publics (1-11)
# --------------------------------------------------------------------------

def test_01_02_03_versions_and_rules_are_exact():
    assert SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION == "safe-assumption-plan-v1"
    assert VALIDATION_ACTIVATION_RULE == "only_if_naturally_relevant_and_not_direct_answer_or_explanation"
    assert VALIDATION_WORK_RULE == "preserve_minimal_residual_user_work"
    assert ASSUMPTION_STAGE_ORDER == CLAIM_STAGES  # un seul ordre conceptuel


def test_04_05_07_to_10_dataclasses_are_frozen_keyword_only_with_the_exact_fields():
    expected = {
        SafeAssumptionCoverage: ["capability_definition_id", "safe_stages"],
        MinimalValidationConstraint: ["intent", "target_stage", "scope_mode", "capability_definition_ids",
                                      "activation_rule", "residual_work_rule"],
        CompetencySafeAssumptions: ["role", "competency_code", "focus_scope_mode",
                                    "focus_capability_definition_ids", "source_inference_run_id",
                                    "source_longitudinal_assessment_run_id", "source_state_generation",
                                    "source_taxonomy_release_id", "coverages", "validation_constraints"],
        SafeAssumptionPlan: ["schema_version", "baseline_schema_version", "focus_schema_version",
                             "focus_policy_version", "focus_taxonomy_release_id",
                             "focus_taxonomy_spec_fingerprint", "resolution_status", "target", "supporting"],
    }
    for cls, names in expected.items():
        assert [f.name for f in dataclasses.fields(cls)] == names
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls))
    result = plan(state(s8()), focus(cf("C8", A)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.target = None
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.target.coverages[0].safe_stages = ()


def test_06_public_api_is_exactly_build_safe_assumption_plan():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    public = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == {"build_safe_assumption_plan"}
    parameters = inspect.signature(build_safe_assumption_plan).parameters
    assert list(parameters) == ["baseline", "state"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
               for p in parameters.values())
    with pytest.raises(TypeError):
        build_safe_assumption_plan(None, None)  # noqa — positionnel interdit


def test_11_no_user_id_in_the_outputs():
    for cls in (SafeAssumptionCoverage, MinimalValidationConstraint, CompetencySafeAssumptions,
                SafeAssumptionPlan):
        assert not any("user" in f.name for f in dataclasses.fields(cls))
    st = AdaptationStateSnapshot(user_id="user-secret-42", competencies=(s8(),))
    assert "user-secret-42" not in repr(plan(st, focus(cf("C8", A))))


def test_errors_are_a_dedicated_business_hierarchy():
    for cls in (InvalidSafeAssumptionArgument, InvalidSafeAssumptionBaseline, InvalidSafeAssumptionState,
                InvalidSafeAssumptionRevisionContext):
        assert issubclass(cls, SafeAssumptionPlanError)
    assert issubclass(SafeAssumptionPlanError, Exception)


def test_108_final_example_of_the_specification():
    """Cible C8 localized (C8_A, C8_D) DCA, support C7 localized C7_B DC ;
    tension Application C8_A ; motif Mastery C8_D ; revalidation Application
    C8_A ; confirmation Application C8_D ; C7 sans fragilité."""
    c7_b = d("C7_B")
    c8 = s8(current="application", ladder={s: ((A, D), False) for s in DCA},
            tensions=[T("application", "localized", A)],
            context=frozen(ctx(M("mastery", "localized", D))),
            needs=[N("revalidation", "application", "localized", A),
                   N("confirmation", "application", "localized", D)])
    c7 = snap("C7", "comprehension", {s: ((c7_b,), False) for s in DC})
    result = plan(state(c7, c8), focus(cf("C8", A, D), [cf("C7", c7_b)]))
    assert safe(result.target) == [(A, DC), (D, DCA)]
    assert result.target.validation_constraints == (
        constraint("revalidation", "application", "localized", A),
        constraint("confirmation", "application", "localized", D))
    (support,) = result.supporting
    assert (support.role, safe(support), support.validation_constraints) == ("supporting", [(c7_b, DC)], ())
    text = repr(result)
    for word in ("tension", "fragil", "revision_status", "reason", "contradiction", "weak", "risk"):
        assert word not in text, word


# --------------------------------------------------------------------------
# 2. Preflight canonique du baseline (12-27)
# --------------------------------------------------------------------------

def _baseline_and_state(**kwargs):
    st = state(s8(current="application", ladder={"discovery": ((A, D), False), "comprehension": ((A,), False),
                                                "application": ((A,), False)}, **kwargs),
               snap("C7", "comprehension", {s: ((d("C7_B"),), False) for s in DC}))
    fo = focus(cf("C8", A, D), [cf("C7", d("C7_B"))])
    return build_assumption_baseline(state=st, focus=fo), st


def test_12_canonical_baseline_on_its_source_state_is_accepted():
    baseline, st = _baseline_and_state()
    result = build_safe_assumption_plan(baseline=baseline, state=st)
    assert result.baseline_schema_version == ASSUMPTION_BASELINE_SCHEMA_VERSION
    assert (result.focus_schema_version, result.focus_policy_version, result.focus_taxonomy_release_id,
            result.focus_taxonomy_spec_fingerprint, result.resolution_status) == (
        baseline.focus_schema_version, baseline.focus_policy_version, baseline.focus_taxonomy_release_id,
        baseline.focus_taxonomy_spec_fingerprint, baseline.resolution_status)
    assert safe(result.target) == [(A, DCA), (D, DISC)]


@pytest.mark.parametrize("value", [None, "baseline", "dict", "focus", "coverage"])
def test_13_wrong_baseline_type(value):
    baseline, st = _baseline_and_state()
    forged = {"focus": focus(cf("C8", A)), "coverage": baseline.target, "dict": {}}.get(value, value)
    with pytest.raises(InvalidSafeAssumptionArgument):
        build_safe_assumption_plan(baseline=forged, state=st)


@pytest.mark.parametrize("value", [None, "u", (), "competency"])
def test_14_wrong_state_type(value):
    baseline, st = _baseline_and_state()
    forged = st.competencies[0] if value == "competency" else value
    with pytest.raises(InvalidSafeAssumptionArgument):
        build_safe_assumption_plan(baseline=baseline, state=forged)


@pytest.mark.parametrize("version", ["assumption-baseline-v2", "", None, 1])
def test_15_wrong_baseline_schema_version(version):
    baseline, st = _baseline_and_state()
    with pytest.raises(InvalidSafeAssumptionBaseline, match="schema_version"):
        build_safe_assumption_plan(baseline=dataclasses.replace(baseline, schema_version=version), state=st)


def _forge_target(baseline, **changes):
    return dataclasses.replace(baseline, target=dataclasses.replace(baseline.target, **changes))


@pytest.mark.parametrize("changes", [
    {"source_inference_run_id": uid("run-forged")},                     # 16
    {"source_longitudinal_assessment_run_id": uid("t5-forged")},        # 17
    {"source_state_generation": 2},                                     # 18
    {"source_taxonomy_release_id": OTHER_RELEASE},                      # 19
    {"coverages": (AssumptionCoverage(capability_definition_id=A, established_stages=DCAM),  # 20
                   AssumptionCoverage(capability_definition_id=D, established_stages=DISC))},
    {"coverages": (AssumptionCoverage(capability_definition_id=A, established_stages=DC),    # 20 (retrait)
                   AssumptionCoverage(capability_definition_id=D, established_stages=DISC))},
    {"coverages": (AssumptionCoverage(capability_definition_id=A, established_stages=DCA),),},  # 21
    {"coverages": (AssumptionCoverage(capability_definition_id=A, established_stages=DCA),     # 22
                   AssumptionCoverage(capability_definition_id=D, established_stages=DISC),
                   AssumptionCoverage(capability_definition_id=B, established_stages=()))},
    {"role": "supporting"},                                             # 25
    {"competency_code": "C9"},                                          # 25
    {"focus_capability_definition_ids": (A,)},                          # 27
    {"focus_scope_mode": "competency_only", "focus_capability_definition_ids": ()},  # 27
    {"coverages": [AssumptionCoverage(capability_definition_id=A, established_stages=DCA),  # forme non tuple
                   AssumptionCoverage(capability_definition_id=D, established_stages=DISC)]},
])
def test_16_to_22_25_27_falsified_target_is_refused(changes):
    baseline, st = _baseline_and_state()
    with pytest.raises(InvalidSafeAssumptionBaseline):
        build_safe_assumption_plan(baseline=_forge_target(baseline, **changes), state=st)


def test_23_old_baseline_r42_on_a_new_state_r43_is_refused():
    old_baseline, _ = _baseline_and_state(run=uid("run-r42"), generation=1)
    _, new_state = _baseline_and_state(run=uid("run-r43"), generation=2)
    with pytest.raises(InvalidSafeAssumptionBaseline):
        build_safe_assumption_plan(baseline=old_baseline, state=new_state)
    # Même ladder, nouveau parent T5 : refusé aussi.
    _, other_t5 = _baseline_and_state(run=uid("run-r42"), t5=uid("t5-other"))
    with pytest.raises(InvalidSafeAssumptionBaseline):
        build_safe_assumption_plan(baseline=old_baseline, state=other_t5)


def test_24_baseline_without_state_on_a_state_that_now_has_one_is_refused():
    fo = focus(cf("C8", A))
    baseline = build_assumption_baseline(state=state(), focus=fo)
    assert baseline.target.source_inference_run_id is None
    with pytest.raises(InvalidSafeAssumptionBaseline):
        build_safe_assumption_plan(baseline=baseline, state=state(s8()))
    # et l'inverse : baseline documenté appliqué à un état devenu absent.
    documented = build_assumption_baseline(state=state(s8()), focus=fo)
    with pytest.raises(InvalidSafeAssumptionBaseline):
        build_safe_assumption_plan(baseline=documented, state=state())


@pytest.mark.parametrize("forge", [
    lambda b: dataclasses.replace(b, supporting=(dataclasses.replace(b.supporting[0], competency_code="C6"),)),
    lambda b: dataclasses.replace(b, supporting=(dataclasses.replace(
        b.supporting[0], source_inference_run_id=uid("run-forged")),)),
    lambda b: dataclasses.replace(b, supporting=(dataclasses.replace(b.supporting[0], coverages=(
        AssumptionCoverage(capability_definition_id=d("C7_B"), established_stages=DCA),)),)),
    lambda b: dataclasses.replace(b, supporting=(dataclasses.replace(b.supporting[0], role="target"),)),
    lambda b: dataclasses.replace(b, supporting=(*b.supporting, b.supporting[0])),
    lambda b: dataclasses.replace(b, supporting=list(b.supporting)),
    lambda b: dataclasses.replace(b, supporting=("C7",)),
])
def test_26_falsified_supporting_is_refused(forge):
    baseline, st = _baseline_and_state()
    with pytest.raises(InvalidSafeAssumptionBaseline):
        build_safe_assumption_plan(baseline=forge(baseline), state=st)


@pytest.mark.parametrize("forge", [
    lambda b: dataclasses.replace(b, target="C8"),
    lambda b: dataclasses.replace(b, target=None),
    lambda b: dataclasses.replace(b, resolution_status="neutral"),
    lambda b: dataclasses.replace(b, focus_taxonomy_release_id=str(RELEASE)),
    lambda b: dataclasses.replace(b, focus_taxonomy_spec_fingerprint=""),
    lambda b: dataclasses.replace(b, focus_schema_version="interaction-focus-v2"),
])
def test_forged_focus_metadata_is_refused(forge):
    baseline, st = _baseline_and_state()
    with pytest.raises(InvalidSafeAssumptionBaseline):
        build_safe_assumption_plan(baseline=forge(baseline), state=st)


def test_focus_release_forgery_is_refused_where_it_changes_the_baseline():
    """competency_only : la release du focus conditionne le baseline (6-1C,
    fail-closed entre releases) ; la falsifier rend le baseline non
    canonique."""
    st = state(s8())
    baseline = build_assumption_baseline(state=st, focus=focus(cf("C8")))
    assert [c.established_stages for c in baseline.target.coverages] == [DCAM]
    with pytest.raises(InvalidSafeAssumptionBaseline):
        build_safe_assumption_plan(baseline=dataclasses.replace(baseline, focus_taxonomy_release_id=OTHER_RELEASE),
                                   state=st)


def test_preflight_binds_the_baseline_to_the_state_never_authenticates_the_focus():
    """Limite documentée : 6-1D ne reçoit que (baseline, state). Un baseline
    cohérent pour un AUTRE focus validé (ex. sans son support) reste le
    baseline canonique de ce focus-là : 6-1D le traite comme tel, sans rien
    inventer. L'authenticité du focus appartient à 6-1B / l'orchestrateur."""
    baseline, st = _baseline_and_state()
    narrower = dataclasses.replace(baseline, supporting=())
    result = build_safe_assumption_plan(baseline=narrower, state=st)
    assert result.supporting == () and safe(result.target) == [(A, DCA), (D, DISC)]


def test_69_6_1c_errors_are_chained_never_masked():
    baseline, st = _baseline_and_state()
    with pytest.raises(InvalidSafeAssumptionBaseline) as info:
        build_safe_assumption_plan(baseline=dataclasses.replace(baseline, focus_policy_version="focus-policy-0"),
                                   state=st)
    assert type(info.value.__cause__) is InvalidAssumptionFocus
    broken = dataclasses.replace(st.competencies[1], current_stage="expert")
    with pytest.raises(InvalidSafeAssumptionBaseline) as info:
        build_safe_assumption_plan(baseline=baseline, state=state(st.competencies[0], broken))
    assert type(info.value.__cause__) is InvalidAssumptionBaselineState


# --------------------------------------------------------------------------
# 3. Focus non résolu (28-33)
# --------------------------------------------------------------------------

def _garbage(code):
    """Compétence dont fragilités / contexte / besoins sont inexploitables."""
    return s8(code=code, tensions=["tension ?"], tension_state="weird", context={"motif": "levier contredit"},
              needs=[{"intent": "question"}])


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_28_to_33_unresolved_focus_gives_no_personalization(status):
    st = state(*(_garbage(code) for code in COMPETENCY_ORDER))
    result = plan(st, focus(status=status))
    assert (result.resolution_status, result.target, result.supporting) == (status, None, ())
    assert result.schema_version == SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION


# --------------------------------------------------------------------------
# 4. Cutoff (34-42)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stage, expected", [
    ("discovery", ()), ("comprehension", DISC), ("application", DC), ("mastery", DCA)])
def test_34_to_37_cutoff_removes_the_fragilized_stage_and_everything_above(stage, expected):
    assert safe(target_of(state(s8(tensions=[T(stage, "localized", A)])), A)) == [(A, expected)]


def test_38_no_fragility_keeps_the_baseline():
    st = state(s8())
    baseline = build_assumption_baseline(state=st, focus=focus(cf("C8", *ALL)))
    result = build_safe_assumption_plan(baseline=baseline, state=st)
    assert safe(result.target) == [(c.capability_definition_id, c.established_stages)
                                   for c in baseline.target.coverages] == [(i, DCAM) for i in ALL]


def test_39_fragility_above_the_highest_established_stage_changes_nothing():
    ladder = {s: ((A,), False) for s in DC}
    st = state(s8(current="comprehension", ladder=ladder, tensions=[T("mastery", "localized", A)]))
    assert safe(target_of(st, A)) == [(A, DC)]
    st = state(s8(current="comprehension", ladder=ladder, tensions=[T("application", "localized", A)]))
    assert safe(target_of(st, A)) == [(A, DC)]


def test_40_a_gap_is_never_filled():
    ladder = {"discovery": ((A,), False), "application": ((A,), False), "mastery": ((A,), False)}
    assert safe(target_of(state(s8(ladder=ladder)), A)) == [(A, ("discovery", "application", "mastery"))]
    st = state(s8(ladder=ladder, tensions=[T("mastery", "localized", A)]))
    assert safe(target_of(st, A)) == [(A, ("discovery", "application"))]
    st = state(s8(ladder=ladder, tensions=[T("comprehension", "localized", A)]))
    assert safe(target_of(st, A)) == [(A, DISC)]


def test_41_42_155_lowest_fragilized_stage_wins_whatever_the_order():
    tensions = [T("application", "localized", A), T("mastery", "localized", A), T("mastery", "whole_competency"),
                T("comprehension", "localized", B)]
    reference = safe(target_of(state(s8(tensions=tensions)), A, B))
    assert reference == [(A, DC), (B, DISC)]
    for seed in range(6):
        shuffled = list(tensions)
        random.Random(seed).shuffle(shuffled)
        assert safe(target_of(state(s8(tensions=shuffled)), A, B)) == reference


# --------------------------------------------------------------------------
# 5. Tensions localized (43-48)
# --------------------------------------------------------------------------

def test_43_44_45_localized_tension_acts_on_the_exact_definition_only():
    tensions = [T("application", "localized", A)]
    assert safe(target_of(state(s8(tensions=tensions)), A)) == [(A, DC)]                 # 43
    assert safe(target_of(state(s8(tensions=tensions)), B)) == [(B, DCAM)]               # 44
    assert safe(target_of(state(s8(tensions=tensions)), A, D)) == [(A, DC), (D, DCAM)]   # 45


@pytest.mark.parametrize("cap_kwargs", [
    {"code": "C8_A"},                                  # 46 même capability_code
    {"code": "C8_Z", "label": "label C8_A"},           # 47 même libellé
    {"code": "C8_Z", "revision": 1},                   # 48 même semantic_revision
])
def test_46_47_48_same_code_label_or_revision_with_another_definition_is_never_remapped(cap_kwargs):
    x = uid("C8_A-other-definition")
    kwargs = {key: value for key, value in cap_kwargs.items() if key != "code"}
    capabilities = (*(cap(f"C8_{c}") for c in "ABCD"), cap(cap_kwargs["code"], x, **kwargs))
    st = state(s8(capabilities=capabilities, tensions=[T("discovery", "localized", x)]))
    assert safe(target_of(st, A)) == [(A, DCAM)]
    assert safe(target_of(st, x)) == [(x, ())]  # x n'a aucune base positive : rien à retirer


# --------------------------------------------------------------------------
# 6. Tensions competency_only / whole_competency (49-54)
# --------------------------------------------------------------------------

def test_49_50_51_competency_only_tension_never_becomes_whole_competency():
    st = state(s8(tensions=[T("application", "competency_only")]))
    assert safe(plan(st, focus(cf("C8"))).target) == [(None, DC)]           # 49
    assert safe(target_of(st, A, B)) == [(A, DCAM), (B, DCAM)]              # 50
    st = state(s8(tensions=[T("application", "localized", A)]))
    assert safe(plan(st, focus(cf("C8"))).target) == [(None, DCAM)]         # 51


def test_52_53_54_whole_competency_tension_acts_on_every_coverage():
    st = state(s8(tensions=[T("application", "whole_competency")]))
    assert safe(target_of(st, A)) == [(A, DC)]                              # 52
    assert safe(target_of(st, A, D)) == [(A, DC), (D, DC)]                  # 53
    assert safe(plan(st, focus(cf("C8"))).target) == [(None, DC)]           # 54


@pytest.mark.parametrize("status", ["unresolved", "revalidation_needed"])
def test_revision_status_never_decides_whether_the_cutoff_applies(status):
    st = state(s8(tensions=[T("application", "localized", A, status=status)]))
    assert safe(target_of(st, A)) == [(A, DC)]


# --------------------------------------------------------------------------
# 7. Structure des tensions (55-66)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tensions, tension_state, match", [
    ([N("revalidation", "application", "localized", A)], "open", "type"),                    # 55
    ([("application", "localized", (A,), "unresolved")], "open", "type"),                   # 55
    ([T("expert", "localized", A)], "open", "fragilized_stage"),                            # 56
    ([T("non_etabli", "localized", A)], "open", "fragilized_stage"),                        # 56
    ([T("application", "partial", A)], "open", "scope_mode"),                               # 57
    ([T("application", "localized", A, status="resolved")], "open", "revision_status"),     # 58
    ([T("application", "localized")], "open", "incohérents"),                               # 59
    ([T("application", "localized", str(A))], "open", "uuid"),                              # 60
    ([T("application", "localized", A, A)], "open", "double"),                              # 61
    ([T("application", "localized", uid("elsewhere"))], "open", "absent"),                  # 62
    ([T("application", "competency_only", A)], "open", "incohérents"),                      # 63
    ([T("application", "whole_competency", A)], "open", "incohérents"),                     # 64
    ([T("application", "localized", A)], "none", "tension_state"),                          # 65
    ([], "open", "tension_state"),                                                          # 66
    ([], "closed", "tension_state"),
])
def test_55_to_66_forged_tensions_fail_closed(tensions, tension_state, match):
    st = state(s8(tensions=tensions, tension_state=tension_state))
    with pytest.raises(InvalidSafeAssumptionState, match=match):
        target_of(st, A)


def test_tension_containers_must_be_tuples():
    st = state(dataclasses.replace(s8(tensions=[T("application", "localized", A)]),
                                   tensions=[T("application", "localized", A)]))
    with pytest.raises(InvalidSafeAssumptionState, match="tuple"):
        target_of(st, A)
    tension = dataclasses.replace(T("application", "localized", A), capability_definition_ids=[A])
    with pytest.raises(InvalidSafeAssumptionState, match="tuple"):
        target_of(state(s8(tensions=[tension])), A)


def test_forged_tension_fails_closed_even_without_positive_baseline():
    st = state(s8(ladder={}, current="non_etabli", tensions=[T("expert", "localized", A)]))
    with pytest.raises(InvalidSafeAssumptionState):
        target_of(st, A)


# --------------------------------------------------------------------------
# 8. Parser revision-context-v1 (67-88)
# --------------------------------------------------------------------------

def _context_plan(context, *ids):
    return target_of(state(s8(context=context)), *(ids or (A,)))


def test_67_no_context_is_accepted():
    assert safe(_context_plan(None)) == [(A, DCAM)]


@pytest.mark.parametrize("shape", [frozen, lambda value: value])
def test_68_canonical_context_is_accepted_frozen_or_plain(shape):
    context = shape(ctx(M("application", "localized", A, B), M("mastery", "whole_competency")))
    assert safe(_context_plan(context, A, D)) == [(A, DC), (D, DCA)]


def test_68_every_canonical_vocabulary_value_is_accepted():
    for reason in REVISION_REASON_CODES:
        assert _context_plan(ctx(M("application", "localized", A), reason_code=reason))
    motif = M("application", "localized", A, reasons=TENSION_REASON_CODES)
    assert _context_plan(ctx(motif))
    # Contrat canonique T6-C2 : reason_codes d'un motif ⊆ vocabulaire (vide accepté par T6-C2).
    assert _context_plan(ctx(M("application", "localized", A, reasons=())))


def _without(data, key):
    data = dict(data)
    del data[key]
    return data


H = uid("historic-definition")
H_SORTED = sorted((uid("historic-1"), uid("historic-2")), key=str)
SOURCES = sorted((uid("obs-a"), uid("obs-b")), key=str)


@pytest.mark.parametrize("context, match", [
    ({}, "champs"),                                                                            # 48
    (["motif"], "champs"),
    ("motif", "champs"),
    (ctx(M("application", "localized", A), schema_version="revision-context-v2"), "schema_version"),  # 69
    (_without(ctx(M("application", "localized", A)), "reason_code"), "champs"),                # 70
    ({**ctx(M("application", "localized", A)), "summary": "x"}, "champs"),                     # 71
    (ctx(M("application", "localized", A), origin_inference_run_id="run-1"), "UUID"),          # 72
    (ctx(M("application", "localized", A), origin_inference_run_id=uid("origin")), "UUID"),    # 72
    (ctx(M("application", "localized", A), origin_inference_run_id=str(uid("o")).upper()), "canonique"),  # 73
    (ctx(M("application", "localized", A), reason_code="levier_contredit"), "reason_code"),   # 74
    (ctx(M("application", "localized", A), resolution_status="resolved_supportively"), "resolution_status"),  # 75
    (ctx(), "vides"),                                                                          # 76
    (ctx(M("application", "localized", A), motifs="motif"), "liste"),
    (ctx(_without(M("application", "localized", A), "reason_codes")), "champs"),               # 77
    (ctx({**M("application", "localized", A), "summary": "x"}), "champs"),                     # 78
    (ctx(M("non_etabli", "localized", A)), "fragilized_stage"),                                # 79
    (ctx(M("expert", "localized", A)), "fragilized_stage"),                                    # 79
    (ctx(M("application", "partial", A)), "scope_mode"),                                       # 80
    (ctx(M("application", "localized")), "incohérents"),                                       # 81
    (ctx(M("application", "competency_only", A)), "incohérents"),                              # 82
    (ctx(M("application", "whole_competency", A)), "incohérents"),                             # 83
    (ctx({**M("application", "localized", A), "capability_definition_ids": ["C8_A"]}), "UUID"),  # 84
    (ctx({**M("application", "localized", A), "capability_definition_ids": [str(A).upper()]}), "canonique"),
    (ctx({**M("application", "localized", A), "capability_definition_ids": str(A)}), "liste"),
    (ctx(M("application", "localized", A, sources=())), "source"),                             # 85
    (ctx({**M("application", "localized", A), "source_contradiction_observation_ids": ["obs"]}), "UUID"),  # 86
    (ctx(M("application", "localized", A, reasons=("levier_contredit",))), "reason_codes"),    # 87
    (ctx(M("application", "localized", A, reasons=(1,))), "reason_codes"),
    (ctx(M("application", "localized", A), M("application", "localized", A)), "double"),       # 88
    (ctx(M("application", "localized", A, reasons=("claim_relevant_contradiction",)),
         M("application", "localized", A, reasons=("structural_recurrence_candidate",))), "double"),  # 88
    (ctx(M("application", "localized", A, A)), "canonique"),                                   # 88
    (ctx(M("application", "localized", B, A)), "canonique"),
    (ctx(M("application", "localized", H, A)), "canonique"),          # historique avant connue
    (ctx(M("application", "localized", *reversed(H_SORTED))), "canonique"),
    (ctx(M("application", "localized", A, sources=(OBS, OBS))), "canonique"),
    (ctx(M("application", "localized", A, sources=tuple(reversed(SOURCES)))), "canonique"),
    (ctx(M("application", "localized", A, reasons=("claim_relevant_contradiction",) * 2)), "canonique"),
    (ctx(M("application", "localized", A, reasons=tuple(reversed(TENSION_REASON_CODES[:2])))), "canonique"),
    (ctx(M("mastery", "localized", A), M("application", "localized", A)), "ordre"),
    (ctx(M("application", "whole_competency"), M("application", "localized", A)), "ordre"),
    (ctx(M("application", "localized", B), M("application", "localized", A)), "ordre"),
    (ctx(M("application", "localized", A, sources=SOURCES[1:]), M("application", "localized", A,
                                                                   sources=SOURCES[:1])), "ordre"),
])
def test_69_to_88_forged_revision_context_fails_closed(context, match):
    for shaped in (context, frozen(context)):
        with pytest.raises(InvalidSafeAssumptionRevisionContext, match=match):
            _context_plan(shaped)


def test_canonical_order_mirrors_t6c2_keys():
    """Ordre canonique T6-C2 : stade, scope (ordre de la chaîne),
    capacités (connues dans l'ordre de la taxonomie, puis historiques par
    str(UUID)), sources par str(UUID)."""
    context = ctx(M("discovery", "whole_competency"),
                  M("application", "competency_only"),
                  M("application", "localized", A, D, *H_SORTED),
                  M("application", "localized", B, sources=SOURCES[:1]),
                  M("application", "localized", B, sources=SOURCES[1:]),
                  M("application", "whole_competency"),
                  M("mastery", "localized", H))
    target = target_of(state(s8(context=context)), A)
    assert safe(target) == [(A, ())]


def test_context_is_never_inspected_without_a_relevant_state():
    """Compétence du focus absente du snapshot : rien à lire ; compétence
    présente : le contexte est TOUJOURS validé, même sans base positive."""
    assert safe(plan(state(_garbage("C2")), focus(cf("C8", A))).target) == [(A, ())]
    with pytest.raises(InvalidSafeAssumptionRevisionContext):
        target_of(state(s8(ladder={}, current="non_etabli", context={"motif": "x"})), A)


# --------------------------------------------------------------------------
# 9. Application du revision context (89-98) et cross-release (104)
# --------------------------------------------------------------------------

def test_89_90_localized_motif_acts_on_the_exact_definition_only():
    context = ctx(M("application", "localized", A))
    assert safe(_context_plan(context, A)) == [(A, DC)]
    assert safe(_context_plan(context, B)) == [(B, DCAM)]
    assert safe(_context_plan(context, A, B)) == [(A, DC), (B, DCAM)]


def test_91_104c_historic_definition_absent_from_the_release_is_valid_and_has_no_effect():
    assert H not in {c.definition_id for c in s8().capabilities}
    target = _context_plan(ctx(M("comprehension", "localized", H)), B)
    assert safe(target) == [(B, DCAM)] and target.validation_constraints == ()


def test_92_104a_historic_motif_acts_on_the_exact_definition_across_releases():
    """Motif émis sous une release historique (A, H) ; snapshot sous
    OTHER_RELEASE (A réutilisée), focus sous RELEASE : effet sur A."""
    st = state(s8(release=OTHER_RELEASE, context=frozen(ctx(M("application", "localized", A, H)))))
    result = plan(st, focus(cf("C8", A, B)))
    assert result.target.source_taxonomy_release_id == OTHER_RELEASE != result.focus_taxonomy_release_id
    assert safe(result.target) == [(A, DC), (B, DCAM)]


def test_104b_104d_same_capability_code_under_another_definition_is_never_remapped():
    """L'ancienne C8_A (H, autre définition) est fragilisée historiquement :
    la C8_A courante (A) n'est jamais touchée, quel que soit le code."""
    capabilities = (*(cap(f"C8_{c}") for c in "ABCD"),)
    st = state(s8(capabilities=capabilities, context=frozen(ctx(M("discovery", "localized", H)))))
    assert safe(target_of(st, A)) == [(A, DCAM)]
    names = _accessed_names()
    for forbidden in ("capability_code", "label", "semantic_revision", "membership_id"):
        assert forbidden not in names, forbidden


def test_93_to_96_unlocalized_motifs_follow_the_frozen_matrix():
    only = ctx(M("application", "competency_only"))
    whole = ctx(M("application", "whole_competency"))
    assert safe(_context_plan(only, A, D)) == [(A, DCAM), (D, DCAM)]                                  # 93
    assert safe(plan(state(s8(context=only)), focus(cf("C8"))).target) == [(None, DC)]               # 94
    assert safe(_context_plan(whole, A, D)) == [(A, DC), (D, DC)]                                     # 95
    assert safe(plan(state(s8(context=whole)), focus(cf("C8"))).target) == [(None, DC)]              # 96


def test_97_98_tensions_and_motifs_share_one_cutoff():
    st = state(s8(tensions=[T("application", "localized", A)], context=ctx(M("mastery", "localized", A))))
    assert safe(target_of(st, A)) == [(A, DC)]
    st = state(s8(tensions=[T("mastery", "localized", A)], context=ctx(M("application", "localized", A))))
    assert safe(target_of(st, A)) == [(A, DC)]


# --------------------------------------------------------------------------
# 10. Structure des validation_needs (99-111)
# --------------------------------------------------------------------------

def test_99_canonical_needs_are_accepted():
    needs = [N("confirmation", "application", "localized", A, B),
             N("revalidation", "application", "competency_only"),
             N("revalidation", "application", "whole_competency",
               reasons=("unresolved_revision_motif", "current_stage_under_tension"))]
    assert target_of(state(s8(needs=needs)), A).validation_constraints == (
        constraint("confirmation", "application", "localized", A),
        constraint("revalidation", "application", "localized", A))


@pytest.mark.parametrize("needs, match", [
    ([T("application", "localized", A)], "type"),                                                # 100
    ([{"intent": "revalidation"}], "type"),
    ([N("question", "application", "localized", A)], "intent"),                                  # 101
    ([N("revalidation", "expert", "localized", A)], "target_stage"),                             # 102
    ([N("revalidation", "application", "partial", A)], "scope_mode"),                            # 103
    ([N("revalidation", "application", "localized")], "incohérents"),                            # 104
    ([N("revalidation", "application", "localized", uid("elsewhere"))], "absent"),               # 105
    ([N("revalidation", "application", "localized", str(A))], "uuid"),
    ([N("revalidation", "application", "localized", A, A)], "double"),
    ([N("revalidation", "application", "competency_only", A)], "incohérents"),                   # 106
    ([N("revalidation", "application", "whole_competency", A)], "incohérents"),                  # 107
    ([N("revalidation", "application", "localized", A, reasons=())], "reason_codes"),            # 108
    ([N("revalidation", "application", "localized", A, reasons=("levier_contredit",))], "reason_codes"),  # 109
    ([N("revalidation", "application", "localized", A, reasons=REVAL_REASON * 2)], "reason_codes"),       # 110
    ([N("revalidation", "application", "localized", A,
        reasons=tuple(reversed(VALIDATION_REASON_CODES[:2])))], "reason_codes"),
    ([dataclasses.replace(N("revalidation", "application", "localized", A), reason_codes=list(REVAL_REASON))],
     "reason_codes"),
    ([N("revalidation", "application", "localized", A),                                          # 111
      N("revalidation", "application", "localized", A, reasons=("unresolved_revision_motif",))], "double"),
    ([N("revalidation", "application", "localized", A, B),
      N("revalidation", "application", "localized", B, A)], "double"),
])
def test_100_to_111_forged_needs_fail_closed(needs, match):
    with pytest.raises(InvalidSafeAssumptionState, match=match):
        target_of(state(s8(needs=needs)), A)


def test_needs_container_must_be_a_tuple():
    st = state(dataclasses.replace(s8(), validation_needs=[N("revalidation", "application", "localized", A)]))
    with pytest.raises(InvalidSafeAssumptionState, match="tuple"):
        target_of(st, A)


CONFIRMATION_FAMILY = ("single_representative_episode_would_benefit_from_independent_context",
                       "independence_not_established_on_current_basis",
                       "competency_only_basis_would_benefit_from_localization")
REVALIDATION_FAMILY = ("unresolved_revision_motif", "current_stage_under_tension",
                       "materially_incompatible_higher_claim")


def test_reason_code_families_are_those_of_the_t6c3_v1_policy():
    from core import adaptation_safety
    from core.inference_final_policies import FINAL_INFERENCE_V1_POLICY
    assert FINAL_INFERENCE_V1_POLICY.confirmation_stages == DCA  # jamais Mastery
    assert adaptation_safety._CONFIRMATION_REASON_CODES == frozenset(CONFIRMATION_FAMILY) == frozenset(
        rule.reason_code for rule in FINAL_INFERENCE_V1_POLICY.confirmation_motifs)
    assert adaptation_safety._REVALIDATION_REASON_CODES == frozenset(REVALIDATION_FAMILY)
    # Partition exacte du vocabulaire validation-need-v1.
    assert not set(CONFIRMATION_FAMILY) & set(REVALIDATION_FAMILY)
    assert set(CONFIRMATION_FAMILY) | set(REVALIDATION_FAMILY) == set(VALIDATION_REASON_CODES)


@pytest.mark.parametrize("need, match", [
    (N("confirmation", "mastery", "localized", A), "confirmation_stages"),                      # 1
    (N("confirmation", "mastery", "competency_only"), "confirmation_stages"),
    (N("confirmation", "application", "whole_competency"), "whole_competency"),                 # 2
    (N("confirmation", "discovery", "whole_competency"), "whole_competency"),
    *((N("confirmation", "application", "localized", A, reasons=(r,)), "famille")              # 3
      for r in REVALIDATION_FAMILY),
    *((N("revalidation", "application", "localized", A, reasons=(r,)), "famille")              # 4
      for r in CONFIRMATION_FAMILY),
    (N("confirmation", "application", "localized", A,                                          # 5
       reasons=("current_stage_under_tension", "independence_not_established_on_current_basis")), "famille"),
    (N("revalidation", "application", "localized", A,
       reasons=("unresolved_revision_motif", "competency_only_basis_would_benefit_from_localization")), "famille"),
])
def test_impossible_t6c3_combinations_fail_closed(need, match):
    for needs in ([need], [N("revalidation", "application", "localized", A), need]):
        with pytest.raises(InvalidSafeAssumptionState, match=match):
            target_of(state(s8(needs=needs)), A)
        with pytest.raises(InvalidSafeAssumptionState, match=match):
            plan(state(s8(needs=needs)), focus(cf("C8")))


@pytest.mark.parametrize("need, fo, expected", [
    *((N("confirmation", stage, "localized", A, reasons=(r,)), focus(cf("C8", A)),             # 6
       constraint("confirmation", stage, "localized", A)) for stage in DCA for r in CONFIRMATION_FAMILY),
    (N("confirmation", "comprehension", "competency_only",                                     # 7
       reasons=("competency_only_basis_would_benefit_from_localization",)), focus(cf("C8")),
     constraint("confirmation", "comprehension", "competency_only")),
    (N("confirmation", "application", "localized", A, reasons=CONFIRMATION_FAMILY[:2]), focus(cf("C8", A)),
     constraint("confirmation", "application", "localized", A)),
    *((N("revalidation", stage, "localized", A, reasons=(r,)), focus(cf("C8", A)),             # 8
       constraint("revalidation", stage, "localized", A)) for stage in STAGES for r in REVALIDATION_FAMILY),
    (N("revalidation", "application", "competency_only"), focus(cf("C8")),                    # 9
     constraint("revalidation", "application", "competency_only")),
    (N("revalidation", "mastery", "whole_competency", reasons=REVALIDATION_FAMILY[:2]), focus(cf("C8", A)),  # 10
     constraint("revalidation", "mastery", "localized", A)),
    (N("revalidation", "discovery", "whole_competency"), focus(cf("C8")),
     constraint("revalidation", "discovery", "competency_only")),
])
def test_possible_t6c3_combinations_are_still_accepted(need, fo, expected):
    result = plan(state(s8(needs=[need])), fo)
    assert result.target.validation_constraints == (expected,)
    assert result.target.coverages == plan(state(s8()), fo).target.coverages  # aucun stade retiré


def test_12_an_impossible_need_never_reaches_the_projection(monkeypatch):
    from core import adaptation_safety
    calls = []
    original = adaptation_safety._constraints

    def recording(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(adaptation_safety, "_constraints", recording)
    valid = N("revalidation", "application", "localized", A)
    assert target_of(state(s8(needs=[valid])), A).validation_constraints and len(calls) == 1
    calls.clear()
    for impossible in (N("confirmation", "mastery", "localized", A),
                       N("confirmation", "application", "whole_competency"),
                       N("revalidation", "application", "localized", A, reasons=CONFIRMATION_FAMILY[:1])):
        with pytest.raises(InvalidSafeAssumptionState):
            target_of(state(s8(needs=[valid, impossible])), A)
    assert calls == []  # jamais projeté, aucune MinimalValidationConstraint produite


# --------------------------------------------------------------------------
# 11. Séparation absolue fragilité / besoin (112-117)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("needs", [
    [N("confirmation", "application", "localized", A)],                                    # 112
    [N("revalidation", "application", "localized", A)],                                    # 113
    [N("revalidation", "discovery", "whole_competency")],
    [N("revalidation", "comprehension", "competency_only"), N("confirmation", "comprehension", "localized", A, B)],
])
def test_112_113_114_validation_needs_never_reduce_safe_stages(needs):
    without = target_of(state(s8()), A, B)
    with_needs = target_of(state(s8(needs=needs)), A, B)
    assert safe(with_needs) == safe(without) == [(A, DCAM), (B, DCAM)]
    assert with_needs.coverages == without.coverages
    st = state(s8(needs=needs))
    assert plan(st, focus(cf("C8"))).target.coverages == plan(state(s8()), focus(cf("C8"))).target.coverages


@pytest.mark.parametrize("fragile", [
    {"tensions": [T("application", "localized", A, status="revalidation_needed")]},        # 115
    {"context": ctx(M("application", "localized", A))},                                    # 116
    {"tensions": [T("application", "whole_competency")],                                   # 117
     "context": ctx(M("discovery", "whole_competency"))},
])
def test_115_116_117_fragilities_never_create_a_constraint(fragile):
    for fo in (focus(cf("C8", A, B)), focus(cf("C8"))):
        assert plan(state(s8(**fragile)), fo).target.validation_constraints == ()


# --------------------------------------------------------------------------
# 12. Projection des besoins sur le focus (118-126)
# --------------------------------------------------------------------------

def _constraints(needs, *ids):
    fo = focus(cf("C8", *ids))
    return plan(state(s8(needs=needs)), fo).target.validation_constraints


def test_118_to_123_projection_on_a_localized_focus():
    assert _constraints([N("revalidation", "application", "localized", A)], A) == (
        constraint("revalidation", "application", "localized", A),)                            # 118
    assert _constraints([N("revalidation", "application", "localized", B)], A) == ()           # 119
    assert _constraints([N("revalidation", "application", "localized", B, C)], A, B) == (
        constraint("revalidation", "application", "localized", B),)                            # 120
    assert _constraints([N("revalidation", "application", "whole_competency")], A, B) == (
        constraint("revalidation", "application", "localized", A, B),)                         # 122
    assert _constraints([N("revalidation", "application", "competency_only")], A, B) == ()     # 123


def test_121_projected_ids_follow_the_focus_order_never_a_lexical_uuid_order():
    ids = sorted((uid(f"lex-{i}") for i in range(4)), key=str, reverse=True)
    capabilities = tuple(cap(f"C8_{x}", i) for x, i in zip("ABCD", ids))
    st = state(s8(capabilities=capabilities, ladder={s: (tuple(ids), False) for s in STAGES},
                  needs=[N("confirmation", "application", "localized", *ids[1:])]))
    result = plan(st, focus(cf("C8", *ids)))
    assert result.target.validation_constraints[0].capability_definition_ids == tuple(ids[1:])
    assert [c.capability_definition_id for c in result.target.coverages] == ids


def test_124_125_126_projection_on_a_competency_only_focus():
    def only(needs):
        return plan(state(s8(needs=needs)), focus(cf("C8"))).target.validation_constraints
    assert only([N("revalidation", "application", "competency_only")]) == (
        constraint("revalidation", "application", "competency_only"),)                         # 124
    assert only([N("revalidation", "application", "whole_competency")]) == (
        constraint("revalidation", "application", "competency_only"),)                         # 125
    assert only([N("revalidation", "application", "localized", A)]) == ()                      # 126


def test_29_output_scope_is_never_whole_competency():
    needs = [N("revalidation", "application", "whole_competency")]
    for fo in (focus(cf("C8", A)), focus(cf("C8"))):
        (item,) = plan(state(s8(needs=needs)), fo).target.validation_constraints
        assert item.scope_mode in ("localized", "competency_only")


# --------------------------------------------------------------------------
# 13. Intent / stade / règles (127-131) et déduplication (132-135)
# --------------------------------------------------------------------------

def test_127_to_131_intent_stage_and_rules_are_preserved_exactly():
    st = state(s8(tensions=[T("comprehension", "localized", A)],
                  needs=[N("confirmation", "discovery", "localized", A),
                         N("revalidation", "mastery", "localized", A)]))
    target = target_of(st, A)
    assert safe(target) == [(A, DISC)]
    assert [(c.intent, c.target_stage) for c in target.validation_constraints] == [
        ("confirmation", "discovery"), ("revalidation", "mastery")]       # Mastery au-dessus des étages sûrs
    assert all((c.activation_rule, c.residual_work_rule) == (VALIDATION_ACTIVATION_RULE, VALIDATION_WORK_RULE)
               for c in target.validation_constraints)


def test_132_converging_needs_give_one_constraint():
    needs = [N("revalidation", "application", "localized", A, B),
             N("revalidation", "application", "localized", A, C, reasons=("unresolved_revision_motif",)),
             N("revalidation", "application", "whole_competency")]
    assert _constraints(needs, A) == (constraint("revalidation", "application", "localized", A),)
    only = [N("revalidation", "application", "competency_only"), N("revalidation", "application", "whole_competency")]
    assert plan(state(s8(needs=only)), focus(cf("C8"))).target.validation_constraints == (
        constraint("revalidation", "application", "competency_only"),)


def test_133_134_135_distinct_identities_are_kept_in_step5_order():
    needs = [N("confirmation", "application", "localized", A),
             N("revalidation", "application", "localized", A, B),
             N("revalidation", "comprehension", "localized", A),
             N("revalidation", "application", "whole_competency")]
    assert _constraints(needs, A) == (
        constraint("confirmation", "application", "localized", A),
        constraint("revalidation", "application", "localized", A),
        constraint("revalidation", "comprehension", "localized", A))
    assert _constraints(needs, A, B) == (
        constraint("confirmation", "application", "localized", A),
        constraint("revalidation", "application", "localized", A, B),
        constraint("revalidation", "comprehension", "localized", A))


# --------------------------------------------------------------------------
# 14. Cible / supports (136-143)
# --------------------------------------------------------------------------

C7_B = d("C7_B")


def _c7(**kwargs):
    return snap("C7", "application", {s: ((C7_B,), True) for s in DCA}, **kwargs)


def _with_support(c8, c7):
    return plan(state(*(s for s in (c7, c8) if s is not None)), focus(cf("C8", A), [cf("C7", C7_B)]))


def test_136_137_target_only_and_target_with_support():
    assert target_of(state(s8()), A).role == "target"
    assert plan(state(s8()), focus(cf("C8", A))).supporting == ()
    result = _with_support(s8(), _c7())
    assert (result.target.competency_code, [s.competency_code for s in result.supporting]) == ("C8", ["C7"])
    assert safe(result.supporting[0]) == [(C7_B, DCA)] and result.supporting[0].role == "supporting"


def test_138_143_fragile_or_empty_support_never_touches_the_target():
    reference = _with_support(s8(), _c7()).target
    fragile = _with_support(s8(), _c7(tension_state="open", tensions=(T("discovery", "whole_competency"),),
                                      context=ctx(M("discovery", "whole_competency"))))
    assert safe(fragile.supporting[0]) == [(C7_B, ())]
    assert fragile.target == reference


def test_139_fragile_target_never_touches_the_support():
    reference = _with_support(s8(), _c7()).supporting
    result = _with_support(s8(tensions=[T("discovery", "whole_competency")]), _c7())
    assert safe(result.target) == [(A, ())]
    assert result.supporting == reference


def test_140_141_needs_stay_on_their_own_competency():
    result = _with_support(s8(), _c7(needs=(N("revalidation", "application", "whole_competency"),)))
    assert result.target.validation_constraints == ()
    assert result.supporting[0].validation_constraints == (
        constraint("revalidation", "application", "localized", C7_B),)
    result = _with_support(s8(needs=[N("revalidation", "application", "whole_competency")]), _c7())
    assert result.supporting[0].validation_constraints == ()
    assert result.target.validation_constraints == (constraint("revalidation", "application", "localized", A),)


def test_142_146_support_without_state_has_no_constraint_and_no_provenance():
    result = _with_support(s8(needs=[N("revalidation", "application", "whole_competency")]), None)
    (support,) = result.supporting
    assert safe(support) == [(C7_B, ())] and support.validation_constraints == ()
    assert (support.source_inference_run_id, support.source_longitudinal_assessment_run_id,
            support.source_state_generation, support.source_taxonomy_release_id) == (None, None, None, None)
    assert result.target.validation_constraints


# --------------------------------------------------------------------------
# 15. Provenance (144-147)
# --------------------------------------------------------------------------

def test_144_provenance_is_copied_exactly_from_the_baseline():
    st = state(s8(run=uid("run-x"), t5=uid("t5-x"), generation=7, release=OTHER_RELEASE,
                  tensions=[T("application", "localized", A)]))
    fo = focus(cf("C8", A))
    baseline = build_assumption_baseline(state=st, focus=fo)
    target = build_safe_assumption_plan(baseline=baseline, state=st).target
    for name in ("role", "competency_code", "focus_scope_mode", "focus_capability_definition_ids",
                 "source_inference_run_id", "source_longitudinal_assessment_run_id", "source_state_generation",
                 "source_taxonomy_release_id"):
        assert getattr(target, name) == getattr(baseline.target, name), name
    assert (target.source_inference_run_id, target.source_longitudinal_assessment_run_id,
            target.source_state_generation, target.source_taxonomy_release_id) == (
        uid("run-x"), uid("t5-x"), 7, OTHER_RELEASE)


def test_145_no_uuid_is_generated(monkeypatch):
    st = state(s8(tensions=[T("application", "localized", A)], context=ctx(M("mastery", "localized", A)),
                  needs=[N("revalidation", "application", "localized", A)]))
    baseline = build_assumption_baseline(state=st, focus=focus(cf("C8", A)))
    reference = build_safe_assumption_plan(baseline=baseline, state=st)

    def forbidden(*args, **kwargs):
        raise AssertionError("aucun UUID ne doit être généré")

    for name in ("uuid1", "uuid3", "uuid4", "uuid5"):
        monkeypatch.setattr(uuid, name, forbidden)
    assert build_safe_assumption_plan(baseline=baseline, state=st) == reference


def test_147_user_id_alone_changes_nothing():
    competencies = (s8(tensions=[T("application", "localized", A)],
                       needs=[N("revalidation", "application", "localized", A)]),)
    fo = focus(cf("C8", A))
    assert plan(AdaptationStateSnapshot(user_id="u", competencies=competencies), fo) == plan(
        AdaptationStateSnapshot(user_id="someone-else", competencies=competencies), fo)


# --------------------------------------------------------------------------
# 16. Minimisation (148-153)
# --------------------------------------------------------------------------

def _full_state(**overrides):
    return state(*(overrides.get(code) or s8(code=code, capabilities=tuple(cap(f"{code}_{x}") for x in "ABCD"),
                                            ladder={s: ((d(f"{code}_A"),), True) for s in STAGES})
                   for code in COMPETENCY_ORDER))


def test_148_149_only_the_focused_competencies_are_output():
    result = plan(_full_state(), focus(cf("C10", d("C10_A"))))
    assert result.target.competency_code == "C10" and result.supporting == ()
    result = plan(_full_state(), focus(cf("C10", d("C10_A")), [cf("C8", d("C8_A"))]))
    assert [p.competency_code for p in (result.target, *result.supporting)] == ["C10", "C8"]


@pytest.mark.parametrize("outside", ["C3", "C4", "C5", "C2"])
def test_150_to_153_an_outside_competency_is_never_exploited(outside):
    fo = focus(cf("C10", d("C10_A")), [cf("C8", d("C8_A"))])
    reference = plan(_full_state(), fo)
    corrupted = {
        "C3": s8(code="C3", tensions=[T("discovery", "whole_competency")]),
        "C4": s8(code="C4", context={"motif": "levier contredit"}),
        "C5": s8(code="C5", needs=[N("question", "expert", "partial", uid("x"))]),
        "C2": _garbage("C2"),
    }[outside]
    assert plan(_full_state(**{outside: corrupted}), fo) == reference


# --------------------------------------------------------------------------
# 17. Déterminisme (154-159)
# --------------------------------------------------------------------------

def test_154_same_inputs_same_plan():
    st = state(s8(tensions=[T("application", "localized", A)], context=ctx(M("mastery", "whole_competency")),
                  needs=[N("revalidation", "application", "localized", A)]), _c7())
    fo = focus(cf("C8", A, D), [cf("C7", C7_B)])
    reference = plan(st, fo)
    assert all(plan(st, fo) == reference for _ in range(5))
    shuffled = list(st.competencies)
    random.Random(61).shuffle(shuffled)
    assert plan(state(*shuffled), fo) == reference


def test_156_to_159_no_clock_random_environment_or_database():
    imported = _imports()
    assert not {"time", "datetime", "random", "secrets", "os", "sys", "sqlalchemy", "core.db", "core.models",
                "threading", "asyncio", "socket", "requests", "httpx"} & imported
    tokens = set(_code_tokens(_source()).split())
    for word in ("now", "utcnow", "time", "environ", "getenv", "random", "shuffle", "choice"):
        assert word not in tokens, word


# --------------------------------------------------------------------------
# 18. Constitution statique (160-180)
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


def test_106_imports_are_exactly_public_contracts_and_step5_vocabularies():
    by_module = {}
    for node in _tree().body:
        if isinstance(node, ast.ImportFrom):
            by_module.setdefault(node.module, set()).update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            by_module.setdefault(None, set()).update(a.name for a in node.names)
    assert by_module == {
        None: {"uuid"},
        "collections.abc": {"Mapping", "Sequence"},
        "dataclasses": {"dataclass"},
        "typing": {"NamedTuple"},
        "core.adaptation_assumptions": {"ASSUMPTION_BASELINE_SCHEMA_VERSION", "ASSUMPTION_STAGE_ORDER",
                                        "AssumptionBaseline", "AssumptionBaselineError",
                                        "CompetencyAssumptionBaseline", "build_assumption_baseline"},
        "core.adaptation_focus": {"COMPETENCY_ONLY", "LOCALIZED", "RESOLVED", "CompetencyFocus",
                                  "InteractionCompetencyFocus"},
        "core.adaptation_state": {"AdaptationStateSnapshot", "AdaptationTension", "AdaptationValidationNeed"},
        # Vocabulaires / versions propriétaires Step 5 : constantes seulement.
        "core.inference_final_policies": {"CONFIRMATION", "CURRENT_STAGE_UNDER_TENSION", "FINAL_INFERENCE_V1_POLICY",
                                          "MATERIALLY_INCOMPATIBLE_HIGHER_CLAIM", "REVALIDATION",
                                          "UNRESOLVED_REVISION_MOTIF", "VALIDATION_INTENTS",
                                          "VALIDATION_REASON_CODES", "VALIDATION_SCOPE_MODES"},
        "core.inference_service": {"CLAIM_STAGES", "REVISION_STATUSES", "TENSION_NONE", "TENSION_OPEN",
                                   "TENSION_SCOPE_MODES", "WHOLE_COMPETENCY"},
        "core.inference_state_policies": {"REVISION_CONTEXT_SCHEMA_VERSION", "REVISION_REASON_CODES",
                                          "TENSION_REASON_CODES", "UNRESOLVED"},
    }
    imported = set().union(*by_module.values())
    assert not any(name.startswith("_") for name in imported)
    for reader in ("load_adaptation_state", "load_current_focus_taxonomy", "validate_focus_proposal",
                   "FocusProposal", "classify_focus_proposal"):
        assert reader not in imported


def test_revision_context_fields_mirror_the_canonical_t6c2_format():
    from core import adaptation_safety
    from core.inference_state import REVISION_CONTEXT_FIELDS, REVISION_MOTIF_FIELDS
    assert adaptation_safety._REVISION_CONTEXT_FIELDS == REVISION_CONTEXT_FIELDS
    assert adaptation_safety._REVISION_MOTIF_FIELDS == REVISION_MOTIF_FIELDS
    assert "core.inference_state" not in _imports()


def test_revision_context_parser_accepts_exactly_what_t6c2_serializes():
    """Payload produit par RevisionContextAssessment.to_payload() (format
    persisté) : accepté tel quel, sous forme figée 6-1A comme brute."""
    from core.inference_state import RevisionContextAssessment, RevisionMotif
    motifs = (RevisionMotif(fragilized_stage="application", scope_mode="localized",
                            capability_definition_ids=(A, B, *H_SORTED), source_contradiction_observation_ids=tuple(
                                SOURCES), reason_codes=TENSION_REASON_CODES[:2]),
              RevisionMotif(fragilized_stage="mastery", scope_mode="whole_competency", capability_definition_ids=(),
                            source_contradiction_observation_ids=(OBS,), reason_codes=()))
    payload = RevisionContextAssessment(schema_version="revision-context-v1", origin_inference_run_id=uid("o"),
                                        reason_code=REVISION_REASON_CODES[1], resolution_status="unresolved",
                                        motifs=motifs).to_payload()
    for shaped in (payload, frozen(payload)):
        assert safe(_context_plan(shaped, A, C)) == [(A, DC), (C, DCA)]


def test_160_to_163_no_database_access():
    imported = _imports()
    assert not {"sqlalchemy", "core.models", "core.db", "core.taxonomy_service", "core.observation_service",
                "core.longitudinal_service", "alembic", "psycopg2"} & imported
    names = _accessed_names()
    for forbidden in ("Session", "session", "db", "get_db", "execute", "query", "commit", "flush", "add_all",
                      "rollback", "no_autoflush", "select", "text", "merge", "delete", "refresh", "SessionLocal",
                      "get_observation", "get_evaluation_run", "get_competency_inference", "get_stage_claims",
                      "get_inference_tensions", "get_inference_basis_refs", "get_release_capabilities",
                      "get_validated_user_competency_state", "get_user_competency_state"):
        assert forbidden not in names, forbidden
    adds = [n.func.value.id for n in ast.walk(_tree()) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "add"]
    assert adds == ["identities", "identities"]  # sets locaux, jamais une session


def test_165_no_step5_engine_call_and_no_re_inference():
    names = _accessed_names()
    for forbidden in ("infer_competency", "evaluate_positive_basis", "evaluate_inference_state",
                      "evaluate_validation_needs", "ValidationNeedEngine", "DecisionAssembler",
                      "_parse_revision_context", "resolve_state_decision_policy", "resolve_positive_basis_policy",
                      "start_competency_inference", "complete_competency_inference"):
        assert forbidden not in names, forbidden
    called = {n.func.id for n in ast.walk(_tree()) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "build_assumption_baseline" in called  # seul usage indirect des claims : le preflight 6-1C


def test_166_167_no_llm_provider_nor_prompt():
    assert not {"anthropic", "openai", "requests", "httpx", "urllib", "socket"} & _imports()
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "backend", "classif", "model_call", "llm"):
        assert word not in tokens, word


def test_168_169_not_wired_to_api_nor_web_chat():
    assert "api" not in _imports()
    tokens = _code_tokens(_source())
    for word in ("web_chat", "web-chat", "_classify_intent", "fastapi", "request", "route"):
        assert word not in tokens, word
    # Étape 6.1E : contexte pédagogique, assembleur pur du focus validé et du
    # plan (contrats de sortie seulement, jamais build_safe_assumption_plan) ;
    # non branché : tests/test_adaptation_context.py.
    step6_consumers = {"core/adaptation_context.py": {
        "SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION", "VALIDATION_ACTIVATION_RULE", "VALIDATION_WORK_RULE",
        "CompetencySafeAssumptions", "MinimalValidationConstraint", "SafeAssumptionCoverage", "SafeAssumptionPlan"}}
    for rel, names in step6_consumers.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if "adaptation_safety" in f"{getattr(n, 'module', '')}.{a.name}"]
        assert sorted(imported) == sorted(("core.adaptation_safety", name) for name in names), rel
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if (rel != "core/adaptation_safety.py" and rel not in step6_consumers
                and "adaptation_safety" in path.read_text(encoding="utf-8", errors="replace")):
            users.append(rel)
    assert users == []
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("build_safe_assumption_plan", "SafeAssumptionPlan", "MinimalValidationConstraint",
                 "SAFE_ASSUMPTION_PLAN_SCHEMA_VERSION", "VALIDATION_ACTIVATION_RULE"):
        assert name not in api, name


def test_170_171_no_migration_no_orm():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0011_assistant_deliveries.py" and len(versions) == 11
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    for word in ("SafeAssumption", "safe_assumption", "ValidationConstraint", "validation_constraint"):
        assert word not in models, word
    tokens = _code_tokens(_source()).split()
    for word in ("__tablename__", "Column", "Base", "mapped_column", "relationship"):
        assert word not in tokens, word


def test_172_no_user_id_in_output_dataclasses_nor_read():
    for node in _tree().body:
        if isinstance(node, ast.ClassDef):
            fields = [n.target.id for n in node.body if isinstance(n, ast.AnnAssign)]
            assert not any("user" in f for f in fields), node.name
    assert "user_id" not in _accessed_names()


def test_71_173_174_claims_are_never_read_after_the_preflight():
    names = _accessed_names()
    for forbidden in ("claims", "current_stage", "basis_mode", "represented_capability_definition_ids",
                      "competency_only_basis", "confidence_profile", "mastery_assessment"):
        assert forbidden not in names, forbidden


def test_175_no_score():
    tokens = _code_tokens(_source()).lower()
    for word in ("score", "threshold", "confidence", "probab", "weight", "average", "percent", "priority",
                 "rank"):
        assert word not in tokens, word
    parts = {part for token in tokens.split() for part in token.split("_")}
    assert not {"mean", "ratio", "level", "max", "min", "sum", "count"} & parts
    assert not {"statistics", "math", "fractions", "decimal"} & _imports()


def test_176_to_180_no_question_posture_move_response_context_nor_support_trace():
    tokens = _code_tokens(_source()).lower()
    for word in ("question", "quiz", "exercise", "challenge", "suggest", "task", "posture",
                 "next_useful_move", "next_move", "pedagogicalresponsecontext", "response_context",
                 "generator_context", "actual_support_trace", "support_trace", "presentation", "surface"):
        assert word not in tokens, word
    parts = {part for token in tokens.split() for part in token.split("_")}
    for word in ("expliquer", "guider", "explain", "guide", "stay", "clarify", "deepen", "apply", "generalize",
                 "integrate", "academy", "route"):
        assert word not in parts, word


def test_35_107_outputs_carry_no_diagnostic_nor_copied_step5_payload():
    names = {f.name for cls in (SafeAssumptionCoverage, MinimalValidationConstraint, CompetencySafeAssumptions,
                                SafeAssumptionPlan) for f in dataclasses.fields(cls)}
    for forbidden in ("has_tension", "tension_state", "tensions", "fragilized_stage", "revision_status",
                      "revision_reason", "contradiction_count", "needs_revalidation", "weakness", "risk", "warning",
                      "reason_codes", "priority", "question", "task", "prompt", "diagnostic", "score",
                      "confidence", "claims", "confidence_profile", "mastery_assessment",
                      "unresolved_revision_context", "source_contradiction_observation_ids", "label",
                      "mapping_guidance", "membership_id", "user_id", "established_stages"):
        assert forbidden not in names, forbidden


def test_rules_are_internal_machine_codes_never_user_text():
    for rule in (VALIDATION_ACTIVATION_RULE, VALIDATION_WORK_RULE):
        assert rule == rule.lower() and " " not in rule and "?" not in rule


# --------------------------------------------------------------------------
# 19. Intégration PostgreSQL : T3 / T4 / T5 / T6 réels -> 6-1A + 6-1B1 + 6-1C -> 6-1D
# --------------------------------------------------------------------------

def _plan_without_any_statement(engine, snapshot, baseline):  # noqa: F811
    from tests.test_adaptation_assumptions import _dump
    statements = []

    def record(conn, cursor, sql, *args):
        statements.append(sql)

    before = _dump(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        result = build_safe_assumption_plan(baseline=baseline, state=snapshot)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements == []  # aucune lecture, aucune écriture pendant le plan
    assert _dump(engine) == before
    return result


def _expected_constraints(needs, focus_ids):
    """Projection attendue des besoins RÉELS (oracle indépendant du module)."""
    expected = []
    for need in needs:
        if need.scope_mode == "competency_only":
            continue
        ids = focus_ids if need.scope_mode == "whole_competency" else tuple(
            i for i in focus_ids if i in need.capability_definition_ids)
        item = constraint(need.intent, need.target_stage, "localized", *ids)
        if ids and item not in expected:
            expected.append(item)
    return tuple(expected)


def test_pg_real_chain_without_relevant_fragility(Sessions, engine):  # noqa: F811
    """SPEC V1 active -> T3 -> T5 -> T6 -> 6-1A -> 6-1B1 -> 6-1C -> 6-1D :
    baseline accepté canoniquement, provenance T6 réelle, safe = baseline."""
    from tests.test_longitudinal_service import sup
    release = _v1(Sessions)
    p = _pipeline(Sessions, release)
    p.t3(p.app("C7_A", "C7_D", evidence_strength="strong"))
    p.t3(sup(p.m["C7_A"], p.m["C7_B"], evidence_strength="strong"))
    context, _ = p.infer()
    snapshot, taxonomy, validated = _upstream(
        Sessions, _proposed("C7", "C7_A@r1", "C7_B@r1", "C7_C@r1", "C7_D@r1"), _proposed("C2"))
    (c7,) = snapshot.competencies
    assert c7.tensions == () and c7.unresolved_revision_context is None
    baseline = build_assumption_baseline(state=snapshot, focus=validated)

    result = _plan_without_any_statement(engine, snapshot, baseline)
    target = result.target
    assert (target.source_inference_run_id, target.source_longitudinal_assessment_run_id,
            target.source_state_generation, target.source_taxonomy_release_id) == (
        context.run_id, context.longitudinal_assessment_run_id, 1, release)
    assert safe(target) == [(c.capability_definition_id, c.established_stages) for c in baseline.target.coverages]
    assert any(c.safe_stages for c in target.coverages)
    assert target.validation_constraints == _expected_constraints(c7.validation_needs,
                                                                  validated.target.capability_definition_ids)
    (c2,) = result.supporting
    assert c2.source_inference_run_id is None and c2.validation_constraints == () and safe(c2) == [(None, ())]
    assert build_safe_assumption_plan(baseline=baseline, state=snapshot) == result
    assert "user_id" not in repr(result) and "'u'" not in repr(result)


def test_pg_real_tension_and_revalidation_give_a_localized_cutoff_and_constraint(Sessions, engine):  # noqa: F811
    """Application C7_A / C7_B, puis contradiction Application sur C7_A :
    tension Step 5 réelle localized C7_A + revalidation réelle -> cutoff
    Application sur C7_A seulement + MinimalValidationConstraint."""
    release = _v1(Sessions)
    p = _pipeline(Sessions, release)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    p.t3(p.contra("C7_A", contradiction_scope="application"))
    context, _ = p.infer()
    snapshot, taxonomy, validated = _upstream(Sessions, _proposed("C7", "C7_A@r1", "C7_B@r1"))
    ids = _ids(taxonomy)
    (c7,) = snapshot.competencies
    assert (c7.current_stage, c7.tension_state, c7.state_generation) == ("application", "open", 2)
    assert c7.tensions == (AdaptationTension(fragilized_stage="application", scope_mode="localized",
                                             capability_definition_ids=(ids["C7_A"],),
                                             revision_status="revalidation_needed"),)
    baseline = build_assumption_baseline(state=snapshot, focus=validated)
    assert [(c.capability_definition_id, c.established_stages) for c in baseline.target.coverages] == [
        (ids["C7_A"], DCA), (ids["C7_B"], DCA)]

    result = _plan_without_any_statement(engine, snapshot, baseline)
    assert result.target.source_inference_run_id == context.run_id
    assert safe(result.target) == [(ids["C7_A"], DC), (ids["C7_B"], DCA)]
    assert constraint("revalidation", "application", "localized", ids["C7_A"]) in \
        result.target.validation_constraints
    assert result.target.validation_constraints == _expected_constraints(
        c7.validation_needs, validated.target.capability_definition_ids)
    # Même état, focus C7_B seul : aucune fragilité, aucune revalidation de C7_A.
    _, _, only_b = _upstream(Sessions, _proposed("C7", "C7_B@r1"))
    on_b = build_safe_assumption_plan(baseline=build_assumption_baseline(state=snapshot, focus=only_b),
                                      state=snapshot).target
    assert safe(on_b) == [(ids["C7_B"], DCA)]
    assert all(ids["C7_A"] not in c.capability_definition_ids for c in on_b.validation_constraints)


def test_pg_real_persisted_revision_context_is_parsed_and_never_creates_a_constraint(Sessions, engine):  # noqa: F811
    """Contradiction représentative et diagnostique : revised_down
    Comprehension et revision-context-v1 RÉELLEMENT persisté (motif
    Application localized C7_A + C7_B) : relu strictement par 6-1D."""
    release = _v1(Sessions)
    p = _pipeline(Sessions, release)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    p.t3(p.contra("C7_A", "C7_B", contradiction_scope="application", evidence_strength="strong"))
    p.infer()
    snapshot, taxonomy, validated = _upstream(Sessions, _proposed("C7", "C7_A@r1", "C7_B@r1", "C7_C@r1"))
    ids = _ids(taxonomy)
    (c7,) = snapshot.competencies
    assert c7.current_stage == "comprehension"
    context = c7.unresolved_revision_context
    assert context["schema_version"] == "revision-context-v1"
    (motif,) = context["motifs"]
    assert (motif["fragilized_stage"], motif["scope_mode"], motif["capability_definition_ids"]) == (
        "application", "localized", (str(ids["C7_A"]), str(ids["C7_B"])))
    baseline = build_assumption_baseline(state=snapshot, focus=validated)

    result = _plan_without_any_statement(engine, snapshot, baseline)
    # Plafond Comprehension (6-1C) : le motif Application ne retire rien de plus.
    assert safe(result.target) == [(c.capability_definition_id, c.established_stages)
                                   for c in baseline.target.coverages]
    assert all(set(c.established_stages) <= set(DC) for c in baseline.target.coverages)
    assert result.target.validation_constraints == _expected_constraints(
        c7.validation_needs, validated.target.capability_definition_ids)
    # Le même payload réel, posé sous un état où Application serait établie,
    # coupe Application sur le périmètre exact du motif seulement.
    ladder = {s: ((ids["C7_A"], ids["C7_B"], ids["C7_C"]), False) for s in DCA}
    forged = dataclasses.replace(c7, current_stage="application", tension_state="none", tensions=(),
                                 validation_needs=(), claims=snap("C7", "application", ladder).claims)
    st = AdaptationStateSnapshot(user_id="u", competencies=(forged,))
    replayed = build_safe_assumption_plan(baseline=build_assumption_baseline(state=st, focus=validated), state=st)
    assert safe(replayed.target) == [(ids["C7_A"], DC), (ids["C7_B"], DC), (ids["C7_C"], DCA)]
    assert replayed.target.validation_constraints == ()


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_pg_unresolved_focus_ignores_a_real_fragile_state(Sessions, engine, status):  # noqa: F811
    p = _pipeline(Sessions, _v1(Sessions))
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    p.t3(p.contra("C7_A", contradiction_scope="application"))
    p.infer()
    snapshot, _, validated = _upstream(Sessions, status=status)
    assert snapshot.competencies[0].tensions
    baseline = build_assumption_baseline(state=snapshot, focus=validated)
    result = _plan_without_any_statement(engine, snapshot, baseline)
    assert (result.resolution_status, result.target, result.supporting) == (status, None, ())


def test_6_1d_never_reads_step5_itself():
    """6-1D ne relit jamais Step 5 : load_adaptation_state reste la seule
    porte d'entrée (côté orchestrateur), jamais appelée par le module."""
    assert "load_adaptation_state" not in _accessed_names()
    assert callable(load_adaptation_state)
