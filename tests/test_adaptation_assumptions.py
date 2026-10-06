"""Tests de l'Étape 6.1C : socle positif localisé des présupposés
(core/adaptation_assumptions.py).

1. Projection PURE (toujours exécutée) : AdaptationStateSnapshot et
   InteractionCompetencyFocus construits à la main -> build_assumption_baseline.
   Contrats, statuts non résolus, absence Step 5 vs non_etabli, localized,
   competency_only, plafond current_stage, échelle complète sans trou comblé,
   cible / supports, minimisation, provenance technique, indépendance vis-à-vis
   des fragilités / contexte de révision / besoins de validation / confiance /
   évaluation de maîtrise, cross-release, fail closed, déterminisme.

2. Contrat statique : API publique, imports, aucune base, aucun modèle de
   langage, aucune persistance, aucun branchement runtime, aucune brique 6-1D+.

3. Intégration contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL, sinon
   SKIPPÉS ; jamais SQLite) : SPEC V1 bootstrap + activation -> T3 -> T4 ->
   T5 -> T6 réels -> load_adaptation_state -> load_current_focus_taxonomy ->
   FocusProposal -> validate_focus_proposal -> build_assumption_baseline ;
   aucune requête pendant le build ; cross-release réel (même definition_id
   compatible, même capability_code sous un autre definition_id jamais
   remappé, competency_only d'une autre release sans baseline).
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
    ASSUMPTION_ROLES,
    ASSUMPTION_STAGE_ORDER,
    NON_ETABLI,
    SUPPORTING,
    TARGET,
    AssumptionBaseline,
    AssumptionBaselineError,
    AssumptionCoverage,
    CompetencyAssumptionBaseline,
    InvalidAssumptionBaselineArgument,
    InvalidAssumptionBaselineState,
    InvalidAssumptionFocus,
    build_assumption_baseline,
)
from core.adaptation_focus import (
    FOCUS_POLICY_VERSION,
    FOCUS_SCHEMA_VERSION,
    CompetencyFocus,
    FocusProposal,
    InteractionCompetencyFocus,
    ProposedCompetencyFocus,
    load_current_focus_taxonomy,
    validate_focus_proposal,
)
from core.adaptation_state import (
    COMPETENCY_ORDER,
    AdaptationStageClaim,
    AdaptationStateSnapshot,
    AdaptationTension,
    AdaptationValidationNeed,
    CapabilitySemanticRef,
    CompetencyAdaptationSnapshot,
    load_adaptation_state,
)
from tests.test_adaptation_state import World, mastery_payload, profile
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens

MODULE_PATH = REPO_ROOT / "core" / "adaptation_assumptions.py"
NS = uuid.UUID("6a1c0000-0000-4000-8000-000000000000")
STAGES = ("discovery", "comprehension", "application", "mastery")
FINGERPRINT = "50b690b9e5ddd6812b625b02037b6f29b8b6cd553c3cf7d4069a634f7df5e2f7"


def uid(name: str) -> uuid.UUID:
    return uuid.uuid5(NS, name)


RELEASE = uid("release-focus")      # release du focus (et, par défaut, du snapshot)
OTHER_RELEASE = uid("release-old")  # release historique d'un snapshot


def d(code: str) -> uuid.UUID:
    """definition_id stable d'une capacité (ex. d("C8_A"))."""
    return uid(f"d-{code}")


# --------------------------------------------------------------------------
# Constructeurs
# --------------------------------------------------------------------------

def cap(code, definition_id=None, *, revision=1, label=None, release=RELEASE):
    return CapabilitySemanticRef(membership_id=uid(f"m-{code}-{release}-{definition_id}"),
                                 definition_id=definition_id or d(code), capability_code=code,
                                 semantic_revision=revision, label=label or f"label {code}")


def claim(stage, ids=(), only=False, *, status=None, basis_mode=None, confidence=None, assessment=None):
    status = status or ("established" if ids or only else "not_established")
    established = status == "established"
    return AdaptationStageClaim(
        stage=stage, status=status,
        basis_mode=basis_mode or ("direct" if established else "none"),
        represented_capability_definition_ids=tuple(ids), competency_only_basis=only,
        confidence_profile=(confidence if confidence is not None else MappingProxyType(profile(*ids)))
        if established else None,
        mastery_assessment=assessment if stage == "mastery" else None)


def claims(ladder=None, **overrides):
    """ladder : {stage: (ids, competency_only)} ; stade absent => not_established."""
    ladder = ladder or {}
    return tuple(claim(stage, *(ladder[stage] if stage in ladder else ((), False)), **overrides.get(stage, {}))
                 for stage in STAGES)


def snap(code="C8", current="application", ladder=None, *, release=RELEASE, capabilities=None, run=None,
         t5=None, generation=1, tension_state="none", tensions=(), context=None, needs=(), claim_overrides=None):
    capabilities = capabilities if capabilities is not None else tuple(cap(f"{code}_{x}") for x in "ABCD")
    return CompetencyAdaptationSnapshot(
        competency_code=code, current_stage=current, tension_state=tension_state,
        active_inference_run_id=run or uid(f"run-{code}"), longitudinal_assessment_run_id=t5 or uid(f"t5-{code}"),
        state_generation=generation, taxonomy_release_id=release, capabilities=capabilities,
        claims=claims(ladder, **(claim_overrides or {})), tensions=tensions, unresolved_revision_context=context,
        validation_needs=needs)


def state(*competencies):
    return AdaptationStateSnapshot(user_id="u", competencies=tuple(competencies))


def cf(code, *ids):
    """CompetencyFocus validé : localized si des definition_id, sinon competency_only."""
    return CompetencyFocus(competency_code=code, scope_mode="localized" if ids else "competency_only",
                           capability_definition_ids=tuple(ids))


def focus(target=None, supporting=(), *, status="resolved", release=RELEASE, **overrides):
    kwargs = dict(schema_version=FOCUS_SCHEMA_VERSION, policy_version=FOCUS_POLICY_VERSION,
                  taxonomy_release_id=release, taxonomy_spec_fingerprint=FINGERPRINT, resolution_status=status,
                  target=target, supporting=tuple(supporting))
    kwargs.update(overrides)
    return InteractionCompetencyFocus(**kwargs)


def build(st, fo):
    return build_assumption_baseline(state=st, focus=fo)


def stages_of(baseline_part):
    return [(c.capability_definition_id, c.established_stages) for c in baseline_part.coverages]


# C8 Application : C8_A Discovery -> Application, C8_D Discovery seulement.
C8_LADDER = {"discovery": ((d("C8_A"), d("C8_D")), False), "comprehension": ((d("C8_A"),), False),
             "application": ((d("C8_A"),), False)}
DCA = ("discovery", "comprehension", "application")


def full_state():
    """C1 -> C12, chacune Application sur sa capacité _A, competency_only
    jusqu'à Comprehension."""
    return state(*(snap(code, "application", {
        "discovery": ((d(f"{code}_A"),), True), "comprehension": ((d(f"{code}_A"),), True),
        "application": ((d(f"{code}_A"),), False)}) for code in COMPETENCY_ORDER))


# --------------------------------------------------------------------------
# 1. Contrats (1-7)
# --------------------------------------------------------------------------

def test_01_schema_version_and_vocabularies_are_exact():
    assert ASSUMPTION_BASELINE_SCHEMA_VERSION == "assumption-baseline-v1"
    assert ASSUMPTION_STAGE_ORDER == ("discovery", "comprehension", "application", "mastery")
    assert NON_ETABLI == "non_etabli"
    assert (TARGET, SUPPORTING) == ("target", "supporting") and ASSUMPTION_ROLES == ("target", "supporting")


