"""T1-A : introduction de analysis_sessions (EXPAND).

Revision ID: 0002_analysis_sessions
Revises: 0001_current_oryx_baseline
Create Date: 2026-09-26

Première migration post-baseline. Strictement additive : crée uniquement
la table analysis_sessions, qui donne une identité stable à chaque
tentative d'analyse fondamentale d'une entreprise par un utilisateur.
Plusieurs sessions peuvent exister pour un même (user_id, ticker).

T1-A = EXPAND seulement : la table n'est lue ni écrite par l'application,
aucune donnée n'est migrée/backfillée, et les six tables historiques ne
sont pas touchées. L'ancien code applicatif reste valide sur ce schéma.

Conventions des nouvelles tables :
- id en UUID natif PostgreSQL (sa.Uuid), sans server_default : l'UUID est
  généré par l'application (ou fourni explicitement par un futur backfill),
  donc aucune dépendance à uuid-ossp / pgcrypto ;
- timestamps en TIMESTAMP WITH TIME ZONE (sa.DateTime(timezone=True)),
  sans server_default, comme la baseline ;
- CHECK nommé limitant status à in_progress / completed / abandoned
  (pas de PostgreSQL ENUM, pas de contrainte status ↔ completed_at, pas de
  CHECK sur current_step) ;
- FK user_id → users.id sans ON DELETE (NO ACTION par défaut, comme les FK
  historiques) : supprimer un utilisateur ayant des sessions est refusé
  plutôt que de supprimer ses sessions en cascade ;
- aucun index ni UNIQUE(user_id, ticker).

Le downgrade supprime la table : acceptable ici car elle ne porte encore
aucune donnée fonctionnelle. Ce n'est pas la stratégie normale de rollback
production (on privilégie le rollback applicatif).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_analysis_sessions"
down_revision: Union[str, Sequence[str], None] = "0001_current_oryx_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crée analysis_sessions (et rien d'autre)."""
    op.create_table(
        "analysis_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("current_step", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed', 'abandoned')",
            name="ck_analysis_sessions_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Supprime analysis_sessions (vide en T1-A), sans toucher au reste."""
    op.drop_table("analysis_sessions")
