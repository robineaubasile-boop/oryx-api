"""Tests de l'Étape 6.1A : instantané validé de l'état d'adaptation
(core/adaptation_state.py).

1. Projection PURE (toujours exécutée) : enregistrements construits à la
   main -> _project_competency. Périmètres des claims directes / implied /
   not_established, competency_only, Mastery, tensions, validation_needs,
   confidence_profile, revision context, ordres canoniques, déterminisme,
   immuabilité, state_generation technique, fail closed.

2. Contrat statique : API publique, aucune écriture, aucun recalcul T6, aucun
   résumé d'audit lu, aucun niveau / score, aucune migration, aucun
   branchement runtime, aucune brique 6-1B+.

3. Intégration contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL, sinon
   SKIPPÉS ; jamais SQLite) : CognitiveEvent -> T3 -> T4 -> T5 -> T6
   (infer_competency + complete_competency_inference) -> COMMIT ->
   load_adaptation_state ; compatibilité de release par definition_id,
   résumés falsifiés, lecteur strict, aucune écriture, anti-snapshot hybride.
"""
import ast
import contextlib
import dataclasses
import inspect
import random
import uuid
from types import MappingProxyType

import pytest
import sqlalchemy as sa

from core import adaptation_state as ad
from core import inference_service as svc
from core.adaptation_state import (
    COMPETENCY_ORDER,
    AdaptationStageClaim,
    AdaptationStateError,
    AdaptationStateSnapshot,
    AdaptationTension,
    AdaptationUserNotFound,
    AdaptationValidationNeed,
    CapabilitySemanticRef,
    CompetencyAdaptationSnapshot,
    InvalidAdaptationArgument,
    InvalidAdaptationState,
    StaleAdaptationState,
    load_adaptation_state,
)
from core.inference_service import ValidatedCompetencyState
from tests.test_inference_engine import Pipeline
from tests.test_inference_service import Sessions  # noqa: F401 — fixture (base et nettoyage T6-B)
from tests.test_longitudinal_service import engine  # noqa: F401 — fixture
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

MODULE_PATH = REPO_ROOT / "core" / "adaptation_state.py"
NS = uuid.UUID("6a1a0000-0000-4000-8000-000000000000")
STAGES = ("discovery", "comprehension", "application", "mastery")
DIMENSIONS = ("diagnosticity", "coverage", "independence", "consistency", "temporal_validation")
MASTERY_PROPERTIES = ("autonomy", "independent_repetition", "variety_transfer", "longitudinality",
                      "robustness_revision")


def uid(name: str) -> uuid.UUID:
    return uuid.uuid5(NS, name)


def thaw(value):
    """Valeur gelée (mappingproxy / tuple) -> JSON Python (dict / list)."""
    if isinstance(value, (dict, MappingProxyType)):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def profile(*definition_ids, facts=("single_representative_episode",)):
    """confidence-profile-v1 tel que persisté par T6-C3."""
    return {"schema_version": "confidence-profile-v1", **{
        name: {"fact_codes": list(facts), "capability_definition_ids": [str(d) for d in definition_ids],
               "limitations": []} for name in DIMENSIONS}}


def mastery_payload(*definition_ids, status="supported"):
    return {"schema_version": "mastery-assessment-v1",
            **{name: {"status": status, "reason_codes": ["t5_durability_evidence"], "limitations": []}
               for name in MASTERY_PROPERTIES},
            "represented_capability_definition_ids": [str(d) for d in definition_ids], "limitations": []}


def need(intent, stage, scope, *definition_ids, reasons=None):
    reasons = reasons or (["unresolved_revision_motif"] if intent == "revalidation"
                          else ["independence_not_established_on_current_basis"])
    return {"schema_version": "validation-need-v1", "intent": intent, "target_stage": stage, "scope_mode": scope,
            "capability_definition_ids": [str(d) for d in definition_ids], "reason_codes": list(reasons)}


class World:
    """Enregistrements d'acquisition d'UN run T6 (compétence C8), construits
    à la main : la projection est une fonction pure de ces données."""

    def __init__(self, code="C8"):
        self.code = code
        self.release = uid("release")
        self.run_id, self.parent_id = uid("run"), uid("t5")
        self.capabilities = [ad._CapabilityRecord(uid(f"m-{c}"), self.release, uid(f"d-{c}"), c, 1, f"label {c}",
                                                  c.split("_")[0])
                             for c in (f"{code}_A", f"{code}_B", f"{code}_C", f"{code}_D", "C9_A")]
        self.claims = {stage: ["not_established", "none", None, None] for stage in STAGES}
        self.claims["mastery"][3] = mastery_payload()
        self.refs, self.observations, self.snapshot, self.tensions, self.needs = [], {}, set(), [], []
        self.current_stage, self.tension_state, self.state_generation = "non_etabli", "none", 1
        self.context = None
        self.interpretation_status = "active"

    def d(self, letter):
        return uid(f"d-{self.code}_{letter}")

    def m(self, letter):
        return uid(f"m-{self.code}_{letter}")

    def obs(self, name, *letters, localization="localized", polarity="supportive", source_release=None,
            mappings=None, in_snapshot=True):
        observation_id = uid(f"o-{name}")
        if mappings is None:
            mappings = tuple(ad._MappingRecord(source_release or self.release, self.d(letter), self.code)
                             for letter in letters)
        self.observations[observation_id] = ad._ObservationRecord(
            observation_id, self.code, polarity, localization, source_release or self.release, tuple(mappings))
        if in_snapshot:
            self.snapshot.add(observation_id)
        return observation_id

    def direct(self, stage, *observation_ids, confidence=None, assessment=None):
        claim = self.claims[stage]
        claim[:3] = ["established", "direct", confidence or profile()]
        if assessment is not None:
            claim[3] = assessment
        for observation_id in observation_ids:
            self.refs.append(ad._RefRecord(self.run_id, uid(f"claim-{stage}"), "positive_basis", "observation",
                                           observation_id))

    def implied(self, stage):
        self.claims[stage][:3] = ["established", "implied_by_higher_claim", profile()]

    def tension(self, stage, scope, *memberships, status="unresolved"):
        self.tensions.append(ad._TensionRecord(self.run_id, stage, scope, status, tuple(memberships)))
        self.tension_state = "open"

    def validated(self):
        return ValidatedCompetencyState(
            user_id="u", competency_code=self.code, current_stage=self.current_stage,
            tension_state=self.tension_state, active_inference_run_id=self.run_id,
            longitudinal_assessment_run_id=self.parent_id, state_generation=self.state_generation)

    def acquired(self):
        claims = tuple(ad._ClaimRecord(uid(f"claim-{stage}"), self.run_id, stage, *self.claims[stage])
                       for stage in STAGES)
        # Refs non positive_basis : ignorées pour le périmètre.
        refs = (*self.refs, ad._RefRecord(self.run_id, None, "validation", "observation", uid("o-ignored")),
                ad._RefRecord(self.run_id, uid("claim-application"), "confidence", "dependency", None))
        return ad._Acquired(
            run=ad._RunRecord(self.run_id, "u", self.code, self.parent_id, "completed", self.interpretation_status,
                              self.current_stage, self.tension_state, self.context, list(self.needs)),
            parent=ad._ParentRecord(self.parent_id, "u", self.code, self.release),
            release_capabilities=tuple(self.capabilities),
            claims=claims, tensions=tuple(self.tensions), refs=refs,
            snapshot_observation_ids=frozenset(self.snapshot),
            observations=MappingProxyType(dict(self.observations)))

    def project(self, acquired=None, validated=None):
        return ad._project_competency(validated or self.validated(), acquired or self.acquired())

    def claim(self, stage, snapshot=None):
        snapshot = snapshot or self.project()
        return next(c for c in snapshot.claims if c.stage == stage)


