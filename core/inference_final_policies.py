"""Policies versionnées de T6-C3 (validation-1 / inference_schema-1 /
evaluator-1), vocabulaires fermés de l'Étape 5 finale et résolution de la
combinaison COMPLÈTE de versions du moteur T6-C V1.

Frontière. T6-C1 (base positive) et T6-C2 (confiance, tensions, stade
défendable, revision-context-v1) restent seuls juges de ce qu'ils décident ;
T6-C3 (core/inference_transition.py, core/inference_validation.py,
core/inference_engine.py) ne fait qu'ATTRIBUER la cause de transition,
exprimer des besoins LATENTS de confirmation / revalidation (5.4), résoudre
les memberships de la taxonomie courante et sérialiser l'InferenceDecision.
Ce module ne porte que les règles et vocabulaires fermés de T6-C3. Aucune
base, aucune Session, aucun modèle, aucun réseau, aucune horloge.

Versions. T6-C V1 supporte EXACTEMENT la combinaison (positive_basis-1,
confidence_profile-1, state_decision-1, validation-1, inference_schema-1,
evaluator-1). Les gardes de format et de taxonomie (InferenceContext,
input_schema_version == 2, model_id / prompt_spec_version None, taxonomie
courante) et la combinaison T6-C1 / T6-C2 sont celles des resolvers
existants, composés tels quels (une seule source de vérité) ; T6-C3 ajoute
les trois versions qui lui sont propres. Version inconnue ou combinaison non
déclarée : fail closed, aucun repli, aucune « dernière version », aucune
conversion.

Pourquoi aucun score. Les vocabulaires ci-dessous sont des codes FERMÉS et
descriptifs, ordonnés par déclaration (ordre canonique de sortie), jamais un
rang : aucune priorité numérique, aucun poids, aucun seuil. Un besoin de
validation n'est ni une preuve, ni une tâche, ni une question : seulement
« cette information serait utile si une occasion pertinente se présente »
(l'Étape 6 décidera, plus tard, s'il faut et comment la provoquer).
"""
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from core.inference_policies import APPLICATION, COMPREHENSION, DISCOVERY, POSITIVE_BASIS_V1
from core.inference_positive_basis import PositiveBasisAssessment
from core.inference_service import COMPETENCY_ONLY, LOCALIZED
from core.inference_state import T6C2Assessment
from core.inference_state_policies import (
    COMPETENCY_ONLY_REPRESENTATIVE_BASIS,
    COMPETENCY_ONLY_SCOPE,
    CONFIDENCE_PROFILE_V1,
    INDEPENDENCE_EVIDENCE_PRESENT,
    INDEPENDENCE_NOT_ESTABLISHED,
    INDEPENDENT_REMOBILIZATION_PRESENT,
    LOCALIZED_REPRESENTATIVE_SCOPE,
    SINGLE_EPISODE_ONLY,
    SINGLE_REPRESENTATIVE_DEMONSTRATION,
    SINGLE_REPRESENTATIVE_EPISODE,
    STATE_DECISION_V1,
    ResolvedStatePolicy,
    resolve_state_decision_policy,
)

VALIDATION_V1 = "validation-1"
INFERENCE_SCHEMA_V1 = "inference_schema-1"
EVALUATOR_V1 = "evaluator-1"
VALIDATION_NEED_SCHEMA_VERSION = "validation-need-v1"


class FinalInferenceError(Exception):
    """Erreur métier T6-C3 : le moteur échoue fermé, jamais un repli."""


class InvalidFinalInferencePolicy(FinalInferenceError):
    """Policy T6-C3 structurellement invalide."""


class UnsupportedFinalInferencePolicy(FinalInferenceError):
    """validation_version, inference_schema_version ou evaluator_version
    inconnue (ou combinaison complète non déclarée) : aucun défaut."""


