"""Adaptateurs SQL PRIVÉS et audités de la frontière d'autorisation (BF1).

Seul module de core/authz_f2 qui exécute du SQL. Chaque adaptateur reçoit
la connexion du runner pour UNE opération prévue, écrite ici en constante ;
aucun ne fait commit(), rollback(), begin() ni SET, aucun ne compose du SQL
à partir d'une donnée, aucun n'appelle le réseau. Le propriétaire est
toujours un user_id serveur : celui de la pré-validation pour les verrous
pris AVANT T_authorization (confirmé ensuite par la décision), celui du
principal autorisé après.

Inventaire (vérifié par tests/test_authz_f2_boundary.py) :

| Opération | Table | Filtre | Verrou |
|---|---|---|---|
| lock_profile | users | id | plan (FOR KEY SHARE / FOR UPDATE) |
| _read_profile | users | id | - |
| _list_positions | portfolio_positions | user_id | - |
| _read_position | portfolio_positions | id ET user_id | - |
| _list_theses | investment_theses | user_id | - |
| _list_theses_for_ticker | investment_theses | user_id ET ticker | - |
| _read_delivery | assistant_deliveries | id ET user_id | - |
| _list_conversation_deliveries | assistant_deliveries | user_id ET conversation_key | - |
| _lock_position | portfolio_positions | id ET user_id | FOR UPDATE |
| _update_position_quantity | portfolio_positions | id ET user_id | (ligne déjà verrouillée) |

Une ressource étrangère et une ressource inexistante rendent le même
ABSENT : le filtre SQL porte simultanément l'identifiant et le user_id, la
ligne d'autrui n'est ni lue ni verrouillée.
"""
from dataclasses import dataclass
from datetime import timezone
from types import MappingProxyType

import sqlalchemy as sa

from core.authz_f2 import commands as c


class _Absent:
    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT = _Absent()


class AdapterInvariantBroken(RuntimeError):
    """État impossible constaté sous verrou : la transaction est annulée."""


_LOCK_PROFILE = {
    "FOR KEY SHARE": "SELECT id FROM users WHERE id = :u FOR KEY SHARE",
    "FOR UPDATE": "SELECT id FROM users WHERE id = :u FOR UPDATE",
}
_READ_PROFILE = "SELECT id, level FROM users WHERE id = :u"
_POSITION_COLUMNS = "SELECT id, ticker, quantity, purchase_price, target_percent, envelope FROM portfolio_positions "
_LIST_POSITIONS = _POSITION_COLUMNS + "WHERE user_id = :u ORDER BY id"
_READ_POSITION = _POSITION_COLUMNS + "WHERE id = :id AND user_id = :u"
_THESIS_COLUMNS = "SELECT id, ticker, thesis_text, created_at FROM investment_theses "
_LIST_THESES = _THESIS_COLUMNS + "WHERE user_id = :u ORDER BY id"
_LIST_THESES_FOR_TICKER = _THESIS_COLUMNS + "WHERE user_id = :u AND ticker = :t ORDER BY id"
_DELIVERY_COLUMNS = "SELECT id, conversation_key, surface, status, generated_at FROM assistant_deliveries "
_READ_DELIVERY = _DELIVERY_COLUMNS + "WHERE id = :id AND user_id = :u"
_LIST_CONVERSATION_DELIVERIES = _DELIVERY_COLUMNS + "WHERE user_id = :u AND conversation_key = :k ORDER BY generated_at, id"
_LOCK_POSITION = "SELECT id FROM portfolio_positions WHERE id = :id AND user_id = :u FOR UPDATE"
_UPDATE_POSITION_QUANTITY = ("UPDATE portfolio_positions SET quantity = :q, updated_at = :t "
                             "WHERE id = :id AND user_id = :u")


def _rows(conn, sql: str, **params):
    return conn.execute(sa.text(sql), params).mappings().all()


def _first(conn, sql: str, **params):
    return conn.execute(sa.text(sql), params).mappings().first()


def lock_profile(conn, user_id: str, plan: c.LockPlan) -> bool:
    """Premier verrou de la phase B (rang users). False : profil absent."""
    return _first(conn, _LOCK_PROFILE[plan.users_lock], u=user_id) is not None


