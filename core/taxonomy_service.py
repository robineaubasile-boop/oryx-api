"""Service interne transactionnel de la taxonomie pédagogique et du mapping
Observation -> Capability (T4-B).

Seul point d'écriture applicatif prévu pour PedagogicalTaxonomyRelease,
CoreCapabilityDefinition, CapabilityTaxonomyMembership et
ObservationCapability (même philosophie que core/cognitive_capture.py pour
T2 et core/observation_service.py pour T3).

Doctrine : observation = force et profondeur de la preuve ; capacité =
périmètre de la preuve. Une ligne ObservationCapability localise une
observation existante ; elle ne crée jamais une nouvelle preuve, ne
multiplie pas evidence_strength et ne porte ni score, ni niveau, ni
confiance, ni progression, ni répétition, ni transfert. O1 -> C7_A et
O1 -> C7_B restent UNE observation. Ce service ne décide rien : il reçoit
des identifiants déjà déterminés par l'évaluateur et garantit la cohérence
de la structure T4-A.

Invariants :

- Transactions : jamais de commit() ni de rollback(). Chaque opération
  valide, verrouille, vérifie l'état sous verrou, mute puis flush() ;
  l'appelant possède la transaction (add_observation -> map -> complete ->
  commit, ou rollback de tout le lot). Seule exception, locale et invisible
  pour l'appelant : l'INSERT d'une release ou d'une définition est isolé
  dans un SAVEPOINT pour traduire une violation d'UNICITÉ concurrente
  (aucun verrou parent ne sérialise deux créations de même clé) sans rendre
  inutilisable la transaction de l'appelant ; seules les contraintes
  nommées sont traduites, toute autre IntegrityError est propagée.

- Release : candidate -> active -> retired, uniquement via
  activate_release (candidate -> active, l'ancienne active -> retired dans
  la même transaction). Aucune autre transition (ni retour, ni candidate ->
  retired). Seule une release candidate reçoit des memberships ; active et
  retired sont immuables. activated_at est posé à l'activation et conservé
  au retrait (historique ; pas de retired_at). Aucun minimum de
  memberships n'est exigé à l'activation (règle de contenu de T4-C).

- Définition : immuable (même id = même sens). Un changement de sens est
  une nouvelle ligne (même capability_code, semantic_revision suivante).
  Une release ne contient jamais deux révisions du même capability_code :
  vérifié sous le verrou de la release (la base ne peut pas l'imposer).

- Mapping : observation localized uniquement, run running / candidate
  évalué sous une release (pedagogical_taxonomy_release_id non NULL),
  membership de CETTE release, capacité de la MÊME compétence que
  l'observation. Jamais de delete / unmap / replace : une interprétation
  erronée = un nouveau run. Avant complete_evaluation_run d'un run
  taxonomisé, _validate_run_capability_mappings exige localized => au moins
  un mapping et competency_only => aucun (appelé par
  core/observation_service.py ; ce module n'importe jamais ce dernier).

- Verrous (ordre global, anti-deadlock) :
      attach   : release cible (FOR NO KEY UPDATE)
      activate : verrou consultatif transactionnel ACTIVATION_LOCK_KEY
                 -> release cible -> release active courante
      map      : run parent (FOR NO KEY UPDATE, même verrou que T3-B)
      complete : run -> [lecture des mappings, sans verrou] -> événement
                 -> run active courant (ordre T3-B inchangé)
  Les verrous de release et ceux de run / événement / observation forment
  deux familles disjointes : aucune opération ne détient l'un en attendant
  l'autre. Les releases sont verrouillées en FOR NO KEY UPDATE (et non FOR
  UPDATE), pour la même raison que les runs en T3-B : un INSERT qui
  référence une release (run évalué sous elle, membership) prend un FOR KEY
  SHARE de contrôle de FK, en conflit avec FOR UPDATE mais pas avec FOR NO
  KEY UPDATE, qui reste exclusif entre les opérations du service (aucune ne
  modifie une colonne clé). Chaque verrou utilise populate_existing : un
  objet déjà présent dans la Session est rechargé après l'attente.
  Hypothèse : isolation READ COMMITTED (défaut PostgreSQL et SessionLocal),
  où chaque requête postérieure à un verrou voit les écritures commitées
  par son précédent détenteur.

- Payloads JSON (mapping_guidance) : validés strictement puis copiés en
  profondeur ; logique volontairement dupliquée de T2-B / T3-B plutôt que
  partagée (frontières nettes, aucune dépendance vers observation_service).

Les garanties d'immutabilité sont celles de ce service (aucun trigger en
base) ; les contraintes de 0007_pedagogical_taxonomy (CHECK, UNIQUE, index
unique partiel « une seule release active », FK) restent la défense finale.
"""
import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.models import (
    CapabilityTaxonomyMembership,
    CoreCapabilityDefinition,
    ObservationCapability,
    ObservationEvaluationRun,
    PedagogicalObservation,
    PedagogicalTaxonomyRelease,
)

