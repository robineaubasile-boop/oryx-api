"""Service interne transactionnel du dossier longitudinal et des relations
entre observations (T5-B, niveau 5 : relations longitudinales).

Seul point d'écriture applicatif des huit tables de T5-A
(LongitudinalAssessmentRun, LongitudinalAssessmentInput,
ObservationDependency, DependencyCapability, ObservationTransfer,
TransferCapability, ObservationRevalidation, RevalidationCapability) : même
philosophie que core/cognitive_capture.py (T2-B),
core/observation_service.py (T3-B) et core/taxonomy_service.py (T4-B).

Doctrine : OBSERVATION = unité de preuve ; CAPABILITY = périmètre de la
preuve ; RELATION T5 = structure historique entre preuves. Une relation ne
crée JAMAIS une preuve : aucune méthode ne crée ni ne modifie
CognitiveEvent, SupportTrace, PedagogicalObservation ou
ObservationCapability, ni polarity, evidence_strength, local_stage ou
contradiction_scope. Aucune transitivité (A -> B et B -> C ne créent jamais
A -> C), aucune ligne « independent » : l'absence d'arête signifie seulement
qu'aucune dépendance n'a été identifiée dans le dossier examiné.

Ce service n'est PAS l'évaluateur longitudinal. Il ne détecte ni
dépendance, ni transfert, ni revalidation (aucune heuristique : un ticker,
une date, une société ou une surface différente ne prouvent rien) ; il
reçoit des relations déjà identifiées par un évaluateur amont et vérifie
tout ce qui est structurellement et historiquement vérifiable. Il ne calcule
ni stade, ni confiance, ni maîtrise, ni besoin de revalidation, ni profil
(T5-C / T6). Aucun appel de modèle.

Invariants :

- Transactions : jamais de commit() ni de rollback(). Chaque mutation
  valide (avant tout accès à la base), verrouille, relit l'état sous
  verrou, mute puis flush() ; l'appelant possède la transaction. Seule
  exception, locale et invisible pour l'appelant : l'INSERT d'un run est
  isolé dans un SAVEPOINT pour traduire une violation de
  uq_longitudinal_assessment_runs_dedup_key en
  DuplicateLongitudinalAssessment sans rendre inutilisable la transaction de
  l'appelant (toute autre IntegrityError est propagée).

- Lifecycle du run (execution_status / interpretation_status) :
      start     -> running   / candidate
      complete  -> completed / active     (l'ancien active -> superseded)
      fail      -> failed    / obsolete   (l'active courant est inchangé)
  Seul running / candidate reçoit des relations ou une transition ; tout
  autre état est terminal (une seconde transition est refusée, jamais un
  no-op). Inputs et relations d'un run failed / superseded restent pour
  l'audit (aucune suppression, aucune cascade).

- Snapshot : construit par le service à start (l'appelant ne choisit ni
  les observations ni input_fingerprint) : toutes les observations
  admissibles du couple (user_id, competency_code), supportive comme
  contradictory, local_stage none compris. Admissible = integrity_status
  valid, même compétence, run T3 completed / active, CognitiveEvent du même
  user, et compatible avec la release du run T5 (voir ci-dessous). Une ligne
  LongitudinalAssessmentInput par observation ; zéro observation est un
  dossier valide. Le snapshot est ensuite immuable (aucune API add/remove).

- Compatibility gate taxonomique : même capability_definition_id = même
  sens ; jamais de remapping par capability_code. Une observation localized
  n'est retenue que sur les définitions de ses mappings T4 (memberships de
  la release de SON run T3) qui appartiennent aussi à la release du run T5 ;
  aucune définition commune => exclue. Une observation competency_only n'est
  retenue que si son run T3 a été évalué sous la release même du run T5
  (aucune identité sémantique persistée d'une définition Cx ne permet de
  prouver l'équivalence entre releases). Run T3 sans release => exclue.

- input_fingerprint V1 : SHA-256 (hex minuscule) du JSON canonique
  (sort_keys, séparateurs compacts, ensure_ascii=False, UTF-8) de
  {input_schema_version: 1, user_id, competency_code,
  pedagogical_taxonomy_release_id, observations: [{observation_id,
  evaluation_run_id, event_id, source_taxonomy_release_id,
  compatible_capability_definition_ids}]}, observations triées par
  observation_id, définitions triées, UUID en str canonique. Aucun
  horodatage ni statut du run T5.

- scope_fingerprint V1 (calculé par le service, jamais fourni) : même
  canonicalisation de {scope_schema_version: 1, competency_code, scope_mode,
  capability_definition_ids} ; identifiants SÉMANTIQUES (definition_id) et
  non les memberships propres à une release. localized = définitions des
  memberships fournis ; whole_observation = scope compatible complet de la
  cible (dépendance) ou intersection compatible source / cible (transfert,
  revalidation) ; competency_only = [].

- Scopes : memberships de la release du run et de sa compétence
  uniquement ; localized => au moins un membership, sans doublon, ⊆ scope
  compatible de la cible (dépendance) ou de l'intersection (transfert,
  revalidation) ; whole_observation et competency_only => aucun membership,
  aucune ligne *_capabilities. Chevauchement (conservateur) : deux scopes
  localized se chevauchent si leurs définitions s'intersectent ; dès qu'un
  scope n'est pas localized, le chevauchement est présumé (aucune
  disjonction n'est démontrable).

- Relations (toutes inter-événements, extrémités dans le snapshot du run) :
  * dépendance : source observation (événement distinct de la cible) ou
    support_trace d'un CognitiveEvent du même user, distinct de celui de la
    cible (l'aide du même événement reste le contexte local T2/T3) ;
  * transfert : source et cible supportive, cible local_stage application
    (« au moins Application » : mastery n'est jamais un stade local) ;
    deux observations localized sans définition compatible commune ne
    forment aucun transfert, quel que soit scope_mode ;
  * revalidation (fait historique observé, pas un besoin T6) : source
    contradictory, cible supportive ; deux observations localized sans
    définition compatible commune ne revalident rien ;
  * une dépendance et un transfert (ou une revalidation) vers la même cible
    ne coexistent pas sur des scopes qui se chevauchent, quel que soit
    l'ordre d'ajout ; une absence de dépendance n'établit jamais
    l'indépendance (c'est à *_basis de l'établir) ;
  * *_basis : dict JSON strict NON VIDE (raison explicable), copié en
    profondeur ; le service ne juge pas sa justesse cognitive ;
  * doublon exact (même identité et même scope_fingerprint) refusé sous le
    verrou du run ; l'identité d'une dépendance (cible, source,
    scope_fingerprint) n'inclut pas dependency_type : un autre type pour la
    même dépendance est une classification contradictoire (refusée).
  Chronologie : le schéma ne porte pas d'horodatage atomique par
  observation ; started_at / closed_at / created_at sont des horodatages
  techniques (événements multi-tours, horloges applicatives) et ne sont
  jamais transformés en vérité cognitive. La direction source -> cible est
  une assertion explicite de l'évaluateur ; aucune règle temporelle n'est
  imposée ici.

- Activation (complete) : le dossier recalculé sous verrou doit être
  exactement celui du candidat (lignes d'input ET input_fingerprint), sous
  une release toujours active, et chaque relation persistée est revalidée
  (défense contre une écriture directe ayant contourné le service). Sinon
  erreur, sans aucune mutation : un snapshot périmé n'est jamais réécrit ni
  activé (l'orchestrateur peut ensuite appeler fail avec
  failure_code="stale_input").

- Verrous et ordre global (anti-deadlock) :
      complete : pg_advisory_xact_lock(clé(user_id, competency_code))
                 -> run candidat (FOR NO KEY UPDATE)
                 -> release du run (FOR SHARE)
                 -> run active courant (FOR NO KEY UPDATE)
      add_* / fail : run seul (FOR NO KEY UPDATE)
      start : aucun verrou de ligne (UNIQUE de déduplication en défense
              finale ; la release est revérifiée par complete)
  La clé consultative est dérivée de SHA-256(JSON canonique de
  ["oryx-t5", user_id, competency_code]), 8 premiers octets, entier signé
  (jamais hash() Python) : elle sérialise toutes les activations d'un même
  couple, qui n'a pas de ligne parent. user_id et competency_code sont
  immuables : ils sont lus sans verrou avant la clé consultative.
  Runs verrouillés en FOR NO KEY UPDATE (comme T3-B / T4-B) : les INSERT
  d'inputs et de relations prennent un FOR KEY SHARE de contrôle de FK sur
  le run, compatible avec ce mode et non avec FOR UPDATE ; FOR NO KEY
  UPDATE reste exclusif entre les opérations du service. La release est
  prise en FOR SHARE : conflit avec le FOR NO KEY UPDATE que
  taxonomy_service prend pour la retirer (activation d'une autre release),
  compatible avec les FOR KEY SHARE des INSERT T3 / T5 et avec les autres
  activations T5 sous la même release. Une activation T4 en cours => la
  complétion T5 attend puis voit la release retired ; une complétion T5 en
  cours => le retrait attend son COMMIT. Aucun verrou T2 / T3 n'est pris
  (les lignes T2 / T3 / T4 sont lues, jamais verrouillées) : aucune famille
  de verrous n'est attendue en détenant l'autre, donc aucun cycle avec
  T2-B / T3-B / T4-B. Les INSERT T5 prennent seulement des FOR KEY SHARE
  (FK) sur observations, aides, memberships, release et user, compatibles
  avec les FOR NO KEY UPDATE de T3-B / T4-B. Chaque verrou utilise
  populate_existing. Hypothèse : READ COMMITTED (défaut PostgreSQL et
  SessionLocal).
  Un dossier activé reflète l'état commité au moment de la complétion ;
  une observation T3 ultérieure le rend simplement périmé (un nouveau run
  T5 le remplacera), jamais réécrit.

- Payloads JSON : validés strictement (types JSON exacts, flottants finis,
  clés str, pas de cycle, pas de NUL) puis copiés en profondeur ; logique
  volontairement dupliquée de T2-B / T3-B / T4-B (frontières nettes, aucune
  dépendance vers les autres services).

Les garanties sont celles de ce service (aucun trigger en base) ; les
contraintes de 0008_longitudinal_relations (CHECK, UNIQUE, index unique
partiel « un seul active par user / compétence », FK) restent la défense
finale.
"""
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

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
    PedagogicalTaxonomyRelease,
    RevalidationCapability,
    SupportTrace,
    TransferCapability,
    User,
)

