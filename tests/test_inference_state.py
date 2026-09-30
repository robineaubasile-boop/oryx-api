"""Tests de T6-C2 : Tension / Compatibility / State engine
(core/inference_state.py, core/inference_state_policies.py).

1. Tests purs (toujours exécutés) : T5-C réel (fonctions pures de
   core/longitudinal_view.py via le builder de T6-C1) -> T6-C1 réel
   (evaluate_positive_basis) -> T6-C2 réel (evaluate_inference_state). Les
   inférences successives sont chaînées comme T6-B le ferait : le
   predecessor porte le stade et le revision-context-v1 (to_payload, figé
   comme une relecture JSONB) de l'inférence précédente ; la causalité de
   transition décrit les faits (nouveaux événements, invalidations,
   réévaluations).

2. Un test d'intégration contre un vrai PostgreSQL (mêmes conditions que
   T1-T6-B, sinon SKIPPÉ) : deux inférences réelles chaînées par T6-B,
   évaluées par T6-C1 puis T6-C2, décisions construites par un adaptateur de
   TEST (T6-C2 ne construit aucune décision) et acceptées par
   complete_competency_inference.
"""
import ast
import dataclasses
import inspect
import re
import uuid
from datetime import timedelta

import pytest

from core import inference_service as svc
from core.inference_confidence import sorted_ids
from core.inference_policies import PositiveBasisError, UnsupportedInferenceContext, UnsupportedPositiveBasisPolicy
from core.inference_positive_basis import evaluate_positive_basis
from core.inference_service import BasisRefDecision, InferenceDecision, StageClaimDecision, TensionDecision
from core.inference_state import (
    AffectedScope,
    ClaimCompatibilityAssessment,
    RevisionContextAssessment,
    RevisionMotif,
    RevisionMotifResolution,
    T6C2Assessment,
    TensionAssessment,
    evaluate_inference_state,
)
from core.inference_state_policies import (
    CONTRADICTION_SCOPES,
    STATE_DECISION_POLICIES,
    STATE_DECISION_V1_POLICY,
    STATE_REASON_CODES,
    InconsistentPositiveBasis,
    InferenceStateError,
    InvalidInferenceStatePolicy,
    T6C2PolicyResolver,
    UnsupportedConfidenceProfilePolicy,
    UnsupportedRevisionContext,
    UnsupportedStateDecisionPolicy,
    validate_state_decision_policy,
)
from tests.test_inference_confidence import evaluate
from tests.test_inference_positive_basis import (
    EPOCH,
    NS,
    RELEASE,
    STAGES,
    VERSIONS,
    Dossier,
    _mastery_dossier,
    competency_codes,
    definition_id,
    taxonomy,
)
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens

STATE_PATH = REPO_ROOT / "core" / "inference_state.py"
STATE_POLICIES_PATH = REPO_ROOT / "core" / "inference_state_policies.py"
CONFIDENCE_PATH = REPO_ROOT / "core" / "inference_confidence.py"
T6C2_PATHS = (STATE_PATH, STATE_POLICIES_PATH, CONFIDENCE_PATH)
RUNS = [uuid.uuid5(NS, f"t6c2-run-{i}") for i in range(1, 5)]


# --------------------------------------------------------------------------
# Chaînage des inférences (predecessor historique + causalité)
# --------------------------------------------------------------------------

def compat(result, stage):
    return result.claim_compatibilities[STAGES.index(stage)]


def tensions_on(result, stage):
    return [t for t in result.tensions if t.fragilized_stage == stage]


def _causality(new_events=(), integrity=(), reevaluated=()):
    empty = svc.RelationFamilyDeltaContext(added=(), removed=(), retained=())
    return svc.TransitionCausalityContext(
        causality_schema_version=1, new_user_event_ids=tuple(new_events),
        integrity_changes=tuple(svc.IntegrityChangeContext(observation_id=o, event_id=uuid.uuid5(NS, f"ev:{o}"),
                                                           evaluation_run_id=uuid.uuid5(NS, f"t3:{o}"),
                                                           capability_definition_ids=()) for o in integrity),
        reevaluations=tuple(svc.ReevaluationContext(
            event_id=uuid.uuid5(NS, f"ev:{o}"), previous_evaluation_run_id=uuid.uuid5(NS, f"t3:{o}"),
            replacement_evaluation_run_id=uuid.uuid5(NS, f"t3-bis:{o}"), replacement_re_evaluates_run_id=None,
            previous_observation_ids=(o,), current_observation_ids=(), previous_capability_definition_ids=(),
            current_capability_definition_ids=(), changed_evaluation_specification_fields=()) for o in reevaluated),
        observation_delta=svc.ObservationDeltaContext(added_observation_ids=(), removed_observation_ids=(),
                                                      retained_observation_ids=(), added_event_ids=(),
                                                      removed_event_ids=(), retained_event_ids=()),
        relation_delta=svc.RelationDeltaContext(dependencies=empty, transfers=empty, revalidations=empty),
        taxonomy_release_changed=False, previous_taxonomy_release_id=RELEASE, current_taxonomy_release_id=RELEASE,
        t5_version_changes=(), t6_specification_changes=())


class Chain:
    """Inférences successives d'un même couple : chaque step() évalue le
    dossier courant avec, pour predecessor, l'inférence précédente (stade,
    claims établies, revision-context-v1 relu comme une valeur JSONB)."""

    def __init__(self):
        self.previous = None  # (run_id, Dossier, T6C2Assessment)
        self.runs = iter(RUNS)

    def context(self, d, *, new=(), integrity=(), reevaluated=(), **overrides):
        run_id = next(self.runs)
        if self.previous is None:
            return d.context(run_id=run_id, **overrides)
        previous_run, previous_d, previous = self.previous
        dossier = previous_d.dossier()
        snapshot = svc.PredecessorSnapshot(
            inference_run_id=previous_run, longitudinal_assessment_run_id=dossier.run_id,
            current_stage=previous.current_stage, previous_stage=None, transition=None, transition_cause=None,
            tension_state="open" if previous.tensions else "none",
            unresolved_revision_context=None if previous.revision_context is None
            else svc._freeze(previous.revision_context.to_payload()),
            established_claim_stages=tuple(p.stage for p in previous.confidence_profiles if p is not None),
            input_fingerprint="3" * 64, output_fingerprint="4" * 64)
        decision = svc.PredecessorDecisionContext(
            inference_run_id=previous_run, longitudinal_assessment_run_id=dossier.run_id,
            pedagogical_taxonomy_release_id=RELEASE,
            historical_longitudinal_context=svc._historical_longitudinal_context(dossier), claims=(), tensions=(),
            run_basis_refs=(), validation_needs=(), state_decision_summary="décision historique")
        return d.context(run_id=run_id, predecessor=snapshot, predecessor_decision_context=decision,
                         transition_causality=_causality([d.events[e].id for e in new], integrity, reevaluated),
                         **overrides)

    def step(self, d, **kwargs):
        context = self.context(d, **kwargs)
        result = evaluate_inference_state(context, evaluate_positive_basis(context))
        self.previous = (context.run_id, _clone(d), result)
        return result


def _clone(d):
    copy = Dossier(d.competency, release=d.release, clock=d.clock, revisions=d.revisions)
    copy.observations, copy.dependencies = list(d.observations), list(d.dependencies)
    copy.transfers, copy.revalidations, copy.events = list(d.transfers), list(d.revalidations), dict(d.events)
    return copy


