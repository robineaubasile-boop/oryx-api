"""Tests de T6-C3 : TransitionCauseResolver (core/inference_transition.py).

Tests purs : T5-C réel (fonctions pures de core/longitudinal_view.py via le
builder de T6-C1) -> T6-C1 -> T6-C2 -> T6-C3 (infer_competency). Les
inférences successives sont chaînées comme T6-B le ferait : le predecessor
(PredecessorSnapshot + PredecessorDecisionContext) est reconstruit à partir
de l'InferenceDecision précédente (claims, tensions, refs, revision-context-v1
figé comme une relecture JSONB) ; la causalité de transition décrit les
faits (nouveaux événements, invalidations, réévaluations, versions).
Chaque décision est aussi soumise à la validation structurelle de T6-B
(_validated_decision, oracle de TEST seulement). Les scénarios bout en bout
contre PostgreSQL vivent dans tests/test_inference_engine.py.
"""
import dataclasses
import uuid

import pytest

from core import inference_service as svc
from core.inference_engine import infer_competency
from core.inference_final_policies import (
    TRANSITION_REASON_CODES,
    AmbiguousTransitionCause,
    FinalInferenceError,
    InconsistentInferenceAssessment,
    UnattributableTransitionCause,
)
from core.inference_positive_basis import evaluate_positive_basis
from core.inference_state import evaluate_inference_state
from core.inference_transition import TransitionCauseAssessment, resolve_transition_cause
from tests.test_inference_positive_basis import NS, RELEASE, STAGES, Dossier, definition_id

RUNS = [uuid.uuid5(NS, f"t6c3-run-{i}") for i in range(1, 9)]
NEW, INTEGRITY, REINTERPRETATION = "new_user_evidence", "evidence_integrity_change", "pedagogical_reinterpretation"


# --------------------------------------------------------------------------
# Chaînage réaliste (predecessor reconstruit depuis la décision précédente)
# --------------------------------------------------------------------------

def causality(new_events=(), integrity=(), reevaluated=None, *, taxonomy_changed=False, t5_changes=(),
              t6_changes=()):
    """TransitionCausalityContext : reevaluated = {ancienne observation:
    (observations de remplacement présentes dans le dossier courant)}."""
    empty = svc.RelationFamilyDeltaContext(added=(), removed=(), retained=())
    reevaluated = reevaluated or {}
    return svc.TransitionCausalityContext(
        causality_schema_version=1, new_user_event_ids=tuple(new_events),
        integrity_changes=tuple(svc.IntegrityChangeContext(
            observation_id=o, event_id=uuid.uuid5(NS, f"ev:{o}"), evaluation_run_id=uuid.uuid5(NS, f"t3:{o}"),
            capability_definition_ids=()) for o in integrity),
        reevaluations=tuple(svc.ReevaluationContext(
            event_id=uuid.uuid5(NS, f"ev:{o}"), previous_evaluation_run_id=uuid.uuid5(NS, f"t3:{o}"),
            replacement_evaluation_run_id=uuid.uuid5(NS, f"t3-bis:{o}"), replacement_re_evaluates_run_id=None,
            previous_observation_ids=(o,), current_observation_ids=tuple(current),
            previous_capability_definition_ids=(), current_capability_definition_ids=(),
            changed_evaluation_specification_fields=()) for o, current in reevaluated.items()),
        observation_delta=svc.ObservationDeltaContext(added_observation_ids=(), removed_observation_ids=(),
                                                      retained_observation_ids=(), added_event_ids=(),
                                                      removed_event_ids=(), retained_event_ids=()),
        relation_delta=svc.RelationDeltaContext(dependencies=empty, transfers=empty, revalidations=empty),
        taxonomy_release_changed=taxonomy_changed, previous_taxonomy_release_id=RELEASE,
        current_taxonomy_release_id=RELEASE,
        t5_version_changes=tuple(svc.VersionChangeContext(field_name=f, previous_value="a", current_value="b")
                                 for f in t5_changes),
        t6_specification_changes=tuple(svc.VersionChangeContext(field_name=f, previous_value="a", current_value="b")
                                       for f in t6_changes))


def clone(d):
    copy = Dossier(d.competency, release=d.release, clock=d.clock, revisions=d.revisions)
    copy.observations, copy.dependencies = list(d.observations), list(d.dependencies)
    copy.transfers, copy.revalidations, copy.events = list(d.transfers), list(d.revalidations), dict(d.events)
    return copy