def application_world():
    """Application directe localized C8_A/B/C, Comprehension / Discovery
    implied, Mastery not_established."""
    w = World()
    o = w.obs("app", "C", "A", "B")
    w.direct("application", o)
    w.implied("comprehension")
    w.implied("discovery")
    w.current_stage = "application"
    return w


# --------------------------------------------------------------------------
# 1. Projection pure
# --------------------------------------------------------------------------

def test_direct_localized_application_represents_exactly_its_compatible_definitions():
    w = application_world()
    snapshot = w.project()
    application = w.claim("application", snapshot)
    assert dataclasses.replace(application, confidence_profile=None) == AdaptationStageClaim(
        stage="application", status="established", basis_mode="direct",
        represented_capability_definition_ids=(w.d("A"), w.d("B"), w.d("C")), competency_only_basis=False,
        confidence_profile=None, mastery_assessment=None)
    assert thaw(application.confidence_profile) == profile()
    assert [c.stage for c in snapshot.claims] == list(STAGES)
    assert snapshot.capabilities == tuple(CapabilitySemanticRef(
        membership_id=w.m(x), definition_id=w.d(x), capability_code=f"C8_{x}", semantic_revision=1,
        label=f"label C8_{x}") for x in "ABCD")


def test_direct_competency_only_application_invents_no_capability():
    w = World()
    w.direct("application", w.obs("only", localization="competency_only"))
    w.current_stage = "application"
    claim = w.claim("application")
    assert (claim.represented_capability_definition_ids, claim.competency_only_basis) == ((), True)


def test_direct_claim_can_be_both_localized_and_competency_only():
    w = World()
    w.direct("application", w.obs("loc", "A", "B"), w.obs("only", localization="competency_only"))
    w.current_stage = "application"
    claim = w.claim("application")
    assert (claim.represented_capability_definition_ids, claim.competency_only_basis) == ((w.d("A"), w.d("B")),
                                                                                          True)


def test_competency_only_observation_with_a_mapping_fails_closed():
    w = World()
    w.direct("application", w.obs("only", "A", localization="competency_only"))
    w.current_stage = "application"
    with pytest.raises(InvalidAdaptationState, match="competency_only"):
        w.project()


def test_implied_claims_inherit_exactly_the_application_scope():
    w = application_world()
    snapshot = w.project()
    for stage in ("discovery", "comprehension"):
        claim = w.claim(stage, snapshot)
        assert (claim.basis_mode, claim.represented_capability_definition_ids, claim.competency_only_basis) == (
            "implied_by_higher_claim", (w.d("A"), w.d("B"), w.d("C")), False)


def test_implied_discovery_inherits_the_closest_direct_claim_not_application():
    w = World()
    w.direct("application", w.obs("app", "A", "B", "C"))
    w.direct("comprehension", w.obs("comp", "B"))
    w.implied("discovery")
    w.current_stage = "application"
    snapshot = w.project()
    assert w.claim("discovery", snapshot).represented_capability_definition_ids == (w.d("B"),)
    assert w.claim("comprehension", snapshot).represented_capability_definition_ids == (w.d("B"),)
    assert w.claim("application", snapshot).represented_capability_definition_ids == (w.d("A"), w.d("B"), w.d("C"))


def test_implied_inherits_competency_only_flag_of_its_source():
    w = World()
    w.direct("application", w.obs("only", localization="competency_only"))
    w.implied("comprehension")
    w.current_stage = "application"
    claim = w.claim("comprehension")
    assert (claim.represented_capability_definition_ids, claim.competency_only_basis) == ((), True)


@pytest.mark.parametrize("stage", ["discovery", "comprehension", "mastery"])
def test_implied_without_a_higher_direct_claim_fails_closed(stage):
    w = World()
    w.implied(stage)
    if stage == "comprehension":
        w.implied("discovery")
    with pytest.raises(InvalidAdaptationState, match="implied_by_higher_claim sans claim directe"):
        w.project()


def test_implied_claim_with_its_own_positive_basis_fails_closed():
    w = application_world()
    w.refs.append(ad._RefRecord(w.run_id, uid("claim-comprehension"), "positive_basis", "observation",
                                w.obs("x", "A")))
    with pytest.raises(InvalidAdaptationState, match="positive_basis propres"):
        w.project()


def test_not_established_has_no_scope_no_flag_and_no_confidence():
    w = application_world()
    mastery = w.claim("mastery")
    assert (mastery.status, mastery.basis_mode, mastery.represented_capability_definition_ids,
            mastery.competency_only_basis, mastery.confidence_profile) == ("not_established", "none", (), False,
                                                                           None)


@pytest.mark.parametrize("tamper", ["profile", "ref", "mode"])
def test_not_established_inconsistencies_fail_closed(tamper):
    w = application_world()
    if tamper == "profile":
        w.claims["mastery"][2] = profile()
    elif tamper == "ref":
        w.refs.append(ad._RefRecord(w.run_id, uid("claim-mastery"), "positive_basis", "observation",
                                    w.obs("m", "A")))
    else:
        w.claims["mastery"][1] = "direct"
    with pytest.raises(InvalidAdaptationState):
        w.project()


def test_current_stage_is_never_turned_into_a_claim_scope():
    """Maintien sous tension : current_stage application, claim application
    not_established, comprehension established, revision context, tension
    ouverte. Rien n'est corrigé."""
    w = World()
    w.direct("comprehension", w.obs("comp", "A"))
    w.implied("discovery")
    w.current_stage = "application"
    w.context = {"schema_version": "revision-context-v1", "reason_code": "held", "motifs": [{"k": ["x"]}]}
    w.tension("application", "localized", w.m("C"), status="revalidation_needed")
    snapshot = w.project()
    assert (snapshot.current_stage, snapshot.tension_state) == ("application", "open")
    application = w.claim("application", snapshot)
    assert (application.status, application.represented_capability_definition_ids) == ("not_established", ())
    assert w.claim("comprehension", snapshot).represented_capability_definition_ids == (w.d("A"),)
    assert thaw(snapshot.unresolved_revision_context) == w.context


def test_direct_claim_without_positive_basis_fails_closed():
    w = World()
    w.direct("application")
    with pytest.raises(InvalidAdaptationState, match="sans ref positive_basis"):
        w.project()


@pytest.mark.parametrize("tamper", ["contradictory", "outside_snapshot", "other_competency", "no_source_release",
                                    "relation_source", "unknown_localization"])
