"""Service interne transactionnel de l'inférence de l'état C1-C12 (T6-B,
niveau 6 : enveloppe transactionnelle, intégrité, provenance, concurrence et
activation atomique).

Seul point d'écriture applicatif des six tables de T6-A
(CompetencyInferenceRun, CompetencyStageClaim, CompetencyInferenceTension,
CompetencyInferenceTensionCapability, CompetencyInferenceBasisRef,
UserCompetencyState) : même philosophie que core/cognitive_capture.py (T2-B),
core/observation_service.py (T3-B), core/taxonomy_service.py (T4-B) et
core/longitudinal_service.py (T5-B).

Frontière : T6-B dit QUELLES données ont le droit d'être interprétées et
garantit que le résultat activé est cohérent avec ces données ; le futur
moteur T6-C dira ce qu'elles signifient pédagogiquement.

    T5-C LongitudinalDossier -> T6-B InferenceContext (start) -> T6-C
    InferenceDecision -> T6-B validation + persistance + activation
    (complete) -> user_competency_states

T6-B ne décide JAMAIS d'un stade (Discovery / Comprehension / Application /
Mastery), ni de la diagnosticité, de la couverture, de la variété d'une
preuve, ni du caractère défendable d'un stade face à une contradiction, ni
de la réussite d'une revalidation, ni de la Mastery. Il vérifie seulement :
structure, identité, provenance, périmètre, lifecycle, concurrence,
cohérence mécanique, chaîne parentale et atomicité. Aucun appel de modèle ;
model_id et prompt_spec_version ne sont que des métadonnées de
reproductibilité. Aucun nombre n'est jamais dérivé d'un stade, d'une
confiance ou d'un décompte de preuves.

Invariants :

- Transactions : jamais de commit() ni de rollback() ; l'appelant possède
  la transaction. Chaque mutation valide (la décision entière AVANT tout
  accès à la base), verrouille, relit sous verrou, vérifie TOUT puis mute
  et flush(). Seule exception, locale et invisible pour l'appelant :
  l'INSERT du candidat est isolé dans un SAVEPOINT pour traduire une
  violation de uq_competency_inference_runs_dedup_key en DuplicateInference
  (toute autre IntegrityError est propagée).

- Lifecycle du run (execution_status / interpretation_status) :
      start     -> running   / candidate
      complete  -> completed / active     (l'ancien active -> superseded)
      fail      -> failed    / obsolete   (l'active et le cache inchangés)
  Tout autre état est terminal : une seconde transition est refusée, jamais
  un no-op. Aucune suppression, aucune cascade : l'historique T6 reste
  auditable. Un candidat running porte des sorties NULL et aucune ligne
  enfant ; sinon il est corrompu (InvalidInferenceState, jamais réparé).

- start : l'appelant ne fournit que le run T5 parent, le trigger et les
  versions / métadonnées T6. user_id, competency_code, predecessor,
  input_fingerprint et inference_dedup_key sont DÉRIVÉS. Le parent doit
  être le dossier COURANT (plus strict que T5-C, qui relit aussi des
  snapshots superseded pour l'audit) : completed / active, unique active du
  couple, release toujours active, et toutes ses observations valid, issues
  d'un run T3 completed / active, d'un CognitiveEvent finalized du même
  user. Le predecessor est l'unique run T6 active du couple (completed) ;
  il peut référencer un ancien T5 superseded : c'est une référence
  historique, jamais une preuve. Le cache doit correspondre exactement à
  l'active (ou être absent sans active), jamais réparé.

- input_fingerprint V1 (calculé, jamais fourni) : SHA-256 (hex minuscule)
  du JSON canonique (sort_keys, séparateurs compacts, ensure_ascii=False,
  UTF-8) de {input_schema_version: 1, user_id, competency_code,
  pedagogical_taxonomy_release_id, longitudinal_input_fingerprint (celui du
  run T5, qui identifie déjà les observations, runs T3, événements et
  périmètres compatibles), dependency_version, transfer_version,
  revalidation_version, relation_schema_version, dependencies, transfers,
  revalidations (identité SÉMANTIQUE complète de chaque relation T5 : ni
  UUID de relation, ni created_at ; chaque collection triée par son JSON
  canonique), predecessor: null | {inference_run_id, current_stage,
  tension_state, unresolved_revision_context, output_fingerprint}}. Même
  dossier logique + même predecessor => même empreinte, même si le run T5
  a été recalculé sous un autre UUID.

- inference_dedup_key V1 : même canonicalisation de {dedup_schema_version:
  1, input_fingerprint, positive_basis_version, confidence_profile_version,
  state_decision_version, validation_version, inference_schema_version,
  evaluator_version, model_id, prompt_spec_version}. Le trigger n'en fait
  PAS partie : aucun reroll (même entrée + mêmes spécifications =>
  DuplicateInference, quel que soit le trigger). Réinterpréter à
  l'identique le dossier de l'active courant (même dossier logique, mêmes
  spécifications) est aussi un reroll : refusé à start. Un audit manuel doit
  changer explicitement une version.

- Reprise : un crash après start laisse un candidat running / candidate
  reprenable ; get_inference_context reconstruit exactement le contexte
  depuis le run persisté et son parent T5 (StaleInferenceInput si l'amont
  a changé, StaleInferencePredecessor si l'active a changé). Jamais de
  second candidat automatique.

- complete : reçoit uniquement run_id et une InferenceDecision (sans
  previous_stage, transition, tension_state ni empreinte : dérivés). Tout
  est vérifié AVANT la première mutation ; l'ancien active est superseded
  et flushé AVANT l'activation (index unique partiel non différable), puis
  le cache est créé (state_generation = 1) ou mis à jour (génération + 1),
  dans la même transaction. state_generation est un compteur TECHNIQUE du
  cache : jamais une progression, un XP ni un nombre de preuves.

- Verrous de complete, ordre global (anti-deadlock) :
      1. lecture NON verrouillée de (user_id, competency_code), immuables ;
      2. pg_advisory_xact_lock(clé(["oryx-t6", user_id, competency_code]))
         (espace de noms distinct de T5 « oryx-t5 ») : sérialise toutes les
         activations T6 du couple ;
      3. candidat CompetencyInferenceRun FOR NO KEY UPDATE ;
      4. run T5 parent FOR SHARE (conflit avec le FOR NO KEY UPDATE de
         T5-B qui le supersederait : une activation T5 concurrente attend,
         ou a déjà commité et le parent est vu superseded) ;
      5. release du parent FOR SHARE (conflit avec son retrait par T4-B) ;
         puis CognitiveEvent FOR SHARE, puis runs T3 FOR SHARE, puis
         PedagogicalObservation FOR SHARE (chaque famille triée par id).
         Événement AVANT run T3 : même ordre que T3-B (run candidat ->
         événement -> run active), aucun cycle avec une réévaluation T3 ;
         memberships / définitions : immuables, lus sans verrou ;
      6. run T6 active courant FOR NO KEY UPDATE ;
      7. UserCompetencyState FOR UPDATE (active_inference_run_id est une
         colonne UNIQUE : sa mise à jour est une mise à jour de clé).
  T6-B ne prend ensuite que des FOR KEY SHARE implicites (FK), compatibles
  avec tous ces modes. Aucun service amont ne verrouille une ligne T6 :
  aucune famille de verrous n'est attendue en détenant l'autre, donc aucun
  cycle avec T2-B / T3-B / T4-B / T5-B. fail : candidat seul (FOR NO KEY
  UPDATE). start : aucun verrou de ligne (predecessor et cache lus par UNE
  seule requête, donc un seul snapshot ; complete revalide tout).
  Hypothèse : READ COMMITTED (défaut PostgreSQL et SessionLocal).

- Décision (T6-C) : exactement quatre claims (discovery, comprehension,
  application, mastery ; jamais non_etabli) ; not_established => none,
  established => direct ou implied_by_higher_claim ; implied exige une claim
  supérieure established, jamais sur mastery ; monotonicité (une claim
  established implique toutes les inférieures established) ; direct
  established => au moins une ref positive_basis vers une observation
  supportive du snapshot ; une même observation n'est positive_basis que
  d'UNE claim (aucune multiplication de preuve) ; confidence_profile objet
  JSON pour established, None sinon (aucun score calculé ni accepté) ;
  mastery_assessment seulement sur mastery. Refs : rattachement par
  ref_role identique au schéma, sources appartenant au dossier T5 consommé
  (observation du snapshot, ou relation du run T5 parent) ; une relation
  T5 reste provenance, jamais preuve positive. Tensions : fragilized_stage
  established maintenant ou dans le predecessor, périmètre = memberships de
  la release et de la compétence, couvert par au moins une source de ref
  tension (jamais inventé depuis une source competency_only).

- Transition dérivée (ordre interne non persisté non_etabli < discovery <
  comprehension < application < mastery) : première inférence => previous,
  transition et cause NULL ; sinon maintained / upgraded / revised_down,
  cause obligatoire. upgraded et revised_down (hors non_etabli) exigent la
  claim du stade d'arrivée established et au moins une ref transition ;
  revised_down vers non_etabli seulement sans aucune claim established et
  par evidence_integrity_change ou pedagogical_reinterpretation. Un
  current_stage au-dessus de la plus haute claim established n'est
  acceptable que comme maintien du stade précédent sous tension ouverte
  avec unresolved_revision_context non vide. new_user_evidence exige au
  moins une observation absente du snapshot T5 du predecessor ;
  pedagogical_reinterpretation exige une spécification différente de
  celle du predecessor. Anti-oscillation (garde STRUCTURELLE seulement) :
  remonter après une révision non résolue par new_user_evidence exige
  qu'au moins une ref positive_basis ou transition cite une observation
  nouvelle (directement ou comme extrémité d'une relation) : les anciennes
  preuves seules ne provoquent jamais une remontée ; T6-C reste seul juge
  de la résolution du motif.

- Lecture validée (get_validated_user_competency_state) : cache -> active
  T6 -> parent T5 active -> observations valid -> runs T3 completed /
  active -> événements finalized. Chaîne devenue non courante =>
  StaleInferenceChain ; incohérence interne (cache vers un autre run,
  stade / tension divergents, active sans cache...) => InvalidInferenceState.
  Aucune mutation, aucune réparation : le cache n'est qu'un read-model. Le
  statut de la release n'y est PAS revérifié : le retrait d'une release
  n'invalide aucune preuve de la chaîne ; il interdit seulement de produire
  un NOUVEL état depuis ce dossier (start / complete l'exigent active, comme
  l'activation T5-B).

- Anti-N+1 : SELECT groupés (parent, chaîne observations + runs T3 +
  événements en une jointure, chaque famille de relations, memberships,
  predecessor / cache) ; aucune requête par ref ni par observation.
"""
import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.longitudinal_view import (
    UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT,
    LongitudinalDossier,
    LongitudinalViewError,
    build_longitudinal_dossier,
)
from core.models import (
    CapabilityTaxonomyMembership,
    CognitiveEvent,
    CompetencyInferenceBasisRef,
    CompetencyInferenceRun,
    CompetencyInferenceTension,
    CompetencyInferenceTensionCapability,
    CompetencyStageClaim,
    CoreCapabilityDefinition,
    LongitudinalAssessmentInput,
    LongitudinalAssessmentRun,
    ObservationDependency,
    ObservationEvaluationRun,
    ObservationRevalidation,
    ObservationTransfer,
    PedagogicalObservation,
    PedagogicalTaxonomyRelease,
    User,
    UserCompetencyState,
)

