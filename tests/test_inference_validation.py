"""Tests de T6-C3 : ValidationNeedEngine (core/inference_validation.py, 5.4)
et revision_status final des tensions.

Tests purs : T5-C réel (builder de T6-C1) -> T6-C1 -> T6-C2 -> T6-C3. Un
besoin de validation est LATENT : jamais une preuve, une claim, une
condition de stade, une tâche ni une question.
"""
import dataclasses
import uuid
from datetime import timedelta

import pytest

from core import inference_service as svc
from core.inference_engine import infer_competency
from core.inference_final_policies import (
    VALIDATION_REASON_CODES,
    VALIDATION_SCOPE_MODES,
    FinalInferenceError,
    InvalidValidationNeedScope,
    MissingValidationProvenance,
    UnresolvableCapabilityMembership,
    check_validation_scope,
    resolve_final_inference_policy,
)
from core.inference_policies import UnsupportedTaxonomySemantics
from core.inference_positive_basis import evaluate_positive_basis
from core.inference_state import evaluate_inference_state
from core.inference_validation import (
    ValidationNeedAssessment,
    _order_key,
    evaluate_validation_needs,
    tension_revision_status,
)
from tests.test_inference_positive_basis import (
    EPOCH,
    NS,
    RELEASE,
    STAGES,
    Dossier,
    _mastery_dossier,
    definition_id,
    taxonomy,
)
from tests.test_inference_state import _held
from tests.test_inference_transition import DecisionChain, causality, clone, revised

EXPECTED_KEYS = ["schema_version", "intent", "target_stage", "scope_mode", "capability_definition_ids",
                 "reason_codes"]


def needs_of(context):
    positive = evaluate_positive_basis(context)
    state = evaluate_inference_state(context, positive)
    return evaluate_validation_needs(context, positive, state)


def ids(*codes):
    return [str(definition_id(c)) for c in codes]


def tension(decision, stage):
    (item,) = [t for t in decision.tensions if t.fragilized_stage == stage]
    return item


def predecessor_with_motif(d, *, stage, motif_caps=(), sources, established=("discovery", "comprehension",
                                                                              "application"), new=(),
                           motif_definition_ids=None, scope_mode="localized"):
    """Contexte dont le predecessor porte un revision-context-v1 exact (motif
    choisi), pour tester le plus petit périmètre fragilisé. motif_definition_ids
    : definition_id historiques explicites (sinon révision 1 des codes)."""
    run = uuid.uuid5(NS, "t6c3-motif-origin")
    definitions = ids(*motif_caps) if motif_definition_ids is None else [str(i) for i in motif_definition_ids]
    payload = {"schema_version": "revision-context-v1", "origin_inference_run_id": str(run),
               "reason_code": "higher_claim_materially_fragilized_defensible_lower_claim_retained",
               "resolution_status": "unresolved",
               "motifs": [{"fragilized_stage": stage, "scope_mode": scope_mode,
                           "capability_definition_ids": definitions,
                           "source_contradiction_observation_ids": [str(s) for s in sorted(sources, key=str)],
                           "reason_codes": ["claim_representative_contradiction"]}]}
    dossier = d.dossier()
    snapshot = svc.PredecessorSnapshot(
        inference_run_id=run, longitudinal_assessment_run_id=dossier.run_id, current_stage=stage,
        previous_stage=None, transition=None, transition_cause=None, tension_state="open",
        unresolved_revision_context=svc._freeze(payload), established_claim_stages=established,
        input_fingerprint="3" * 64, output_fingerprint="4" * 64)
    decision = svc.PredecessorDecisionContext(
        inference_run_id=run, longitudinal_assessment_run_id=dossier.run_id, pedagogical_taxonomy_release_id=RELEASE,
        historical_longitudinal_context=svc._historical_longitudinal_context(dossier), claims=(), tensions=(),
        run_basis_refs=(), validation_needs=(), state_decision_summary="historique")
    return d.context(run_id=uuid.uuid5(NS, "t6c3-motif-run"), predecessor=snapshot,
                     predecessor_decision_context=decision,
                     transition_causality=causality([d.events[e].id for e in new]))


# --------------------------------------------------------------------------
# Structure et format
# --------------------------------------------------------------------------