def test_positive_basis_observation_inconsistencies_fail_closed(tamper):
    w = World()
    if tamper == "contradictory":
        o = w.obs("o", "A", polarity="contradictory")
    elif tamper == "outside_snapshot":
        o = w.obs("o", "A", in_snapshot=False)
    elif tamper == "no_source_release":
        o = w.obs("o", "A")
        w.observations[o] = w.observations[o]._replace(source_taxonomy_release_id=None)
    elif tamper == "unknown_localization":
        o = w.obs("o", "A", localization="somewhere")
    else:
        o = w.obs("o", "A")
        if tamper == "other_competency":
            w.observations[o] = w.observations[o]._replace(competency_code="C9")
    w.direct("application", o)
    if tamper == "relation_source":
        w.refs[-1] = w.refs[-1]._replace(source_kind="transfer", source_observation_id=None)
    w.current_stage = "application"
    with pytest.raises(InvalidAdaptationState):
        w.project()


def test_historical_localized_observation_keeps_only_definitions_of_the_current_release():
    """Observation évaluée sous une ANCIENNE release : ses memberships sont
    autres, mais ses definition_id A et B existent dans la release courante
    (compatibles) ; son C8_C d'une autre révision n'y existe pas : jamais
    remappé vers le C8_C courant par capability_code."""
    w = World()
    old = uid("old-release")
    o = w.obs("hist", source_release=old, mappings=(
        ad._MappingRecord(old, w.d("A"), "C8"), ad._MappingRecord(old, w.d("B"), "C8"),
        ad._MappingRecord(old, uid("d-C8_C-r2"), "C8"),
        # mapping d'une autre release que celle de SON run T3 : jamais retenu.
        ad._MappingRecord(w.release, w.d("D"), "C8")))
    w.direct("application", o)
    w.current_stage = "application"
    assert w.claim("application").represented_capability_definition_ids == (w.d("A"), w.d("B"))


def test_localized_observation_without_any_compatible_definition_fails_closed():
    w = World()
    old = uid("old-release")
    o = w.obs("hist", source_release=old, mappings=(ad._MappingRecord(old, uid("d-C8_A-r2"), "C8"),))
    w.direct("application", o)
    w.current_stage = "application"
    with pytest.raises(InvalidAdaptationState, match="aucune capacité compatible"):
        w.project()


# --- Mastery ---------------------------------------------------------------

def mastery_world(*represented):
    w = World()
    a = w.obs("a", "A", "B", "C")
    b = w.obs("b", "A", "B")
    w.direct("mastery", a, b, assessment=mastery_payload(*represented))
    for stage in ("application", "comprehension", "discovery"):
        w.implied(stage)
    w.current_stage = "mastery"
    return w


def test_mastery_scope_is_the_structured_assessment_validated_against_the_current_release():
    w = mastery_world(uid("d-C8_B"), uid("d-C8_A"))
    snapshot = w.project()
    mastery = w.claim("mastery", snapshot)
    # Le périmètre Mastery n'est PAS l'union A/B/C des refs : celui décidé par T6-C1.
    assert mastery.represented_capability_definition_ids == (w.d("A"), w.d("B"))
    assert thaw(mastery.mastery_assessment) == mastery_payload(w.d("B"), w.d("A"))
    assert isinstance(mastery.mastery_assessment, MappingProxyType)
    for stage in ("application", "comprehension", "discovery"):
        assert w.claim(stage, snapshot).represented_capability_definition_ids == (w.d("A"), w.d("B"))


@pytest.mark.parametrize("represented", [
    (uid("d-C8_A-r2"),),  # même code, autre révision : jamais remappé
    (uid("d-C9_A"),),     # autre compétence
    (uid("d-C8_D"),),     # hors du périmètre compatible de ses refs positive_basis
])
def test_mastery_definition_outside_current_scope_fails_closed(represented):
    with pytest.raises(InvalidAdaptationState):
        mastery_world(*represented).project()


@pytest.mark.parametrize("tamper", ["missing", "schema", "score_key", "not_uuid", "on_application"])
def test_mastery_assessment_format_fails_closed(tamper):
    w = mastery_world(uid("d-C8_A"))
    payload = w.claims["mastery"][3]
    if tamper == "missing":
        w.claims["mastery"][3] = None
    elif tamper == "schema":
        payload["schema_version"] = "mastery-assessment-v2"
    elif tamper == "score_key":
        payload["mastery_score"] = "4/5"
    elif tamper == "not_uuid":
        payload["represented_capability_definition_ids"] = ["C8_A"]
    else:
        w.claims["application"][3] = mastery_payload()
    with pytest.raises(InvalidAdaptationState):
        w.project()


def test_not_established_mastery_keeps_its_assessment_but_no_scope():
    w = application_world()
    w.claims["mastery"][3] = mastery_payload(w.d("A"), status="not_demonstrated")
    mastery = w.claim("mastery")
    assert mastery.represented_capability_definition_ids == ()
    assert mastery.mastery_assessment["represented_capability_definition_ids"] == (str(w.d("A")),)


# --- confiance, contexte de révision ----------------------------------------

def test_confidence_profile_is_kept_separated_and_never_aggregated():
    w = application_world()
    stored = profile(w.d("A"), facts=("independence_not_established", "single_representative_episode"))
    w.claims["application"][2] = stored
    claim = w.claim("application")
    assert thaw(claim.confidence_profile) == stored
    assert set(claim.confidence_profile) == {"schema_version", *DIMENSIONS}
    assert claim.confidence_profile["independence"]["fact_codes"] == ("independence_not_established",
                                                                      "single_representative_episode")
    with pytest.raises(TypeError):
        claim.confidence_profile["coverage"] = {}


@pytest.mark.parametrize("tamper", ["missing_dimension", "score", "schema", "flat"])
def test_confidence_profile_format_fails_closed(tamper):
    w = application_world()
    stored = w.claims["application"][2]
    if tamper == "missing_dimension":
        del stored["temporal_validation"]
    elif tamper == "score":
        stored["confidence_score"] = 0.8
    elif tamper == "schema":
        stored["schema_version"] = "confidence-profile-v2"
    else:
        stored["coverage"] = "low"
    with pytest.raises(InvalidAdaptationState):
        w.project()


