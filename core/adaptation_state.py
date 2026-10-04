"""Étape 6.1A : instantané VALIDÉ de l'état d'adaptation (AdaptationStateSnapshot).

Question, et seulement elle : « qu'est-ce que Step 5 affirme réellement
aujourd'hui, sous une forme sûre, structurée et exploitable par les
prochaines briques de l'adaptation ? ». 6-1A ne personnalise aucune réponse :
pas de sélection cible / soutien (6-1B), pas de présupposition sûre (6-1C),
pas de posture ni d'opportunité de validation (6-1D / 6-1E), aucun
branchement runtime (/web-chat, moteurs, prompts, request.level).

    preuve utilisateur -> T3 observation -> T5 longitudinal -> T6 (Step 5)
        -> 6-1A AdaptationStateSnapshot

Jamais l'inverse : aucune sortie de ce module ne remonte dans la chaîne
probante. Step 5 possède l'état épistémique ; 6-1A le LIT. Il ne crée,
ne modifie, ne maintient ni ne dégrade jamais un stade ; il ne recalcule
jamais une claim, une confiance, une tension ni un validation_need ; il ne
crée ni observation ni preuve ; il ne relance jamais T6 (infer_competency,
evaluate_positive_basis, evaluate_inference_state).

Invariants :

- Lecture sûre : pour chaque compétence demandée, la SEULE porte d'entrée
  est get_validated_user_competency_state (cache -> active T6 -> parent T5
  active -> observations valid -> runs T3 completed / active -> événements
  finalized). get_user_competency_state (cache brut) n'est jamais une vérité
  pédagogique. None => compétence ABSENTE du snapshot : aucun T6 n'est pas un
  T6 concluant non_etabli (présent, current_stage non_etabli). Les C1-C12
  restent indépendantes ; current_stage reste local à UNE compétence ; aucun
  niveau global, aucun score, aucune moyenne.

- Strict : StaleInferenceChain => StaleAdaptationState ; InvalidInferenceState
  ou toute incohérence structurelle relevée ici => InvalidAdaptationState.
  Aucun snapshot partiel, aucune réparation, aucun repli : le fallback neutre
  du produit appartiendra à l'orchestrateur 6.1 / runtime.

- Anti-snapshot hybride : run_id = validated.active_inference_run_id est
  capturé UNE fois ; toutes les lectures suivantes lui sont rattachées
  EXPLICITEMENT (get_competency_inference / get_stage_claims /
  get_inference_tensions / get_inference_basis_refs(run_id)), jamais via
  une API « active courante ». Isolation : READ COMMITTED (SessionLocal),
  aucun verrou ni snapshot SQL ; la cohérence repose sur l'IMMUTABILITÉ
  garantie par les services propriétaires : un run T6 completed, ses claims,
  tensions, périmètres et refs ne sont plus jamais réécrits (seul
  interpretation_status passe active -> superseded) ; le run T5 parent, les
  memberships d'une release active / retirée, la localisation et les
  mappings T4 d'une observation d'un run T3 completed sont immuables. Si un
  R43 est activé entre deux SELECT, le snapshot reste entièrement celui de R42
  réellement capturé : l'état active / completed de R42 au moment de la
  capture est garanti par la lecture validée ; la relecture du run accepte
  donc completed / superseded (évolution postérieure légitime), jamais un
  autre état.

- Cohérence du run capturé : user_id, competency_code,
  longitudinal_assessment_run_id, current_stage et tension_state du run
  relu = ceux du ValidatedCompetencyState ; enfants rattachés au run ;
  exactement quatre claims ; tensions <=> tension_state open.

- state_generation : TECHNICAL ONLY (compteur du cache). Jamais un niveau,
  un nombre de progrès, une quantité de preuves, une ancienneté, une
  stabilité cognitive, une confiance, une répétition ni une maturité ;
  aucune logique de ce module ne dépend de sa valeur numérique.

- Taxonomie : release = pedagogical_taxonomy_release_id du run T5 parent
  réellement consommé par le run T6, résolue par get_release_capabilities
  (ordre naturel T4-B, calculé en Python : C2 avant C10), limitée à la
  compétence PERSISTÉE de la définition. Identité sémantique =
  definition_id, jamais capability_code seul : même code, autre definition_id
  / semantic_revision = autre capacité ; aucun remapping par code. Doublon de
  membership, de définition ou de capability_code => InvalidAdaptationState.

- Audit-only : basis_summary, scope_summary, state_decision_summary et
  tension.summary ne sont JAMAIS lus par ce module (ils ne sont même pas
  copiés dans les données acquises) : toute décision repose sur des données
  structurées.

- Claims : exactement discovery, comprehension, application, mastery, dans
  cet ordre, lues telles que persistées (statut, basis_mode,
  confidence_profile, mastery_assessment) ; seul le PÉRIMÈTRE sémantique
  utile à Step 6 est reconstruit, structurellement :
    * direct (hors Mastery) : refs positive_basis de CETTE claim, toutes des
      observations du snapshot T5 consommé, supportive, de la compétence.
      Observation localized : definition_id de ses mappings T4 (release de
      SON run T3, même compétence) qui existent aussi dans la release
      courante pour cette compétence (frontière de compatibilité de T5 /
      T6-C1) ; aucun definition_id compatible => InvalidAdaptationState.
      Observation competency_only : competency_only_basis=True, aucune
      capacité inventée (et aucun mapping toléré). Une claim directe peut
      porter les deux. represented_capability_definition_ids = union, dans
      l'ordre de la taxonomie courante ;
    * Mastery directe : périmètre CANONIQUE =
      mastery_assessment.represented_capability_definition_ids (décidé par
      T6-C1 / T6-C3), vérifié contre la taxonomie courante et contenu dans le
      périmètre compatible de ses refs positive_basis (cohérence ; jamais un
      autre périmètre inventé) ; competency_only_basis lu sur ces refs ;
    * implied_by_higher_claim : aucune ref positive_basis propre ; hérite
      EXACTEMENT du périmètre de la claim DIRECTE supérieure la plus proche
      (ordre conceptuel des stades) ; aucune source => InvalidAdaptationState ;
    * not_established (basis_mode none) : (), False, confidence_profile None.
  current_stage n'est jamais converti en périmètre : current_stage
  application avec une claim application not_established reste tel quel.
  Aucune absence n'est lue comme une faiblesse de l'utilisateur.

- confidence_profile : copie profonde immuable du profil persisté
  (schema_version + diagnosticity, coverage, independence, consistency,
  temporal_validation), sans agrégation ni lecture. Deux formats, TOUJOURS
  choisis par leur schema_version (jamais déduits de la forme) :
    * confidence-profile-v1 (historique, immuable, plus jamais écrit) :
      contrat historique inchangé (chaque dimension est un objet) ; ses
      capability_definition_ids sont AGRÉGÉS par dimension : 6-1A ne prétend
      jamais savoir quel fait porte quelle capacité et ne reconstruit
      jamais une attribution par fait ;
    * confidence-profile-v2 (courant) : validé STRICTEMENT par
      check_confidence_profile_v2 (définition unique du format, partagée
      avec l'écriture T6-C3) contre la taxonomie courante du snapshot :
      chaque fait, au plus une fois et dans l'ordre de DIMENSION_FACT_CODES,
      avec EXACTEMENT ses propres capability_definition_ids, chacun présent
      par son identité (definition_id, jamais capability_code) dans la
      release du run T5 courant, sans doublon, dans l'ordre de la taxonomie.
      Un consommateur retrouve donc sans ambiguïté, par exemple, coverage ->
      fait unobserved_capabilities_present -> SES capability_definition_ids
      (str canoniques, copiés tels quels).
  Autre schema_version => InvalidAdaptationState.

- mastery_assessment (mastery-assessment-v1) : copie immuable sur la seule
  claim mastery, ses capability_definition_ids vérifiés contre la taxonomie
  courante ; aucun x/5, aucun pourcentage.

- Tensions : get_inference_tensions(run_id) ; localized => memberships
  résolus EXACTEMENT dans la release du run T5 courant et la compétence
  (absent, autre compétence, doublon, aucun membership =>
  InvalidAdaptationState) ; whole_competency / competency_only => aucune
  capacité. Aucun scope converti en un autre.

- validation_needs : payload validation-need-v1 de
  CompetencyInferenceRun.validation_needs validé structurellement (intent
  confirmation / revalidation, target_stage, scope localized avec
  definition_id EXACTS de la release courante, competency_only /
  whole_competency sans capacité, reason_codes du vocabulaire V1). Besoins
  LATENTS : aucune question, aucune priorité, aucune action.

- unresolved_revision_context : copie défensive immuable, jamais
  interprétée.

- Lecture seule : uniquement des lectures des services propriétaires, sous
  db.no_autoflush ; jamais add / flush / commit / rollback / UPDATE /
  DELETE / INSERT, aucune migration, aucun modèle Step 6, aucun cache.

- Pureté : l'acquisition (A, _acquire) copie les lignes lues en
  enregistrements immuables ; la projection (B, _project_competency) est
  une fonction pure et déterministe de (ValidatedCompetencyState,
  enregistrements). Même état Step 5 persisté => même snapshot.

Ordres canoniques : compétences C1 -> C12 (jamais un tri lexical) ; claims
discovery -> mastery ; capacités et definition_ids : ordre de la taxonomie
courante ; tensions : (stade fragilisé, scope_mode, capacités, revision_status)
dans ces vocabulaires ; validation_needs : (intent, target_stage, scope_mode,
capacités), ordre de T6-C3 ; reason_codes : ordre du vocabulaire V1.
"""
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import NamedTuple