def test_02_dataclasses_are_frozen_keyword_only_with_the_exact_fields():
    expected = {
        AssumptionCoverage: ["capability_definition_id", "established_stages"],
        CompetencyAssumptionBaseline: ["role", "competency_code", "focus_scope_mode",
                                       "focus_capability_definition_ids", "source_inference_run_id",
                                       "source_longitudinal_assessment_run_id", "source_state_generation",
                                       "source_taxonomy_release_id", "coverages"],
        AssumptionBaseline: ["schema_version", "focus_schema_version", "focus_policy_version",
                             "focus_taxonomy_release_id", "focus_taxonomy_spec_fingerprint", "resolution_status",
                             "target", "supporting"],
    }
    for cls, names in expected.items():
        assert [f.name for f in dataclasses.fields(cls)] == names
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls))
    baseline = build(state(snap()), focus(cf("C8", d("C8_A"))))
    with pytest.raises(dataclasses.FrozenInstanceError):
        baseline.target = None
    with pytest.raises(dataclasses.FrozenInstanceError):
        baseline.target.coverages[0].established_stages = ("mastery",)
    with pytest.raises(TypeError):
        AssumptionCoverage(None, ())  # keyword-only


def test_03_public_api_is_exactly_build_assumption_baseline():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    public = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == {"build_assumption_baseline"}
    params = list(inspect.signature(build_assumption_baseline).parameters.values())
    assert [p.name for p in params] == ["state", "focus"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty for p in params)
    with pytest.raises(TypeError):
        build_assumption_baseline(state(), focus(status="neutral"))  # positionnel refusé


def test_04_no_user_id_in_the_output():
    for cls in (AssumptionBaseline, CompetencyAssumptionBaseline, AssumptionCoverage):
        assert not any("user" in f.name for f in dataclasses.fields(cls))
    baseline = build(state(snap()), focus(cf("C8", d("C8_A"))))
    assert "'u'" not in repr(baseline) and "user" not in repr(baseline)


def test_05_06_07_coverage_shapes():
    localized = build(state(snap(ladder=C8_LADDER)), focus(cf("C8", d("C8_A")))).target.coverages
    assert localized == (AssumptionCoverage(capability_definition_id=d("C8_A"), established_stages=DCA),)
    assert type(localized[0].capability_definition_id) is uuid.UUID
    only = build(state(snap()), focus(cf("C8"))).target.coverages
    assert only == (AssumptionCoverage(capability_definition_id=None, established_stages=()),)
    assert type(localized) is tuple and type(localized[0].established_stages) is tuple
    assert type(only[0].established_stages) is tuple


def test_errors_are_a_dedicated_business_hierarchy():
    for cls in (InvalidAssumptionBaselineArgument, InvalidAssumptionBaselineState, InvalidAssumptionFocus):
        assert issubclass(cls, AssumptionBaselineError)
    assert issubclass(AssumptionBaselineError, Exception)


def test_final_example_of_the_specification():
    st = state(snap("C7", "comprehension", {"discovery": ((d("C7_B"),), False),
                                            "comprehension": ((d("C7_B"),), False)}),
               snap("C8", "application", C8_LADDER))
    fo = focus(cf("C8", d("C8_A"), d("C8_D")), (cf("C7", d("C7_B")),))
    assert build(st, fo) == AssumptionBaseline(
        schema_version="assumption-baseline-v1", focus_schema_version=FOCUS_SCHEMA_VERSION,
        focus_policy_version=FOCUS_POLICY_VERSION, focus_taxonomy_release_id=RELEASE,
        focus_taxonomy_spec_fingerprint=FINGERPRINT, resolution_status="resolved",
        target=CompetencyAssumptionBaseline(
            role="target", competency_code="C8", focus_scope_mode="localized",
            focus_capability_definition_ids=(d("C8_A"), d("C8_D")), source_inference_run_id=uid("run-C8"),
            source_longitudinal_assessment_run_id=uid("t5-C8"), source_state_generation=1,
            source_taxonomy_release_id=RELEASE,
            coverages=(AssumptionCoverage(capability_definition_id=d("C8_A"), established_stages=DCA),
                       AssumptionCoverage(capability_definition_id=d("C8_D"), established_stages=("discovery",)))),
        supporting=(CompetencyAssumptionBaseline(
            role="supporting", competency_code="C7", focus_scope_mode="localized",
            focus_capability_definition_ids=(d("C7_B"),), source_inference_run_id=uid("run-C7"),
            source_longitudinal_assessment_run_id=uid("t5-C7"), source_state_generation=1,
            source_taxonomy_release_id=RELEASE,
            coverages=(AssumptionCoverage(capability_definition_id=d("C7_B"),
                                          established_stages=("discovery", "comprehension")),)),),
    )


# --------------------------------------------------------------------------
# 2. Statuts non résolus (8-12)
# --------------------------------------------------------------------------

class _Untouchable:
    """competencies piégées : toute inspection échoue."""

    def __getattribute__(self, name):
        raise AssertionError(f"compétences inspectées ({name}) pour un focus non résolu")


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_08_09_10_unresolved_statuses_give_no_competency_baseline(status):
    baseline = build(state(), focus(status=status))
    assert baseline == AssumptionBaseline(
        schema_version=ASSUMPTION_BASELINE_SCHEMA_VERSION, focus_schema_version=FOCUS_SCHEMA_VERSION,
        focus_policy_version=FOCUS_POLICY_VERSION, focus_taxonomy_release_id=RELEASE,
        focus_taxonomy_spec_fingerprint=FINGERPRINT, resolution_status=status, target=None, supporting=())


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_11_full_c1_c12_state_does_not_change_unresolved_outputs(status):
    assert build(full_state(), focus(status=status)) == build(state(), focus(status=status))


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_12_no_state_competency_is_inspected_for_an_unresolved_focus(status):
    trapped = AdaptationStateSnapshot(user_id="u", competencies=_Untouchable())
    baseline = build(trapped, focus(status=status))
    assert (baseline.target, baseline.supporting) == (None, ())
    with pytest.raises(InvalidAssumptionBaselineState):  # resolved : la structure est vérifiée
        build(AdaptationStateSnapshot(user_id="u", competencies=[]), focus(cf("C8")))


# --------------------------------------------------------------------------
# 3. Absence Step 5 / non_etabli (13-16)
# --------------------------------------------------------------------------

def _no_provenance(part):
    return (part.source_inference_run_id, part.source_longitudinal_assessment_run_id,
            part.source_state_generation, part.source_taxonomy_release_id) == (None, None, None, None)


def test_13_target_absent_from_the_state():
    baseline = build(state(snap("C8", ladder=C8_LADDER)), focus(cf("C10", d("C10_B"))))
    assert baseline.target == CompetencyAssumptionBaseline(
        role="target", competency_code="C10", focus_scope_mode="localized",
        focus_capability_definition_ids=(d("C10_B"),), source_inference_run_id=None,
        source_longitudinal_assessment_run_id=None, source_state_generation=None, source_taxonomy_release_id=None,
        coverages=(AssumptionCoverage(capability_definition_id=d("C10_B"), established_stages=()),))
    only = build(state(), focus(cf("C10"))).target
    assert _no_provenance(only) and only.coverages == (AssumptionCoverage(capability_definition_id=None,
                                                                         established_stages=()),)


def test_14_support_absent_from_the_state():
    baseline = build(state(snap("C8", ladder=C8_LADDER)), focus(cf("C8", d("C8_A")), (cf("C9", d("C9_C")),)))
    (support,) = baseline.supporting
    assert support.role == "supporting" and support.competency_code == "C9" and _no_provenance(support)
    assert stages_of(support) == [(d("C9_C"), ())]


def test_15_present_non_etabli_keeps_provenance_and_empty_ladders():
    present = snap("C10", NON_ETABLI, run=uid("R42"), generation=3)
    target = build(state(present), focus(cf("C10", d("C10_B")))).target
    assert (target.source_inference_run_id, target.source_longitudinal_assessment_run_id,
            target.source_state_generation, target.source_taxonomy_release_id) == (
        uid("R42"), uid("t5-C10"), 3, RELEASE)
    assert stages_of(target) == [(d("C10_B"), ())]


def test_16_absence_and_non_etabli_are_structurally_different():
    fo = focus(cf("C10", d("C10_B")))
    absent = build(state(), fo).target
    non_etabli = build(state(snap("C10", NON_ETABLI)), fo).target
    assert absent.coverages == non_etabli.coverages
    assert absent != non_etabli
    assert _no_provenance(absent) and not _no_provenance(non_etabli)