def test_payloads_are_defensive_immutable_copies():
    w = application_world()
    w.context = {"schema_version": "revision-context-v1", "motifs": [{"ids": ["a"]}]}
    w.current_stage = "application"
    acquired = w.acquired()
    snapshot = w.project(acquired)
    acquired.run.unresolved_revision_context["motifs"][0]["ids"].append("b")
    acquired.claims[2].confidence_profile["coverage"]["fact_codes"].append("mutated")
    assert snapshot.unresolved_revision_context["motifs"][0]["ids"] == ("a",)
    assert "mutated" not in w.claim("application", snapshot).confidence_profile["coverage"]["fact_codes"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.current_stage = "mastery"
    with pytest.raises(TypeError):
        snapshot.unresolved_revision_context["reason_code"] = "x"


@pytest.mark.parametrize("value", [{}, ["not", "an", "object"], {"k": {1, 2}}])
def test_revision_context_must_be_a_json_object(value):
    w = application_world()
    w.context = value
    with pytest.raises(InvalidAdaptationState):
        w.project()


# --- tensions ---------------------------------------------------------------

def test_localized_tension_resolves_exact_definitions_in_taxonomy_order():
    w = application_world()
    w.tension("application", "localized", w.m("C"), w.m("A"), status="revalidation_needed")
    assert w.project().tensions == (AdaptationTension(
        fragilized_stage="application", scope_mode="localized", capability_definition_ids=(w.d("A"), w.d("C")),
        revision_status="revalidation_needed"),)


@pytest.mark.parametrize("scope", ["whole_competency", "competency_only"])
def test_unlocalized_tensions_have_no_capability(scope):
    w = application_world()
    w.tension("application", scope)
    (tension,) = w.project().tensions
    assert (tension.scope_mode, tension.capability_definition_ids) == (scope, ())


@pytest.mark.parametrize("memberships, match", [
    ((), "sans membership"),
    ((uid("m-unknown"),), "absent de la release"),
    ((uid("m-C9_A"),), "de C9"),
    ((uid("m-C8_A"), uid("m-C8_A")), "en double"),
])
def test_localized_tension_inconsistencies_fail_closed(memberships, match):
    w = application_world()
    w.tension("application", "localized", *memberships)
    with pytest.raises(InvalidAdaptationState, match=match):
        w.project()


@pytest.mark.parametrize("scope", ["whole_competency", "competency_only"])
def test_unlocalized_tension_with_membership_is_never_converted(scope):
    w = application_world()
    w.tension("application", scope, w.m("A"))
    with pytest.raises(InvalidAdaptationState):
        w.project()


def test_tension_state_and_tensions_must_agree():
    w = application_world()
    w.tension_state = "open"
    with pytest.raises(InvalidAdaptationState, match="tension_state"):
        w.project()
    w = application_world()
    w.tension("application", "competency_only")
    w.tension_state = "none"
    with pytest.raises(InvalidAdaptationState):
        w.project()


# --- validation needs --------------------------------------------------------

def test_localized_validation_need_on_a_current_definition_is_accepted():
    w = application_world()
    w.needs = [need("revalidation", "application", "localized", w.d("C"), w.d("A"))]
    assert w.project().validation_needs == (AdaptationValidationNeed(
        intent="revalidation", target_stage="application", scope_mode="localized",
        capability_definition_ids=(w.d("A"), w.d("C")), reason_codes=("unresolved_revision_motif",)),)


def test_validation_need_on_an_old_definition_with_the_same_code_is_never_remapped():
    w = application_world()
    w.needs = [need("revalidation", "application", "localized", uid("d-C8_A-r2"))]
    with pytest.raises(InvalidAdaptationState, match="aucun remapping"):
        w.project()


@pytest.mark.parametrize("scope", ["whole_competency", "competency_only"])
def test_unlocalized_validation_needs_have_no_capability(scope):
    w = application_world()
    w.needs = [need("revalidation", "application", scope)]
    (item,) = w.project().validation_needs
    assert (item.scope_mode, item.capability_definition_ids) == (scope, ())


@pytest.mark.parametrize("tamper", ["localized_empty", "competency_only_ids", "intent", "stage", "scope", "schema",
                                    "extra_key", "priority", "reason", "no_reason", "duplicate", "not_list"])
def test_validation_need_format_fails_closed(tamper):
    w = application_world()
    item = need("confirmation", "application", "localized", w.d("A"))
    if tamper == "localized_empty":
        item["capability_definition_ids"] = []
    elif tamper == "competency_only_ids":
        item["scope_mode"] = "competency_only"
    elif tamper == "intent":
        item["intent"] = "question"
    elif tamper == "stage":
        item["target_stage"] = "non_etabli"
    elif tamper == "scope":
        item["scope_mode"] = "whole_observation"
    elif tamper == "schema":
        item["schema_version"] = "validation-need-v2"
    elif tamper == "extra_key":
        item["question"] = "Pouvez-vous expliquer ?"
    elif tamper == "priority":
        item["priority"] = "high"
    elif tamper == "reason":
        item["reason_codes"] = ["user_is_weak"]
    elif tamper == "no_reason":
        item["reason_codes"] = []
    w.needs = [item, dict(item)] if tamper == "duplicate" else [item]
    acquired = w.acquired()
    if tamper == "not_list":
        acquired = acquired._replace(run=acquired.run._replace(validation_needs={"intent": "confirmation"}))
    with pytest.raises(InvalidAdaptationState):
        w.project(acquired)


def test_validation_needs_canonical_order():
    w = application_world()
    w.needs = [need("revalidation", "comprehension", "whole_competency"),
               need("revalidation", "application", "localized", w.d("C")),
               need("revalidation", "application", "localized", w.d("A")),
               need("confirmation", "application", "competency_only",
                    reasons=["independence_not_established_on_current_basis",
                             "competency_only_basis_would_benefit_from_localization"][::-1])]
    needs = w.project().validation_needs
    assert [(n.intent, n.target_stage, n.scope_mode, n.capability_definition_ids) for n in needs] == [
        ("confirmation", "application", "competency_only", ()),
        ("revalidation", "comprehension", "whole_competency", ()),
        ("revalidation", "application", "localized", (w.d("A"),)),
        ("revalidation", "application", "localized", (w.d("C"),))]
    assert needs[0].reason_codes == ("independence_not_established_on_current_basis",
                                     "competency_only_basis_would_benefit_from_localization")


# --- run capturé, ordres, déterminisme ----------------------------------------

@pytest.mark.parametrize("field, value", [
    ("id", uid("other-run")), ("user_id", "u2"), ("competency_code", "C9"),
    ("longitudinal_assessment_run_id", uid("other-t5")), ("current_stage", "mastery"), ("tension_state", "open"),
    ("execution_status", "running"), ("interpretation_status", "candidate"), ("interpretation_status", "obsolete"),
])
def test_captured_run_must_match_the_validated_state(field, value):
    w = application_world()
    acquired = w.acquired()
    acquired = acquired._replace(run=acquired.run._replace(**{field: value}))
    with pytest.raises(InvalidAdaptationState):
        w.project(acquired)


def test_captured_run_superseded_after_capture_keeps_its_own_snapshot():
    w = application_world()
    expected = w.project()
    w.interpretation_status = "superseded"
    assert w.project() == expected


@pytest.mark.parametrize("tamper", ["parent", "child_run", "claim_count", "duplicate_claim", "release",
                                    "duplicate_definition"])
def test_structural_inconsistencies_fail_closed(tamper):
    w = application_world()
    acquired = w.acquired()
    if tamper == "parent":
        acquired = acquired._replace(parent=acquired.parent._replace(user_id="u2"))
    elif tamper == "child_run":
        acquired = acquired._replace(claims=(acquired.claims[0]._replace(inference_run_id=uid("x")),
                                             *acquired.claims[1:]))
    elif tamper == "claim_count":
        acquired = acquired._replace(claims=acquired.claims[1:])
    elif tamper == "duplicate_claim":
        acquired = acquired._replace(claims=(*acquired.claims[:3], acquired.claims[2]))
    elif tamper == "release":
        acquired = acquired._replace(release_capabilities=(
            acquired.release_capabilities[0]._replace(taxonomy_release_id=uid("x")),
            *acquired.release_capabilities[1:]))
    else:
        acquired = acquired._replace(release_capabilities=(
            *acquired.release_capabilities,
            acquired.release_capabilities[0]._replace(membership_id=uid("m-dup"), semantic_revision=2)))
    with pytest.raises(InvalidAdaptationState):
        w.project(acquired)


def test_state_generation_is_kept_but_never_drives_any_computation():
    w = application_world()
    w.tension("comprehension", "localized", w.m("B"))
    w.needs = [need("revalidation", "comprehension", "localized", w.d("B"))]
    reference = w.project()
    assert reference.state_generation == 1
    for generation in (2, 7, 10 ** 12):
        w.state_generation = generation
        projected = w.project()
        assert projected.state_generation == generation
        assert dataclasses.replace(projected, state_generation=1) == reference
    w.state_generation = 0
    with pytest.raises(InvalidAdaptationState):
        w.project()


def _rich_world():
    w = World()
    w.direct("application", w.obs("app", "A", "C"), w.obs("only", localization="competency_only"))
    w.direct("comprehension", w.obs("comp", "B", "A"))
    w.implied("discovery")
    w.current_stage = "application"
    w.tension("application", "localized", w.m("C"), w.m("A"))
    w.tension("application", "whole_competency")
    w.tension("discovery", "competency_only", status="revalidation_needed")
    w.tension("application", "localized", w.m("B"), status="revalidation_needed")
    w.needs = [need("revalidation", "application", "localized", w.d("B")),
               need("confirmation", "application", "competency_only"),
               need("revalidation", "discovery", "competency_only")]
    w.context = {"schema_version": "revision-context-v1", "motifs": []}
    return w


def test_collection_order_never_changes_the_snapshot():
    w = _rich_world()
    reference = w.project()
    assert [(t.fragilized_stage, t.scope_mode, t.capability_definition_ids) for t in reference.tensions] == [
        ("discovery", "competency_only", ()),
        ("application", "localized", (w.d("A"), w.d("C"))), ("application", "localized", (w.d("B"),)),
        ("application", "whole_competency", ())]
    rng = random.Random(61)
    for _ in range(25):
        acquired = w.acquired()
        shuffled = acquired._replace(
            claims=tuple(rng.sample(acquired.claims, len(acquired.claims))),
            tensions=tuple(t._replace(capability_membership_ids=tuple(rng.sample(
                t.capability_membership_ids, len(t.capability_membership_ids))))
                for t in rng.sample(acquired.tensions, len(acquired.tensions))),
            refs=tuple(rng.sample(acquired.refs, len(acquired.refs))),
            run=acquired.run._replace(validation_needs=rng.sample(acquired.run.validation_needs,
                                                                  len(acquired.run.validation_needs))),
            observations=MappingProxyType({k: v._replace(mappings=tuple(rng.sample(v.mappings, len(v.mappings))))
                                           for k, v in rng.sample(list(acquired.observations.items()),
                                                                  len(acquired.observations))}))
        assert w.project(shuffled) == reference


def test_same_records_same_snapshot():
    w = _rich_world()
    assert w.project() == w.project()


def test_requested_codes_follow_the_conceptual_order():
    assert COMPETENCY_ORDER == tuple(sorted(svc.COMPETENCY_CODES, key=lambda code: int(code[1:])))
    assert ad._requested_codes(None) == COMPETENCY_ORDER
    assert ad._requested_codes(("C10", "C2", "C8")) == ("C2", "C8", "C10")
    assert ad._requested_codes(["C12", "C1"]) == ("C1", "C12")
    assert ad._requested_codes(()) == ()


@pytest.mark.parametrize("value", [("C13",), ("c8",), ("C8", "C8"), "C8", {"C8"}, (8,), ("C8_A",)])
def test_requested_codes_are_validated(value):
    with pytest.raises(InvalidAdaptationArgument):
        load_adaptation_state(_NoDB(), user_id="u", competency_codes=value)


@pytest.mark.parametrize("value", ["", " ", None, 42, "a\x00b"])
def test_user_id_is_validated_before_the_database(value):
    with pytest.raises(InvalidAdaptationArgument):
        load_adaptation_state(_NoDB(), user_id=value)


class _NoDB:
    def __getattr__(self, name):
        raise AssertionError(f"accès à la base inattendu : {name}")


class _FakeDB:
    """Session factice : seul no_autoflush est attendu (lectures mockées)."""

    @property
    def no_autoflush(self):
        return contextlib.nullcontext()


@pytest.mark.parametrize("raised, expected", [
    (svc.StaleInferenceChain("T5 superseded"), StaleAdaptationState),
    (svc.InvalidInferenceState("cache divergent"), InvalidAdaptationState),
    (svc.UserNotFound("u"), AdaptationUserNotFound),
    (svc.InvalidInferenceArgument("competency_code"), InvalidAdaptationArgument),
])
def test_strict_loader_translates_step5_errors_and_returns_nothing(monkeypatch, raised, expected):
    def boom(db, *, user_id, competency_code):
        raise raised
    monkeypatch.setattr(ad, "get_validated_user_competency_state", boom)
    with pytest.raises(expected) as info:
        load_adaptation_state(_FakeDB(), user_id="u", competency_codes=("C8",))
    assert info.value.__cause__ is raised
    assert issubclass(expected, AdaptationStateError)


def test_loader_orders_present_competencies_and_skips_absent_ones(monkeypatch):
    worlds = {code: World(code) for code in ("C2", "C10")}
    for w in worlds.values():
        w.direct("application", w.obs("app", "A", "B"))
        w.current_stage = "application"
    calls = []

    def validated(db, *, user_id, competency_code):
        calls.append(competency_code)
        return worlds[competency_code].validated() if competency_code in worlds else None

    monkeypatch.setattr(ad, "get_validated_user_competency_state", validated)
    monkeypatch.setattr(ad, "_acquire", lambda db, v: worlds[v.competency_code].acquired())
    snapshot = load_adaptation_state(_FakeDB(), user_id="u")
    assert calls == list(COMPETENCY_ORDER)
    assert [c.competency_code for c in snapshot.competencies] == ["C2", "C10"]
    calls.clear()
    assert [c.competency_code for c in load_adaptation_state(
        _FakeDB(), user_id="u", competency_codes=("C10", "C8", "C2")).competencies] == ["C2", "C10"]
    assert calls == ["C2", "C8", "C10"]


# --------------------------------------------------------------------------
# 2. Contrat statique
# --------------------------------------------------------------------------

def _tree():
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


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
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
    return names


def test_public_api_is_exactly_load_adaptation_state():
    public = {n.name for n in _tree().body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == {"load_adaptation_state"}
    params = list(inspect.signature(load_adaptation_state).parameters.values())
    assert [p.name for p in params] == ["db", "user_id", "competency_codes"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:])
    assert params[2].default is None


def test_structures_are_frozen_keyword_only_dataclasses_with_the_frozen_fields():
    expected = {
        AdaptationStateSnapshot: ["user_id", "competencies"],
        CompetencyAdaptationSnapshot: ["competency_code", "current_stage", "tension_state",
                                       "active_inference_run_id", "longitudinal_assessment_run_id",
                                       "state_generation", "taxonomy_release_id", "capabilities", "claims",
                                       "tensions", "unresolved_revision_context", "validation_needs"],
        CapabilitySemanticRef: ["membership_id", "definition_id", "capability_code", "semantic_revision", "label"],
        AdaptationStageClaim: ["stage", "status", "basis_mode", "represented_capability_definition_ids",
                               "competency_only_basis", "confidence_profile", "mastery_assessment"],
        AdaptationTension: ["fragilized_stage", "scope_mode", "capability_definition_ids", "revision_status"],
        AdaptationValidationNeed: ["intent", "target_stage", "scope_mode", "capability_definition_ids",
                                   "reason_codes"],
    }
    for cls, names in expected.items():
        assert [f.name for f in dataclasses.fields(cls)] == names
        assert cls.__dataclass_params__.frozen and all(f.kw_only for f in dataclasses.fields(cls))


def test_errors_are_a_dedicated_business_hierarchy():
    for cls in (InvalidAdaptationArgument, AdaptationUserNotFound, InvalidAdaptationState, StaleAdaptationState):
        assert issubclass(cls, AdaptationStateError)
    assert not issubclass(AdaptationStateError, svc.InferenceServiceError)


def test_no_database_write_no_sql_and_no_orm_model():
    names = _identifiers()
    for forbidden in ("add_all", "flush", "commit", "rollback", "delete", "merge", "execute", "insert",
                      "begin_nested", "with_for_update", "select", "text", "query", "refresh", "expire"):
        assert forbidden not in names, forbidden
    # La session n'est utilisée que pour no_autoflush ; tout le reste passe
    # par les lectures des services propriétaires.
    used = {node.attr for node in ast.walk(_tree())
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "db"}
    assert used == {"no_autoflush"}
    imports = {node.module for node in ast.walk(_tree()) if isinstance(node, ast.ImportFrom)}
    assert not {"sqlalchemy", "core.models", "sqlalchemy.orm"} & imports


def test_no_reinference_and_no_raw_cache_read():
    names = _identifiers()
    for forbidden in ("infer_competency", "evaluate_positive_basis", "evaluate_inference_state",
                      "evaluate_validation_needs", "resolve_transition_cause", "get_user_competency_state",
                      "get_active_competency_inference", "start_competency_inference", "complete_competency_inference",
                      "fail_competency_inference", "get_inference_context", "build_longitudinal_dossier",
                      "_build_current_taxonomy_context", "_freeze", "_active_and_cache"):
        assert forbidden not in names, forbidden
    assert "get_validated_user_competency_state" in names


def test_audit_summaries_are_never_read():
    assert not [name for name in _identifiers() if "summary" in name.lower()]
    for record in (ad._ClaimRecord, ad._TensionRecord, ad._RunRecord):
        assert not [f for f in record._fields if "summary" in f]


def test_no_level_score_or_progression_vocabulary():
    tokens = {token for name in _identifiers() for token in name.lower().split("_")}
    for forbidden in ("level", "beginner", "advanced", "score", "weight", "probability", "percentage", "percent",
                      "average", "xp", "streak", "priority", "recommendation", "progress", "completion", "weak",
                      "strong", "overall", "global", "ratio"):
        assert forbidden not in tokens, forbidden


def test_no_later_step6_brick():
    names = _identifiers()
    for forbidden in ("InteractionContext", "target_competency", "supporting_competencies", "SafeAssumption",
                      "ValidationOpportunity", "PedagogicalResponseContext", "PosturePlan", "next_useful_move",
                      "SupportTrace", "add_support_trace"):
        assert forbidden not in names, forbidden


def test_no_migration_no_step6_model_and_not_wired_to_the_runtime():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions[-1] == "0009_competency_inference_state.py" and len(versions) == 9
    models = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    assert "Adaptation" not in models
    # Étape 6.1C : socle positif localisé non branché, consommateur des seuls
    # contrats publics du snapshot (jamais load_adaptation_state, jamais un
    # privé) ; lui-même non branché : tests/test_adaptation_assumptions.py.
    # Étape 6.1D : présupposés sûrs, consommateur des seuls contrats publics
    # du snapshot et de ses fragilités / besoins ; non branché :
    # tests/test_adaptation_safety.py.
    # Étape 6.1E : contexte pédagogique, ordre C1 -> C12 seulement (aucun
    # état Step 5 lu) ; non branché : tests/test_adaptation_context.py.
    # Étape 6.4A : projection descriptive de l'état actuel, consommatrice des
    # seuls contrats publics du snapshot (stade, claim courante, catalogue ;
    # jamais load_adaptation_state) ; non branchée :
    # tests/test_progress_projection.py.
    # Étape 6.4B1 : acquisition des preuves de la carte 6-4A, consommatrice
    # des seuls contrats publics du snapshot (jamais load_adaptation_state) ;
    # non branchée : tests/test_progress_evidence.py.
    step6_consumers = {"core/adaptation_assumptions.py": {
        "COMPETENCY_ORDER", "AdaptationStageClaim", "AdaptationStateSnapshot", "CapabilitySemanticRef",
        "CompetencyAdaptationSnapshot"},
        "core/adaptation_safety.py": {"AdaptationStateSnapshot", "AdaptationTension", "AdaptationValidationNeed"},
        "core/adaptation_context.py": {"COMPETENCY_ORDER"},
        "core/progress_projection.py": {
        "COMPETENCY_ORDER", "AdaptationStageClaim", "AdaptationStateSnapshot", "CapabilitySemanticRef",
        "CompetencyAdaptationSnapshot"},
        "core/progress_evidence.py": {
        "COMPETENCY_ORDER", "AdaptationStageClaim", "AdaptationStateSnapshot", "CompetencyAdaptationSnapshot"},
        # 6-4B2 : seul le vocabulaire des compétences, jamais le snapshot.
        "core/progress_evidence_selection.py": {"COMPETENCY_ORDER"},
        # 6-4B3 : idem (tests/test_progress_evidence_rendering.py).
        "core/progress_evidence_rendering.py": {"COMPETENCY_ORDER"}}
    for rel, names in step6_consumers.items():
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        imported = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names if "adaptation_state" in f"{getattr(n, 'module', '')}.{a.name}"]
        assert sorted(imported) == sorted(("core.adaptation_state", name) for name in names), rel
    users = [p.relative_to(REPO_ROOT).as_posix() for p in REPO_ROOT.rglob("*")
             if p.suffix in (".py", ".js", ".html") and p.is_file() and not {".git", "tests", "node_modules"} & set(
                 p.relative_to(REPO_ROOT).parts) and "adaptation_state" in p.read_text(encoding="utf-8", errors="replace")]
    assert sorted(users) == sorted(["core/adaptation_state.py", *step6_consumers])


