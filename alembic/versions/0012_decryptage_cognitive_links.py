"""R1-C2 : rattachement cognitif T2 des tours Décrypter (EXPAND).

Revision ID: 0012_decryptage_cognitive_links
Revises: 0011_assistant_deliveries
Create Date: 2026-10-05

Crée UNE table runtime spécifique Décrypter, decryptage_cognitive_links :
au plus une ligne par AssistantDelivery R1-C2, qui relie la production
utilisateur du tour (input_action / input_event_id / context_exit_event_id)
et la réponse assistant réellement delivered (response_action /
response_event_id / support_trace_id) aux CognitiveEvents / SupportTraces
T2. Ce n'est ni une preuve, ni une observation, ni une évaluation (aucun
T3+).

Colonnes : assistant_delivery_id UUID (PK, FK assistant_deliveries.id),
capture_version VARCHAR NOT NULL, input_action VARCHAR NOT NULL,
input_event_id UUID NULL (FK cognitive_events.id), context_exit_event_id
UUID NULL (FK cognitive_events.id ; event quitté sur rupture de contexte,
finalized si travail présent, abandoned s'il était vide), capture_state
VARCHAR NOT NULL, response_action VARCHAR NULL, response_event_id UUID NULL
(FK cognitive_events.id),
support_trace_id UUID NULL (FK support_traces.id), created_at TIMESTAMPTZ
NOT NULL, captured_at TIMESTAMPTZ NULL.

Contraintes : CHECK des vocabulaires fermés (capture_state, input_action,
response_action), CHECK de cohérence awaiting_delivery / captured, CHECK
input_event_id <-> contribution_appended, CHECK response_event_id /
support_trace_id <-> response_action ; index UNIQUE partiel
uq_decryptage_cognitive_links_opening_event (un event est ouvert par au plus
une livraison). FK sans ON DELETE / ON UPDATE (NO ACTION).

Aucune autre table modifiée, aucun trigger, aucune fonction, aucun ENUM,
aucun server_default. Aucune donnée écrite hors alembic_version : aucun
backfill (les AssistantDelivery R1-C1 historiques restent legacy).

Identifiant de révision (31 caractères) : alembic_version.version_num est
un VARCHAR(32).

Downgrade : supprime la table et son index (retour exact à 0011).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0012_decryptage_cognitive_links"
down_revision: Union[str, Sequence[str], None] = "0011_assistant_deliveries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LINKS = "decryptage_cognitive_links"
OPENING_INDEX = "uq_decryptage_cognitive_links_opening_event"
OPENING_WHERE = "response_action IN ('opened_event', 'transitioned_event')"


def upgrade() -> None:
    """Crée decryptage_cognitive_links et son index partiel (et rien d'autre)."""
    op.create_table(
        LINKS,
        sa.Column("assistant_delivery_id", sa.Uuid(), nullable=False),
        sa.Column("capture_version", sa.String(), nullable=False),
        sa.Column("input_action", sa.String(), nullable=False),
        sa.Column("input_event_id", sa.Uuid(), nullable=True),
        sa.Column("context_exit_event_id", sa.Uuid(), nullable=True),
        sa.Column("capture_state", sa.String(), nullable=False),
        sa.Column("response_action", sa.String(), nullable=True),
        sa.Column("response_event_id", sa.Uuid(), nullable=True),
        sa.Column("support_trace_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "capture_state IN ('awaiting_delivery', 'captured')",
            name="ck_decryptage_cognitive_links_capture_state",
        ),
        sa.CheckConstraint(
            "input_action IN ('no_open_event', 'no_user_contribution', 'contribution_appended', "
            "'event_closed_context_change', 'event_abandoned_context_change')",
            name="ck_decryptage_cognitive_links_input_action",
        ),
        sa.CheckConstraint(
            "response_action IS NULL OR response_action IN ('opened_event', 'continued_event', "
            "'continued_without_boundary_signal', 'transitioned_event', 'closed_terminal', "
            "'no_cognitive_action')",
            name="ck_decryptage_cognitive_links_response_action",
        ),
        sa.CheckConstraint(
            "(capture_state = 'awaiting_delivery' AND response_action IS NULL AND response_event_id IS NULL "
            "AND support_trace_id IS NULL AND captured_at IS NULL) OR (capture_state = 'captured' "
            "AND response_action IS NOT NULL AND captured_at IS NOT NULL)",
            name="ck_decryptage_cognitive_links_capture_state_fields",
        ),
        sa.CheckConstraint(
            "(input_action = 'contribution_appended') = (input_event_id IS NOT NULL)",
            name="ck_decryptage_cognitive_links_input_event",
        ),
        sa.CheckConstraint(
            "response_action IS NULL OR ((response_action IN ('opened_event', 'continued_event', "
            "'continued_without_boundary_signal', 'transitioned_event')) = (response_event_id IS NOT NULL) "
            "AND (response_action IN ('continued_event', 'continued_without_boundary_signal')) "
            "= (support_trace_id IS NOT NULL))",
            name="ck_decryptage_cognitive_links_response_refs",
        ),
        sa.ForeignKeyConstraint(["assistant_delivery_id"], ["assistant_deliveries.id"]),
        sa.ForeignKeyConstraint(["input_event_id"], ["cognitive_events.id"]),
        sa.ForeignKeyConstraint(["context_exit_event_id"], ["cognitive_events.id"]),
        sa.ForeignKeyConstraint(["response_event_id"], ["cognitive_events.id"]),
        sa.ForeignKeyConstraint(["support_trace_id"], ["support_traces.id"]),
        sa.PrimaryKeyConstraint("assistant_delivery_id"),
    )
    op.create_index(
        OPENING_INDEX,
        LINKS,
        ["response_event_id"],
        unique=True,
        postgresql_where=sa.text(OPENING_WHERE),
    )


def downgrade() -> None:
    """Supprime decryptage_cognitive_links (retour exact à 0011)."""
    op.drop_index(OPENING_INDEX, table_name=LINKS, postgresql_where=sa.text(OPENING_WHERE))
    op.drop_table(LINKS)