# PedagogicalTaxonomyRelease.status
RELEASE_CANDIDATE = "candidate"
RELEASE_ACTIVE = "active"
RELEASE_RETIRED = "retired"

# ObservationEvaluationRun : seul état qui accepte encore des mappings
# (vocabulaire T3).
RUN_RUNNING = "running"
RUN_CANDIDATE = "candidate"

# PedagogicalObservation.capability_localization
LOCALIZED = "localized"
COMPETENCY_ONLY = "competency_only"

# Verrou consultatif PostgreSQL TRANSACTION-LEVEL (pg_advisory_xact_lock,
# libéré par PostgreSQL au COMMIT / ROLLBACK), réservé à « Oryx pedagogical
# taxonomy activation » : sérialise toutes les activations, qui n'ont pas
# de ligne parent commune. Constante figée (jamais hash() Python, instable
# entre processus), dérivée une fois de :
#   int.from_bytes(sha256(b"oryx:pedagogical_taxonomy_release_activation")
#                  .digest()[:8], "big", signed=True)
# donc dans la plage bigint PostgreSQL.
ACTIVATION_LOCK_KEY = -1336609790640743501

# Les 45 codes figés par T4-A (CHECK ck_core_capability_definitions_capability_code),
# dans l'ordre pédagogique naturel C1 -> C12 puis A -> D : c'est l'ordre des
# lectures (un tri lexical placerait C10 avant C2). Vocabulaire de
# validation uniquement : aucune donnée n'est créée à partir de lui.
_CAPABILITY_CODES = (
    "C1_A", "C1_B", "C1_C",
    "C2_A", "C2_B", "C2_C",
    "C3_A", "C3_B", "C3_C",
    "C4_A", "C4_B", "C4_C", "C4_D",
    "C5_A", "C5_B", "C5_C", "C5_D",
    "C6_A", "C6_B", "C6_C", "C6_D",
    "C7_A", "C7_B", "C7_C", "C7_D",
    "C8_A", "C8_B", "C8_C", "C8_D",
    "C9_A", "C9_B", "C9_C", "C9_D",
    "C10_A", "C10_B", "C10_C", "C10_D",
    "C11_A", "C11_B", "C11_C", "C11_D",
    "C12_A", "C12_B", "C12_C", "C12_D",
)
_CAPABILITY_ORDER = {code: index for index, code in enumerate(_CAPABILITY_CODES)}
_COMPETENCY_CODES = frozenset(f"C{i}" for i in range(1, 13))

# semantic_revision est un INTEGER PostgreSQL.
_MAX_SEMANTIC_REVISION = 2 ** 31 - 1

_RELEASE_UNIQUES = (
    "uq_pedagogical_taxonomy_releases_version_key",
    "uq_pedagogical_taxonomy_releases_spec_fingerprint",
)
_DEFINITION_UNIQUE = "uq_core_capability_definitions_code_revision"


class TaxonomyServiceError(Exception):
    """Erreur métier du service de taxonomie."""


class TaxonomyReleaseNotFound(TaxonomyServiceError):
    """Aucune PedagogicalTaxonomyRelease pour cet identifiant."""