# --------------------------------------------------------------------------
# 3. Intégration PostgreSQL : T3 / T4 / T5 / T6 réels -> 6-1A
# --------------------------------------------------------------------------

T6_TABLES = ("competency_inference_runs", "competency_stage_claims", "competency_inference_tensions",
             "competency_inference_tension_capabilities", "competency_inference_basis_refs", "user_competency_states")


def _load(Sessions, **kwargs):  # noqa: N803
    with Sessions() as session:
        snapshot = load_adaptation_state(session, user_id="u", **kwargs)
        assert not session.new and not session.dirty and not session.deleted
        return snapshot


def _dump(engine):  # noqa: F811
    with engine.connect() as conn:
        return {table: sorted(map(repr, conn.execute(sa.text(f"SELECT * FROM {table}")).all()))
                for table in T6_TABLES}


def _sql(engine, statement, **params):  # noqa: F811
    with engine.begin() as conn:
        conn.execute(sa.text(statement), params)


def _definitions(p):
    from core import models
    with p.Sessions() as session:
        return {code: session.get(models.CapabilityTaxonomyMembership, m).capability_definition_id
                for code, m in p.m.items()}


def _need(payload, d):
    by_id = {str(v): v for v in d.values()}
    return AdaptationValidationNeed(intent=payload["intent"], target_stage=payload["target_stage"],
                                    scope_mode=payload["scope_mode"],
                                    capability_definition_ids=tuple(by_id[i] for i in
                                                                    payload["capability_definition_ids"]),
                                    reason_codes=tuple(payload["reason_codes"]))