# Statuts de run (vocabulaires T3 / T5 / T6 identiques).
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANDIDATE = "candidate"
ACTIVE = "active"
SUPERSEDED = "superseded"
OBSOLETE = "obsolete"

# Chaîne amont (vocabulaires T2 / T3 / T4, lus seulement).
FINALIZED = "finalized"
VALID = "valid"
SUPPORTIVE = "supportive"
LOCALIZED = "localized"
RELEASE_ACTIVE = "active"

# Stades : ordre conceptuel interne, jamais persisté ni converti en nombre.
NON_ETABLI = "non_etabli"
CLAIM_STAGES = ("discovery", "comprehension", "application", "mastery")
STAGE_SEQUENCE = (NON_ETABLI, *CLAIM_STAGES)
CURRENT_STAGES = frozenset(STAGE_SEQUENCE)
MASTERY = "mastery"

# CompetencyStageClaim.
ESTABLISHED = "established"
NOT_ESTABLISHED = "not_established"
POSITIVE_BASIS_STATUSES = frozenset({ESTABLISHED, NOT_ESTABLISHED})
DIRECT = "direct"
IMPLIED_BY_HIGHER_CLAIM = "implied_by_higher_claim"
BASIS_MODE_NONE = "none"
BASIS_MODES = frozenset({DIRECT, IMPLIED_BY_HIGHER_CLAIM, BASIS_MODE_NONE})

# CompetencyInferenceRun.
MAINTAINED = "maintained"
UPGRADED = "upgraded"
REVISED_DOWN = "revised_down"
TRANSITIONS = frozenset({MAINTAINED, UPGRADED, REVISED_DOWN})
NEW_USER_EVIDENCE = "new_user_evidence"
EVIDENCE_INTEGRITY_CHANGE = "evidence_integrity_change"
PEDAGOGICAL_REINTERPRETATION = "pedagogical_reinterpretation"
TRANSITION_CAUSES = frozenset({NEW_USER_EVIDENCE, EVIDENCE_INTEGRITY_CHANGE, PEDAGOGICAL_REINTERPRETATION})
TENSION_NONE = "none"
TENSION_OPEN = "open"
TENSION_STATES = frozenset({TENSION_NONE, TENSION_OPEN})

# CompetencyInferenceTension.
WHOLE_COMPETENCY = "whole_competency"
COMPETENCY_ONLY = "competency_only"
TENSION_SCOPE_MODES = frozenset({WHOLE_COMPETENCY, LOCALIZED, COMPETENCY_ONLY})
REVISION_STATUSES = frozenset({"unresolved", "revalidation_needed"})

# CompetencyInferenceBasisRef.
POSITIVE_BASIS = "positive_basis"
CONFIDENCE = "confidence"
TENSION = "tension"
TRANSITION = "transition"
VALIDATION = "validation"
MASTERY_REF = "mastery"
REF_ROLES = frozenset({POSITIVE_BASIS, CONFIDENCE, TENSION, TRANSITION, VALIDATION, MASTERY_REF})
CONFIDENCE_DIMENSIONS = frozenset({"diagnosticity", "coverage", "independence", "consistency",
                                   "temporal_validation"})
SOURCE_OBSERVATION = "observation"
SOURCE_DEPENDENCY = "dependency"
SOURCE_TRANSFER = "transfer"
SOURCE_REVALIDATION = "revalidation"
SOURCE_KINDS = frozenset({SOURCE_OBSERVATION, SOURCE_DEPENDENCY, SOURCE_TRANSFER, SOURCE_REVALIDATION})
# Rattachement exigé par ref_role : (claim_stage, tension_key, confidence_dimension),
# True = obligatoire, False = interdit. Miroir des CHECK *_ref de 0009.
REF_ATTACHMENTS = {
    POSITIVE_BASIS: (True, False, False),
    CONFIDENCE: (True, False, True),
    MASTERY_REF: (True, False, False),
    TENSION: (False, True, False),
    TRANSITION: (False, False, False),
    VALIDATION: (False, False, False),
}
# Refs pouvant porter la remontée après une révision non résolue.
REBOUND_ROLES = frozenset({POSITIVE_BASIS, TRANSITION})

COMPETENCY_CODES = frozenset(f"C{i}" for i in range(1, 13))
VERSION_FIELDS = ("positive_basis_version", "confidence_profile_version", "state_decision_version",
                  "validation_version", "inference_schema_version", "evaluator_version")
SPECIFICATION_FIELDS = (*VERSION_FIELDS, "model_id", "prompt_spec_version")

# Versions des formats canoniques possédés par T6-B.
INPUT_SCHEMA_VERSION = 1
DEDUP_SCHEMA_VERSION = 1
OUTPUT_SCHEMA_VERSION = 1
# Espace de noms de la clé consultative d'activation (distinct de « oryx-t5 »).
ACTIVATION_LOCK_NAMESPACE = "oryx-t6"

DEDUP_CONSTRAINT = "uq_competency_inference_runs_dedup_key"


class InferenceServiceError(Exception):
    """Erreur métier du service d'inférence T6-B."""


class InvalidInferenceArgument(InferenceServiceError):
    """Argument structurellement invalide (type, chaîne vide, vocabulaire)."""


class UserNotFound(InferenceServiceError):
    """Aucun utilisateur pour cet identifiant."""


class InferenceRunNotFound(InferenceServiceError):
    """Aucun CompetencyInferenceRun pour cet identifiant."""


class LongitudinalParentNotUsable(InferenceServiceError):
    """Run T5 inconnu, ou qui n'est pas le dossier COURANT du couple (état,
    unicité, release, chaîne amont) : aucun candidat n'est créé."""


class InvalidInferenceState(InferenceServiceError):
    """Corruption interne ou état incompatible, lu sous verrou : run qui
    n'est plus running / candidate, lignes enfants ou sorties sur un
    candidat, cache absent / divergent, plusieurs actives... Jamais réparé."""


class DuplicateInference(InferenceServiceError):
    """Même identité logique (inference_dedup_key) déjà présente, ou
    réinterprétation à l'identique de l'active courant : aucun reroll."""


class StaleInferenceInput(InferenceServiceError):
    """L'entrée du candidat n'est plus courante (parent T5 superseded,
    observation invalidée, run T3 superseded, release retirée, empreinte
    différente) : le candidat ne peut pas devenir active."""


class StaleInferencePredecessor(InferenceServiceError):
    """L'active courant n'est plus le predecessor capturé au start : un
    candidat ancien n'écrase jamais un état plus récent."""


class InvalidInferenceDecision(InferenceServiceError):
    """Décision incohérente au niveau du run : stade, cause, transition,
    contexte de révision, besoins de validation, garde anti-oscillation."""


class InvalidStageClaim(InferenceServiceError):
    """Claims incohérentes : nombre, stade, statut / mode, implication,
    monotonicité, profil de confiance, mastery_assessment."""


class InvalidInferenceTension(InferenceServiceError):
    """Tension incohérente : clé, stade fragilisé, périmètre, memberships,
    provenance absente ou ne couvrant pas le périmètre."""


class InvalidInferenceBasisReference(InferenceServiceError):
    """Ref incohérente : rattachement, doublon, source hors du dossier T5
    consommé, positive_basis non supportive ou multipliée."""