def drop(d, *observation_ids):
    """Dossier courant sans ces observations (invalidation / réévaluation)."""
    copy = clone(d)
    copy.observations = [o for o in copy.observations if o.observation_id not in observation_ids]
    return copy


def _ref_contexts(refs):
    contexts = (svc.PredecessorBasisRefContext(ref_role=r.ref_role, confidence_dimension=r.confidence_dimension,
                                               source_kind=r.source_kind, source_id=r.source_id) for r in refs)
    return tuple(sorted(contexts, key=lambda r: (r.ref_role, r.confidence_dimension or "", r.source_kind,
                                                 str(r.source_id))))


def predecessor_contexts(run_id, d, decision, *, previous_stage=None, transition=None):
    """(PredecessorSnapshot, PredecessorDecisionContext) tels que T6-B les
    reconstruirait après la complétion de `decision` sur le dossier `d`."""
    dossier = d.dossier()
    memberships = {c.membership_id: c.definition_id for c in d.context().current_taxonomy_context.capabilities}
    snapshot = svc.PredecessorSnapshot(
        inference_run_id=run_id, longitudinal_assessment_run_id=dossier.run_id,
        current_stage=decision.current_stage, previous_stage=previous_stage, transition=transition,
        transition_cause=decision.transition_cause, tension_state="open" if decision.tensions else "none",
        unresolved_revision_context=None if decision.unresolved_revision_context is None
        else svc._freeze(decision.unresolved_revision_context),
        established_claim_stages=tuple(c.stage for c in decision.claims if c.positive_basis_status == "established"),
        input_fingerprint="3" * 64, output_fingerprint="4" * 64)
    context = svc.PredecessorDecisionContext(
        inference_run_id=run_id, longitudinal_assessment_run_id=dossier.run_id,
        pedagogical_taxonomy_release_id=RELEASE,
        historical_longitudinal_context=svc._historical_longitudinal_context(dossier),
        claims=tuple(svc.PredecessorStageClaimContext(
            stage=c.stage, positive_basis_status=c.positive_basis_status, basis_mode=c.basis_mode,
            basis_summary=c.basis_summary, scope_summary=c.scope_summary,
            confidence_profile=None if c.confidence_profile is None else svc._freeze(c.confidence_profile),
            mastery_assessment=None if c.mastery_assessment is None else svc._freeze(c.mastery_assessment),
            basis_refs=_ref_contexts(r for r in decision.basis_refs if r.claim_stage == c.stage))
            for c in decision.claims),
        tensions=tuple(svc.PredecessorTensionContext(
            fragilized_stage=t.fragilized_stage, scope_mode=t.scope_mode, summary=t.summary,
            revision_status=t.revision_status, capability_membership_ids=t.capability_membership_ids,
            capability_definition_ids=tuple(sorted((memberships[m] for m in t.capability_membership_ids), key=str)),
            basis_refs=_ref_contexts(r for r in decision.basis_refs if r.tension_key == t.tension_key))
            for t in decision.tensions),
        run_basis_refs=_ref_contexts(r for r in decision.basis_refs
                                     if r.claim_stage is None and r.tension_key is None),
        validation_needs=svc._freeze(decision.validation_needs),
        state_decision_summary=decision.state_decision_summary)
    return snapshot, context


def transition_of(previous, current):
    order = ("non_etabli", *STAGES)
    if previous is None:
        return None
    if previous == current:
        return "maintained"
    return "upgraded" if order.index(current) > order.index(previous) else "revised_down"


class DecisionChain:
    """Inférences T6-C complètes successives d'un même couple."""

    def __init__(self):
        self.previous = None  # (run_id, Dossier, InferenceDecision, previous_stage, transition)
        self.runs = iter(RUNS)

    def context(self, d, *, new=(), integrity=(), reevaluated=None, **facts):
        run_id = next(self.runs)
        if self.previous is None:
            return d.context(run_id=run_id)
        previous_run, previous_d, decision, previous_stage, transition = self.previous
        snapshot, context = predecessor_contexts(previous_run, previous_d, decision, previous_stage=previous_stage,
                                                 transition=transition)
        return d.context(run_id=run_id, predecessor=snapshot, predecessor_decision_context=context,
                         transition_causality=causality([d.events[e].id for e in new], integrity, reevaluated,
                                                        **facts))

    def step(self, d, **kwargs):
        context = self.context(d, **kwargs)
        decision = infer_competency(context)
        svc._validated_decision(decision)  # oracle structurel T6-B (test seulement)
        before = None if self.previous is None else self.previous[2].current_stage
        self.previous = (context.run_id, clone(d), decision, before, transition_of(before, decision.current_stage))
        return context, decision