def test_pg_end_to_end_first_inference_snapshot(Sessions, engine):  # noqa: F811
    """CognitiveEvent -> T3 -> T4 -> T5 -> T6 (infer + complete) -> COMMIT ->
    load_adaptation_state : stade, claims, périmètres, taxonomie, besoins,
    tensions, provenance technique, aucune mutation."""
    p = Pipeline(Sessions)
    d = _definitions(p)
    p.t3(p.app("C7_A", "C7_B", "C7_C", evidence_strength="strong"))
    context, decision = p.infer()
    before = _dump(engine)
    statements = []

    def record(conn, cursor, sql, *args):
        statements.append(sql)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        snapshot = _load(Sessions)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert _dump(engine) == before
    assert statements and all(" ".join(s.split()).startswith("SELECT") and "FOR " not in s for s in statements)

    assert snapshot.user_id == "u"
    (c7,) = snapshot.competencies
    assert (c7.competency_code, c7.current_stage, c7.tension_state, c7.state_generation) == (
        "C7", "application", "none", 1)
    assert (c7.active_inference_run_id, c7.longitudinal_assessment_run_id, c7.taxonomy_release_id) == (
        context.run_id, context.longitudinal_assessment_run_id, p.release_id)
    assert c7.capabilities == tuple(CapabilitySemanticRef(
        membership_id=c.membership_id, definition_id=c.definition_id, capability_code=c.capability_code,
        semantic_revision=c.semantic_revision, label=c.label) for c in context.current_taxonomy_context.capabilities)
    assert [c.capability_code for c in c7.capabilities] == ["C7_A", "C7_B", "C7_C", "C7_D"]
    abc = (d["C7_A"], d["C7_B"], d["C7_C"])
    assert [(c.stage, c.status, c.basis_mode, c.represented_capability_definition_ids, c.competency_only_basis)
            for c in c7.claims] == [
        ("discovery", "established", "implied_by_higher_claim", abc, False),
        ("comprehension", "established", "implied_by_higher_claim", abc, False),
        ("application", "established", "direct", abc, False),
        ("mastery", "not_established", "none", (), False)]
    for claim, decided in zip(c7.claims, decision.claims):
        assert thaw(claim.confidence_profile) == decided.confidence_profile
        assert thaw(claim.mastery_assessment) == decided.mastery_assessment
    assert c7.tensions == () and c7.unresolved_revision_context is None
    assert c7.validation_needs == tuple(_need(n, d) for n in decision.validation_needs)
    assert c7.validation_needs  # confirmation latente de T6-C3, jamais une question
    assert _load(Sessions) == snapshot  # même état -> même snapshot