# --------------------------------------------------------------------------
# 4. Localized (17-29)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("current, ladder, expected", [
    ("discovery", {"discovery": ((d("C8_A"),), False)}, ("discovery",)),
    ("comprehension", {"discovery": ((d("C8_A"),), False), "comprehension": ((d("C8_A"),), False)},
     ("discovery", "comprehension")),
    ("application", C8_LADDER, DCA),
    ("mastery", {**C8_LADDER, "mastery": ((d("C8_A"),), False)}, STAGES),
], ids=["17-discovery", "18-comprehension", "19-application", "20-mastery"])
def test_17_to_20_localized_ladders(current, ladder, expected):
    assert stages_of(build(state(snap(current=current, ladder=ladder)), focus(cf("C8", d("C8_A")))).target) == [
        (d("C8_A"), expected)]


def test_20_mastery_localized_uses_only_the_represented_scope():
    ladder = {**C8_LADDER, "mastery": ((d("C8_A"),), False)}
    fo = focus(cf("C8", d("C8_A"), d("C8_D")))
    assert stages_of(build(state(snap(current="mastery", ladder=ladder)), fo).target) == [
        (d("C8_A"), STAGES), (d("C8_D"), ("discovery",))]


def test_21_two_capabilities_with_different_ladders():
    target = build(state(snap(ladder=C8_LADDER)), focus(cf("C8", d("C8_A"), d("C8_D")))).target
    assert stages_of(target) == [(d("C8_A"), DCA), (d("C8_D"), ("discovery",))]


def test_22_coverage_order_is_the_focus_order():
    # Ordre du focus volontairement différent de celui des capacités du snapshot.
    target = build(state(snap(ladder=C8_LADDER)), focus(cf("C8", d("C8_D"), d("C8_B"), d("C8_A")))).target
    assert [c.capability_definition_id for c in target.coverages] == [d("C8_D"), d("C8_B"), d("C8_A")]
    assert target.focus_capability_definition_ids == (d("C8_D"), d("C8_B"), d("C8_A"))


def test_23_focus_definition_absent_from_the_snapshot_capabilities():
    unknown = uid("C8_E-new-definition")
    assert stages_of(build(state(snap(ladder=C8_LADDER)), focus(cf("C8", unknown, d("C8_A")))).target) == [
        (unknown, ()), (d("C8_A"), DCA)]


def test_24_same_definition_id_in_another_release_is_compatible():
    historical = snap(ladder=C8_LADDER, release=OTHER_RELEASE,
                      capabilities=tuple(cap(f"C8_{x}", release=OTHER_RELEASE) for x in "ABCD"))
    target = build(state(historical), focus(cf("C8", d("C8_A")), release=RELEASE)).target
    assert stages_of(target) == [(d("C8_A"), DCA)]
    assert target.source_taxonomy_release_id == OTHER_RELEASE


def _historical_c8_a(**cap_kwargs):
    """Snapshot R1 dont C8_A est une AUTRE définition (definition_id X) que
    celle du focus R2 (d("C8_A")) ; échelle établie sur X."""
    x = uid("C8_A-historical-definition")
    capabilities = (cap("C8_A", x, release=OTHER_RELEASE, **cap_kwargs),
                    *(cap(f"C8_{l}", release=OTHER_RELEASE) for l in "BCD"))
    ladder = {stage: ((x,), False) for stage in DCA}
    return snap(ladder=ladder, release=OTHER_RELEASE, capabilities=capabilities), x


@pytest.mark.parametrize("cap_kwargs", [
    {}, {"label": "label C8_A"}, {"revision": 1}, {"label": "label C8_A", "revision": 1}],
    ids=["25-same-code", "26-same-label", "27-same-semantic-revision", "same-everything"])
def test_25_26_27_same_code_label_or_revision_with_another_definition_is_never_remapped(cap_kwargs):
    historical, x = _historical_c8_a(**cap_kwargs)
    assert historical.capabilities[0].capability_code == "C8_A"
    target = build(state(historical), focus(cf("C8", d("C8_A")))).target
    assert stages_of(target) == [(d("C8_A"), ())]
    assert stages_of(build(state(historical), focus(cf("C8", x))).target) == [(x, DCA)]  # X lui-même


def test_28_competency_only_basis_never_localizes_a_capability():
    only = {stage: ((), True) for stage in DCA}
    fo = focus(cf("C8", d("C8_A")))
    assert stages_of(build(state(snap(ladder=only)), fo).target) == [(d("C8_A"), ())]
    mixed = {stage: ((d("C8_B"),), True) for stage in DCA}
    assert stages_of(build(state(snap(ladder=mixed)), fo).target) == [(d("C8_A"), ())]


def test_29_explicit_represented_id_localizes_the_capability():
    mixed = {stage: ((d("C8_A"),), True) for stage in DCA}
    assert stages_of(build(state(snap(ladder=mixed)), focus(cf("C8", d("C8_A")))).target) == [(d("C8_A"), DCA)]


# --------------------------------------------------------------------------
# 5. Competency_only (30-35)
# --------------------------------------------------------------------------

def test_30_same_release_competency_only_discovery():
    st = state(snap(current="discovery", ladder={"discovery": ((), True)}))
    assert stages_of(build(st, focus(cf("C8"))).target) == [(None, ("discovery",))]


def test_31_same_release_competency_only_up_to_application():
    st = state(snap(ladder={stage: ((), True) for stage in DCA}))
    assert stages_of(build(st, focus(cf("C8"))).target) == [(None, DCA)]
    partial = state(snap(ladder={"discovery": ((), True), "comprehension": ((d("C8_A"),), True),
                                 "application": ((d("C8_A"),), False)}))
    assert stages_of(build(partial, focus(cf("C8"))).target) == [(None, ("discovery", "comprehension"))]


def test_32_localized_scopes_alone_never_create_a_competency_only_baseline():
    every = tuple(d(f"C8_{x}") for x in "ABCD")
    st = state(snap(current="mastery", ladder={stage: (every, False) for stage in STAGES}))
    assert stages_of(build(st, focus(cf("C8"))).target) == [(None, ())]


def test_33_34_other_release_gives_no_competency_only_baseline():
    ladder = {stage: ((), True) for stage in DCA}
    same = build(state(snap("C8", ladder=ladder, release=RELEASE)), focus(cf("C8"), release=RELEASE)).target
    assert stages_of(same) == [(None, DCA)]  # témoin : même release
    other = state(snap("C8", ladder=ladder, release=OTHER_RELEASE))
    target = build(other, focus(cf("C8"), release=RELEASE)).target
    assert target.competency_code == other.competencies[0].competency_code == "C8"  # même code : aucun contournement
    assert stages_of(target) == [(None, ())]
    assert target.source_taxonomy_release_id == OTHER_RELEASE  # provenance toujours copiée


def test_35_exactly_one_coverage_with_none():
    target = build(state(snap(ladder={stage: ((), True) for stage in DCA})), focus(cf("C8"))).target
    assert len(target.coverages) == 1 and target.coverages[0].capability_definition_id is None
    assert target.focus_scope_mode == "competency_only" and target.focus_capability_definition_ids == ()


# --------------------------------------------------------------------------
# 6. current_stage = plafond (36-40)
# --------------------------------------------------------------------------

FULL_LADDER = {stage: ((d("C8_A"),), True) for stage in STAGES}


@pytest.mark.parametrize("current, expected", [
    (NON_ETABLI, ()), ("discovery", ("discovery",)), ("comprehension", ("discovery", "comprehension")),
    ("application", DCA), ("mastery", STAGES)], ids=["36", "37", "38", "39", "mastery"])
def test_36_to_39_current_stage_is_a_ceiling(current, expected):
    st = state(snap(current=current, ladder=FULL_LADDER))
    assert stages_of(build(st, focus(cf("C8", d("C8_A")))).target) == [(d("C8_A"), expected)]
    assert stages_of(build(st, focus(cf("C8"))).target) == [(None, expected)]


def test_40_claim_above_current_stage_never_raises_the_baseline():
    st = state(snap(current="comprehension", ladder=C8_LADDER))
    assert stages_of(build(st, focus(cf("C8", d("C8_A")))).target) == [(d("C8_A"), ("discovery", "comprehension"))]
    forged = state(snap(current=NON_ETABLI, ladder=FULL_LADDER, run=uid("R42")))
    target = build(forged, focus(cf("C8", d("C8_A")))).target
    assert stages_of(target) == [(d("C8_A"), ())] and target.source_inference_run_id == uid("R42")