def test_validation_need_structure_and_canonical_payload():
    assert ValidationNeedAssessment.__dataclass_params__.frozen
    assert [f.name for f in dataclasses.fields(ValidationNeedAssessment)] == [
        "schema_version", "intent", "target_stage", "scope_mode", "capability_definition_ids", "reason_codes",
        "observation_ids", "structural_refs"]
    assert issubclass(MissingValidationProvenance, FinalInferenceError)
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B")
    decision = infer_competency(d.context())
    (need,) = decision.validation_needs
    assert list(need) == EXPECTED_KEYS
    assert need == {"schema_version": "validation-need-v1", "intent": "confirmation", "target_stage": "application",
                    "scope_mode": "localized", "capability_definition_ids": ids("C2_A", "C2_B"),
                    "reason_codes": ["single_representative_episode_would_benefit_from_independent_context",
                                     "independence_not_established_on_current_basis"]}
    assert set(VALIDATION_REASON_CODES) >= set(need["reason_codes"])


# --------------------------------------------------------------------------
# 16-22 : revalidation et revision_status
# --------------------------------------------------------------------------

def test_current_stage_under_decisional_tension_needs_revalidation():
    """16 + 21."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    k = d.contra("e2", "C2_B", name="k")
    context = d.context()
    decision = infer_competency(context)
    assert decision.current_stage == "application"
    (need,) = [n for n in needs_of(context) if n.intent == "revalidation"]
    assert (need.target_stage, need.scope_mode, need.capability_definition_ids, need.reason_codes) == (
        "application", "localized", (definition_id("C2_B"),), ("current_stage_under_tension",))
    assert need.observation_ids == (k,)
    assert tension(decision, "application").revision_status == "revalidation_needed"
    assert "revalidation" in [n["intent"] for n in decision.validation_needs]


def _c8_motif(extra_c8a=False):
    d = Dossier("C8")
    d.observe("e1", "C8_B", "C8_C", name="a")
    k = d.contra("e2", "C8_C", scope="application", name="k")
    if extra_c8a:
        d.observe("e3", "C8_A", "C8_B", name="n")
    return d, k


def test_unresolved_revision_motif_needs_revalidation_of_its_exact_scope():
    """17 + 30 : motif C8_C => revalidation C8_C ; motif et tension du stade
    courant justifient le MÊME besoin : un seul, raisons réunies."""
    d, k = _c8_motif()
    context = predecessor_with_motif(d, stage="application", motif_caps=["C8_C"], sources=[k], new=["e2"])
    decision = infer_competency(context)
    assert decision.current_stage == "application"
    (need,) = needs_of(context)
    assert (need.intent, need.target_stage, need.scope_mode, need.capability_definition_ids) == (
        "revalidation", "application", "localized", (definition_id("C8_C"),))
    assert need.reason_codes == ("unresolved_revision_motif", "current_stage_under_tension")
    assert decision.validation_needs == ({"schema_version": "validation-need-v1", "intent": "revalidation",
                                          "target_stage": "application", "scope_mode": "localized",
                                          "capability_definition_ids": ids("C8_C"),
                                          "reason_codes": ["unresolved_revision_motif",
                                                           "current_stage_under_tension"]},)
    assert tension(decision, "application").revision_status == "revalidation_needed"


def test_motif_on_c8c_never_becomes_a_need_on_c8a_or_the_whole_competency():
    """18."""
    d, k = _c8_motif(extra_c8a=True)
    context = predecessor_with_motif(d, stage="application", motif_caps=["C8_C"], sources=[k], new=["e2", "e3"])
    needs = needs_of(context)
    assert [n.capability_definition_ids for n in needs if n.intent == "revalidation"] == [(definition_id("C8_C"),)]
    assert all(definition_id("C8_A") not in n.capability_definition_ids for n in needs)
    assert all(n.scope_mode != "whole_competency" for n in needs)


def test_mastery_under_tension_revalidates_only_the_fragilized_mechanism():
    """19 : jamais « revalider la Mastery » globalement."""
    d, *_ = _mastery_dossier()
    k = d.contra("k", "C2_B", name="k")
    context = d.context()
    decision = infer_competency(context)
    assert decision.current_stage == "mastery"
    (need,) = needs_of(context)
    assert (need.intent, need.target_stage, need.scope_mode, need.capability_definition_ids, need.observation_ids) == (
        "revalidation", "mastery", "localized", (definition_id("C2_B"),), (k,))
    assert not [n for n in decision.validation_needs if n["capability_definition_ids"] == []]
    assert tension(decision, "mastery").revision_status == "revalidation_needed"


def test_peripheral_tension_creates_no_revalidation_need():
    """20 + 22 : tension visible sur Discovery (C8_D), sans intérêt
    décisionnel pour le stade Application retenu."""
    d = Dossier("C8")
    d.observe("e0", "C8_D", stage="discovery", name="x")
    d.observe("e1", "C8_B", "C8_C", name="a")
    d.contra("e2", "C8_D", scope="recognition", name="k")
    decision = infer_competency(d.context())
    assert decision.current_stage == "application"
    assert [t.fragilized_stage for t in decision.tensions] == ["discovery"]
    assert tension(decision, "discovery").revision_status == "unresolved"
    assert [n["intent"] for n in decision.validation_needs] == ["confirmation"]


def test_materially_incompatible_higher_claim_needs_revalidation():
    """Tension materially_incompatible qui a empêché Application d'être le
    current_stage (Comprehension défendable retenue)."""
    d = Dossier("C5")
    d.observe("e0", "C5_A", stage="comprehension", name="c")
    d.observe("e1", "C5_B", "C5_C", name="a")
    k = d.contra("e2", "C5_B", "C5_C", scope="recognition", name="k")
    context = d.context()
    decision = infer_competency(context)
    assert decision.current_stage == "comprehension"
    revalidations = [n for n in needs_of(context) if n.intent == "revalidation"]
    assert [(n.target_stage, n.capability_definition_ids, n.reason_codes) for n in revalidations] == [
        ("application", (definition_id("C5_B"), definition_id("C5_C")),
         ("unresolved_revision_motif", "materially_incompatible_higher_claim"))]
    assert revalidations[0].observation_ids == (k,)
    assert tension(decision, "application").revision_status == "revalidation_needed"


# --------------------------------------------------------------------------
# 23-28 : confirmation
# --------------------------------------------------------------------------

def test_single_representative_application_can_receive_a_confirmation_without_changing_anything():
    """23 + 24 : la confirmation ne change ni le stade ni les claims."""
    d = Dossier("C2")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    context = d.context()
    decision = infer_competency(context)
    (need,) = needs_of(context)
    assert (need.intent, need.target_stage) == ("confirmation", "application")
    assert need.observation_ids == (a,)
    positive = evaluate_positive_basis(context)
    assert [(c.stage, c.positive_basis_status, c.basis_mode) for c in decision.claims] == [
        (c.stage, c.status, c.basis_mode) for c in positive.claims]
    assert decision.current_stage == evaluate_inference_state(context, positive).current_stage == "application"
    assert [c.positive_basis_status for c in decision.claims] == ["established"] * 3 + ["not_established"]


def test_confirmation_targets_only_the_current_stage():
    d = Dossier("C2")
    d.observe("e0", "C2_C", stage="discovery", name="x")
    d.observe("e1", "C2_A", "C2_B", name="a")
    needs = needs_of(d.context())
    assert {n.target_stage for n in needs} == {"application"}


def test_competency_only_basis_would_benefit_from_localization():
    d = Dossier("C10")
    d.observe("e1", name="a")
    (need,) = needs_of(d.context())
    assert (need.intent, need.scope_mode, need.capability_definition_ids) == ("confirmation", "competency_only", ())
    assert "competency_only_basis_would_benefit_from_localization" in need.reason_codes


def test_unobserved_capability_alone_creates_no_confirmation():
    """25 : base unique mais indépendance établie par T5 (revalidation) ;
    C2_C jamais observée => aucun besoin."""
    d = Dossier("C2")
    k = d.contra("e1", "C2_A", "C2_B", name="k")
    b = d.observe("e2", "C2_A", "C2_B", name="b")
    d.revalidation(k, b)
    context = d.context()
    state = evaluate_inference_state(context, evaluate_positive_basis(context))
    assert "unobserved_capabilities_present" in state.confidence_profiles[2].coverage.fact_codes
    assert state.current_stage == "application"
    assert needs_of(context) == ()
    assert infer_competency(context).validation_needs == ()


def test_mastery_without_tension_needs_no_automatic_confirmation():
    """26."""
    d, *_ = _mastery_dossier()
    decision = infer_competency(d.context())
    assert decision.current_stage == "mastery"
    assert decision.validation_needs == ()


def test_time_alone_creates_no_validation_need():
    """27 : ni l'âge des preuves ni le temps qui passe."""
    def build(clock):
        d, *_ = _mastery_dossier()
        d.clock = clock
        again = Dossier("C2", clock=clock)
        again.observations, again.transfers, again.events = d.observations, d.transfers, d.events
        return infer_competency(again.context())

    assert build(EPOCH) == build(EPOCH + timedelta(days=4000))
    assert build(EPOCH + timedelta(days=4000)).validation_needs == ()
    chain, d = DecisionChain(), Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    first = chain.step(clone(d))[1]
    later = clone(d)
    later.clock = EPOCH + timedelta(days=4000)
    second = chain.step(later, t6_changes=["evaluator_version"])[1]
    assert second.validation_needs == first.validation_needs