def test_pg_absent_competency_is_never_non_etabli_and_real_non_etabli_is_present(Sessions):  # noqa: F811
    assert _load(Sessions) == AdaptationStateSnapshot(user_id="u", competencies=())
    p = Pipeline(Sessions)
    p.t3(p.app("C7_D", local_stage="none"))
    p.infer()
    snapshot = _load(Sessions)
    (c7,) = snapshot.competencies
    assert (c7.competency_code, c7.current_stage) == ("C7", "non_etabli")
    assert all(c.status == "not_established" and c.represented_capability_definition_ids == () for c in c7.claims)
    assert _load(Sessions, competency_codes=("C8",)).competencies == ()
    assert [c.competency_code for c in _load(Sessions, competency_codes=("C8", "C7")).competencies] == ["C7"]
    with Sessions() as session:
        with pytest.raises(AdaptationUserNotFound):
            load_adaptation_state(session, user_id="nobody", competency_codes=("C7",))


def test_pg_competency_only_and_mixed_direct_bases(Sessions):  # noqa: F811
    from tests.test_longitudinal_service import only
    p = Pipeline(Sessions)
    d = _definitions(p)
    p.t3(only(p.app(evidence_strength="strong")))
    _, decision = p.infer()
    (c7,) = _load(Sessions).competencies
    application = c7.claims[2]
    assert (application.status, application.basis_mode, application.represented_capability_definition_ids,
            application.competency_only_basis) == ("established", "direct", (), True)
    assert all(n.capability_definition_ids == () for n in c7.validation_needs
               if n.scope_mode == "competency_only")
    assert c7.validation_needs == tuple(_need(n, d) for n in decision.validation_needs)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    (c7,) = _load(Sessions).competencies
    assert (c7.claims[2].represented_capability_definition_ids, c7.claims[2].competency_only_basis) == (
        (d["C7_A"], d["C7_B"]), True)


def test_pg_implied_discovery_inherits_the_direct_comprehension_scope(Sessions):  # noqa: F811
    from tests.test_longitudinal_service import sup
    p = Pipeline(Sessions)
    d = _definitions(p)
    p.t3(p.app("C7_A", "C7_D", evidence_strength="strong"))
    p.t3(sup(p.m["C7_A"], p.m["C7_B"], evidence_strength="strong"))
    _, decision = p.infer()
    assert [(c.stage, c.basis_mode) for c in decision.claims][:3] == [
        ("discovery", "implied_by_higher_claim"), ("comprehension", "direct"), ("application", "direct")]
    (c7,) = _load(Sessions).competencies
    assert [c.represented_capability_definition_ids for c in c7.claims] == [
        (d["C7_A"], d["C7_B"]), (d["C7_A"], d["C7_B"]), (d["C7_A"], d["C7_D"]), ()]


def _tensioned(Sessions):  # noqa: N803
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    p.infer()
    p.t3(p.contra("C7_A", contradiction_scope="application"))
    context, decision = p.infer()
    return p, context, decision


def test_pg_localized_tension_and_revalidation_need_are_exact(Sessions, engine):  # noqa: F811
    p, context, decision = _tensioned(Sessions)
    d = _definitions(p)
    before = _dump(engine)
    (c7,) = _load(Sessions).competencies
    assert _dump(engine) == before  # ni run, ni claims, ni tensions, ni refs, ni state_generation
    assert (c7.current_stage, c7.tension_state, c7.state_generation) == ("application", "open", 2)
    assert c7.tensions == (AdaptationTension(fragilized_stage="application", scope_mode="localized",
                                             capability_definition_ids=(d["C7_A"],),
                                             revision_status="revalidation_needed"),)
    assert ("revalidation", "application", "localized", (d["C7_A"],)) in [
        (n.intent, n.target_stage, n.scope_mode, n.capability_definition_ids) for n in c7.validation_needs]
    assert c7.validation_needs == tuple(_need(n, d) for n in decision.validation_needs)


def test_pg_falsified_audit_summaries_change_nothing(Sessions, engine):  # noqa: F811
    _, context, _ = _tensioned(Sessions)
    reference = _load(Sessions)
    _sql(engine, "UPDATE competency_stage_claims SET basis_summary = 'implied_by_higher_claim:mastery',"
                 " scope_summary = 'localized:C7_D@r1;competency_only' WHERE inference_run_id = :r", r=context.run_id)
    _sql(engine, "UPDATE competency_inference_runs SET state_decision_summary = 'current_stage=mastery;"
                 "state_reason=x;transition_cause=none;validation=none' WHERE id = :r", r=context.run_id)
    _sql(engine, "UPDATE competency_inference_tensions SET summary = 'mastery:whole_competency:x:C7_C@r1'"
                 " WHERE inference_run_id = :r", r=context.run_id)
    assert _load(Sessions) == reference


