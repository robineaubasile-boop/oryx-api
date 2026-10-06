"""Service interne de capture cognitive (T2-B, niveau 2 : evidence /
provenance ; R1-B : identité idempotente).

Seul point d'écriture applicatif de CognitiveEvent et SupportTrace : toute
mutation future de ces tables doit passer par ce module. Il CAPTURE ce que
l'utilisateur a vu et produit, et les aides qui lui ont été montrées ; il
n'évalue rien et n'orchestre rien (quand ouvrir, segmenter, admettre,
finaliser ou abandonner un événement est décidé par l'appelant).

API publique (R1-B) : open_event_idempotent, append_user_contribution,
add_support_trace, finalize_event, abandon_event, get_event,
get_support_traces. Aucune voie d'écriture legacy (ni open_event ni
append_user_work bruts) : tout nouvel événement porte son identité de
déduplication et ses versions, toute nouvelle production a une identité
stable.

Invariants :

- Transactions : le service ne fait jamais commit() ni rollback(). Il
  valide, verrouille, mute puis flush() ; la frontière transactionnelle
  appartient à l'appelant. Un commit interne validerait aussi les autres
  écritures en attente dans la Session et empêcherait de combiner écritures
  métier et écritures cognitives dans une même transaction atomique. Seule
  exception, locale et invisible pour l'appelant : l'INSERT d'un événement
  est isolé dans un SAVEPOINT (begin_nested) pour traduire une collision
  sur uq_cognitive_events_event_dedup_key sans rendre la transaction de
  l'appelant inutilisable. SAVEPOINT local != ownership de la transaction.

- Identité d'un événement (R1-B) : event_dedup_key = SHA-256 (hex
  minuscule) du JSON canonique de l'identité de segmentation : version du
  schéma de déduplication, user_id, conversation_key, source_turn_refs
  (dans l'ordre fourni), segmentation_ordinal, event_builder_version.
  Calculée ici, jamais fournie par l'appelant ; jamais de contenu (texte,
  stimulus), d'horodatage, d'origine, de task_kind ni d'aléa. Elle est
  figée à l'ouverture : une contribution ultérieure ne la recalcule pas.
  Même clé + mêmes champs structurants => l'événement existant est
  retourné tel quel (quel que soit son statut, sans mutation) ; même clé +
  champ divergent => EventDedupCollision.

- Conversation : un nouvel événement exige une conversation_key résolue
  dans le registre d'appartenance (core/interaction_identity.py) pour le
  même user_id. Une analysis_session liée doit exister et appartenir au
  même user_id (vérifié ici, pas seulement par la FK) ; aucune autre
  sémantique n'en est tirée.

- Contributions (event_schema_version cognitive-event-v2) : chaque entrée
  de user_work_snapshot a une identité stable contribution_id. phase,
  occurred_at et support_refs_before sont capturés par le service au
  premier append ; un retry exact est un no-op (aucune mutation, même sur
  un événement fermé), un retry divergent lève
  ContributionIdentityCollision. Un événement legacy (event_schema_version
  NULL) n'est jamais enrichi ni converti.

- Concurrence : toute mutation d'un événement existant commence par
  SELECT ... FOR UPDATE sur sa ligne, AVANT de lire status. Le verrou est
  transactionnel (PostgreSQL) : deux transactions ne peuvent ni travailler
  sur un état obsolète, ni ajouter une production ou une aide pendant une
  fermeture, ni allouer le même sequence_no ou la même phase.

- Lifecycle : open -> finalized, ou open -> abandoned. finalized et
  abandoned sont terminaux : plus aucune production, aide ni transition
  (y compris une seconde finalisation, refusée plutôt qu'ignorée pour
  révéler un bug d'orchestration). L'idempotence de l'identité ne rend pas
  le lifecycle permissif. stimulus_snapshot est figé à l'ouverture ; aucune
  fonction ne permet de le modifier.

- SupportTrace est append-only : aucune fonction de modification ni de
  suppression. Même une aide erronée reste l'historique de ce qui a
  réellement été montré. support_payload ne doit contenir que ce qui a été
  montré à l'utilisateur (jamais de chain-of-thought, raisonnement interne,
  prompt système ni diagnostic non montré) ; c'est un contrat des
  appelants, que le service n'inspecte pas. support_refs_before ne
  certifie pas l'affichage : ce sont les traces persistées avant la
  contribution (toutes par défaut, ou le sous-ensemble explicitement fourni
  par l'orchestrateur, R1-C4).

- Payloads : dicts JSON-compatibles, sans schéma métier imposé ({} est
  valide). Ils sont validés puis copiés en profondeur à la capture : une
  mutation ultérieure de l'objet de l'appelant ne change pas l'historique.
  Rien de non JSON-compatible n'est converti silencieusement.

- Données : les messages d'erreur ne contiennent que des identifiants
  techniques et des noms de champs, jamais de texte utilisateur ni de
  snapshot.

La garantie d'immutabilité est une garantie de ce service (aucun trigger en
base) : elle ne couvre pas une modification directe des objets ORM par un
appelant qui contournerait ce module.
"""
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core import interaction_identity
from core.models import AnalysisSession, CognitiveEvent, SupportTrace