from core.inference_final_policies import (
    VALIDATION_INTENTS,
    VALIDATION_NEED_SCHEMA_VERSION,
    VALIDATION_REASON_CODES,
    VALIDATION_SCOPE_MODES,
    InvalidConfidenceProfilePayload,
    check_confidence_profile_v2,
)
from core.inference_positive_basis import MASTERY_ASSESSMENT_SCHEMA_VERSION, MASTERY_PROPERTIES
from core.inference_service import (
    ACTIVE,
    BASIS_MODE_NONE,
    CLAIM_STAGES,
    COMPETENCY_CODES,
    COMPETENCY_ONLY,
    COMPLETED,
    CONFIDENCE_DIMENSIONS,
    CURRENT_STAGES,
    DIRECT,
    ESTABLISHED,
    IMPLIED_BY_HIGHER_CLAIM,
    LOCALIZED,
    MASTERY,
    NOT_ESTABLISHED,
    POSITIVE_BASIS,
    REVISION_STATUSES,
    SOURCE_OBSERVATION,
    SUPERSEDED,
    SUPPORTIVE,
    TENSION_OPEN,
    TENSION_SCOPE_MODES,
    TENSION_STATES,
    InferenceServiceError,
    InvalidInferenceArgument,
    InvalidInferenceState,
    StaleInferenceChain,
    UserNotFound,
    ValidatedCompetencyState,
    get_competency_inference,
    get_inference_basis_refs,
    get_inference_tensions,
    get_stage_claims,
    get_validated_user_competency_state,
)
from core.inference_state_policies import CONFIDENCE_PROFILE_SCHEMA_V1, READABLE_CONFIDENCE_PROFILE_SCHEMA_VERSIONS
from core.longitudinal_service import LongitudinalServiceError, get_longitudinal_assessment, get_longitudinal_inputs
from core.observation_service import ObservationServiceError, get_evaluation_run, get_observation
from core.taxonomy_service import TaxonomyServiceError, get_observation_capabilities, get_release_capabilities