def test_current_stage_is_never_a_coverage():
    # current_stage application, mais C8_B n'est représentée par aucune claim.
    st = state(snap(current="application", ladder=C8_LADDER))
    assert stages_of(build(st, focus(cf("C8", d("C8_B")))).target) == [(d("C8_B"), ())]
    # current_stage application, aucune claim établie du tout.
    assert stages_of(build(state(snap(current="application")), focus(cf("C8", d("C8_A")))).target) == [
        (d("C8_A"), ())]


# --------------------------------------------------------------------------
# 7. Claims (41-46)
# --------------------------------------------------------------------------

def test_41_not_established_claims_are_excluded():
    # not_established forgé avec un périmètre : jamais retenu.
    overrides = {"comprehension": {"status": "not_established"}}
    st = state(snap(ladder=C8_LADDER, claim_overrides=overrides))
    assert stages_of(build(st, focus(cf("C8", d("C8_A")))).target) == [(d("C8_A"), ("discovery", "application"))]


def test_42_established_claim_counts_only_on_a_compatible_scope():
    st = state(snap(ladder=C8_LADDER))
    assert stages_of(build(st, focus(cf("C8", d("C8_A"), d("C8_C")))).target) == [
        (d("C8_A"), DCA), (d("C8_C"), ())]


def test_43_no_automatic_gap_filling():
    gap = {"discovery": ((d("C8_A"),), True), "application": ((d("C8_A"),), True)}
    st = state(snap(ladder=gap))
    assert stages_of(build(st, focus(cf("C8", d("C8_A")))).target) == [(d("C8_A"), ("discovery", "application"))]
    assert stages_of(build(st, focus(cf("C8"))).target) == [(None, ("discovery", "application"))]


def test_44_basis_mode_is_never_interpreted():
    reference = build(state(snap(ladder=C8_LADDER)), focus(cf("C8", d("C8_A"), d("C8_D"))))
    for mode in ("implied_by_higher_claim", "direct", "none", "anything"):
        overrides = {stage: {"basis_mode": mode} for stage in DCA}
        assert build(state(snap(ladder=C8_LADDER, claim_overrides=overrides)),
                     focus(cf("C8", d("C8_A"), d("C8_D")))) == reference


def test_45_46_no_observation_nor_positive_basis_ref_is_read():
    imported = _imports()
    assert not {"core.observation_service", "core.longitudinal_service", "core.inference_service",
                "core.inference_positive_basis", "core.longitudinal_view"} & imported
    tokens = _code_tokens(_source())
    for word in ("observation", "positive_basis", "basis_refs", "_acquire", "get_", "load_adaptation_state"):
        assert word not in tokens, word


def test_real_6_1a_projection_is_consumed_as_is():
    """Snapshot produit par la VRAIE projection 6-1A (World de
    tests/test_adaptation_state.py) : Application directe localized C8_A/B,
    Comprehension / Discovery implied."""
    w = World()
    o = w.obs("a", "A", "B")
    w.direct("application", o)
    w.implied("comprehension")
    w.implied("discovery")
    w.current_stage = "application"
    projected = w.project()
    target = build(state(projected), focus(cf("C8", w.d("A"), w.d("C")), release=w.release)).target
    assert stages_of(target) == [(w.d("A"), DCA), (w.d("C"), ())]
    assert (target.source_inference_run_id, target.source_taxonomy_release_id) == (w.run_id, w.release)


# --------------------------------------------------------------------------
# 8. Indépendance : fragilités, révision, besoins, confiance, maîtrise (47-52)
# --------------------------------------------------------------------------

def _reference():
    fo = focus(cf("C8", d("C8_A"), d("C8_D")), (cf("C7"),))
    c7 = snap("C7", "comprehension", {"discovery": ((), True), "comprehension": ((), True)})
    return fo, c7, build(state(c7, snap(ladder=C8_LADDER)), fo)


def test_47_48_tensions_and_tension_state_change_nothing():
    fo, c7, reference = _reference()
    tensions = (AdaptationTension(fragilized_stage="application", scope_mode="localized",
                                  capability_definition_ids=(d("C8_A"),), revision_status="revalidation_needed"),
                AdaptationTension(fragilized_stage="discovery", scope_mode="whole_competency",
                                  capability_definition_ids=(), revision_status="unresolved"))
    for tension_state, items in (("open", tensions), ("open", ()), ("none", tensions), ("x", "forged")):
        assert build(state(c7, snap(ladder=C8_LADDER, tension_state=tension_state, tensions=items)), fo) == reference


def test_49_unresolved_revision_context_changes_nothing():
    fo, c7, reference = _reference()
    for context in ({"motif": "revision", "stage": "application"}, MappingProxyType({"x": [1]}), "opaque"):
        assert build(state(c7, snap(ladder=C8_LADDER, context=context)), fo) == reference


def test_50_validation_needs_change_nothing():
    fo, c7, reference = _reference()
    needs = (AdaptationValidationNeed(intent="revalidation", target_stage="application", scope_mode="localized",
                                      capability_definition_ids=(d("C8_A"),),
                                      reason_codes=("unresolved_revision_motif",)),
             AdaptationValidationNeed(intent="confirmation", target_stage="mastery", scope_mode="competency_only",
                                      capability_definition_ids=(), reason_codes=("x",)))
    for items in (needs, (), "forged"):
        assert build(state(c7, snap(ladder=C8_LADDER, needs=items)), fo) == reference


def test_51_confidence_profile_changes_nothing():
    fo, c7, reference = _reference()
    for confidence in (MappingProxyType(profile(facts=("weak",))), MappingProxyType({}), "forged"):
        overrides = {stage: {"confidence": confidence} for stage in DCA}
        assert build(state(c7, snap(ladder=C8_LADDER, claim_overrides=overrides)), fo) == reference


def test_52_mastery_assessment_changes_nothing_when_the_represented_scope_is_identical():
    fo = focus(cf("C8", d("C8_A"), d("C8_D")))
    ladder = {**C8_LADDER, "mastery": ((d("C8_A"),), False)}
    payloads = (MappingProxyType(mastery_payload(d("C8_A"))), MappingProxyType(mastery_payload(d("C8_D"))),
                MappingProxyType(mastery_payload(d("C8_A"), status="not_supported")), None, "forged")
    results = {build(state(snap(current="mastery", ladder=ladder,
                                claim_overrides={"mastery": {"assessment": p}})), fo) for p in payloads}
    assert len(results) == 1
    (only,) = results
    assert stages_of(only.target) == [(d("C8_A"), STAGES), (d("C8_D"), ("discovery",))]


# --------------------------------------------------------------------------
# 9. Supports (53-58)
# --------------------------------------------------------------------------

C11_LADDER = {stage: ((d("C11_B"),), False) for stage in DCA}
C9_LADDER = {"discovery": ((d("C9_C"),), False), "comprehension": ((d("C9_C"),), False)}


def test_53_target_positive_and_support_positive():
    st = state(snap("C9", "comprehension", C9_LADDER), snap("C11", "application", C11_LADDER))
    baseline = build(st, focus(cf("C11", d("C11_B")), (cf("C9", d("C9_C")),)))
    assert stages_of(baseline.target) == [(d("C11_B"), DCA)]
    assert stages_of(baseline.supporting[0]) == [(d("C9_C"), ("discovery", "comprehension"))]


def test_54_56_target_positive_support_absent_never_blocks_the_target():
    st = state(snap("C11", "application", C11_LADDER))
    alone = build(st, focus(cf("C11", d("C11_B"))))
    with_support = build(st, focus(cf("C11", d("C11_B")), (cf("C9", d("C9_C")),)))
    assert with_support.target == alone.target
    assert stages_of(with_support.target) == [(d("C11_B"), DCA)]
    assert _no_provenance(with_support.supporting[0]) and stages_of(with_support.supporting[0]) == [(d("C9_C"), ())]


def test_55_target_absent_support_positive():
    baseline = build(state(snap("C9", "comprehension", C9_LADDER)),
                     focus(cf("C11", d("C11_B")), (cf("C9", d("C9_C")),)))
    assert _no_provenance(baseline.target) and stages_of(baseline.target) == [(d("C11_B"), ())]
    assert stages_of(baseline.supporting[0]) == [(d("C9_C"), ("discovery", "comprehension"))]


