"""Reconstruction READ-ONLY du dossier longitudinal (T5-C, niveau 5 :
relations longitudinales).

Transforme UN LongitudinalAssessmentRun finalisé (completed / active, ou
completed / superseded pour l'audit historique) et ses données T2 / T3 / T4 /
T5 en une vue structurée et immuable (LongitudinalDossier), consommable plus
tard par T6. Aucun appelant applicatif, aucune route.

Doctrine : OBSERVATION = unité de preuve ; CAPABILITY = périmètre sémantique
de la preuve ; RELATION T5 = structure historique entre preuves ; PROFIL
T5-C = vue reconstruite de cette histoire. Un profil n'est jamais une
observation, une preuve supplémentaire, un score ni un état utilisateur.
T5-C répond « quelle histoire ces démonstrations forment-elles ? » ; « qu'est
-ce qu'Oryx peut aujourd'hui considérer comme établi ? » appartient à T6.
Aucun stade utilisateur, aucune confiance, aucune maîtrise, aucun besoin de
revalidation / confirmation, aucune suffisance, aucun ratio, aucun appel de
modèle. local_stage reste l'attribut LOCAL d'une observation ; evidence_strength
reste un attribut qualitatif (jamais converti en nombre ni sommé).

Invariants :

- Lecture seule : uniquement des SELECT, exécutés sous db.no_autoflush (une
  lecture ne déclenche jamais le flush d'écritures en attente de
  l'appelant). Jamais add / delete / flush / commit / rollback, aucun
  verrou, aucune réparation. Les lignes sont lues colonne par colonne (aucun
  objet ORM chargé ni modifié). Aucune persistance du dossier, aucun cache.

- Snapshot figé : la vue est reconstruite UNIQUEMENT à partir des
  LongitudinalAssessmentInput du run et des relations du run. Une
  observation apparue après le run n'est jamais ajoutée ; un run superseded
  reste reconstructible exactement. Un état amont changé depuis
  (observation invalidée, run T3 réévalué) n'est ni masqué ni corrigé : il
  est exposé (current_*) et signalé dans limitations.

- Défense : même si T5-B valide avant activation, un dossier manifestement
  corrompu est refusé (InvalidLongitudinalDossier), sans aucune réparation :
  observation d'input absente, d'une autre compétence, d'un autre user, d'un
  run T3 non completed ou sans release, incompatible avec la release du
  run, deux runs T3 d'un même événement, input_fingerprint différent du
  recalcul, relation vers une observation hors snapshot, vocabulaire, basis,
  polarités / stade / événements d'une relation, aide d'un autre user ou du
  même événement, membership d'une autre release ou compétence, scope hors
  du périmètre compatible, scope_fingerprint différent du recalcul, doublon
  ou classification contradictoire d'une dépendance, dépendance chevauchant
  un transfert ou une revalidation de la même cible. Règles volontairement
  réimplémentées localement (formats V1 de T5-B : input_fingerprint,
  scope_fingerprint, compatibility gate, chevauchement conservateur) plutôt
  qu'importées : aucune dépendance vers le service transactionnel, parité
  vérifiée par les tests.

- Compatibility gate (identique à T5-B) : même capability_definition_id =
  même sens, jamais de correspondance par capability_code. Une observation
  localized ne garde que les définitions de ses mappings T4 (release de SON
  run T3, même compétence) qui appartiennent aussi à la release du run T5 ;
  competency_only n'est jamais localisée sur une capacité.

- Indépendance : l'absence d'arête de dépendance ne signifie JAMAIS
  indépendance (lecture not_established). Seule la cible d'un
  ObservationTransfer ou d'une ObservationRevalidation validés porte une
  independence_evidence, sur SON scope exact. Une dépendance limite de façon
  conservatrice (competency_only / whole_observation couvrent tout le
  périmètre de la cible) ; une preuve d'indépendance ne couvre que le
  périmètre établi (un scope non localisé sur une cible localized n'est
  attaché à aucune capacité).

- Temps : le schéma ne porte aucun horodatage pédagogique canonique.
  CognitiveEvent.started_at / closed_at sont exposés comme
  event_*_at_technical, PedagogicalObservation.created_at comme
  observation_created_at_inference ; ils ne sont jamais rebaptisés
  event_time et ne servent à aucun âge, délai, seuil ni qualification de
  séparation temporelle. demonstration_time vaut None et
  demonstration_time_source « unavailable » (champ prévu pour un futur
  horodatage canonique T2). L'ordre des collections est un ordre
  d'affichage technique stable, jamais une chronologie cognitive.

- Anti-N+1 : un nombre constant de SELECT groupés par dossier (run,
  inputs, observations + runs T3 + événements, mappings T4, définitions de
  la release, puis chaque famille de relations et ses périmètres).
"""
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from types import MappingProxyType

from sqlalchemy import select

from core.models import (
    CapabilityTaxonomyMembership,
    CognitiveEvent,
    CoreCapabilityDefinition,
    DependencyCapability,
    LongitudinalAssessmentInput,
    LongitudinalAssessmentRun,
    ObservationCapability,
    ObservationDependency,
    ObservationEvaluationRun,
    ObservationRevalidation,
    ObservationTransfer,
    PedagogicalObservation,
    RevalidationCapability,
    SupportTrace,
    TransferCapability,
)

# Statuts (vocabulaires T3 / T5, lus seulement).
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANDIDATE = "candidate"
ACTIVE = "active"
SUPERSEDED = "superseded"
OBSOLETE = "obsolete"
# Seuls états d'un dossier stabilisé et valide pour une lecture.
READABLE_RUN_STATES = frozenset({(COMPLETED, ACTIVE), (COMPLETED, SUPERSEDED)})
# États légitimes mais non lisibles (en cours, ou échec).
UNSTABILIZED_RUN_STATES = frozenset({(RUNNING, CANDIDATE), (FAILED, OBSOLETE)})
# Un run T3 d'un snapshot était completed / active ; il peut être superseded depuis.
T3_SNAPSHOT_INTERPRETATIONS = frozenset({ACTIVE, SUPERSEDED})
FINALIZED = "finalized"
VALID = "valid"

# PedagogicalObservation (vocabulaire T3, lu seulement).
SUPPORTIVE = "supportive"
CONTRADICTORY = "contradictory"
LOCAL_STAGE_NONE = "none"
APPLICATION = "application"
# Profondeurs locales positives, dans l'ordre du référentiel (jamais un rang
# numérique ni un stade utilisateur).
POSITIVE_LOCAL_STAGES = ("discovery", "comprehension", APPLICATION)

# capability_localization (T3) et scope_mode (T5).
LOCALIZED = "localized"
COMPETENCY_ONLY = "competency_only"
WHOLE_OBSERVATION = "whole_observation"
SCOPE_MODES = frozenset({WHOLE_OBSERVATION, LOCALIZED, COMPETENCY_ONLY})
SOURCE_OBSERVATION = "observation"
SOURCE_SUPPORT_TRACE = "support_trace"
SOURCE_KINDS = frozenset({SOURCE_OBSERVATION, SOURCE_SUPPORT_TRACE})
DEPENDENT = "dependent"
PARTIALLY_DEPENDENT = "partially_dependent"
DEPENDENCY_TYPES = frozenset({DEPENDENT, PARTIALLY_DEPENDENT})
COMPETENCY_CODES = frozenset(f"C{i}" for i in range(1, 13))

# Formats canoniques V1 de T5-B (réimplémentés, parité testée).
INPUT_SCHEMA_VERSION = 1
SCOPE_SCHEMA_VERSION = 1
# Version du format de la vue T5-C (to_payload).
VIEW_SCHEMA_VERSION = 1

# Granularité d'un périmètre de lecture.
CAPABILITY_GRANULARITY = "capability"
COMPETENCY_GRANULARITY = "competency_only"

# Lecture d'un périmètre (dependency_profile). « independent » n'existe pas.
INDEPENDENCE_EVIDENCE = "independence_evidence"
NOT_ESTABLISHED = "not_established"
SCOPE_CLASSIFICATIONS = (DEPENDENT, PARTIALLY_DEPENDENT, INDEPENDENCE_EVIDENCE, NOT_ESTABLISHED)

# coverage_profile : non observé != faible.
OBSERVED_POSITIVE = "observed_positive"
NOT_OBSERVED_POSITIVE = "not_observed_positive"