# Ordre conceptuel C1 -> C12 (jamais sorted(str) : C1, C10, C11...).
COMPETENCY_ORDER = tuple(f"C{i}" for i in range(1, 13))
# Statuts d'un run T6 capturé, relus après la capture : active, ou
# superseded par une activation postérieure (le snapshot reste celui capturé).
CAPTURED_INTERPRETATIONS = frozenset({ACTIVE, SUPERSEDED})

_CONFIDENCE_PROFILE_KEYS = frozenset({"schema_version", *CONFIDENCE_DIMENSIONS})
_MASTERY_ASSESSMENT_KEYS = frozenset({"schema_version", *MASTERY_PROPERTIES,
                                      "represented_capability_definition_ids", "limitations"})
_VALIDATION_NEED_KEYS = frozenset({"schema_version", "intent", "target_stage", "scope_mode",
                                   "capability_definition_ids", "reason_codes"})


class AdaptationStateError(Exception):
    """Erreur métier de 6-1A : jamais un snapshot pédagogique partiel."""


class InvalidAdaptationArgument(AdaptationStateError):
    """Argument structurellement invalide (user_id, competency_codes)."""


class AdaptationUserNotFound(AdaptationStateError):
    """Aucun utilisateur pour cet identifiant."""


class InvalidAdaptationState(AdaptationStateError):
    """État Step 5 incohérent (InvalidInferenceState, run capturé divergent,
    enfants, taxonomie, périmètre, payload hors format) : jamais réparé."""


class StaleAdaptationState(AdaptationStateError):
    """Chaîne amont devenue non courante (StaleInferenceChain) : l'état ne
    doit pas être utilisé comme état courant."""


# --------------------------------------------------------------------------
# Structures immuables
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class CapabilitySemanticRef:
    """Une capacité de la release du run T5 courant. definition_id = sens
    (identité sémantique) ; membership_id = appartenance à CETTE release ;
    capability_code / semantic_revision / label : descriptifs, jamais une
    identité ni un ordre pédagogique utilisateur."""
    membership_id: uuid.UUID
    definition_id: uuid.UUID
    capability_code: str
    semantic_revision: int
    label: str


@dataclass(frozen=True, kw_only=True)
class AdaptationStageClaim:
    """Claim Step 5 telle que persistée (status = positive_basis_status) et
    son périmètre sémantique reconstruit structurellement. Aucune absence
    n'est une faiblesse de l'utilisateur."""
    stage: str
    status: str
    basis_mode: str
    represented_capability_definition_ids: tuple
    competency_only_basis: bool
    confidence_profile: Mapping | None
    mastery_assessment: Mapping | None


@dataclass(frozen=True, kw_only=True)
class AdaptationTension:
    fragilized_stage: str
    scope_mode: str
    capability_definition_ids: tuple
    revision_status: str


@dataclass(frozen=True, kw_only=True)
class AdaptationValidationNeed:
    """Besoin LATENT décidé par Step 5 : ni question, ni priorité, ni action."""
    intent: str
    target_stage: str
    scope_mode: str
    capability_definition_ids: tuple
    reason_codes: tuple


@dataclass(frozen=True, kw_only=True)
class CompetencyAdaptationSnapshot:
    """Ce que Step 5 affirme pour UNE compétence, rattaché à UN run T6.
    state_generation : TECHNICAL ONLY (voir la docstring du module)."""
    competency_code: str
    current_stage: str
    tension_state: str
    active_inference_run_id: uuid.UUID
    longitudinal_assessment_run_id: uuid.UUID
    state_generation: int
    taxonomy_release_id: uuid.UUID
    capabilities: tuple
    claims: tuple
    tensions: tuple
    unresolved_revision_context: Mapping | None
    validation_needs: tuple


@dataclass(frozen=True, kw_only=True)
class AdaptationStateSnapshot:
    """Compétences disposant d'un état Step 5 validé, C1 -> C12. Une
    compétence absente n'a aucun T6 : elle n'est jamais non_etabli."""
    user_id: str
    competencies: tuple


# --------------------------------------------------------------------------
# Enregistrements acquis (A) : copies immuables des lignes lues, jamais un
# objet ORM, jamais un résumé d'audit.
# --------------------------------------------------------------------------

class _RunRecord(NamedTuple):
    id: uuid.UUID
    user_id: str
    competency_code: str
    longitudinal_assessment_run_id: uuid.UUID
    execution_status: str
    interpretation_status: str
    current_stage: str | None
    tension_state: str | None
    unresolved_revision_context: object
    validation_needs: object


class _ParentRecord(NamedTuple):
    id: uuid.UUID
    user_id: str
    competency_code: str
    pedagogical_taxonomy_release_id: uuid.UUID | None


class _CapabilityRecord(NamedTuple):
    """(membership, définition) de la release, dans l'ordre de T4-B."""
    membership_id: uuid.UUID
    taxonomy_release_id: uuid.UUID
    definition_id: uuid.UUID
    capability_code: str
    semantic_revision: int
    label: str
    competency_code: str


class _ClaimRecord(NamedTuple):
    id: uuid.UUID
    inference_run_id: uuid.UUID
    stage: str
    positive_basis_status: str
    basis_mode: str
    confidence_profile: object
    mastery_assessment: object