def assessment(context):
    positive = evaluate_positive_basis(context)
    state = evaluate_inference_state(context, positive)
    return resolve_transition_cause(context, positive, state)


def refs(decision, role):
    return [r for r in decision.basis_refs if r.ref_role == role]


def transition_sources(decision):
    return {r.source_id for r in refs(decision, "transition")}


# --------------------------------------------------------------------------
# Structures
# --------------------------------------------------------------------------

def test_transition_cause_assessment_is_a_frozen_structure_with_a_closed_vocabulary():
    assert TransitionCauseAssessment.__dataclass_params__.frozen
    assert [f.name for f in dataclasses.fields(TransitionCauseAssessment)] == [
        "cause", "observation_ids", "structural_refs", "reason_codes"]
    assert all(f.kw_only for f in dataclasses.fields(TransitionCauseAssessment))
    assert len(set(TRANSITION_REASON_CODES)) == len(TRANSITION_REASON_CODES)
    for cls in (AmbiguousTransitionCause, UnattributableTransitionCause, InconsistentInferenceAssessment):
        assert issubclass(cls, FinalInferenceError)


# --------------------------------------------------------------------------
# 1-2 : première inférence
# --------------------------------------------------------------------------

def test_first_inference_application_has_no_cause():
    """1."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B")
    context = d.context()
    decision = infer_competency(context)
    assert (decision.current_stage, decision.transition_cause) == ("application", None)
    assert assessment(context) == TransitionCauseAssessment(cause=None, observation_ids=(), structural_refs=(),
                                                            reason_codes=("first_inference",))
    assert refs(decision, "transition") == []


@pytest.mark.parametrize("contradictions", [False, True])
def test_first_inference_non_etabli_has_no_cause(contradictions):
    """2."""
    d = Dossier("C1")
    if contradictions:
        d.contra("e1", "C1_A", scope="recognition")
    decision = infer_competency(d.context())
    assert (decision.current_stage, decision.transition_cause) == ("non_etabli", None)
    svc._validated_decision(decision)


# --------------------------------------------------------------------------
# 3-8 : maintien, progression, révisions
# --------------------------------------------------------------------------

def test_application_maintained_after_new_relevant_demonstration():
    """3."""
    chain, d = DecisionChain(), Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    chain.step(clone(d))
    n = d.observe("e2", "C2_A", "C2_B", name="n")
    context, decision = chain.step(d, new=["e2"])
    assert (decision.current_stage, decision.transition_cause) == ("application", NEW)
    assert assessment(context).reason_codes == ("new_user_evidence_supports_current_decision",
                                                "maintained_after_new_user_evidence")
    assert transition_sources(decision) == {n}


def test_upgrade_comprehension_to_application_by_new_positive_basis():
    """4."""
    chain, d = DecisionChain(), Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", stage="comprehension", name="c")
    assert chain.step(clone(d))[1].current_stage == "comprehension"
    n = d.observe("e2", "C2_A", "C2_B", name="n")
    context, decision = chain.step(d, new=["e2"])
    assert (decision.current_stage, decision.transition_cause) == ("application", NEW)
    assert assessment(context).reason_codes == ("new_user_evidence_supports_current_decision",)
    assert transition_sources(decision) == {n}


def _c5_base():
    d = Dossier("C5")
    d.observe("e0", "C5_A", stage="comprehension", name="c")
    d.observe("e1", "C5_B", "C5_C", name="a")
    return d


def test_downgrade_application_to_comprehension_by_new_user_contradiction():
    """5."""
    chain, d = DecisionChain(), _c5_base()
    assert chain.step(clone(d))[1].current_stage == "application"
    k = d.contra("e2", "C5_B", "C5_C", scope="recognition", name="k")
    context, decision = chain.step(d, new=["e2"])
    assert (decision.current_stage, decision.transition_cause) == ("comprehension", NEW)
    assert assessment(context).reason_codes == ("new_contradiction_drives_revision",)
    assert transition_sources(decision) == {k}


def test_application_to_non_etabli_after_invalidation_of_its_only_evidence():
    """6 : reset technique, aucune ref transition exigée ni citée."""
    chain, d = DecisionChain(), Dossier("C2")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    chain.step(d)
    context, decision = chain.step(drop(d, a), integrity=[a])
    assert (decision.current_stage, decision.transition_cause) == ("non_etabli", INTEGRITY)
    assert assessment(context).reason_codes == ("integrity_change_removed_previous_basis",)
    assert refs(decision, "transition") == []


def test_application_to_lower_stage_after_controlled_reevaluation():
    """7 : l'événement e1 est réinterprété (a -> a2, profondeur
    comprehension hors du motif de Comprehension C5)."""
    chain, d = DecisionChain(), _c5_base()
    a = next(o.observation_id for o in d.observations if o.observation_id == uuid.uuid5(NS, "observation:a"))
    chain.step(clone(d))
    current = drop(d, a)
    a2 = current.observe("e1", "C5_B", "C5_C", stage="comprehension", name="a2")
    context, decision = chain.step(current, reevaluated={a: (a2,)})
    assert (decision.current_stage, decision.transition_cause) == ("comprehension", REINTERPRETATION)
    assert assessment(context).reason_codes == ("reevaluation_changed_interpretation",)
    # Aucune source explicative présente : la base du stade retenu documente la révision.
    assert transition_sources(decision) == {uuid.uuid5(NS, "observation:c")}


def test_user_contradictions_never_reset_to_non_etabli():
    """8 : une contradiction utilisateur nouvelle ne remet jamais à zéro ;
    un reset n'est attribuable qu'à l'intégrité / la réinterprétation, et
    sans fait structurel => fail closed (jamais new_user_evidence)."""
    chain, d = DecisionChain(), Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    chain.step(clone(d))
    for i in range(3):
        d.contra(f"z{i}", "C2_A", "C2_B", scope="recognition", name=f"z{i}")
    _, decision = chain.step(d, new=["z0", "z1", "z2"])
    assert (decision.current_stage, decision.transition_cause) == ("application", NEW)
    # Reset après invalidation, même en présence d'une contradiction nouvelle.
    chain, d = DecisionChain(), Dossier("C2")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    chain.step(clone(d))
    d.contra("e2", "C2_A", "C2_B", scope="recognition", name="k")
    _, decision = chain.step(drop(d, a), new=["e2"], integrity=[a])
    assert (decision.current_stage, decision.transition_cause) == ("non_etabli", INTEGRITY)
    # Base sortie sans aucun fait structurel : seule une contradiction
    # nouvelle existe => jamais new_user_evidence, fail closed.
    chain, d = DecisionChain(), Dossier("C2")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    chain.step(clone(d))
    d.contra("e2", "C2_A", "C2_B", scope="recognition", name="k")
    with pytest.raises(UnattributableTransitionCause):
        infer_competency(chain.context(drop(d, a), new=["e2"]))


# --------------------------------------------------------------------------
# 9-11 : motifs de révision
# --------------------------------------------------------------------------

def revised(chain):
    """R1 Application (C8_B + C8_C) ; R2 contradiction diagnostique et
    représentative : revised_down => Comprehension, motif (application,
    C8_B + C8_C)."""
    d = Dossier("C8")
    a = d.observe("e1", "C8_B", "C8_C", name="a")
    chain.step(clone(d))
    k = d.contra("e2", "C8_B", "C8_C", scope="application", name="k")
    _, decision = chain.step(d, new=["e2"])
    assert (decision.current_stage, decision.transition_cause) == ("comprehension", NEW)
    assert decision.unresolved_revision_context["motifs"][0]["source_contradiction_observation_ids"] == [str(k)]
    return d, a, k


def test_motif_resolved_supportively_by_new_event_is_new_user_evidence():
    """9 : les refs transition portent l'anti-oscillation T6-B."""
    chain = DecisionChain()
    d, a, k = revised(chain)
    n = d.observe("e3", "C8_B", "C8_C", name="n")
    context, decision = chain.step(d, new=["e3"])
    assert (decision.current_stage, decision.transition_cause) == ("application", NEW)
    assert assessment(context).reason_codes == ("new_user_evidence_resolves_revision_motif",)
    assert transition_sources(decision) == {n}
    assert decision.unresolved_revision_context is None