def _position(row) -> c.PositionView:
    return c.PositionView(row["id"], row["ticker"], row["quantity"], row["purchase_price"], row["target_percent"],
                          row["envelope"])


def _thesis(row) -> c.ThesisView:
    return c.ThesisView(row["id"], row["ticker"], row["thesis_text"], row["created_at"])


def _delivery(row) -> c.DeliveryView:
    return c.DeliveryView(row["id"], row["conversation_key"], row["surface"], row["status"], row["generated_at"])


# --------------------------------------------------------------------------
# Verrous métiers (avant T_authorization) et exécution (après la décision)
# --------------------------------------------------------------------------

def _no_business_lock(conn, owner_id, command):
    return None


def _read_profile(conn, ctx, command, locked):
    row = _first(conn, _READ_PROFILE, u=ctx.principal.user_id)
    if row is None:  # verrouillé par lock_profile dans cette transaction
        raise AdapterInvariantBroken("profil verrouillé introuvable")
    return c.ProfileView(row["id"], row["level"])


def _list_positions(conn, ctx, command, locked):
    return tuple(_position(r) for r in _rows(conn, _LIST_POSITIONS, u=ctx.principal.user_id))


def _read_position(conn, ctx, command, locked):
    row = _first(conn, _READ_POSITION, id=command.position_id, u=ctx.principal.user_id)
    return ABSENT if row is None else _position(row)


def _list_theses(conn, ctx, command, locked):
    return tuple(_thesis(r) for r in _rows(conn, _LIST_THESES, u=ctx.principal.user_id))


def _list_theses_for_ticker(conn, ctx, command, locked):
    return tuple(_thesis(r) for r in _rows(conn, _LIST_THESES_FOR_TICKER, u=ctx.principal.user_id,
                                           t=command.ticker))


def _read_delivery(conn, ctx, command, locked):
    row = _first(conn, _READ_DELIVERY, id=command.assistant_turn_id, u=ctx.principal.user_id)
    return ABSENT if row is None else _delivery(row)


def _list_conversation_deliveries(conn, ctx, command, locked):
    return tuple(_delivery(r) for r in _rows(conn, _LIST_CONVERSATION_DELIVERIES, u=ctx.principal.user_id,
                                             k=command.conversation_key))


def _lock_position(conn, owner_id, command):
    row = _first(conn, _LOCK_POSITION, id=command.position_id, u=owner_id)
    return ABSENT if row is None else row["id"]


def _update_position_quantity(conn, ctx, command, locked):
    updated_at = ctx.authorized_at.astimezone(timezone.utc).replace(tzinfo=None)  # colonne historique sans fuseau
    result = conn.execute(sa.text(_UPDATE_POSITION_QUANTITY), {"q": float(command.quantity), "t": updated_at,
                                                                "id": locked, "u": ctx.principal.user_id})
    if result.rowcount != 1:
        raise AdapterInvariantBroken("position verrouillée introuvable")
    return _position(_first(conn, _READ_POSITION, id=locked, u=ctx.principal.user_id))


@dataclass(frozen=True)
class Spec:
    plan: c.LockPlan
    lock: object     # (conn, owner_id, command) -> état verrouillé | ABSENT ; avant T_authorization
    execute: object  # (conn, ctx, command, état) -> vue | ABSENT ; après la décision


# Registre FERMÉ : type exact de commande -> plan et adaptateurs. Toute
# commande absente (ou sous-classe) est refusée par le runner.
REGISTRY = MappingProxyType({
    c.ReadProfile: Spec(c.READ, _no_business_lock, _read_profile),
    c.ListPositions: Spec(c.READ, _no_business_lock, _list_positions),
    c.ReadPosition: Spec(c.READ, _no_business_lock, _read_position),
    c.ListTheses: Spec(c.READ, _no_business_lock, _list_theses),
    c.ListThesesForTicker: Spec(c.READ, _no_business_lock, _list_theses_for_ticker),
    c.ReadDelivery: Spec(c.READ, _no_business_lock, _read_delivery),
    c.ListConversationDeliveries: Spec(c.READ, _no_business_lock, _list_conversation_deliveries),
    c.UpdatePositionQuantity: Spec(c.USER_MUTATION, _lock_position, _update_position_quantity),
})