# consistency_profile.
OPEN_HISTORICAL_TENSION = "open_historical_tension"
HISTORICALLY_REVALIDATED = "historically_revalidated"
CONTRADICTION_WITHOUT_COMPARABLE_POSITIVE = "historical_contradiction_without_comparable_positive"
STRUCTURAL_RECURRENCE_CANDIDATE = "structural_recurrence_candidate"

# Relations.
DEPENDENCY = "dependency"
TRANSFER = "transfer"
REVALIDATION = "revalidation"

# temporal_validation_profile.
TECHNICAL_TIMESTAMPS_ONLY = "technical_timestamps_only"
DEMONSTRATION_TIME_UNAVAILABLE = "unavailable"
CANONICAL_EVENT_TIME = "canonical_event_time"

# limitations : limites de représentation, jamais des faiblesses utilisateur.
EMPTY_SNAPSHOT = "empty_snapshot"
EXACT_DEMONSTRATION_TIME_UNAVAILABLE = "exact_demonstration_time_unavailable"
COGNITIVE_CONTEXT_FAMILY_NOT_PERSISTED = "cognitive_context_family_not_persisted"
SEMANTIC_CONTRADICTION_MOTIF_NOT_PERSISTED = "semantic_contradiction_motif_not_persisted"
COMPETENCY_ONLY_SCOPE_NOT_LOCALIZABLE = "competency_only_scope_not_localizable"
INDEPENDENCE_NOT_ESTABLISHED_FOR_SOME_OBSERVATIONS = "independence_not_established_for_some_observations"
CROSS_RELEASE_MAPPINGS_NOT_RETAINED = "cross_release_capability_mappings_not_retained"
UNLOCALIZED_RELATION_SCOPE = "relation_scope_not_localized_on_capabilities"
UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT = "upstream_evidence_changed_since_snapshot"


class LongitudinalViewError(Exception):
    """Erreur métier de la reconstruction du dossier longitudinal."""


class InvalidLongitudinalViewArgument(LongitudinalViewError):
    """Argument structurellement invalide (type, chaîne vide, vocabulaire)."""


class LongitudinalDossierNotFound(LongitudinalViewError):
    """Aucun LongitudinalAssessmentRun pour cet identifiant."""


class LongitudinalDossierNotStabilized(LongitudinalViewError):
    """Run légitime mais non lisible : running / candidate (dossier non
    stabilisé) ou failed / obsolete (dossier non valide pour l'inférence)."""


class InvalidLongitudinalDossier(LongitudinalViewError):
    """Dossier manifestement corrompu ou incohérent : refusé, jamais réparé."""


# --------------------------------------------------------------------------
# Structures immuables de la vue
# --------------------------------------------------------------------------