def test_motif_superseded_by_integrity_change():
    """10."""
    chain = DecisionChain()
    d, a, k = revised(chain)
    context, decision = chain.step(drop(d, k), integrity=[k])
    assert (decision.current_stage, decision.transition_cause) == ("application", INTEGRITY)
    assert assessment(context).reason_codes == ("integrity_change_superseded_revision_motif",)
    assert transition_sources(decision) == {a}  # base du stade retenu (k est sortie du dossier)


def test_motif_superseded_by_reevaluation():
    """11."""
    chain = DecisionChain()
    d, a, k = revised(chain)
    context, decision = chain.step(drop(d, k), reevaluated={k: ()})
    assert (decision.current_stage, decision.transition_cause) == ("application", REINTERPRETATION)
    assert assessment(context).reason_codes == ("reevaluation_changed_interpretation",)


# --------------------------------------------------------------------------
# 12-15 : familles multiples, maintien réinterprété, fail closed
# --------------------------------------------------------------------------

def test_unrelated_new_event_does_not_mask_a_decisive_invalidation():
    """12 : l'événement nouveau (profondeur none) n'a aucun rapport avec la
    décision ; l'invalidation fait tomber Application."""
    chain, d = DecisionChain(), Dossier("C8")
    x = d.observe("e0", "C8_D", stage="discovery", name="x")
    a = d.observe("e1", "C8_B", "C8_C", name="a")
    chain.step(clone(d))
    u = d.observe("e3", "C8_A", stage="none", name="u")
    context, decision = chain.step(drop(d, a), new=["e3"], integrity=[a])
    assert (decision.current_stage, decision.transition_cause) == ("discovery", INTEGRITY)
    assert assessment(context).reason_codes == ("integrity_change_removed_previous_basis",)
    assert u not in transition_sources(decision) and x in transition_sources(decision)