def test_pg_state_generation_magnitude_changes_nothing_else(Sessions, engine):  # noqa: F811
    _, context, _ = _tensioned(Sessions)
    (reference,) = _load(Sessions).competencies
    _sql(engine, "UPDATE user_competency_states SET state_generation = 987654321")
    (bumped,) = _load(Sessions).competencies
    assert bumped.state_generation == 987654321
    assert dataclasses.replace(bumped, state_generation=reference.state_generation) == reference


def test_pg_strict_loader_on_stale_chain_returns_no_snapshot(Sessions):  # noqa: F811
    from tests.test_longitudinal_service import _invalidate
    p = Pipeline(Sessions)
    a = p.t3(p.app("C7_A", "C7_B", evidence_strength="strong")).id
    p.infer()
    _invalidate(Sessions, a)
    with Sessions() as session:
        with pytest.raises(StaleAdaptationState) as info:
            load_adaptation_state(session, user_id="u")
    assert isinstance(info.value.__cause__, svc.StaleInferenceChain)


@pytest.mark.parametrize("statement", [
    "UPDATE user_competency_states SET current_stage = 'mastery'",
    "DELETE FROM competency_inference_tension_capabilities",
    "UPDATE competency_inference_runs SET validation_needs = '[{\"intent\": \"question\"}]'::jsonb"
    " WHERE interpretation_status = 'active'",
])
def test_pg_strict_loader_fails_closed_on_inconsistent_state(Sessions, engine, statement):  # noqa: F811
    _tensioned(Sessions)
    _sql(engine, statement)
    with Sessions() as session:
        with pytest.raises(InvalidAdaptationState):
            load_adaptation_state(session, user_id="u")


def test_pg_release_reusing_definitions_is_compatible_and_same_code_is_never_remapped(Sessions, engine):  # noqa: F811
    """Observation évaluée sous une release ANCIENNE (memberships différents)
    qui réutilise les definition_id V1 de C7_A / C7_B (compatibles) et porte
    un C7_C d'une autre révision (nouveau definition_id) : jamais remappé
    vers le C7_C courant ; un besoin de validation sur ce C7_C ancien =>
    fail closed, sur le C7_C courant => accepté."""
    import json

    from core import taxonomy_service as tax
    from tests.test_taxonomy_service import _def_kwargs
    p = Pipeline(Sessions)
    d = _definitions(p)
    with Sessions() as session:
        old = tax.create_candidate_release(session, version_key=f"6-1a-{uuid.uuid4()}",
                                           spec_fingerprint=f"sha256:{uuid.uuid4()}")
        old_c = tax.create_capability_definition(session, **_def_kwargs("C7_C", 61001)).id
        old_m = {code: tax.attach_capability_to_release(session, release_id=old.id, capability_definition_id=i).id
                 for code, i in (("C7_A", d["C7_A"]), ("C7_B", d["C7_B"]), ("C7_C", old_c), ("C7_D", d["C7_D"]))}
        old_release = old.id
        session.commit()
    current_release = p.release_id
    p.release_id, current_m = old_release, p.m
    p.m = old_m
    p.t3(p.app("C7_A", "C7_B", "C7_C", evidence_strength="strong"))
    p.release_id, p.m = current_release, current_m
    context, _ = p.infer()
    (c7,) = _load(Sessions).competencies
    assert c7.taxonomy_release_id == current_release
    assert c7.claims[2].represented_capability_definition_ids == (d["C7_A"], d["C7_B"])
    assert old_c not in {c.definition_id for c in c7.capabilities}

    def needs(definition_id):
        return json.dumps([need("revalidation", "application", "localized", definition_id)])

    _sql(engine, "UPDATE competency_inference_runs SET validation_needs = CAST(:n AS jsonb) WHERE id = :r",
         n=needs(d["C7_C"]), r=context.run_id)
    (accepted,) = _load(Sessions).competencies
    assert accepted.validation_needs[0].capability_definition_ids == (d["C7_C"],)
    _sql(engine, "UPDATE competency_inference_runs SET validation_needs = CAST(:n AS jsonb) WHERE id = :r",
         n=needs(old_c), r=context.run_id)
    with Sessions() as session:
        with pytest.raises(InvalidAdaptationState, match="aucun remapping"):
            load_adaptation_state(session, user_id="u")


def test_pg_captured_run_is_never_mixed_with_a_run_activated_during_the_load(Sessions, monkeypatch):  # noqa: F811
    """R1 active et validé ; 6-1A capture R1 ; R2 (nouveau dossier T5, tension
    ouverte) est activé et COMMITÉ par une autre session APRÈS la capture et
    AVANT les sous-lectures ; le snapshot reste intégralement R1 (READ
    COMMITTED : les sous-lectures voient R2 commité, mais elles sont toutes
    rattachées à run_id = R1, dont les lignes sont immuables)."""
    from core import longitudinal_service as t5
    from core.inference_engine import infer_competency
    from tests.test_inference_engine import T6_VERSIONS
    from tests.test_longitudinal_service import _start as start_t5
    p = Pipeline(Sessions)
    p.t3(p.app("C7_A", "C7_B", evidence_strength="strong"))
    first, _ = p.infer()
    reference = _load(Sessions)
    p.t3(p.contra("C7_A", contradiction_scope="application"))
    parent = start_t5(Sessions, p.release_id)  # candidat : R1 reste courant

    real = ad.get_validated_user_competency_state
    captured, activated = [], []

    def capture_then_activate_r2(db, *, user_id, competency_code):
        validated = real(db, user_id=user_id, competency_code=competency_code)
        captured.append(validated)
        with Sessions() as other:
            t5.complete_longitudinal_assessment(other, run_id=parent)
            other.commit()
        with Sessions() as other:
            context = svc.start_competency_inference(other, longitudinal_assessment_run_id=parent,
                                                     trigger="longitudinal_completed", **T6_VERSIONS)
            other.commit()
        decision = infer_competency(context)
        assert decision.tensions
        with Sessions() as other:
            svc.complete_competency_inference(other, run_id=context.run_id, decision=decision)
            other.commit()
        activated.append(context.run_id)
        return validated

    monkeypatch.setattr(ad, "get_validated_user_competency_state", capture_then_activate_r2)
    with Sessions() as session:
        snapshot = load_adaptation_state(session, user_id="u", competency_codes=("C7",))
    monkeypatch.undo()
    assert captured[0].active_inference_run_id == first.run_id
    assert p.run(first.run_id).interpretation_status == "superseded"
    assert p.run(activated[0]).interpretation_status == "active"
    assert snapshot == reference
    (c7,) = snapshot.competencies
    assert (c7.active_inference_run_id, c7.tensions, c7.tension_state) == (first.run_id, (), "none")
    (after,) = _load(Sessions).competencies
    assert (after.active_inference_run_id, after.tension_state, after.state_generation) == (activated[0], "open", 2)
    assert after.tensions and after.longitudinal_assessment_run_id == parent