class StaleInferenceChain(InferenceServiceError):
    """Historique cohérent, mais sa chaîne amont n'est plus courante (T5
    superseded, observation invalidée, run T3 superseded, événement non
    finalized) : l'état ne doit pas être utilisé comme état courant."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class PredecessorSnapshot:
    """Référence HISTORIQUE à l'inférence active remplacée : jamais une
    preuve. Son run T5 peut être superseded depuis (c'est normal)."""
    inference_run_id: uuid.UUID
    longitudinal_assessment_run_id: uuid.UUID
    current_stage: str
    previous_stage: str | None
    transition: str | None
    transition_cause: str | None
    tension_state: str
    unresolved_revision_context: Mapping | None
    established_claim_stages: tuple
    input_fingerprint: str
    output_fingerprint: str


@dataclass(frozen=True, kw_only=True)
class InferenceContext:
    """Unique entrée autorisée du futur T6-C : le dossier T5 courant (vue
    T5-C), le predecessor (ou None) et l'identité figée du candidat."""
    run_id: uuid.UUID
    user_id: str
    competency_code: str
    trigger: str
    longitudinal_assessment_run_id: uuid.UUID
    pedagogical_taxonomy_release_id: uuid.UUID
    longitudinal_dossier: LongitudinalDossier
    predecessor: PredecessorSnapshot | None
    input_fingerprint: str
    inference_dedup_key: str
    positive_basis_version: str
    confidence_profile_version: str
    state_decision_version: str
    validation_version: str
    inference_schema_version: str
    evaluator_version: str
    model_id: str | None
    prompt_spec_version: str | None


@dataclass(frozen=True, kw_only=True)
class StageClaimDecision:
    """Prétention positive sur UN stade, décidée par T6-C."""
    stage: str
    positive_basis_status: str
    basis_mode: str
    basis_summary: str | None = None
    scope_summary: str | None = None
    confidence_profile: Mapping | None = None
    mastery_assessment: Mapping | None = None


@dataclass(frozen=True, kw_only=True)
class TensionDecision:
    """Tension décidée par T6-C. tension_key est LOCAL à la décision (sert à
    rattacher des refs avant la génération des UUID) ; jamais persisté."""
    tension_key: str
    fragilized_stage: str
    scope_mode: str
    summary: str
    revision_status: str
    capability_membership_ids: tuple = ()


@dataclass(frozen=True, kw_only=True)
class BasisRefDecision:
    """Provenance d'un élément de la décision. Cible : claim_stage, OU
    tension_key, OU aucune (run-level) ; jamais d'identifiant de ligne T6."""
    ref_role: str
    source_kind: str
    source_id: uuid.UUID
    claim_stage: str | None = None
    tension_key: str | None = None
    confidence_dimension: str | None = None


@dataclass(frozen=True, kw_only=True)
class InferenceDecision:
    """Décision déjà calculée par T6-C. previous_stage, transition,
    tension_state et les empreintes n'en font volontairement pas partie :
    T6-B les dérive."""
    current_stage: str
    transition_cause: str | None
    unresolved_revision_context: Mapping | None
    validation_needs: tuple
    state_decision_summary: str
    claims: tuple
    tensions: tuple
    basis_refs: tuple


@dataclass(frozen=True, kw_only=True)
class ValidatedCompetencyState:
    """État courant dont toute la chaîne a été revérifiée à la lecture.
    state_generation : compteur technique du cache, jamais un signal
    pédagogique."""
    user_id: str
    competency_code: str
    current_stage: str
    tension_state: str
    active_inference_run_id: uuid.UUID
    longitudinal_assessment_run_id: uuid.UUID
    state_generation: int


class _Claim(NamedTuple):
    stage: str
    status: str
    mode: str
    basis_summary: str | None
    scope_summary: str | None
    confidence_profile: dict | None
    mastery_assessment: dict | None


class _Tension(NamedTuple):
    key: str
    fragilized_stage: str
    scope_mode: str
    summary: str
    revision_status: str
    membership_ids: tuple


class _Ref(NamedTuple):
    role: str
    claim_stage: str | None
    tension_key: str | None
    dimension: str | None
    source_kind: str
    source_id: uuid.UUID


class _Decision(NamedTuple):
    current_stage: str
    cause: str | None
    context: dict | None
    needs: list
    summary: str
    claims: dict
    tensions: tuple
    refs: tuple


class _Relations(NamedTuple):
    """Relations du run T5 : payload = identité sémantique complète (pour
    input_fingerprint) ; identities = {(kind, id): identité sans basis (pour
    output_fingerprint)} ; endpoints = {(kind, id): observations}."""
    payload: dict
    identities: dict
    endpoints: dict


class _Inputs(NamedTuple):
    parent: object
    dossier: LongitudinalDossier
    relations: _Relations
    predecessor: object
    snapshot: PredecessorSnapshot | None
    input_fingerprint: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Validation structurelle
# --------------------------------------------------------------------------

def _require_text(value, name: str, error=InvalidInferenceArgument) -> str:
    """str non vide après strip, sans NUL. Conservée telle quelle."""
    if type(value) is not str or not value.strip():
        raise error(f"{name} doit être une chaîne non vide")
    if "\x00" in value:
        raise error(f"{name} : caractère NUL refusé")
    return value


def _optional_text(value, name: str, error=InvalidInferenceArgument) -> str | None:
    return None if value is None else _require_text(value, name, error)


def _require_uuid(value, name: str, error=InvalidInferenceArgument) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise error(f"{name} doit être un uuid.UUID")
    return value


def _require_choice(value, name: str, allowed, error=InvalidInferenceArgument) -> str:
    """Type str exact : une Enum str n'est jamais convertie."""
    if type(value) is not str or value not in allowed:
        raise error(f"{name} : valeur {value!r} hors vocabulaire {sorted(allowed)}")
    return value


def _require_sequence(value, name: str, error) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise error(f"{name} doit être une liste ou un tuple")
    return tuple(value)


def _json_copy(value, path: str, error, active: frozenset = frozenset()):
    """Copie profonde d'une valeur JSON stricte (objets : Mapping à clés str ;
    tableaux : list / tuple ; scalaires de type exact ; flottants finis ;
    aucun NUL, aucun cycle), sinon `error`. Rien n'est converti
    silencieusement (Enum, Decimal, datetime, UUID, set, bytes... refusés)."""
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise error(f"{path} : nombre non fini refusé")
        return value
    if type(value) is str:
        if "\x00" in value:
            raise error(f"{path} : caractère NUL refusé")
        return value
    if isinstance(value, (Mapping, list, tuple)):
        if id(value) in active:
            raise error(f"{path} : référence circulaire")
        active = active | {id(value)}
        if not isinstance(value, Mapping):
            return [_json_copy(item, f"{path}[{i}]", error, active) for i, item in enumerate(value)]
        copy = {}
        for key, item in value.items():
            if type(key) is not str or "\x00" in key:
                raise error(f"{path} : clé {key!r} refusée (str sans NUL attendue)")
            copy[key] = _json_copy(item, f"{path}.{key}", error, active)
        return copy
    raise error(f"{path} : type {type(value).__name__} non JSON-compatible")


def _json_object(value, name: str, error, *, non_empty: bool = False) -> dict:
    if not isinstance(value, Mapping):
        raise error(f"{name} doit être un objet JSON (mapping)")
    if non_empty and not value:
        raise error(f"{name} : objet non vide requis")
    return _json_copy(value, name, error)


def _freeze(value):
    """Copie immuable d'une valeur JSON (mapping -> mappingproxy, tableau ->
    tuple)."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _is_dedup_violation(exc: IntegrityError) -> bool:
    """Vrai seulement pour une violation de uq_competency_inference_runs_dedup_key."""
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == DEDUP_CONSTRAINT


def _above(first: str, second: str) -> bool:
    """first strictement au-dessus de second dans l'ordre conceptuel (tuple
    interne, jamais exposé ni persisté comme nombre)."""
    return STAGE_SEQUENCE.index(first) > STAGE_SEQUENCE.index(second)


def _highest_established(claims: dict) -> str:
    highest = NON_ETABLI
    for stage in CLAIM_STAGES:
        if claims[stage].status == ESTABLISHED:
            highest = stage
    return highest


def _validated_claims(values) -> dict:
    items = _require_sequence(values, "claims", InvalidStageClaim)
    claims = {}
    for item in items:
        if type(item) is not StageClaimDecision:
            raise InvalidStageClaim("claims : StageClaimDecision attendu")
        if item.stage == NON_ETABLI:
            raise InvalidStageClaim(f"{NON_ETABLI} n'est jamais une stage claim")
        stage = _require_choice(item.stage, "claim.stage", CLAIM_STAGES, InvalidStageClaim)
        if stage in claims:
            raise InvalidStageClaim(f"claim {stage} en double")
        status = _require_choice(item.positive_basis_status, f"{stage}.positive_basis_status",
                                 POSITIVE_BASIS_STATUSES, InvalidStageClaim)
        mode = _require_choice(item.basis_mode, f"{stage}.basis_mode", BASIS_MODES, InvalidStageClaim)
        if status == NOT_ESTABLISHED and mode != BASIS_MODE_NONE:
            raise InvalidStageClaim(f"{stage} : {NOT_ESTABLISHED} exige basis_mode {BASIS_MODE_NONE}")
        if status == ESTABLISHED and mode == BASIS_MODE_NONE:
            raise InvalidStageClaim(f"{stage} : {ESTABLISHED} exige {DIRECT} ou {IMPLIED_BY_HIGHER_CLAIM}")
        if status == ESTABLISHED:
            profile = _json_object(item.confidence_profile, f"{stage}.confidence_profile", InvalidStageClaim)
        elif item.confidence_profile is not None:
            raise InvalidStageClaim(f"{stage} : aucun confidence_profile sans base positive établie")
        else:
            profile = None
        assessment = None
        if item.mastery_assessment is not None:
            if stage != MASTERY:
                raise InvalidStageClaim(f"{stage} : mastery_assessment réservé à la claim {MASTERY}")
            assessment = _json_object(item.mastery_assessment, "mastery.mastery_assessment", InvalidStageClaim)
        claims[stage] = _Claim(stage, status, mode,
                               _optional_text(item.basis_summary, f"{stage}.basis_summary", InvalidStageClaim),
                               _optional_text(item.scope_summary, f"{stage}.scope_summary", InvalidStageClaim),
                               profile, assessment)
    if len(claims) != len(CLAIM_STAGES):
        raise InvalidStageClaim(f"exactement quatre claims requises ({', '.join(CLAIM_STAGES)}),"
                                f" {len(items)} reçue(s)")
    for index, stage in enumerate(CLAIM_STAGES):
        claim = claims[stage]
        higher = CLAIM_STAGES[index + 1:]
        if claim.mode == IMPLIED_BY_HIGHER_CLAIM:
            if stage == MASTERY:
                raise InvalidStageClaim(f"{MASTERY} ne peut jamais être {IMPLIED_BY_HIGHER_CLAIM}")
            if not any(claims[h].status == ESTABLISHED for h in higher):
                raise InvalidStageClaim(f"{stage} {IMPLIED_BY_HIGHER_CLAIM} sans claim supérieure established")
        if claim.status == ESTABLISHED:
            lower = [s for s in CLAIM_STAGES[:index] if claims[s].status != ESTABLISHED]
            if lower:
                raise InvalidStageClaim(f"{stage} established mais {', '.join(lower)} not_established"
                                        " (base positive non monotone)")
    return claims


def _validated_tensions(values) -> tuple:
    items = _require_sequence(values, "tensions", InvalidInferenceTension)
    tensions, keys = [], set()
    for item in items:
        if type(item) is not TensionDecision:
            raise InvalidInferenceTension("tensions : TensionDecision attendu")
        key = _require_text(item.tension_key, "tension_key", InvalidInferenceTension)
        if key in keys:
            raise InvalidInferenceTension(f"tension_key {key!r} en double")
        keys.add(key)
        stage = _require_choice(item.fragilized_stage, f"{key}.fragilized_stage", CLAIM_STAGES,
                                InvalidInferenceTension)
        mode = _require_choice(item.scope_mode, f"{key}.scope_mode", TENSION_SCOPE_MODES, InvalidInferenceTension)
        memberships = _require_sequence(item.capability_membership_ids, f"{key}.capability_membership_ids",
                                        InvalidInferenceTension)
        for membership_id in memberships:
            _require_uuid(membership_id, f"{key}.capability_membership_ids", InvalidInferenceTension)
        if len(set(memberships)) != len(memberships):
            raise InvalidInferenceTension(f"{key} : capability_membership_ids en double")
        if mode == LOCALIZED and not memberships:
            raise InvalidInferenceTension(f"{key} : {LOCALIZED} exige au moins un capability_membership_id")
        if mode != LOCALIZED and memberships:
            raise InvalidInferenceTension(f"{key} : {mode} n'accepte aucun capability_membership_id")
        tensions.append(_Tension(
            key, stage, mode, _require_text(item.summary, f"{key}.summary", InvalidInferenceTension),
            _require_choice(item.revision_status, f"{key}.revision_status", REVISION_STATUSES,
                            InvalidInferenceTension),
            memberships))
    return tuple(tensions)


def _validated_refs(values, claims: dict, tensions: tuple) -> tuple:
    items = _require_sequence(values, "basis_refs", InvalidInferenceBasisReference)
    error = InvalidInferenceBasisReference
    keys = {t.key for t in tensions}
    refs, seen, positive_owner = [], set(), {}
    for item in items:
        if type(item) is not BasisRefDecision:
            raise error("basis_refs : BasisRefDecision attendu")
        role = _require_choice(item.ref_role, "ref_role", REF_ROLES, error)
        kind = _require_choice(item.source_kind, f"{role}.source_kind", SOURCE_KINDS, error)
        source_id = _require_uuid(item.source_id, f"{role}.source_id", error)
        stage = None if item.claim_stage is None else _require_choice(
            item.claim_stage, f"{role}.claim_stage", CLAIM_STAGES, error)
        key = None if item.tension_key is None else _require_text(item.tension_key, f"{role}.tension_key", error)
        dimension = None if item.confidence_dimension is None else _require_choice(
            item.confidence_dimension, f"{role}.confidence_dimension", CONFIDENCE_DIMENSIONS, error)
        needs_stage, needs_key, needs_dimension = REF_ATTACHMENTS[role]
        for present, required, field in ((stage, needs_stage, "claim_stage"), (key, needs_key, "tension_key"),
                                         (dimension, needs_dimension, "confidence_dimension")):
            if required and present is None:
                raise error(f"ref {role} : {field} obligatoire")
            if not required and present is not None:
                raise error(f"ref {role} : {field} interdit")
        if key is not None and key not in keys:
            raise error(f"ref {role} : tension_key {key!r} inconnue dans la décision")
        if role == POSITIVE_BASIS:
            if kind != SOURCE_OBSERVATION:
                raise error(f"positive_basis : source {kind} refusée (seule une observation soutient une claim ;"
                            " une relation T5 n'est jamais une preuve)")
            claim = claims[stage]
            if claim.status != ESTABLISHED or claim.mode != DIRECT:
                raise error(f"positive_basis vers {stage} {claim.status} / {claim.mode}"
                            f" ({ESTABLISHED} / {DIRECT} requis)")
            owner = positive_owner.setdefault(source_id, stage)
            if owner != stage:
                raise error(f"observation {source_id} positive_basis de {owner} et de {stage} :"
                            f" une claim inférieure est {IMPLIED_BY_HIGHER_CLAIM}, jamais re-prouvée")
        if role == CONFIDENCE and claims[stage].status != ESTABLISHED:
            raise error(f"ref confidence vers {stage} {claims[stage].status} ({ESTABLISHED} requis)")
        if role == MASTERY_REF and stage != MASTERY:
            raise error(f"ref mastery rattachée à {stage} ({MASTERY} requis)")
        ref = _Ref(role, stage, key, dimension, kind, source_id)
        if ref in seen:
            raise error(f"ref {role} {kind} {source_id} en double (même cible)")
        seen.add(ref)
        refs.append(ref)
    for stage, claim in claims.items():
        if claim.status == ESTABLISHED and claim.mode == DIRECT and not any(
                r.role == POSITIVE_BASIS and r.claim_stage == stage for r in refs):
            raise InvalidStageClaim(f"{stage} {ESTABLISHED} / {DIRECT} sans ref positive_basis")
    for tension in tensions:
        if not any(r.role == TENSION and r.tension_key == tension.key for r in refs):
            raise InvalidInferenceTension(f"tension {tension.key!r} sans ref tension (provenance absente)")
    return tuple(refs)


def _validated_decision(decision) -> _Decision:
    """Validation COMPLÈTE de tout ce qui ne dépend pas de la base, avant
    tout accès à la base. Les payloads JSON sont copiés en profondeur."""
    if type(decision) is not InferenceDecision:
        raise InvalidInferenceArgument("decision doit être une InferenceDecision")
    error = InvalidInferenceDecision
    current = _require_choice(decision.current_stage, "current_stage", CURRENT_STAGES, error)
    cause = None if decision.transition_cause is None else _require_choice(
        decision.transition_cause, "transition_cause", TRANSITION_CAUSES, error)
    context = None if decision.unresolved_revision_context is None else _json_object(
        decision.unresolved_revision_context, "unresolved_revision_context", error, non_empty=True)
    needs = [_json_object(item, f"validation_needs[{i}]", error)
             for i, item in enumerate(_require_sequence(decision.validation_needs, "validation_needs", error))]
    summary = _require_text(decision.state_decision_summary, "state_decision_summary", error)
    claims = _validated_claims(decision.claims)
    tensions = _validated_tensions(decision.tensions)
    refs = _validated_refs(decision.basis_refs, claims, tensions)
    if current == NON_ETABLI and _highest_established(claims) != NON_ETABLI:
        raise error(f"current_stage {NON_ETABLI} = aucune prétention positive établie :"
                    f" {_highest_established(claims)} est established")
    if needs and not any(r.role == VALIDATION for r in refs):
        raise error("validation_needs non vide sans ref validation")
    return _Decision(current, cause, context, needs, summary, claims, tensions, refs)


# --------------------------------------------------------------------------
# Formats canoniques (V1)
# --------------------------------------------------------------------------

def _canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_sha256(payload) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _str(value) -> str | None:
    return None if value is None else str(value)


def _canonical_list(items) -> list:
    return sorted(items, key=_canonical_json)


def _specification(source) -> dict:
    """Versions T6 + model_id / prompt_spec_version d'un run ou d'un mapping."""
    get = source.get if isinstance(source, Mapping) else lambda name: getattr(source, name)
    return {name: get(name) for name in SPECIFICATION_FIELDS}


def _dossier_payload(parent, relations: _Relations) -> dict:
    return {
        "user_id": parent.user_id,
        "competency_code": parent.competency_code,
        "pedagogical_taxonomy_release_id": str(parent.pedagogical_taxonomy_release_id),
        "longitudinal_input_fingerprint": parent.input_fingerprint,
        "dependency_version": parent.dependency_version,
        "transfer_version": parent.transfer_version,
        "revalidation_version": parent.revalidation_version,
        "relation_schema_version": parent.relation_schema_version,
        **relations.payload,
    }


def _predecessor_payload(snapshot: PredecessorSnapshot | None, raw_context) -> dict | None:
    if snapshot is None:
        return None
    return {
        "inference_run_id": str(snapshot.inference_run_id),
        "current_stage": snapshot.current_stage,
        "tension_state": snapshot.tension_state,
        "unresolved_revision_context": raw_context,
        "output_fingerprint": snapshot.output_fingerprint,
    }


def _input_fingerprint(dossier_payload: dict, predecessor_payload: dict | None) -> str:
    return _canonical_sha256({"input_schema_version": INPUT_SCHEMA_VERSION, **dossier_payload,
                              "predecessor": predecessor_payload})


def _inference_dedup_key(input_fingerprint: str, specification: dict) -> str:
    return _canonical_sha256({"dedup_schema_version": DEDUP_SCHEMA_VERSION, "input_fingerprint": input_fingerprint,
                              **specification})


def _activation_lock_key(user_id: str, competency_code: str) -> int:
    """Clé bigint déterministe entre processus (jamais hash() Python)."""
    encoded = json.dumps([ACTIVATION_LOCK_NAMESPACE, user_id, competency_code], separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big", signed=True)


def _ref_source_identity(ref: _Ref, relations: _Relations) -> dict:
    if ref.source_kind == SOURCE_OBSERVATION:
        return {"kind": SOURCE_OBSERVATION, "observation_id": str(ref.source_id)}
    return {"kind": ref.source_kind, **relations.identities[(ref.source_kind, ref.source_id)]}


def _ref_payload(ref: _Ref, relations: _Relations) -> dict:
    return {"ref_role": ref.role, "confidence_dimension": ref.dimension,
            "source": _ref_source_identity(ref, relations)}


def _output_fingerprint(*, input_fingerprint: str, specification: dict, decision: _Decision, previous_stage,
                        transition, tension_state, relations: _Relations, membership_definitions: dict) -> str:
    """Empreinte de la DÉCISION SÉMANTIQUE : ni UUID de ligne T6, ni
    horodatage, ni ordre d'insertion, ni tension_key ; relations T5 par leur
    identité sémantique ; périmètres par capability_definition_id."""
    claims = []
    for stage in CLAIM_STAGES:
        claim = decision.claims[stage]
        claims.append({
            "stage": stage,
            "positive_basis_status": claim.status,
            "basis_mode": claim.mode,
            "basis_summary": claim.basis_summary,
            "scope_summary": claim.scope_summary,
            "confidence_profile": claim.confidence_profile,
            "mastery_assessment": claim.mastery_assessment,
            "refs": _canonical_list(_ref_payload(r, relations) for r in decision.refs if r.claim_stage == stage),
        })
    tensions = _canonical_list({
        "fragilized_stage": t.fragilized_stage,
        "scope_mode": t.scope_mode,
        "summary": t.summary,
        "revision_status": t.revision_status,
        "capability_definition_ids": sorted(str(membership_definitions[m]) for m in t.membership_ids),
        "refs": _canonical_list(_ref_payload(r, relations) for r in decision.refs if r.tension_key == t.key),
    } for t in decision.tensions)
    return _canonical_sha256({
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "input_fingerprint": input_fingerprint,
        "specification": specification,
        "current_stage": decision.current_stage,
        "previous_stage": previous_stage,
        "transition": transition,
        "transition_cause": decision.cause,
        "tension_state": tension_state,
        "unresolved_revision_context": decision.context,
        "validation_needs": decision.needs,
        "state_decision_summary": decision.summary,
        "claims": claims,
        "tensions": tensions,
        "run_refs": _canonical_list(_ref_payload(r, relations) for r in decision.refs
                                    if r.claim_stage is None and r.tension_key is None),
    })


# --------------------------------------------------------------------------
# Lecture de la chaîne amont
# --------------------------------------------------------------------------

def _parent_row(db, parent_id: uuid.UUID, *, lock: bool):
    """Run T5 lu colonne par colonne (jamais un objet ORM périmé) ; FOR SHARE
    si lock."""
    statement = select(*LongitudinalAssessmentRun.__table__.c).where(LongitudinalAssessmentRun.id == parent_id)
    if lock:
        statement = statement.with_for_update(read=True)
    return db.execute(statement).one_or_none()


def _parent_problems(db, parent, *, lock: bool, check_release: bool) -> list:
    """Raisons pour lesquelles le run T5 n'est pas le dossier COURANT."""
    problems = []
    if (parent.execution_status, parent.interpretation_status) != (COMPLETED, ACTIVE):
        problems.append(f"run T5 {parent.id} est {parent.execution_status} / {parent.interpretation_status}"
                        f" ({COMPLETED} / {ACTIVE} requis)")
    actives = db.execute(
        select(LongitudinalAssessmentRun.id)
        .where(LongitudinalAssessmentRun.user_id == parent.user_id,
               LongitudinalAssessmentRun.competency_code == parent.competency_code,
               LongitudinalAssessmentRun.interpretation_status == ACTIVE)
    ).scalars().all()
    if actives and actives != [parent.id]:
        problems.append(f"run T5 {parent.id} n'est pas l'unique active du couple ({len(actives)} active(s))")
    if check_release:
        statement = select(PedagogicalTaxonomyRelease.status).where(
            PedagogicalTaxonomyRelease.id == parent.pedagogical_taxonomy_release_id)
        if lock:
            statement = statement.with_for_update(read=True)
        status = db.execute(statement).scalar_one_or_none()
        if status != RELEASE_ACTIVE:
            problems.append(f"release {parent.pedagogical_taxonomy_release_id} {status or 'inconnue'}"
                            f" ({RELEASE_ACTIVE} requise)")
    return problems