class CapabilityDefinitionNotFound(TaxonomyServiceError):
    """Aucune CoreCapabilityDefinition pour cet identifiant."""


class CapabilityMembershipNotFound(TaxonomyServiceError):
    """Aucun CapabilityTaxonomyMembership pour cet identifiant."""


class PedagogicalObservationNotFound(TaxonomyServiceError):
    """Aucune PedagogicalObservation pour cet identifiant."""


class InvalidTaxonomyPayload(TaxonomyServiceError):
    """Entrée structurelle invalide : identifiant, chaîne vide, code hors
    vocabulaire, code / compétence incohérents, révision invalide, payload
    non JSON-compatible."""


class InvalidTaxonomyState(TaxonomyServiceError):
    """État incompatible avec l'opération, lu sous verrou : transition de
    release interdite, seconde révision d'un code dans une release, run qui
    n'est plus running / candidate, plusieurs releases actives observées."""


class InvalidCapabilityMapping(TaxonomyServiceError):
    """Localisation incohérente : observation competency_only, run sans
    release, membership d'une autre release, capacité d'une autre
    compétence ; ou, avant complétion d'un run taxonomisé, localized sans
    mapping / competency_only avec mapping."""


class DuplicateTaxonomyRelease(TaxonomyServiceError):
    """version_key ou spec_fingerprint déjà utilisé."""


class DuplicateCapabilityDefinition(TaxonomyServiceError):
    """(capability_code, semantic_revision) déjà défini : une définition
    n'est jamais réécrite."""


class DuplicateCapabilityMembership(TaxonomyServiceError):
    """Cette définition appartient déjà à cette release."""


class DuplicateObservationCapability(TaxonomyServiceError):
    """Cette observation est déjà localisée sur ce membership."""