def test_57_target_is_never_reintroduced_as_a_support():
    baseline = build(full_state(), focus(cf("C8", d("C8_A")), (cf("C7"), cf("C9"))))
    assert baseline.target.competency_code == "C8" and baseline.target.role == "target"
    assert [s.competency_code for s in baseline.supporting] == ["C7", "C9"]
    assert all(s.role == "supporting" for s in baseline.supporting)
    with pytest.raises(InvalidAssumptionFocus, match="déjà la cible"):
        build(full_state(), focus(cf("C8"), (cf("C8"),)))


def test_58_support_order_c2_c7_c10_is_preserved():
    baseline = build(full_state(), focus(cf("C4"), (cf("C2"), cf("C7", d("C7_A")), cf("C10"))))
    assert [s.competency_code for s in baseline.supporting] == ["C2", "C7", "C10"]


# --------------------------------------------------------------------------
# 10. Minimisation (59-63)
# --------------------------------------------------------------------------

def _codes(baseline):
    return [p.competency_code for p in (baseline.target, *baseline.supporting)]


def test_59_60_only_the_focused_competencies_are_output():
    assert _codes(build(full_state(), focus(cf("C10", d("C10_A"))))) == ["C10"]
    assert _codes(build(full_state(), focus(cf("C10", d("C10_A")), (cf("C8"),)))) == ["C10", "C8"]


@pytest.mark.parametrize("outside", ["C1", "C12"], ids=["61-C1", "62-C12"])
def test_61_62_modifying_an_outside_competency_changes_nothing(outside):
    fo = focus(cf("C10", d("C10_A")), (cf("C8"),))
    reference = build(full_state(), fo)
    modified = tuple(snap(outside, NON_ETABLI, run=uid("other"), release=OTHER_RELEASE, generation=99,
                          tension_state="open", needs="forged") if c.competency_code == outside else c
                     for c in full_state().competencies)
    assert build(state(*modified), fo) == reference
    removed = tuple(c for c in full_state().competencies if c.competency_code != outside)
    assert build(state(*removed), fo) == reference


def test_63_a_clearly_different_outside_snapshot_is_never_exploited():
    fo = focus(cf("C10", d("C10_A")))
    reference = build(state(snap("C10", ladder={"discovery": ((d("C10_A"),), False)})), fo)
    # Snapshots hors focus incohérents (claims manquantes, doublons, types forgés) : jamais validés ni exploités.
    broken = dataclasses.replace(snap("C3"), claims=(), capabilities="forged", state_generation="x")
    st = state(broken, broken, snap("C10", ladder={"discovery": ((d("C10_A"),), False)}),
               snap("C11", "mastery", {stage: ((d("C11_A"),), True) for stage in STAGES}))
    assert build(st, fo) == reference


# --------------------------------------------------------------------------
# 11. Provenance (64-69)
# --------------------------------------------------------------------------

def test_64_to_67_provenance_is_copied_exactly():
    present = snap("C8", ladder=C8_LADDER, run=uid("R7"), t5=uid("T5-7"), generation=987654321,
                   release=OTHER_RELEASE, capabilities=tuple(cap(f"C8_{x}", release=OTHER_RELEASE) for x in "ABCD"))
    target = build(state(present), focus(cf("C8", d("C8_A")))).target
    assert target.source_inference_run_id is present.active_inference_run_id
    assert target.source_longitudinal_assessment_run_id is present.longitudinal_assessment_run_id
    assert target.source_state_generation == 987654321
    assert target.source_taxonomy_release_id is present.taxonomy_release_id
    # state_generation est technique : sa magnitude ne change rien d'autre.
    small = build(state(dataclasses.replace(present, state_generation=1)), focus(cf("C8", d("C8_A")))).target
    assert dataclasses.replace(small, source_state_generation=987654321) == target


def test_68_no_uuid_is_generated(monkeypatch):
    c8_a = d("C8_A")
    st, fo = state(snap(ladder=C8_LADDER)), focus(cf("C8", c8_a), (cf("C9"),))

    def forbidden(*args, **kwargs):
        raise AssertionError("UUID généré")

    for name in ("uuid1", "uuid3", "uuid4", "uuid5"):
        monkeypatch.setattr(uuid, name, forbidden)
    baseline = build(st, fo)
    assert baseline.target.coverages[0].capability_definition_id is c8_a
    assert baseline.target.source_inference_run_id is st.competencies[0].active_inference_run_id


def test_69_absent_state_has_all_provenance_none():
    baseline = build(state(), focus(cf("C8", d("C8_A")), (cf("C2"), cf("C9", d("C9_A")))))
    assert all(_no_provenance(p) for p in (baseline.target, *baseline.supporting))


# --------------------------------------------------------------------------
# 12. Inputs forgés : fail closed (70-85 et compléments)
# --------------------------------------------------------------------------

GOOD_FOCUS = focus(cf("C8", d("C8_A")), (cf("C9"),))


def test_70_71_wrong_top_level_types():
    for bad_state in (None, {}, (), dataclasses.asdict(state())):
        with pytest.raises(InvalidAssumptionBaselineArgument):
            build(bad_state, GOOD_FOCUS)

    class SubState(AdaptationStateSnapshot):
        pass

    with pytest.raises(InvalidAssumptionBaselineArgument):
        build(SubState(user_id="u", competencies=()), GOOD_FOCUS)
    proposal = FocusProposal(schema_version=FOCUS_SCHEMA_VERSION, policy_version=FOCUS_POLICY_VERSION,
                             resolution_status="resolved", supporting=(),
                             target=ProposedCompetencyFocus(competency_code="C8", scope_mode="localized",
                                                            capability_tokens=("C8_A@r1",)))
    for bad_focus in (None, proposal, dataclasses.asdict(GOOD_FOCUS)):
        with pytest.raises(InvalidAssumptionBaselineArgument):
            build(state(), bad_focus)


@pytest.mark.parametrize("forged, match", [
    (dict(target=None), "sans cible"),                                                            # 72
    (dict(target=CompetencyFocus(competency_code="C8", scope_mode="localized",
                                 capability_definition_ids=())), "sans definition_id"),           # 73
    (dict(target=CompetencyFocus(competency_code="C8", scope_mode="competency_only",
                                 capability_definition_ids=(d("C8_A"),))), "competency_only avec"),  # 74
    (dict(supporting=(cf("C9"), cf("C9", d("C9_A")))), "en double"),                              # 75
    (dict(supporting=(cf("C10"), cf("C2"))), "ordre"),
    (dict(supporting=[cf("C9")]), "tuple"),
    (dict(target=cf("C8", d("C8_A"), d("C8_A"))), "en double"),
    (dict(target=cf("C8", str(d("C8_A")))), "uuid"),
    (dict(target=CompetencyFocus(competency_code="C8", scope_mode="localized",
                                 capability_definition_ids=[d("C8_A")])), "tuple"),
    (dict(target=CompetencyFocus(competency_code="C8", scope_mode="whole_competency",
                                 capability_definition_ids=())), "scope_mode"),
    (dict(target=cf("C13")), "competency_code"),
    (dict(target=cf("c8")), "competency_code"),
    (dict(target=ProposedCompetencyFocus(competency_code="C8", scope_mode="competency_only",
                                         capability_tokens=())), "CompetencyFocus"),
    (dict(schema_version="interaction-focus-v2"), "schema_version"),
    (dict(policy_version="focus-policy-2"), "policy_version"),
    (dict(taxonomy_release_id=str(RELEASE)), "taxonomy_release_id"),
    (dict(taxonomy_spec_fingerprint="  "), "fingerprint"),
    (dict(taxonomy_spec_fingerprint=None), "fingerprint"),
    (dict(resolution_status="partial"), "resolution_status"),
    (dict(resolution_status="neutral"), "ni cible"),
    (dict(resolution_status="ambiguous", target=None, supporting=(cf("C9"),)), "ni cible"),
])
def test_72_to_75_forged_focus_fails_closed(forged, match):
    with pytest.raises(InvalidAssumptionFocus, match=match):
        build(state(snap()), dataclasses.replace(GOOD_FOCUS, **forged))