def _share_chain(db, parent_id: uuid.UUID) -> None:
    """FOR SHARE sur la chaîne du snapshot : événements, PUIS runs T3, PUIS
    observations (chaque famille triée par id ; voir l'ordre global)."""
    inputs = select(LongitudinalAssessmentInput.observation_id).where(LongitudinalAssessmentInput.run_id == parent_id)
    t3_runs = select(PedagogicalObservation.evaluation_run_id).where(PedagogicalObservation.id.in_(inputs))
    events = select(ObservationEvaluationRun.event_id).where(ObservationEvaluationRun.id.in_(t3_runs))
    for model, subquery in ((CognitiveEvent, events), (ObservationEvaluationRun, t3_runs),
                            (PedagogicalObservation, inputs)):
        db.execute(select(model.id).where(model.id.in_(subquery)).order_by(model.id).with_for_update(read=True)).all()


def _chain_rows(db, parent_id: uuid.UUID) -> list:
    """Une seule jointure : observations du snapshot + run T3 + événement."""
    return db.execute(
        select(PedagogicalObservation.id, PedagogicalObservation.integrity_status,
               PedagogicalObservation.competency_code, ObservationEvaluationRun.id.label("t3_run_id"),
               ObservationEvaluationRun.execution_status, ObservationEvaluationRun.interpretation_status,
               CognitiveEvent.id.label("event_id"), CognitiveEvent.status.label("event_status"),
               CognitiveEvent.user_id.label("event_user_id"))
        .select_from(LongitudinalAssessmentInput)
        .join(PedagogicalObservation, PedagogicalObservation.id == LongitudinalAssessmentInput.observation_id)
        .join(ObservationEvaluationRun, ObservationEvaluationRun.id == PedagogicalObservation.evaluation_run_id)
        .join(CognitiveEvent, CognitiveEvent.id == ObservationEvaluationRun.event_id)
        .where(LongitudinalAssessmentInput.run_id == parent_id)
        .order_by(PedagogicalObservation.id)
    ).all()