# LongitudinalAssessmentRun.execution_status (et vocabulaire T3 identique).
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"

# LongitudinalAssessmentRun.interpretation_status (et vocabulaire T3 identique).
CANDIDATE = "candidate"
ACTIVE = "active"
SUPERSEDED = "superseded"
OBSOLETE = "obsolete"

# PedagogicalTaxonomyRelease.status utilisable par un run T5.
RELEASE_ACTIVE = "active"

# PedagogicalObservation (vocabulaire T3, lu seulement).
VALID = "valid"
SUPPORTIVE = "supportive"
CONTRADICTORY = "contradictory"
# Stade local minimal de la cible d'un transfert (mastery n'est jamais local).
APPLICATION = "application"

# capability_localization (T3) et scope_mode (T5).
LOCALIZED = "localized"
COMPETENCY_ONLY = "competency_only"
WHOLE_OBSERVATION = "whole_observation"

# Vocabulaires fermés : identiques aux CHECK de 0008_longitudinal_relations.
COMPETENCY_CODES = frozenset(f"C{i}" for i in range(1, 13))
SOURCE_OBSERVATION = "observation"
SOURCE_SUPPORT_TRACE = "support_trace"
SOURCE_KINDS = frozenset({SOURCE_OBSERVATION, SOURCE_SUPPORT_TRACE})
DEPENDENCY_TYPES = frozenset({"dependent", "partially_dependent"})
SCOPE_MODES = frozenset({WHOLE_OBSERVATION, LOCALIZED, COMPETENCY_ONLY})

# Versions des formats canoniques possédés par T5-B.
INPUT_SCHEMA_VERSION = 1
SCOPE_SCHEMA_VERSION = 1
# Espace de noms de la clé consultative d'activation (voir _activation_lock_key).
ACTIVATION_LOCK_NAMESPACE = "oryx-t5"

DEDUP_CONSTRAINT = "uq_longitudinal_assessment_runs_dedup_key"


class LongitudinalServiceError(Exception):
    """Erreur métier du service longitudinal."""


class UserNotFound(LongitudinalServiceError):
    """Aucun utilisateur pour cet identifiant."""


class LongitudinalAssessmentNotFound(LongitudinalServiceError):
    """Aucun LongitudinalAssessmentRun pour cet identifiant."""


class InvalidLongitudinalPayload(LongitudinalServiceError):
    """Entrée structurelle invalide : identifiant, chaîne vide, valeur hors
    vocabulaire, basis non JSON-compatible ou vide."""


class InvalidLongitudinalState(LongitudinalServiceError):
    """État incompatible avec l'opération, lu sous verrou : run qui n'est
    plus running / candidate, active courant incohérent ou multiple."""


class DuplicateLongitudinalAssessment(LongitudinalServiceError):
    """assessment_dedup_key déjà utilisée : jamais de get_or_create."""


class TaxonomyReleaseNotUsable(LongitudinalServiceError):
    """Release inconnue ou qui n'est pas (ou plus) la release active."""


class StaleLongitudinalSnapshot(LongitudinalServiceError):
    """Le dossier admissible courant n'est plus celui du candidat (lignes
    d'input ou input_fingerprint) : le candidat ne peut pas devenir active."""


class ObservationNotEligible(LongitudinalServiceError):
    """L'observation existe mais ne fait pas partie du snapshot du run."""


class RelationSourceNotFound(LongitudinalServiceError):
    """Extrémité de relation introuvable (observation ou support_trace)."""


class InvalidDependency(LongitudinalServiceError):
    """Dépendance incohérente : source XOR, auto-dépendance, même événement,
    aide d'un autre user, conflit avec un transfert ou une revalidation,
    classification contradictoire (même dépendance, autre type)."""


class InvalidTransfer(LongitudinalServiceError):
    """Transfert incohérent : polarité, cible non application, même
    événement, deux observations localized sans raisonnement commun, conflit
    avec une dépendance."""


class InvalidRevalidation(LongitudinalServiceError):
    """Revalidation incohérente : polarités, même événement, aucun mécanisme
    commun, conflit avec une dépendance de la cible."""


class InvalidRelationScope(LongitudinalServiceError):
    """Périmètre incohérent : memberships absents / en trop / en double,
    d'une autre release ou compétence, hors du scope compatible, ou
    scope_fingerprint persisté différent du recalcul."""


class DuplicateLongitudinalRelation(LongitudinalServiceError):
    """Relation exactement identique déjà présente dans le run."""


class _Evidence(NamedTuple):
    """Ce que T5-B lit d'une observation T3 (jamais modifiée).
    compatible = capability_definition_id compatibles avec la release du
    run T5 (vide pour competency_only)."""
    observation_id: uuid.UUID
    evaluation_run_id: uuid.UUID
    event_id: uuid.UUID
    source_release_id: uuid.UUID | None
    localization: str
    polarity: str
    local_stage: str | None
    compatible: frozenset


class _Scope(NamedTuple):
    mode: str
    definitions: frozenset