class _TensionRecord(NamedTuple):
    inference_run_id: uuid.UUID
    fragilized_stage: str
    scope_mode: str
    revision_status: str
    capability_membership_ids: tuple


class _RefRecord(NamedTuple):
    inference_run_id: uuid.UUID
    stage_claim_id: uuid.UUID | None
    ref_role: str
    source_kind: str
    source_observation_id: uuid.UUID | None


class _MappingRecord(NamedTuple):
    """Mapping T4 d'une observation : release du membership, définition."""
    taxonomy_release_id: uuid.UUID
    definition_id: uuid.UUID
    competency_code: str


class _ObservationRecord(NamedTuple):
    id: uuid.UUID
    competency_code: str
    polarity: str
    capability_localization: str
    source_taxonomy_release_id: uuid.UUID | None
    mappings: tuple


class _Acquired(NamedTuple):
    run: _RunRecord
    parent: _ParentRecord
    release_capabilities: tuple
    claims: tuple
    tensions: tuple
    refs: tuple
    snapshot_observation_ids: frozenset
    observations: Mapping


class _Taxonomy(NamedTuple):
    release_id: uuid.UUID
    capabilities: tuple
    by_membership: Mapping
    position: Mapping
    release_competencies: Mapping


# --------------------------------------------------------------------------
# Validation structurelle (pure)
# --------------------------------------------------------------------------

def _fail(message: str) -> InvalidAdaptationState:
    return InvalidAdaptationState(message)


def _choice(value, name: str, allowed) -> str:
    """Type str exact : une Enum str n'est jamais convertie."""
    if type(value) is not str or value not in allowed:
        raise _fail(f"{name} : valeur {value!r} hors vocabulaire {sorted(allowed)}")
    return value


def _frozen_json(value, path: str):
    """Copie profonde immuable d'une valeur JSON (objet -> mappingproxy,
    tableau -> tuple). Tout autre type => InvalidAdaptationState."""
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, Mapping):
        copy = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _fail(f"{path} : clé {key!r} non str")
            copy[key] = _frozen_json(item, f"{path}.{key}")
        return MappingProxyType(copy)
    if isinstance(value, (list, tuple)):
        return tuple(_frozen_json(item, f"{path}[{i}]") for i, item in enumerate(value))
    raise _fail(f"{path} : type {type(value).__name__} non JSON")


def _canonical_uuid(value, path: str) -> uuid.UUID:
    """UUID sérialisé par Step 5 : str canonique (str(uuid)), jamais deviné."""
    if type(value) is not str:
        raise _fail(f"{path} : UUID sérialisé attendu, reçu {value!r}")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise _fail(f"{path} : UUID invalide {value!r}") from exc
    if str(parsed) != value:
        raise _fail(f"{path} : UUID non canonique {value!r}")
    return parsed


def _id_list(value, path: str) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise _fail(f"{path} : tableau attendu")
    ids = tuple(_canonical_uuid(item, f"{path}[{i}]") for i, item in enumerate(value))
    if len(set(ids)) != len(ids):
        raise _fail(f"{path} : identifiant en double")
    return ids


def _ordered(definition_ids, taxonomy: _Taxonomy) -> tuple:
    """definition_ids dans l'ordre de la taxonomie courante (jamais str(uuid))."""
    return tuple(sorted(definition_ids, key=taxonomy.position.__getitem__))


def _current_definitions(definition_ids, taxonomy: _Taxonomy, path: str) -> tuple:
    """Chaque definition_id doit exister EXACTEMENT dans la release courante
    pour cette compétence : aucune correspondance par capability_code."""
    missing = [str(d) for d in definition_ids if d not in taxonomy.position]
    if missing:
        raise _fail(f"{path} : capability_definition_ids {missing} absents de la release"
                    f" {taxonomy.release_id} (aucun remapping par capability_code)")
    return _ordered(definition_ids, taxonomy)


# --------------------------------------------------------------------------
# Projection (B) : fonctions pures
# --------------------------------------------------------------------------

def _check_run(validated: ValidatedCompetencyState, run: _RunRecord) -> None:
    """Le run relu est EXACTEMENT celui de la lecture validée."""
    expected = (validated.active_inference_run_id, validated.user_id, validated.competency_code,
                validated.longitudinal_assessment_run_id, validated.current_stage, validated.tension_state)
    if (run.id, run.user_id, run.competency_code, run.longitudinal_assessment_run_id, run.current_stage,
            run.tension_state) != expected:
        raise _fail(f"run T6 {run.id} divergent de l'état validé capturé ({validated.active_inference_run_id})")
    if run.execution_status != COMPLETED or run.interpretation_status not in CAPTURED_INTERPRETATIONS:
        raise _fail(f"run T6 {run.id} est {run.execution_status} / {run.interpretation_status}"
                    f" ({COMPLETED} / {ACTIVE} capturé, éventuellement {SUPERSEDED} depuis)")
    _choice(run.current_stage, "current_stage", CURRENT_STAGES)
    _choice(run.tension_state, "tension_state", TENSION_STATES)
    if type(validated.state_generation) is not int or validated.state_generation < 1:
        raise _fail(f"state_generation {validated.state_generation!r} incohérent")


def _check_parent(validated: ValidatedCompetencyState, parent: _ParentRecord) -> None:
    if (parent.id, parent.user_id, parent.competency_code) != (
            validated.longitudinal_assessment_run_id, validated.user_id, validated.competency_code):
        raise _fail(f"run T5 {parent.id} divergent du parent capturé {validated.longitudinal_assessment_run_id}")
    if parent.pedagogical_taxonomy_release_id is None:
        raise _fail(f"run T5 {parent.id} sans release de taxonomie")