def _drop(d, *observation_ids):
    """Dossier sans ces observations (invalidation / réévaluation T3 : le
    snapshot T5 courant ne les contient plus)."""
    copy = _clone(d)
    copy.observations = [o for o in copy.observations if o.observation_id not in observation_ids]
    return copy


# --------------------------------------------------------------------------
# Structures, API
# --------------------------------------------------------------------------

STRUCTURES = (TensionAssessment, AffectedScope, ClaimCompatibilityAssessment, RevisionMotif,
              RevisionContextAssessment, RevisionMotifResolution, T6C2Assessment)


def test_structures_are_frozen_keyword_only_dataclasses():
    for cls in STRUCTURES:
        assert cls.__dataclass_params__.frozen, cls
        assert all(f.kw_only for f in dataclasses.fields(cls)), cls
        for f in dataclasses.fields(cls):
            assert f.type not in (int, float, "int", "float"), (cls, f.name)
            assert f.name not in ("score", "level", "count", "ratio", "weight", "revision_status",
                                  "transition_cause", "validation_needs", "state_decision_summary",
                                  "capability_membership_ids", "summary"), (cls, f.name)
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]  # noqa: E731
    assert names(T6C2Assessment) == [
        "positive_basis_version", "confidence_profile_version", "state_decision_version", "competency_code",
        "confidence_profiles", "claim_compatibilities", "tensions", "dossier_candidate_stage", "current_stage",
        "revision_context", "revision_resolutions", "state_reason_code"]
    assert names(TensionAssessment) == ["fragilized_stage", "scope_mode", "capability_definition_ids",
                                        "contradiction_observation_ids", "structural_refs", "compatibility_effect",
                                        "reason_codes"]
    assert names(RevisionContextAssessment) == ["schema_version", "origin_inference_run_id", "reason_code",
                                                "resolution_status", "motifs"]
    assert names(RevisionMotif) == ["fragilized_stage", "scope_mode", "capability_definition_ids",
                                    "source_contradiction_observation_ids", "reason_codes"]


def test_public_entry_point_is_a_pure_two_argument_function():
    assert list(inspect.signature(evaluate_inference_state).parameters) == ["context", "positive_basis"]
    public = {}
    for path in T6C2_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        public[path.name] = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public["inference_state.py"] == {"evaluate_inference_state"}
    assert public["inference_state_policies.py"] == {"validate_state_decision_policy", "resolve_state_decision_policy"}


def test_errors_are_a_dedicated_business_hierarchy():
    for cls in (InvalidInferenceStatePolicy, UnsupportedConfidenceProfilePolicy, UnsupportedStateDecisionPolicy,
                InconsistentPositiveBasis, UnsupportedRevisionContext):
        assert issubclass(cls, InferenceStateError)


# --------------------------------------------------------------------------
# 1-5 : premières inférences, tensions, garde descendante
# --------------------------------------------------------------------------

def test_first_expert_application_without_contradiction():
    """1."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B")
    result = evaluate(d)
    assert (result.current_stage, result.dossier_candidate_stage) == ("application", "application")
    assert result.state_reason_code == "highest_compatible_positive_claim"
    assert result.tensions == () and result.revision_context is None and result.revision_resolutions == ()
    assert [c.status if c else None for c in result.claim_compatibilities] == [
        "compatible", "compatible", "compatible", None]
    assert (result.positive_basis_version, result.confidence_profile_version, result.state_decision_version) == (
        "positive_basis-1", "confidence_profile-1", "state_decision-1")


def test_localized_isolated_contradiction_keeps_application_under_tension():
    """2 : C2_B seule ne représente pas la relation affirmée."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B")
    k = d.contra("e2", "C2_B", name="k")
    result = evaluate(d)
    assert result.current_stage == "application"
    assert result.state_reason_code == "highest_positive_claim_with_open_tension"
    (tension,) = tensions_on(result, "application")
    assert (tension.scope_mode, tension.capability_definition_ids, tension.contradiction_observation_ids) == (
        "localized", (definition_id("C2_B"),), (k,))
    assert tension.compatibility_effect == "tensioned"
    assert tension.reason_codes == ("claim_relevant_contradiction",)
    assert compat(result, "application").status == "tensioned"
    assert result.revision_context is None


def _held(scope="recognition"):
    """Application (C2_A + C2_B) ; Comprehension / Discovery implied, dont le
    périmètre hérité porte aussi la relation représentative de LEUR niveau
    (C2 Comprehension : C2_A reliée à C2_B | C2_C ; Discovery : toute
    capacité) ; contradiction unique, diagnostique, représentative à chacun
    de ces niveaux."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    k = d.contra("e2", "C2_A", "C2_B", scope=scope, name="k")
    return d, k


def _c5_fragilized():
    """Application (C5_B + C5_C) fragilisée par une contradiction
    représentative de niveau recognition. La Comprehension C5 (C5_A) n'est
    PAS portée par ce périmètre : évaluée à son propre niveau cognitif."""
    d = Dossier("C5")
    d.observe("e1", "C5_B", "C5_C", name="a")
    k = d.contra("e2", "C5_B", "C5_C", scope="recognition", name="k")
    return d, k


def test_application_materially_fragilized_without_defensible_lower_stage_is_held():
    """3 + 21 + 31 : jamais non_etabli par élimination, maintien sous
    revision-context-v1 (première inférence et predecessor)."""
    d, k = _held()
    result = evaluate(d, run_id=RUNS[0])
    assert result.dossier_candidate_stage is None
    assert result.current_stage == "application"
    assert result.state_reason_code == "first_inference_positive_stage_held_under_unresolved_tension"
    assert [c.status for c in result.claim_compatibilities[:3]] == ["materially_incompatible"] * 3
    context = result.revision_context
    assert (context.schema_version, context.origin_inference_run_id, context.reason_code,
            context.resolution_status) == ("revision-context-v1", RUNS[0],
                                           "higher_claim_materially_fragilized_lower_claim_not_identified",
                                           "unresolved")
    (motif,) = context.motifs
    assert (motif.fragilized_stage, motif.scope_mode, motif.capability_definition_ids,
            motif.source_contradiction_observation_ids) == (
        "application", "localized", (definition_id("C2_A"), definition_id("C2_B")), (k,))
    # Avec un predecessor Application : maintien temporaire du stade précédent.
    chain, base = Chain(), Dossier("C2")
    base.observe("e1", "C2_A", "C2_B", name="a")
    assert chain.step(base).current_stage == "application"
    held = chain.step(d, new=["e2"])
    assert held.current_stage == "application"
    assert held.state_reason_code == "previous_stage_temporarily_held_under_unresolved_tension"
    assert held.revision_context.reason_code == "higher_claim_materially_fragilized_lower_claim_not_identified"
    assert held.revision_context.origin_inference_run_id == RUNS[1]


def test_defensible_lower_comprehension_becomes_the_current_stage():
    """4 : Application materially fragilisée, Comprehension positivement
    établie (base directe distincte sur C5_A) et hors du périmètre contredit.
    Sans cette base directe, la Comprehension implied reste aussi défendable :
    son niveau cognitif (C5_A) n'est pas porté par le périmètre contredit."""
    d, k = _c5_fragilized()
    implied = evaluate(d)
    assert compat(implied, "comprehension").status == "tensioned"
    assert implied.current_stage == "comprehension"
    d.observe("e0", "C5_A", stage="comprehension", name="c")
    result = evaluate(d)
    assert compat(result, "application").status == "materially_incompatible"
    assert compat(result, "comprehension").status == "compatible"
    assert (result.dossier_candidate_stage, result.current_stage) == ("comprehension", "comprehension")
    assert result.revision_context.reason_code == "higher_claim_materially_fragilized_defensible_lower_claim_retained"
    chain, base = Chain(), Dossier("C5")
    base.observe("e0", "C5_A", stage="comprehension", name="c")
    base.observe("e1", "C5_B", "C5_C", name="a")
    assert chain.step(base).current_stage == "application"
    revised = chain.step(d, new=["e2"])
    assert revised.current_stage == "comprehension"
    assert revised.state_reason_code == "downward_revision_to_defensible_lower_claim"