class InconsistentInferenceAssessment(FinalInferenceError):
    """Sorties T6-C1 / T6-C2 incompatibles avec le contexte (type, version,
    compétence) ou entre elles : T6-C3 ne les recalcule ni ne les répare."""


class AmbiguousTransitionCause(FinalInferenceError):
    """Plusieurs familles causales expliquent ÉGALEMENT la décision au même
    niveau sémantique : aucune priorité arbitraire, fail closed."""


class UnattributableTransitionCause(FinalInferenceError):
    """Predecessor présent mais aucune famille causale structurellement
    attribuable (ou seulement une famille interdite pour cette transition)."""


class UnresolvableCapabilityMembership(FinalInferenceError):
    """capability_definition_id absent (ou en double) de la taxonomie
    courante : jamais de correspondance par capability_code."""


class MissingValidationProvenance(FinalInferenceError):
    """Besoin de validation sans aucune source courante légale : aucune
    source n'est jamais fabriquée."""


class InvalidAssembledDecision(FinalInferenceError):
    """DecisionInvariantGuard : la décision assemblée viole un invariant."""


# --------------------------------------------------------------------------
# Transition cause (vocabulaire fermé des raisons, ordre de déclaration)
# --------------------------------------------------------------------------

FIRST_INFERENCE = "first_inference"
NEW_USER_EVIDENCE_RESOLVES_REVISION_MOTIF = "new_user_evidence_resolves_revision_motif"
NEW_USER_EVIDENCE_SUPPORTS_CURRENT_DECISION = "new_user_evidence_supports_current_decision"
NEW_CONTRADICTION_DRIVES_REVISION = "new_contradiction_drives_revision"
NEW_USER_EVIDENCE_CHANGES_TENSION = "new_user_evidence_changes_tension"
NEW_USER_EVIDENCE_IN_CURRENT_SNAPSHOT = "new_user_evidence_in_current_snapshot"
INTEGRITY_CHANGE_SUPERSEDED_REVISION_MOTIF = "integrity_change_superseded_revision_motif"
INTEGRITY_CHANGE_REMOVED_PREVIOUS_BASIS = "integrity_change_removed_previous_basis"
INTEGRITY_CHANGE_REMOVED_PREVIOUS_CONTRADICTION = "integrity_change_removed_previous_contradiction"
INTEGRITY_CHANGE_REMOVED_PREVIOUS_SOURCE = "integrity_change_removed_previous_source"
INTEGRITY_CHANGE_IN_CAUSAL_WINDOW = "integrity_change_in_causal_window"
REEVALUATION_CHANGED_INTERPRETATION = "reevaluation_changed_interpretation"
TAXONOMY_CHANGE_CHANGED_INTERPRETATION = "taxonomy_change_changed_interpretation"
T5_VERSION_CHANGE_CHANGED_INTERPRETATION = "t5_version_change_changed_interpretation"
T6_SPECIFICATION_CHANGE_CHANGED_INTERPRETATION = "t6_specification_change_changed_interpretation"
MAINTAINED_AFTER_NEW_USER_EVIDENCE = "maintained_after_new_user_evidence"
MAINTAINED_AFTER_INTEGRITY_CHANGE = "maintained_after_integrity_change"
MAINTAINED_AFTER_CONTROLLED_REINTERPRETATION = "maintained_after_controlled_reinterpretation"
TRANSITION_REASON_CODES = (
    FIRST_INFERENCE, NEW_USER_EVIDENCE_RESOLVES_REVISION_MOTIF, NEW_USER_EVIDENCE_SUPPORTS_CURRENT_DECISION,
    NEW_CONTRADICTION_DRIVES_REVISION, NEW_USER_EVIDENCE_CHANGES_TENSION, NEW_USER_EVIDENCE_IN_CURRENT_SNAPSHOT,
    INTEGRITY_CHANGE_SUPERSEDED_REVISION_MOTIF, INTEGRITY_CHANGE_REMOVED_PREVIOUS_BASIS,
    INTEGRITY_CHANGE_REMOVED_PREVIOUS_CONTRADICTION, INTEGRITY_CHANGE_REMOVED_PREVIOUS_SOURCE,
    INTEGRITY_CHANGE_IN_CAUSAL_WINDOW, REEVALUATION_CHANGED_INTERPRETATION, TAXONOMY_CHANGE_CHANGED_INTERPRETATION,
    T5_VERSION_CHANGE_CHANGED_INTERPRETATION, T6_SPECIFICATION_CHANGE_CHANGED_INTERPRETATION,
    MAINTAINED_AFTER_NEW_USER_EVIDENCE, MAINTAINED_AFTER_INTEGRITY_CHANGE, MAINTAINED_AFTER_CONTROLLED_REINTERPRETATION)