def _primitive(value):
    if is_dataclass(value):
        return {f.name: _primitive(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    return value


class _View:
    """to_payload() : primitives JSON (UUID en str canonique, datetime en ISO
    8601, tuples en listes). Jamais persisté par T5-C."""

    def to_payload(self) -> dict:
        return _primitive(self)


@dataclass(frozen=True)
class CapabilityRef(_View):
    """UNE signification de capacité (definition_id), jamais un code seul."""
    definition_id: uuid.UUID
    capability_code: str
    semantic_revision: int
    label: str


@dataclass(frozen=True)
class ScopeUnit(_View):
    """Périmètre de lecture : une capacité précise, ou la compétence entière
    quand la preuve n'est pas localisable (competency_only, capability=None)."""
    granularity: str
    capability: CapabilityRef | None


@dataclass(frozen=True)
class EventProvenance(_View):
    """Provenance d'UN CognitiveEvent (une seule occasion cognitive
    longitudinale). Horodatages techniques uniquement : demonstration_time
    reste None tant qu'aucun horodatage pédagogique canonique n'existe."""
    event_id: uuid.UUID
    event_origin: str
    task_kind: str | None
    analysis_session_id: uuid.UUID | None
    conversation_key: str | None
    event_started_at_technical: datetime
    event_closed_at_technical: datetime | None
    demonstration_time: datetime | None
    demonstration_time_source: str


@dataclass(frozen=True)
class HistoryObservation(_View):
    """Une observation du snapshot, telle que persistée (T3) et localisée
    (T4, définitions compatibles avec la release du run uniquement).
    current_* = état amont relu maintenant, jamais celui du snapshot."""
    observation_id: uuid.UUID
    evaluation_run_id: uuid.UUID
    event_id: uuid.UUID
    ordinal: int
    competency_code: str
    polarity: str
    evidence_strength: str
    local_stage: str | None
    contradiction_scope: str | None
    error_type: str | None
    observation_role: str
    task_kind: str | None
    elicitation_mode: str
    support_level: str
    capability_localization: str
    observation_text: str
    primary_user_action: Mapping
    contributive_user_actions: tuple
    residual_cognitive_work: Mapping
    source_contribution_refs: tuple
    compatible_capabilities: tuple
    source_taxonomy_release_id: uuid.UUID
    re_evaluates_run_id: uuid.UUID | None
    event: EventProvenance
    observation_created_at_inference: datetime
    current_integrity_status: str
    current_evaluation_run_interpretation_status: str


@dataclass(frozen=True)
class HistoryEpisode(_View):
    """Observations sœurs d'un même CognitiveEvent : elles peuvent élargir la
    couverture mais ne sont jamais des répétitions indépendantes, un
    transfert, une revalidation ni une preuve de durabilité entre elles."""
    event: EventProvenance
    evaluation_run_id: uuid.UUID
    observation_ids: tuple


@dataclass(frozen=True)
class ActiveHistory(_View):
    observations: tuple
    episodes: tuple


@dataclass(frozen=True)
class RelationScope(_View):
    """Périmètre sémantique RÉSOLU d'une relation : localized = définitions
    persistées ; whole_observation = scope compatible de la cible
    (dépendance) ou intersection source / cible (transfert, revalidation) ;
    competency_only = aucune capacité."""
    scope_mode: str
    capabilities: tuple
    scope_fingerprint: str


@dataclass(frozen=True)
class DependencyRelation(_View):
    relation_id: uuid.UUID
    target_observation_id: uuid.UUID
    target_event_id: uuid.UUID
    source_kind: str
    source_observation_id: uuid.UUID | None
    source_support_trace_id: uuid.UUID | None
    source_event_id: uuid.UUID
    source_support_kind: str | None
    dependency_type: str
    scope: RelationScope
    dependency_basis: Mapping


@dataclass(frozen=True)
class ScopeReading(_View):
    """Lecture factuelle d'un périmètre d'une observation : dependent /
    partially_dependent (arêtes de dépendance), independence_evidence
    (cible d'un transfert ou d'une revalidation sur CE périmètre), sinon
    not_established — jamais « independent »."""
    observation_id: uuid.UUID
    event_id: uuid.UUID
    unit: ScopeUnit
    classification: str
    dependency_relation_ids: tuple
    independence_evidence_relation_ids: tuple


@dataclass(frozen=True)
class UnlocalizedRelationEvidence(_View):
    """Relation dont le périmètre résolu n'est attaché à aucune capacité de
    l'observation concernée (scope competency_only, ou intersection vide) :
    conservée, jamais distribuée arbitrairement sur des capacités."""
    observation_id: uuid.UUID
    relation_kind: str
    relation_id: uuid.UUID


@dataclass(frozen=True)
class DependencyProfile(_View):
    dependencies: tuple
    scope_readings: tuple
    dependent_scopes: tuple
    partially_dependent_scopes: tuple
    independence_evidence_scopes: tuple
    not_established_scopes: tuple
    unlocalized_independence_evidence: tuple


@dataclass(frozen=True)
class CapabilityCoverage(_View):
    """Démonstrations d'UNE capacité de la release du run. Seules les
    supportive localisées de profondeur discovery / comprehension /
    application forment positive_depths ; supportive local_stage none et
    contradictory restent visibles à part, jamais comme couverture."""
    capability: CapabilityRef
    discovery: tuple
    comprehension: tuple
    application: tuple
    supportive_local_stage_none: tuple
    contradictory: tuple
    positive_depths: tuple
    positive_event_ids: tuple
    positive_observation_status: str


@dataclass(frozen=True)
class CoverageProfile(_View):
    capabilities: tuple
    competency_only_supportive: tuple
    competency_only_supportive_local_stage_none: tuple
    competency_only_contradictory: tuple


@dataclass(frozen=True)
class ContextDescriptor(_View):
    """Descripteurs NOMINAUX d'un épisode : jamais, à eux seuls, la preuve
    d'un contexte cognitivement différent."""
    event: EventProvenance
    observation_ids: tuple
    capabilities: tuple
    competency_only_observation_ids: tuple
    local_stages: tuple
    polarities: tuple


@dataclass(frozen=True)
class CognitiveVariation(_View):
    """Variation cognitive positivement établie : un transfert validé
    source_event -> target_event, sur son périmètre."""
    transfer_id: uuid.UUID
    source_event_id: uuid.UUID
    target_event_id: uuid.UUID
    source_observation_id: uuid.UUID
    target_observation_id: uuid.UUID
    scope: RelationScope


@dataclass(frozen=True)
class VarietyProfile(_View):
    nominal_context_descriptors: tuple
    demonstrated_cognitive_variation: tuple
    unclassified_context_event_ids: tuple


@dataclass(frozen=True)
class TransferRelation(_View):
    relation_id: uuid.UUID
    source_observation_id: uuid.UUID
    target_observation_id: uuid.UUID
    source_event_id: uuid.UUID
    target_event_id: uuid.UUID
    scope: RelationScope
    transfer_basis: Mapping
    source_local_stage: str
    target_local_stage: str


@dataclass(frozen=True)
class TransferProfile(_View):
    """Exactement les transferts persistés du run : aucune closure."""
    transfers: tuple


@dataclass(frozen=True)
class ContradictionRecord(_View):
    """Contradiction historique : toujours visible, jamais une pénalité."""
    observation_id: uuid.UUID
    event_id: uuid.UUID
    contradiction_scope: str
    error_type: str | None
    evidence_strength: str
    capability_localization: str
    capabilities: tuple
    observation_text: str


@dataclass(frozen=True)
class RevalidationRelation(_View):
    """Fait historique : la contradiction source reste intacte et visible."""
    relation_id: uuid.UUID
    source_contradiction_observation_id: uuid.UUID
    target_supportive_observation_id: uuid.UUID
    source_event_id: uuid.UUID
    target_event_id: uuid.UUID
    scope: RelationScope
    revalidation_basis: Mapping
    target_local_stage: str


@dataclass(frozen=True)
class TensionReading(_View):
    """Lecture d'UNE contradiction sur UN périmètre. open_historical_tension
    = preuve positive comparable dans le dossier ET aucune revalidation
    historique de ce périmètre ; ni fragilité, ni besoin futur (T6)."""
    contradiction_observation_id: uuid.UUID
    contradiction_event_id: uuid.UUID
    unit: ScopeUnit
    status: str
    comparable_positive_observation_ids: tuple
    revalidation_ids: tuple
    unlocalized_revalidation_ids: tuple


@dataclass(frozen=True)
class RecurrenceCandidate(_View):
    """Au moins deux occasions autonomes (événements distincts) de
    contradiction sur un même périmètre, sans dépendance connue sur ce
    périmètre : candidat STRUCTUREL, jamais une récurrence indépendante
    confirmée ni un motif cognitif confirmé. observation_ids / event_ids /
    occurrence_groups (une occasion par événement) ne portent que les
    occurrences autonomes ; dependency_linked_observation_ids liste les
    contradictions du périmètre rattachées à une lignée de dépendance
    (visibles, jamais comptées)."""
    kind: str
    common_scope: ScopeUnit
    observation_ids: tuple
    event_ids: tuple
    contradiction_scopes: tuple
    error_types: tuple
    occurrence_groups: tuple
    dependency_linked_observation_ids: tuple


@dataclass(frozen=True)
class ConsistencyProfile(_View):
    historical_contradictions: tuple
    historical_revalidations: tuple
    tension_readings: tuple
    open_historical_tensions: tuple
    historically_revalidated_tensions: tuple
    structural_recurrence_candidates: tuple


@dataclass(frozen=True)
class ObservationInferenceTime(_View):
    """Temps d'INTERPRÉTATION (création de l'observation), jamais le temps
    de démonstration."""
    observation_id: uuid.UUID
    event_id: uuid.UUID
    observation_created_at_inference: datetime


@dataclass(frozen=True)
class DirectionalRelation(_View):
    relation_kind: str
    relation_id: uuid.UUID
    source_observation_id: uuid.UUID | None
    source_support_trace_id: uuid.UUID | None
    source_event_id: uuid.UUID
    target_observation_id: uuid.UUID
    target_event_id: uuid.UUID
    scope: RelationScope


@dataclass(frozen=True)
class DurabilityEvidence(_View):
    """Remobilisation indépendante structurellement démontrée (transfert ou
    revalidation) entre deux épisodes distincts ; la séparation temporelle
    n'est jamais qualifiée (aucun horodatage canonique)."""
    relation_kind: str
    relation_id: uuid.UUID
    source_event_id: uuid.UUID
    target_event_id: uuid.UUID


@dataclass(frozen=True)
class TemporalValidationProfile(_View):
    exact_demonstration_time_available: bool
    temporal_precision: str
    events: tuple
    observation_inference_times: tuple
    positive_observation_ids: tuple
    positive_event_ids: tuple
    transfer_target_event_ids: tuple
    revalidation_target_event_ids: tuple
    directional_relations: tuple
    evidence_of_independent_remobilization: tuple
    durability_evidence_events: tuple


@dataclass(frozen=True)
class Limitation(_View):
    code: str
    observation_ids: tuple
    relation_ids: tuple


@dataclass(frozen=True)
class LongitudinalDossier(_View):
    """Vue reconstruite d'UN run T5 (référencé explicitement par run_id).
    Décrit l'histoire des preuves ; ne constitue aucune décision
    pédagogique."""
    view_schema_version: int
    run_id: uuid.UUID
    user_id: str
    competency_code: str
    pedagogical_taxonomy_release_id: uuid.UUID
    input_fingerprint: str
    execution_status: str
    interpretation_status: str
    dependency_version: str
    transfer_version: str
    revalidation_version: str
    relation_schema_version: str
    run_completed_at_technical: datetime
    active_history: ActiveHistory
    dependency_profile: DependencyProfile
    coverage_profile: CoverageProfile
    variety_profile: VarietyProfile
    transfer_profile: TransferProfile
    consistency_profile: ConsistencyProfile
    temporal_validation_profile: TemporalValidationProfile
    limitations: tuple


# --------------------------------------------------------------------------
# Utilitaires purs
# --------------------------------------------------------------------------

def _freeze(value):
    """Copie profonde immuable d'une valeur JSON (dict -> mappingproxy,
    list -> tuple)."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _canonical_sha256(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sorted_ids(ids) -> list:
    return sorted(str(value) for value in ids)


def _input_fingerprint(*, user_id: str, competency_code: str, release_id: uuid.UUID, evidence) -> str:
    """input_fingerprint V1 de T5-B. evidence : itérable de (observation_id,
    evaluation_run_id, event_id, source_release_id, compatible)."""
    observations = sorted((
        {
            "observation_id": str(observation_id),
            "evaluation_run_id": str(evaluation_run_id),
            "event_id": str(event_id),
            "source_taxonomy_release_id": str(source_release_id),
            "compatible_capability_definition_ids": _sorted_ids(compatible),
        }
        for observation_id, evaluation_run_id, event_id, source_release_id, compatible in evidence
    ), key=lambda observation: observation["observation_id"])
    return _canonical_sha256({
        "input_schema_version": INPUT_SCHEMA_VERSION,
        "user_id": user_id,
        "competency_code": competency_code,
        "pedagogical_taxonomy_release_id": str(release_id),
        "observations": observations,
    })


def _scope_fingerprint(competency_code: str, scope_mode: str, definitions) -> str:
    """scope_fingerprint V1 de T5-B (identifiants SÉMANTIQUES)."""
    return _canonical_sha256({
        "scope_schema_version": SCOPE_SCHEMA_VERSION,
        "competency_code": competency_code,
        "scope_mode": scope_mode,
        "capability_definition_ids": _sorted_ids(definitions),
    })


def _overlaps(first_mode: str, first_definitions, second_mode: str, second_definitions) -> bool:
    """Chevauchement conservateur de T5-B : seule une disjonction de deux
    scopes localized est démontrable."""
    if first_mode == LOCALIZED and second_mode == LOCALIZED:
        return bool(set(first_definitions) & set(second_definitions))
    return True


def _is_compatible(localization: str, source_release_id, compatible, release_id) -> bool:
    """Compatibility gate de T5-B."""
    if source_release_id is None:
        return False
    if localization == LOCALIZED:
        return bool(compatible)
    return source_release_id == release_id


def _capability_order(ref: CapabilityRef) -> tuple:
    """Ordre naturel C1_A < C1_B < ... < C2_A < ... < C10_A (jamais lexical)."""
    competency, letter = ref.capability_code.split("_", 1)
    return int(competency[1:]), letter, ref.semantic_revision, str(ref.definition_id)


def _unit_order(unit: ScopeUnit) -> tuple:
    if unit.capability is None:
        return (1,)
    return (0, *_capability_order(unit.capability))


def _unique(values) -> tuple:
    """Valeurs distinctes, dans l'ordre de première apparition."""
    return tuple(dict.fromkeys(values))


def _require_uuid(value, name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise InvalidLongitudinalViewArgument(f"{name} doit être un uuid.UUID")
    return value


def _require_text(value, name: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise InvalidLongitudinalViewArgument(f"{name} doit être une chaîne non vide")
    return value


def _corrupt(message: str):
    return InvalidLongitudinalDossier(message)


# --------------------------------------------------------------------------
# Lecture (SELECT uniquement, sous no_autoflush)
# --------------------------------------------------------------------------

def _load_run(db, run_id: uuid.UUID):
    return db.execute(
        select(*LongitudinalAssessmentRun.__table__.c).where(LongitudinalAssessmentRun.id == run_id)
    ).one_or_none()


def _snapshot_rows(db, run) -> dict:
    """{observation_id: row} des observations d'input du run, avec leur run
    T3 et leur événement."""
    input_ids = set(db.execute(
        select(LongitudinalAssessmentInput.observation_id).where(LongitudinalAssessmentInput.run_id == run.id)
    ).scalars())
    rows = db.execute(
        select(
            PedagogicalObservation.id, PedagogicalObservation.evaluation_run_id, PedagogicalObservation.ordinal,
            PedagogicalObservation.competency_code, PedagogicalObservation.observation_role,
            PedagogicalObservation.task_kind, PedagogicalObservation.primary_user_action,
            PedagogicalObservation.contributive_user_actions, PedagogicalObservation.elicitation_mode,
            PedagogicalObservation.support_level, PedagogicalObservation.source_contribution_refs,
            PedagogicalObservation.residual_cognitive_work, PedagogicalObservation.polarity,
            PedagogicalObservation.evidence_strength, PedagogicalObservation.local_stage,
            PedagogicalObservation.contradiction_scope, PedagogicalObservation.error_type,
            PedagogicalObservation.observation_text, PedagogicalObservation.capability_localization,
            PedagogicalObservation.integrity_status, PedagogicalObservation.created_at,
            ObservationEvaluationRun.event_id,
            ObservationEvaluationRun.execution_status.label("t3_execution_status"),
            ObservationEvaluationRun.interpretation_status.label("t3_interpretation_status"),
            ObservationEvaluationRun.pedagogical_taxonomy_release_id.label("source_release_id"),
            ObservationEvaluationRun.re_evaluates_run_id,
            CognitiveEvent.user_id.label("event_user_id"),
            CognitiveEvent.status.label("event_status"),
            CognitiveEvent.event_origin,
            CognitiveEvent.task_kind.label("event_task_kind"),
            CognitiveEvent.analysis_session_id,
            CognitiveEvent.conversation_key,
            CognitiveEvent.started_at.label("event_started_at"),
            CognitiveEvent.closed_at.label("event_closed_at"),
        )
        .join(ObservationEvaluationRun, ObservationEvaluationRun.id == PedagogicalObservation.evaluation_run_id)
        .join(CognitiveEvent, CognitiveEvent.id == ObservationEvaluationRun.event_id)
        .where(PedagogicalObservation.id.in_(
            select(LongitudinalAssessmentInput.observation_id).where(LongitudinalAssessmentInput.run_id == run.id)))
    ).all()
    found = {row.id: row for row in rows}
    missing = input_ids - set(found)
    if missing:
        raise _corrupt(f"observations d'input introuvables : {_sorted_ids(missing)}")
    return found


def _observation_mappings(db, run) -> dict:
    """{observation_id: [(release_id, definition_id, competency_code)]} des
    mappings T4 des observations d'input."""
    located = {}
    for observation_id, release_id, definition_id, competency_code in db.execute(
        select(ObservationCapability.observation_id, CapabilityTaxonomyMembership.taxonomy_release_id,
               CoreCapabilityDefinition.id, CoreCapabilityDefinition.competency_code)
        .join(CapabilityTaxonomyMembership,
              CapabilityTaxonomyMembership.id == ObservationCapability.capability_membership_id)
        .join(CoreCapabilityDefinition,
              CoreCapabilityDefinition.id == CapabilityTaxonomyMembership.capability_definition_id)
        .where(ObservationCapability.observation_id.in_(
            select(LongitudinalAssessmentInput.observation_id).where(LongitudinalAssessmentInput.run_id == run.id)))
    ).all():
        located.setdefault(observation_id, []).append((release_id, definition_id, competency_code))
    return located


def _release_definitions(db, release_id: uuid.UUID) -> tuple:
    """({definition_id: CapabilityRef}, {definition_id: competency_code}) de
    TOUTES les définitions membres de la release du run."""
    refs, competencies = {}, {}
    for definition_id, code, revision, label, competency_code in db.execute(
        select(CoreCapabilityDefinition.id, CoreCapabilityDefinition.capability_code,
               CoreCapabilityDefinition.semantic_revision, CoreCapabilityDefinition.label,
               CoreCapabilityDefinition.competency_code)
        .join(CapabilityTaxonomyMembership,
              CapabilityTaxonomyMembership.capability_definition_id == CoreCapabilityDefinition.id)
        .where(CapabilityTaxonomyMembership.taxonomy_release_id == release_id)
    ).all():
        refs[definition_id] = CapabilityRef(definition_id, code, revision, label)
        competencies[definition_id] = competency_code
    return refs, competencies


def _relation_rows(db, run, model, link_model, link_column):
    """(lignes de relation ORDER BY created_at, id ; {relation_id: [(membership_id,
    release_id, definition_id, competency_code)]})."""
    if model is ObservationDependency:
        statement = (
            select(*ObservationDependency.__table__.c,
                   SupportTrace.cognitive_event_id.label("trace_event_id"),
                   SupportTrace.support_kind.label("trace_support_kind"),
                   CognitiveEvent.user_id.label("trace_user_id"))
            .outerjoin(SupportTrace, SupportTrace.id == ObservationDependency.source_support_trace_id)
            .outerjoin(CognitiveEvent, CognitiveEvent.id == SupportTrace.cognitive_event_id)
        )
    else:
        statement = select(*model.__table__.c)
    rows = db.execute(statement.where(model.run_id == run.id).order_by(model.created_at, model.id)).all()
    links = {}
    for relation_id, membership_id, release_id, definition_id, competency_code in db.execute(
        select(link_column, CapabilityTaxonomyMembership.id, CapabilityTaxonomyMembership.taxonomy_release_id,
               CoreCapabilityDefinition.id, CoreCapabilityDefinition.competency_code)
        .join(CapabilityTaxonomyMembership, CapabilityTaxonomyMembership.id == link_model.capability_membership_id)
        .join(CoreCapabilityDefinition,
              CoreCapabilityDefinition.id == CapabilityTaxonomyMembership.capability_definition_id)
        .where(link_column.in_(select(model.id).where(model.run_id == run.id)))
    ).all():
        links.setdefault(relation_id, []).append((membership_id, release_id, definition_id, competency_code))
    return rows, links


# --------------------------------------------------------------------------
# Construction de l'historique actif (avec défense)
# --------------------------------------------------------------------------

def _event_provenance(row) -> EventProvenance:
    return EventProvenance(
        event_id=row.event_id,
        event_origin=row.event_origin,
        task_kind=row.event_task_kind,
        analysis_session_id=row.analysis_session_id,
        conversation_key=row.conversation_key,
        event_started_at_technical=row.event_started_at,
        event_closed_at_technical=row.event_closed_at,
        # Aucun horodatage pédagogique canonique dans le schéma actuel.
        demonstration_time=None,
        demonstration_time_source=DEMONSTRATION_TIME_UNAVAILABLE,
    )


def _history(run, rows: dict, mappings: dict, release_refs: dict) -> tuple:
    """(observations ordonnées, {observation_id: frozenset(definitions
    compatibles)}, observations dont des mappings n'ont pas été retenus)."""
    compatible_by_observation, dropped, observations, run_by_event = {}, [], [], {}
    for observation_id, row in rows.items():
        if row.competency_code != run.competency_code:
            raise _corrupt(f"observation {observation_id} de compétence {row.competency_code},"
                           f" run {run.competency_code}")
        if row.event_user_id != run.user_id:
            raise _corrupt(f"observation {observation_id} d'un autre utilisateur")
        if row.event_status != FINALIZED:
            raise _corrupt(f"observation {observation_id} d'un événement {row.event_status}")
        if row.t3_execution_status != COMPLETED or row.t3_interpretation_status not in T3_SNAPSHOT_INTERPRETATIONS:
            raise _corrupt(f"observation {observation_id} d'un run T3 {row.t3_execution_status} /"
                           f" {row.t3_interpretation_status}")
        previous_run = run_by_event.setdefault(row.event_id, row.evaluation_run_id)
        if previous_run != row.evaluation_run_id:
            raise _corrupt(f"deux runs T3 du même événement {row.event_id} dans le snapshot")
        mapped = [definition_id for release_id, definition_id, competency_code in mappings.get(observation_id, ())
                  if release_id == row.source_release_id and competency_code == row.competency_code]
        compatible = frozenset()
        if row.source_release_id is not None and row.capability_localization == LOCALIZED:
            compatible = frozenset(d for d in mapped if d in release_refs)
            if len(set(mapped)) != len(compatible):
                dropped.append(observation_id)
        if not _is_compatible(row.capability_localization, row.source_release_id, compatible,
                              run.pedagogical_taxonomy_release_id):
            raise _corrupt(f"observation {observation_id} incompatible avec la release du run")
        compatible_by_observation[observation_id] = compatible
        observations.append(HistoryObservation(
            observation_id=observation_id,
            evaluation_run_id=row.evaluation_run_id,
            event_id=row.event_id,
            ordinal=row.ordinal,
            competency_code=row.competency_code,
            polarity=row.polarity,
            evidence_strength=row.evidence_strength,
            local_stage=row.local_stage,
            contradiction_scope=row.contradiction_scope,
            error_type=row.error_type,
            observation_role=row.observation_role,
            task_kind=row.task_kind,
            elicitation_mode=row.elicitation_mode,
            support_level=row.support_level,
            capability_localization=row.capability_localization,
            observation_text=row.observation_text,
            primary_user_action=_freeze(row.primary_user_action),
            contributive_user_actions=_freeze(row.contributive_user_actions),
            residual_cognitive_work=_freeze(row.residual_cognitive_work),
            source_contribution_refs=_freeze(row.source_contribution_refs),
            compatible_capabilities=tuple(sorted((release_refs[d] for d in compatible), key=_capability_order)),
            source_taxonomy_release_id=row.source_release_id,
            re_evaluates_run_id=row.re_evaluates_run_id,
            event=_event_provenance(row),
            observation_created_at_inference=row.created_at,
            current_integrity_status=row.integrity_status,
            current_evaluation_run_interpretation_status=row.t3_interpretation_status,
        ))
    # Ordre d'affichage technique stable (jamais une chronologie cognitive).
    observations.sort(key=lambda o: (o.event.event_started_at_technical, str(o.event_id), o.ordinal,
                                     str(o.observation_id)))
    fingerprint = _input_fingerprint(
        user_id=run.user_id, competency_code=run.competency_code, release_id=run.pedagogical_taxonomy_release_id,
        evidence=[(o.observation_id, o.evaluation_run_id, o.event_id, o.source_taxonomy_release_id,
                   compatible_by_observation[o.observation_id]) for o in observations])
    if fingerprint != run.input_fingerprint:
        raise _corrupt(f"run {run.id} : input_fingerprint différent du recalcul du snapshot")
    return tuple(observations), compatible_by_observation, tuple(sorted(dropped, key=str))


def _episodes(observations) -> tuple:
    grouped = {}
    for observation in observations:
        grouped.setdefault(observation.event_id, []).append(observation)
    return tuple(HistoryEpisode(event=items[0].event, evaluation_run_id=items[0].evaluation_run_id,
                                observation_ids=tuple(o.observation_id for o in items))
                 for items in grouped.values())


# --------------------------------------------------------------------------
# Relations (défense) et périmètres
# --------------------------------------------------------------------------

class _Resolved:
    """Relation relue et vérifiée : identité, extrémités et périmètre
    résolu (mode, définitions)."""
    __slots__ = ("kind", "row", "target", "source", "mode", "definitions", "scope", "key")

    def __init__(self, kind, row, target, source, mode, definitions, scope, key):
        self.kind, self.row, self.target, self.source = kind, row, target, source
        self.mode, self.definitions, self.scope, self.key = mode, definitions, scope, key


def _check_basis(row, basis, name):
    if not isinstance(basis, Mapping) or not basis:
        raise _corrupt(f"relation {row.id} : {name} doit être un objet JSON non vide")


def _resolve_scope(run, row, links, available, release_refs) -> tuple:
    """(mode, définitions, RelationScope) : memberships de la release et de
    la compétence du run, localized ⊆ available, scope_fingerprint égal au
    recalcul."""
    mode = row.scope_mode
    if mode not in SCOPE_MODES:
        raise _corrupt(f"relation {row.id} : scope_mode {mode!r} hors vocabulaire")
    if mode == LOCALIZED and not links:
        raise _corrupt(f"relation {row.id} : {LOCALIZED} sans membership")
    if mode != LOCALIZED and links:
        raise _corrupt(f"relation {row.id} : {mode} avec {len(links)} membership(s)")
    for membership_id, release_id, _, competency_code in links:
        if release_id != run.pedagogical_taxonomy_release_id:
            raise _corrupt(f"relation {row.id} : membership {membership_id} d'une autre release")
        if competency_code != run.competency_code:
            raise _corrupt(f"relation {row.id} : membership {membership_id} hors de {run.competency_code}")
    definitions = {definition_id for _, _, definition_id, _ in links}
    if mode == LOCALIZED:
        if definitions - available:
            raise _corrupt(f"relation {row.id} : définitions hors du scope compatible")
        resolved = frozenset(definitions)
    elif mode == WHOLE_OBSERVATION:
        resolved = frozenset(available)
    else:
        resolved = frozenset()
    fingerprint = _scope_fingerprint(run.competency_code, mode, resolved)
    if fingerprint != row.scope_fingerprint:
        raise _corrupt(f"relation {row.id} : scope_fingerprint différent du recalcul")
    scope = RelationScope(scope_mode=mode,
                          capabilities=tuple(sorted((release_refs[d] for d in resolved), key=_capability_order)),
                          scope_fingerprint=row.scope_fingerprint)
    return mode, resolved, scope


def _member(snapshot: dict, observation_id, row, name):
    observation = snapshot.get(observation_id)
    if observation is None:
        raise _corrupt(f"relation {row.id} : {name} {observation_id} hors du snapshot du run")
    return observation


def _dependencies(run, rows, links, snapshot, compatible, release_refs) -> list:
    resolved = []
    for row in rows:
        if row.source_kind not in SOURCE_KINDS or row.dependency_type not in DEPENDENCY_TYPES:
            raise _corrupt(f"relation {row.id} : source_kind / dependency_type hors vocabulaire")
        if row.source_kind == SOURCE_OBSERVATION:
            if row.source_observation_id is None or row.source_support_trace_id is not None:
                raise _corrupt(f"relation {row.id} : source observation XOR incohérent")
            if row.source_observation_id == row.target_observation_id:
                raise _corrupt(f"relation {row.id} : auto-dépendance")
        elif row.source_support_trace_id is None or row.source_observation_id is not None:
            raise _corrupt(f"relation {row.id} : source support_trace XOR incohérent")
        _check_basis(row, row.dependency_basis, "dependency_basis")
        target = _member(snapshot, row.target_observation_id, row, "cible")
        if row.source_kind == SOURCE_OBSERVATION:
            source = _member(snapshot, row.source_observation_id, row, "source")
            if source.event_id == target.event_id:
                raise _corrupt(f"relation {row.id} : dépendance dans un même événement")
        else:
            source = None
            if row.trace_event_id is None:
                raise _corrupt(f"relation {row.id} : support_trace introuvable")
            if row.trace_user_id != run.user_id:
                raise _corrupt(f"relation {row.id} : support_trace d'un autre utilisateur")
            if row.trace_event_id == target.event_id:
                raise _corrupt(f"relation {row.id} : aide du même événement que la cible")
        mode, definitions, scope = _resolve_scope(run, row, links.get(row.id, []),
                                                  compatible[target.observation_id], release_refs)
        key = (row.target_observation_id, row.source_kind, row.source_observation_id, row.source_support_trace_id,
               row.scope_fingerprint)
        resolved.append(_Resolved(DEPENDENCY, row, target, source, mode, definitions, scope, key))
    seen = {}
    for item in resolved:
        if item.key in seen:
            raise _corrupt(f"dépendance {item.row.id} : même dépendance déjà classée"
                           f" {seen[item.key]} (doublon ou classification contradictoire)")
        seen[item.key] = item.row.dependency_type
    return resolved


def _pairs(kind, run, rows, links, snapshot, compatible, release_refs, source_column, target_column,
           basis_column) -> list:
    """Transferts (supportive -> supportive application) ou revalidations
    (contradictory -> supportive), extrémités d'événements distincts."""
    resolved = []
    for row in rows:
        source_id, target_id = getattr(row, source_column), getattr(row, target_column)
        if source_id == target_id:
            raise _corrupt(f"relation {row.id} : source et cible identiques")
        _check_basis(row, getattr(row, basis_column), basis_column)
        source = _member(snapshot, source_id, row, "source")
        target = _member(snapshot, target_id, row, "cible")
        if source.event_id == target.event_id:
            raise _corrupt(f"relation {row.id} : source et cible du même événement")
        expected_source = SUPPORTIVE if kind == TRANSFER else CONTRADICTORY
        if source.polarity != expected_source or target.polarity != SUPPORTIVE:
            raise _corrupt(f"relation {row.id} : polarités {source.polarity} -> {target.polarity}")
        if kind == TRANSFER and target.local_stage != APPLICATION:
            raise _corrupt(f"relation {row.id} : cible de transfert local_stage {target.local_stage}")
        common = compatible[source_id] & compatible[target_id]
        if source.capability_localization == LOCALIZED and target.capability_localization == LOCALIZED \
                and not common:
            raise _corrupt(f"relation {row.id} : aucune définition compatible commune")
        mode, definitions, scope = _resolve_scope(run, row, links.get(row.id, []), common, release_refs)
        resolved.append(_Resolved(kind, row, target, source, mode, definitions, scope,
                                  (source_id, target_id, row.scope_fingerprint)))
    keys = [item.key for item in resolved]
    if len(set(keys)) != len(keys):
        raise _corrupt(f"{kind} persisté en double")
    return resolved


def _check_no_overlap(dependencies, others) -> None:
    for other in others:
        for dependency in dependencies:
            if dependency.target.observation_id == other.target.observation_id and _overlaps(
                    dependency.mode, dependency.definitions, other.mode, other.definitions):
                raise _corrupt(f"{other.kind} {other.row.id} chevauchant la dépendance {dependency.row.id}")


def _units(observation: HistoryObservation) -> tuple:
    if observation.capability_localization == LOCALIZED:
        return tuple(ScopeUnit(CAPABILITY_GRANULARITY, ref) for ref in observation.compatible_capabilities)
    return (ScopeUnit(COMPETENCY_GRANULARITY, None),)


def _unit_key(unit: ScopeUnit):
    return None if unit.capability is None else unit.capability.definition_id


def _limits(dependency: _Resolved, unit_key) -> bool:
    """Une dépendance limite de façon CONSERVATRICE : localized = ses
    définitions ; whole_observation / competency_only = tout le périmètre de
    la cible."""
    return dependency.mode != LOCALIZED or unit_key in dependency.definitions


def _establishes(relation: _Resolved, unit_key) -> bool:
    """Une preuve d'indépendance / une revalidation ne couvre que son
    périmètre ÉTABLI. unit_key None (observation competency_only) : toute la
    compétence (localized impossible, déjà refusé)."""
    if unit_key is None:
        return True
    return unit_key in relation.definitions


# --------------------------------------------------------------------------
# Profils
# --------------------------------------------------------------------------

def _relation_scope_empty_on_localized(relation: _Resolved, observation: HistoryObservation) -> bool:
    return observation.capability_localization == LOCALIZED and not relation.definitions


def _dependency_profile(observations, dependencies, transfers, revalidations) -> DependencyProfile:
    readings, unlocalized = [], []
    independence = transfers + revalidations
    for observation in observations:
        for unit in _units(observation):
            key = _unit_key(unit)
            limiting = [d for d in dependencies if d.target.observation_id == observation.observation_id
                        and _limits(d, key)]
            evidence = [r for r in independence if r.target.observation_id == observation.observation_id
                        and _establishes(r, key)]
            if any(d.row.dependency_type == DEPENDENT for d in limiting):
                classification = DEPENDENT
            elif limiting:
                classification = PARTIALLY_DEPENDENT
            elif evidence:
                classification = INDEPENDENCE_EVIDENCE
            else:
                classification = NOT_ESTABLISHED
            readings.append(ScopeReading(
                observation_id=observation.observation_id, event_id=observation.event_id, unit=unit,
                classification=classification,
                dependency_relation_ids=tuple(d.row.id for d in limiting),
                independence_evidence_relation_ids=tuple(r.row.id for r in evidence)))
        for relation in independence:
            if relation.target.observation_id == observation.observation_id and \
                    _relation_scope_empty_on_localized(relation, observation):
                unlocalized.append(UnlocalizedRelationEvidence(observation.observation_id, relation.kind,
                                                               relation.row.id))
    by = {c: tuple(r for r in readings if r.classification == c) for c in SCOPE_CLASSIFICATIONS}
    return DependencyProfile(
        dependencies=tuple(DependencyRelation(
            relation_id=d.row.id,
            target_observation_id=d.target.observation_id,
            target_event_id=d.target.event_id,
            source_kind=d.row.source_kind,
            source_observation_id=d.row.source_observation_id,
            source_support_trace_id=d.row.source_support_trace_id,
            source_event_id=d.source.event_id if d.source is not None else d.row.trace_event_id,
            source_support_kind=d.row.trace_support_kind if d.source is None else None,
            dependency_type=d.row.dependency_type,
            scope=d.scope,
            dependency_basis=_freeze(d.row.dependency_basis),
        ) for d in dependencies),
        scope_readings=tuple(readings),
        dependent_scopes=by[DEPENDENT],
        partially_dependent_scopes=by[PARTIALLY_DEPENDENT],
        independence_evidence_scopes=by[INDEPENDENCE_EVIDENCE],
        not_established_scopes=by[NOT_ESTABLISHED],
        unlocalized_independence_evidence=tuple(unlocalized),
    )


def _is_positive(observation: HistoryObservation) -> bool:
    return observation.polarity == SUPPORTIVE and observation.local_stage in POSITIVE_LOCAL_STAGES


def _coverage_profile(run, observations, release_refs, release_competencies) -> CoverageProfile:
    capabilities = sorted((ref for d, ref in release_refs.items() if release_competencies[d] == run.competency_code),
                          key=_capability_order)
    entries = []
    for ref in capabilities:
        on = [o for o in observations if o.capability_localization == LOCALIZED
              and ref.definition_id in {c.definition_id for c in o.compatible_capabilities}]
        by_depth = {depth: tuple(o.observation_id for o in on if o.polarity == SUPPORTIVE and o.local_stage == depth)
                    for depth in POSITIVE_LOCAL_STAGES}
        depths = tuple(depth for depth in POSITIVE_LOCAL_STAGES if by_depth[depth])
        entries.append(CapabilityCoverage(
            capability=ref,
            discovery=by_depth["discovery"],
            comprehension=by_depth["comprehension"],
            application=by_depth[APPLICATION],
            supportive_local_stage_none=tuple(o.observation_id for o in on if o.polarity == SUPPORTIVE
                                              and o.local_stage == LOCAL_STAGE_NONE),
            contradictory=tuple(o.observation_id for o in on if o.polarity == CONTRADICTORY),
            positive_depths=depths,
            positive_event_ids=_unique(o.event_id for o in on if _is_positive(o)),
            positive_observation_status=OBSERVED_POSITIVE if depths else NOT_OBSERVED_POSITIVE,
        ))
    only = [o for o in observations if o.capability_localization == COMPETENCY_ONLY]
    return CoverageProfile(
        capabilities=tuple(entries),
        competency_only_supportive=tuple(o.observation_id for o in only if _is_positive(o)),
        competency_only_supportive_local_stage_none=tuple(o.observation_id for o in only if o.polarity == SUPPORTIVE
                                                          and o.local_stage == LOCAL_STAGE_NONE),
        competency_only_contradictory=tuple(o.observation_id for o in only if o.polarity == CONTRADICTORY),
    )


def _variety_profile(observations, episodes, transfers) -> VarietyProfile:
    by_id = {o.observation_id: o for o in observations}
    descriptors = []
    for episode in episodes:
        items = [by_id[i] for i in episode.observation_ids]
        refs = {c.definition_id: c for o in items for c in o.compatible_capabilities}
        seen_local_stages = {o.local_stage for o in items if o.local_stage is not None}
        descriptors.append(ContextDescriptor(
            event=episode.event,
            observation_ids=episode.observation_ids,
            capabilities=tuple(sorted(refs.values(), key=_capability_order)),
            competency_only_observation_ids=tuple(o.observation_id for o in items
                                                  if o.capability_localization == COMPETENCY_ONLY),
            local_stages=tuple(s for s in (LOCAL_STAGE_NONE, *POSITIVE_LOCAL_STAGES) if s in seen_local_stages),
            polarities=tuple(p for p in (SUPPORTIVE, CONTRADICTORY) if any(o.polarity == p for o in items)),
        ))
    variations = tuple(CognitiveVariation(
        transfer_id=t.row.id, source_event_id=t.source.event_id, target_event_id=t.target.event_id,
        source_observation_id=t.source.observation_id, target_observation_id=t.target.observation_id,
        scope=t.scope) for t in transfers)
    involved = {e for v in variations for e in (v.source_event_id, v.target_event_id)}
    return VarietyProfile(
        nominal_context_descriptors=tuple(descriptors),
        demonstrated_cognitive_variation=variations,
        unclassified_context_event_ids=tuple(e.event.event_id for e in episodes if e.event.event_id not in involved),
    )


def _transfer_profile(transfers) -> TransferProfile:
    return TransferProfile(transfers=tuple(TransferRelation(
        relation_id=t.row.id,
        source_observation_id=t.source.observation_id,
        target_observation_id=t.target.observation_id,
        source_event_id=t.source.event_id,
        target_event_id=t.target.event_id,
        scope=t.scope,
        transfer_basis=_freeze(t.row.transfer_basis),
        source_local_stage=t.source.local_stage,
        target_local_stage=t.target.local_stage,
    ) for t in transfers))


def _recurrence_candidates(contradictions, dependencies) -> tuple:
    """Par périmètre. Une contradiction ciblée par une dépendance (dependent
    OU partially_dependent, quelle que soit la source : observation du
    groupe ou non, supportive, aide) qui chevauche ce périmètre (règle
    conservatrice de _limits) est une occurrence DÉPENDANTE : rattachée à sa
    lignée, elle ne compte jamais comme occurrence autonome. Les
    contradictions restantes d'un même événement forment une seule
    occasion. Candidat seulement si au moins deux occasions autonomes
    distinctes : aucune dépendance CONNUE n'invalide la lecture, ce qui ne
    prouve jamais leur indépendance."""
    by_unit = {}
    for observation in contradictions:
        for unit in _units(observation):
            by_unit.setdefault(_unit_key(unit), (unit, []))[1].append(observation)
    candidates = []
    for key, (unit, items) in sorted(by_unit.items(), key=lambda entry: _unit_order(entry[1][0])):
        linked = {o.observation_id for o in items
                  if any(d.target.observation_id == o.observation_id and _limits(d, key) for d in dependencies)}
        autonomous = [o for o in items if o.observation_id not in linked]
        occasions = {}
        for observation in autonomous:
            occasions.setdefault(observation.event_id, []).append(observation.observation_id)
        if len(occasions) < 2:
            continue
        candidates.append(RecurrenceCandidate(
            kind=STRUCTURAL_RECURRENCE_CANDIDATE,
            common_scope=unit,
            observation_ids=tuple(o.observation_id for o in autonomous),
            event_ids=tuple(occasions),
            contradiction_scopes=tuple(sorted({o.contradiction_scope for o in autonomous})),
            error_types=tuple(sorted({o.error_type for o in autonomous if o.error_type is not None})),
            occurrence_groups=tuple(tuple(group) for group in occasions.values()),
            dependency_linked_observation_ids=tuple(o.observation_id for o in items if o.observation_id in linked),
        ))
    return tuple(candidates)


def _consistency_profile(observations, dependencies, revalidations) -> ConsistencyProfile:
    contradictions = [o for o in observations if o.polarity == CONTRADICTORY]
    positives = [o for o in observations if _is_positive(o)]
    readings = []
    for contradiction in contradictions:
        mine = [r for r in revalidations if r.source.observation_id == contradiction.observation_id]
        unlocalized = tuple(r.row.id for r in mine if _relation_scope_empty_on_localized(r, contradiction))
        for unit in _units(contradiction):
            key = _unit_key(unit)
            if key is None:
                # Périmètre exact inconnu : chevauchement conservateur avec
                # toute preuve positive de la compétence.
                comparable = positives
            else:
                comparable = [p for p in positives if p.capability_localization == LOCALIZED
                              and key in {c.definition_id for c in p.compatible_capabilities}]
            closing = tuple(r.row.id for r in mine if _establishes(r, key))
            if closing:
                status = HISTORICALLY_REVALIDATED
            elif comparable:
                status = OPEN_HISTORICAL_TENSION
            else:
                status = CONTRADICTION_WITHOUT_COMPARABLE_POSITIVE
            readings.append(TensionReading(
                contradiction_observation_id=contradiction.observation_id,
                contradiction_event_id=contradiction.event_id,
                unit=unit,
                status=status,
                comparable_positive_observation_ids=tuple(p.observation_id for p in comparable),
                revalidation_ids=closing,
                unlocalized_revalidation_ids=unlocalized,
            ))
    return ConsistencyProfile(
        historical_contradictions=tuple(ContradictionRecord(
            observation_id=o.observation_id, event_id=o.event_id, contradiction_scope=o.contradiction_scope,
            error_type=o.error_type, evidence_strength=o.evidence_strength,
            capability_localization=o.capability_localization, capabilities=o.compatible_capabilities,
            observation_text=o.observation_text) for o in contradictions),
        historical_revalidations=tuple(RevalidationRelation(
            relation_id=r.row.id,
            source_contradiction_observation_id=r.source.observation_id,
            target_supportive_observation_id=r.target.observation_id,
            source_event_id=r.source.event_id,
            target_event_id=r.target.event_id,
            scope=r.scope,
            revalidation_basis=_freeze(r.row.revalidation_basis),
            target_local_stage=r.target.local_stage,
        ) for r in revalidations),
        tension_readings=tuple(readings),
        open_historical_tensions=tuple(r for r in readings if r.status == OPEN_HISTORICAL_TENSION),
        historically_revalidated_tensions=tuple(r for r in readings if r.status == HISTORICALLY_REVALIDATED),
        structural_recurrence_candidates=_recurrence_candidates(contradictions, dependencies),
    )


def _directional(relation: _Resolved) -> DirectionalRelation:
    row = relation.row
    return DirectionalRelation(
        relation_kind=relation.kind,
        relation_id=row.id,
        source_observation_id=relation.source.observation_id if relation.source is not None else None,
        source_support_trace_id=row.source_support_trace_id if relation.kind == DEPENDENCY else None,
        source_event_id=relation.source.event_id if relation.source is not None else row.trace_event_id,
        target_observation_id=relation.target.observation_id,
        target_event_id=relation.target.event_id,
        scope=relation.scope,
    )


def _temporal_profile(observations, episodes, dependencies, transfers, revalidations) -> TemporalValidationProfile:
    positives = [o for o in observations if _is_positive(o)]
    remobilization = tuple(_directional(r) for r in transfers + revalidations)
    events = tuple(e.event for e in episodes)
    return TemporalValidationProfile(
        exact_demonstration_time_available=bool(events) and all(e.demonstration_time is not None for e in events),
        temporal_precision=TECHNICAL_TIMESTAMPS_ONLY,
        events=events,
        observation_inference_times=tuple(ObservationInferenceTime(o.observation_id, o.event_id,
                                                                   o.observation_created_at_inference)
                                          for o in observations),
        positive_observation_ids=tuple(o.observation_id for o in positives),
        positive_event_ids=_unique(o.event_id for o in positives),
        transfer_target_event_ids=_unique(t.target.event_id for t in transfers),
        revalidation_target_event_ids=_unique(r.target.event_id for r in revalidations),
        directional_relations=tuple(_directional(r) for r in dependencies + transfers + revalidations),
        evidence_of_independent_remobilization=remobilization,
        durability_evidence_events=tuple(
            DurabilityEvidence(r.relation_kind, r.relation_id, r.source_event_id, r.target_event_id)
            for r in remobilization if r.source_event_id != r.target_event_id),
    )


def _limitations(observations, dropped, dependency_profile, consistency_profile, dependencies, transfers,
                 revalidations) -> tuple:
    found = []

    def add_limitation(code, observation_ids=(), relation_ids=()):
        found.append(Limitation(code, tuple(observation_ids), tuple(relation_ids)))

    if not observations:
        add_limitation(EMPTY_SNAPSHOT)
    else:
        ids = [o.observation_id for o in observations]
        if any(o.event.demonstration_time is None for o in observations):
            add_limitation(EXACT_DEMONSTRATION_TIME_UNAVAILABLE, ids)
        add_limitation(COGNITIVE_CONTEXT_FAMILY_NOT_PERSISTED)
    if consistency_profile.historical_contradictions:
        add_limitation(SEMANTIC_CONTRADICTION_MOTIF_NOT_PERSISTED,
                       [c.observation_id for c in consistency_profile.historical_contradictions])
    competency_only = [o.observation_id for o in observations if o.capability_localization == COMPETENCY_ONLY]
    if competency_only:
        add_limitation(COMPETENCY_ONLY_SCOPE_NOT_LOCALIZABLE, competency_only)
    if dependency_profile.not_established_scopes:
        add_limitation(INDEPENDENCE_NOT_ESTABLISHED_FOR_SOME_OBSERVATIONS,
                       _unique(r.observation_id for r in dependency_profile.not_established_scopes))
    if dropped:
        add_limitation(CROSS_RELEASE_MAPPINGS_NOT_RETAINED, dropped)
    unlocalized = [r.row.id for r in dependencies + transfers + revalidations if not r.definitions]
    if unlocalized:
        add_limitation(UNLOCALIZED_RELATION_SCOPE, relation_ids=unlocalized)
    changed = [o.observation_id for o in observations
               if o.current_integrity_status != VALID or o.current_evaluation_run_interpretation_status != ACTIVE]
    if changed:
        add_limitation(UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT, changed)
    return tuple(sorted(found, key=lambda limitation: limitation.code))


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def _build(db, run) -> LongitudinalDossier:
    state = (run.execution_status, run.interpretation_status)
    if state in UNSTABILIZED_RUN_STATES:
        raise LongitudinalDossierNotStabilized(f"{run.id} est {state[0]} / {state[1]}")
    if state not in READABLE_RUN_STATES:
        raise _corrupt(f"{run.id} : état incohérent {state[0]} / {state[1]}")
    if run.completed_at is None:
        raise _corrupt(f"{run.id} : completed sans completed_at")

    release_refs, release_competencies = _release_definitions(db, run.pedagogical_taxonomy_release_id)
    observations, compatible, dropped = _history(run, _snapshot_rows(db, run), _observation_mappings(db, run),
                                                 release_refs)
    snapshot = {o.observation_id: o for o in observations}
    dependency_rows, dependency_links = _relation_rows(db, run, ObservationDependency, DependencyCapability,
                                                       DependencyCapability.dependency_id)
    transfer_rows, transfer_links = _relation_rows(db, run, ObservationTransfer, TransferCapability,
                                                   TransferCapability.transfer_id)
    revalidation_rows, revalidation_links = _relation_rows(db, run, ObservationRevalidation, RevalidationCapability,
                                                           RevalidationCapability.revalidation_id)
    dependencies = _dependencies(run, dependency_rows, dependency_links, snapshot, compatible, release_refs)
    transfers = _pairs(TRANSFER, run, transfer_rows, transfer_links, snapshot, compatible, release_refs,
                       "source_observation_id", "target_observation_id", "transfer_basis")
    revalidations = _pairs(REVALIDATION, run, revalidation_rows, revalidation_links, snapshot, compatible,
                           release_refs, "source_contradiction_observation_id", "target_supportive_observation_id",
                           "revalidation_basis")
    _check_no_overlap(dependencies, transfers + revalidations)

    episodes = _episodes(observations)
    dependency_profile = _dependency_profile(observations, dependencies, transfers, revalidations)
    consistency_profile = _consistency_profile(observations, dependencies, revalidations)
    return LongitudinalDossier(
        view_schema_version=VIEW_SCHEMA_VERSION,
        run_id=run.id,
        user_id=run.user_id,
        competency_code=run.competency_code,
        pedagogical_taxonomy_release_id=run.pedagogical_taxonomy_release_id,
        input_fingerprint=run.input_fingerprint,
        execution_status=run.execution_status,
        interpretation_status=run.interpretation_status,
        dependency_version=run.dependency_version,
        transfer_version=run.transfer_version,
        revalidation_version=run.revalidation_version,
        relation_schema_version=run.relation_schema_version,
        run_completed_at_technical=run.completed_at,
        active_history=ActiveHistory(observations=observations, episodes=episodes),
        dependency_profile=dependency_profile,
        coverage_profile=_coverage_profile(run, observations, release_refs, release_competencies),
        variety_profile=_variety_profile(observations, episodes, transfers),
        transfer_profile=_transfer_profile(transfers),
        consistency_profile=consistency_profile,
        temporal_validation_profile=_temporal_profile(observations, episodes, dependencies, transfers,
                                                      revalidations),
        limitations=_limitations(observations, dropped, dependency_profile, consistency_profile, dependencies,
                                 transfers, revalidations),
    )


def build_longitudinal_dossier(db, *, run_id: uuid.UUID) -> LongitudinalDossier:
    """Reconstruit le dossier d'UN run T5 : completed / active ou completed /
    superseded (audit historique). running / candidate et failed / obsolete
    => LongitudinalDossierNotStabilized ; état incohérent ou données
    corrompues => InvalidLongitudinalDossier. Lecture seule."""
    _require_uuid(run_id, "run_id")
    with db.no_autoflush:
        run = _load_run(db, run_id)
        if run is None:
            raise LongitudinalDossierNotFound(str(run_id))
        return _build(db, run)


def build_active_longitudinal_dossier(
    db,
    *,
    user_id: str,
    competency_code: str,
) -> LongitudinalDossier | None:
    """Dossier du run active courant du couple, ou None s'il n'y en a pas.
    Plusieurs actives, ou un active non completed => InvalidLongitudinalDossier.
    Le dossier reste celui du run (dossier.run_id), jamais élargi aux
    observations apparues depuis. Lecture seule."""
    _require_text(user_id, "user_id")
    if type(competency_code) is not str or competency_code not in COMPETENCY_CODES:
        raise InvalidLongitudinalViewArgument(f"competency_code : valeur {competency_code!r} hors vocabulaire")
    with db.no_autoflush:
        actives = db.execute(
            select(*LongitudinalAssessmentRun.__table__.c)
            .where(LongitudinalAssessmentRun.user_id == user_id,
                   LongitudinalAssessmentRun.competency_code == competency_code,
                   LongitudinalAssessmentRun.interpretation_status == ACTIVE)
        ).all()
        if len(actives) > 1:
            raise _corrupt(f"{len(actives)} runs actifs observés pour ({user_id}, {competency_code})")
        if not actives:
            return None
        if actives[0].execution_status != COMPLETED:
            raise _corrupt(f"run actif {actives[0].id} est {actives[0].execution_status} ({COMPLETED} attendu)")
        return _build(db, actives[0])