def test_downward_revision_can_skip_several_stages():
    """5 : Comprehension (implied, relation C2 portée par le périmètre) aussi
    fragilisée à son propre niveau ; Discovery directe hors d'atteinte d'une
    contradiction de niveau comprehension."""
    d = Dossier("C2")
    d.observe("e0", "C2_C", stage="discovery", name="x")
    d.observe("e1", "C2_A", "C2_B", name="a")
    chain = Chain()
    assert chain.step(_clone(d)).current_stage == "application"
    d.contra("e2", "C2_A", "C2_B", scope="comprehension", name="k")
    result = chain.step(d, new=["e2"])
    assert [c.status for c in result.claim_compatibilities[:3]] == [
        "compatible", "materially_incompatible", "materially_incompatible"]
    assert (result.current_stage, result.state_reason_code) == ("discovery",
                                                                "downward_revision_to_defensible_lower_claim")


def _c10_application():
    """Application directe C10_B + C10_D ; Comprehension / Discovery implied."""
    d = Dossier("C10")
    a = d.observe("e1", "C10_B", "C10_D", name="a")
    return d, a


def test_implied_comprehension_is_tested_at_its_own_cognitive_level():
    """Correction d'audit : C10_B seule ne représente pas l'Application C10
    (C10_B reliée à C10_A | C10_C | C10_D) mais représente la Comprehension
    C10 (le prix dépend d'hypothèses). Une contradiction forte de niveau
    comprehension sur C10_B est donc lue selon CHAQUE niveau testé."""
    d, a = _c10_application()
    positive = evaluate_positive_basis(d.context())
    comprehension_claim = positive.claims[STAGES.index("comprehension")]
    assert (comprehension_claim.basis_mode, comprehension_claim.implied_from_stage) == ("implied_by_higher_claim",
                                                                                       "application")
    k = d.contra("e2", "C10_B", scope="comprehension", name="k")
    result = evaluate(d)
    (application,) = tensions_on(result, "application")
    (comprehension,) = tensions_on(result, "comprehension")
    assert (application.compatibility_effect, application.reason_codes) == ("tensioned",
                                                                            ("claim_relevant_contradiction",))
    assert (comprehension.compatibility_effect, comprehension.reason_codes) == (
        "materially_incompatible", ("claim_representative_contradiction",))
    for tension in (application, comprehension):
        assert (tension.capability_definition_ids, tension.contradiction_observation_ids) == (
            (definition_id("C10_B"),), (k,))
    assert tensions_on(result, "discovery") == []  # niveau comprehension : Discovery hors d'atteinte
    assert [c.status for c in result.claim_compatibilities[:3]] == ["compatible", "materially_incompatible",
                                                                    "tensioned"]
    # La base positive T6-C1 n'est pas touchée par la contradiction.
    assert evaluate_positive_basis(d.context()) == positive


def test_application_scope_still_never_fragilizes_implied_comprehension_c10():
    """Non-régression : contradiction_scope=application représentative de
    l'Application ne touche pas la Comprehension implied."""
    d, a = _c10_application()
    d.contra("e2", "C10_B", "C10_D", scope="application", name="k")
    result = evaluate(d)
    assert compat(result, "application").status == "materially_incompatible"
    assert compat(result, "comprehension").status == "compatible"
    assert tensions_on(result, "comprehension") == [] and tensions_on(result, "discovery") == []
    assert result.current_stage == "comprehension"


def test_application_scope_does_not_fragilize_comprehension():
    """6 + 32 : Comprehension implied reste compatible et devient le
    candidat : ce n'est pas une attribution par élimination (T6-C1 l'avait
    établie par implication fonctionnelle)."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    d.contra("e2", "C2_A", "C2_B", scope="application", name="k")
    result = evaluate(d)
    assert tensions_on(result, "comprehension") == [] and tensions_on(result, "discovery") == []
    assert compat(result, "application").status == "materially_incompatible"
    assert compat(result, "comprehension").status == "compatible"
    assert result.current_stage == "comprehension"
    positive = evaluate_positive_basis(d.context())
    assert positive.claims[STAGES.index("comprehension")].basis_mode == "implied_by_higher_claim"
    # Si le mécanisme contradictoire touche aussi Comprehension, elle est testée.
    d.contra("e3", "C2_A", "C2_B", scope="comprehension", name="k2")
    result = evaluate(d)
    assert compat(result, "comprehension").status == "materially_incompatible"
    assert compat(result, "discovery").status == "compatible"  # niveau comprehension : Discovery hors d'atteinte
    assert result.current_stage == "discovery"


def test_c8c_contradiction_does_not_fragilize_a_claim_carried_by_c8a():
    """7 + 14 : périmètre exact ; une difficulté périphérique reste une
    limitation, une difficulté pertinente mais locale une tension."""
    d = Dossier("C8")
    d.observe("e1", "C8_A", "C8_B", stage="comprehension", name="c")
    d.contra("e2", "C8_C", scope="comprehension", name="k")
    result = evaluate(d)
    assert result.tensions == () and compat(result, "comprehension").status == "compatible"
    d = Dossier("C8")
    d.observe("e1", "C8_B", "C8_A", name="a")
    d.contra("e2", "C8_D", name="k")
    result = evaluate(d)
    assert result.tensions == () and result.current_stage == "application"
    k2 = d.contra("e3", "C8_C", name="k2")  # touche la relation affirmée sans la représenter
    result = evaluate(d)
    (tension,) = result.tensions
    assert (tension.fragilized_stage, tension.capability_definition_ids, tension.contradiction_observation_ids,
            tension.compatibility_effect) == ("application", (definition_id("C8_C"),), (k2,), "tensioned")
    assert (result.current_stage, result.state_reason_code) == ("application",
                                                                "highest_positive_claim_with_open_tension")


def test_undetermined_scope_alone_is_never_materially_incompatible():
    """8."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B")
    d.contra("e2", "C2_A", "C2_B", scope="undetermined", name="k")
    result = evaluate(d)
    assert result.tensions and all(t.compatibility_effect == "tensioned" for t in result.tensions)
    assert all("undetermined_contradiction_scope" in t.reason_codes for t in result.tensions)
    assert {t.fragilized_stage for t in result.tensions} == {"discovery", "comprehension", "application"}
    assert result.current_stage == "application"


def test_contradiction_without_positive_claim_creates_no_tension():
    """9 : elle reste dans l'histoire T5, jamais un stade négatif."""
    d = Dossier("C2")
    d.contra("e1", "C2_A", "C2_B", scope="recognition", name="k")
    d.contra("e2", scope="application", name="k2")
    result = evaluate(d)
    assert result.tensions == () and result.current_stage == "non_etabli"
    assert result.state_reason_code == "no_current_positive_claim"
    assert result.claim_compatibilities == (None, None, None, None)