# --------------------------------------------------------------------------
# Validation needs (5.4) : besoins LATENTS uniquement
# --------------------------------------------------------------------------

CONFIRMATION = "confirmation"
REVALIDATION = "revalidation"
VALIDATION_INTENTS = (CONFIRMATION, REVALIDATION)
VALIDATION_SCOPE_MODES = (LOCALIZED, COMPETENCY_ONLY)

# Revalidation : le plus petit périmètre fragilisé, jamais « toute la
# compétence » ni « revalider la Mastery ».
UNRESOLVED_REVISION_MOTIF = "unresolved_revision_motif"
CURRENT_STAGE_UNDER_TENSION = "current_stage_under_tension"
MATERIALLY_INCOMPATIBLE_HIGHER_CLAIM = "materially_incompatible_higher_claim"
# Confirmation (policy V1 conservatrice, figée) : enrichir une base déjà
# légitime du current_stage, jamais conditionner ni créer une claim.
SINGLE_REPRESENTATIVE_EPISODE_WOULD_BENEFIT_FROM_INDEPENDENT_CONTEXT = \
    "single_representative_episode_would_benefit_from_independent_context"
INDEPENDENCE_NOT_ESTABLISHED_ON_CURRENT_BASIS = "independence_not_established_on_current_basis"
COMPETENCY_ONLY_BASIS_WOULD_BENEFIT_FROM_LOCALIZATION = "competency_only_basis_would_benefit_from_localization"
VALIDATION_REASON_CODES = (
    UNRESOLVED_REVISION_MOTIF, CURRENT_STAGE_UNDER_TENSION, MATERIALLY_INCOMPATIBLE_HIGHER_CLAIM,
    SINGLE_REPRESENTATIVE_EPISODE_WOULD_BENEFIT_FROM_INDEPENDENT_CONTEXT, INDEPENDENCE_NOT_ESTABLISHED_ON_CURRENT_BASIS,
    COMPETENCY_ONLY_BASIS_WOULD_BENEFIT_FROM_LOCALIZATION)


# --------------------------------------------------------------------------
# Policy (validation-1, inference_schema-1, evaluator-1)
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ConfirmationMotifRule:
    """Motif de confirmation : fait(s) STRUCTURÉS (fact_codes T6-C2) qui le
    déclenchent. any_of : au moins un de ces faits dans `dimension` ;
    all_of : tous ces faits ((dimension, code)) ; none_of : aucun de ces
    faits ((dimension, code)). Aucun texte, aucune capacité non observée,
    aucun temps."""
    reason_code: str
    dimension: str
    any_of: tuple
    all_of: tuple = ()
    none_of: tuple = ()