def _forged_c8(**changes):
    return dataclasses.replace(snap(ladder=C8_LADDER), **changes)


def _claim_change(stage, **changes):
    base = snap(ladder=C8_LADDER)
    return dataclasses.replace(base, claims=tuple(dataclasses.replace(c, **changes) if c.stage == stage else c
                                                  for c in base.claims))


@pytest.mark.parametrize("forged, match", [
    (lambda: (snap(), snap()), "en double"),                                                       # 76
    (lambda: (_forged_c8(competency_code="c8"),), "hors de C1..C12"),                               # 77
    (lambda: (_forged_c8(competency_code="C13"),), "hors de C1..C12"),
    (lambda: (_forged_c8(capabilities=(cap("C8_A"), cap("C8_B", d("C8_A")))),), "en double"),       # 78
    (lambda: (_claim_change("comprehension", represented_capability_definition_ids=(uid("ghost"),)),),
     "hors des capacités"),                                                                         # 79
    (lambda: (_forged_c8(claims=(*snap().claims[:3], snap().claims[0])),), "exactement"),          # 80
    (lambda: (_forged_c8(claims=snap().claims[:3]),), "exactement"),                               # 81
    (lambda: (_forged_c8(claims=tuple(reversed(snap().claims))),), "exactement"),
    (lambda: (_claim_change("discovery", status="supported"),), "status"),                          # 82
    (lambda: (_claim_change("discovery", competency_only_basis=1),), "competency_only_basis"),     # 83
    (lambda: (_claim_change("discovery", competency_only_basis=None),), "competency_only_basis"),
    (lambda: (_forged_c8(active_inference_run_id=str(uid("run-C8"))),), "active_inference_run_id"),  # 84
    (lambda: (_forged_c8(longitudinal_assessment_run_id=None),), "longitudinal_assessment_run_id"),
    (lambda: (_forged_c8(taxonomy_release_id=str(RELEASE)),), "taxonomy_release_id"),
    (lambda: (_forged_c8(state_generation="1"),), "state_generation"),                              # 85
    (lambda: (_forged_c8(state_generation=True),), "state_generation"),
    (lambda: (_forged_c8(state_generation=0),), "state_generation"),
    (lambda: (_forged_c8(current_stage="expert"),), "current_stage"),
    (lambda: (_forged_c8(current_stage=None),), "current_stage"),
    (lambda: (_forged_c8(capabilities=list(snap().capabilities)),), "capabilities"),
    (lambda: (_forged_c8(capabilities=({"definition_id": d("C8_A")},)),), "capacité de type"),
    (lambda: (_forged_c8(capabilities=(cap("C8_A", str(d("C8_A"))),)),), "definition_id"),
    (lambda: (_forged_c8(claims=list(snap().claims)),), "claims"),
    (lambda: (_forged_c8(claims=(*snap().claims[:3], dataclasses.asdict(snap().claims[3]))),), "type"),
    (lambda: (_claim_change("discovery", represented_capability_definition_ids=[d("C8_A")]),), "tuple"),
    (lambda: (_claim_change("discovery", represented_capability_definition_ids=(str(d("C8_A")),)),), "uuid"),
    (lambda: (_claim_change("discovery", represented_capability_definition_ids=(d("C8_A"), d("C8_A"))),),
     "en double"),
    (lambda: ("forged",), "compétence de type"),
])
def test_76_to_85_forged_relevant_state_fails_closed(forged, match):
    with pytest.raises(InvalidAssumptionBaselineState, match=match):
        build(state(*forged()), GOOD_FOCUS)


def test_76_duplicate_snapshot_is_never_resolved_even_for_a_support():
    with pytest.raises(InvalidAssumptionBaselineState, match="C9 : snapshot en double"):
        build(state(snap(), snap("C9"), snap("C9")), GOOD_FOCUS)


def test_errors_never_produce_a_neutral_baseline():
    for call in (lambda: build(state(snap(), snap()), GOOD_FOCUS),
                 lambda: build(state(), dataclasses.replace(GOOD_FOCUS, target=None)),
                 lambda: build(None, GOOD_FOCUS)):
        with pytest.raises(AssumptionBaselineError):
            call()


# --------------------------------------------------------------------------
# 13. Déterminisme (86-91)
# --------------------------------------------------------------------------

def test_86_same_inputs_same_baseline():
    st = full_state()
    fo = focus(cf("C10", d("C10_A"), d("C10_B")), (cf("C2"), cf("C7", d("C7_A"))))
    reference = build(st, fo)
    assert all(build(st, fo) == reference for _ in range(5))
    shuffled = list(st.competencies)
    random.Random(61).shuffle(shuffled)
    assert build(state(*shuffled), fo) == reference  # ordre du snapshot sans effet


def test_87_to_90_no_clock_random_environment_or_database():
    imported = _imports()
    assert not {"time", "datetime", "random", "secrets", "os", "sys", "sqlalchemy", "core.db", "core.models",
                "threading", "asyncio"} & imported
    assert imported == {"uuid", "dataclasses", "core.adaptation_focus", "core.adaptation_state"}


def test_91_no_lexical_uuid_order():
    # definition_id dont l'ordre lexical est l'inverse de l'ordre du focus.
    ids = sorted((uid(f"lex-{i}") for i in range(4)), key=str, reverse=True)
    capabilities = tuple(cap(f"C8_{x}", i) for x, i in zip("ABCD", ids))
    st = state(snap(ladder={"discovery": (tuple(ids), False)}, capabilities=capabilities))
    target = build(st, focus(cf("C8", *ids))).target
    assert [c.capability_definition_id for c in target.coverages] == ids
    assert "sorted" not in _code_tokens(_source()).split()


# --------------------------------------------------------------------------
# 14. Constitution statique (92-109)
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
    """Attributs lus + chaînes littérales du code (getattr par nom compris)."""
    tree = _tree()
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= set(_code_tokens(_source()).split())
    return names


def test_imports_are_exactly_the_public_6_1a_and_6_1b1_boundaries():
    tree = _tree()
    by_module = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            by_module.setdefault(node.module, set()).update(a.name for a in node.names)
    assert by_module == {
        "dataclasses": {"dataclass"},
        "core.adaptation_focus": {"COMPETENCY_ONLY", "FOCUS_POLICY_VERSION", "FOCUS_SCHEMA_VERSION", "LOCALIZED",
                                  "RESOLUTION_STATUSES", "RESOLVED", "SCOPE_MODES", "CompetencyFocus",
                                  "InteractionCompetencyFocus"},
        "core.adaptation_state": {"COMPETENCY_ORDER", "AdaptationStageClaim", "AdaptationStateSnapshot",
                                  "CapabilitySemanticRef", "CompetencyAdaptationSnapshot"},
    }
    imported_names = set().union(*by_module.values())
    for private_or_reader in ("load_adaptation_state", "load_current_focus_taxonomy", "validate_focus_proposal",
                              "FocusProposal", "ProposedCompetencyFocus"):
        assert private_or_reader not in imported_names
    assert not any(name.startswith("_") for name in imported_names)


def test_92_to_96_no_sqlalchemy_models_services_or_session():
    imported = _imports()
    assert not {"sqlalchemy", "core.models", "core.db", "core.taxonomy_service", "core.observation_service",
                "core.inference_service", "core.longitudinal_service", "alembic", "psycopg2"} & imported
    names = _accessed_names()
    for forbidden in ("Session", "session", "db", "get_db", "execute", "query", "commit", "flush", "add_all",
                      "rollback", "no_autoflush", "select", "text", "merge", "delete", "refresh"):
        assert forbidden not in names, forbidden
    # Le seul .add est celui du set local des definition_id (jamais une session).
    adds = [n.func.value.id for n in ast.walk(_tree()) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "add"]
    assert adds == ["definitions"]
    tokens = _code_tokens(_source())
    for word in ("taxonomy_service", "observation_service", "inference_service", "sqlalchemy", "SessionLocal"):
        assert word not in tokens, word


def test_97_no_llm_provider_nor_prompt():
    assert not {"anthropic", "openai", "requests", "httpx", "urllib", "socket"} & _imports()
    tokens = _code_tokens(_source()).lower()
    for word in ("anthropic", "openai", "claude", "prompt", "backend", "classif", "model_call", "llm"):
        assert word not in tokens, word