def test_historically_revalidated_contradiction_is_not_an_open_tension():
    """10."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    k = d.contra("e2", "C2_A", "C2_B", scope="recognition", name="k")
    r = d.observe("e3", "C2_A", "C2_B", name="r")
    d.revalidation(k, r)
    result = evaluate(d)
    assert result.tensions == () and result.current_stage == "application"
    consistency = result.confidence_profiles[STAGES.index("application")].consistency
    assert "historically_revalidated_contradiction_present" in consistency.fact_codes


def test_dependent_contradictions_never_reinforce_mechanically():
    """12 + 13 : aucune accumulation ; une contradiction unique diagnostique
    et représentative suffit, plusieurs non diagnostiques ou dépendantes
    jamais."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    k1 = d.contra("e2", "C2_A", "C2_B", strength="medium", name="k1")
    k2 = d.contra("e3", "C2_A", "C2_B", strength="medium", name="k2")
    k3 = d.contra("e4", "C2_A", "C2_B", strength="medium", name="k3")
    d.dependency(k2, k1)
    d.dependency(k3, k2, kind="partially_dependent")
    result = evaluate(d)
    (tension,) = tensions_on(result, "application")
    assert tension.compatibility_effect == "tensioned"
    assert tension.contradiction_observation_ids == sorted_ids([k1, k2, k3])
    assert "dependency_linked_contradiction" in tension.reason_codes
    assert "representative_scope_without_exceptional_diagnosticity" in tension.reason_codes
    assert {r.relation_kind for r in tension.structural_refs} == {"dependency"}
    assert result.current_stage == "application"
    # Trois contradictions autonomes (candidat de récurrence) : jamais une
    # indépendance établie, donc jamais la voie complémentaire.
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    for i in range(3):
        d.contra(f"k{i}", "C2_A", "C2_B", strength="medium", name=f"k{i}")
    (tension,) = tensions_on(evaluate(d), "application")
    assert tension.compatibility_effect == "tensioned"
    assert "structural_recurrence_candidate" in tension.reason_codes
    # Une contradiction strong DÉPENDANTE n'est pas attribuable : tension.
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    s = d.observe("e0", "C2_C", stage="discovery", name="s")
    k = d.contra("e2", "C2_A", "C2_B", name="k")
    d.dependency(k, s)
    assert compat(evaluate(d), "application").status == "tensioned"


def test_single_exceptionally_representative_contradiction_can_be_material():
    """13 : strong seul ne suffit pas (représentativité exigée) ;
    représentatif et diagnostique suffit."""
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    d.contra("e2", "C2_A", "C2_C", name="k")  # satisfait la relation, touche C2_A représentée
    result = evaluate(d)
    (tension,) = tensions_on(result, "application")
    assert tension.compatibility_effect == "materially_incompatible"
    assert tension.reason_codes == ("claim_representative_contradiction",)
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    d.contra("e2", "C2_C", name="k")  # strong mais non représentatif
    assert compat(evaluate(d), "application").status == "tensioned"


def test_claims_with_several_representative_relations_need_each_to_be_contradicted():
    d = Dossier("C12")
    d.observe("e1", "C12_A", "C12_B", "C12_C", "C12_D", name="a")
    d.contra("e2", "C12_A", "C12_B", name="k")
    result = evaluate(d)
    assert compat(result, "application").status == "tensioned"
    d.contra("e3", "C12_A", "C12_B", "C12_C", "C12_D", name="k2")
    assert compat(evaluate(d), "application").status == "materially_incompatible"


# --------------------------------------------------------------------------
# 18-23 : la base positive n'est jamais rouverte ; temps ; Mastery
# --------------------------------------------------------------------------

def test_time_alone_changes_nothing():
    """20."""
    def build(clock):
        d = Dossier("C2", clock=clock)
        d.observe("e1", "C2_A", "C2_B", name="a")
        d.contra("e2", "C2_B", name="k")
        return evaluate(d)

    assert build(EPOCH) == build(EPOCH + timedelta(days=3650)) == build(EPOCH - timedelta(days=900))
    chain = Chain()
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B", name="a")
    first = chain.step(d)
    later = _clone(d)
    later.clock = EPOCH + timedelta(days=4000)
    assert chain.step(later).current_stage == first.current_stage == "application"


def test_old_mastery_without_new_activity_remains_the_candidate():
    """22."""
    d, *_ = _mastery_dossier()
    chain = Chain()
    first = chain.step(d)
    assert first.current_stage == "mastery"
    again = chain.step(_clone(d))
    assert (again.current_stage, again.dossier_candidate_stage, again.state_reason_code) == (
        "mastery", "mastery", "highest_compatible_positive_claim")


def test_mastery_with_non_representative_localized_contradiction_stays_current():
    """23."""
    d, *_ = _mastery_dossier()
    d.contra("k", "C2_B", name="k")
    result = evaluate(d)
    assert result.current_stage == "mastery"
    assert result.state_reason_code == "highest_positive_claim_with_open_tension"
    assert compat(result, "mastery").status == "tensioned"
    assert all(t.compatibility_effect == "tensioned" for t in result.tensions)


# --------------------------------------------------------------------------
# 24-28 : anti-oscillation et résolution des motifs
# --------------------------------------------------------------------------

def _revised(chain=None):
    """R1 Application (C8_B + C8_C) ; R2 contradiction diagnostique et
    représentative (niveau application) : revised_down => Comprehension,
    motif (application, C8_B + C8_C)."""
    chain = chain or Chain()
    d = Dossier("C8")
    a = d.observe("e1", "C8_B", "C8_C", name="a")
    assert chain.step(_clone(d)).current_stage == "application"
    k = d.contra("e2", "C8_B", "C8_C", scope="application", name="k")
    revised = chain.step(d, new=["e2"])
    assert (revised.current_stage, revised.state_reason_code) == ("comprehension",
                                                                  "downward_revision_to_defensible_lower_claim")
    (motif,) = revised.revision_context.motifs
    assert (motif.fragilized_stage, motif.capability_definition_ids, motif.source_contradiction_observation_ids) == (
        "application", (definition_id("C8_B"), definition_id("C8_C")), (k,))
    return chain, d, a, k


def test_old_higher_evidence_alone_never_rebounds():
    """24 : T5 revalide la contradiction par l'ANCIENNE démonstration :
    Application redevient compatible dans le dossier, mais aucune
    démonstration nouvelle ne résout le motif."""
    chain, d, a, k = _revised()
    same = chain.step(_clone(d))
    assert same.current_stage == "comprehension"
    d.revalidation(k, a)
    blocked = chain.step(d)
    assert blocked.dossier_candidate_stage == "application"
    assert (blocked.current_stage, blocked.state_reason_code) == ("comprehension",
                                                                  "rebound_blocked_by_unresolved_revision_motif")
    (resolution,) = blocked.revision_resolutions
    assert resolution.resolution_status == "unresolved"
    assert resolution.reason_codes == ("no_supportive_resolution_on_motif_scope",)
    assert blocked.revision_context.motifs == (resolution.motif,)
    assert blocked.revision_context.origin_inference_run_id == RUNS[1]