@dataclass(frozen=True, kw_only=True)
class FinalInferencePolicy:
    """Règles qualitatives de T6-C3 pour UNE combinaison complète.

    confirmation_stages : current_stage pouvant recevoir une confirmation
      latente (Mastery exclue : conclusion longitudinale, jamais confirmée
      automatiquement, ni par le temps) ;
    confirmation_motifs : motifs V1 figés, lus sur le profil de confiance
      du SEUL current_stage, et seulement s'il est compatible (une claim
      sous tension relève de la revalidation, pas de la confirmation)."""
    positive_basis_version: str
    confidence_profile_version: str
    state_decision_version: str
    validation_version: str
    inference_schema_version: str
    evaluator_version: str
    validation_need_schema_version: str
    confirmation_stages: tuple
    confirmation_motifs: tuple


_NO_T5_INDEPENDENCE = (("independence", INDEPENDENCE_EVIDENCE_PRESENT),
                       ("independence", INDEPENDENT_REMOBILIZATION_PRESENT))

FINAL_INFERENCE_V1_POLICY = FinalInferencePolicy(
    positive_basis_version=POSITIVE_BASIS_V1,
    confidence_profile_version=CONFIDENCE_PROFILE_V1,
    state_decision_version=STATE_DECISION_V1,
    validation_version=VALIDATION_V1,
    inference_schema_version=INFERENCE_SCHEMA_V1,
    evaluator_version=EVALUATOR_V1,
    validation_need_schema_version=VALIDATION_NEED_SCHEMA_VERSION,
    confirmation_stages=(DISCOVERY, COMPREHENSION, APPLICATION),
    confirmation_motifs=(
        ConfirmationMotifRule(
            reason_code=SINGLE_REPRESENTATIVE_EPISODE_WOULD_BENEFIT_FROM_INDEPENDENT_CONTEXT,
            dimension="diagnosticity",
            any_of=(SINGLE_REPRESENTATIVE_DEMONSTRATION, SINGLE_REPRESENTATIVE_EPISODE),
            all_of=(("independence", SINGLE_EPISODE_ONLY),),
            none_of=_NO_T5_INDEPENDENCE),
        ConfirmationMotifRule(
            reason_code=INDEPENDENCE_NOT_ESTABLISHED_ON_CURRENT_BASIS,
            dimension="independence",
            any_of=(INDEPENDENCE_NOT_ESTABLISHED,),
            none_of=_NO_T5_INDEPENDENCE),
        ConfirmationMotifRule(
            reason_code=COMPETENCY_ONLY_BASIS_WOULD_BENEFIT_FROM_LOCALIZATION,
            dimension="diagnosticity",
            any_of=(COMPETENCY_ONLY_REPRESENTATIVE_BASIS,),
            none_of=(("coverage", LOCALIZED_REPRESENTATIVE_SCOPE),)),
        ConfirmationMotifRule(
            reason_code=COMPETENCY_ONLY_BASIS_WOULD_BENEFIT_FROM_LOCALIZATION,
            dimension="coverage",
            any_of=(COMPETENCY_ONLY_SCOPE,),
            none_of=(("coverage", LOCALIZED_REPRESENTATIVE_SCOPE),)),
    ),
)

# Registry : combinaison complète des six versions -> policy.
FINAL_INFERENCE_POLICIES = MappingProxyType({
    (POSITIVE_BASIS_V1, CONFIDENCE_PROFILE_V1, STATE_DECISION_V1, VALIDATION_V1, INFERENCE_SCHEMA_V1,
     EVALUATOR_V1): FINAL_INFERENCE_V1_POLICY,
})

_DIMENSIONS = ("diagnosticity", "coverage", "independence", "consistency", "temporal_validation")