def _taxonomy(release_id: uuid.UUID, competency_code: str, rows) -> _Taxonomy:
    """Capacités de la compétence dans la release, ordre de T4-B conservé."""
    capabilities, by_membership, position, release_competencies, seen = [], {}, {}, {}, set()
    for row in rows:
        if row.taxonomy_release_id != release_id:
            raise _fail(f"membership {row.membership_id} hors de la release {release_id}")
        if row.membership_id in release_competencies:
            raise _fail(f"release {release_id} : membership {row.membership_id} en double")
        release_competencies[row.membership_id] = row.competency_code
        if row.competency_code != competency_code:
            continue
        for identity in (("definition", row.definition_id), ("capability_code", row.capability_code)):
            if identity in seen:
                raise _fail(f"release {release_id}, {competency_code} : {identity[0]} {identity[1]} en double")
            seen.add(identity)
        capability = CapabilitySemanticRef(membership_id=row.membership_id, definition_id=row.definition_id,
                                           capability_code=row.capability_code,
                                           semantic_revision=row.semantic_revision, label=row.label)
        position[row.definition_id] = len(capabilities)
        by_membership[row.membership_id] = capability
        capabilities.append(capability)
    return _Taxonomy(release_id, tuple(capabilities), MappingProxyType(by_membership), MappingProxyType(position),
                     MappingProxyType(release_competencies))


def _confidence_profile(value, stage: str, status: str, taxonomy: _Taxonomy):
    """Profil persisté copié tel quel (cinq dimensions séparées), jamais
    agrégé ni converti ; None exactement pour une claim not_established.
    Format choisi par schema_version : v1 historique (contrat inchangé, IDs
    agrégés par dimension, aucune attribution par fait), v2 courant (faits
    validés strictement contre la taxonomie courante, par identité)."""
    if status != ESTABLISHED:
        if value is not None:
            raise _fail(f"{stage} {status} avec un confidence_profile")
        return None
    if not isinstance(value, Mapping) or set(value) != _CONFIDENCE_PROFILE_KEYS:
        raise _fail(f"{stage} : confidence_profile = schema_version + {sorted(CONFIDENCE_DIMENSIONS)} attendu")
    version = value["schema_version"]
    if type(version) is not str or version not in READABLE_CONFIDENCE_PROFILE_SCHEMA_VERSIONS:
        raise _fail(f"{stage} : confidence_profile {version!r} non supporté")
    if version == CONFIDENCE_PROFILE_SCHEMA_V1:
        if not all(isinstance(value[name], Mapping) for name in CONFIDENCE_DIMENSIONS):
            raise _fail(f"{stage} : dimension de confiance non structurée")
    else:
        try:
            check_confidence_profile_v2(value, tuple(str(c.definition_id) for c in taxonomy.capabilities))
        except InvalidConfidenceProfilePayload as exc:
            raise _fail(f"{stage} : confidence_profile {version} invalide ({exc})") from exc
    return _frozen_json(value, f"{stage}.confidence_profile")


def _mastery_assessment(value, stage: str, taxonomy: _Taxonomy) -> tuple:
    """(payload immuable | None, represented_capability_definition_ids
    vérifiés et ordonnés | None). Réservé à la claim mastery."""
    if value is None:
        return None, None
    if stage != MASTERY:
        raise _fail(f"{stage} : mastery_assessment réservé à la claim {MASTERY}")
    if not isinstance(value, Mapping) or set(value) != _MASTERY_ASSESSMENT_KEYS:
        raise _fail("mastery_assessment hors format mastery-assessment-v1")
    if value["schema_version"] != MASTERY_ASSESSMENT_SCHEMA_VERSION:
        raise _fail(f"mastery_assessment {value['schema_version']!r} non supporté")
    represented = _current_definitions(
        _id_list(value["represented_capability_definition_ids"], "mastery_assessment.represented"),
        taxonomy, "mastery_assessment")
    return _frozen_json(value, "mastery.mastery_assessment"), represented


def _observation_scope(observation: _ObservationRecord, taxonomy: _Taxonomy, competency_code: str) -> tuple:
    """(definition_ids compatibles, competency_only) d'une observation de
    base positive : frontière de compatibilité de T5 / T6-C1 reproduite
    (mappings de la release de SON run T3 et de la compétence, conservés
    seulement s'ils existent dans la release courante ; jamais par code)."""
    label = f"observation {observation.id}"
    if observation.competency_code != competency_code:
        raise _fail(f"{label} de {observation.competency_code}, compétence {competency_code}")
    if observation.polarity != SUPPORTIVE:
        raise _fail(f"{label} {observation.polarity} citée en positive_basis")
    if observation.source_taxonomy_release_id is None:
        raise _fail(f"{label} : run T3 sans release, incompatible avec le dossier T5")
    if observation.capability_localization == COMPETENCY_ONLY:
        if observation.mappings or observation.source_taxonomy_release_id != taxonomy.release_id:
            raise _fail(f"{label} {COMPETENCY_ONLY} localisée ou d'une autre release")
        return frozenset(), True
    if observation.capability_localization != LOCALIZED:
        raise _fail(f"{label} : capability_localization {observation.capability_localization!r} inconnue")
    compatible = frozenset(
        m.definition_id for m in observation.mappings
        if m.taxonomy_release_id == observation.source_taxonomy_release_id
        and m.competency_code == competency_code and m.definition_id in taxonomy.position)
    if not compatible:
        raise _fail(f"{label} {LOCALIZED} sans aucune capacité compatible avec la release {taxonomy.release_id}")
    return compatible, False