class _Relation(NamedTuple):
    """Relation persistée, réduite à ce qu'exigent les contrôles de doublon
    et de chevauchement. key = identité sémantique de la relation ;
    qualifier = sa qualification (dependency_type pour une dépendance, qui
    n'appartient PAS à l'identité : une même dépendance n'a qu'un type)."""
    target: uuid.UUID
    scope: _Scope
    key: tuple
    qualifier: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _require_text(value, name: str) -> str:
    """str non vide après strip, sans NUL (refusé par PostgreSQL). La valeur
    est conservée telle quelle (aucune normalisation)."""
    if type(value) is not str or not value.strip():
        raise InvalidLongitudinalPayload(f"{name} doit être une chaîne non vide")
    if "\x00" in value:
        raise InvalidLongitudinalPayload(f"{name} : caractère NUL refusé")
    return value


def _optional_text(value, name: str) -> str | None:
    return None if value is None else _require_text(value, name)


def _require_uuid(value, name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise InvalidLongitudinalPayload(f"{name} doit être un uuid.UUID")
    return value


def _optional_uuid(value, name: str) -> uuid.UUID | None:
    return None if value is None else _require_uuid(value, name)


def _require_choice(value, name: str, allowed: frozenset) -> str:
    """Type str exact : une Enum str dont la valeur serait dans le
    vocabulaire est refusée, jamais convertie."""
    if type(value) is not str or value not in allowed:
        raise InvalidLongitudinalPayload(f"{name} : valeur {value!r} hors vocabulaire {sorted(allowed)}")
    return value


def _json_copy(value, path: str, active: frozenset):
    """Copie profonde d'une valeur JSON-compatible, ou
    InvalidLongitudinalPayload. Types exacts pour les scalaires : un objet
    qui ne serait JSON que par conversion (clé non str, Enum, Decimal,
    datetime, tuple, set, NaN...) est refusé plutôt que transformé.
    PostgreSQL refuse \\u0000 dans un JSONB : refusé ici plutôt qu'au
    flush."""
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InvalidLongitudinalPayload(f"{path} : nombre non fini refusé")
        return value
    if type(value) is str:
        if "\x00" in value:
            raise InvalidLongitudinalPayload(f"{path} : caractère NUL refusé")
        return value
    if isinstance(value, (dict, list)):
        if id(value) in active:
            raise InvalidLongitudinalPayload(f"{path} : référence circulaire")
        active = active | {id(value)}
        if isinstance(value, list):
            return [_json_copy(item, f"{path}[{i}]", active) for i, item in enumerate(value)]
        copy = {}
        for key, item in value.items():
            if type(key) is not str or "\x00" in key:
                raise InvalidLongitudinalPayload(f"{path} : clé {key!r} refusée (str sans NUL attendue)")
            copy[key] = _json_copy(item, f"{path}.{key}", active)
        return copy
    raise InvalidLongitudinalPayload(f"{path} : type {type(value).__name__} non JSON-compatible")


def _require_basis(value, name: str) -> dict:
    """dict JSON strict et NON VIDE (une relation a toujours une raison
    explicable), copié en profondeur. Sa justesse cognitive appartient à
    l'évaluateur amont."""
    if not isinstance(value, dict):
        raise InvalidLongitudinalPayload(f"{name} doit être un dict")
    if not value:
        raise InvalidLongitudinalPayload(f"{name} : dict non vide requis (raison de la relation)")
    return _json_copy(value, name, frozenset())


def _scope_arguments(scope_mode, capability_membership_ids) -> tuple:
    """Structure du périmètre, avant tout accès à la base : liste (ou tuple)
    explicite de uuid.UUID sans doublon ; localized => au moins un,
    whole_observation / competency_only => aucun."""
    _require_choice(scope_mode, "scope_mode", SCOPE_MODES)
    if not isinstance(capability_membership_ids, (list, tuple)):
        raise InvalidLongitudinalPayload("capability_membership_ids doit être une liste de uuid.UUID")
    for membership_id in capability_membership_ids:
        _require_uuid(membership_id, "capability_membership_ids")
    ids = tuple(capability_membership_ids)
    if len(set(ids)) != len(ids):
        raise InvalidRelationScope("capability_membership_ids : doublon")
    if scope_mode == LOCALIZED and not ids:
        raise InvalidRelationScope(f"{LOCALIZED} : au moins un capability_membership_id requis")
    if scope_mode != LOCALIZED and ids:
        raise InvalidRelationScope(f"{scope_mode} : aucun capability_membership_id accepté")
    return ids


def _check_dependency_source(source_kind, source_observation_id, source_support_trace_id, target_observation_id):
    """Exactement une source, cohérente avec source_kind ; jamais la cible
    elle-même."""
    if source_kind == SOURCE_OBSERVATION:
        if source_observation_id is None or source_support_trace_id is not None:
            raise InvalidDependency("source_kind observation : source_observation_id seul requis")
        if source_observation_id == target_observation_id:
            raise InvalidDependency("une observation ne dépend jamais d'elle-même")
    elif source_support_trace_id is None or source_observation_id is not None:
        raise InvalidDependency("source_kind support_trace : source_support_trace_id seul requis")


def _is_dedup_violation(exc: IntegrityError) -> bool:
    """Vrai seulement pour une violation de la contrainte UNIQUE
    d'assessment_dedup_key (nom de contrainte rapporté par PostgreSQL)."""
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == DEDUP_CONSTRAINT


# --------------------------------------------------------------------------
# Formats canoniques (V1)
# --------------------------------------------------------------------------

def _canonical_sha256(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sorted_ids(ids) -> list:
    return sorted(str(value) for value in ids)


def _input_fingerprint(*, user_id: str, competency_code: str, release_id: uuid.UUID, evidence) -> str:
    """Identifie exactement la VERSION du dossier : quelles observations,
    de quels runs / événements, sous quelle release source, et sur quel
    scope compatible avec la release du run. Indépendant de l'ordre SQL."""
    observations = sorted((
        {
            "observation_id": str(item.observation_id),
            "evaluation_run_id": str(item.evaluation_run_id),
            "event_id": str(item.event_id),
            "source_taxonomy_release_id": str(item.source_release_id),
            "compatible_capability_definition_ids": _sorted_ids(item.compatible),
        }
        for item in evidence
    ), key=lambda observation: observation["observation_id"])
    return _canonical_sha256({
        "input_schema_version": INPUT_SCHEMA_VERSION,
        "user_id": user_id,
        "competency_code": competency_code,
        "pedagogical_taxonomy_release_id": str(release_id),
        "observations": observations,
    })


def _scope_fingerprint(competency_code: str, scope: _Scope) -> str:
    return _canonical_sha256({
        "scope_schema_version": SCOPE_SCHEMA_VERSION,
        "competency_code": competency_code,
        "scope_mode": scope.mode,
        "capability_definition_ids": _sorted_ids(scope.definitions),
    })


def _activation_lock_key(user_id: str, competency_code: str) -> int:
    """Clé bigint déterministe entre processus (jamais hash() Python)."""
    encoded = json.dumps([ACTIVATION_LOCK_NAMESPACE, user_id, competency_code], separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big", signed=True)


def _overlaps(first: _Scope, second: _Scope) -> bool:
    """Chevauchement conservateur : seule une disjonction de deux scopes
    localized est démontrable ; tout autre cas est présumé chevauchant."""
    if first.mode == LOCALIZED and second.mode == LOCALIZED:
        return bool(first.definitions & second.definitions)
    return True


# --------------------------------------------------------------------------
# Verrouillage
# --------------------------------------------------------------------------

def _lock_run(db, run_id: uuid.UUID) -> LongitudinalAssessmentRun:
    """SELECT ... FOR NO KEY UPDATE sur le run (voir la docstring du module
    pour le choix de ce mode). L'état n'est lu qu'une fois le verrou obtenu."""
    run = db.execute(
        select(LongitudinalAssessmentRun)
        .where(LongitudinalAssessmentRun.id == run_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if run is None:
        raise LongitudinalAssessmentNotFound(str(run_id))
    return run


def _lock_open_run(db, run_id: uuid.UUID) -> LongitudinalAssessmentRun:
    """Verrouille le run puis exige running / candidate."""
    run = _lock_run(db, run_id)
    if run.execution_status != RUNNING or run.interpretation_status != CANDIDATE:
        raise InvalidLongitudinalState(
            f"{run_id} est {run.execution_status} / {run.interpretation_status}"
            f" ({RUNNING} / {CANDIDATE} requis)")
    return run


def _lock_activation(db, user_id: str, competency_code: str) -> None:
    """pg_advisory_xact_lock : attend toute activation en cours du même
    (user_id, competency_code) ; libéré automatiquement à la fin de la
    transaction de l'appelant (jamais un verrou de session)."""
    db.execute(select(func.pg_advisory_xact_lock(_activation_lock_key(user_id, competency_code))))


def _share_release(db, release_id: uuid.UUID) -> PedagogicalTaxonomyRelease | None:
    """SELECT ... FOR SHARE : empêche le retrait de la release (FOR NO KEY
    UPDATE de taxonomy_service) jusqu'à la fin de la transaction."""
    return db.execute(
        select(PedagogicalTaxonomyRelease)
        .where(PedagogicalTaxonomyRelease.id == release_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


# --------------------------------------------------------------------------
# Lecture des preuves et du snapshot (aucun verrou T2 / T3 / T4)
# --------------------------------------------------------------------------

def _load_evidence(db, release_id: uuid.UUID, *conditions) -> dict:
    """{observation_id: _Evidence} des observations filtrées par
    `conditions`, avec leur scope compatible avec `release_id` : définitions
    de leurs mappings T4 (memberships de la release de LEUR run T3, même
    compétence) qui appartiennent aussi à `release_id`. Jamais de
    correspondance par capability_code."""
    rows = db.execute(
        select(PedagogicalObservation.id, PedagogicalObservation.evaluation_run_id,
               ObservationEvaluationRun.event_id, ObservationEvaluationRun.pedagogical_taxonomy_release_id,
               PedagogicalObservation.competency_code, PedagogicalObservation.capability_localization,
               PedagogicalObservation.polarity, PedagogicalObservation.local_stage)
        .join(ObservationEvaluationRun, ObservationEvaluationRun.id == PedagogicalObservation.evaluation_run_id)
        .join(CognitiveEvent, CognitiveEvent.id == ObservationEvaluationRun.event_id)
        .where(*conditions)
    ).all()
    if not rows:
        return {}
    target_definitions = set(db.execute(
        select(CapabilityTaxonomyMembership.capability_definition_id)
        .where(CapabilityTaxonomyMembership.taxonomy_release_id == release_id)
    ).scalars())
    located = {}
    for observation_id, mapped_release_id, definition_id, definition_competency in db.execute(
        select(ObservationCapability.observation_id, CapabilityTaxonomyMembership.taxonomy_release_id,
               CoreCapabilityDefinition.id, CoreCapabilityDefinition.competency_code)
        .join(CapabilityTaxonomyMembership,
              CapabilityTaxonomyMembership.id == ObservationCapability.capability_membership_id)
        .join(CoreCapabilityDefinition,
              CoreCapabilityDefinition.id == CapabilityTaxonomyMembership.capability_definition_id)
        .where(ObservationCapability.observation_id.in_([row[0] for row in rows]))
    ).all():
        located.setdefault(observation_id, []).append((mapped_release_id, definition_id, definition_competency))

    evidence = {}
    for (observation_id, evaluation_run_id, event_id, source_release_id, competency_code, localization,
         polarity, local_stage) in rows:
        compatible = frozenset()
        if source_release_id is not None and localization == LOCALIZED:
            compatible = frozenset(
                definition_id for mapped_release_id, definition_id, definition_competency
                in located.get(observation_id, ())
                if mapped_release_id == source_release_id and definition_competency == competency_code
                and definition_id in target_definitions)
        evidence[observation_id] = _Evidence(observation_id, evaluation_run_id, event_id, source_release_id,
                                             localization, polarity, local_stage, compatible)
    return evidence


def _is_compatible(item: _Evidence, release_id: uuid.UUID) -> bool:
    """Compatibility gate : run T3 sans release => non ; localized => au
    moins une définition compatible ; competency_only => même release
    seulement (aucune équivalence de définition Cx n'est prouvable)."""
    if item.source_release_id is None:
        return False
    if item.localization == LOCALIZED:
        return bool(item.compatible)
    return item.source_release_id == release_id


def _current_snapshot(db, *, user_id: str, competency_code: str, release_id: uuid.UUID) -> list:
    """Observations admissibles courantes, triées par observation_id :
    valid, même compétence, run T3 completed / active, événement du même
    user, compatibles avec la release. supportive, contradictory et
    local_stage none sont TOUS conservés."""
    evidence = _load_evidence(
        db, release_id,
        PedagogicalObservation.integrity_status == VALID,
        PedagogicalObservation.competency_code == competency_code,
        ObservationEvaluationRun.execution_status == COMPLETED,
        ObservationEvaluationRun.interpretation_status == ACTIVE,
        CognitiveEvent.user_id == user_id,
    )
    return sorted((item for item in evidence.values() if _is_compatible(item, release_id)),
                  key=lambda item: str(item.observation_id))


def _run_evidence(db, run: LongitudinalAssessmentRun) -> dict:
    """Preuves du snapshot persisté du run (sous le verrou du run)."""
    return _load_evidence(
        db, run.pedagogical_taxonomy_release_id,
        PedagogicalObservation.id.in_(
            select(LongitudinalAssessmentInput.observation_id)
            .where(LongitudinalAssessmentInput.run_id == run.id)),
    )


def _snapshot_member(db, evidence: dict, observation_id: uuid.UUID, name: str) -> _Evidence:
    item = evidence.get(observation_id)
    if item is not None:
        return item
    exists = db.execute(select(PedagogicalObservation.id).where(PedagogicalObservation.id == observation_id)).first()
    if exists is None:
        raise RelationSourceNotFound(f"{name} {observation_id} introuvable")
    raise ObservationNotEligible(f"{name} {observation_id} absente du snapshot du run")


# --------------------------------------------------------------------------
# Scopes
# --------------------------------------------------------------------------

def _membership_links(db, membership_ids: tuple) -> list:
    """[(membership_id, release_id, definition_id, competency_code)] des
    memberships fournis, dans l'ordre fourni ; membership inconnu =>
    InvalidRelationScope."""
    if not membership_ids:
        return []
    rows = {row[0]: tuple(row) for row in db.execute(
        select(CapabilityTaxonomyMembership.id, CapabilityTaxonomyMembership.taxonomy_release_id,
               CoreCapabilityDefinition.id, CoreCapabilityDefinition.competency_code)
        .join(CoreCapabilityDefinition,
              CoreCapabilityDefinition.id == CapabilityTaxonomyMembership.capability_definition_id)
        .where(CapabilityTaxonomyMembership.id.in_(membership_ids))
    ).all()}
    missing = [str(m) for m in membership_ids if m not in rows]
    if missing:
        raise InvalidRelationScope(f"capability_membership_ids inconnus : {missing}")
    return [rows[m] for m in membership_ids]


def _stored_links(db, link_column, membership_column, relation_ids: list) -> dict:
    """{relation_id: [(membership_id, release_id, definition_id,
    competency_code)]} des lignes *_capabilities persistées."""
    if not relation_ids:
        return {}
    links = {}
    for row in db.execute(
        select(link_column, CapabilityTaxonomyMembership.id, CapabilityTaxonomyMembership.taxonomy_release_id,
               CoreCapabilityDefinition.id, CoreCapabilityDefinition.competency_code)
        .join(CapabilityTaxonomyMembership, CapabilityTaxonomyMembership.id == membership_column)
        .join(CoreCapabilityDefinition,
              CoreCapabilityDefinition.id == CapabilityTaxonomyMembership.capability_definition_id)
        .where(link_column.in_(relation_ids))
    ).all():
        links.setdefault(row[0], []).append(tuple(row[1:]))
    return links


def _resolve_scope(run: LongitudinalAssessmentRun, scope_mode: str, links: list, available: frozenset) -> _Scope:
    """Périmètre sémantique d'une relation. `available` = scope compatible
    de la cible (dépendance) ou intersection source / cible (transfert,
    revalidation). Memberships de la release et de la compétence du run
    uniquement ; localized ⊆ available ; jamais de capacité inventée pour
    competency_only."""
    if scope_mode == LOCALIZED and not links:
        raise InvalidRelationScope(f"{LOCALIZED} sans membership")
    if scope_mode != LOCALIZED and links:
        raise InvalidRelationScope(f"{scope_mode} avec {len(links)} membership(s)")
    definitions = set()
    for membership_id, release_id, definition_id, competency_code in links:
        if release_id != run.pedagogical_taxonomy_release_id:
            raise InvalidRelationScope(
                f"membership {membership_id} de la release {release_id}, run sous"
                f" {run.pedagogical_taxonomy_release_id}")
        if competency_code != run.competency_code:
            raise InvalidRelationScope(f"membership {membership_id} hors de la compétence {run.competency_code}")
        definitions.add(definition_id)
    if scope_mode == LOCALIZED:
        outside = definitions - available
        if outside:
            raise InvalidRelationScope(f"définitions {_sorted_ids(outside)} hors du scope compatible")
        return _Scope(LOCALIZED, frozenset(definitions))
    if scope_mode == WHOLE_OBSERVATION:
        return _Scope(WHOLE_OBSERVATION, available)
    return _Scope(COMPETENCY_ONLY, frozenset())


def _conflicts(target_id: uuid.UUID, scope: _Scope, relations) -> bool:
    return any(r.target == target_id and _overlaps(r.scope, scope) for r in relations)


# --------------------------------------------------------------------------
# Règles des relations (partagées par add_* et complete)
# --------------------------------------------------------------------------

def _dependency_target(db, run, evidence, target_id, source_kind, source_observation_id,
                       source_support_trace_id) -> _Evidence:
    """Cible et source dans le snapshot, sur des CognitiveEvent distincts ;
    une aide doit appartenir au même user et à un autre événement."""
    target = _snapshot_member(db, evidence, target_id, "target_observation_id")
    if source_kind == SOURCE_OBSERVATION:
        source = _snapshot_member(db, evidence, source_observation_id, "source_observation_id")
        if source.event_id == target.event_id:
            raise InvalidDependency("source et cible du même CognitiveEvent : contexte local T2/T3, pas T5")
        return target
    trace = db.execute(
        select(SupportTrace.cognitive_event_id, CognitiveEvent.user_id)
        .join(CognitiveEvent, CognitiveEvent.id == SupportTrace.cognitive_event_id)
        .where(SupportTrace.id == source_support_trace_id)
    ).one_or_none()
    if trace is None:
        raise RelationSourceNotFound(f"source_support_trace_id {source_support_trace_id} introuvable")
    if trace.user_id != run.user_id:
        raise InvalidDependency(f"support_trace {source_support_trace_id} d'un autre utilisateur")
    if trace.cognitive_event_id == target.event_id:
        raise InvalidDependency("aide du même CognitiveEvent que la cible : contexte local T2/T3, pas T5")
    return target


def _transfer_available(db, evidence, source_id, target_id) -> frozenset:
    """Source et cible supportive d'événements distincts, cible
    application ; retourne l'intersection de leurs scopes compatibles."""
    if source_id == target_id:
        raise InvalidTransfer("source et cible identiques")
    source = _snapshot_member(db, evidence, source_id, "source_observation_id")
    target = _snapshot_member(db, evidence, target_id, "target_observation_id")
    if source.event_id == target.event_id:
        raise InvalidTransfer("source et cible du même CognitiveEvent")
    if source.polarity != SUPPORTIVE:
        raise InvalidTransfer(f"source {source.polarity} ({SUPPORTIVE} requis)")
    if target.polarity != SUPPORTIVE:
        raise InvalidTransfer(f"cible {target.polarity} ({SUPPORTIVE} requis)")
    if target.local_stage != APPLICATION:
        raise InvalidTransfer(f"cible local_stage {target.local_stage} ({APPLICATION} requis)")
    common = source.compatible & target.compatible
    # Avant toute résolution de scope_mode : ni whole_observation ni
    # competency_only ne contournent l'absence de raisonnement commun.
    if source.localization == LOCALIZED and target.localization == LOCALIZED and not common:
        raise InvalidTransfer("aucune définition de capacité compatible commune : aucun raisonnement commun")
    return common


def _revalidation_available(db, evidence, source_id, target_id) -> frozenset:
    """Source contradictory, cible supportive, événements distincts, même
    mécanisme au moins possible ; retourne l'intersection compatible."""
    if source_id == target_id:
        raise InvalidRevalidation("source et cible identiques")
    source = _snapshot_member(db, evidence, source_id, "source_contradiction_observation_id")
    target = _snapshot_member(db, evidence, target_id, "target_supportive_observation_id")
    if source.event_id == target.event_id:
        raise InvalidRevalidation("source et cible du même CognitiveEvent : jamais une revalidation")
    if source.polarity != CONTRADICTORY:
        raise InvalidRevalidation(f"source {source.polarity} ({CONTRADICTORY} requis)")
    if target.polarity != SUPPORTIVE:
        raise InvalidRevalidation(f"cible {target.polarity} ({SUPPORTIVE} requis)")
    common = source.compatible & target.compatible
    if source.localization == LOCALIZED and target.localization == LOCALIZED and not common:
        raise InvalidRevalidation("aucune définition de capacité compatible commune : aucun mécanisme commun")
    return common


def _relations(db, run, evidence=None) -> tuple:
    """(dépendances, transferts, revalidations) persistées du run, en
    _Relation. evidence=None (add_*) : scope lu tel que persisté.
    evidence=snapshot (complete) : CHAQUE relation est revalidée
    intégralement (vocabulaires, basis, extrémités, scope, fingerprint)."""
    verify = evidence is not None
    dependencies = db.execute(
        select(ObservationDependency).where(ObservationDependency.run_id == run.id)
        .order_by(ObservationDependency.created_at, ObservationDependency.id)
    ).scalars().all()
    transfers = db.execute(
        select(ObservationTransfer).where(ObservationTransfer.run_id == run.id)
        .order_by(ObservationTransfer.created_at, ObservationTransfer.id)
    ).scalars().all()
    revalidations = db.execute(
        select(ObservationRevalidation).where(ObservationRevalidation.run_id == run.id)
        .order_by(ObservationRevalidation.created_at, ObservationRevalidation.id)
    ).scalars().all()
    dependency_links = _stored_links(db, DependencyCapability.dependency_id,
                                     DependencyCapability.capability_membership_id, [r.id for r in dependencies])
    transfer_links = _stored_links(db, TransferCapability.transfer_id,
                                   TransferCapability.capability_membership_id, [r.id for r in transfers])
    revalidation_links = _stored_links(db, RevalidationCapability.revalidation_id,
                                       RevalidationCapability.capability_membership_id,
                                       [r.id for r in revalidations])

    def stored(row, links):
        return _Scope(row.scope_mode, frozenset(link[2] for link in links))

    def verified(row, links, available, basis, basis_name, error):
        try:
            _require_choice(row.scope_mode, "scope_mode", SCOPE_MODES)
            _require_basis(basis, basis_name)
        except InvalidLongitudinalPayload as exc:
            raise error(f"relation {row.id} : {exc}") from exc
        scope = _resolve_scope(run, row.scope_mode, links, available)
        if _scope_fingerprint(run.competency_code, scope) != row.scope_fingerprint:
            raise InvalidRelationScope(f"relation {row.id} : scope_fingerprint différent du recalcul")
        return scope

    dependency_relations = []
    for row in dependencies:
        links = dependency_links.get(row.id, [])
        if verify:
            try:
                _require_choice(row.source_kind, "source_kind", SOURCE_KINDS)
                _require_choice(row.dependency_type, "dependency_type", DEPENDENCY_TYPES)
            except InvalidLongitudinalPayload as exc:
                raise InvalidDependency(f"relation {row.id} : {exc}") from exc
            _check_dependency_source(row.source_kind, row.source_observation_id, row.source_support_trace_id,
                                     row.target_observation_id)
            target = _dependency_target(db, run, evidence, row.target_observation_id, row.source_kind,
                                        row.source_observation_id, row.source_support_trace_id)
            scope = verified(row, links, target.compatible, row.dependency_basis, "dependency_basis",
                             InvalidDependency)
        else:
            scope = stored(row, links)
        dependency_relations.append(_Relation(
            row.target_observation_id, scope,
            _dependency_key(row.target_observation_id, row.source_kind, row.source_observation_id,
                            row.source_support_trace_id, row.scope_fingerprint),
            row.dependency_type))

    transfer_relations = []
    for row in transfers:
        links = transfer_links.get(row.id, [])
        if verify:
            available = _transfer_available(db, evidence, row.source_observation_id, row.target_observation_id)
            scope = verified(row, links, available, row.transfer_basis, "transfer_basis", InvalidTransfer)
        else:
            scope = stored(row, links)
        transfer_relations.append(_Relation(
            row.target_observation_id, scope,
            (row.source_observation_id, row.target_observation_id, row.scope_fingerprint)))

    revalidation_relations = []
    for row in revalidations:
        links = revalidation_links.get(row.id, [])
        if verify:
            available = _revalidation_available(db, evidence, row.source_contradiction_observation_id,
                                                row.target_supportive_observation_id)
            scope = verified(row, links, available, row.revalidation_basis, "revalidation_basis",
                             InvalidRevalidation)
        else:
            scope = stored(row, links)
        revalidation_relations.append(_Relation(
            row.target_supportive_observation_id, scope,
            (row.source_contradiction_observation_id, row.target_supportive_observation_id,
             row.scope_fingerprint)))

    return dependency_relations, transfer_relations, revalidation_relations


def _dependency_key(target_id, source_kind, source_observation_id, source_support_trace_id,
                    scope_fingerprint) -> tuple:
    """Identité sémantique d'une dépendance, SANS dependency_type : même
    cible, même source, même scope = UNE dépendance, qualifiée d'un seul
    type (dependent ou partially_dependent)."""
    return target_id, source_kind, source_observation_id, source_support_trace_id, scope_fingerprint


def _check_dependency_identity(existing, key: tuple, dependency_type: str) -> None:
    """Même identité et même type => doublon exact ; même identité et type
    différent => classification contradictoire de la même dépendance."""
    for relation in existing:
        if relation.key == key:
            if relation.qualifier == dependency_type:
                raise DuplicateLongitudinalRelation("dépendance déjà présente (même identité, même type)")
            raise InvalidDependency(
                f"classification contradictoire de la même dépendance : déjà {relation.qualifier},"
                f" {dependency_type} refusé")


def _check_relation_set(dependencies, transfers, revalidations) -> None:
    """Défense finale de complete : aucune dépendance classée deux fois
    (même ou autre type), aucun doublon exact, aucune dépendance
    chevauchant un transfert ou une revalidation de la même cible."""
    for index, dependency in enumerate(dependencies):
        _check_dependency_identity(dependencies[:index], dependency.key, dependency.qualifier)
    for relations in (transfers, revalidations):
        keys = [r.key for r in relations]
        if len(set(keys)) != len(keys):
            raise DuplicateLongitudinalRelation("relation persistée en double")
    for transfer in transfers:
        if _conflicts(transfer.target, transfer.scope, dependencies):
            raise InvalidTransfer(f"transfert vers {transfer.target} chevauchant une dépendance")
    for revalidation in revalidations:
        if _conflicts(revalidation.target, revalidation.scope, dependencies):
            raise InvalidRevalidation(f"revalidation de {revalidation.target} chevauchant une dépendance")


def _insert_scope(db, model, link_name: str, relation_id: uuid.UUID, membership_ids: tuple) -> None:
    if membership_ids:
        db.add_all([model(**{link_name: relation_id, "capability_membership_id": m}) for m in membership_ids])
        db.flush()


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def start_longitudinal_assessment(
    db,
    *,
    user_id: str,
    competency_code: str,
    trigger: str,
    pedagogical_taxonomy_release_id: uuid.UUID,
    dependency_version: str,
    transfer_version: str,
    revalidation_version: str,
    relation_schema_version: str,
    assessment_dedup_key: str,
) -> LongitudinalAssessmentRun:
    """Crée un run running / candidate et son snapshot EXPLICITE.

    L'appelant ne fournit ni observations ni input_fingerprint : le service
    sélectionne lui-même toutes les observations admissibles (voir la
    docstring du module), crée une LongitudinalAssessmentInput par
    observation (zéro est valide) et calcule input_fingerprint V1. La
    release doit être la release active courante (revérifiée sous verrou à
    la complétion).

    Déduplication : assessment_dedup_key déjà présente =>
    DuplicateLongitudinalAssessment, jamais le retour de l'ancien run ;
    pré-vérification puis UNIQUE uq_longitudinal_assessment_runs_dedup_key
    en défense finale (INSERT dans un SAVEPOINT : seule CETTE violation est
    traduite, la transaction de l'appelant reste utilisable)."""
    _require_text(user_id, "user_id")
    _require_choice(competency_code, "competency_code", COMPETENCY_CODES)
    _require_text(trigger, "trigger")
    _require_uuid(pedagogical_taxonomy_release_id, "pedagogical_taxonomy_release_id")
    _require_text(dependency_version, "dependency_version")
    _require_text(transfer_version, "transfer_version")
    _require_text(revalidation_version, "revalidation_version")
    _require_text(relation_schema_version, "relation_schema_version")
    _require_text(assessment_dedup_key, "assessment_dedup_key")

    if db.execute(select(User.id).where(User.id == user_id)).first() is None:
        raise UserNotFound(user_id)
    status = db.execute(
        select(PedagogicalTaxonomyRelease.status)
        .where(PedagogicalTaxonomyRelease.id == pedagogical_taxonomy_release_id)
    ).scalar_one_or_none()
    if status != RELEASE_ACTIVE:
        raise TaxonomyReleaseNotUsable(
            f"{pedagogical_taxonomy_release_id} : {status or 'inconnue'} ({RELEASE_ACTIVE} requise)")
    duplicate = db.execute(
        select(LongitudinalAssessmentRun.id)
        .where(LongitudinalAssessmentRun.assessment_dedup_key == assessment_dedup_key)
    ).first()
    if duplicate is not None:
        raise DuplicateLongitudinalAssessment(assessment_dedup_key)

    snapshot = _current_snapshot(db, user_id=user_id, competency_code=competency_code,
                                 release_id=pedagogical_taxonomy_release_id)
    now = _utcnow()
    run = LongitudinalAssessmentRun(
        id=uuid.uuid4(),
        user_id=user_id,
        competency_code=competency_code,
        execution_status=RUNNING,
        interpretation_status=CANDIDATE,
        trigger=trigger,
        pedagogical_taxonomy_release_id=pedagogical_taxonomy_release_id,
        dependency_version=dependency_version,
        transfer_version=transfer_version,
        revalidation_version=revalidation_version,
        relation_schema_version=relation_schema_version,
        input_fingerprint=_input_fingerprint(user_id=user_id, competency_code=competency_code,
                                             release_id=pedagogical_taxonomy_release_id, evidence=snapshot),
        assessment_dedup_key=assessment_dedup_key,
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
            raise DuplicateLongitudinalAssessment(assessment_dedup_key) from exc
        raise
    if snapshot:
        db.add_all([LongitudinalAssessmentInput(run_id=run.id, observation_id=item.observation_id)
                    for item in snapshot])
        db.flush()
    return run


def add_dependency(
    db,
    *,
    run_id: uuid.UUID,
    target_observation_id: uuid.UUID,
    source_kind: str,
    source_observation_id: uuid.UUID | None = None,
    source_support_trace_id: uuid.UUID | None = None,
    dependency_type: str,
    scope_mode: str,
    dependency_basis: dict,
    capability_membership_ids=(),
) -> ObservationDependency:
    """Persiste une dépendance INTER-événements déjà identifiée (jamais
    détectée ici). Validation structurelle complète avant le verrou du run ;
    sous ce verrou : snapshot, événements distincts, aide du même user,
    scope ⊆ cible, aucun transfert ni revalidation de la même cible sur un
    scope chevauchant, pas de doublon. scope_fingerprint est calculé."""
    _require_uuid(run_id, "run_id")
    _require_uuid(target_observation_id, "target_observation_id")
    _require_choice(source_kind, "source_kind", SOURCE_KINDS)
    _optional_uuid(source_observation_id, "source_observation_id")
    _optional_uuid(source_support_trace_id, "source_support_trace_id")
    _check_dependency_source(source_kind, source_observation_id, source_support_trace_id, target_observation_id)
    _require_choice(dependency_type, "dependency_type", DEPENDENCY_TYPES)
    membership_ids = _scope_arguments(scope_mode, capability_membership_ids)
    basis = _require_basis(dependency_basis, "dependency_basis")

    run = _lock_open_run(db, run_id)
    evidence = _run_evidence(db, run)
    target = _dependency_target(db, run, evidence, target_observation_id, source_kind, source_observation_id,
                                source_support_trace_id)
    scope = _resolve_scope(run, scope_mode, _membership_links(db, membership_ids), target.compatible)
    fingerprint = _scope_fingerprint(run.competency_code, scope)
    dependencies, transfers, revalidations = _relations(db, run)
    _check_dependency_identity(dependencies, _dependency_key(target_observation_id, source_kind,
                                                             source_observation_id, source_support_trace_id,
                                                             fingerprint), dependency_type)
    if _conflicts(target_observation_id, scope, transfers):
        raise InvalidDependency("un transfert autonome vers cette cible chevauche ce scope")
    if _conflicts(target_observation_id, scope, revalidations):
        raise InvalidDependency("une revalidation de cette cible chevauche ce scope")

    row = ObservationDependency(
        id=uuid.uuid4(),
        run_id=run.id,
        target_observation_id=target_observation_id,
        source_kind=source_kind,
        source_observation_id=source_observation_id,
        source_support_trace_id=source_support_trace_id,
        dependency_type=dependency_type,
        scope_mode=scope_mode,
        dependency_basis=basis,
        scope_fingerprint=fingerprint,
        created_at=_utcnow(),
    )
    db.add(row)
    db.flush()
    _insert_scope(db, DependencyCapability, "dependency_id", row.id, membership_ids)
    return row


def add_transfer(
    db,
    *,
    run_id: uuid.UUID,
    source_observation_id: uuid.UUID,
    target_observation_id: uuid.UUID,
    scope_mode: str,
    transfer_basis: dict,
    capability_membership_ids=(),
) -> ObservationTransfer:
    """Persiste un transfert déjà identifié (jamais détecté ici : aucune
    variation de ticker, date ou surface n'est interprétée). Sous le verrou
    du run : snapshot, supportive -> supportive application, événements
    distincts, scope ⊆ intersection, aucune dépendance de la cible sur un
    scope chevauchant, pas de doublon."""
    _require_uuid(run_id, "run_id")
    _require_uuid(source_observation_id, "source_observation_id")
    _require_uuid(target_observation_id, "target_observation_id")
    if source_observation_id == target_observation_id:
        raise InvalidTransfer("source et cible identiques")
    membership_ids = _scope_arguments(scope_mode, capability_membership_ids)
    basis = _require_basis(transfer_basis, "transfer_basis")

    run = _lock_open_run(db, run_id)
    evidence = _run_evidence(db, run)
    available = _transfer_available(db, evidence, source_observation_id, target_observation_id)
    scope = _resolve_scope(run, scope_mode, _membership_links(db, membership_ids), available)
    fingerprint = _scope_fingerprint(run.competency_code, scope)
    dependencies, transfers, _ = _relations(db, run)
    if any(r.key == (source_observation_id, target_observation_id, fingerprint) for r in transfers):
        raise DuplicateLongitudinalRelation(f"transfert déjà présent dans {run_id}")
    if _conflicts(target_observation_id, scope, dependencies):
        raise InvalidTransfer("une dépendance de cette cible chevauche ce scope")

    row = ObservationTransfer(
        id=uuid.uuid4(),
        run_id=run.id,
        source_observation_id=source_observation_id,
        target_observation_id=target_observation_id,
        scope_mode=scope_mode,
        transfer_basis=basis,
        scope_fingerprint=fingerprint,
        created_at=_utcnow(),
    )
    db.add(row)
    db.flush()
    _insert_scope(db, TransferCapability, "transfer_id", row.id, membership_ids)
    return row


def add_revalidation(
    db,
    *,
    run_id: uuid.UUID,
    source_contradiction_observation_id: uuid.UUID,
    target_supportive_observation_id: uuid.UUID,
    scope_mode: str,
    revalidation_basis: dict,
    capability_membership_ids=(),
) -> ObservationRevalidation:
    """Persiste une revalidation HISTORIQUE effectivement observée (jamais
    un besoin futur, décision T6). La contradiction source reste intacte.
    Sous le verrou du run : snapshot, contradictory -> supportive,
    événements distincts (une répétition dans le même événement n'est
    jamais une revalidation), scope ⊆ intersection, aucune dépendance de la
    cible sur un scope chevauchant, pas de doublon."""
    _require_uuid(run_id, "run_id")
    _require_uuid(source_contradiction_observation_id, "source_contradiction_observation_id")
    _require_uuid(target_supportive_observation_id, "target_supportive_observation_id")
    if source_contradiction_observation_id == target_supportive_observation_id:
        raise InvalidRevalidation("source et cible identiques")
    membership_ids = _scope_arguments(scope_mode, capability_membership_ids)
    basis = _require_basis(revalidation_basis, "revalidation_basis")

    run = _lock_open_run(db, run_id)
    evidence = _run_evidence(db, run)
    available = _revalidation_available(db, evidence, source_contradiction_observation_id,
                                        target_supportive_observation_id)
    scope = _resolve_scope(run, scope_mode, _membership_links(db, membership_ids), available)
    fingerprint = _scope_fingerprint(run.competency_code, scope)
    dependencies, _, revalidations = _relations(db, run)
    key = (source_contradiction_observation_id, target_supportive_observation_id, fingerprint)
    if any(r.key == key for r in revalidations):
        raise DuplicateLongitudinalRelation(f"revalidation déjà présente dans {run_id}")
    if _conflicts(target_supportive_observation_id, scope, dependencies):
        raise InvalidRevalidation("une dépendance identifiée de la cible chevauche ce scope")

    row = ObservationRevalidation(
        id=uuid.uuid4(),
        run_id=run.id,
        source_contradiction_observation_id=source_contradiction_observation_id,
        target_supportive_observation_id=target_supportive_observation_id,
        scope_mode=scope_mode,
        revalidation_basis=basis,
        scope_fingerprint=fingerprint,
        created_at=_utcnow(),
    )
    db.add(row)
    db.flush()
    _insert_scope(db, RevalidationCapability, "revalidation_id", row.id, membership_ids)
    return row


def complete_longitudinal_assessment(db, *, run_id: uuid.UUID) -> LongitudinalAssessmentRun:
    """running / candidate -> completed / active, atomiquement avec la
    supersession de l'ancien active du même (user_id, competency_code).

    Ordre (voir la docstring du module) :
    1. lecture non verrouillée de (user_id, competency_code), immuables,
       puis pg_advisory_xact_lock de ce couple : sérialise les activations ;
    2. run candidat (FOR NO KEY UPDATE), running / candidate exigé ;
    3. release du run (FOR SHARE), qui doit être la release active ;
    4. snapshot admissible recalculé : ensemble des lignes d'input ET
       input_fingerprint identiques, sinon StaleLongitudinalSnapshot ;
    5. revalidation de chaque relation persistée et de l'ensemble (doublons,
       chevauchements dépendance / transfert / revalidation) ;
    6. run active courant relu sous la clé consultative (FOR NO KEY
       UPDATE), qui doit être completed / active, passé superseded et
       flushé AVANT l'activation (index unique partiel non différable).
    Toute erreur survient avant la moindre mutation : le candidat n'est
    jamais réécrit (ni snapshot, ni fingerprint)."""
    _require_uuid(run_id, "run_id")

    identity = db.execute(
        select(LongitudinalAssessmentRun.user_id, LongitudinalAssessmentRun.competency_code)
        .where(LongitudinalAssessmentRun.id == run_id)
    ).one_or_none()
    if identity is None:
        raise LongitudinalAssessmentNotFound(str(run_id))
    _lock_activation(db, identity.user_id, identity.competency_code)
    run = _lock_open_run(db, run_id)

    release = _share_release(db, run.pedagogical_taxonomy_release_id)
    if release is None or release.status != RELEASE_ACTIVE:
        raise TaxonomyReleaseNotUsable(
            f"{run.pedagogical_taxonomy_release_id} n'est plus la release {RELEASE_ACTIVE}"
            " : un nouveau dossier doit être calculé")

    snapshot = _current_snapshot(db, user_id=run.user_id, competency_code=run.competency_code,
                                 release_id=run.pedagogical_taxonomy_release_id)
    stored = set(db.execute(
        select(LongitudinalAssessmentInput.observation_id).where(LongitudinalAssessmentInput.run_id == run.id)
    ).scalars())
    if stored != {item.observation_id for item in snapshot}:
        raise StaleLongitudinalSnapshot(f"{run_id} : les observations admissibles ont changé depuis start")
    fingerprint = _input_fingerprint(user_id=run.user_id, competency_code=run.competency_code,
                                     release_id=run.pedagogical_taxonomy_release_id, evidence=snapshot)
    if fingerprint != run.input_fingerprint:
        raise StaleLongitudinalSnapshot(f"{run_id} : input_fingerprint différent du dossier courant")

    _check_relation_set(*_relations(db, run, {item.observation_id: item for item in snapshot}))

    previous = db.execute(
        select(LongitudinalAssessmentRun)
        .where(LongitudinalAssessmentRun.user_id == run.user_id,
               LongitudinalAssessmentRun.competency_code == run.competency_code,
               LongitudinalAssessmentRun.interpretation_status == ACTIVE)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalars().all()
    if len(previous) > 1:
        raise InvalidLongitudinalState(f"{len(previous)} runs actifs observés : intégrité rompue")
    if previous:
        if previous[0].execution_status != COMPLETED:
            raise InvalidLongitudinalState(
                f"run actif {previous[0].id} est {previous[0].execution_status} ({COMPLETED} attendu)")
        previous[0].interpretation_status = SUPERSEDED
        db.flush()

    run.execution_status = COMPLETED
    run.interpretation_status = ACTIVE
    run.completed_at = _utcnow()
    run.failure_code = None
    db.flush()
    return run


def fail_longitudinal_assessment(
    db,
    *,
    run_id: uuid.UUID,
    failure_code: str | None = None,
) -> LongitudinalAssessmentRun:
    """running / candidate -> failed / obsolete. N'affecte jamais l'active
    courant (verrou du seul run) ; inputs et relations déjà créés restent
    pour l'audit. Une seconde transition est refusée."""
    _require_uuid(run_id, "run_id")
    _optional_text(failure_code, "failure_code")

    run = _lock_open_run(db, run_id)
    run.execution_status = FAILED
    run.interpretation_status = OBSOLETE
    run.completed_at = _utcnow()
    run.failure_code = failure_code
    db.flush()
    return run


def get_longitudinal_assessment(db, *, run_id: uuid.UUID) -> LongitudinalAssessmentRun:
    """Lecture simple, sans verrou."""
    _require_uuid(run_id, "run_id")
    run = db.get(LongitudinalAssessmentRun, run_id)
    if run is None:
        raise LongitudinalAssessmentNotFound(str(run_id))
    return run


def get_active_longitudinal_assessment(
    db,
    *,
    user_id: str,
    competency_code: str,
) -> LongitudinalAssessmentRun | None:
    """L'unique run active du couple, ou None. Plusieurs actives
    (impossible sauf contournement de l'index) => InvalidLongitudinalState."""
    _require_text(user_id, "user_id")
    _require_choice(competency_code, "competency_code", COMPETENCY_CODES)
    active = db.execute(
        select(LongitudinalAssessmentRun)
        .where(LongitudinalAssessmentRun.user_id == user_id,
               LongitudinalAssessmentRun.competency_code == competency_code,
               LongitudinalAssessmentRun.interpretation_status == ACTIVE)
    ).scalars().all()
    if len(active) > 1:
        raise InvalidLongitudinalState(f"{len(active)} runs actifs observés : intégrité rompue")
    return active[0] if active else None


def get_longitudinal_inputs(db, *, run_id: uuid.UUID) -> list[LongitudinalAssessmentInput]:
    """Snapshot du run, ORDER BY observation_id."""
    run = get_longitudinal_assessment(db, run_id=run_id)
    return list(db.execute(
        select(LongitudinalAssessmentInput)
        .where(LongitudinalAssessmentInput.run_id == run.id)
        .order_by(LongitudinalAssessmentInput.observation_id)
    ).scalars())


def _with_scopes(db, rows, link_column, membership_column) -> list:
    links = _stored_links(db, link_column, membership_column, [row.id for row in rows])
    return [(row, tuple(sorted((link[0] for link in links.get(row.id, [])), key=str))) for row in rows]


def get_dependencies(db, *, run_id: uuid.UUID) -> list[tuple[ObservationDependency, tuple]]:
    """(dépendance, capability_membership_ids triés) du run, ORDER BY
    created_at, id."""
    run = get_longitudinal_assessment(db, run_id=run_id)
    rows = db.execute(
        select(ObservationDependency).where(ObservationDependency.run_id == run.id)
        .order_by(ObservationDependency.created_at, ObservationDependency.id)
    ).scalars().all()
    return _with_scopes(db, rows, DependencyCapability.dependency_id, DependencyCapability.capability_membership_id)


def get_transfers(db, *, run_id: uuid.UUID) -> list[tuple[ObservationTransfer, tuple]]:
    """(transfert, capability_membership_ids triés) du run, ORDER BY
    created_at, id."""
    run = get_longitudinal_assessment(db, run_id=run_id)
    rows = db.execute(
        select(ObservationTransfer).where(ObservationTransfer.run_id == run.id)
        .order_by(ObservationTransfer.created_at, ObservationTransfer.id)
    ).scalars().all()
    return _with_scopes(db, rows, TransferCapability.transfer_id, TransferCapability.capability_membership_id)


def get_revalidations(db, *, run_id: uuid.UUID) -> list[tuple[ObservationRevalidation, tuple]]:
    """(revalidation, capability_membership_ids triés) du run, ORDER BY
    created_at, id."""
    run = get_longitudinal_assessment(db, run_id=run_id)
    rows = db.execute(
        select(ObservationRevalidation).where(ObservationRevalidation.run_id == run.id)
        .order_by(ObservationRevalidation.created_at, ObservationRevalidation.id)
    ).scalars().all()
    return _with_scopes(db, rows, RevalidationCapability.revalidation_id,
                        RevalidationCapability.capability_membership_id)