def _chain_problems(parent, rows) -> tuple:
    """(incohérences d'identité, statuts non courants) de la chaîne."""
    identity, status = [], []
    for row in rows:
        if row.competency_code != parent.competency_code:
            identity.append(f"observation {row.id} de {row.competency_code}, dossier {parent.competency_code}")
        if row.event_user_id != parent.user_id:
            identity.append(f"observation {row.id} d'un événement d'un autre utilisateur")
        if row.integrity_status != VALID:
            status.append(f"observation {row.id} {row.integrity_status}")
        if (row.execution_status, row.interpretation_status) != (COMPLETED, ACTIVE):
            status.append(f"observation {row.id} d'un run T3 {row.execution_status} / {row.interpretation_status}")
        if row.event_status != FINALIZED:
            status.append(f"observation {row.id} d'un événement {row.event_status}")
    return identity, status


def _dossier(db, parent_id: uuid.UUID, error) -> LongitudinalDossier:
    """Vue T5-C du parent. Une vue d'audit (amont changé depuis le snapshot)
    n'est jamais utilisable comme dossier courant."""
    try:
        dossier = build_longitudinal_dossier(db, run_id=parent_id)
    except LongitudinalViewError as exc:
        raise error(f"dossier T5 {parent_id} non reconstructible : {exc}") from exc
    changed = [limitation for limitation in dossier.limitations
               if limitation.code == UPSTREAM_EVIDENCE_CHANGED_SINCE_SNAPSHOT]
    if changed:
        raise error(f"dossier T5 {parent_id} : amont changé depuis le snapshot ({changed[0].observation_ids})")
    return dossier


def _relations(db, parent_id: uuid.UUID) -> _Relations:
    """Relations du run T5 par identité SÉMANTIQUE (trois SELECT groupés)."""
    payload, identities, endpoints = {}, {}, {}
    dependencies = []
    for row in db.execute(
        select(ObservationDependency.id, ObservationDependency.target_observation_id,
               ObservationDependency.source_kind, ObservationDependency.source_observation_id,
               ObservationDependency.source_support_trace_id, ObservationDependency.dependency_type,
               ObservationDependency.scope_mode, ObservationDependency.scope_fingerprint,
               ObservationDependency.dependency_basis)
        .where(ObservationDependency.run_id == parent_id)
    ).all():
        identity = {"target_observation_id": str(row.target_observation_id), "source_kind": row.source_kind,
                    "source_observation_id": _str(row.source_observation_id),
                    "source_support_trace_id": _str(row.source_support_trace_id),
                    "scope_fingerprint": row.scope_fingerprint}
        dependencies.append({**identity, "dependency_type": row.dependency_type, "scope_mode": row.scope_mode,
                             "dependency_basis": row.dependency_basis})
        identities[(SOURCE_DEPENDENCY, row.id)] = identity
        endpoints[(SOURCE_DEPENDENCY, row.id)] = {row.target_observation_id, row.source_observation_id} - {None}
    transfers = []
    for row in db.execute(
        select(ObservationTransfer.id, ObservationTransfer.source_observation_id,
               ObservationTransfer.target_observation_id, ObservationTransfer.scope_mode,
               ObservationTransfer.scope_fingerprint, ObservationTransfer.transfer_basis)
        .where(ObservationTransfer.run_id == parent_id)
    ).all():
        identity = {"source_observation_id": str(row.source_observation_id),
                    "target_observation_id": str(row.target_observation_id),
                    "scope_fingerprint": row.scope_fingerprint}
        transfers.append({**identity, "scope_mode": row.scope_mode, "transfer_basis": row.transfer_basis})
        identities[(SOURCE_TRANSFER, row.id)] = identity
        endpoints[(SOURCE_TRANSFER, row.id)] = {row.source_observation_id, row.target_observation_id}
    revalidations = []
    for row in db.execute(
        select(ObservationRevalidation.id, ObservationRevalidation.source_contradiction_observation_id,
               ObservationRevalidation.target_supportive_observation_id, ObservationRevalidation.scope_mode,
               ObservationRevalidation.scope_fingerprint, ObservationRevalidation.revalidation_basis)
        .where(ObservationRevalidation.run_id == parent_id)
    ).all():
        identity = {"source_contradiction_observation_id": str(row.source_contradiction_observation_id),
                    "target_supportive_observation_id": str(row.target_supportive_observation_id),
                    "scope_fingerprint": row.scope_fingerprint}
        revalidations.append({**identity, "scope_mode": row.scope_mode,
                              "revalidation_basis": row.revalidation_basis})
        identities[(SOURCE_REVALIDATION, row.id)] = identity
        endpoints[(SOURCE_REVALIDATION, row.id)] = {row.source_contradiction_observation_id,
                                                     row.target_supportive_observation_id}
    payload = {"dependencies": _canonical_list(dependencies), "transfers": _canonical_list(transfers),
               "revalidations": _canonical_list(revalidations)}
    return _Relations(payload, identities, endpoints)


def _run_row(db, run_id: uuid.UUID):
    return db.execute(
        select(*CompetencyInferenceRun.__table__.c).where(CompetencyInferenceRun.id == run_id)
    ).one_or_none()


def _snapshot(db, run) -> PredecessorSnapshot:
    established = set(db.execute(
        select(CompetencyStageClaim.stage)
        .where(CompetencyStageClaim.inference_run_id == run.id,
               CompetencyStageClaim.positive_basis_status == ESTABLISHED)
    ).scalars())
    return PredecessorSnapshot(
        inference_run_id=run.id,
        longitudinal_assessment_run_id=run.longitudinal_assessment_run_id,
        current_stage=run.current_stage,
        previous_stage=run.previous_stage,
        transition=run.transition,
        transition_cause=run.transition_cause,
        tension_state=run.tension_state,
        unresolved_revision_context=_freeze(run.unresolved_revision_context),
        established_claim_stages=tuple(s for s in CLAIM_STAGES if s in established),
        input_fingerprint=run.input_fingerprint,
        output_fingerprint=run.output_fingerprint,
    )


def _require_completed_active(run) -> None:
    if (run.execution_status, run.interpretation_status) != (COMPLETED, ACTIVE):
        raise InvalidInferenceState(f"run T6 active {run.id} est {run.execution_status} ({COMPLETED} attendu)")
    missing = [name for name in ("current_stage", "tension_state", "output_fingerprint")
               if getattr(run, name) is None]
    if missing or run.current_stage not in CURRENT_STAGES or run.tension_state not in TENSION_STATES:
        raise InvalidInferenceState(f"run T6 active {run.id} sans sorties cohérentes ({missing})")


def _active_and_cache(db, user_id: str, competency_code: str) -> tuple:
    """(run T6 active | None, cache | None) lus par UNE seule requête (un seul
    snapshot : jamais une fausse divergence due à une activation commitée
    entre deux lectures)."""
    run_columns = CompetencyInferenceRun.__table__.c.keys()
    cache_columns = UserCompetencyState.__table__.c.keys()
    active = (select(*CompetencyInferenceRun.__table__.c)
              .where(CompetencyInferenceRun.user_id == user_id,
                     CompetencyInferenceRun.competency_code == competency_code,
                     CompetencyInferenceRun.interpretation_status == ACTIVE)
              .subquery("active_run"))
    cache = (select(*UserCompetencyState.__table__.c)
             .where(UserCompetencyState.user_id == user_id,
                    UserCompetencyState.competency_code == competency_code)
             .subquery("cache"))
    rows = db.execute(
        select(*(active.c[name].label(f"run_{name}") for name in run_columns),
               *(cache.c[name].label(f"cache_{name}") for name in cache_columns))
        .select_from(active.outerjoin(
            cache, (active.c.user_id == cache.c.user_id) & (active.c.competency_code == cache.c.competency_code),
            full=True))
    ).all()
    if len(rows) > 1:
        raise InvalidInferenceState(f"{len(rows)} runs T6 actifs pour ({user_id}, {competency_code})")
    if not rows:
        return None, None
    row = rows[0]._mapping
    run_value = None if row["run_id"] is None else _Row({name: row[f"run_{name}"] for name in run_columns})
    cache_value = None if row["cache_user_id"] is None else _Row(
        {name: row[f"cache_{name}"] for name in cache_columns})
    return run_value, cache_value


class _Row:
    """Ligne lue par colonnes, accès par attribut."""
    __slots__ = ("_values",)

    def __init__(self, values: dict):
        self._values = values

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None


def _check_cache(active, cache, user_id: str, competency_code: str) -> None:
    """Le cache doit refléter EXACTEMENT l'active (ou être absent sans
    active). Jamais réparé."""
    if active is None:
        if cache is not None:
            raise InvalidInferenceState(f"cache ({user_id}, {competency_code}) sans run T6 active")
        return
    if cache is None:
        raise InvalidInferenceState(f"run T6 active {active.id} sans cache user_competency_states")
    mismatches = [name for name, expected in (("user_id", user_id), ("competency_code", competency_code),
                                              ("active_inference_run_id", active.id),
                                              ("current_stage", active.current_stage),
                                              ("tension_state", active.tension_state))
                  if getattr(cache, name) != expected]
    if mismatches:
        raise InvalidInferenceState(f"cache ({user_id}, {competency_code}) divergent de l'active {active.id} :"
                                    f" {', '.join(mismatches)}")
    if type(cache.state_generation) is not int or cache.state_generation < 1:
        raise InvalidInferenceState(f"cache ({user_id}, {competency_code}) : state_generation"
                                    f" {cache.state_generation!r} incohérent")


