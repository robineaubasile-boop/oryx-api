"""R1 PR 1 : registre durable d'idempotence des écritures portefeuille (EXPAND).

Revision ID: 0016_portfolio_idempotency
Revises: 0015_users_level_revision
Create Date: 2026-10-16

Crée UNE table, portfolio_write_idempotency : le reçu durable d'une
écriture portefeuille idempotente du futur contrat R1. Inactive : aucune
route ne la lit ni ne l'écrit ; les anciennes routes portefeuille sont
inchangées.

Colonnes : user_id VARCHAR (FK users.id), operation_type VARCHAR,
operation_id UUID (clé d'opération du contrat R1), fingerprint_version
VARCHAR, fingerprint_key_version VARCHAR, request_fingerprint VARCHAR
(HMAC hexadécimal), result_version VARCHAR, result_status SMALLINT,
result_payload JSONB, position_id INTEGER (référence logique SANS FK : le
reçu survit à la suppression de la position), created_at TIMESTAMPTZ.
Toutes NOT NULL.

Contraintes : PK (user_id, operation_type, operation_id) — même triplet =
rejeu du reçu, empreinte différente = 409 (règle du service) ; CHECK
operation_type (vocabulaire fermé), CHECK format hexadécimal de
request_fingerprint, CHECK result_status 2xx.

Aucune purge, aucun expires_at, aucun index hors PK : aucune déduplication
fondée sur les valeurs financières. Aucun trigger, aucun server_default,
aucune donnée écrite hors alembic_version. Identifiant de révision de
26 caractères.

Downgrade : supprime la table (retour exact à 0015).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0016_portfolio_idempotency"
down_revision: Union[str, Sequence[str], None] = "0015_users_level_revision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "portfolio_write_idempotency"


def upgrade() -> None:
    """Crée portfolio_write_idempotency (et rien d'autre)."""
    op.create_table(
        TABLE,
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("operation_type", sa.String(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint_version", sa.String(), nullable=False),
        sa.Column("fingerprint_key_version", sa.String(), nullable=False),
        sa.Column("request_fingerprint", sa.String(), nullable=False),
        sa.Column("result_version", sa.String(), nullable=False),
        sa.Column("result_status", sa.SmallInteger(), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation_type IN ('portfolio_position_create', 'portfolio_position_update', "
            "'portfolio_position_delete')",
            name="ck_portfolio_write_idempotency_operation_type",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_portfolio_write_idempotency_request_fingerprint_format",
        ),
        sa.CheckConstraint("result_status BETWEEN 200 AND 299", name="ck_portfolio_write_idempotency_result_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "operation_type", "operation_id"),
    )


def downgrade() -> None:
    """Supprime portfolio_write_idempotency (retour exact à 0015)."""
    op.drop_table(TABLE)
