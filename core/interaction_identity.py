"""Registre d'appartenance des conversations produit (R1-B).

Seul point d'écriture applicatif de ConversationIdentity. Il garantit,
y compris sous concurrence, qu'une conversation_key appartient à exactement
un user_id et ne change jamais de propriétaire (R1-A). Ce n'est pas un
système de conversation : aucun message, historique, surface ni lifecycle.

Invariants :

- Idempotence : register_or_resolve_conversation crée la ligne si la clé
  est absente, retourne la ligne existante si elle appartient au même
  user_id, et lève ConversationOwnershipConflict sinon (jamais de
  réattribution silencieuse). Aucune fonction de modification ni de
  suppression.

- Transactions : le service ne fait jamais commit() ni rollback() de la
  transaction de l'appelant ; il valide, lit, insère puis flush(). Seule
  exception, locale et invisible pour l'appelant : l'INSERT est isolé dans
  un SAVEPOINT (begin_nested) pour traduire une collision sur la PK sans
  rendre la transaction de l'appelant inutilisable. SAVEPOINT local !=
  ownership de la transaction : un rollback de l'appelant annule aussi la
  création.

- Concurrence : la PK conversation_identities_pkey est la défense finale.
  Deux transactions qui insèrent la même clé : la seconde attend la
  première ; si celle-ci commite, la seconde reçoit la violation de PK,
  revient au SAVEPOINT, relit la ligne gagnante (READ COMMITTED : nouveau
  snapshot par instruction) puis la retourne (même user_id) ou lève
  ConversationOwnershipConflict (autre user_id). Seule une violation de
  CETTE contrainte est traduite ; toute autre IntegrityError (ex. user_id
  inconnu, FK conversation_identities_user_id_fkey) est propagée telle
  quelle, après retour au SAVEPOINT. begin_nested() flushe les écritures
  en attente de l'appelant AVANT d'émettre le SAVEPOINT : celui-ci ne
  contient que l'INSERT du registre et son annulation ne les touche pas.

- R1 PR 9-B1 (E1.2) : la migration 0021 ajoute la contrainte UNIQUE
  uq_conversation_identities_key_owner (conversation_key, user_id), cible
  de la FK composite des réservations Décrypter. Une insertion concurrente
  de la même clé peut être signalée par PostgreSQL sur l'une OU l'autre
  contrainte (l'ordre de contrôle des index uniques n'est pas un contrat).
  Une collision n'est donc résoluble que si SQLSTATE vaut 23505
  (unique_violation) ET que le nom de contrainte rapporté est EXACTEMENT
  conversation_identities_pkey ou uq_conversation_identities_key_owner ;
  le gagnant est alors relu par sa clé et son propriétaire vérifié comme
  avant. Toute autre IntegrityError reste propagée telle quelle. Seul ce
  prédicat privé change ; interface, SAVEPOINT, relecture, populate_existing
  et refus de réattribution sont inchangés. Les réservations F2 écrivent le
  registre par un adaptateur SQL fermé distinct de la frontière
  d'autorisation F2 (_register_conversation), soumis aux mêmes
  règles.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.models import ConversationIdentity

PRIMARY_KEY_CONSTRAINT = "conversation_identities_pkey"
# R1 PR 9-B1 (migration 0021) : seconde contrainte d'identité de la clé.
OWNER_UNIQUE_CONSTRAINT = "uq_conversation_identities_key_owner"
IDENTITY_COLLISION_CONSTRAINTS = (PRIMARY_KEY_CONSTRAINT, OWNER_UNIQUE_CONSTRAINT)
UNIQUE_VIOLATION = "23505"


class InteractionIdentityError(Exception):
    """Erreur métier du registre d'identité des interactions."""


class InvalidConversationIdentity(InteractionIdentityError):
    """Entrée structurelle invalide (chaîne vide, type, caractère NUL)."""


class ConversationOwnershipConflict(InteractionIdentityError):
    """La conversation_key appartient déjà à un autre user_id."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_identifier(value, name: str) -> str:
    """str non vide (après strip), sans NUL ; jamais normalisée."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidConversationIdentity(f"{name} doit être une chaîne non vide")
    if "\x00" in value:
        raise InvalidConversationIdentity(f"{name} : caractère NUL refusé")
    return value


def _is_identity_collision(exc: IntegrityError) -> bool:
    """unique_violation (23505) sur l'une des DEUX contraintes d'identité de
    la clé, et rien d'autre (FK, NOT NULL, CHECK, autre contrainte : faux)."""
    diag = getattr(exc.orig, "diag", None)
    return (getattr(diag, "sqlstate", None) == UNIQUE_VIOLATION
            and getattr(diag, "constraint_name", None) in IDENTITY_COLLISION_CONSTRAINTS)


def _resolve(db, conversation_key: str, user_id: str) -> ConversationIdentity | None:
    """Ligne existante appartenant à user_id, None si absente,
    ConversationOwnershipConflict si elle appartient à un autre user_id."""
    identity = db.execute(
        select(ConversationIdentity)
        .where(ConversationIdentity.conversation_key == conversation_key)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if identity is not None and identity.user_id != user_id:
        raise ConversationOwnershipConflict(f"conversation_key déjà attribuée à un autre utilisateur ({user_id} refusé)")
    return identity


def register_or_resolve_conversation(db, *, user_id: str, conversation_key: str) -> ConversationIdentity:
    """Retourne l'identité de conversation_key pour user_id, en la créant si
    elle est absente. Idempotent ; fail-closed si la clé appartient à un
    autre utilisateur. Flush sans commit."""
    _require_identifier(user_id, "user_id")
    _require_identifier(conversation_key, "conversation_key")

    identity = _resolve(db, conversation_key, user_id)
    if identity is not None:
        return identity

    identity = ConversationIdentity(conversation_key=conversation_key, user_id=user_id, created_at=_utcnow())
    try:
        with db.begin_nested():
            db.add(identity)
            db.flush()
    except IntegrityError as exc:
        if not _is_identity_collision(exc):
            raise
        winner = _resolve(db, conversation_key, user_id)
        if winner is None:
            # Ligne concurrente invisible (isolation REPEATABLE READ /
            # SERIALIZABLE) : impossible à résoudre sans réessayer la
            # transaction ; l'appelant reçoit la violation telle quelle.
            raise
        return winner
    return identity