@pytest.mark.parametrize("contradictions", [False, True])
def test_first_inference_non_etabli_has_no_validation_need(contradictions):
    """28."""
    d = Dossier("C1")
    if contradictions:
        d.contra("e1", "C1_A", scope="recognition")
    decision = infer_competency(d.context())
    assert (decision.current_stage, decision.validation_needs) == ("non_etabli", ())
    assert not [r for r in decision.basis_refs if r.ref_role == "validation"]


# --------------------------------------------------------------------------
# 29-30 : persistance par inertie, déduplication
# --------------------------------------------------------------------------

def test_naturally_resolved_motif_does_not_keep_its_old_need():
    """29."""
    chain = DecisionChain()
    d, a, k = revised(chain)
    before = chain.previous[2]
    motif_caps = ids("C8_B", "C8_C")
    assert [n["reason_codes"] for n in before.validation_needs if n["intent"] == "revalidation"
            and (n["target_stage"], n["capability_definition_ids"]) == ("application", motif_caps)] == [
        ["unresolved_revision_motif", "materially_incompatible_higher_claim"]]
    d.observe("e3", "C8_B", "C8_C", name="n")
    _, decision = chain.step(d, new=["e3"])
    assert decision.current_stage == "application"
    assert not [n for n in decision.validation_needs if n["intent"] == "revalidation"
                and "unresolved_revision_motif" in n["reason_codes"]]