def test_98_99_not_wired_to_api_nor_web_chat():
    assert "api" not in _imports()
    tokens = _code_tokens(_source())
    for word in ("web_chat", "web-chat", "_classify_intent", "fastapi", "request"):
        assert word not in tokens, word
    # Étape 6.1D : présupposés sûrs, consommateur du seul contrat public du
    # baseline (preflight canonique via build_assumption_baseline) ; non
    # branché : tests/test_adaptation_safety.py.
    # Étape 6.1E : contexte pédagogique, version du baseline, ordre des stades
    # et rôles seulement (jamais build_assumption_baseline) ; non branché :
    # tests/test_adaptation_context.py.
    step6_consumers = {"core/adaptation_safety.py": {
        "ASSUMPTION_BASELINE_SCHEMA_VERSION", "ASSUMPTION_STAGE_ORDER", "AssumptionBaseline",
        "AssumptionBaselineError", "CompetencyAssumptionBaseline", "build_assumption_baseline"},
        "core/adaptation_context.py": {
        "ASSUMPTION_BASELINE_SCHEMA_VERSION", "ASSUMPTION_STAGE_ORDER", "SUPPORTING", "TARGET"},
        # Étape 6.2C : calibration du support, ordre des stades et rôles
        # seulement (jamais un calcul sur les stades) ; non branché :
        # tests/test_adaptation_support.py.
        "core/adaptation_support.py": {"ASSUMPTION_STAGE_ORDER", "SUPPORTING", "TARGET"}}
    for rel, names in step6_consumers.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if "adaptation_assumptions" in f"{getattr(n, 'module', '')}.{a.name}"]
        assert sorted(imported) == sorted(("core.adaptation_assumptions", name) for name in names), rel
    users = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or {".git", "tests", "node_modules"} & set(path.relative_to(REPO_ROOT).parts)):
            continue
        if (rel != "core/adaptation_assumptions.py" and rel not in step6_consumers
                and "adaptation_assumptions" in path.read_text(encoding="utf-8", errors="replace")):
            users.append(rel)
    assert users == []
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for name in ("build_assumption_baseline", "AssumptionBaseline", "AssumptionCoverage",
                 "CompetencyAssumptionBaseline", "ASSUMPTION_BASELINE_SCHEMA_VERSION"):
        assert name not in api, name


def test_100_101_no_migration_no_db_model():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0013_decryptage_conversation_affinity.py" and len(versions) == 13
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    assert "Assumption" not in models and "assumption" not in models
    tokens = _code_tokens(_source())
    for word in ("__tablename__", "Column", "Base", "mapped_column", "relationship"):
        assert word not in tokens.split(), word


def test_102_103_no_support_trace_nor_final_safe_assumption():
    tokens = _code_tokens(_source()).lower()
    for word in ("actual_support_trace", "support_trace", "supporttrace", "safeassumption", "safe_assumption",
                 "pedagogicalresponsecontext", "posture", "validationopportunity", "next_useful_move", "explain",
                 "guide", "challenge", "clarify", "deepen", "generalize", "integrate", "surface"):
        assert word not in tokens, word


def test_104_105_106_no_tension_validation_need_nor_revision_context_logic():
    names = _accessed_names()
    for forbidden in ("tensions", "tension_state", "fragilized_stage", "revision_status", "validation_needs",
                      "unresolved_revision_context", "reason_codes", "intent", "target_stage"):
        assert forbidden not in names, forbidden
    tokens = _code_tokens(_source()).lower()
    for word in ("tension", "validation_need", "revision", "revalidation", "confirmation", "fragil"):
        assert word not in tokens, word


def test_107_108_no_score_no_confidence_threshold_no_mastery_reinterpretation():
    names = _accessed_names()
    for forbidden in ("confidence_profile", "mastery_assessment", "basis_mode", "label", "capability_code",
                      "semantic_revision", "membership_id", "mapping_guidance"):
        assert forbidden not in names, forbidden
    tokens = _code_tokens(_source()).lower()
    for word in ("score", "threshold", "confidence", "probab", "weight", "average", "percent", "highest",
                 "minimum", "maximum"):
        assert word not in tokens, word
    parts = {part for token in tokens.split() for part in token.split("_")}
    assert not {"mean", "ratio", "level", "max", "min", "sum", "count"} & parts
    assert not {"statistics", "math", "fractions", "decimal"} & _imports()


def test_109_no_user_id_in_output_dataclasses_nor_read():
    tree = _tree()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in ("AssumptionCoverage", "CompetencyAssumptionBaseline",
                                                             "AssumptionBaseline"):
            fields = [n.target.id for n in node.body if isinstance(n, ast.AnnAssign)]
            assert not any("user" in f for f in fields), node.name
    assert "user_id" not in _accessed_names()


def test_established_stages_ladder_is_never_replaced_by_a_single_stage():
    names = {f.name for cls in (AssumptionCoverage, CompetencyAssumptionBaseline, AssumptionBaseline)
             for f in dataclasses.fields(cls)}
    assert "established_stages" in names
    assert not {n for n in names if "highest" in n or n in ("stage", "current_stage", "level")}


# --------------------------------------------------------------------------
# 15. Intégration PostgreSQL : T3 / T4 / T5 / T6 réels -> 6-1A + 6-1B1 -> 6-1C
# --------------------------------------------------------------------------

UPSTREAM_TABLES = ("competency_inference_runs", "competency_stage_claims", "competency_inference_tensions",
                   "competency_inference_basis_refs", "user_competency_states", "longitudinal_assessment_runs",
                   "pedagogical_taxonomy_releases", "core_capability_definitions",
                   "capability_taxonomy_memberships", "observation_capabilities")


def _dump(engine):  # noqa: F811
    with engine.connect() as conn:
        return {table: sorted(map(repr, conn.execute(sa.text(f"SELECT * FROM {table}")).all()))
                for table in UPSTREAM_TABLES}


def _pipeline(Sessions, release_id):  # noqa: N803
    """Pipeline réel (tests/test_inference_engine.py) rattaché à une release
    existante et ACTIVE (au lieu d'une release de test créée par lui)."""
    from core import taxonomy_service as tax
    from tests.test_inference_engine import Pipeline
    p = Pipeline.__new__(Pipeline)
    p.Sessions, p.release_id = Sessions, release_id
    with Sessions() as session:
        p.m = {d_.capability_code: m.id for m, d_ in tax.get_release_capabilities(session, release_id=release_id)
               if d_.competency_code == "C7"}
    return p


def _v1(Sessions, activate=True):  # noqa: N803
    from core.pedagogy.taxonomy_bootstrap import activate_taxonomy_v1, bootstrap_taxonomy_v1
    with Sessions() as session:
        release_id = bootstrap_taxonomy_v1(session).release_id
        if activate:
            activate_taxonomy_v1(session)
        session.commit()
        return release_id


def _upstream(Sessions, *focuses, status="resolved"):  # noqa: N803
    """Chaîne amont réelle : 6-1A (lecture validée) + 6-1B1 (taxonomie
    courante + validation d'une proposition)."""
    with Sessions() as session:
        snapshot = load_adaptation_state(session, user_id="u")
        taxonomy = load_current_focus_taxonomy(session)
        assert not session.new and not session.dirty and not session.deleted
    proposal = FocusProposal(
        schema_version=FOCUS_SCHEMA_VERSION, policy_version=FOCUS_POLICY_VERSION, resolution_status=status,
        target=focuses[0] if focuses else None, supporting=tuple(focuses[1:]))
    return snapshot, taxonomy, validate_focus_proposal(proposal, taxonomy)


def _proposed(code, *tokens):
    return ProposedCompetencyFocus(competency_code=code, scope_mode="localized" if tokens else "competency_only",
                                   capability_tokens=tuple(tokens))


def _build_without_any_statement(engine, snapshot, validated):  # noqa: F811
    statements = []

    def record(conn, cursor, sql, *args):
        statements.append(sql)

    before = _dump(engine)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        baseline = build_assumption_baseline(state=snapshot, focus=validated)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements == []  # aucune lecture, aucune écriture pendant le build
    assert _dump(engine) == before
    return baseline


def _ids(taxonomy):
    return {c.capability_code: c.definition_id for c in taxonomy.capabilities}