def _children(db, run_id: uuid.UUID) -> tuple:
    """(claims, tensions, refs, périmètres de tension) d'un run, en une
    requête."""
    def count(model, *conditions):
        return select(func.count()).select_from(model).where(*conditions).scalar_subquery()

    return tuple(db.execute(select(
        count(CompetencyStageClaim, CompetencyStageClaim.inference_run_id == run_id),
        count(CompetencyInferenceTension, CompetencyInferenceTension.inference_run_id == run_id),
        count(CompetencyInferenceBasisRef, CompetencyInferenceBasisRef.inference_run_id == run_id),
        count(CompetencyInferenceTensionCapability, CompetencyInferenceTensionCapability.tension_id.in_(
            select(CompetencyInferenceTension.id).where(CompetencyInferenceTension.inference_run_id == run_id))),
    )).one())


def _require_pristine_candidate(db, run) -> None:
    """running / candidate, sorties NULL, aucune ligne enfant : sinon le
    candidat a été écrit hors service (InvalidInferenceState, jamais réparé)."""
    if (run.execution_status, run.interpretation_status) != (RUNNING, CANDIDATE):
        raise InvalidInferenceState(f"{run.id} est {run.execution_status} / {run.interpretation_status}"
                                    f" ({RUNNING} / {CANDIDATE} requis)")
    written = [name for name in ("previous_stage", "current_stage", "transition", "transition_cause",
                                 "tension_state", "unresolved_revision_context", "validation_needs",
                                 "state_decision_summary", "output_fingerprint", "completed_at", "failure_code")
               if getattr(run, name) is not None]
    if written:
        raise InvalidInferenceState(f"candidat {run.id} porte déjà des sorties : {', '.join(written)}")
    children = _children(db, run.id)
    if any(children):
        raise InvalidInferenceState(f"candidat {run.id} porte déjà des lignes enfants"
                                    f" (claims, tensions, refs, périmètres) = {children}")


def _verified_inputs(db, run, *, lock: bool) -> _Inputs:
    """Entrée d'un candidat existant, revérifiée intégralement (sous verrous
    partagés si lock) : parent T5 courant, chaîne amont, dossier T5-C,
    predecessor capturé, input_fingerprint et inference_dedup_key
    recalculés. Jamais réécrits."""
    parent = _parent_row(db, run.longitudinal_assessment_run_id, lock=lock)
    if parent is None:
        raise InvalidInferenceState(f"run T5 parent {run.longitudinal_assessment_run_id} introuvable")
    if (parent.user_id, parent.competency_code) != (run.user_id, run.competency_code):
        raise InvalidInferenceState(f"candidat {run.id} et son run T5 parent n'ont pas le même couple")
    problems = _parent_problems(db, parent, lock=lock, check_release=True)
    if problems:
        raise StaleInferenceInput("; ".join(problems))
    if lock:
        _share_chain(db, parent.id)
    identity, status = _chain_problems(parent, _chain_rows(db, parent.id))
    if identity or status:
        raise StaleInferenceInput("; ".join(identity + status))
    dossier = _dossier(db, parent.id, StaleInferenceInput)
    relations = _relations(db, parent.id)
    predecessor = snapshot = None
    if run.predecessor_inference_run_id is not None:
        predecessor = _run_row(db, run.predecessor_inference_run_id)
        if predecessor is None or (predecessor.user_id, predecessor.competency_code) != (
                run.user_id, run.competency_code) or predecessor.execution_status != COMPLETED:
            raise InvalidInferenceState(f"predecessor {run.predecessor_inference_run_id} du candidat incohérent")
        snapshot = _snapshot(db, predecessor)
    fingerprint = _input_fingerprint(_dossier_payload(parent, relations),
                                     _predecessor_payload(snapshot, None if predecessor is None
                                                          else predecessor.unresolved_revision_context))
    if fingerprint != run.input_fingerprint:
        raise StaleInferenceInput(f"{run.id} : input_fingerprint différent de l'entrée courante")
    if _inference_dedup_key(fingerprint, _specification(run)) != run.inference_dedup_key:
        raise InvalidInferenceState(f"{run.id} : inference_dedup_key incohérente avec l'entrée et les versions")
    return _Inputs(parent, dossier, relations, predecessor, snapshot, fingerprint)


def _context(run, inputs: _Inputs) -> InferenceContext:
    return InferenceContext(
        run_id=run.id,
        user_id=run.user_id,
        competency_code=run.competency_code,
        trigger=run.trigger,
        longitudinal_assessment_run_id=inputs.parent.id,
        pedagogical_taxonomy_release_id=inputs.parent.pedagogical_taxonomy_release_id,
        longitudinal_dossier=inputs.dossier,
        predecessor=inputs.snapshot,
        input_fingerprint=run.input_fingerprint,
        inference_dedup_key=run.inference_dedup_key,
        **{name: getattr(run, name) for name in SPECIFICATION_FIELDS},
    )


# --------------------------------------------------------------------------
# Vérification de la décision contre le dossier et le predecessor
# --------------------------------------------------------------------------

def _membership_definitions(db, tensions: tuple, parent) -> dict:
    """{membership_id: capability_definition_id} des memberships des
    tensions : release du parent T5 et compétence du run uniquement ; jamais
    de correspondance par capability_code."""
    wanted = {m for t in tensions for m in t.membership_ids}
    if not wanted:
        return {}
    rows = {row.id: row for row in db.execute(
        select(CapabilityTaxonomyMembership.id, CapabilityTaxonomyMembership.taxonomy_release_id,
               CoreCapabilityDefinition.id.label("definition_id"), CoreCapabilityDefinition.competency_code)
        .join(CoreCapabilityDefinition,
              CoreCapabilityDefinition.id == CapabilityTaxonomyMembership.capability_definition_id)
        .where(CapabilityTaxonomyMembership.id.in_(wanted))
    ).all()}
    for tension in tensions:
        for membership_id in tension.membership_ids:
            row = rows.get(membership_id)
            if row is None:
                raise InvalidInferenceTension(f"{tension.key} : membership {membership_id} inconnu")
            if row.taxonomy_release_id != parent.pedagogical_taxonomy_release_id:
                raise InvalidInferenceTension(f"{tension.key} : membership {membership_id} d'une autre release")
            if row.competency_code != parent.competency_code:
                raise InvalidInferenceTension(f"{tension.key} : membership {membership_id} hors de"
                                              f" {parent.competency_code}")
    return {membership_id: row.definition_id for membership_id, row in rows.items()}


def _source_scopes(dossier: LongitudinalDossier) -> tuple:
    """({observation_id: HistoryObservation}, {(kind, relation_id):
    définitions du périmètre résolu}) du dossier T5 consommé."""
    observations = {o.observation_id: o for o in dossier.active_history.observations}
    scopes = {}
    for kind, relations in ((SOURCE_DEPENDENCY, dossier.dependency_profile.dependencies),
                            (SOURCE_TRANSFER, dossier.transfer_profile.transfers),
                            (SOURCE_REVALIDATION, dossier.consistency_profile.historical_revalidations)):
        for relation in relations:
            scopes[(kind, relation.relation_id)] = frozenset(c.definition_id for c in relation.scope.capabilities)
    return observations, scopes


def _check_decision(db, run, inputs: _Inputs, predecessor, decision: _Decision) -> tuple:
    """Tout ce qui dépend du dossier et du predecessor. Retourne
    (previous_stage, transition, tension_state, {membership: définition})."""
    error = InvalidInferenceDecision
    observations, relation_scopes = _source_scopes(inputs.dossier)

    # Provenance : chaque source appartient au dossier T5 consommé.
    for ref in decision.refs:
        if ref.source_kind == SOURCE_OBSERVATION:
            observation = observations.get(ref.source_id)
            if observation is None:
                raise InvalidInferenceBasisReference(f"ref {ref.role} : observation {ref.source_id} hors du"
                                                     f" snapshot du run T5 {inputs.parent.id}")
            if ref.role == POSITIVE_BASIS and observation.polarity != SUPPORTIVE:
                raise InvalidInferenceBasisReference(f"positive_basis : observation {ref.source_id}"
                                                     f" {observation.polarity} ({SUPPORTIVE} requise)")
        elif (ref.source_kind, ref.source_id) not in relation_scopes:
            raise InvalidInferenceBasisReference(f"ref {ref.role} : {ref.source_kind} {ref.source_id} n'appartient"
                                                 f" pas au run T5 {inputs.parent.id}")

    # Tensions : stade défendable, memberships, périmètre couvert.
    established = {s for s, c in decision.claims.items() if c.status == ESTABLISHED}
    historical = set() if inputs.snapshot is None else set(inputs.snapshot.established_claim_stages)
    for tension in decision.tensions:
        held = inputs.snapshot is not None and not _above(tension.fragilized_stage, inputs.snapshot.current_stage)
        if tension.fragilized_stage not in established | historical and not held:
            raise InvalidInferenceTension(f"{tension.key} : {tension.fragilized_stage} n'est established ni dans"
                                          " cette décision ni dans le predecessor (aucune prétention à fragiliser)")
    membership_definitions = _membership_definitions(db, decision.tensions, inputs.parent)
    for tension in decision.tensions:
        if tension.scope_mode != LOCALIZED:
            continue
        covered = set()
        for ref in decision.refs:
            if ref.role != TENSION or ref.tension_key != tension.key:
                continue
            if ref.source_kind == SOURCE_OBSERVATION:
                observation = observations[ref.source_id]
                if observation.capability_localization == LOCALIZED:
                    covered |= {c.definition_id for c in observation.compatible_capabilities}
            else:
                covered |= relation_scopes[(ref.source_kind, ref.source_id)]
        outside = [m for m in tension.membership_ids if membership_definitions[m] not in covered]
        if outside:
            raise InvalidInferenceTension(f"{tension.key} : memberships {sorted(map(str, outside))} hors du"
                                          " périmètre réel de ses sources tension")

    # Transition dérivée.
    tension_state = TENSION_OPEN if decision.tensions else TENSION_NONE
    current = decision.current_stage
    if predecessor is None:
        if decision.cause is not None:
            raise error("première inférence : transition_cause doit être None")
        previous_stage = transition = None
    else:
        if decision.cause is None:
            raise error("transition_cause obligatoire lorsqu'un predecessor existe")
        previous_stage = predecessor.current_stage
        if current == previous_stage:
            transition = MAINTAINED
        elif _above(current, previous_stage):
            transition = UPGRADED
        else:
            transition = REVISED_DOWN

    highest = _highest_established(decision.claims)
    if transition == UPGRADED and decision.claims[current].status != ESTABLISHED:
        raise error(f"{UPGRADED} vers {current} sans claim {current} established")
    if transition == REVISED_DOWN:
        if current != NON_ETABLI and decision.claims[current].status != ESTABLISHED:
            raise error(f"{REVISED_DOWN} vers {current} sans claim {current} established"
                        " (jamais une révision par simple élimination)")
        if current == NON_ETABLI and decision.cause == NEW_USER_EVIDENCE:
            raise error(f"{REVISED_DOWN} vers {NON_ETABLI} par {NEW_USER_EVIDENCE} refusé : une contradiction"
                        " nouvelle seule ne remet jamais la compétence à zéro")
    if current != NON_ETABLI and _above(current, highest):
        if not (predecessor is not None and current == previous_stage and tension_state == TENSION_OPEN
                and decision.context):
            raise error(f"current_stage {current} au-dessus de la plus haute claim established ({highest}) :"
                        " acceptable seulement comme maintien du stade précédent, sous tension ouverte, avec"
                        " unresolved_revision_context")
    if transition in (UPGRADED, REVISED_DOWN) and not any(r.role == TRANSITION for r in decision.refs):
        raise error(f"transition {transition} sans ref transition")

    if predecessor is not None:
        previous_inputs = set(db.execute(
            select(LongitudinalAssessmentInput.observation_id)
            .where(LongitudinalAssessmentInput.run_id == predecessor.longitudinal_assessment_run_id)
        ).scalars())
        new_observations = set(observations) - previous_inputs
        if decision.cause == NEW_USER_EVIDENCE and not new_observations:
            raise error(f"{NEW_USER_EVIDENCE} sans aucune observation absente du dossier du predecessor")
        if decision.cause == PEDAGOGICAL_REINTERPRETATION and _specification(run) == _specification(predecessor):
            raise error(f"{PEDAGOGICAL_REINTERPRETATION} sans aucune spécification différente du predecessor")
        unresolved = (predecessor.unresolved_revision_context is not None
                      or predecessor.tension_state == TENSION_OPEN or predecessor.transition == REVISED_DOWN)
        if unresolved and transition == UPGRADED and decision.cause == NEW_USER_EVIDENCE:
            cited = set()
            for ref in decision.refs:
                if ref.role in REBOUND_ROLES:
                    cited |= ({ref.source_id} if ref.source_kind == SOURCE_OBSERVATION
                              else inputs.relations.endpoints[(ref.source_kind, ref.source_id)])
            if not cited & new_observations:
                raise error("remontée après une révision non résolue : aucune ref positive_basis / transition ne"
                            " cite une observation nouvelle (les anciennes preuves seules ne suffisent jamais)")
    return previous_stage, transition, tension_state, membership_definitions