class TaxonomyReleaseImmutable(TaxonomyServiceError):
    """La release n'est plus candidate : ses memberships sont figés."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _require_text(value, name: str) -> str:
    """str non vide après strip, sans NUL (refusé par PostgreSQL). La valeur
    est conservée telle quelle (aucune normalisation)."""
    if type(value) is not str or not value.strip():
        raise InvalidTaxonomyPayload(f"{name} doit être une chaîne non vide")
    if "\x00" in value:
        raise InvalidTaxonomyPayload(f"{name} : caractère NUL refusé")
    return value


def _require_uuid(value, name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise InvalidTaxonomyPayload(f"{name} doit être un uuid.UUID")
    return value


def _json_copy(value, path: str, active: frozenset):
    """Copie profonde d'une valeur JSON-compatible, ou
    InvalidTaxonomyPayload. Types exacts pour les scalaires : un objet qui
    ne serait JSON que par conversion (clé non str, Enum, Decimal,
    datetime, tuple, set, NaN...) est refusé plutôt que transformé.
    PostgreSQL refuse \\u0000 dans un JSONB : refusé ici plutôt qu'au
    flush."""
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InvalidTaxonomyPayload(f"{path} : nombre non fini refusé")
        return value
    if type(value) is str:
        if "\x00" in value:
            raise InvalidTaxonomyPayload(f"{path} : caractère NUL refusé")
        return value
    if isinstance(value, (dict, list)):
        if id(value) in active:
            raise InvalidTaxonomyPayload(f"{path} : référence circulaire")
        active = active | {id(value)}
        if isinstance(value, list):
            return [_json_copy(item, f"{path}[{i}]", active) for i, item in enumerate(value)]
        copy = {}
        for key, item in value.items():
            if type(key) is not str or "\x00" in key:
                raise InvalidTaxonomyPayload(f"{path} : clé {key!r} refusée (str sans NUL attendue)")
            copy[key] = _json_copy(item, f"{path}.{key}", active)
        return copy
    raise InvalidTaxonomyPayload(f"{path} : type {type(value).__name__} non JSON-compatible")


def _json_object(value, name: str) -> dict:
    """Racine dict imposée puis copie profonde validée ; {} est valide.
    Aucun schéma interne (include / exclude...) : contenu de T4-C."""
    if not isinstance(value, dict):
        raise InvalidTaxonomyPayload(f"{name} doit être un dict")
    return _json_copy(value, name, frozenset())


def _require_capability(capability_code, competency_code) -> None:
    """Code parmi les 45, compétence parmi C1..C12 (str exacts : une Enum
    str est refusée, jamais convertie), et préfixe cohérent (C7_A => C7)."""
    if type(capability_code) is not str or capability_code not in _CAPABILITY_ORDER:
        raise InvalidTaxonomyPayload(f"capability_code : valeur {capability_code!r} hors des 45 codes")
    if type(competency_code) is not str or competency_code not in _COMPETENCY_CODES:
        raise InvalidTaxonomyPayload(f"competency_code : valeur {competency_code!r} hors de C1..C12")
    if capability_code.split("_", 1)[0] != competency_code:
        raise InvalidTaxonomyPayload(
            f"capability_code {capability_code} n'appartient pas à la compétence {competency_code}")


def _require_revision(value) -> int:
    """int réel (bool refusé : True n'est pas la révision 1), >= 1, dans la
    plage INTEGER."""
    if type(value) is not int or not 1 <= value <= _MAX_SEMANTIC_REVISION:
        raise InvalidTaxonomyPayload(f"semantic_revision : entier >= 1 attendu, reçu {value!r}")
    return value


def _violated_constraint(exc: IntegrityError) -> str | None:
    """Nom de la contrainte violée rapporté par PostgreSQL (ou None)."""
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def _insert_translating_uniques(db, row, uniques, error) -> None:
    """INSERT isolé dans un SAVEPOINT : seule une violation d'une des
    contraintes UNIQUE `uniques` devient `error`, après retour au
    savepoint (la transaction de l'appelant reste utilisable). Toute autre
    IntegrityError est propagée telle quelle."""
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        if _violated_constraint(exc) in uniques:
            raise error from exc
        raise


def _capability_sort_key(definition: CoreCapabilityDefinition, row_id) -> tuple:
    """Ordre pédagogique naturel (C1_A ... C12_D), puis révision et id pour
    un ordre total déterministe."""
    return _CAPABILITY_ORDER[definition.capability_code], definition.semantic_revision, str(row_id)


# --------------------------------------------------------------------------
# Verrouillage
# --------------------------------------------------------------------------

def _lock_release(db, release_id: uuid.UUID) -> PedagogicalTaxonomyRelease:
    """SELECT ... FOR NO KEY UPDATE sur la release (voir la docstring du
    module pour le choix de ce mode). L'état n'est lu qu'une fois le verrou
    obtenu."""
    release = db.execute(
        select(PedagogicalTaxonomyRelease)
        .where(PedagogicalTaxonomyRelease.id == release_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if release is None:
        raise TaxonomyReleaseNotFound(str(release_id))
    return release


def _lock_activation(db) -> None:
    """pg_advisory_xact_lock(ACTIVATION_LOCK_KEY) : attend toute activation
    en cours ; libéré automatiquement à la fin de la transaction de
    l'appelant (jamais le verrou de session pg_advisory_lock, qui
    survivrait au COMMIT)."""
    db.execute(select(func.pg_advisory_xact_lock(ACTIVATION_LOCK_KEY)))


def _lock_run(db, run_id: uuid.UUID) -> ObservationEvaluationRun:
    """Même verrou que T3-B (FOR NO KEY UPDATE sur le run) : le run est la
    frontière de mutation de ses observations ET de leurs mappings, ce qui
    sérialise map_observation_capability avec add_observation et
    complete_evaluation_run."""
    return db.execute(
        select(ObservationEvaluationRun)
        .where(ObservationEvaluationRun.id == run_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one()


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def create_candidate_release(
    db,
    *,
    version_key: str,
    spec_fingerprint: str,
) -> PedagogicalTaxonomyRelease:
    """Crée une release candidate (activated_at NULL). Le statut n'est
    jamais fourni par l'appelant ; spec_fingerprint est fourni tel quel
    (aucun calcul ici).

    Doublon de version_key ou de spec_fingerprint => DuplicateTaxonomyRelease :
    pré-vérification, puis UNIQUE en défense finale (savepoint, voir la
    docstring du module)."""
    _require_text(version_key, "version_key")
    _require_text(spec_fingerprint, "spec_fingerprint")

    duplicate = db.execute(
        select(PedagogicalTaxonomyRelease.id)
        .where((PedagogicalTaxonomyRelease.version_key == version_key)
               | (PedagogicalTaxonomyRelease.spec_fingerprint == spec_fingerprint))
    ).first()
    if duplicate is not None:
        raise DuplicateTaxonomyRelease(f"{version_key} / {spec_fingerprint}")

    release = PedagogicalTaxonomyRelease(
        id=uuid.uuid4(),
        version_key=version_key,
        status=RELEASE_CANDIDATE,
        spec_fingerprint=spec_fingerprint,
        created_at=_utcnow(),
        activated_at=None,
    )
    _insert_translating_uniques(db, release, _RELEASE_UNIQUES,
                                DuplicateTaxonomyRelease(f"{version_key} / {spec_fingerprint}"))
    return release


def create_capability_definition(
    db,
    *,
    capability_code: str,
    semantic_revision: int,
    competency_code: str,
    label: str,
    definition: str,
    mapping_guidance: dict,
) -> CoreCapabilityDefinition:
    """Crée UNE signification immuable d'une capacité noyau. Aucune mise à
    jour n'existe : un nouveau sens = une nouvelle semantic_revision (nouvel
    UUID), l'ancienne reste intacte.

    (capability_code, semantic_revision) déjà défini =>
    DuplicateCapabilityDefinition (pré-vérification, puis UNIQUE en défense
    finale via savepoint)."""
    _require_capability(capability_code, competency_code)
    _require_revision(semantic_revision)
    _require_text(label, "label")
    _require_text(definition, "definition")
    guidance = _json_object(mapping_guidance, "mapping_guidance")

    duplicate = db.execute(
        select(CoreCapabilityDefinition.id)
        .where(CoreCapabilityDefinition.capability_code == capability_code,
               CoreCapabilityDefinition.semantic_revision == semantic_revision)
    ).first()
    if duplicate is not None:
        raise DuplicateCapabilityDefinition(f"{capability_code} révision {semantic_revision}")

    row = CoreCapabilityDefinition(
        id=uuid.uuid4(),
        capability_code=capability_code,
        semantic_revision=semantic_revision,
        competency_code=competency_code,
        label=label,
        definition=definition,
        mapping_guidance=guidance,
        created_at=_utcnow(),
    )
    _insert_translating_uniques(db, row, (_DEFINITION_UNIQUE,),
                                DuplicateCapabilityDefinition(f"{capability_code} révision {semantic_revision}"))
    return row


def attach_capability_to_release(
    db,
    *,
    release_id: uuid.UUID,
    capability_definition_id: uuid.UUID,
) -> CapabilityTaxonomyMembership:
    """Ajoute une définition à une release candidate.

    La release est verrouillée AVANT de lire ses memberships : deux ajouts
    concurrents à la même release sont sérialisés, le second voit le
    premier une fois celui-ci commité. Sous ce verrou :
    - même définition déjà présente => DuplicateCapabilityMembership ;
    - autre révision du même capability_code déjà présente =>
      InvalidTaxonomyState (jamais deux révisions d'un code par release).
    Une autre release peut réutiliser librement la même définition."""
    _require_uuid(release_id, "release_id")
    _require_uuid(capability_definition_id, "capability_definition_id")

    release = _lock_release(db, release_id)
    if release.status != RELEASE_CANDIDATE:
        raise TaxonomyReleaseImmutable(f"{release_id} est {release.status} ({RELEASE_CANDIDATE} requis)")
    definition = db.get(CoreCapabilityDefinition, capability_definition_id)
    if definition is None:
        raise CapabilityDefinitionNotFound(str(capability_definition_id))

    present = db.execute(
        select(CoreCapabilityDefinition.id, CoreCapabilityDefinition.semantic_revision)
        .join(CapabilityTaxonomyMembership,
              CapabilityTaxonomyMembership.capability_definition_id == CoreCapabilityDefinition.id)
        .where(CapabilityTaxonomyMembership.taxonomy_release_id == release.id,
               CoreCapabilityDefinition.capability_code == definition.capability_code)
    ).all()
    for present_id, present_revision in present:
        if present_id == definition.id:
            raise DuplicateCapabilityMembership(f"{definition.capability_code} déjà dans {release_id}")
        raise InvalidTaxonomyState(
            f"{release_id} contient déjà {definition.capability_code} révision {present_revision}"
            f" (révision {definition.semantic_revision} refusée)")

    membership = CapabilityTaxonomyMembership(
        id=uuid.uuid4(),
        taxonomy_release_id=release.id,
        capability_definition_id=definition.id,
        created_at=_utcnow(),
    )
    db.add(membership)
    db.flush()
    return membership


def activate_release(
    db,
    *,
    release_id: uuid.UUID,
) -> PedagogicalTaxonomyRelease:
    """candidate -> active, atomiquement avec active -> retired pour
    l'ancienne release active (s'il y en a une). Une release déjà active ou
    retired => InvalidTaxonomyState (jamais un no-op).

    Ordre des verrous :
    1. pg_advisory_xact_lock(ACTIVATION_LOCK_KEY) : sérialise toutes les
       activations (pas de ligne parent commune) ;
    2. la release cible (FOR NO KEY UPDATE), état vérifié ;
    3. la release active courante, relue SOUS le verrou consultatif (donc à
       jour d'une activation concurrente commitée) et verrouillée.

    L'ancienne active passe retired et est flushée AVANT l'activation de la
    cible : l'index unique partiel uq_pedagogical_taxonomy_releases_one_active
    (non différable) est vérifié ligne par ligne et le flush SQLAlchemy
    n'ordonne pas les UPDATE d'une même table selon l'ordre des mutations.
    Son activated_at est conservé. La cible reçoit activated_at = un unique
    `now` UTC. Aucun nombre minimal de memberships n'est exigé (T4-C)."""
    _require_uuid(release_id, "release_id")

    _lock_activation(db)
    release = _lock_release(db, release_id)
    if release.status != RELEASE_CANDIDATE:
        raise InvalidTaxonomyState(
            f"{release_id} est {release.status} : seule une release {RELEASE_CANDIDATE} est activable")
    previous = db.execute(
        select(PedagogicalTaxonomyRelease)
        .where(PedagogicalTaxonomyRelease.status == RELEASE_ACTIVE)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalars().all()
    if len(previous) > 1:
        raise InvalidTaxonomyState(f"{len(previous)} releases actives observées : intégrité rompue")
    if previous:
        previous[0].status = RELEASE_RETIRED
        db.flush()

    release.status = RELEASE_ACTIVE
    release.activated_at = _utcnow()
    db.flush()
    return release


def map_observation_capability(
    db,
    *,
    observation_id: uuid.UUID,
    capability_membership_id: uuid.UUID,
) -> ObservationCapability:
    """Localise une observation existante sur un membership. Ne crée
    aucune preuve : une observation localisée sur C7_A et C7_B reste UNE
    observation.

    Ordre : lecture de l'observation (son run parent est immuable), puis
    verrou du run parent (FOR NO KEY UPDATE, comme add_observation et
    complete_evaluation_run), puis, sous ce verrou :
    - run running / candidate, sinon InvalidTaxonomyState (aucun mapping
      après complétion ou échec) ;
    - run évalué sous une release, observation localized, membership de
      cette release, capacité de la compétence de l'observation, sinon
      InvalidCapabilityMapping ;
    - pas de doublon, sinon DuplicateObservationCapability (PK composite en
      défense finale).
    competency_code, capability_localization et evaluation_run_id sont
    immuables : l'observation n'a pas à être relue sous le verrou. Son
    integrity_status n'intervient pas (une observation invalidated reste
    localisable, et ses mappings restent dans l'historique)."""
    _require_uuid(observation_id, "observation_id")
    _require_uuid(capability_membership_id, "capability_membership_id")

    observation = db.get(PedagogicalObservation, observation_id)
    if observation is None:
        raise PedagogicalObservationNotFound(str(observation_id))
    run = _lock_run(db, observation.evaluation_run_id)
    if run.execution_status != RUN_RUNNING or run.interpretation_status != RUN_CANDIDATE:
        raise InvalidTaxonomyState(
            f"run {run.id} est {run.execution_status} / {run.interpretation_status}"
            f" ({RUN_RUNNING} / {RUN_CANDIDATE} requis)")
    if run.pedagogical_taxonomy_release_id is None:
        raise InvalidCapabilityMapping(f"run {run.id} évalué sans release de taxonomie")
    if observation.capability_localization != LOCALIZED:
        raise InvalidCapabilityMapping(
            f"{observation_id} est {observation.capability_localization} ({LOCALIZED} requis)")

    membership = db.get(CapabilityTaxonomyMembership, capability_membership_id)
    if membership is None:
        raise CapabilityMembershipNotFound(str(capability_membership_id))
    if membership.taxonomy_release_id != run.pedagogical_taxonomy_release_id:
        raise InvalidCapabilityMapping(
            f"membership de la release {membership.taxonomy_release_id}, run évalué sous"
            f" {run.pedagogical_taxonomy_release_id}")
    definition = db.get(CoreCapabilityDefinition, membership.capability_definition_id)
    if definition.competency_code != observation.competency_code:
        raise InvalidCapabilityMapping(
            f"capacité {definition.capability_code} hors de la compétence {observation.competency_code}")

    duplicate = db.execute(
        select(ObservationCapability.observation_id)
        .where(ObservationCapability.observation_id == observation.id,
               ObservationCapability.capability_membership_id == membership.id)
    ).first()
    if duplicate is not None:
        raise DuplicateObservationCapability(f"{observation_id} -> {capability_membership_id}")

    mapping = ObservationCapability(
        observation_id=observation.id,
        capability_membership_id=membership.id,
        created_at=_utcnow(),
    )
    db.add(mapping)
    db.flush()
    return mapping


def get_active_release(db) -> PedagogicalTaxonomyRelease | None:
    """L'unique release active, ou None. Lecture simple, sans verrou.
    Plusieurs actives (impossible sauf contournement de l'index) =>
    InvalidTaxonomyState, jamais un choix arbitraire."""
    active = db.execute(
        select(PedagogicalTaxonomyRelease).where(PedagogicalTaxonomyRelease.status == RELEASE_ACTIVE)
    ).scalars().all()
    if len(active) > 1:
        raise InvalidTaxonomyState(f"{len(active)} releases actives observées : intégrité rompue")
    return active[0] if active else None


def get_release_capabilities(
    db,
    *,
    release_id: uuid.UUID,
) -> list[tuple[CapabilityTaxonomyMembership, CoreCapabilityDefinition]]:
    """Les (membership, définition) de la release, sans verrou, dans l'ordre
    pédagogique naturel C1_A, C1_B, ..., C2_A, ..., C10_A, ..., C12_D (et
    non l'ordre lexical, qui placerait C10 avant C2)."""
    _require_uuid(release_id, "release_id")
    if db.get(PedagogicalTaxonomyRelease, release_id) is None:
        raise TaxonomyReleaseNotFound(str(release_id))
    rows = db.execute(
        select(CapabilityTaxonomyMembership, CoreCapabilityDefinition)
        .join(CoreCapabilityDefinition,
              CoreCapabilityDefinition.id == CapabilityTaxonomyMembership.capability_definition_id)
        .where(CapabilityTaxonomyMembership.taxonomy_release_id == release_id)
    ).all()
    return sorted(((m, d) for m, d in rows), key=lambda pair: _capability_sort_key(pair[1], pair[0].id))


def get_observation_capabilities(
    db,
    *,
    observation_id: uuid.UUID,
) -> list[tuple[ObservationCapability, CapabilityTaxonomyMembership, CoreCapabilityDefinition]]:
    """Les (mapping, membership, définition) de l'observation, sans verrou,
    dans le même ordre naturel. Rien n'est filtré selon integrity_status :
    les mappings d'une observation invalidated restent son historique."""
    _require_uuid(observation_id, "observation_id")
    if db.get(PedagogicalObservation, observation_id) is None:
        raise PedagogicalObservationNotFound(str(observation_id))
    rows = db.execute(
        select(ObservationCapability, CapabilityTaxonomyMembership, CoreCapabilityDefinition)
        .join(CapabilityTaxonomyMembership,
              CapabilityTaxonomyMembership.id == ObservationCapability.capability_membership_id)
        .join(CoreCapabilityDefinition,
              CoreCapabilityDefinition.id == CapabilityTaxonomyMembership.capability_definition_id)
        .where(ObservationCapability.observation_id == observation_id)
    ).all()
    return sorted(((o, m, d) for o, m, d in rows), key=lambda row: _capability_sort_key(row[2], row[1].id))


# --------------------------------------------------------------------------
# Contrôle interne appelé par complete_evaluation_run (non public)
# --------------------------------------------------------------------------

def _validate_run_capability_mappings(db, run: ObservationEvaluationRun) -> None:
    """Défense finale avant running / candidate -> completed / active.

    Appelé par core/observation_service.complete_evaluation_run APRÈS le
    verrou du run (qui sérialise add_observation et
    map_observation_capability sur ce run) : les lectures ci-dessous, sans
    verrou supplémentaire, voient donc un état stable et à jour.

    Run sans release (historique T3-B) : aucun contrôle, comportement
    inchangé. Run taxonomisé : pour CHAQUE observation (invalidated
    comprises), localized => au moins un mapping, competency_only => aucun ;
    pour chaque mapping, membership de la release du run et capacité de la
    compétence de l'observation. Zéro observation reste valide. Toute
    incohérence => InvalidCapabilityMapping ; rien n'est modifié (le run
    reste running / candidate, l'appelant décide du rollback)."""
    release_id = run.pedagogical_taxonomy_release_id
    if release_id is None:
        return
    observations = db.execute(
        select(PedagogicalObservation.id, PedagogicalObservation.ordinal,
               PedagogicalObservation.competency_code, PedagogicalObservation.capability_localization)
        .where(PedagogicalObservation.evaluation_run_id == run.id)
        .order_by(PedagogicalObservation.ordinal.asc())
    ).all()
    if not observations:
        return
    mappings = db.execute(
        select(ObservationCapability.observation_id, CapabilityTaxonomyMembership.taxonomy_release_id,
               CoreCapabilityDefinition.capability_code, CoreCapabilityDefinition.competency_code)
        .join(PedagogicalObservation, PedagogicalObservation.id == ObservationCapability.observation_id)
        .join(CapabilityTaxonomyMembership,
              CapabilityTaxonomyMembership.id == ObservationCapability.capability_membership_id)
        .join(CoreCapabilityDefinition,
              CoreCapabilityDefinition.id == CapabilityTaxonomyMembership.capability_definition_id)
        .where(PedagogicalObservation.evaluation_run_id == run.id)
    ).all()
    by_observation = {}
    for observation_id, mapped_release_id, capability_code, competency_code in mappings:
        by_observation.setdefault(observation_id, []).append((mapped_release_id, capability_code, competency_code))

    for observation_id, ordinal, competency_code, localization in observations:
        located = by_observation.get(observation_id, [])
        if localization == LOCALIZED and not located:
            raise InvalidCapabilityMapping(f"run {run.id}, observation {ordinal} : {LOCALIZED} sans mapping")
        if localization != LOCALIZED and located:
            raise InvalidCapabilityMapping(
                f"run {run.id}, observation {ordinal} : {localization} avec {len(located)} mapping(s)")
        for mapped_release_id, capability_code, mapped_competency in located:
            if mapped_release_id != release_id:
                raise InvalidCapabilityMapping(
                    f"run {run.id}, observation {ordinal} : {capability_code} de la release"
                    f" {mapped_release_id}, run évalué sous {release_id}")
            if mapped_competency != competency_code:
                raise InvalidCapabilityMapping(
                    f"run {run.id}, observation {ordinal} : {capability_code} hors de {competency_code}")
