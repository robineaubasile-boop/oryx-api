"""R1 PR 9-B1 : réservation durable des tours Décrypter F2 (EXPAND, dormante).

Revision ID: 0021_decryptage_reservations
Revises: 0020_position_revision
Create Date: 2026-10-31

Fichier 0021_decryptage_turn_reservations.py ; identifiant de révision
raccourci à 28 caractères (alembic_version.version_num est un VARCHAR(32),
même règle que 0013_decryptage_conv_affinity). Les fichiers 0001 à 0020 ne
sont pas modifiés.

1. conversation_identities : contrainte UNIQUE NON différable
   uq_conversation_identities_key_owner (conversation_key, user_id), cible
   de la FK composite ci-dessous. La PK historique
   conversation_identities_pkey est conservée telle quelle ; la clé reste
   unique à elle seule (aucune réattribution de propriétaire possible).

2. decryptage_turn_reservations : une réservation par identité logique de
   tour (UNIQUE uq_decryptage_turn_reservations_turn sur user_id,
   conversation_key, surface, source_user_turn_id, delivery_ordinal ;
   l'empreinte n'est jamais une clé). Aucun texte de question, contexte,
   prompt ni réponse. FK : users, conversation propriétaire (FK composite
   (conversation_key, user_id) -> uq_conversation_identities_key_owner) et
   assistant_deliveries (nullable, UNIQUE sur les valeurs non nulles). CHECK
   nommés : surface, ordinal, formats d'empreinte et de version, états et
   motifs, compteurs, forme de chaque état, horodatages.

Aucun backfill, aucune réparation, aucun trigger, aucune fonction
persistée, aucun ENUM, aucun server_default ; aucune donnée écrite hors
alembic_version. Les CHECK ne garantissent pas l'immutabilité historique :
elle relève des mutations SQL fermées des adaptateurs de la frontière
d'autorisation F2 (seul point d'écriture).

Garde-fous de verrouillage (SET LOCAL, transaction Alembic) : 5 s d'attente
de verrou, 15 s par instruction, 15 s d'inactivité. Un verrou DDL qui
n'est pas obtenu fait échouer la migration (jamais masqué).

Downgrade PROTÉGÉ, dans une seule transaction : LOCK
decryptage_turn_reservations IN ACCESS EXCLUSIVE MODE, refus (exception,
rien n'est supprimé) s'il existe une réservation, sinon suppression de la
table puis de la contrainte UNIQUE composite (retour exact à 0020). En mode
offline, le contrôle est un bloc DO évalué à l'exécution du script.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0021_decryptage_reservations"
down_revision: Union[str, Sequence[str], None] = "0020_position_revision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCK_GUARDS = (
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL statement_timeout = '15s'",
    "SET LOCAL idle_in_transaction_session_timeout = '15s'",
)
CONVERSATIONS = "conversation_identities"
OWNER_UNIQUE = "uq_conversation_identities_key_owner"
TABLE = "decryptage_turn_reservations"
TURN_UNIQUE = "uq_decryptage_turn_reservations_turn"
DELIVERY_UNIQUE = "uq_decryptage_turn_reservations_assistant_delivery"
USER_FK = "decryptage_turn_reservations_user_id_fkey"
CONVERSATION_FK = "decryptage_turn_reservations_conversation_fkey"
DELIVERY_FK = "decryptage_turn_reservations_assistant_delivery_id_fkey"

# Expressions exactes, recopiées par core/models.py (concordance testée).
CHECKS = (
    ("ck_decryptage_turn_reservations_surface", "surface = 'decryptage'"),
    ("ck_decryptage_turn_reservations_delivery_ordinal", "delivery_ordinal = 1"),
    ("ck_decryptage_turn_reservations_fingerprint_version",
     "fingerprint_version IN ('oryx-decryptage-reservation-v1')"),
    ("ck_decryptage_turn_reservations_fingerprint_key_version",
     "fingerprint_key_version ~ '^[a-z0-9][a-z0-9_-]{0,31}$'"),
    ("ck_decryptage_turn_reservations_request_fingerprint", "request_fingerprint ~ '^[0-9a-f]{64}$'"),
    ("ck_decryptage_turn_reservations_lease_token_hash",
     "lease_token_hash IS NULL OR lease_token_hash ~ '^[0-9a-f]{64}$'"),
    ("ck_decryptage_turn_reservations_state", "state IN ('reserved', 'blocked', 'finalized')"),
    ("ck_decryptage_turn_reservations_blocked_reason",
     "blocked_reason IS NULL OR blocked_reason IN ('legacy_collision', 'attempts_exhausted')"),
    ("ck_decryptage_turn_reservations_counters", "lease_epoch >= 0 AND attempts_used >= 0"),
    ("ck_decryptage_turn_reservations_reserved",
     "state <> 'reserved' OR (assistant_delivery_id IS NULL AND finalized_at IS NULL AND blocked_reason IS NULL "
     "AND ((lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL AND retry_not_before_at IS NULL) "
     "OR (lease_token_hash IS NULL AND lease_expires_at IS NULL AND retry_not_before_at IS NOT NULL)))"),
    ("ck_decryptage_turn_reservations_blocked",
     "state <> 'blocked' OR (blocked_reason IS NOT NULL AND lease_token_hash IS NULL AND lease_expires_at IS NULL "
     "AND retry_not_before_at IS NULL AND assistant_delivery_id IS NULL AND finalized_at IS NULL)"),
    ("ck_decryptage_turn_reservations_finalized",
     "state <> 'finalized' OR (assistant_delivery_id IS NOT NULL AND finalized_at IS NOT NULL "
     "AND lease_token_hash IS NULL AND lease_expires_at IS NULL AND retry_not_before_at IS NULL "
     "AND blocked_reason IS NULL)"),
    ("ck_decryptage_turn_reservations_timestamps",
     "updated_at >= created_at AND (finalized_at IS NULL OR finalized_at >= created_at)"),
)
DOWNGRADE_LOCK = f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE"
BLOCKING = f"SELECT 1 FROM {TABLE}"
DOWNGRADE_MESSAGE = (
    "R1 PR 9-B1 : downgrade de 0021_decryptage_reservations refusé, des réservations Décrypter existent. "
    "Aucune donnée supprimée."
)


def upgrade() -> None:
    """UNIQUE composite des conversations, puis table des réservations."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.create_unique_constraint(OWNER_UNIQUE, CONVERSATIONS, ["conversation_key", "user_id"])
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("conversation_key", sa.Text(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("source_user_turn_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_ordinal", sa.Integer(), nullable=False),
        sa.Column("fingerprint_version", sa.Text(), nullable=False),
        sa.Column("fingerprint_key_version", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("resolved_ticker", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column("lease_token_hash", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts_used", sa.Integer(), nullable=False),
        sa.Column("retry_not_before_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assistant_delivery_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        *(sa.CheckConstraint(expression, name=name) for name, expression in CHECKS),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=USER_FK),
        sa.ForeignKeyConstraint(["conversation_key", "user_id"],
                                [f"{CONVERSATIONS}.conversation_key", f"{CONVERSATIONS}.user_id"],
                                name=CONVERSATION_FK),
        sa.ForeignKeyConstraint(["assistant_delivery_id"], ["assistant_deliveries.id"], name=DELIVERY_FK),
        sa.PrimaryKeyConstraint("id", name="decryptage_turn_reservations_pkey"),
        sa.UniqueConstraint("user_id", "conversation_key", "surface", "source_user_turn_id", "delivery_ordinal",
                            name=TURN_UNIQUE),
        sa.UniqueConstraint("assistant_delivery_id", name=DELIVERY_UNIQUE),
    )


def downgrade() -> None:
    """Retour exact à 0020, seulement sans aucune réservation."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.execute(DOWNGRADE_LOCK)
    if context.is_offline_mode():
        message = DOWNGRADE_MESSAGE.replace("'", "''")
        op.execute(f"DO $$ BEGIN IF EXISTS ({BLOCKING}) THEN RAISE EXCEPTION '{message}'; END IF; END $$")
    elif op.get_bind().execute(sa.text(f"SELECT EXISTS ({BLOCKING})")).scalar_one():
        raise RuntimeError(DOWNGRADE_MESSAGE)
    op.drop_table(TABLE)
    op.drop_constraint(OWNER_UNIQUE, CONVERSATIONS, type_="unique")