def test_two_sources_of_the_same_need_yield_one_canonical_need():
    """30 : motif + tension du stade tenu (même identité)."""
    d, k = _held()
    context = d.context()
    needs = needs_of(context)
    (need,) = needs
    assert need.reason_codes == ("unresolved_revision_motif", "current_stage_under_tension")
    assert need.observation_ids == (k,)
    decision = infer_competency(context)
    validation_refs = [r for r in decision.basis_refs if r.ref_role == "validation"]
    assert [(r.source_kind, r.source_id) for r in validation_refs] == [("observation", k)]


def test_needs_are_in_canonical_order_and_never_duplicated():
    d = Dossier("C5")
    d.observe("e0", "C5_A", stage="comprehension", name="c")
    d.observe("e1", "C5_B", "C5_C", name="a")
    d.contra("e2", "C5_B", "C5_C", scope="recognition", name="k")
    needs = needs_of(d.context())
    identities = [(n.intent, n.target_stage, n.scope_mode, n.capability_definition_ids) for n in needs]
    assert len(set(identities)) == len(identities)
    order = [(("confirmation", "revalidation").index(i), STAGES.index(s)) for i, s, *_ in identities]
    assert order == sorted(order)


def test_need_without_any_current_provenance_fails_closed():
    """Motif dont les sources ont quitté le dossier sans fait structuré :
    aucune source n'est fabriquée."""
    d, k = _c8_motif()
    gone = uuid.uuid5(NS, "observation:disparue")
    context = predecessor_with_motif(d, stage="application", motif_caps=["C8_B"], sources=[gone], new=["e2"])
    with pytest.raises(MissingValidationProvenance):
        needs_of(context)


def test_tension_revision_status_requires_exact_semantic_coverage():
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    d.contra("e2", "C2_B", name="k")
    context = d.context()
    state = evaluate_inference_state(context, evaluate_positive_basis(context))
    (item,) = state.tensions
    need = ValidationNeedAssessment(schema_version="validation-need-v1", intent="revalidation",
                                    target_stage="application", scope_mode="localized",
                                    capability_definition_ids=(definition_id("C2_B"),), reason_codes=(),
                                    observation_ids=(), structural_refs=())
    assert tension_revision_status(item, (need,)) == "revalidation_needed"
    for other in (dataclasses.replace(need, capability_definition_ids=(definition_id("C2_A"),)),
                  dataclasses.replace(need, target_stage="mastery"),
                  dataclasses.replace(need, scope_mode="competency_only"),
                  dataclasses.replace(need, intent="confirmation")):
        assert tension_revision_status(item, (other,)) == "unresolved"


# --------------------------------------------------------------------------
# Scopes des besoins : trois modes T6-B, invariants, taxonomie courante
# --------------------------------------------------------------------------