def validate_final_inference_policy(policy) -> None:
    """Validation statique stricte (pure)."""
    if type(policy) is not FinalInferencePolicy:
        raise InvalidFinalInferencePolicy("FinalInferencePolicy attendue")
    if policy.validation_need_schema_version != VALIDATION_NEED_SCHEMA_VERSION:
        raise InvalidFinalInferencePolicy("validation_need_schema_version inconnue")
    if not policy.confirmation_stages or not set(policy.confirmation_stages) <= {DISCOVERY, COMPREHENSION,
                                                                                 APPLICATION}:
        raise InvalidFinalInferencePolicy("confirmation_stages : Discovery / Comprehension / Application seulement"
                                          " (jamais Mastery)")
    for rule in policy.confirmation_motifs:
        if type(rule) is not ConfirmationMotifRule or rule.reason_code not in VALIDATION_REASON_CODES or \
                rule.dimension not in _DIMENSIONS or not rule.any_of:
            raise InvalidFinalInferencePolicy("confirmation_motifs invalides")
        if not all(dimension in _DIMENSIONS for dimension, _ in (*rule.all_of, *rule.none_of)):
            raise InvalidFinalInferencePolicy("confirmation_motifs : dimension inconnue")


# --------------------------------------------------------------------------
# Résolution
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ResolvedFinalPolicy:
    """Combinaison complète résolue : policy T6-C2 (qui porte positive_basis-1
    résolue contre la taxonomie courante) + règles T6-C3."""
    state: ResolvedStatePolicy
    rules: FinalInferencePolicy


@dataclass(frozen=True, kw_only=True)
class FinalInferencePolicyResolver:
    """Compose les resolvers T6-C1 / T6-C2 (gardes de contexte, taxonomie,
    trois premières versions), puis résout EXPLICITEMENT la combinaison
    complète des six versions. Aucun défaut, aucun repli."""
    registry: Mapping = field(default_factory=lambda: FINAL_INFERENCE_POLICIES)

    def resolve(self, context) -> ResolvedFinalPolicy:
        state = resolve_state_decision_policy(context)
        prefix = (state.rules.positive_basis_version, state.rules.confidence_profile_version,
                  state.rules.state_decision_version)
        for name in ("validation_version", "inference_schema_version", "evaluator_version"):
            value = getattr(context, name)
            if type(value) is not str or not any(all(a == b for a, b in zip(key, (*prefix, value)))
                                                 for key in self.registry):
                raise UnsupportedFinalInferencePolicy(f"{name} {value!r} inconnue pour la combinaison {prefix}"
                                                      " (T6-C V1)")
            prefix = (*prefix, value)
        rules = self.registry.get(prefix)
        if rules is None:
            raise UnsupportedFinalInferencePolicy(f"combinaison {prefix} non déclarée (T6-C V1)")
        validate_final_inference_policy(rules)
        return ResolvedFinalPolicy(state=state, rules=rules)


def resolve_final_inference_policy(context) -> ResolvedFinalPolicy:
    """Résolution avec le registry versionné par défaut (T6-C V1)."""
    return FinalInferencePolicyResolver().resolve(context)


def check_inference_assessments(context, positive_basis, state) -> None:
    """Garde d'entrée des étapes T6-C3 : sorties T6-C1 / T6-C2 du bon type,
    des versions et de la compétence du contexte. Jamais un recalcul : T6-C3
    ne relit ni ne répare T6-C1 / T6-C2 (infer_competency les enchaîne)."""
    if type(positive_basis) is not PositiveBasisAssessment or type(state) is not T6C2Assessment:
        raise InconsistentInferenceAssessment("PositiveBasisAssessment et T6C2Assessment attendus")
    versions = (positive_basis.policy_version, state.positive_basis_version, state.confidence_profile_version,
                state.state_decision_version)
    expected = (context.positive_basis_version, context.positive_basis_version, context.confidence_profile_version,
                context.state_decision_version)
    if versions != expected:
        raise InconsistentInferenceAssessment(f"versions T6-C1 / T6-C2 {versions} != contexte {expected}")
    if {positive_basis.competency_code, state.competency_code} != {context.competency_code}:
        raise InconsistentInferenceAssessment("compétence divergente entre contexte, T6-C1 et T6-C2")
    if (context.predecessor is None) != (context.transition_causality is None):
        raise InconsistentInferenceAssessment("predecessor et transition_causality incohérents (V2 exigé)")
