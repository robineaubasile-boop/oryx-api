"""Commandes typées de la frontière d'autorisation (BF1) et plans de verrous (BF2).

Une commande est une DONNÉE immuable, jamais du code : le runner
(core.authz_f2.runner) n'accepte que les types exacts du registre fermé
core.authz_f2._adapters.REGISTRY, chacun associé à un plan de verrouillage
et à des adaptateurs SQL privés audités. Aucune commande ne porte de
user_id : le propriétaire faisant autorité est toujours
AuthorizedPrincipal.user_id. Les identifiants de ressource (position_id,
ticker, assistant_turn_id, conversation_key) ne sont que des filtres
SUPPLÉMENTAIRES, toujours combinés à ce user_id.

Plans (ordre global F2 : ... -> users -> ... -> session -> génération ;
les tables métiers, absentes de l'ordre F2, viennent après) :

- READ           : users FOR KEY SHARE -> oryx_sessions FOR SHARE ->
                   oryx_session_generations FOR SHARE ; aucun verrou métier ;
- USER_MUTATION  : users FOR UPDATE -> oryx_sessions FOR SHARE ->
                   oryx_session_generations FOR SHARE -> verrous métiers de
                   la commande (lignes filtrées par id ET user_id).

PR 5 ne livre que des lectures et UNE mutation de démonstration interne
(UpdatePositionQuantity), sans route HTTP : les créations et mutations de
profil, portefeuille, thèses, Décrypter et ACK relèvent de PR 6 à PR 9.
"""
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

MAX_POSITION_ID = 2**31 - 1
TICKER_MAX_CHARS = 32
CONVERSATION_KEY_MAX_CHARS = 200


@dataclass(frozen=True, slots=True)
class LockPlan:
    name: str
    users_lock: str  # mode du verrou de rang users, pris EN PREMIER en phase B
    business_locks: tuple  # tables métiers verrouillées après la génération, dans cet ordre


READ = LockPlan("read", "FOR KEY SHARE", ())
USER_MUTATION = LockPlan("user_mutation", "FOR UPDATE", ("portfolio_positions",))
PLANS = (READ, USER_MUTATION)
SESSION_LOCK = "FOR SHARE"
GENERATION_LOCK = "FOR SHARE"


class Command:
    """Base des commandes du registre fermé (données pures)."""

    __slots__ = ()


def _position_id(value) -> int:
    if type(value) is not int or not 1 <= value <= MAX_POSITION_ID:
        raise ValueError("position_id : entier strictement positif attendu")
    return value


def _bounded_text(value, max_chars: int, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > max_chars or value != value.strip():
        raise ValueError(f"{label} : chaîne non vide, sans blanc de bord, {max_chars} caractères au plus")
    return value


@dataclass(frozen=True, slots=True)
class ReadProfile(Command):
    pass


@dataclass(frozen=True, slots=True)
class ListPositions(Command):
    pass


@dataclass(frozen=True, slots=True)
class ReadPosition(Command):
    position_id: int

    def __post_init__(self):
        _position_id(self.position_id)


@dataclass(frozen=True, slots=True)
class ListTheses(Command):
    pass


@dataclass(frozen=True, slots=True)
class ListThesesForTicker(Command):
    ticker: str

    def __post_init__(self):
        _bounded_text(self.ticker, TICKER_MAX_CHARS, "ticker")


@dataclass(frozen=True, slots=True)
class ReadDelivery(Command):
    assistant_turn_id: uuid.UUID

    def __post_init__(self):
        if not isinstance(self.assistant_turn_id, uuid.UUID):
            raise ValueError("assistant_turn_id : uuid.UUID attendu")


@dataclass(frozen=True, slots=True)
class ListConversationDeliveries(Command):
    conversation_key: str

    def __post_init__(self):
        _bounded_text(self.conversation_key, CONVERSATION_KEY_MAX_CHARS, "conversation_key")


@dataclass(frozen=True, slots=True)
class UpdatePositionQuantity(Command):
    """Mutation de DÉMONSTRATION (interne, sans route) : prouve le plan
    USER_MUTATION, l'appartenance sous verrou et le rollback intégral."""
    position_id: int
    quantity: float

    def __post_init__(self):
        _position_id(self.position_id)
        if type(self.quantity) not in (int, float) or not math.isfinite(self.quantity) or self.quantity <= 0:
            raise ValueError("quantity : nombre fini strictement positif attendu")


# --------------------------------------------------------------------------
# Vues rendues (aucune métadonnée privée, aucun secret)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ProfileView:
    user_id: str
    level: str


@dataclass(frozen=True, slots=True)
class PositionView:
    position_id: int
    ticker: str
    quantity: float
    purchase_price: object
    target_percent: object
    envelope: object


@dataclass(frozen=True, slots=True)
class ThesisView:
    thesis_id: int
    ticker: str
    thesis_text: str
    created_at: object


@dataclass(frozen=True, slots=True)
class DeliveryView:
    """Sans private_metadata, request_fingerprint ni response_payload."""
    assistant_turn_id: uuid.UUID
    conversation_key: str
    surface: str
    status: str
    generated_at: datetime