def _claims(run: _RunRecord, acquired: _Acquired, taxonomy: _Taxonomy, competency_code: str) -> tuple:
    rows = {}
    for row in acquired.claims:
        if row.inference_run_id != run.id:
            raise _fail(f"claim {row.id} d'un autre run que {run.id}")
        stage = _choice(row.stage, "claim.stage", CLAIM_STAGES)
        if stage in rows:
            raise _fail(f"claim {stage} en double")
        rows[stage] = row
    if len(rows) != len(CLAIM_STAGES):
        raise _fail(f"run {run.id} : {len(rows)} claim(s), exactement quatre attendues")
    stage_of = {row.id: stage for stage, row in rows.items()}

    # Refs positive_basis : observations du snapshot T5 consommé, par claim.
    basis = {stage: [] for stage in CLAIM_STAGES}
    for ref in acquired.refs:
        if ref.inference_run_id != run.id:
            raise _fail(f"ref d'un autre run que {run.id}")
        if ref.ref_role != POSITIVE_BASIS:
            continue
        stage = stage_of.get(ref.stage_claim_id)
        if stage is None:
            raise _fail("ref positive_basis non rattachée à une claim du run")
        if ref.source_kind != SOURCE_OBSERVATION or ref.source_observation_id is None:
            raise _fail(f"ref positive_basis {stage} : source {ref.source_kind} (observation requise)")
        if ref.source_observation_id not in acquired.snapshot_observation_ids:
            raise _fail(f"observation {ref.source_observation_id} hors du snapshot T5 {run.longitudinal_assessment_run_id}")
        basis[stage].append(acquired.observations[ref.source_observation_id])

    decoded, scopes = {}, {}
    for stage in CLAIM_STAGES:
        row = rows[stage]
        status = _choice(row.positive_basis_status, f"{stage}.positive_basis_status",
                         {ESTABLISHED, NOT_ESTABLISHED})
        mode = _choice(row.basis_mode, f"{stage}.basis_mode", {DIRECT, IMPLIED_BY_HIGHER_CLAIM, BASIS_MODE_NONE})
        if (status == NOT_ESTABLISHED) != (mode == BASIS_MODE_NONE):
            raise _fail(f"{stage} : {status} / {mode} incohérents")
        if mode != DIRECT and basis[stage]:
            raise _fail(f"{stage} {mode} avec des refs positive_basis propres")
        profile = _confidence_profile(row.confidence_profile, stage, status, taxonomy)
        assessment, mastery_scope = _mastery_assessment(row.mastery_assessment, stage, taxonomy)
        decoded[stage] = (status, mode, profile, assessment)
        if mode == BASIS_MODE_NONE:
            scopes[stage] = ((), False)
        elif mode == DIRECT:
            if not basis[stage]:
                raise _fail(f"{stage} {DIRECT} sans ref positive_basis")
            readings = [_observation_scope(o, taxonomy, competency_code) for o in basis[stage]]
            compatible = frozenset(d for ids, _ in readings for d in ids)
            competency_only = any(flag for _, flag in readings)
            if stage == MASTERY:
                if mastery_scope is None:
                    raise _fail(f"{MASTERY} {DIRECT} sans mastery_assessment (périmètre canonique absent)")
                if not frozenset(mastery_scope) <= compatible:
                    raise _fail(f"{MASTERY} : represented_capability_definition_ids hors du périmètre compatible"
                                " de ses refs positive_basis")
                scopes[stage] = (mastery_scope, competency_only)
            else:
                scopes[stage] = (_ordered(compatible, taxonomy), competency_only)

    # implied_by_higher_claim : périmètre EXACT de la claim directe
    # supérieure la plus proche (ordre conceptuel, jamais un résumé).
    for index, stage in enumerate(CLAIM_STAGES):
        if decoded[stage][1] != IMPLIED_BY_HIGHER_CLAIM:
            continue
        source = next((higher for higher in CLAIM_STAGES[index + 1:] if decoded[higher][1] == DIRECT), None)
        if source is None:
            raise _fail(f"{stage} {IMPLIED_BY_HIGHER_CLAIM} sans claim directe supérieure")
        scopes[stage] = scopes[source]

    return tuple(AdaptationStageClaim(
        stage=stage,
        status=decoded[stage][0],
        basis_mode=decoded[stage][1],
        represented_capability_definition_ids=scopes[stage][0],
        competency_only_basis=scopes[stage][1],
        confidence_profile=decoded[stage][2],
        mastery_assessment=decoded[stage][3],
    ) for stage in CLAIM_STAGES)