def test_new_evidence_on_another_capability_does_not_resolve_the_motif():
    """25 : une excellente preuve C8_A ne résout pas C8_B + C8_C."""
    chain, d, a, k = _revised()
    d.observe("e3", "C8_B", "C8_A", name="n")
    result = chain.step(d, new=["e3"])
    (resolution,) = result.revision_resolutions
    assert resolution.resolution_status == "unresolved"
    assert result.current_stage == "comprehension"


def test_new_diagnostic_demonstration_on_the_motif_scope_resolves_it():
    """26 : démonstration nouvelle, diagnostique, représentative, sans
    dépendance, sur le périmètre exact du motif."""
    chain, d, a, k = _revised()
    n = d.observe("e3", "C8_B", "C8_C", name="n")
    result = chain.step(d, new=["e3"])
    (resolution,) = result.revision_resolutions
    assert resolution.resolution_status == "resolved_supportively"
    assert resolution.observation_ids == (n,)
    assert resolution.reason_codes == ("new_demonstration_on_motif_scope", "exceptionally_diagnostic_demonstration")
    assert compat(result, "application").status == "tensioned"
    (tension,) = tensions_on(result, "application")
    assert "revision_motif_resolved_supportively" in tension.reason_codes
    assert (result.current_stage, result.state_reason_code) == ("application", "revision_motif_resolved_supportively")
    assert result.revision_context is None


@pytest.mark.parametrize("case", ["medium_without_independence", "dependent_on_correction", "old_event"])
def test_insufficient_new_demonstrations_leave_the_motif_unresolved(case):
    chain, d, a, k = _revised()
    if case == "medium_without_independence":
        d.observe("e3", "C8_B", "C8_C", strength="medium", name="n")
    elif case == "dependent_on_correction":
        n = d.observe("e3", "C8_B", "C8_C", name="n")
        d.dependency(n, k)
    else:
        d.observe("e3", "C8_B", "C8_C", name="n")
    result = chain.step(d, new=[] if case == "old_event" else ["e3"])
    assert result.revision_resolutions[0].resolution_status == "unresolved"
    assert result.current_stage == "comprehension"


def test_t5_revalidation_of_the_exact_motif_resolves_it():
    """27 : une démonstration nouvelle, même medium, revalidant par T5 la
    contradiction du motif sur son périmètre exact."""
    chain, d, a, k = _revised()
    n = d.observe("e3", "C8_B", "C8_C", strength="medium", name="n")
    rv = d.revalidation(k, n)
    result = chain.step(d, new=["e3"])
    (resolution,) = result.revision_resolutions
    assert resolution.resolution_status == "resolved_supportively"
    assert resolution.reason_codes == ("new_demonstration_on_motif_scope", "t5_independence_on_motif_scope")
    assert [(r.relation_kind, r.relation_id) for r in resolution.structural_refs] == [("revalidation", rv)]
    assert result.tensions == ()
    assert (result.current_stage, result.state_reason_code) == ("application", "revision_motif_resolved_supportively")


@pytest.mark.parametrize("fact", ["integrity", "reevaluated"])
def test_invalidated_or_reevaluated_motif_is_superseded(fact):
    """28 : jamais « l'utilisateur a progressé »."""
    chain, d, a, k = _revised()
    kwargs = {"integrity": [k]} if fact == "integrity" else {"reevaluated": [k]}
    result = chain.step(_drop(d, k), **kwargs)
    (resolution,) = result.revision_resolutions
    assert resolution.resolution_status == "superseded_by_reinterpretation_or_integrity_change"
    assert resolution.reason_codes == ((
        "motif_contradictions_invalidated",) if fact == "integrity" else ("motif_contradictions_reevaluated",))
    assert (result.current_stage, result.state_reason_code) == (
        "application", "revision_motif_superseded_by_reinterpretation")
    # Contradiction absente sans fait d'intégrité / réévaluation : non superseded.
    chain, d, a, k = _revised()
    assert chain.step(_drop(d, k)).revision_resolutions[0].resolution_status == "unresolved"


def test_unresolved_motif_on_a_claim_no_longer_established_keeps_a_revision_tension():
    """§12 : claim historiquement pertinente du predecessor dans un contexte
    de révision ; jamais une incompatibilité, jamais une preuve."""
    chain, d, a, k = _revised()
    d.observe("e3", "C8_A", stage="discovery", name="x")
    result = chain.step(_drop(d, a), new=["e3"], integrity=[a])
    assert (result.current_stage, result.state_reason_code) == ("discovery",
                                                                "downward_revision_to_defensible_lower_claim")
    (tension,) = result.tensions
    assert (tension.fragilized_stage, tension.capability_definition_ids, tension.contradiction_observation_ids,
            tension.compatibility_effect, tension.reason_codes) == (
        "application", (definition_id("C8_B"), definition_id("C8_C")), (k,), "tensioned",
        ("predecessor_claim_in_revision_context",))
    assert compat(result, "discovery").status == "compatible"
    assert result.revision_context.motifs == (result.revision_resolutions[0].motif,)


def test_revision_context_payload_round_trips_and_unknown_formats_fail_closed():
    chain, d, a, k = _revised()
    context = chain.previous[2].revision_context
    payload = context.to_payload()
    assert payload["schema_version"] == "revision-context-v1"
    carried = chain.step(_clone(d))
    assert carried.revision_resolutions[0].motif == context.motifs[0]
    for broken in ({**payload, "schema_version": "revision-context-v2"}, {**payload, "extra": 1},
                   {**payload, "resolution_status": "resolved_supportively"}, {**payload, "motifs": []},
                   {**payload, "motifs": [{**payload["motifs"][0], "fragilized_stage": "expert"}]},
                   {**payload, "motifs": [{**payload["motifs"][0], "capability_definition_ids": ["C8_B"]}]}):
        run_id, previous_d, previous = chain.previous
        fake = dataclasses.replace(previous, revision_context=None)
        chain.previous = (run_id, previous_d, fake)
        ctx = chain.context(_clone(d))
        ctx = dataclasses.replace(ctx, predecessor=dataclasses.replace(
            ctx.predecessor, unresolved_revision_context=svc._freeze(broken)))
        chain.runs = iter(RUNS)
        with pytest.raises(UnsupportedRevisionContext):
            evaluate_inference_state(ctx, evaluate_positive_basis(ctx))


# --------------------------------------------------------------------------
# 29-31 : predecessor jamais preuve ; non_etabli
# --------------------------------------------------------------------------

def test_predecessor_is_never_positive_evidence():
    """29."""
    d, *_ = _mastery_dossier()
    chain = Chain()
    assert chain.step(d).current_stage == "mastery"
    lower = Dossier("C2")
    lower.observe("x", "C2_C", stage="discovery", name="x")
    result = chain.step(lower, integrity=[o.observation_id for o in d.observations])
    assert (result.current_stage, result.state_reason_code) == ("discovery",
                                                                "downward_revision_to_defensible_lower_claim")
    # Les claims et profils ne dépendent pas du predecessor.
    alone = evaluate(lower)
    assert alone.confidence_profiles == result.confidence_profiles
    assert alone.claim_compatibilities == result.claim_compatibilities


def test_loss_of_every_positive_claim_after_integrity_correction_is_non_etabli():
    """30."""
    chain = Chain()
    d = Dossier("C2")
    a = d.observe("e1", "C2_A", "C2_B", name="a")
    d.contra("e2", "C2_C", name="k")
    assert chain.step(d).current_stage == "application"
    result = chain.step(_drop(d, a), integrity=[a])
    assert (result.current_stage, result.state_reason_code) == ("non_etabli", "no_current_positive_claim")
    assert result.tensions == ()