def test_pg_real_upstream_chain_to_localized_baseline(Sessions, engine):  # noqa: F811
    """SPEC V1 active -> T3 (Application C7_A / C7_D, Comprehension C7_A /
    C7_B) -> T5 -> T6 -> 6-1A -> 6-1B1 -> 6-1C : definition_id exacts de
    PostgreSQL, provenance du run T6 réel, échelle = claims persistées (y
    compris un étage supérieur sans les inférieurs : aucun trou comblé)."""
    from tests.test_longitudinal_service import sup
    release = _v1(Sessions)
    p = _pipeline(Sessions, release)
    p.t3(p.app("C7_A", "C7_D", evidence_strength="strong"))
    p.t3(sup(p.m["C7_A"], p.m["C7_B"], evidence_strength="strong"))
    context, decision = p.infer()

    snapshot, taxonomy, validated = _upstream(
        Sessions, _proposed("C7", "C7_D@r1", "C7_A@r1", "C7_B@r1", "C7_C@r1"), _proposed("C2"))
    ids = _ids(taxonomy)
    (c7,) = snapshot.competencies
    assert validated.target.capability_definition_ids == (ids["C7_A"], ids["C7_B"], ids["C7_C"], ids["C7_D"])
    assert {c.definition_id for c in c7.capabilities} == {ids[f"C7_{x}"] for x in "ABCD"}

    baseline = _build_without_any_statement(engine, snapshot, validated)
    target = baseline.target
    assert (target.source_inference_run_id, target.source_longitudinal_assessment_run_id,
            target.source_state_generation, target.source_taxonomy_release_id) == (
        context.run_id, context.longitudinal_assessment_run_id, 1, release)
    allowed = ASSUMPTION_STAGE_ORDER[:ASSUMPTION_STAGE_ORDER.index(c7.current_stage) + 1]
    expected = [(ids[f"C7_{x}"], tuple(c.stage for c in c7.claims if c.status == "established"
                                       and c.stage in allowed
                                       and ids[f"C7_{x}"] in c.represented_capability_definition_ids))
                for x in "ABCD"]
    assert stages_of(target) == expected
    assert c7.current_stage == decision.current_stage == "application"
    assert stages_of(target) == [(ids["C7_A"], DCA), (ids["C7_B"], ("discovery", "comprehension")),
                                 (ids["C7_C"], ()), (ids["C7_D"], ("application",))]
    (c2,) = baseline.supporting
    assert c2.competency_code == "C2" and _no_provenance(c2) and stages_of(c2) == [(None, ())]
    assert build_assumption_baseline(state=snapshot, focus=validated) == baseline
    assert "user" not in repr(baseline)


def test_pg_real_competency_only_same_release(Sessions, engine):  # noqa: F811
    from tests.test_longitudinal_service import only
    release = _v1(Sessions)
    p = _pipeline(Sessions, release)
    p.t3(only(p.app(evidence_strength="strong")))
    p.infer()
    snapshot, taxonomy, validated = _upstream(Sessions, _proposed("C7"))
    (c7,) = snapshot.competencies
    assert c7.taxonomy_release_id == validated.taxonomy_release_id == release
    baseline = _build_without_any_statement(engine, snapshot, validated)
    assert stages_of(baseline.target) == [(None, DCA)]
    # competency_only_basis ne localise jamais : C7_A localized reste vide.
    _, _, localized = _upstream(Sessions, _proposed("C7", "C7_A@r1"))
    assert stages_of(build_assumption_baseline(state=snapshot, focus=localized).target) == [
        (_ids(taxonomy)["C7_A"], ())]


@pytest.mark.parametrize("status", ["neutral", "ambiguous", "composite"])
def test_pg_unresolved_focus_ignores_a_real_documented_state(Sessions, engine, status):  # noqa: F811
    p = _pipeline(Sessions, _v1(Sessions))
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    snapshot, _, validated = _upstream(Sessions, status=status)
    assert snapshot.competencies
    baseline = _build_without_any_statement(engine, snapshot, validated)
    assert (baseline.resolution_status, baseline.target, baseline.supporting) == (status, None, ())


def test_pg_cross_release_definition_identity(Sessions, engine):  # noqa: F811
    """Step 5 calculé sous une release R1 qui RÉUTILISE les definition_id V1
    de C7 ; une observation évaluée sous une release historique R0 est mappée
    à un C7_C d'une AUTRE définition (r2, même capability_code). V1 (R2) est
    ensuite activée et sert au focus.
    A. même definition_id dans deux releases -> localized compatible ;
    B. même capability_code, autre definition_id -> jamais remappé ;
    C. competency_only, release du snapshot != release du focus -> vide.
    (T6 n'accepte que semantic_revision 1 et UNIQUE(code, révision) interdit
    un second C7_C r1 : le cas B ne peut exister que du côté des preuves,
    jamais dans les capacités d'un snapshot T6 réel ; sa forme snapshot est
    couverte par les tests purs 25-27.)"""
    from core import taxonomy_service as tax
    from tests.test_longitudinal_service import only
    from tests.test_taxonomy_service import _def_kwargs
    v1 = _v1(Sessions, activate=False)
    with Sessions() as session:
        v1_ids = {d_.capability_code: d_.id for _, d_ in tax.get_release_capabilities(session, release_id=v1)}
        r0 = tax.create_candidate_release(session, version_key=f"6-1c-old-{uuid.uuid4()}",
                                          spec_fingerprint=f"sha256:{uuid.uuid4()}")
        c7_c_r2 = tax.create_capability_definition(session, **_def_kwargs("C7_C", 2)).id
        r0_m = {code: tax.attach_capability_to_release(session, release_id=r0.id, capability_definition_id=i).id
                for code, i in (("C7_A", v1_ids["C7_A"]), ("C7_B", v1_ids["C7_B"]), ("C7_C", c7_c_r2),
                                ("C7_D", v1_ids["C7_D"]))}
        r1 = tax.create_candidate_release(session, version_key=f"6-1c-{uuid.uuid4()}",
                                          spec_fingerprint=f"sha256:{uuid.uuid4()}")
        for code in ("C7_A", "C7_B", "C7_C", "C7_D"):
            tax.attach_capability_to_release(session, release_id=r1.id, capability_definition_id=v1_ids[code])
        tax.activate_release(session, release_id=r1.id)
        r0_id, r1_id = r0.id, r1.id
        session.commit()
    p = _pipeline(Sessions, r1_id)
    p.release_id, current_m, p.m = r0_id, p.m, r0_m  # observation évaluée sous R0 (C7_C = r2)
    p.t3(p.app("C7_A", "C7_B", "C7_C", evidence_strength="strong"))
    p.release_id, p.m = r1_id, current_m
    p.t3(only(p.app(evidence_strength="strong")))
    context, _ = p.infer()
    with Sessions() as session:
        tax.activate_release(session, release_id=v1)  # R1 -> retired, V1 active
        session.commit()

    snapshot, taxonomy, validated = _upstream(Sessions, _proposed("C7", "C7_A@r1", "C7_B@r1", "C7_C@r1"))
    (c7,) = snapshot.competencies
    ids = _ids(taxonomy)
    assert (c7.taxonomy_release_id, validated.taxonomy_release_id) == (r1_id, v1)
    assert {c.definition_id for c in c7.capabilities} == {v1_ids[f"C7_{x}"] for x in "ABCD"}
    assert ids["C7_C"] == v1_ids["C7_C"] != c7_c_r2
    application = c7.claims[2]
    assert application.status == "established" and application.competency_only_basis is True
    assert application.represented_capability_definition_ids == (ids["C7_A"], ids["C7_B"])

    baseline = _build_without_any_statement(engine, snapshot, validated)
    assert baseline.target.source_taxonomy_release_id == r1_id
    assert baseline.target.source_inference_run_id == context.run_id
    assert stages_of(baseline.target) == [(ids["C7_A"], DCA), (ids["C7_B"], DCA),  # A
                                          (ids["C7_C"], ())]                       # B
    _, _, global_focus = _upstream(Sessions, _proposed("C7"))
    assert global_focus.taxonomy_release_id != c7.taxonomy_release_id
    assert stages_of(build_assumption_baseline(state=snapshot, focus=global_focus).target) == [(None, ())]  # C