# --------------------------------------------------------------------------
# Verrouillage
# --------------------------------------------------------------------------

def _lock_couple(db, user_id: str, competency_code: str) -> None:
    """pg_advisory_xact_lock : libéré à la fin de la transaction de
    l'appelant (jamais un verrou de session)."""
    db.execute(select(func.pg_advisory_xact_lock(_activation_lock_key(user_id, competency_code))))


def _lock_run(db, run_id: uuid.UUID) -> CompetencyInferenceRun:
    run = db.execute(
        select(CompetencyInferenceRun)
        .where(CompetencyInferenceRun.id == run_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if run is None:
        raise InferenceRunNotFound(str(run_id))
    return run


def _lock_active(db, user_id: str, competency_code: str) -> CompetencyInferenceRun | None:
    actives = db.execute(
        select(CompetencyInferenceRun)
        .where(CompetencyInferenceRun.user_id == user_id,
               CompetencyInferenceRun.competency_code == competency_code,
               CompetencyInferenceRun.interpretation_status == ACTIVE)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalars().all()
    if len(actives) > 1:
        raise InvalidInferenceState(f"{len(actives)} runs T6 actifs pour ({user_id}, {competency_code})")
    return actives[0] if actives else None


def _lock_cache(db, user_id: str, competency_code: str) -> UserCompetencyState | None:
    return db.execute(
        select(UserCompetencyState)
        .where(UserCompetencyState.user_id == user_id, UserCompetencyState.competency_code == competency_code)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


# --------------------------------------------------------------------------
# Persistance
# --------------------------------------------------------------------------

def _persist_children(db, run, decision: _Decision) -> None:
    """Claims (4), tensions et leurs périmètres, puis refs résolues vers les
    lignes qui viennent d'être créées. UUID générés par l'application."""
    now = _utcnow()
    claim_ids = {stage: uuid.uuid4() for stage in CLAIM_STAGES}
    db.add_all([CompetencyStageClaim(
        id=claim_ids[stage], inference_run_id=run.id, stage=stage,
        positive_basis_status=decision.claims[stage].status, basis_mode=decision.claims[stage].mode,
        basis_summary=decision.claims[stage].basis_summary, scope_summary=decision.claims[stage].scope_summary,
        confidence_profile=decision.claims[stage].confidence_profile,
        mastery_assessment=decision.claims[stage].mastery_assessment, created_at=now,
    ) for stage in CLAIM_STAGES])
    db.flush()
    tension_ids = {tension.key: uuid.uuid4() for tension in decision.tensions}
    if decision.tensions:
        db.add_all([CompetencyInferenceTension(
            id=tension_ids[t.key], inference_run_id=run.id, fragilized_stage=t.fragilized_stage,
            scope_mode=t.scope_mode, summary=t.summary, revision_status=t.revision_status, created_at=now,
        ) for t in decision.tensions])
        db.flush()
        scope_rows = [CompetencyInferenceTensionCapability(tension_id=tension_ids[t.key], capability_membership_id=m)
                      for t in decision.tensions for m in t.membership_ids]
        if scope_rows:
            db.add_all(scope_rows)
            db.flush()
    if decision.refs:
        db.add_all([CompetencyInferenceBasisRef(
            id=uuid.uuid4(), inference_run_id=run.id,
            stage_claim_id=None if r.claim_stage is None else claim_ids[r.claim_stage],
            tension_id=None if r.tension_key is None else tension_ids[r.tension_key],
            ref_role=r.role, confidence_dimension=r.dimension, source_kind=r.source_kind,
            source_observation_id=r.source_id if r.source_kind == SOURCE_OBSERVATION else None,
            source_dependency_id=r.source_id if r.source_kind == SOURCE_DEPENDENCY else None,
            source_transfer_id=r.source_id if r.source_kind == SOURCE_TRANSFER else None,
            source_revalidation_id=r.source_id if r.source_kind == SOURCE_REVALIDATION else None,
            created_at=now,
        ) for r in decision.refs])
        db.flush()


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def start_competency_inference(
    db,
    *,
    longitudinal_assessment_run_id: uuid.UUID,
    trigger: str,
    positive_basis_version: str,
    confidence_profile_version: str,
    state_decision_version: str,
    validation_version: str,
    inference_schema_version: str,
    evaluator_version: str,
    model_id: str | None = None,
    prompt_spec_version: str | None = None,
) -> InferenceContext:
    """Crée un candidat running / candidate et retourne son InferenceContext.

    Le run T5 parent doit être le dossier COURANT du couple (voir la
    docstring du module), sinon LongitudinalParentNotUsable. user_id,
    competency_code, predecessor, input_fingerprint et inference_dedup_key
    sont dérivés ; aucune sortie ni ligne enfant n'est créée.

    Déduplication : inference_dedup_key déjà présente, ou réinterprétation à
    l'identique (même dossier logique, mêmes spécifications) du dossier de
    l'active courant => DuplicateInference, jamais le retour de l'ancien
    run. Pré-vérification puis UNIQUE uq_competency_inference_runs_dedup_key
    en défense finale (INSERT dans un SAVEPOINT : seule CETTE violation est
    traduite, la transaction de l'appelant reste utilisable)."""
    _require_uuid(longitudinal_assessment_run_id, "longitudinal_assessment_run_id")
    _require_text(trigger, "trigger")
    specification = {name: _require_text(value, name) for name, value in (
        ("positive_basis_version", positive_basis_version),
        ("confidence_profile_version", confidence_profile_version),
        ("state_decision_version", state_decision_version),
        ("validation_version", validation_version),
        ("inference_schema_version", inference_schema_version),
        ("evaluator_version", evaluator_version))}
    specification["model_id"] = _optional_text(model_id, "model_id")
    specification["prompt_spec_version"] = _optional_text(prompt_spec_version, "prompt_spec_version")

    parent = _parent_row(db, longitudinal_assessment_run_id, lock=False)
    if parent is None:
        raise LongitudinalParentNotUsable(f"run T5 {longitudinal_assessment_run_id} introuvable")
    problems = _parent_problems(db, parent, lock=False, check_release=True)
    identity, status = _chain_problems(parent, _chain_rows(db, parent.id))
    if problems or identity or status:
        raise LongitudinalParentNotUsable("; ".join(problems + identity + status))
    dossier = _dossier(db, parent.id, LongitudinalParentNotUsable)
    relations = _relations(db, parent.id)

    active, cache = _active_and_cache(db, parent.user_id, parent.competency_code)
    if active is not None:
        _require_completed_active(active)
    _check_cache(active, cache, parent.user_id, parent.competency_code)
    snapshot = None if active is None else _snapshot(db, active)
    dossier_payload = _dossier_payload(parent, relations)
    fingerprint = _input_fingerprint(dossier_payload, _predecessor_payload(
        snapshot, None if active is None else active.unresolved_revision_context))
    dedup_key = _inference_dedup_key(fingerprint, specification)

    if active is not None and _specification(active) == specification:
        previous_parent = _parent_row(db, active.longitudinal_assessment_run_id, lock=False)
        if previous_parent is None:
            raise InvalidInferenceState(f"active {active.id} : run T5 parent introuvable")
        if _dossier_payload(previous_parent, _relations(db, previous_parent.id)) == dossier_payload:
            raise DuplicateInference(f"réinterprétation à l'identique de l'active {active.id} (même dossier"
                                     " logique, mêmes spécifications) : changer une version")
    if db.execute(select(CompetencyInferenceRun.id)
                  .where(CompetencyInferenceRun.inference_dedup_key == dedup_key)).first() is not None:
        raise DuplicateInference(dedup_key)

    now = _utcnow()
    run = CompetencyInferenceRun(
        id=uuid.uuid4(),
        user_id=parent.user_id,
        competency_code=parent.competency_code,
        longitudinal_assessment_run_id=parent.id,
        predecessor_inference_run_id=None if active is None else active.id,
        execution_status=RUNNING,
        interpretation_status=CANDIDATE,
        trigger=trigger,
        previous_stage=None,
        current_stage=None,
        transition=None,
        transition_cause=None,
        tension_state=None,
        unresolved_revision_context=None,
        validation_needs=None,
        state_decision_summary=None,
        **specification,
        input_fingerprint=fingerprint,
        output_fingerprint=None,
        inference_dedup_key=dedup_key,
        started_at=now,
        completed_at=None,
        created_at=now,
        failure_code=None,
    )
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError as exc:
        if _is_dedup_violation(exc):
            raise DuplicateInference(dedup_key) from exc
        raise
    return _context(run, _Inputs(parent, dossier, relations, active, snapshot, fingerprint))


def get_inference_context(db, *, run_id: uuid.UUID) -> InferenceContext:
    """Reconstruit, pour la reprise d'un candidat running / candidate, le
    contexte EXACT attendu par T6-C. Entrée devenue non courante =>
    StaleInferenceInput ; active remplacé => StaleInferencePredecessor ;
    corruption => InvalidInferenceState. Jamais de nouveau candidat, aucune
    mutation."""
    _require_uuid(run_id, "run_id")
    with db.no_autoflush:
        run = _run_row(db, run_id)
        if run is None:
            raise InferenceRunNotFound(str(run_id))
        _require_pristine_candidate(db, run)
        inputs = _verified_inputs(db, run, lock=False)
        active, cache = _active_and_cache(db, run.user_id, run.competency_code)
        if (None if active is None else active.id) != run.predecessor_inference_run_id:
            raise StaleInferencePredecessor(f"{run_id} : l'active courant n'est plus le predecessor capturé")
        _check_cache(active, cache, run.user_id, run.competency_code)
        return _context(run, inputs)


def complete_competency_inference(db, *, run_id: uuid.UUID, decision: InferenceDecision) -> CompetencyInferenceRun:
    """running / candidate -> completed / active, atomiquement avec la
    supersession de l'ancien active et la mise à jour du cache.

    1. décision validée structurellement, avant tout accès à la base ;
    2. verrous dans l'ordre global (voir la docstring du module) ;
    3. revérification sous verrou : candidat vierge, parent T5 courant,
       chaîne T2 / T3, dossier T5-C, input_fingerprint recalculé
       (StaleInferenceInput), active courant = predecessor capturé
       (StaleInferencePredecessor), cache (InvalidInferenceState) ;
    4. décision contre le dossier et le predecessor (provenance, périmètres,
       transition dérivée, causes, maintien, anti-oscillation) ;
    5. SEULEMENT ALORS : claims, tensions, périmètres, refs, sorties et
       output_fingerprint ; ancien active superseded + flush ; candidat
       active + flush ; cache créé ou mis à jour + flush.
    Toute erreur survient avant la première mutation."""
    _require_uuid(run_id, "run_id")
    checked = _validated_decision(decision)

    identity = db.execute(
        select(CompetencyInferenceRun.user_id, CompetencyInferenceRun.competency_code)
        .where(CompetencyInferenceRun.id == run_id)
    ).one_or_none()
    if identity is None:
        raise InferenceRunNotFound(str(run_id))
    _lock_couple(db, identity.user_id, identity.competency_code)
    run = _lock_run(db, run_id)
    _require_pristine_candidate(db, run)
    inputs = _verified_inputs(db, run, lock=True)
    active = _lock_active(db, run.user_id, run.competency_code)
    if (None if active is None else active.id) != run.predecessor_inference_run_id:
        raise StaleInferencePredecessor(f"{run_id} : l'active courant n'est plus le predecessor capturé ;"
                                        " un candidat ancien n'écrase jamais un état plus récent")
    if active is not None:
        _require_completed_active(active)
    cache = _lock_cache(db, run.user_id, run.competency_code)
    _check_cache(active, cache, run.user_id, run.competency_code)
    previous_stage, transition, tension_state, membership_definitions = _check_decision(
        db, run, inputs, active, checked)

    # Première mutation : tout est vérifié.
    _persist_children(db, run, checked)
    run.previous_stage = previous_stage
    run.current_stage = checked.current_stage
    run.transition = transition
    run.transition_cause = checked.cause
    run.tension_state = tension_state
    run.unresolved_revision_context = checked.context
    run.validation_needs = checked.needs
    run.state_decision_summary = checked.summary
    run.output_fingerprint = _output_fingerprint(
        input_fingerprint=run.input_fingerprint, specification=_specification(run), decision=checked,
        previous_stage=previous_stage, transition=transition, tension_state=tension_state,
        relations=inputs.relations, membership_definitions=membership_definitions)
    db.flush()
    if active is not None:
        active.interpretation_status = SUPERSEDED
        db.flush()
    now = _utcnow()
    run.execution_status = COMPLETED
    run.interpretation_status = ACTIVE
    run.completed_at = now
    run.failure_code = None
    db.flush()
    if cache is None:
        db.add(UserCompetencyState(
            user_id=run.user_id, competency_code=run.competency_code, active_inference_run_id=run.id,
            current_stage=run.current_stage, tension_state=run.tension_state, state_generation=1, updated_at=now))
    else:
        cache.active_inference_run_id = run.id
        cache.current_stage = run.current_stage
        cache.tension_state = run.tension_state
        cache.state_generation = cache.state_generation + 1
        cache.updated_at = now
    db.flush()
    return run


def fail_competency_inference(
    db,
    *,
    run_id: uuid.UUID,
    failure_code: str | None = None,
) -> CompetencyInferenceRun:
    """running / candidate -> failed / obsolete. Verrou du seul candidat :
    l'active courant et le cache ne sont jamais touchés. Une seconde
    transition est refusée ; un candidat portant déjà des sorties ou des
    lignes enfants est corrompu (InvalidInferenceState, jamais réparé)."""
    _require_uuid(run_id, "run_id")
    _optional_text(failure_code, "failure_code")

    run = _lock_run(db, run_id)
    _require_pristine_candidate(db, run)
    run.execution_status = FAILED
    run.interpretation_status = OBSOLETE
    run.completed_at = _utcnow()
    run.failure_code = failure_code
    db.flush()
    return run


def get_competency_inference(db, *, run_id: uuid.UUID) -> CompetencyInferenceRun:
    """Lecture simple, sans verrou."""
    _require_uuid(run_id, "run_id")
    run = db.get(CompetencyInferenceRun, run_id)
    if run is None:
        raise InferenceRunNotFound(str(run_id))
    return run


def _require_couple(db, user_id, competency_code) -> None:
    _require_text(user_id, "user_id")
    _require_choice(competency_code, "competency_code", COMPETENCY_CODES)
    if db.execute(select(User.id).where(User.id == user_id)).first() is None:
        raise UserNotFound(user_id)


def get_active_competency_inference(
    db,
    *,
    user_id: str,
    competency_code: str,
) -> CompetencyInferenceRun | None:
    """L'unique run T6 active du couple, ou None. Plusieurs actives
    (impossible sauf contournement de l'index) => InvalidInferenceState."""
    _require_couple(db, user_id, competency_code)
    actives = db.execute(
        select(CompetencyInferenceRun)
        .where(CompetencyInferenceRun.user_id == user_id,
               CompetencyInferenceRun.competency_code == competency_code,
               CompetencyInferenceRun.interpretation_status == ACTIVE)
    ).scalars().all()
    if len(actives) > 1:
        raise InvalidInferenceState(f"{len(actives)} runs T6 actifs pour ({user_id}, {competency_code})")
    return actives[0] if actives else None


def get_stage_claims(db, *, run_id: uuid.UUID) -> list[CompetencyStageClaim]:
    """Claims du run dans l'ordre CONCEPTUEL discovery, comprehension,
    application, mastery (jamais un ordre probant)."""
    run = get_competency_inference(db, run_id=run_id)
    claims = db.execute(
        select(CompetencyStageClaim).where(CompetencyStageClaim.inference_run_id == run.id)
    ).scalars().all()
    return sorted(claims, key=lambda claim: (CLAIM_STAGES.index(claim.stage), str(claim.id)))


def get_inference_tensions(db, *, run_id: uuid.UUID) -> list[tuple[CompetencyInferenceTension, tuple]]:
    """(tension, capability_membership_ids triés) du run, ORDER BY
    created_at, id (ordre technique stable)."""
    run = get_competency_inference(db, run_id=run_id)
    tensions = db.execute(
        select(CompetencyInferenceTension).where(CompetencyInferenceTension.inference_run_id == run.id)
        .order_by(CompetencyInferenceTension.created_at, CompetencyInferenceTension.id)
    ).scalars().all()
    scopes = {}
    if tensions:
        for tension_id, membership_id in db.execute(
            select(CompetencyInferenceTensionCapability.tension_id,
                   CompetencyInferenceTensionCapability.capability_membership_id)
            .where(CompetencyInferenceTensionCapability.tension_id.in_([t.id for t in tensions]))
        ).all():
            scopes.setdefault(tension_id, []).append(membership_id)
    return [(t, tuple(sorted(scopes.get(t.id, ()), key=str))) for t in tensions]


def get_inference_basis_refs(db, *, run_id: uuid.UUID) -> list[CompetencyInferenceBasisRef]:
    """Refs du run, ORDER BY ref_role, source_kind, source, id : ordre
    technique stable, jamais une hiérarchie probante."""
    run = get_competency_inference(db, run_id=run_id)
    return list(db.execute(
        select(CompetencyInferenceBasisRef).where(CompetencyInferenceBasisRef.inference_run_id == run.id)
        .order_by(CompetencyInferenceBasisRef.ref_role, CompetencyInferenceBasisRef.source_kind,
                  func.coalesce(CompetencyInferenceBasisRef.source_observation_id,
                                CompetencyInferenceBasisRef.source_dependency_id,
                                CompetencyInferenceBasisRef.source_transfer_id,
                                CompetencyInferenceBasisRef.source_revalidation_id),
                  CompetencyInferenceBasisRef.id)
    ).scalars())


def get_user_competency_state(
    db,
    *,
    user_id: str,
    competency_code: str,
) -> UserCompetencyState | None:
    """Ligne BRUTE du cache, ou None : inspection technique seulement ; ne
    prétend jamais que la chaîne est encore valide."""
    _require_couple(db, user_id, competency_code)
    return db.execute(
        select(UserCompetencyState)
        .where(UserCompetencyState.user_id == user_id, UserCompetencyState.competency_code == competency_code)
    ).scalar_one_or_none()


def get_validated_user_competency_state(
    db,
    *,
    user_id: str,
    competency_code: str,
) -> ValidatedCompetencyState | None:
    """Lecture SÛRE (futur Step 6) : None si aucune inférence ; sinon cache
    -> active T6 -> parent T5 active -> observations valid -> runs T3
    completed / active -> événements finalized, et cohérence user /
    compétence / stade / tension. Chaîne devenue non courante =>
    StaleInferenceChain ; corruption interne => InvalidInferenceState.
    Lecture seule : ne supprime, ne réécrit, n'abaisse ni ne recalcule
    rien."""
    _require_couple(db, user_id, competency_code)
    with db.no_autoflush:
        active, cache = _active_and_cache(db, user_id, competency_code)
        if active is None and cache is None:
            return None
        _check_cache(active, cache, user_id, competency_code)
        _require_completed_active(active)
        claims, tensions, _, _ = _children(db, active.id)
        if claims != len(CLAIM_STAGES) or (tensions > 0) != (active.tension_state == TENSION_OPEN):
            raise InvalidInferenceState(f"active {active.id} : {claims} claims, {tensions} tensions pour"
                                        f" tension_state {active.tension_state}")
        parent = _parent_row(db, active.longitudinal_assessment_run_id, lock=False)
        if parent is None or (parent.user_id, parent.competency_code) != (user_id, competency_code):
            raise InvalidInferenceState(f"active {active.id} : run T5 parent absent ou d'un autre couple")
        problems = _parent_problems(db, parent, lock=False, check_release=False)
        identity, status = _chain_problems(parent, _chain_rows(db, parent.id))
        if identity:
            raise InvalidInferenceState("; ".join(identity))
        if problems or status:
            raise StaleInferenceChain("; ".join(problems + status))
        return ValidatedCompetencyState(
            user_id=user_id,
            competency_code=competency_code,
            current_stage=active.current_stage,
            tension_state=active.tension_state,
            active_inference_run_id=active.id,
            longitudinal_assessment_run_id=parent.id,
            state_generation=cache.state_generation,
        )