OPEN = "open"
FINALIZED = "finalized"
ABANDONED = "abandoned"

# Versions persistées sur chaque nouvel événement. builder / admission
# versionnent les politiques (futures) de l'orchestrateur qui décide de la
# segmentation et de l'admission ; event_schema_version est le format de
# user_work_snapshot écrit par CE service (les lignes legacy pré-R1-B ont
# NULL, jamais un « v1 » artificiel).
EVENT_BUILDER_VERSION = "cognitive-event-builder-1"
ADMISSION_VERSION = "cognitive-event-admission-1"
EVENT_SCHEMA_VERSION = "cognitive-event-v2"
# Version du payload canonique hashé dans event_dedup_key (constante de code).
EVENT_DEDUP_SCHEMA_VERSION = "cognitive-event-dedup-v1"
DEDUP_CONSTRAINT = "uq_cognitive_events_event_dedup_key"

# Champs structurants immuables comparés lors d'un retry d'ouverture.
EVENT_IDENTITY_FIELDS = (
    "user_id", "conversation_key", "event_origin", "task_kind", "analysis_session_id", "stimulus_snapshot",
    "event_builder_version", "admission_version", "event_schema_version", "event_dedup_key",
)
# Champs d'une contribution contrôlés par l'appelant (comparés lors d'un
# retry) ; phase, occurred_at et support_refs_before sont capturés par le
# service au premier append et ne sont jamais comparés ni recalculés.
CONTRIBUTION_CALLER_FIELDS = ("contribution_id", "source_turn_ref", "surface", "session_ref", "text")


class CognitiveCaptureError(Exception):
    """Erreur métier du service de capture cognitive."""


class CognitiveEventNotFound(CognitiveCaptureError):
    """Aucun CognitiveEvent pour cet identifiant."""


class CognitiveEventClosed(CognitiveCaptureError):
    """L'événement est finalized ou abandoned : plus aucune mutation."""


class InvalidCognitivePayload(CognitiveCaptureError):
    """Entrée structurelle invalide : identifiant, chaîne vide, payload qui
    n'est pas un dict JSON-compatible."""


class AnalysisSessionNotFound(CognitiveCaptureError):
    """analysis_session_id ne désigne aucune AnalysisSession."""


class AnalysisSessionOwnershipConflict(CognitiveCaptureError):
    """L'AnalysisSession appartient à un autre utilisateur."""


class EventDedupCollision(CognitiveCaptureError):
    """Même event_dedup_key qu'un événement existant, mais un champ
    structurant diverge : ni retour silencieux, ni modification, ni
    seconde ligne."""