def test_contradiction_alone_never_leads_to_non_etabli():
    """31."""
    chain = Chain()
    d, _ = _held()
    base = _drop(d, uuid.uuid5(NS, "observation:k"))
    chain.step(base)
    for i in range(3):
        d.contra(f"z{i}", "C2_A", "C2_B", scope="recognition", name=f"z{i}")
    result = chain.step(d, new=["e2", "z0", "z1", "z2"])
    assert result.current_stage == "application"
    assert result.state_reason_code == "previous_stage_temporarily_held_under_unresolved_tension"


def test_held_stage_can_come_down_when_a_lower_claim_becomes_defensible():
    chain = Chain()
    d, k = _held()
    chain.step(d)
    d.observe("e3", "C2_C", stage="discovery", name="x")  # hors du périmètre contredit
    result = chain.step(d, new=["e3"])
    assert compat(result, "discovery").status == "tensioned"
    assert (result.current_stage, result.state_reason_code) == ("discovery",
                                                                "downward_revision_to_defensible_lower_claim")


# --------------------------------------------------------------------------
# 33 : competency_only
# --------------------------------------------------------------------------

def test_competency_only_contradiction_stays_competency_only():
    """33."""
    d = Dossier("C10")
    d.observe("e1", "C10_B", "C10_A", name="a")
    k = d.contra("e2", scope="application", name="k")
    result = evaluate(d)
    (tension,) = tensions_on(result, "application")
    assert (tension.scope_mode, tension.capability_definition_ids, tension.contradiction_observation_ids) == (
        "competency_only", (), (k,))
    assert tension.compatibility_effect == "tensioned"
    assert "competency_only_contradiction" in tension.reason_codes
    assert compat(result, "application").affected_scope == AffectedScope(capability_definition_ids=(),
                                                                          competency_only=True)
    # Comparable au seul niveau de la compétence : claim purement competency_only.
    d = Dossier("C10")
    d.observe("e1", name="a")
    d.contra("e2", scope="application", name="k")
    assert compat(evaluate(d), "application").status == "materially_incompatible"
    # Une contradiction localisée n'est pas comparable à une claim competency_only.
    d = Dossier("C10")
    d.observe("e1", name="a")
    d.contra("e2", "C10_B", "C10_A", name="k")
    assert evaluate(d).tensions == ()


# --------------------------------------------------------------------------
# 34-35 : versions, entrées, déterminisme
# --------------------------------------------------------------------------

def _context():
    d = Dossier("C2")
    d.observe("e1", "C2_A", "C2_B")
    return d.context()


@pytest.mark.parametrize("version", ["confidence_profile-2", "confidence_profile-0", "confidence_profile", "", None, 1])
def test_unknown_confidence_profile_version_fails_closed(version):
    """34."""
    context = dataclasses.replace(_context(), confidence_profile_version=version)
    with pytest.raises(UnsupportedConfidenceProfilePolicy):
        evaluate_inference_state(context, evaluate_positive_basis(_context()))


@pytest.mark.parametrize("version", ["state_decision-2", "state_decision-0", "state_decision", "", None, 1])
def test_unknown_state_decision_version_fails_closed(version):
    """34."""
    context = dataclasses.replace(_context(), state_decision_version=version)
    with pytest.raises(UnsupportedStateDecisionPolicy):
        evaluate_inference_state(context, evaluate_positive_basis(_context()))


def test_unknown_positive_basis_version_and_v1_contexts_fail_closed():
    with pytest.raises(UnsupportedPositiveBasisPolicy):
        evaluate_inference_state(dataclasses.replace(_context(), positive_basis_version="positive_basis-2"),
                                 evaluate_positive_basis(_context()))
    for overrides in ({"input_schema_version": 1}, {"model_id": "m"}, {"prompt_spec_version": "p"}):
        with pytest.raises(UnsupportedInferenceContext):
            evaluate_inference_state(dataclasses.replace(_context(), **overrides), evaluate_positive_basis(_context()))
    with pytest.raises(PositiveBasisError):
        evaluate_inference_state("context", evaluate_positive_basis(_context()))


def test_undeclared_combination_fails_closed_without_default():
    registry = {("positive_basis-1", "confidence_profile-1", "state_decision-2"): STATE_DECISION_V1_POLICY}
    with pytest.raises(UnsupportedStateDecisionPolicy):
        T6C2PolicyResolver(registry=registry).resolve(_context())
    with pytest.raises(UnsupportedConfidenceProfilePolicy):
        T6C2PolicyResolver(registry={}).resolve(_context())
    assert tuple(STATE_DECISION_POLICIES) == (("positive_basis-1", "confidence_profile-1", "state_decision-1"),)
    validate_state_decision_policy(STATE_DECISION_V1_POLICY)
    for broken in ({"material_contradiction_scopes": frozenset(CONTRADICTION_SCOPES)},
                   {"contradiction_scope_reach": {}}, {"exceptional_diagnostic_strengths": frozenset()}):
        with pytest.raises(InvalidInferenceStatePolicy):
            validate_state_decision_policy(dataclasses.replace(STATE_DECISION_V1_POLICY, **broken))


def test_positive_basis_must_be_the_t6c1_basis_of_the_same_context():
    context = _context()
    positive = evaluate_positive_basis(context)
    for bad in ("positive", dataclasses.replace(positive, competency_code="C3"),
                dataclasses.replace(positive, policy_version="positive_basis-0"),
                dataclasses.replace(positive, highest_established_stage="mastery")):
        with pytest.raises(InconsistentPositiveBasis):
            evaluate_inference_state(context, bad)
    other = Dossier("C2")
    other.observe("e1", "C2_B", stage="discovery")
    with pytest.raises(InconsistentPositiveBasis):
        evaluate_inference_state(context, evaluate_positive_basis(other.context()))


def test_incoherent_predecessor_inputs_fail_closed():
    chain, d, a, k = _revised()
    context = chain.context(_clone(d))
    with pytest.raises(PositiveBasisError):
        evaluate_inference_state(dataclasses.replace(context, transition_causality=None),
                                 evaluate_positive_basis(context))
    with pytest.raises(PositiveBasisError):
        evaluate_inference_state(dataclasses.replace(context, predecessor_decision_context=None),
                                 evaluate_positive_basis(context))
    with pytest.raises(PositiveBasisError):
        evaluate_inference_state(dataclasses.replace(context, predecessor=None, predecessor_decision_context=None),
                                 evaluate_positive_basis(context))


def _rich():
    d = Dossier("C12")
    a = d.observe("e1", "C12_A", "C12_B", name="a")
    b = d.observe("e2", "C12_B", "C12_C", name="b", support="hinted")
    d.transfer(a, b, caps=["C12_B"])
    d.observe("e3", "C12_B", stage="comprehension", strength="medium", name="c")
    s = d.observe("e4", "C12_A", stage="discovery", strength="weak", name="d")
    k = d.contra("e6", "C12_C", name="k")
    r = d.observe("e7", "C12_C", "C12_D", name="r")
    d.revalidation(k, r)
    k2 = d.contra("e8", "C12_B", "C12_C", scope="comprehension", name="k2")
    d.dependency(k2, s)
    d.contra("e9", "C12_A", "C12_B", "C12_C", "C12_D", scope="undetermined", name="k3")
    d.contra("e10", scope="recognition", strength="medium", name="k4")
    d.contra("e11", "C12_D", scope="application", name="k5")
    return d