def _tensions(run: _RunRecord, rows, taxonomy: _Taxonomy) -> tuple:
    tensions = []
    for row in rows:
        if row.inference_run_id != run.id:
            raise _fail(f"tension d'un autre run que {run.id}")
        stage = _choice(row.fragilized_stage, "tension.fragilized_stage", CLAIM_STAGES)
        mode = _choice(row.scope_mode, "tension.scope_mode", TENSION_SCOPE_MODES)
        status = _choice(row.revision_status, "tension.revision_status", REVISION_STATUSES)
        memberships = tuple(row.capability_membership_ids)
        if len(set(memberships)) != len(memberships):
            raise _fail(f"tension {stage} {mode} : membership en double")
        if mode != LOCALIZED:
            if memberships:
                raise _fail(f"tension {stage} {mode} avec {len(memberships)} membership(s)")
            definitions = ()
        else:
            if not memberships:
                raise _fail(f"tension {stage} {LOCALIZED} sans membership")
            resolved = []
            for membership_id in memberships:
                capability = taxonomy.by_membership.get(membership_id)
                if capability is None:
                    where = taxonomy.release_competencies.get(membership_id)
                    raise _fail(f"tension {stage} : membership {membership_id} "
                                + ("absent de la release" if where is None else f"de {where}")
                                + f" ({taxonomy.release_id})")
                resolved.append(capability.definition_id)
            if len(set(resolved)) != len(resolved):
                raise _fail(f"tension {stage} : capacité en double")
            definitions = _ordered(resolved, taxonomy)
        tensions.append(AdaptationTension(fragilized_stage=stage, scope_mode=mode,
                                          capability_definition_ids=definitions, revision_status=status))
    if bool(tensions) != (run.tension_state == TENSION_OPEN):
        raise _fail(f"run {run.id} : {len(tensions)} tension(s) pour tension_state {run.tension_state}")
    # Ordre canonique documenté (jamais created_at / UUID / ordre SQL).
    return tuple(sorted(tensions, key=lambda t: (
        CLAIM_STAGES.index(t.fragilized_stage), VALIDATION_SCOPE_MODES.index(t.scope_mode),
        tuple(taxonomy.position[d] for d in t.capability_definition_ids), t.revision_status)))


def _validation_needs(value, taxonomy: _Taxonomy) -> tuple:
    """validation-need-v1 validé ; besoins latents, ordre canonique T6-C3."""
    if not isinstance(value, (list, tuple)):
        raise _fail("validation_needs : tableau JSON attendu")
    needs, identities = [], set()
    for index, item in enumerate(value):
        path = f"validation_needs[{index}]"
        if not isinstance(item, Mapping) or set(item) != _VALIDATION_NEED_KEYS:
            raise _fail(f"{path} hors format {VALIDATION_NEED_SCHEMA_VERSION}")
        if item["schema_version"] != VALIDATION_NEED_SCHEMA_VERSION:
            raise _fail(f"{path} : schema_version {item['schema_version']!r} non supporté")
        intent = _choice(item["intent"], f"{path}.intent", VALIDATION_INTENTS)
        stage = _choice(item["target_stage"], f"{path}.target_stage", CLAIM_STAGES)
        mode = _choice(item["scope_mode"], f"{path}.scope_mode", VALIDATION_SCOPE_MODES)
        ids = _id_list(item["capability_definition_ids"], f"{path}.capability_definition_ids")
        if (mode == LOCALIZED) != bool(ids):
            raise _fail(f"{path} : {mode} et {len(ids)} capability_definition_id(s) incohérents")
        definitions = _current_definitions(ids, taxonomy, path)
        reasons = item["reason_codes"]
        if not isinstance(reasons, (list, tuple)) or not reasons or len(set(reasons)) != len(reasons):
            raise _fail(f"{path} : reason_codes non vides et distincts attendus")
        for reason in reasons:
            _choice(reason, f"{path}.reason_codes", VALIDATION_REASON_CODES)
        identity = (intent, stage, mode, definitions)
        if identity in identities:
            raise _fail(f"{path} : besoin {intent} {stage} {mode} en double")
        identities.add(identity)
        needs.append(AdaptationValidationNeed(
            intent=intent, target_stage=stage, scope_mode=mode, capability_definition_ids=definitions,
            reason_codes=tuple(code for code in VALIDATION_REASON_CODES if code in reasons)))
    return tuple(sorted(needs, key=lambda n: (
        VALIDATION_INTENTS.index(n.intent), CLAIM_STAGES.index(n.target_stage),
        VALIDATION_SCOPE_MODES.index(n.scope_mode), tuple(taxonomy.position[d] for d in n.capability_definition_ids))))


