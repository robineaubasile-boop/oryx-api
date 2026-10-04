"""Service interne des livraisons de réponses assistant (R1-C1).

Seul point d'écriture applicatif d'AssistantDelivery. Il répond à une seule
question : identifier de manière idempotente un tour utilisateur et la
réponse Oryx canonique produite pour lui, puis établir que le frontend
first-party l'a effectivement rendue (pending -> delivered).

AssistantDelivery est une infrastructure runtime : ni SupportTrace, ni
CognitiveEvent, ni observation, ni preuve utilisateur. Ce module n'écrit
aucune table pédagogique et n'appelle aucun service T2+ (capture cognitive,
observations, longitudinal, inférence). delivered signifie seulement « le
frontend Oryx affirme avoir inséré le contenu canonique dans l'interface » ;
jamais lu, compris, regardé, mémorisé ni utilisé cognitivement.

API publique : preflight_delivery, claim_delivery, bind_analysis_session,
acknowledge_delivery, decryptage_request_fingerprint,
visible_content_fingerprint. Aucune fonction générique de mise à jour, de
suppression ni de listing.

Invariants :

- Transactions : le service ne fait jamais commit() ni rollback() de la
  transaction de l'appelant ; il valide, lit, verrouille, insère ou mute
  puis flush(). Seule exception, locale et invisible pour l'appelant :
  l'INSERT de claim_delivery est isolé dans un SAVEPOINT (begin_nested)
  pour traduire une course perdue sur uq_assistant_deliveries_turn_ordinal
  sans rendre la transaction de l'appelant inutilisable. SAVEPOINT local
  != ownership de la transaction : un rollback de l'appelant annule aussi
  la création. Aucune transaction n'est censée rester ouverte pendant un
  appel externe : c'est la responsabilité de l'orchestrateur (route).

- Identité logique : (user_id, conversation_key, surface,
  source_user_turn_id, delivery_ordinal). La contrainte UNIQUE est la
  défense finale. id (assistant_turn_id) est aléatoire, généré ici, et ne
  sert jamais à la déduplication.

- Retry : même identité + même request_fingerprint => la livraison
  canonique existante (son response_payload persisté, jamais recalculé).
  Même identité + request_fingerprint différent =>
  AssistantTurnIdentityCollision. Deux générations concurrentes pour le
  même tour (même request_fingerprint, réponses différentes) ne sont PAS
  une collision : la première ligne commitée gagne, la réponse du perdant
  est jetée et ne devient jamais canonique.

- Conversation : la conversation_key doit être résolue (ou enregistrée)
  pour le même user_id dans le registre R1-B (core/interaction_identity.py) ;
  sinon AssistantDeliveryOwnershipConflict. Le user_id doit exister (aucune
  création implicite d'utilisateur).

- Immutabilité : après le claim, response_payload, request_fingerprint,
  visible_content_fingerprint, private_metadata et generated_at ne
  changent plus. acknowledge_delivery ne mute que status / delivered_at ;
  bind_analysis_session ne mute que analysis_session_id (jamais de
  remapping).

- Versions : toute nouvelle livraison porte ASSISTANT_DELIVERY_SCHEMA_VERSION ;
  une ligne d'une autre version n'est jamais lue de manière permissive
  (UnsupportedAssistantDeliverySchema).

- Données : les messages d'erreur ne contiennent que des identifiants
  techniques et des noms de champs, jamais de texte utilisateur, de
  response_payload ni de private_metadata.
"""
import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core import interaction_identity
from core.models import AnalysisSession, AssistantDelivery, User

ASSISTANT_DELIVERY_SCHEMA_VERSION = "assistant-delivery-v1"
DECRYPTAGE_REQUEST_FINGERPRINT_SCHEMA_VERSION = "decryptage-request-fingerprint-v2"

PENDING = "pending"
DELIVERED = "delivered"

# Surface pilote unique de R1-C1, imposée par le serveur (jamais choisie par
# le client). Aucune autre surface n'est branchée.
DECRYPTAGE_SURFACE = "decryptage"
SUPPORTED_SURFACES = (DECRYPTAGE_SURFACE,)

TURN_UNIQUE_CONSTRAINT = "uq_assistant_deliveries_turn_ordinal"

# Clés réservées à la réponse HTTP ou au stockage privé : jamais dans le
# payload public canonique.
RESERVED_RESPONSE_KEYS = ("assistant_turn_id", "delivery_status", "private_metadata")

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class AssistantDeliveryError(Exception):
    """Erreur métier du service de livraison des réponses assistant."""