class UnsupportedCognitiveEventSchema(CognitiveCaptureError):
    """L'événement n'est pas au format event_schema_version courant (ligne
    legacy pré-R1-B) : il n'est jamais enrichi ni converti implicitement."""


class ContributionIdentityCollision(CognitiveCaptureError):
    """Même contribution_id qu'une contribution existante, mais un champ
    contrôlé par l'appelant diverge."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _require_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidCognitivePayload(f"{name} doit être une chaîne non vide")
    return value


def _require_uuid(value, name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise InvalidCognitivePayload(f"{name} doit être un uuid.UUID")
    return value


def _require_exact_uuid(value, name: str) -> uuid.UUID:
    if type(value) is not uuid.UUID:
        raise InvalidCognitivePayload(f"{name} doit être un uuid.UUID")
    return value


def _require_identifier(value, name: str) -> str:
    """str non vide (après strip), sans NUL, encodable en UTF-8 ; jamais
    normalisée (la valeur est persistée et hashée telle quelle)."""
    _require_text(value, name)
    if "\x00" in value:
        raise InvalidCognitivePayload(f"{name} : caractère NUL refusé")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidCognitivePayload(f"{name} : chaîne non encodable en UTF-8") from None
    return value


def _require_source_turn_refs(value) -> tuple:
    """tuple non vide d'uuid.UUID exacts, sans doublon ; ordre conservé."""
    if type(value) is not tuple or not value:
        raise InvalidCognitivePayload("source_turn_refs doit être un tuple non vide")
    for index, ref in enumerate(value):
        _require_exact_uuid(ref, f"source_turn_refs[{index}]")
    if len(set(value)) != len(value):
        raise InvalidCognitivePayload("source_turn_refs : doublon refusé")
    return value


def _require_ordinal(value) -> int:
    if type(value) is not int or value < 1:
        raise InvalidCognitivePayload("segmentation_ordinal doit être un int >= 1")
    return value


def _json_copy(value, path: str, active: frozenset):
    """Copie profonde d'une valeur JSON-compatible, ou InvalidCognitivePayload.

    Types exacts pour les scalaires : un objet qui ne serait JSON que par
    conversion (clé non str, Enum, Decimal, datetime, tuple, NaN...) est
    refusé plutôt que transformé. PostgreSQL refuse \\u0000 dans un JSONB :
    refusé ici plutôt qu'au flush."""
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InvalidCognitivePayload(f"{path} : nombre non fini refusé")
        return value
    if type(value) is str:
        if "\x00" in value:
            raise InvalidCognitivePayload(f"{path} : caractère NUL refusé")
        return value
    if isinstance(value, (dict, list)):
        if id(value) in active:
            raise InvalidCognitivePayload(f"{path} : référence circulaire")
        active = active | {id(value)}
        if isinstance(value, list):
            return [_json_copy(item, f"{path}[{i}]", active) for i, item in enumerate(value)]
        copy = {}
        for key, item in value.items():
            if type(key) is not str or "\x00" in key:
                raise InvalidCognitivePayload(f"{path} : clé {key!r} refusée (str sans NUL attendue)")
            copy[key] = _json_copy(item, f"{path}.{key}", active)
        return copy
    raise InvalidCognitivePayload(f"{path} : type {type(value).__name__} non JSON-compatible")


