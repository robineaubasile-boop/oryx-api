"""Service interne de capture cognitive (T2-B, niveau 2 : evidence /
provenance).

Seul point d'écriture applicatif de CognitiveEvent et SupportTrace : toute
mutation future de ces tables doit passer par ce module. Il CAPTURE ce que
l'utilisateur a vu et produit, et les aides qui lui ont été montrées ; il
n'évalue rien et n'orchestre rien (quand ouvrir, finaliser ou abandonner un
événement est décidé par l'appelant).

Invariants :

- Transactions : le service ne fait jamais commit() ni rollback(). Il
  valide, verrouille, mute puis flush() ; la frontière transactionnelle
  appartient à l'appelant. Un commit interne validerait aussi les autres
  écritures en attente dans la Session et empêcherait de combiner écritures
  métier et écritures cognitives dans une même transaction atomique.

- Concurrence : toute mutation d'un événement existant commence par
  SELECT ... FOR UPDATE sur sa ligne, AVANT de lire status. Le verrou est
  transactionnel (PostgreSQL) : deux transactions ne peuvent ni travailler
  sur un état obsolète, ni ajouter une production ou une aide pendant une
  fermeture, ni allouer le même sequence_no.

- Lifecycle : open -> finalized, ou open -> abandoned. finalized et
  abandoned sont terminaux : plus aucune production, aide ni transition
  (y compris une seconde finalisation, refusée plutôt qu'ignorée pour
  révéler un bug d'orchestration). stimulus_snapshot est figé à
  l'ouverture ; aucune fonction ne permet de le modifier.

- SupportTrace est append-only : aucune fonction de modification ni de
  suppression. Même une aide erronée reste l'historique de ce qui a
  réellement été montré. support_payload ne doit contenir que ce qui a été
  montré à l'utilisateur (jamais de chain-of-thought, raisonnement interne,
  prompt système ni diagnostic non montré) ; c'est un contrat des
  appelants, que le service n'inspecte pas.

- Payloads : dicts JSON-compatibles, sans schéma métier imposé ({} est
  valide). Ils sont validés puis copiés en profondeur à la capture : une
  mutation ultérieure de l'objet de l'appelant ne change pas l'historique.
  Rien de non JSON-compatible n'est converti silencieusement.

La garantie d'immutabilité est une garantie de ce service (aucun trigger en
base) : elle ne couvre pas une modification directe des objets ORM par un
appelant qui contournerait ce module.
"""
import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from core.models import CognitiveEvent, SupportTrace

OPEN = "open"
FINALIZED = "finalized"
ABANDONED = "abandoned"


class CognitiveCaptureError(Exception):
    """Erreur métier du service de capture cognitive."""


class CognitiveEventNotFound(CognitiveCaptureError):
    """Aucun CognitiveEvent pour cet identifiant."""


class CognitiveEventClosed(CognitiveCaptureError):
    """L'événement est finalized ou abandoned : plus aucune mutation."""


class InvalidCognitivePayload(CognitiveCaptureError):
    """Entrée structurelle invalide : identifiant, chaîne vide, payload qui
    n'est pas un dict JSON-compatible."""


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


# --------------------------------------------------------------------------
# Verrouillage
# --------------------------------------------------------------------------

def _lock_open_event(db, event_id) -> CognitiveEvent:
    """Verrouille la ligne (SELECT ... FOR UPDATE) puis vérifie qu'elle est
    open. Le statut n'est lu qu'une fois le verrou obtenu, et
    populate_existing recharge l'objet déjà présent dans la Session : une
    fermeture validée par une autre transaction pendant l'attente est vue."""
    _require_uuid(event_id, "event_id")
    event = db.execute(
        select(CognitiveEvent)
        .where(CognitiveEvent.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if event is None:
        raise CognitiveEventNotFound(str(event_id))
    if event.status != OPEN:
        raise CognitiveEventClosed(f"{event_id} est {event.status}")
    return event


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def open_event(
    db,
    *,
    user_id: str,
    event_origin: str,
    task_kind: str,
    stimulus_snapshot: dict,
    analysis_session_id: uuid.UUID | None = None,
    conversation_key: str | None = None,
) -> CognitiveEvent:
    """Ouvre un événement (status open, user_work_snapshot vide). Le stimulus
    est copié et figé. event_origin et task_kind sont des vocabulaires
    ouverts. Flush sans commit : l'UUID est matérialisé."""
    _require_text(user_id, "user_id")
    _require_text(event_origin, "event_origin")
    _require_text(task_kind, "task_kind")
    if analysis_session_id is not None:
        _require_uuid(analysis_session_id, "analysis_session_id")
    if conversation_key is not None:
        _require_text(conversation_key, "conversation_key")
    stimulus = _json_dict(stimulus_snapshot, "stimulus_snapshot")

    now = _utcnow()
    event = CognitiveEvent(
        user_id=user_id,
        analysis_session_id=analysis_session_id,
        event_origin=event_origin,
        task_kind=task_kind,
        status=OPEN,
        conversation_key=conversation_key,
        stimulus_snapshot=stimulus,
        user_work_snapshot=[],
        started_at=now,
        updated_at=now,
        closed_at=None,
    )
    db.add(event)
    db.flush()
    return event


def append_user_work(db, *, event_id: uuid.UUID, work: dict) -> CognitiveEvent:
    """Ajoute une production utilisateur à la fin de user_work_snapshot
    (append-only). La liste est reconstruite puis réassignée : le JSONB
    n'est pas suivi par SQLAlchemy en cas de mutation en place."""
    event = _lock_open_event(db, event_id)
    item = _json_dict(work, "work")

    snapshot = list(event.user_work_snapshot)
    snapshot.append(item)
    event.user_work_snapshot = snapshot
    event.updated_at = _utcnow()
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