class InvalidAssistantDeliveryInput(AssistantDeliveryError):
    """Entrée structurelle invalide (identifiant, surface, ordinal,
    empreinte, payload non JSON-compatible...)."""


class AssistantDeliveryUserNotFound(AssistantDeliveryError):
    """user_id ne désigne aucun User (aucune création implicite)."""


class AssistantDeliveryNotFound(AssistantDeliveryError):
    """Aucune AssistantDelivery pour cet assistant_turn_id."""


class AssistantTurnIdentityCollision(AssistantDeliveryError):
    """Même identité logique de livraison qu'une livraison existante, mais
    request_fingerprint différent : ce n'est pas un retry du même tour."""


class AssistantDeliveryOwnershipConflict(AssistantDeliveryError):
    """La conversation ou la livraison appartient à un autre user_id /
    conversation_key."""


class UnsupportedAssistantDeliverySchema(AssistantDeliveryError):
    """delivery_schema_version inconnue : jamais lue de manière permissive."""


class AnalysisSessionBindingConflict(AssistantDeliveryError):
    """Liaison analysis_session refusée : session inconnue, d'un autre
    utilisateur, ou livraison déjà liée à une autre session."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _require_identifier(value, name: str) -> str:
    """str non vide (après strip), sans NUL, encodable en UTF-8 ; jamais
    normalisée (persistée et hashée telle quelle)."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidAssistantDeliveryInput(f"{name} doit être une chaîne non vide")
    if "\x00" in value:
        raise InvalidAssistantDeliveryInput(f"{name} : caractère NUL refusé")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidAssistantDeliveryInput(f"{name} : chaîne non encodable en UTF-8") from None
    return value


def _require_text(value, name: str) -> str:
    """str (éventuellement vide), sans NUL, encodable en UTF-8."""
    if type(value) is not str:
        raise InvalidAssistantDeliveryInput(f"{name} doit être une chaîne")
    if "\x00" in value:
        raise InvalidAssistantDeliveryInput(f"{name} : caractère NUL refusé")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidAssistantDeliveryInput(f"{name} : chaîne non encodable en UTF-8") from None
    return value


def _require_uuid(value, name: str) -> uuid.UUID:
    if type(value) is not uuid.UUID:
        raise InvalidAssistantDeliveryInput(f"{name} doit être un uuid.UUID")
    return value


def _require_surface(value) -> str:
    if type(value) is not str or value not in SUPPORTED_SURFACES:
        raise InvalidAssistantDeliveryInput(f"surface non supportée (attendu : {list(SUPPORTED_SURFACES)})")
    return value


def _require_ordinal(value) -> int:
    if type(value) is not int or value < 1:
        raise InvalidAssistantDeliveryInput("delivery_ordinal doit être un int >= 1")
    return value


def _require_fingerprint(value, name: str) -> str:
    if type(value) is not str or not _SHA256_HEX.fullmatch(value):
        raise InvalidAssistantDeliveryInput(f"{name} doit être un SHA-256 hexadécimal minuscule (64 caractères)")
    return value


def _require_schema_version(value) -> str:
    if type(value) is not str or value != ASSISTANT_DELIVERY_SCHEMA_VERSION:
        raise UnsupportedAssistantDeliverySchema(
            f"delivery_schema_version non supportée (attendu : {ASSISTANT_DELIVERY_SCHEMA_VERSION})")
    return value


def _json_copy(value, path: str, active: frozenset):
    """Copie profonde d'une valeur JSON-compatible, ou
    InvalidAssistantDeliveryInput. Types exacts pour les scalaires : rien
    n'est converti silencieusement (Decimal, datetime, tuple, NaN...).
    PostgreSQL refuse \\u0000 dans un JSONB : refusé ici plutôt qu'au flush."""
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InvalidAssistantDeliveryInput(f"{path} : nombre non fini refusé")
        return value
    if type(value) is str:
        if "\x00" in value:
            raise InvalidAssistantDeliveryInput(f"{path} : caractère NUL refusé")
        return value
    if type(value) in (dict, list):
        if id(value) in active:
            raise InvalidAssistantDeliveryInput(f"{path} : référence circulaire")
        active = active | {id(value)}
        if type(value) is list:
            return [_json_copy(item, f"{path}[{i}]", active) for i, item in enumerate(value)]
        copy = {}
        for key, item in value.items():
            if type(key) is not str or "\x00" in key:
                raise InvalidAssistantDeliveryInput(f"{path} : clé refusée (str sans NUL attendue)")
            copy[key] = _json_copy(item, f"{path}.{key}", active)
        return copy
    raise InvalidAssistantDeliveryInput(f"{path} : type {type(value).__name__} non JSON-compatible")