def _json_dict(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidCognitivePayload(f"{name} doit être un dict")
    return _json_copy(value, name, frozenset())


def _json_number(value) -> Decimal:
    """Valeur décimale d'un nombre JSON telle que JSONB la stocke : un float
    est sérialisé par sa repr (json.dumps), que numeric conserve exactement."""
    return Decimal(repr(value)) if type(value) is float else Decimal(value)


def _json_equal(a, b) -> bool:
    """Égalité de deux valeurs JSON telle que JSONB la définit : les nombres
    se comparent en décimal (1 == 1.0 ; 1e30 relu depuis JSONB revient en
    int exact 10**30), mais un booléen n'est jamais égal à un nombre (en
    Python, True == 1)."""
    if type(a) is bool or type(b) is bool:
        return type(a) is type(b) and a == b
    if type(a) in (int, float) and type(b) in (int, float):
        return _json_number(a) == _json_number(b)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


# --------------------------------------------------------------------------
# Identité de déduplication
# --------------------------------------------------------------------------

def _event_dedup_key(
    *,
    user_id: str,
    conversation_key: str,
    source_turn_refs: tuple,
    segmentation_ordinal: int,
    event_builder_version: str,
) -> str:
    """SHA-256 (hex minuscule) du JSON canonique (sort_keys, séparateurs
    compacts, ensure_ascii=False, UTF-8) de l'identité de segmentation.
    user_id + conversation_key en font un espace de noms : deux
    utilisateurs réutilisant le même UUID de tour ne partagent jamais un
    événement. Fonction pure et déterministe (aucun hash() Python)."""
    payload = {
        "dedup_schema_version": EVENT_DEDUP_SCHEMA_VERSION,
        "user_id": _require_identifier(user_id, "user_id"),
        "conversation_key": _require_identifier(conversation_key, "conversation_key"),
        "source_turn_refs": [str(ref) for ref in _require_source_turn_refs(source_turn_refs)],
        "segmentation_ordinal": _require_ordinal(segmentation_ordinal),
        "event_builder_version": _require_identifier(event_builder_version, "event_builder_version"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_dedup_violation(exc: IntegrityError) -> bool:
    """Vrai seulement pour une violation de uq_cognitive_events_event_dedup_key
    (nom de contrainte rapporté par PostgreSQL)."""
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == DEDUP_CONSTRAINT


def _event_by_dedup_key(db, dedup_key: str) -> CognitiveEvent | None:
    return db.execute(
        select(CognitiveEvent).where(CognitiveEvent.event_dedup_key == dedup_key)
    ).scalar_one_or_none()


def _same_event_or_collision(event: CognitiveEvent, expected: dict) -> CognitiveEvent:
    """Retourne l'événement existant si tous ses champs structurants sont
    identiques à ceux demandés, sinon EventDedupCollision (noms des champs
    seulement, jamais leur contenu)."""
    diverging = [field for field in EVENT_IDENTITY_FIELDS
                 if not _json_equal(getattr(event, field), expected[field])]
    if diverging:
        raise EventDedupCollision(f"event_dedup_key de {event.id} : champs divergents {diverging}")
    return event


def _check_analysis_session_owner(db, analysis_session_id: uuid.UUID, user_id: str) -> None:
    owner = db.execute(
        select(AnalysisSession.user_id).where(AnalysisSession.id == analysis_session_id)
    ).scalar_one_or_none()
    if owner is None:
        raise AnalysisSessionNotFound(str(analysis_session_id))
    if owner != user_id:
        raise AnalysisSessionOwnershipConflict(f"{analysis_session_id} n'appartient pas à {user_id}")


# --------------------------------------------------------------------------
# Verrouillage
# --------------------------------------------------------------------------

def _lock_event(db, event_id) -> CognitiveEvent:
    """Verrouille la ligne (SELECT ... FOR UPDATE), quel que soit son
    statut. populate_existing recharge l'objet déjà présent dans la
    Session : une mutation validée par une autre transaction pendant
    l'attente est vue."""
    _require_uuid(event_id, "event_id")
    event = db.execute(
        select(CognitiveEvent)
        .where(CognitiveEvent.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if event is None:
        raise CognitiveEventNotFound(str(event_id))
    return event


def _lock_open_event(db, event_id) -> CognitiveEvent:
    """Verrouille la ligne puis vérifie qu'elle est open. Le statut n'est lu
    qu'une fois le verrou obtenu : une fermeture validée par une autre
    transaction pendant l'attente est vue."""
    event = _lock_event(db, event_id)
    if event.status != OPEN:
        raise CognitiveEventClosed(f"{event_id} est {event.status}")
    return event


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def open_event_idempotent(
    db,
    *,
    user_id: str,
    conversation_key: str,
    source_turn_refs: tuple,
    segmentation_ordinal: int,
    event_origin: str,
    stimulus_snapshot: dict,
    event_builder_version: str,
    admission_version: str,
    task_kind: str | None = None,
    analysis_session_id: uuid.UUID | None = None,
) -> CognitiveEvent:
    """Ouvre l'événement identifié par (user_id, conversation_key,
    source_turn_refs, segmentation_ordinal, event_builder_version), ou
    retourne celui déjà ouvert sous cette identité.

    Les versions de politique (builder, admission) sont explicites :
    EVENT_BUILDER_VERSION / ADMISSION_VERSION pour la politique courante.
    event_schema_version n'est pas un paramètre : c'est le format écrit par
    ce service (EVENT_SCHEMA_VERSION). task_kind None = démonstration sans
    tâche imposée.

    1. Validation structurelle complète, avant tout accès à la base.
    2. analysis_session_id éventuelle : doit exister et appartenir à
       user_id (lecture seule, avant toute écriture).
    3. conversation_key résolue (ou enregistrée) pour user_id dans le
       registre d'appartenance : ConversationOwnershipConflict sinon.
    4. Clé déjà présente : retour de l'événement existant si ses champs
       structurants sont identiques (quel que soit son statut, aucune
       mutation), EventDedupCollision sinon.
    5. Clé absente : INSERT open (user_work_snapshot vide, stimulus copié)
       isolé dans un SAVEPOINT. Une course perdue sur
       uq_cognitive_events_event_dedup_key revient au SAVEPOINT, relit
       l'événement gagnant (READ COMMITTED) et applique la règle 4 ; toute
       autre IntegrityError est propagée. begin_nested() flushe les écritures
       en attente de l'appelant AVANT d'émettre le SAVEPOINT : celui-ci ne
       contient que cet INSERT et son annulation ne les touche pas. Flush
       sans commit."""
    _require_identifier(user_id, "user_id")
    _require_identifier(conversation_key, "conversation_key")
    _require_identifier(event_origin, "event_origin")
    if task_kind is not None:
        _require_identifier(task_kind, "task_kind")
    if analysis_session_id is not None:
        _require_uuid(analysis_session_id, "analysis_session_id")
    _require_identifier(admission_version, "admission_version")
    stimulus = _json_dict(stimulus_snapshot, "stimulus_snapshot")
    dedup_key = _event_dedup_key(
        user_id=user_id,
        conversation_key=conversation_key,
        source_turn_refs=source_turn_refs,
        segmentation_ordinal=segmentation_ordinal,
        event_builder_version=event_builder_version,
    )

    if analysis_session_id is not None:
        _check_analysis_session_owner(db, analysis_session_id, user_id)
    interaction_identity.register_or_resolve_conversation(db, user_id=user_id, conversation_key=conversation_key)

    expected = {
        "user_id": user_id,
        "conversation_key": conversation_key,
        "event_origin": event_origin,
        "task_kind": task_kind,
        "analysis_session_id": analysis_session_id,
        "stimulus_snapshot": stimulus,
        "event_builder_version": event_builder_version,
        "admission_version": admission_version,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "event_dedup_key": dedup_key,
    }
    existing = _event_by_dedup_key(db, dedup_key)
    if existing is not None:
        return _same_event_or_collision(existing, expected)

    now = _utcnow()
    event = CognitiveEvent(
        status=OPEN,
        user_work_snapshot=[],
        started_at=now,
        updated_at=now,
        closed_at=None,
        **expected,
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError as exc:
        if not _is_dedup_violation(exc):
            raise
        winner = _event_by_dedup_key(db, dedup_key)
        if winner is None:
            # Ligne concurrente invisible (isolation REPEATABLE READ /
            # SERIALIZABLE) : non résoluble sans réessayer la transaction.
            raise
        return _same_event_or_collision(winner, expected)
    return event


def append_user_contribution(
    db,
    *,
    event_id: uuid.UUID,
    contribution_id: uuid.UUID,
    source_turn_ref: uuid.UUID,
    surface: str,
    session_ref: str,
    text_excerpt: str,
    support_refs_before: tuple | None = None,
) -> CognitiveEvent:
    """Ajoute une contribution utilisateur identifiée par contribution_id à
    la fin de user_work_snapshot (append-only), sous le verrou de
    l'événement :

        {"contribution_id", "source_turn_ref", "phase", "surface",
         "session_ref", "occurred_at", "text", "support_refs_before"}

    phase (1, 2, ...) et occurred_at (UTC, = updated_at) sont capturés ici,
    jamais fournis par l'appelant. text_excerpt est l'extrait minimal choisi
    par l'orchestrateur, persisté tel quel (aucune normalisation).

    support_refs_before :
    - None (défaut, comportement historique) : ids de TOUS les SupportTrace
      déjà persistés de l'événement, ORDER BY sequence_no ;
    - tuple d'uuid.UUID (R1-C4) : l'orchestrateur restreint explicitement
      les aides réellement disponibles pour CETTE production (ex. Décrypter :
      celles rendues dans la conversation du tour). Ce service ne décide pas
      de cette disponibilité ; il vérifie sous le verrou que chaque id est
      un SupportTrace déjà persisté de CET événement (sinon
      InvalidCognitivePayload, jamais de référence étrangère ni future) et
      les enregistre dans l'ordre chronologique (sequence_no), sans doublon.
      () = aucune aide disponible.

    - contribution_id déjà présent, champs appelant identiques : no-op
      (aucune mutation, updated_at inchangé, rien de recalculé), même si
      l'événement est fermé ;
    - contribution_id déjà présent, champ divergent :
      ContributionIdentityCollision ;
    - nouveau contribution_id sur un événement fermé : CognitiveEventClosed ;
    - événement legacy (event_schema_version != EVENT_SCHEMA_VERSION) :
      UnsupportedCognitiveEventSchema, jamais de structure mixte.

    La liste est reconstruite puis réassignée : le JSONB n'est pas suivi par
    SQLAlchemy en cas de mutation en place."""
    _require_uuid(event_id, "event_id")
    _require_exact_uuid(contribution_id, "contribution_id")
    _require_exact_uuid(source_turn_ref, "source_turn_ref")
    _require_identifier(surface, "surface")
    _require_identifier(session_ref, "session_ref")
    _require_identifier(text_excerpt, "text_excerpt")
    if support_refs_before is not None:
        if type(support_refs_before) is not tuple:
            raise InvalidCognitivePayload("support_refs_before doit être un tuple ou None")
        for index, ref in enumerate(support_refs_before):
            _require_exact_uuid(ref, f"support_refs_before[{index}]")
        if len(set(support_refs_before)) != len(support_refs_before):
            raise InvalidCognitivePayload("support_refs_before : doublon refusé")
    requested = {
        "contribution_id": str(contribution_id),
        "source_turn_ref": str(source_turn_ref),
        "surface": surface,
        "session_ref": session_ref,
        "text": text_excerpt,
    }

    event = _lock_event(db, event_id)
    if event.event_schema_version != EVENT_SCHEMA_VERSION:
        raise UnsupportedCognitiveEventSchema(
            f"{event_id} : event_schema_version {event.event_schema_version!r}, {EVENT_SCHEMA_VERSION} requis")

    prior = next((item for item in event.user_work_snapshot
                  if item.get("contribution_id") == requested["contribution_id"]), None)
    if prior is not None:
        diverging = [field for field in CONTRIBUTION_CALLER_FIELDS if prior.get(field) != requested[field]]
        if diverging:
            raise ContributionIdentityCollision(
                f"contribution {contribution_id} de {event_id} : champs divergents {diverging}")
        return event
    if event.status != OPEN:
        raise CognitiveEventClosed(f"{event_id} est {event.status}")

    support_refs = db.execute(
        select(SupportTrace.id)
        .where(SupportTrace.cognitive_event_id == event.id)
        .order_by(SupportTrace.sequence_no.asc())
    ).scalars().all()
    if support_refs_before is not None:
        foreign = set(support_refs_before) - set(support_refs)
        if foreign:
            raise InvalidCognitivePayload(
                f"support_refs_before : {len(foreign)} référence(s) hors des aides persistées de {event_id}")
        available = set(support_refs_before)
        support_refs = [ref for ref in support_refs if ref in available]
    now = _utcnow()
    snapshot = list(event.user_work_snapshot)
    snapshot.append({
        "contribution_id": requested["contribution_id"],
        "source_turn_ref": requested["source_turn_ref"],
        "phase": len(snapshot) + 1,
        "surface": surface,
        "session_ref": session_ref,
        "occurred_at": now.isoformat(),
        "text": text_excerpt,
        "support_refs_before": [str(ref) for ref in support_refs],
    })
    event.user_work_snapshot = snapshot
    event.updated_at = now
    db.flush()
    return event


def add_support_trace(
    db,
    *,
    event_id: uuid.UUID,
    support_kind: str,
    support_payload: dict,
) -> SupportTrace:
    """Enregistre une aide réellement montrée à l'utilisateur (jamais une
    preuve de compétence). sequence_no est alloué ici (1, puis MAX + 1) sous
    le verrou de l'événement parent, qui sérialise les ajouts concurrents ;
    UNIQUE(cognitive_event_id, sequence_no) reste une seconde défense."""
    event = _lock_open_event(db, event_id)
    _require_text(support_kind, "support_kind")
    payload = _json_dict(support_payload, "support_payload")

    last = db.execute(
        select(func.max(SupportTrace.sequence_no))
        .where(SupportTrace.cognitive_event_id == event.id)
    ).scalar_one()
    now = _utcnow()
    trace = SupportTrace(
        cognitive_event_id=event.id,
        sequence_no=(last or 0) + 1,
        support_kind=support_kind,
        support_payload=payload,
        created_at=now,
    )
    db.add(trace)
    event.updated_at = now
    db.flush()
    return trace


def _close(db, event_id, status: str) -> CognitiveEvent:
    event = _lock_open_event(db, event_id)
    now = _utcnow()
    event.status = status
    event.closed_at = now
    event.updated_at = now
    db.flush()
    return event


def finalize_event(db, *, event_id: uuid.UUID) -> CognitiveEvent:
    """open -> finalized. Strict : un événement déjà fermé (finalized ou
    abandoned) lève CognitiveEventClosed, jamais de no-op."""
    return _close(db, event_id, FINALIZED)


def abandon_event(db, *, event_id: uuid.UUID) -> CognitiveEvent:
    """open -> abandoned. Strict : un événement déjà fermé lève
    CognitiveEventClosed."""
    return _close(db, event_id, ABANDONED)


def get_event(db, *, event_id: uuid.UUID) -> CognitiveEvent:
    """Lecture simple, sans verrou."""
    _require_uuid(event_id, "event_id")
    event = db.get(CognitiveEvent, event_id)
    if event is None:
        raise CognitiveEventNotFound(str(event_id))
    return event


def get_support_traces(db, *, event_id: uuid.UUID) -> list[SupportTrace]:
    """Aides de l'événement, toujours ORDER BY sequence_no ASC."""
    event = get_event(db, event_id=event_id)
    return list(db.execute(
        select(SupportTrace)
        .where(SupportTrace.cognitive_event_id == event.id)
        .order_by(SupportTrace.sequence_no.asc())
    ).scalars())