def test_same_input_same_output_whatever_the_collection_order():
    """35."""
    d = _rich()
    baseline = evaluate(d)
    assert baseline.tensions
    assert all(evaluate(d) == baseline for _ in range(5))
    assert evaluate(d, reverse=True) == baseline
    shuffled = taxonomy("C12")
    shuffled = dataclasses.replace(shuffled, capabilities=tuple(reversed(shuffled.capabilities)))
    assert evaluate(d, taxonomy_context=shuffled, reverse=True) == baseline
    # Avec un predecessor portant un contexte de révision.
    chain, c8, a, k = _revised()
    context = chain.context(_clone(c8))
    reversed_context = dataclasses.replace(context, longitudinal_dossier=c8.dossier(reverse=True))
    assert evaluate_inference_state(context, evaluate_positive_basis(context)) == \
        evaluate_inference_state(reversed_context, evaluate_positive_basis(reversed_context))


def test_canonical_orders():
    result = evaluate(_rich())
    order = [definition_id(c) for c in competency_codes("C12")]
    keys = [(STAGES.index(t.fragilized_stage), t.scope_mode, [order.index(i) for i in t.capability_definition_ids],
             [str(i) for i in t.contradiction_observation_ids]) for t in result.tensions]
    assert keys == sorted(keys)
    for t in result.tensions:
        assert list(t.contradiction_observation_ids) == sorted(t.contradiction_observation_ids, key=str)
        assert list(t.structural_refs) == sorted(t.structural_refs, key=lambda r: (r.relation_kind,
                                                                                    str(r.relation_id)))
    for item in result.claim_compatibilities:
        if item is not None:
            assert item.tensions == tuple(t for t in result.tensions if t.fragilized_stage == item.stage)
    assert result.state_reason_code in STATE_REASON_CODES


# --------------------------------------------------------------------------
# 36 : pureté statique
# --------------------------------------------------------------------------

def _imports(path):
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    return imported


def test_no_database_llm_network_clock_or_randomness():
    """36."""
    assert _imports(STATE_PATH) == {"uuid", "collections.abc", "dataclasses", "core.inference_confidence",
                                    "core.inference_policies", "core.inference_positive_basis",
                                    "core.inference_service", "core.inference_state_policies"}
    assert _imports(STATE_POLICIES_PATH) == {"collections.abc", "dataclasses", "types", "core.inference_policies",
                                             "core.inference_positive_basis", "core.inference_service"}
    for path in T6C2_PATHS:
        tokens = _code_tokens(path.read_text(encoding="utf-8")).lower().split("\n")
        for word in ("sqlalchemy", "session", "select", "execute", "commit", "rollback", "flush", "db", "models",
                     "anthropic", "openai", "claude", "llm", "requests", "httpx", "urllib", "socket", "random",
                     "datetime", "now", "utcnow", "time", "uuid4", "sleep", "getenv", "environ", "open"):
            assert word not in tokens, (path.name, word)


def test_no_text_reinterpretation_and_no_technical_time():
    for path in T6C2_PATHS:
        tokens = _code_tokens(path.read_text(encoding="utf-8")).split("\n")
        for field in ("observation_text", "primary_user_action", "contributive_user_actions",
                      "residual_cognitive_work", "source_contribution_refs", "event_started_at_technical",
                      "event_closed_at_technical", "demonstration_time", "observation_created_at_inference",
                      "run_completed_at_technical", "observation_inference_times", "state_decision_summary",
                      "basis_summary", "scope_summary", "summary"):
            assert field not in tokens, (path.name, field)


def test_no_score_counter_threshold_or_numeric_rule():
    for path in T6C2_PATHS:
        tokens = _code_tokens(path.read_text(encoding="utf-8")).lower()
        # support_level : champ T3 lu tel quel (ANSWER_GIVEN), jamais un niveau calculé.
        words = set(re.split(r"[^a-z0-9]+", tokens.replace("support_level", "")))
        for word in ("score", "scores", "scoring", "percent", "percentage", "ratio", "average", "mean", "points",
                     "weight", "weights", "weighted", "coefficient", "threshold", "xp", "rank", "decay", "sum",
                     "count", "counter", "min", "max", "days", "months", "freshness", "fresh", "stale", "balance",
                     "penalty", "probability", "almost", "high", "low", "level", "rating"):
            assert word not in words, (path.name, word)
        for compound in ("competency_score", "confidence_score", "evidence_balance", "contradiction_penalty",
                         "decay_rate", "mastery_points", "overall_user_level", "coverage_percent",
                         "capability_stage", "capability_mastered", "overall_confidence"):
            assert compound not in tokens, (path.name, compound)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        numbers = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) in (int, float)]
        assert numbers == [], (path.name, numbers)
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "len"]


def test_t6c2_builds_no_decision_and_decides_nothing_of_t6c3():
    for path in T6C2_PATHS:
        tokens = _code_tokens(path.read_text(encoding="utf-8")).split("\n")
        for name in ("InferenceDecision", "StageClaimDecision", "TensionDecision", "BasisRefDecision",
                     "start_competency_inference", "complete_competency_inference", "get_inference_context",
                     "fail_competency_inference", "transition_cause", "validation_needs", "revision_status",
                     "revalidation_needed", "membership_id", "capability_membership_ids", "new_user_evidence",
                     "evidence_integrity_change", "pedagogical_reinterpretation", "tension_state"):
            assert name not in tokens, (path.name, name)
    assert _imports(REPO_ROOT / "core" / "inference_service.py").isdisjoint(
        {"core.inference_state", "core.inference_confidence", "core.inference_state_policies"})
    assert "confidence_profile" not in _code_tokens(STATE_POLICIES_PATH.read_text(encoding="utf-8")).split("\n")


def test_c1_c12_semantic_policy_is_not_duplicated():
    """Aucune capability_code ni SemanticPattern dans T6-C2 : la
    représentativité vient de positive_basis-1."""
    for path in T6C2_PATHS:
        tokens = _code_tokens(path.read_text(encoding="utf-8"))
        assert not re.search(r"\bC\d+_[A-D]\b", tokens), path.name
        assert not {"SemanticPattern", "StageRepresentativityPolicy", "_pattern",
                    "POSITIVE_BASIS_V1_POLICY"} & set(tokens.split("\n")), path.name


# --------------------------------------------------------------------------
# Compatibilité structurelle avec T6-B (adaptateur de TEST uniquement)
# --------------------------------------------------------------------------

