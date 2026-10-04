"""Policies versionnées de T6-C2 (confidence_profile-1 / state_decision-1) et
leur résolution contre le contexte d'inférence.

Frontière. T6-C1 (core/inference_policies.py, core/inference_positive_basis.py)
dit QUELLES prétentions positives sont établies. T6-C2 dit, pour chacune :
quelle est la solidité DESCRIPTIVE de sa base (cinq dimensions séparées,
jamais agrégées), quelles contradictions la fragilisent réellement, et quel
stade reste aujourd'hui défendable (stabilité, anti-oscillation). Ce module
ne porte que les RÈGLES et vocabulaires fermés de T6-C2 ; les moteurs purs
vivent dans core/inference_confidence.py et core/inference_state.py. Aucune
base, aucune Session, aucun modèle, aucun réseau, aucune horloge.

Aucune duplication de policy. La représentativité sémantique C1-C12 reste
celle des SemanticPattern de positive_basis-1 (core/inference_policies.py),
seule définition disponible : T6-C2 la réutilise pour dire si une
contradiction est périphérique, pertinente ou représentative du périmètre
affirmé par une claim. Les gardes de format (InferenceContext,
input_schema_version == 2, model_id / prompt_spec_version None, taxonomie
courante) sont celles du resolver T6-C1, réutilisé tel quel : une seule
source de vérité.

Versions. T6-C2 V1 supporte EXACTEMENT la combinaison (positive_basis-1,
confidence_profile-1, state_decision-1). Version inconnue ou combinaison non
déclarée : fail closed, aucun repli, aucune « dernière version », aucune
conversion.

Pourquoi aucun score. Les vocabulaires ci-dessous sont des codes FERMÉS et
descriptifs, ordonnés par déclaration (ordre canonique de sortie), jamais un
rang : aucun niveau, poids, ratio, seuil ni compte. contradiction_scope
(vocabulaire T3) dit seulement quels stades une contradiction PEUT
concerner ; « peut concerner » n'invalide jamais rien à lui seul.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from core.inference_policies import (
    APPLICATION,
    COMPREHENSION,
    DISCOVERY,
    MASTERY,
    POSITIVE_BASIS_V1,
    ResolvedPositiveBasisPolicy,
    resolve_positive_basis_policy,
)
from core.inference_positive_basis import SUPPORTABLE_CLAIMS
from core.inference_service import CLAIM_STAGES

CONFIDENCE_PROFILE_V1 = "confidence_profile-1"
STATE_DECISION_V1 = "state_decision-1"
# Format SÉRIALISÉ du profil de confiance (jamais une policy : la policy
# pédagogique reste confidence_profile-1, inchangée). v1 : format historique
# compact (fact_codes + capacités agrégées par dimension), immuable, lisible,
# plus jamais écrit. v2 : format courant écrit, chaque fait avec SES propres
# capacités. La version d'un profil persisté est TOUJOURS lue dans son
# schema_version, jamais déduite de sa forme.
CONFIDENCE_PROFILE_SCHEMA_V1 = "confidence-profile-v1"
CONFIDENCE_PROFILE_SCHEMA_V2 = "confidence-profile-v2"
CONFIDENCE_PROFILE_SCHEMA_VERSION = CONFIDENCE_PROFILE_SCHEMA_V2
READABLE_CONFIDENCE_PROFILE_SCHEMA_VERSIONS = (CONFIDENCE_PROFILE_SCHEMA_V1, CONFIDENCE_PROFILE_SCHEMA_V2)
REVISION_CONTEXT_SCHEMA_VERSION = "revision-context-v1"


class InferenceStateError(Exception):
    """Erreur métier T6-C2 : le moteur échoue fermé, jamais un repli."""


class InvalidInferenceStatePolicy(InferenceStateError):
    """Policy T6-C2 structurellement invalide."""


class UnsupportedConfidenceProfilePolicy(InferenceStateError):
    """confidence_profile_version inconnue (ou non déclarée pour cette
    positive_basis_version) : aucune policy par défaut."""


class UnsupportedStateDecisionPolicy(InferenceStateError):
    """state_decision_version inconnue (ou non déclarée pour cette
    combinaison) : aucune policy par défaut."""


class InconsistentPositiveBasis(InferenceStateError):
    """PositiveBasisAssessment incompatible avec le contexte : autre type,
    version, compétence, ou base différente de celle du dossier courant."""


class UnsupportedRevisionContext(InferenceStateError):
    """unresolved_revision_context du predecessor hors du format
    revision-context-v1 (jamais converti), ou predecessor incohérent."""


# --------------------------------------------------------------------------
# Vocabulaires fermés (ordre de déclaration = ordre canonique de sortie)
# --------------------------------------------------------------------------

# contradiction_scope (T3, lu seulement).
RECOGNITION = "recognition"
UNDETERMINED = "undetermined"
CONTRADICTION_SCOPES = (RECOGNITION, COMPREHENSION, APPLICATION, UNDETERMINED)

# Dimensions (miroir de inference_service.CONFIDENCE_DIMENSIONS).
DIAGNOSTICITY = "diagnosticity"
COVERAGE = "coverage"
INDEPENDENCE = "independence"
CONSISTENCY = "consistency"
TEMPORAL_VALIDATION = "temporal_validation"
CONFIDENCE_DIMENSIONS = (DIAGNOSTICITY, COVERAGE, INDEPENDENCE, CONSISTENCY, TEMPORAL_VALIDATION)

# Faits descriptifs par dimension (jamais un niveau).
DIRECT_REPRESENTATIVE_BASIS = "direct_representative_basis"
IMPLIED_FROM_HIGHER_CLAIM = "implied_from_higher_claim"
SINGLE_REPRESENTATIVE_DEMONSTRATION = "single_representative_demonstration"
SINGLE_REPRESENTATIVE_EPISODE = "single_representative_episode"
COMPLEMENTARY_REPRESENTATIVE_HISTORY = "complementary_representative_history"
COMPETENCY_ONLY_REPRESENTATIVE_BASIS = "competency_only_representative_basis"
LONGITUDINAL_MASTERY_BASIS = "longitudinal_mastery_basis"

LOCALIZED_REPRESENTATIVE_SCOPE = "localized_representative_scope"
COMPETENCY_ONLY_SCOPE = "competency_only_scope"
ADDITIONAL_POSITIVE_SCOPE_PRESENT = "additional_positive_scope_present"
COVERAGE_CONCENTRATED_ON_CLAIM_SCOPE = "coverage_concentrated_on_claim_scope"
# Capacités de la compétence SANS observation POSITIVE située dans le dossier
# T5 courant (positive_observation_status != observed_positive). Jamais
# « aucune observation » : une capacité portant seulement des observations
# contradictoires (ou supportive sans profondeur locale) en fait partie.
UNOBSERVED_CAPABILITIES_PRESENT = "unobserved_capabilities_present"

SINGLE_EPISODE_ONLY = "single_episode_only"
INDEPENDENCE_EVIDENCE_PRESENT = "independence_evidence_present"
INDEPENDENT_REMOBILIZATION_PRESENT = "independent_remobilization_present"
DEPENDENCY_PRESENT = "dependency_present"
PARTIAL_DEPENDENCY_PRESENT = "partial_dependency_present"
INDEPENDENCE_NOT_ESTABLISHED = "independence_not_established"
MIXED_DEPENDENCY_STRUCTURE = "mixed_dependency_structure"

NO_OBSERVED_CURRENT_TENSION = "no_observed_current_tension"
OPEN_COMPARABLE_CONTRADICTION_PRESENT = "open_comparable_contradiction_present"
HISTORICALLY_REVALIDATED_CONTRADICTION_PRESENT = "historically_revalidated_contradiction_present"
STRUCTURAL_RECURRENCE_CANDIDATE_PRESENT = "structural_recurrence_candidate_present"
CONTRADICTION_OUTSIDE_CLAIM_SCOPE_PRESENT = "contradiction_outside_claim_scope_present"
MIXED_CONSISTENCY_HISTORY = "mixed_consistency_history"

DISTINCT_POSITIVE_EPISODES_PRESENT = "distinct_positive_episodes_present"
DURABILITY_EVIDENCE_PRESENT = "durability_evidence_present"
EXACT_DEMONSTRATION_TIME_AVAILABLE = "exact_demonstration_time_available"
EXACT_DEMONSTRATION_TIME_UNAVAILABLE = "exact_demonstration_time_unavailable"

DIMENSION_FACT_CODES = MappingProxyType({
    DIAGNOSTICITY: (DIRECT_REPRESENTATIVE_BASIS, IMPLIED_FROM_HIGHER_CLAIM, SINGLE_REPRESENTATIVE_DEMONSTRATION,
                    SINGLE_REPRESENTATIVE_EPISODE, COMPLEMENTARY_REPRESENTATIVE_HISTORY,
                    COMPETENCY_ONLY_REPRESENTATIVE_BASIS, LONGITUDINAL_MASTERY_BASIS),
    COVERAGE: (LOCALIZED_REPRESENTATIVE_SCOPE, COMPETENCY_ONLY_SCOPE, ADDITIONAL_POSITIVE_SCOPE_PRESENT,
               COVERAGE_CONCENTRATED_ON_CLAIM_SCOPE, UNOBSERVED_CAPABILITIES_PRESENT),
    INDEPENDENCE: (SINGLE_EPISODE_ONLY, INDEPENDENCE_EVIDENCE_PRESENT, INDEPENDENT_REMOBILIZATION_PRESENT,
                   DEPENDENCY_PRESENT, PARTIAL_DEPENDENCY_PRESENT, INDEPENDENCE_NOT_ESTABLISHED,
                   MIXED_DEPENDENCY_STRUCTURE),
    CONSISTENCY: (NO_OBSERVED_CURRENT_TENSION, OPEN_COMPARABLE_CONTRADICTION_PRESENT,
                  HISTORICALLY_REVALIDATED_CONTRADICTION_PRESENT, STRUCTURAL_RECURRENCE_CANDIDATE_PRESENT,
                  CONTRADICTION_OUTSIDE_CLAIM_SCOPE_PRESENT, MIXED_CONSISTENCY_HISTORY),
    TEMPORAL_VALIDATION: (SINGLE_EPISODE_ONLY, DISTINCT_POSITIVE_EPISODES_PRESENT, INDEPENDENT_REMOBILIZATION_PRESENT,
                          DURABILITY_EVIDENCE_PRESENT, EXACT_DEMONSTRATION_TIME_AVAILABLE,
                          EXACT_DEMONSTRATION_TIME_UNAVAILABLE),
})

# Lecture d'une contradiction par rapport à UNE claim (§ matérialité).
CONTRADICTION_OUTSIDE_CLAIM_STAGE = "contradiction_scope_outside_claim_stage"
LOCALIZED_PERIPHERAL = "localized_peripheral"
HISTORICALLY_REVALIDATED_ONLY = "historically_revalidated_only"
CLAIM_RELEVANT_TENSION = "claim_relevant_tension"
CLAIM_REPRESENTATIVE_INCOMPATIBILITY = "claim_representative_incompatibility"
CONTRADICTION_READINGS = (CONTRADICTION_OUTSIDE_CLAIM_STAGE, LOCALIZED_PERIPHERAL, HISTORICALLY_REVALIDATED_ONLY,
                          CLAIM_RELEVANT_TENSION, CLAIM_REPRESENTATIVE_INCOMPATIBILITY)

# Tensions (T6-C2 ne produit jamais revision_status : T6-C3).
TENSIONED = "tensioned"
MATERIALLY_INCOMPATIBLE = "materially_incompatible"
COMPATIBILITY_EFFECTS = (TENSIONED, MATERIALLY_INCOMPATIBLE)
CLAIM_RELEVANT_CONTRADICTION = "claim_relevant_contradiction"
CLAIM_REPRESENTATIVE_CONTRADICTION = "claim_representative_contradiction"
INDEPENDENT_COMPLEMENTARY_CONTRADICTIONS = "independent_complementary_contradictions"
REPRESENTATIVE_SCOPE_WITHOUT_EXCEPTIONAL_DIAGNOSTICITY = "representative_scope_without_exceptional_diagnosticity"
UNDETERMINED_CONTRADICTION_SCOPE = "undetermined_contradiction_scope"
COMPETENCY_ONLY_CONTRADICTION = "competency_only_contradiction"
DEPENDENCY_LINKED_CONTRADICTION = "dependency_linked_contradiction"
STRUCTURAL_RECURRENCE_CANDIDATE = "structural_recurrence_candidate"
REVISION_MOTIF_RESOLVED_SUPPORTIVELY = "revision_motif_resolved_supportively"
PREDECESSOR_CLAIM_IN_REVISION_CONTEXT = "predecessor_claim_in_revision_context"
TENSION_REASON_CODES = (CLAIM_RELEVANT_CONTRADICTION, CLAIM_REPRESENTATIVE_CONTRADICTION,
                        INDEPENDENT_COMPLEMENTARY_CONTRADICTIONS,
                        REPRESENTATIVE_SCOPE_WITHOUT_EXCEPTIONAL_DIAGNOSTICITY,
                        UNDETERMINED_CONTRADICTION_SCOPE, COMPETENCY_ONLY_CONTRADICTION,
                        DEPENDENCY_LINKED_CONTRADICTION, STRUCTURAL_RECURRENCE_CANDIDATE,
                        REVISION_MOTIF_RESOLVED_SUPPORTIVELY, PREDECESSOR_CLAIM_IN_REVISION_CONTEXT)

# Compatibilité d'une claim established.
COMPATIBLE = "compatible"
COMPATIBILITY_STATUSES = (COMPATIBLE, TENSIONED, MATERIALLY_INCOMPATIBLE)
NO_RELEVANT_OPEN_CONTRADICTION = "no_relevant_open_contradiction"
RELEVANT_CONTRADICTION_CLAIM_REMAINS_DEFENSIBLE = "relevant_contradiction_claim_remains_defensible"
REPRESENTATIVE_CONTRADICTION_CLAIM_NOT_DEFENSIBLE = "representative_contradiction_claim_not_defensible_as_current_stage"
COMPATIBILITY_REASON_CODES = (NO_RELEVANT_OPEN_CONTRADICTION, RELEVANT_CONTRADICTION_CLAIM_REMAINS_DEFENSIBLE,
                              REPRESENTATIVE_CONTRADICTION_CLAIM_NOT_DEFENSIBLE)

# Motif de révision (revision-context-v1).
UNRESOLVED = "unresolved"
RESOLVED_SUPPORTIVELY = "resolved_supportively"
SUPERSEDED_BY_REINTERPRETATION_OR_INTEGRITY_CHANGE = "superseded_by_reinterpretation_or_integrity_change"
RESOLUTION_STATUSES = (UNRESOLVED, RESOLVED_SUPPORTIVELY, SUPERSEDED_BY_REINTERPRETATION_OR_INTEGRITY_CHANGE)
HIGHER_CLAIM_MATERIALLY_FRAGILIZED_LOWER_CLAIM_NOT_IDENTIFIED = \
    "higher_claim_materially_fragilized_lower_claim_not_identified"
HIGHER_CLAIM_MATERIALLY_FRAGILIZED_DEFENSIBLE_LOWER_CLAIM_RETAINED = \
    "higher_claim_materially_fragilized_defensible_lower_claim_retained"
REVISION_REASON_CODES = (HIGHER_CLAIM_MATERIALLY_FRAGILIZED_LOWER_CLAIM_NOT_IDENTIFIED,
                         HIGHER_CLAIM_MATERIALLY_FRAGILIZED_DEFENSIBLE_LOWER_CLAIM_RETAINED)
# Raisons de résolution d'un motif (jamais « l'utilisateur a progressé »
# pour une supersession).
MOTIF_CONTRADICTIONS_INVALIDATED = "motif_contradictions_invalidated"
MOTIF_CONTRADICTIONS_REEVALUATED = "motif_contradictions_reevaluated"
NEW_DEMONSTRATION_ON_MOTIF_SCOPE = "new_demonstration_on_motif_scope"
T5_INDEPENDENCE_ON_MOTIF_SCOPE = "t5_independence_on_motif_scope"
EXCEPTIONALLY_DIAGNOSTIC_DEMONSTRATION = "exceptionally_diagnostic_demonstration"
NO_SUPPORTIVE_RESOLUTION_ON_MOTIF_SCOPE = "no_supportive_resolution_on_motif_scope"
RESOLUTION_REASON_CODES = (MOTIF_CONTRADICTIONS_INVALIDATED, MOTIF_CONTRADICTIONS_REEVALUATED,
                           NEW_DEMONSTRATION_ON_MOTIF_SCOPE, T5_INDEPENDENCE_ON_MOTIF_SCOPE,
                           EXCEPTIONALLY_DIAGNOSTIC_DEMONSTRATION, NO_SUPPORTIVE_RESOLUTION_ON_MOTIF_SCOPE)

# state_reason_code (fermé).
HIGHEST_COMPATIBLE_POSITIVE_CLAIM = "highest_compatible_positive_claim"
HIGHEST_POSITIVE_CLAIM_WITH_OPEN_TENSION = "highest_positive_claim_with_open_tension"
DOWNWARD_REVISION_TO_DEFENSIBLE_LOWER_CLAIM = "downward_revision_to_defensible_lower_claim"
PREVIOUS_STAGE_TEMPORARILY_HELD_UNDER_UNRESOLVED_TENSION = "previous_stage_temporarily_held_under_unresolved_tension"
FIRST_INFERENCE_POSITIVE_STAGE_HELD_UNDER_UNRESOLVED_TENSION = \
    "first_inference_positive_stage_held_under_unresolved_tension"
HIGHEST_POSITIVE_STAGE_HELD_UNDER_UNRESOLVED_TENSION = "highest_positive_stage_held_under_unresolved_tension"
NO_CURRENT_POSITIVE_CLAIM = "no_current_positive_claim"
REBOUND_BLOCKED_BY_UNRESOLVED_REVISION_MOTIF = "rebound_blocked_by_unresolved_revision_motif"
REVISION_MOTIF_RESOLVED_SUPPORTIVELY_STATE = REVISION_MOTIF_RESOLVED_SUPPORTIVELY
REVISION_MOTIF_SUPERSEDED_BY_REINTERPRETATION = "revision_motif_superseded_by_reinterpretation"
STATE_REASON_CODES = (HIGHEST_COMPATIBLE_POSITIVE_CLAIM, HIGHEST_POSITIVE_CLAIM_WITH_OPEN_TENSION,
                      DOWNWARD_REVISION_TO_DEFENSIBLE_LOWER_CLAIM,
                      PREVIOUS_STAGE_TEMPORARILY_HELD_UNDER_UNRESOLVED_TENSION,
                      FIRST_INFERENCE_POSITIVE_STAGE_HELD_UNDER_UNRESOLVED_TENSION,
                      HIGHEST_POSITIVE_STAGE_HELD_UNDER_UNRESOLVED_TENSION, NO_CURRENT_POSITIVE_CLAIM,
                      REBOUND_BLOCKED_BY_UNRESOLVED_REVISION_MOTIF, REVISION_MOTIF_RESOLVED_SUPPORTIVELY_STATE,
                      REVISION_MOTIF_SUPERSEDED_BY_REINTERPRETATION)


# --------------------------------------------------------------------------
# Policy state_decision-1
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class StateDecisionPolicy:
    """Règles qualitatives de T6-C2 pour UNE combinaison de versions.

    contradiction_scope_reach : stades qu'une contradiction de ce niveau T3
      PEUT concerner (hiérarchie qualitative figée, jamais une invalidation
      automatique ; le scope sémantique, la représentativité, les
      dépendances et les revalidations sont toujours vérifiés).
    material_contradiction_scopes : niveaux qui peuvent, avec le reste des
      conditions, rendre une claim materially_incompatible (undetermined :
      jamais seul).
    exceptional_diagnostic_strengths : lecture diagnostique LOCALE de T3
      (evidence_strength) qu'une observation UNIQUE doit porter pour
      pouvoir, avec la représentativité, l'attribution et la profondeur,
      suffire seule (condition nécessaire, jamais suffisante : aucune
      « strong => incompatibilité » ni « strong => résolution »).
    pattern_stage : policy de représentativité (positive_basis-1) dont une
      claim tire son périmètre affirmé (Mastery : celle d'Application).
    resolution_depths : profondeurs locales (T3) qui peuvent redémontrer un
      stade fragilisé (celles qui peuvent soutenir cette claim, T6-C1)."""
    positive_basis_version: str
    confidence_profile_version: str
    state_decision_version: str
    contradiction_scope_reach: Mapping
    material_contradiction_scopes: frozenset
    exceptional_diagnostic_strengths: frozenset
    pattern_stage: Mapping
    resolution_depths: Mapping


def _resolution_depths() -> Mapping:
    depths = {stage: tuple(depth for depth, claims in SUPPORTABLE_CLAIMS.items() if stage in claims)
              for stage in (DISCOVERY, COMPREHENSION, APPLICATION)}
    depths[MASTERY] = depths[APPLICATION]
    return MappingProxyType(depths)


STATE_DECISION_V1_POLICY = StateDecisionPolicy(
    positive_basis_version=POSITIVE_BASIS_V1,
    confidence_profile_version=CONFIDENCE_PROFILE_V1,
    state_decision_version=STATE_DECISION_V1,
    contradiction_scope_reach=MappingProxyType({
        RECOGNITION: (DISCOVERY, COMPREHENSION, APPLICATION, MASTERY),
        COMPREHENSION: (COMPREHENSION, APPLICATION, MASTERY),
        APPLICATION: (APPLICATION, MASTERY),
        UNDETERMINED: (DISCOVERY, COMPREHENSION, APPLICATION, MASTERY),
    }),
    material_contradiction_scopes=frozenset({RECOGNITION, COMPREHENSION, APPLICATION}),
    exceptional_diagnostic_strengths=frozenset({"strong"}),
    pattern_stage=MappingProxyType({DISCOVERY: DISCOVERY, COMPREHENSION: COMPREHENSION, APPLICATION: APPLICATION,
                                    MASTERY: APPLICATION}),
    resolution_depths=_resolution_depths(),
)

# Registry : (positive_basis_version, confidence_profile_version,
# state_decision_version) -> policy. Seules les combinaisons déclarées sont
# supportées.
STATE_DECISION_POLICIES = MappingProxyType({
    (POSITIVE_BASIS_V1, CONFIDENCE_PROFILE_V1, STATE_DECISION_V1): STATE_DECISION_V1_POLICY,
})


def validate_state_decision_policy(policy) -> None:
    """Validation statique stricte (pure)."""
    if type(policy) is not StateDecisionPolicy:
        raise InvalidInferenceStatePolicy("StateDecisionPolicy attendue")
    reach = policy.contradiction_scope_reach
    if tuple(reach) != CONTRADICTION_SCOPES:
        raise InvalidInferenceStatePolicy(f"contradiction_scope_reach : {tuple(reach)} != {CONTRADICTION_SCOPES}")
    for scope, stages in reach.items():
        if type(stages) is not tuple or not stages or not set(stages) <= set(CLAIM_STAGES):
            raise InvalidInferenceStatePolicy(f"contradiction_scope_reach[{scope}] invalide")
        if tuple(s for s in CLAIM_STAGES if s in stages) != stages:
            raise InvalidInferenceStatePolicy(f"contradiction_scope_reach[{scope}] hors ordre canonique")
    if UNDETERMINED in policy.material_contradiction_scopes or \
            not policy.material_contradiction_scopes <= set(CONTRADICTION_SCOPES):
        raise InvalidInferenceStatePolicy("undetermined ne suffit jamais seul à une incompatibilité matérielle")
    if not policy.exceptional_diagnostic_strengths:
        raise InvalidInferenceStatePolicy("exceptional_diagnostic_strengths vide")
    if tuple(policy.pattern_stage) != CLAIM_STAGES or policy.pattern_stage[MASTERY] != APPLICATION:
        raise InvalidInferenceStatePolicy("pattern_stage : quatre claims, Mastery lue sur Application")
    if tuple(policy.resolution_depths) != CLAIM_STAGES or not all(policy.resolution_depths.values()):
        raise InvalidInferenceStatePolicy("resolution_depths : une profondeur au moins par claim")


# --------------------------------------------------------------------------
# Résolution
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class ResolvedStatePolicy:
    """Policy T6-C2 résolue pour un contexte : positive_basis-1 résolue
    contre la taxonomie courante (resolver T6-C1) + règles state_decision."""
    positive_basis: ResolvedPositiveBasisPolicy
    rules: StateDecisionPolicy


@dataclass(frozen=True, kw_only=True)
class T6C2PolicyResolver:
    """Résout explicitement la combinaison de versions du contexte. Les
    gardes de contexte (type, V2, métadonnées de modèle, taxonomie) sont
    celles de T6-C1, appliquées d'abord. Aucun défaut, aucun repli."""
    registry: Mapping = field(default_factory=lambda: STATE_DECISION_POLICIES)

    def resolve(self, context) -> ResolvedStatePolicy:
        positive = resolve_positive_basis_policy(context)
        confidence, state = context.confidence_profile_version, context.state_decision_version
        declared = any(basis == positive.policy_version and profile == confidence
                       for basis, profile, _ in self.registry)
        if type(confidence) is not str or not declared:
            raise UnsupportedConfidenceProfilePolicy(
                f"confidence_profile_version {confidence!r} inconnue pour {positive.policy_version}")
        rules = self.registry.get((positive.policy_version, confidence, state)) if type(state) is str else None
        if rules is None:
            raise UnsupportedStateDecisionPolicy(
                f"state_decision_version {state!r} inconnue pour ({positive.policy_version}, {confidence})")
        validate_state_decision_policy(rules)
        return ResolvedStatePolicy(positive_basis=positive, rules=rules)


def resolve_state_decision_policy(context) -> ResolvedStatePolicy:
    """Résolution avec le registry versionné par défaut (T6-C2 V1)."""
    return T6C2PolicyResolver().resolve(context)