def _json_dict(value, name: str) -> dict:
    if type(value) is not dict:
        raise InvalidAssistantDeliveryInput(f"{name} doit être un dict")
    return _json_copy(value, name, frozenset())


def _require_response_payload(value) -> dict:
    payload = _json_dict(value, "response_payload")
    reserved = [key for key in RESERVED_RESPONSE_KEYS if key in payload]
    if reserved:
        raise InvalidAssistantDeliveryInput(f"response_payload : clés réservées refusées {reserved}")
    return payload


def _validate_identity(*, user_id, conversation_key, surface, source_user_turn_id, delivery_ordinal) -> None:
    _require_identifier(user_id, "user_id")
    _require_identifier(conversation_key, "conversation_key")
    _require_surface(surface)
    _require_uuid(source_user_turn_id, "source_user_turn_id")
    _require_ordinal(delivery_ordinal)


# --------------------------------------------------------------------------
# Empreintes
# --------------------------------------------------------------------------

def _sha256_canonical_json(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decryptage_request_fingerprint(
    *,
    user_id: str,
    conversation_key: str,
    client_turn_id: uuid.UUID,
    ticker_input: str,
    question: str,
    context: str,
    last_method_id: str | None,
    level: str,
) -> str:
    """SHA-256 (hex minuscule) du JSON canonique (sort_keys, séparateurs
    compacts, ensure_ascii=False, UTF-8) du payload utilisateur logique de
    /decryptage. Fonction 100 % PURE (aucune I/O) : calculable avant tout
    appel externe, donc avant le preflight.

    ticker_input est la SAISIE du ticker avec une normalisation locale
    déterministe uniquement (strip().upper()) ; ce n'est PAS le ticker
    canonique EODHD : la résolution externe (normalize_ticker, recherche
    EODHD) ne fait pas partie de l'identité d'un retry. Deux saisies
    différentes avec le même client_turn_id sont donc une collision, même si
    elles résoudraient vers le même instrument. question / context sont
    strippés par l'appelant ; last_method_id et level sont les valeurs
    effectives. Aucune donnée de marché, aucun horodatage, aucune réponse
    générée, aucun identifiant serveur ni aléa (pas de hash() Python)."""
    if type(ticker_input) is not str:
        raise InvalidAssistantDeliveryInput("ticker_input doit être une chaîne")
    ticker_input = _require_identifier(ticker_input.strip().upper(), "ticker_input")
    if last_method_id is not None:
        _require_text(last_method_id, "last_method_id")
    payload = {
        "schema_version": DECRYPTAGE_REQUEST_FINGERPRINT_SCHEMA_VERSION,
        "user_id": _require_identifier(user_id, "user_id"),
        "conversation_key": _require_identifier(conversation_key, "conversation_key"),
        "client_turn_id": str(_require_uuid(client_turn_id, "client_turn_id")),
        "ticker_input": ticker_input,
        "question": _require_text(question, "question"),
        "context": _require_text(context, "context"),
        "last_method_id": last_method_id,
        "level": _require_text(level, "level"),
    }
    return _sha256_canonical_json(payload)


def visible_content_fingerprint(text: str) -> str:
    """SHA-256 (hex minuscule) du texte exact destiné à l'affichage (après
    retrait de tout marqueur privé), encodé en UTF-8. Empreinte d'intégrité
    du contenu canonique serveur : jamais une preuve de rendu ni de
    lecture."""
    return hashlib.sha256(_require_text(text, "text").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Lectures internes
# --------------------------------------------------------------------------

def _require_existing_user(db, user_id: str) -> None:
    if db.execute(select(User.id).where(User.id == user_id)).scalar_one_or_none() is None:
        raise AssistantDeliveryUserNotFound("user_id inconnu")


def _resolve_conversation(db, *, user_id: str, conversation_key: str) -> None:
    """Résout (ou enregistre) conversation_key pour user_id dans le registre
    R1-B ; une clé d'un autre utilisateur est refusée (jamais réattribuée)."""
    try:
        interaction_identity.register_or_resolve_conversation(
            db, user_id=user_id, conversation_key=conversation_key)
    except interaction_identity.ConversationOwnershipConflict:
        raise AssistantDeliveryOwnershipConflict("conversation_key appartient à un autre utilisateur") from None


def _delivery_by_identity(db, *, user_id, conversation_key, surface, source_user_turn_id,
                          delivery_ordinal) -> AssistantDelivery | None:
    """populate_existing : une ligne déjà présente dans la Session est
    rechargée (READ COMMITTED : la ligne gagnante d'une course est vue)."""
    return db.execute(
        select(AssistantDelivery)
        .where(
            AssistantDelivery.user_id == user_id,
            AssistantDelivery.conversation_key == conversation_key,
            AssistantDelivery.surface == surface,
            AssistantDelivery.source_user_turn_id == source_user_turn_id,
            AssistantDelivery.delivery_ordinal == delivery_ordinal,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _same_request_or_collision(delivery: AssistantDelivery, request_fingerprint: str) -> AssistantDelivery:
    """Livraison existante pour la même identité logique : retournée si la
    requête est la même (quel que soit son statut, sans mutation), sinon
    AssistantTurnIdentityCollision. Le contenu généré n'est jamais comparé."""
    _require_schema_version(delivery.delivery_schema_version)
    if delivery.request_fingerprint != request_fingerprint:
        raise AssistantTurnIdentityCollision(f"assistant turn {delivery.id} : request_fingerprint divergent")
    return delivery


def _is_turn_unique_violation(exc: IntegrityError) -> bool:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == TURN_UNIQUE_CONSTRAINT


def _lock_delivery(db, delivery_id) -> AssistantDelivery:
    """SELECT ... FOR UPDATE de la livraison ; populate_existing recharge un
    objet déjà présent dans la Session (mutation concurrente vue)."""
    _require_uuid(delivery_id, "assistant_turn_id")
    delivery = db.execute(
        select(AssistantDelivery)
        .where(AssistantDelivery.id == delivery_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if delivery is None:
        raise AssistantDeliveryNotFound(str(delivery_id))
    _require_schema_version(delivery.delivery_schema_version)
    return delivery


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------

def preflight_delivery(
    db,
    *,
    user_id: str,
    conversation_key: str,
    surface: str,
    source_user_turn_id: uuid.UUID,
    delivery_ordinal: int,
    request_fingerprint: str,
) -> AssistantDelivery | None:
    """Contrôle préalable à TOUT appel externe (données financières, LLM).

    1. Validation structurelle complète, avant tout accès à la base.
    2. User existant (AssistantDeliveryUserNotFound sinon).
    3. conversation_key résolue / enregistrée pour user_id
       (AssistantDeliveryOwnershipConflict sinon).
    4. Livraison de cette identité logique : None si absente (nouvelle
       génération autorisée), la livraison canonique si request_fingerprint
       identique, AssistantTurnIdentityCollision sinon.

    Aucune écriture hors registre de conversation ; flush sans commit."""
    _validate_identity(user_id=user_id, conversation_key=conversation_key, surface=surface,
                       source_user_turn_id=source_user_turn_id, delivery_ordinal=delivery_ordinal)
    _require_fingerprint(request_fingerprint, "request_fingerprint")

    _require_existing_user(db, user_id)
    _resolve_conversation(db, user_id=user_id, conversation_key=conversation_key)
    existing = _delivery_by_identity(db, user_id=user_id, conversation_key=conversation_key, surface=surface,
                                     source_user_turn_id=source_user_turn_id, delivery_ordinal=delivery_ordinal)
    if existing is None:
        return None
    return _same_request_or_collision(existing, request_fingerprint)


def claim_delivery(
    db,
    *,
    user_id: str,
    conversation_key: str,
    surface: str,
    source_user_turn_id: uuid.UUID,
    delivery_ordinal: int,
    request_fingerprint: str,
    visible_content_fingerprint: str,
    response_payload: dict,
    private_metadata: dict,
    delivery_schema_version: str,
) -> tuple[AssistantDelivery, bool]:
    """Revendique la livraison canonique de ce tour. Retourne
    (livraison, created) : created=True si CET appel l'a créée (pending),
    False si une livraison canonique existait déjà (retry ou course perdue).

    1. Validation structurelle complète, payloads copiés en profondeur.
    2. User existant ; conversation_key résolue pour user_id.
    3. Identité déjà présente : règle de _same_request_or_collision.
    4. Sinon INSERT pending (generated_at = maintenant UTC, delivered_at
       NULL) isolé dans un SAVEPOINT. Une course perdue sur
       uq_assistant_deliveries_turn_ordinal revient au SAVEPOINT, relit la
       ligne gagnante (READ COMMITTED) et applique la règle 3 ; toute autre
       IntegrityError est propagée. begin_nested() flushe les écritures en
       attente de l'appelant AVANT d'émettre le SAVEPOINT : celui-ci ne
       contient que cet INSERT. Flush sans commit."""
    _validate_identity(user_id=user_id, conversation_key=conversation_key, surface=surface,
                       source_user_turn_id=source_user_turn_id, delivery_ordinal=delivery_ordinal)
    _require_fingerprint(request_fingerprint, "request_fingerprint")
    _require_fingerprint(visible_content_fingerprint, "visible_content_fingerprint")
    payload = _require_response_payload(response_payload)
    private = _json_dict(private_metadata, "private_metadata")
    _require_schema_version(delivery_schema_version)

    _require_existing_user(db, user_id)
    _resolve_conversation(db, user_id=user_id, conversation_key=conversation_key)
    identity = {
        "user_id": user_id,
        "conversation_key": conversation_key,
        "surface": surface,
        "source_user_turn_id": source_user_turn_id,
        "delivery_ordinal": delivery_ordinal,
    }
    existing = _delivery_by_identity(db, **identity)
    if existing is not None:
        return _same_request_or_collision(existing, request_fingerprint), False

    delivery = AssistantDelivery(
        id=uuid.uuid4(),
        analysis_session_id=None,
        request_fingerprint=request_fingerprint,
        visible_content_fingerprint=visible_content_fingerprint,
        response_payload=payload,
        private_metadata=private,
        status=PENDING,
        delivery_schema_version=delivery_schema_version,
        generated_at=_utcnow(),
        delivered_at=None,
        **identity,
    )
    try:
        with db.begin_nested():
            db.add(delivery)
            db.flush()
    except IntegrityError as exc:
        if not _is_turn_unique_violation(exc):
            raise
        winner = _delivery_by_identity(db, **identity)
        if winner is None:
            # Ligne concurrente invisible (isolation REPEATABLE READ /
            # SERIALIZABLE) : non résoluble sans réessayer la transaction.
            raise
        return _same_request_or_collision(winner, request_fingerprint), False
    return delivery, True


def bind_analysis_session(
    db,
    *,
    delivery_id: uuid.UUID,
    analysis_session_id: uuid.UUID,
) -> AssistantDelivery:
    """Lie la livraison (pending ou delivered) à l'AnalysisSession produit
    mise à jour pour ce tour, sous le verrou de la livraison.

    - session inconnue ou d'un autre utilisateur : AnalysisSessionBindingConflict ;
    - analysis_session_id NULL : renseignée ;
    - déjà la même : no-op idempotent ;
    - déjà une autre : AnalysisSessionBindingConflict (aucun remapping).

    Seule analysis_session_id est mutée ; flush sans commit."""
    _require_uuid(delivery_id, "delivery_id")
    _require_uuid(analysis_session_id, "analysis_session_id")
    delivery = _lock_delivery(db, delivery_id)
    owner = db.execute(
        select(AnalysisSession.user_id).where(AnalysisSession.id == analysis_session_id)
    ).scalar_one_or_none()
    if owner is None:
        raise AnalysisSessionBindingConflict(f"analysis_session {analysis_session_id} inconnue")
    if owner != delivery.user_id:
        raise AnalysisSessionBindingConflict(
            f"analysis_session {analysis_session_id} n'appartient pas au propriétaire de {delivery.id}")
    if delivery.analysis_session_id == analysis_session_id:
        return delivery
    if delivery.analysis_session_id is not None:
        raise AnalysisSessionBindingConflict(f"assistant turn {delivery.id} déjà lié à une autre analysis_session")
    delivery.analysis_session_id = analysis_session_id
    db.flush()
    return delivery


def acknowledge_delivery(
    db,
    *,
    assistant_turn_id: uuid.UUID,
    user_id: str,
    conversation_key: str,
) -> AssistantDelivery:
    """ACK du frontend first-party : la réponse canonique a été insérée
    dans l'interface. Sous le verrou de la livraison :

    - inconnue : AssistantDeliveryNotFound ;
    - user_id ou conversation_key différent : AssistantDeliveryOwnershipConflict ;
    - pending : delivered, delivered_at = maintenant UTC ;
    - delivered : no-op idempotent (delivered_at historique inchangé).

    Ne prouve ni lecture, ni attention, ni compréhension. Ne crée aucune
    livraison, aucune trace pédagogique ; seuls status / delivered_at sont
    mutés. Flush sans commit."""
    _require_uuid(assistant_turn_id, "assistant_turn_id")
    _require_identifier(user_id, "user_id")
    _require_identifier(conversation_key, "conversation_key")
    delivery = _lock_delivery(db, assistant_turn_id)
    if delivery.user_id != user_id or delivery.conversation_key != conversation_key:
        raise AssistantDeliveryOwnershipConflict(f"assistant turn {assistant_turn_id} : propriétaire différent")
    if delivery.status == DELIVERED:
        return delivery
    delivery.status = DELIVERED
    delivery.delivered_at = _utcnow()
    db.flush()
    return delivery