def test_validation_scope_vocabulary_is_exactly_the_t6b_tension_vocabulary():
    assert VALIDATION_SCOPE_MODES == ("localized", "competency_only", "whole_competency")
    assert frozenset(VALIDATION_SCOPE_MODES) == svc.TENSION_SCOPE_MODES


@pytest.mark.parametrize("scope_mode, caps, accepted", [
    ("localized", ("C8_C",), True),
    ("localized", (), False),
    ("competency_only", (), True),
    ("competency_only", ("C8_C",), False),
    ("whole_competency", (), True),
    ("whole_competency", ("C8_C",), False),
])
def test_validation_need_scope_invariants(scope_mode, caps, accepted):
    current = taxonomy("C8")
    definitions = tuple(definition_id(c) for c in caps)
    if accepted:
        check_validation_scope(scope_mode, definitions, current)
    else:
        with pytest.raises(InvalidValidationNeedScope):
            check_validation_scope(scope_mode, definitions, current)


def test_unknown_scope_mode_fails_closed_with_a_business_error_not_a_value_error():
    with pytest.raises(InvalidValidationNeedScope):
        check_validation_scope("whole_observation", (), taxonomy("C8"))
    # L'ordre canonique connaît les trois modes (aucun ValueError).
    d, _ = _c8_motif()
    key = _order_key(resolve_final_inference_policy(d.context()))
    needs = [ValidationNeedAssessment(schema_version="validation-need-v1", intent="revalidation",
                                      target_stage="application", scope_mode=mode,
                                      capability_definition_ids=caps, reason_codes=(), observation_ids=(),
                                      structural_refs=())
             for mode, caps in (("whole_competency", ()), ("competency_only", ()),
                                ("localized", (definition_id("C8_C"),)))]
    assert [n.scope_mode for n in sorted(needs, key=key)] == ["localized", "competency_only", "whole_competency"]


def test_confirmations_are_never_whole_competency():
    single = Dossier("C2")
    single.observe("e1", "C2_A", "C2_B")
    only = Dossier("C10")
    only.observe("e1", name="a")
    scopes = {n.scope_mode for d in (single, only) for n in needs_of(d.context()) if n.intent == "confirmation"}
    assert scopes == {"localized", "competency_only"}


def test_localized_need_on_a_definition_absent_from_the_current_taxonomy_fails_closed():
    """Motif historique localized sur C8_C révision 2 (autre definition_id,
    même capability_code) ; la taxonomie courante porte C8_C révision 1.
    T6-C2 conserve le motif (fait historique) ; T6-C3 ne le remappe jamais
    par code et ne persiste jamais un besoin vers une définition absente."""
    d, k = _c8_motif()
    other = definition_id("C8_C", 2)
    context = predecessor_with_motif(d, stage="application", motif_definition_ids=[other], sources=[k],
                                     new=["e2"])
    assert other not in {c.definition_id for c in context.current_taxonomy_context.capabilities}
    state = evaluate_inference_state(context, evaluate_positive_basis(context))
    assert [m.capability_definition_ids for m in state.revision_context.motifs] == [(other,)]
    with pytest.raises(UnresolvableCapabilityMembership):
        needs_of(context)
    with pytest.raises(UnresolvableCapabilityMembership):
        infer_competency(context)


def test_current_taxonomy_with_a_new_semantic_revision_is_never_remapped():
    """Taxonomie courante : C8_C en révision 2 (nouvelle définition) ; motif
    ancien sur C8_C révision 1. Aucun remapping : positive_basis-1 échoue
    fermé avant toute sérialisation (UnsupportedTaxonomySemantics)."""
    d = Dossier("C8", revisions={"C8_C": 2})
    d.observe("e1", "C8_B", "C8_C", name="a")
    k = d.contra("e2", "C8_C", scope="application", name="k")
    context = predecessor_with_motif(d, stage="application", motif_definition_ids=[definition_id("C8_C", 1)],
                                     sources=[k], new=["e2"])
    assert {(c.capability_code, c.semantic_revision) for c in context.current_taxonomy_context.capabilities
            if c.capability_code == "C8_C"} == {("C8_C", 2)}
    with pytest.raises(UnsupportedTaxonomySemantics):
        infer_competency(context)