def _revision_context(value):
    """Copie défensive immuable, jamais interprétée."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or not value:
        raise _fail("unresolved_revision_context : objet JSON non vide attendu")
    return _frozen_json(value, "unresolved_revision_context")


def _project_competency(validated: ValidatedCompetencyState, acquired: _Acquired) -> CompetencyAdaptationSnapshot:
    """Projection PURE et déterministe : (état validé capturé, enregistrements
    acquis pour CE run) -> CompetencyAdaptationSnapshot. Aucune base."""
    run = acquired.run
    _check_run(validated, run)
    _check_parent(validated, acquired.parent)
    taxonomy = _taxonomy(acquired.parent.pedagogical_taxonomy_release_id, validated.competency_code,
                         acquired.release_capabilities)
    return CompetencyAdaptationSnapshot(
        competency_code=validated.competency_code,
        current_stage=validated.current_stage,
        tension_state=validated.tension_state,
        active_inference_run_id=validated.active_inference_run_id,
        longitudinal_assessment_run_id=validated.longitudinal_assessment_run_id,
        state_generation=validated.state_generation,
        taxonomy_release_id=taxonomy.release_id,
        capabilities=taxonomy.capabilities,
        claims=_claims(run, acquired, taxonomy, validated.competency_code),
        tensions=_tensions(run, acquired.tensions, taxonomy),
        unresolved_revision_context=_revision_context(run.unresolved_revision_context),
        validation_needs=_validation_needs(run.validation_needs, taxonomy),
    )


# --------------------------------------------------------------------------
# Acquisition (A) : lectures des services propriétaires, rattachées au run
# capturé
# --------------------------------------------------------------------------

def _validated_state(db, user_id: str, competency_code: str) -> ValidatedCompetencyState | None:
    try:
        return get_validated_user_competency_state(db, user_id=user_id, competency_code=competency_code)
    except StaleInferenceChain as exc:
        raise StaleAdaptationState(f"({user_id}, {competency_code}) : {exc}") from exc
    except UserNotFound as exc:
        raise AdaptationUserNotFound(user_id) from exc
    except InvalidInferenceArgument as exc:
        raise InvalidAdaptationArgument(str(exc)) from exc
    except (InvalidInferenceState, InferenceServiceError) as exc:
        raise InvalidAdaptationState(f"({user_id}, {competency_code}) : {exc!r}") from exc


def _observation_record(db, observation_id: uuid.UUID) -> _ObservationRecord:
    observation = get_observation(db, observation_id=observation_id)
    evaluation_run = get_evaluation_run(db, run_id=observation.evaluation_run_id)
    return _ObservationRecord(
        id=observation.id,
        competency_code=observation.competency_code,
        polarity=observation.polarity,
        capability_localization=observation.capability_localization,
        source_taxonomy_release_id=evaluation_run.pedagogical_taxonomy_release_id,
        mappings=tuple(_MappingRecord(membership.taxonomy_release_id, definition.id, definition.competency_code)
                       for _, membership, definition in get_observation_capabilities(
                           db, observation_id=observation_id)),
    )


def _acquire(db, validated: ValidatedCompetencyState) -> _Acquired:
    """Toutes les lectures EXPLICITEMENT rattachées au run capturé (jamais
    l'API « active courante »), copiées en enregistrements immuables."""
    run_id = validated.active_inference_run_id
    try:
        run = get_competency_inference(db, run_id=run_id)
        claims = get_stage_claims(db, run_id=run_id)
        tensions = get_inference_tensions(db, run_id=run_id)
        refs = get_inference_basis_refs(db, run_id=run_id)
        parent = get_longitudinal_assessment(db, run_id=validated.longitudinal_assessment_run_id)
        snapshot = frozenset(i.observation_id for i in get_longitudinal_inputs(db, run_id=parent.id))
        release = () if parent.pedagogical_taxonomy_release_id is None else tuple(
            _CapabilityRecord(m.id, m.taxonomy_release_id, d.id, d.capability_code, d.semantic_revision, d.label,
                              d.competency_code)
            for m, d in get_release_capabilities(db, release_id=parent.pedagogical_taxonomy_release_id))
        basis_ids = sorted({r.source_observation_id for r in refs
                            if r.ref_role == POSITIVE_BASIS and r.source_observation_id is not None}, key=str)
        observations = {i: _observation_record(db, i) for i in basis_ids if i in snapshot}
    except (InferenceServiceError, LongitudinalServiceError, TaxonomyServiceError, ObservationServiceError) as exc:
        raise InvalidAdaptationState(f"run T6 {run_id} : lecture impossible ({exc!r})") from exc
    return _Acquired(
        run=_RunRecord(run.id, run.user_id, run.competency_code, run.longitudinal_assessment_run_id,
                       run.execution_status, run.interpretation_status, run.current_stage, run.tension_state,
                       run.unresolved_revision_context, run.validation_needs),
        parent=_ParentRecord(parent.id, parent.user_id, parent.competency_code, parent.pedagogical_taxonomy_release_id),
        release_capabilities=release,
        claims=tuple(_ClaimRecord(c.id, c.inference_run_id, c.stage, c.positive_basis_status, c.basis_mode,
                                  c.confidence_profile, c.mastery_assessment) for c in claims),
        tensions=tuple(_TensionRecord(t.inference_run_id, t.fragilized_stage, t.scope_mode, t.revision_status,
                                      tuple(memberships)) for t, memberships in tensions),
        refs=tuple(_RefRecord(r.inference_run_id, r.stage_claim_id, r.ref_role, r.source_kind, r.source_observation_id)
                   for r in refs),
        snapshot_observation_ids=snapshot,
        observations=MappingProxyType(observations),
    )


def _requested_codes(competency_codes) -> tuple:
    if competency_codes is None:
        return COMPETENCY_ORDER
    if not isinstance(competency_codes, (list, tuple)):
        raise InvalidAdaptationArgument("competency_codes doit être un tuple de codes C1..C12 ou None")
    for code in competency_codes:
        if type(code) is not str or code not in COMPETENCY_CODES:
            raise InvalidAdaptationArgument(f"competency_codes : {code!r} hors de C1..C12")
    if len(set(competency_codes)) != len(competency_codes):
        raise InvalidAdaptationArgument("competency_codes : code en double")
    return tuple(code for code in COMPETENCY_ORDER if code in competency_codes)


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def load_adaptation_state(
    db,
    *,
    user_id: str,
    competency_codes: tuple[str, ...] | None = None,
) -> AdaptationStateSnapshot:
    """Snapshot immuable de ce que Step 5 affirme pour cet utilisateur.

    competency_codes None => toutes les compétences disposant d'un état
    Step 5 validé, C1 -> C12 ; sinon uniquement celles-ci, dans l'ordre
    conceptuel C1 -> C12 (jamais lexical ni d'entrée). 6-1A ne décide pas de
    leur pertinence (6-1B). Compétence sans état validé : absente (jamais
    non_etabli). Lecture STRICTE : StaleAdaptationState,
    InvalidAdaptationState, AdaptationUserNotFound, InvalidAdaptationArgument ;
    aucun snapshot partiel. Lecture seule, aucune transaction possédée."""
    if type(user_id) is not str or not user_id.strip() or "\x00" in user_id:
        raise InvalidAdaptationArgument("user_id doit être une chaîne non vide")
    codes = _requested_codes(competency_codes)
    competencies = []
    with db.no_autoflush:
        for competency_code in codes:
            validated = _validated_state(db, user_id, competency_code)
            if validated is None:
                continue
            competencies.append(_project_competency(validated, _acquire(db, validated)))
    return AdaptationStateSnapshot(user_id=user_id, competencies=tuple(competencies))
