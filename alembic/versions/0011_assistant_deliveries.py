"""R1-C1 : livraisons idempotentes des réponses assistant (EXPAND).

Revision ID: 0011_assistant_deliveries
Revises: 0010_r1b_event_idempotence
Create Date: 2026-10-04

Crée UNE table runtime, assistant_deliveries, qui identifie de manière
idempotente la réponse Oryx produite pour un tour utilisateur et établit si
le frontend first-party a confirmé l'avoir rendue (pending -> delivered).

AssistantDelivery est une infrastructure runtime : ce n'est ni un
SupportTrace, ni un CognitiveEvent, ni une observation, ni une preuve
utilisateur. delivered signifie seulement que le frontend Oryx affirme avoir
inséré le contenu canonique dans l'interface (jamais lu, compris ni
mémorisé).

Colonnes : id UUID (PK, généré par l'application), user_id VARCHAR NOT NULL
(FK users.id), conversation_key VARCHAR NOT NULL (FK
conversation_identities.conversation_key, registre R1-B), surface VARCHAR
NOT NULL, source_user_turn_id UUID NOT NULL, delivery_ordinal INTEGER NOT
NULL, analysis_session_id UUID NULL (FK analysis_sessions.id),
request_fingerprint VARCHAR NOT NULL, visible_content_fingerprint VARCHAR
NOT NULL, response_payload JSONB NOT NULL, private_metadata JSONB NOT NULL,
status VARCHAR NOT NULL, delivery_schema_version VARCHAR NOT NULL,
generated_at TIMESTAMPTZ NOT NULL, delivered_at TIMESTAMPTZ NULL.

Contraintes : CHECK status IN ('pending', 'delivered'), CHECK
delivery_ordinal >= 1, UNIQUE(user_id, conversation_key, surface,
source_user_turn_id, delivery_ordinal) nommée
uq_assistant_deliveries_turn_ordinal (défense finale de l'idempotence sous
concurrence). FK sans ON DELETE / ON UPDATE (NO ACTION).

Aucune autre table modifiée, aucun index supplémentaire (seuls ceux
implicites de la PK et de l'UNIQUE), aucun trigger, aucune fonction, aucun
ENUM, aucun server_default. Aucune donnée écrite hors alembic_version :
aucun backfill.

Identifiant de révision court (25 caractères) : alembic_version.version_num
est un VARCHAR(32).

Downgrade : supprime la table (retour exact à 0010). Ce n'est pas la
stratégie normale de rollback production (on privilégie le rollback
applicatif).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0011_assistant_deliveries"
down_revision: Union[str, Sequence[str], None] = "0010_r1b_event_idempotence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crée assistant_deliveries (et rien d'autre)."""
    op.create_table(
        "assistant_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("conversation_key", sa.String(), nullable=False),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("source_user_turn_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_ordinal", sa.Integer(), nullable=False),
        sa.Column("analysis_session_id", sa.Uuid(), nullable=True),
        sa.Column("request_fingerprint", sa.String(), nullable=False),
        sa.Column("visible_content_fingerprint", sa.String(), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(), nullable=False),
        sa.Column("private_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("delivery_schema_version", sa.String(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'delivered')", name="ck_assistant_deliveries_status"),
        sa.CheckConstraint("delivery_ordinal >= 1", name="ck_assistant_deliveries_delivery_ordinal"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["conversation_key"], ["conversation_identities.conversation_key"]),
        sa.ForeignKeyConstraint(["analysis_session_id"], ["analysis_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "conversation_key", "surface", "source_user_turn_id", "delivery_ordinal",
            name="uq_assistant_deliveries_turn_ordinal",
        ),
    )


def downgrade() -> None:
    """Supprime assistant_deliveries (retour exact à 0010)."""
    op.drop_table("assistant_deliveries")