def test_coexisting_families_resolve_to_the_one_that_explains_the_decision():
    """13 : une démonstration nouvelle porte le stade retenu, mais c'est
    l'invalidation qui explique la révision ; inversement une nouvelle base
    explique un maintien malgré une invalidation sans rapport."""
    chain, d = DecisionChain(), Dossier("C8")
    a = d.observe("e1", "C8_B", "C8_C", name="a")
    chain.step(clone(d))
    x = d.observe("e3", "C8_A", stage="discovery", name="x")
    context, decision = chain.step(drop(d, a), new=["e3"], integrity=[a])
    assert (decision.current_stage, decision.transition_cause) == ("discovery", INTEGRITY)
    assert transition_sources(decision) == {x}
    # Maintien : nouvelle base citée ; l'observation invalidée ne fondait rien.
    chain, d = DecisionChain(), Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    z = d.observe("e0", "C2_C", stage="none", name="z")
    chain.step(clone(d))
    n = d.observe("e2", "C2_A", "C2_B", name="n")
    context, decision = chain.step(drop(d, z), new=["e2"], integrity=[z])
    assert (decision.current_stage, decision.transition_cause) == ("application", NEW)
    assert transition_sources(decision) == {n}


def test_equally_explanatory_families_fail_closed():
    """13 bis : aucune priorité arbitraire. Maintien sans changement
    matériel, un événement nouveau sans rapport ET une invalidation sans
    rapport : les faits structurés ne départagent pas => fail closed."""
    chain, d = DecisionChain(), Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    z = d.observe("e0", "C2_C", stage="none", name="z")
    chain.step(clone(d))
    d.observe("e3", "C2_C", stage="none", name="u")
    with pytest.raises(AmbiguousTransitionCause):
        infer_competency(chain.context(drop(d, z), new=["e3"], integrity=[z]))


def test_maintained_after_controlled_reinterpretation():
    """14 : réévaluation de l'événement de base (même stade), puis simple
    changement de spécification T6 sur un dossier identique."""
    chain, d = DecisionChain(), Dossier("C2")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    chain.step(clone(d))
    current = drop(d, a)
    a2 = current.observe("e1", "C2_A", "C2_B", name="a2")
    context, decision = chain.step(current, reevaluated={a: (a2,)})
    assert (decision.current_stage, decision.transition_cause) == ("application", REINTERPRETATION)
    assert assessment(context).reason_codes == ("reevaluation_changed_interpretation",
                                                "maintained_after_controlled_reinterpretation")
    assert transition_sources(decision) == {a2}
    context, decision = chain.step(clone(current), t6_changes=["evaluator_version"])
    assert (decision.current_stage, decision.transition_cause) == ("application", REINTERPRETATION)
    assert assessment(context).reason_codes == ("t6_specification_change_changed_interpretation",
                                                "maintained_after_controlled_reinterpretation")
    assert refs(decision, "transition") == []