def test_other_release_reusing_the_exact_definition_is_compatible():
    """Release courante différente, MÊME definition_id C8_C (révision 1) :
    même sens, besoin localized conservé ; membership de la release
    courante pour la tension."""
    other_release = uuid.uuid5(NS, "release:oryx-v1-bis")
    d = Dossier("C8", release=other_release)
    d.observe("e1", "C8_B", "C8_C", name="a")
    k = d.contra("e2", "C8_C", scope="application", name="k")
    context = predecessor_with_motif(d, stage="application", motif_caps=["C8_C"], sources=[k], new=["e2"])
    assert context.current_taxonomy_context.release_id == other_release != RELEASE
    decision = infer_competency(context)
    (need,) = [n for n in decision.validation_needs if n["intent"] == "revalidation"]
    assert (need["scope_mode"], need["capability_definition_ids"]) == ("localized", ids("C8_C"))
    (item,) = [t for t in decision.tensions if t.fragilized_stage == "application"]
    current = {c.capability_code: c.membership_id for c in context.current_taxonomy_context.capabilities}
    assert item.capability_membership_ids == (current["C8_C"],)
    assert item.revision_status == "revalidation_needed"
    svc._validated_decision(decision)


def test_historical_whole_competency_motif_is_revalidated_without_scope_conversion():
    """revision-context-v1 historique : motif application / whole_competency
    / aucune capacité, contradiction K toujours courante et pertinente. Le
    besoin garde EXACTEMENT ce scope (jamais competency_only ni localized),
    aucun membership n'est inventé."""
    d, k = _c8_motif()
    context = predecessor_with_motif(d, stage="application", scope_mode="whole_competency",
                                     motif_definition_ids=[], sources=[k], new=["e2"])
    motif = context.predecessor.unresolved_revision_context["motifs"][0]
    assert (motif["scope_mode"], tuple(motif["capability_definition_ids"])) == ("whole_competency", ())
    decision = infer_competency(context)
    motif_needs = [n for n in decision.validation_needs if "unresolved_revision_motif" in n["reason_codes"]]
    assert [{key: n[key] for key in ("intent", "target_stage", "scope_mode", "capability_definition_ids")}
            for n in motif_needs] == [{"intent": "revalidation", "target_stage": "application",
                                       "scope_mode": "whole_competency", "capability_definition_ids": []}]
    # Aucune conversion : jamais competency_only ; le seul besoin localized
    # est celui, distinct, de la tension COURANTE sur C8_C (Application reste
    # établie), jamais une localisation du motif.
    assert not [n for n in decision.validation_needs if n["scope_mode"] == "competency_only"]
    assert [(n["scope_mode"], n["capability_definition_ids"], n["reason_codes"]) for n in decision.validation_needs
            if n["scope_mode"] == "localized"] == [("localized", ids("C8_C"), ["current_stage_under_tension"])]
    assert decision.unresolved_revision_context["motifs"][0]["scope_mode"] == "whole_competency"
    for tension in decision.tensions:
        if tension.scope_mode == "whole_competency":
            assert tension.capability_membership_ids == ()
            assert tension.revision_status == "revalidation_needed"
    svc._validated_decision(decision)


def test_historical_whole_competency_tension_is_preserved_and_covered_by_its_revalidation():
    """Claim Application établie par le predecessor, non établie aujourd'hui
    (seule Comprehension) ; motif whole_competency non résolu, K ouverte :
    tension historique whole_competency sans capacité ni membership,
    couverte par le besoin de revalidation du motif."""
    d = Dossier("C8")
    d.observe("e1", "C8_A", "C8_B", stage="comprehension", name="c")
    k = d.contra("e2", "C8_C", scope="application", name="k")
    context = predecessor_with_motif(d, stage="application", scope_mode="whole_competency",
                                     motif_definition_ids=[], sources=[k], new=["e1", "e2"])
    decision = infer_competency(context)
    assert decision.current_stage == "comprehension"
    (tension,) = decision.tensions
    assert (tension.fragilized_stage, tension.scope_mode, tension.capability_membership_ids,
            tension.revision_status) == ("application", "whole_competency", (), "revalidation_needed")
    assert [(r.source_kind, r.source_id) for r in decision.basis_refs if r.tension_key == tension.tension_key] == [
        ("observation", k)]
    (need,) = [n for n in decision.validation_needs if n["intent"] == "revalidation"]
    assert (need["target_stage"], need["scope_mode"], need["capability_definition_ids"], need["reason_codes"]) == (
        "application", "whole_competency", [], ["unresolved_revision_motif"])
    assert decision.unresolved_revision_context["motifs"][0]["scope_mode"] == "whole_competency"
    svc._validated_decision(decision)