def decision_from(context, result, *, cause=None, transition_refs=()):
    """Adaptateur de test : T6-C2 ne construit aucune décision ; T6-C3
    résoudra memberships, revision_status, causes et résumés. Ici :
    valeurs de remplissage, memberships résolus par la taxonomie courante."""
    positive = evaluate_positive_basis(context)
    membership = {c.definition_id: c.membership_id for c in context.current_taxonomy_context.capabilities}
    claims, refs, tensions = [], [], []
    for claim, prof in zip(positive.claims, result.confidence_profiles):
        claims.append(StageClaimDecision(
            stage=claim.stage, positive_basis_status=claim.status, basis_mode=claim.basis_mode,
            confidence_profile=None if prof is None else {"schema_version": prof.schema_version},
            mastery_assessment=None if claim.stage != "mastery"
            else {"schema_version": claim.mastery_assessment.schema_version}))
        refs.extend(BasisRefDecision(ref_role="positive_basis", source_kind="observation", source_id=i,
                                     claim_stage=claim.stage) for i in claim.basis_observation_ids)
        refs.extend(BasisRefDecision(ref_role="mastery", source_kind=r.relation_kind, source_id=r.relation_id,
                                     claim_stage="mastery") for r in claim.structural_basis_refs)
        if prof is not None:
            for name in ("diagnosticity", "coverage", "independence", "consistency", "temporal_validation"):
                dimension = getattr(prof, name)
                refs.extend(BasisRefDecision(ref_role="confidence", source_kind="observation", source_id=i,
                                             claim_stage=claim.stage, confidence_dimension=name)
                            for i in dimension.observation_ids)
                refs.extend(BasisRefDecision(ref_role="confidence", source_kind=r.relation_kind,
                                             source_id=r.relation_id, claim_stage=claim.stage,
                                             confidence_dimension=name) for r in dimension.structural_refs)
    for index, tension in enumerate(result.tensions):
        key = f"t{index}"
        tensions.append(TensionDecision(
            tension_key=key, fragilized_stage=tension.fragilized_stage, scope_mode=tension.scope_mode,
            summary="adaptateur de test", revision_status="unresolved",
            capability_membership_ids=tuple(membership[d] for d in tension.capability_definition_ids)))
        refs.extend(BasisRefDecision(ref_role="tension", source_kind="observation", source_id=i, tension_key=key)
                    for i in tension.contradiction_observation_ids)
    refs.extend(BasisRefDecision(ref_role="transition", source_kind="observation", source_id=i)
                for i in transition_refs)
    return InferenceDecision(
        current_stage=result.current_stage, transition_cause=cause,
        unresolved_revision_context=None if result.revision_context is None
        else result.revision_context.to_payload(),
        validation_needs=(), state_decision_summary="adaptateur de test", claims=tuple(claims),
        tensions=tuple(tensions), basis_refs=tuple(refs))


@pytest.mark.parametrize("builder", [_rich, lambda: _held()[0], lambda: _mastery_dossier()[0], lambda: Dossier("C1")])
def test_t6c2_outputs_satisfy_t6b_structural_validation(builder):
    context = builder().context()
    result = evaluate_inference_state(context, evaluate_positive_basis(context))
    svc._validated_decision(decision_from(context, result))


def test_implied_confidence_provenance_is_accepted_by_t6b_without_a_second_positive_basis():
    """Correction d'audit : O42 est positive_basis de la seule Application ;
    un adaptateur T6-C3 peut citer O42 comme provenance confidence de la
    Comprehension implied (diagnosticity), jamais comme sa positive_basis.
    La validation structurelle de T6-B l'accepte."""
    d = Dossier("C2")
    o42 = d.observe("e42", "C2_A", "C2_B", name="O42")
    context = d.context()
    result = evaluate_inference_state(context, evaluate_positive_basis(context))
    decision = decision_from(context, result)
    refs = {(r.ref_role, r.claim_stage, r.confidence_dimension, r.source_id) for r in decision.basis_refs}
    assert ("confidence", "comprehension", "diagnosticity", o42) in refs
    assert ("positive_basis", "application", None, o42) in refs
    assert not [r for r in decision.basis_refs if r.ref_role == "positive_basis" and r.claim_stage != "application"]
    svc._validated_decision(decision)
    # Une seconde positive_basis de O42 serait, elle, refusée par T6-B.
    forged = dataclasses.replace(decision, basis_refs=(*decision.basis_refs, BasisRefDecision(
        ref_role="positive_basis", source_kind="observation", source_id=o42, claim_stage="comprehension")))
    with pytest.raises(svc.InferenceServiceError):
        svc._validated_decision(forged)


# --------------------------------------------------------------------------
# 2. Intégration PostgreSQL : T6-B -> T6-C1 -> T6-C2 -> T6-B (deux runs)
# --------------------------------------------------------------------------

def test_pg_real_contexts_are_evaluated_chained_and_accepted_by_t6b(Sessions):  # noqa: F811
    from core import longitudinal_service as t5
    from core import models
    from core import taxonomy_service as tax
    from tests.test_longitudinal_service import _t3, app, contra
    from tests.test_longitudinal_service import _start as start_t5
    from tests.test_taxonomy_service import _def_kwargs

    with Sessions() as session:
        release = tax.create_candidate_release(session, version_key=f"t6c2-{uuid.uuid4()}",
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

    def activate_t5():
        run = start_t5(Sessions, release_id)
        with Sessions() as s:
            t5.complete_longitudinal_assessment(s, run_id=run)
            s.commit()
        return run

    def infer(parent):
        with Sessions() as session:
            context = svc.start_competency_inference(
                session, longitudinal_assessment_run_id=parent, trigger="longitudinal_completed",
                **{name: f"{name.split('_version')[0]}-1" for name in VERSIONS})
            session.commit()
        result = evaluate_inference_state(context, evaluate_positive_basis(context))
        assert result == evaluate_inference_state(context, evaluate_positive_basis(context))
        return context, result

    def complete(context, result, **kwargs):
        with Sessions() as session:
            svc.complete_competency_inference(session, run_id=context.run_id,
                                              decision=decision_from(context, result, **kwargs))
            session.commit()

    # R1 : Application (C7_A reliée à C7_B), contradiction locale C7_A seule.
    _t3(Sessions, release_id, app(m["C7_A"], m["C7_B"], evidence_strength="strong"))
    local = _t3(Sessions, release_id, contra(m["C7_A"], contradiction_scope="application")).id
    first, r1 = infer(activate_t5())
    assert (r1.current_stage, r1.state_reason_code) == ("application", "highest_positive_claim_with_open_tension")
    assert [t.contradiction_observation_ids for t in r1.tensions] == [(local,)]
    complete(first, r1)

    # R2 : nouvelle contradiction diagnostique et représentative (niveau
    # application) : revised_down vers Comprehension (implied), motif persisté.
    material = _t3(Sessions, release_id, contra(m["C7_A"], m["C7_B"], contradiction_scope="application",
                                                evidence_strength="strong")).id
    second, r2 = infer(activate_t5())
    assert second.predecessor.current_stage == "application"
    assert (r2.current_stage, r2.state_reason_code) == ("comprehension",
                                                        "downward_revision_to_defensible_lower_claim")
    assert r2.revision_context.motifs[0].source_contradiction_observation_ids == (material,)
    complete(second, r2, cause="new_user_evidence", transition_refs=(material,))

    # R3 : le motif revient de la base (JSONB) et reste non résolu.
    _t3(Sessions, release_id, app(m["C7_D"], local_stage="discovery"))
    third, r3 = infer(activate_t5())
    assert third.predecessor.unresolved_revision_context["schema_version"] == "revision-context-v1"
    assert [r.resolution_status for r in r3.revision_resolutions] == ["unresolved"]
    assert r3.current_stage == "comprehension"
    assert r3.revision_context.origin_inference_run_id == second.run_id
    complete(third, r3, cause="new_user_evidence",
             transition_refs=tuple(o.observation_id for o in third.longitudinal_dossier.active_history.observations
                                   if o.event_id in third.transition_causality.new_user_event_ids))
    with Sessions() as session:
        state = svc.get_validated_user_competency_state(session, user_id=third.user_id, competency_code="C7")
        assert (state.current_stage, state.tension_state) == ("comprehension", "open")