@pytest.mark.parametrize("fact", ["taxonomy_changed", "t5_changes"])
def test_global_reinterpretation_explains_a_stage_change_without_source(fact):
    """B2 : changement de stade sans source attribuable, release / versions
    T5 différentes => pedagogical_reinterpretation (jamais la preuve
    nouvelle sans rapport citée ailleurs)."""
    chain, d = DecisionChain(), Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", stage="comprehension", name="c")
    chain.step(clone(d))
    # Même observation, dossier réinterprété à profondeur application
    # (aucune réévaluation T3 : seulement une version / release).
    upgraded = Dossier("C2")
    upgraded.observe("e1", "C2_A", "C2_B", name="c")
    kwargs = {"taxonomy_changed": True} if fact == "taxonomy_changed" else {"t5_changes": ["dependency_version"]}
    context, decision = chain.step(upgraded, **kwargs)
    assert (decision.current_stage, decision.transition_cause) == ("application", REINTERPRETATION)
    code = {"taxonomy_changed": "taxonomy_change_changed_interpretation",
            "t5_changes": "t5_version_change_changed_interpretation"}[fact]
    assert assessment(context).reason_codes == (code,)
    assert transition_sources(decision) == {uuid.uuid5(NS, "observation:c")}


def test_predecessor_without_attributable_cause_fails_closed():
    """15."""
    chain, d = DecisionChain(), Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    chain.step(clone(d))
    with pytest.raises(UnattributableTransitionCause):
        infer_competency(chain.context(clone(d)))


def test_held_stage_coming_down_is_explained_by_the_new_defensible_claim():
    """Stade précédent déjà tenu sous tension : la révision s'explique par
    la claim inférieure devenue défendable (nouvelle démonstration)."""
    chain, d = DecisionChain(), Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    d.contra("e2", "C2_A", "C2_B", scope="recognition", name="k")
    assert chain.step(clone(d))[1].current_stage == "application"
    x = d.observe("e3", "C2_C", stage="discovery", name="x")
    _, decision = chain.step(d, new=["e3"])
    assert (decision.current_stage, decision.transition_cause) == ("discovery", NEW)
    assert transition_sources(decision) == {x}


def test_resolver_rejects_foreign_assessments():
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B")
    context = d.context()
    positive = evaluate_positive_basis(context)
    state = evaluate_inference_state(context, positive)
    with pytest.raises(InconsistentInferenceAssessment):
        resolve_transition_cause(context, state, positive)
    with pytest.raises(InconsistentInferenceAssessment):
        resolve_transition_cause(dataclasses.replace(context, competency_code="C3"), positive, state)


def test_every_new_user_evidence_cause_reaches_a_new_event_through_a_legal_ref():
    """12 (garde T6-B) : new_user_evidence cite toujours une source présente
    d'un événement nouveau ; jamais une observation absente du dossier."""
    chain = DecisionChain()
    d, a, k = revised(chain)
    n = d.observe("e3", "C8_B", "C8_C", name="n")
    context, decision = chain.step(d, new=["e3"])
    new_events = set(context.transition_causality.new_user_event_ids)
    events = {o.observation_id: o.event_id for o in context.longitudinal_dossier.active_history.observations}
    assert {events[r.source_id] for r in refs(decision, "transition")} <= new_events
    assert all(r.source_id in events for r in decision.basis_refs if r.source_kind == "observation")
    assert definition_id("C8_A") not in {m for t in decision.tensions for m in t.capability_membership_ids}
    assert n in transition_sources(decision)


def test_motif_superseded_by_mixed_integrity_and_reevaluation_fails_closed():
    """Supersession dont une source est invalidée et une autre seulement
    réévaluée : deux familles également explicatives au niveau A => aucune
    priorité arbitraire, fail closed."""
    chain, d = DecisionChain(), Dossier("C8")
    d.observe("e1", "C8_B", "C8_C", name="a")
    chain.step(clone(d))
    k1 = d.contra("e2", "C8_B", "C8_C", scope="application", name="k1")
    k2 = d.contra("e4", "C8_B", "C8_C", scope="application", name="k2")
    _, decision = chain.step(d, new=["e2", "e4"])
    assert decision.current_stage == "comprehension"
    assert decision.unresolved_revision_context["motifs"][0]["source_contradiction_observation_ids"] == sorted(
        [str(k1), str(k2)])
    with pytest.raises(AmbiguousTransitionCause):
        infer_competency(chain.context(drop(d, k1, k2), integrity=[k1], reevaluated={k2: ()}))
    # Chaque famille seule reste attribuable.
    context = chain.context(drop(d, k1, k2), integrity=[k1, k2])
    assert infer_competency(context).transition_cause == INTEGRITY
