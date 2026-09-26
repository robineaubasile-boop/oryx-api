"""T1-B1 : rattachement préparatoire des données aux analysis_sessions (EXPAND).

Revision ID: 0003_analysis_session_links
Revises: 0002_analysis_sessions
Create Date: 2026-09-26

Strictement additive : ajoute une colonne analysis_session_id (UUID,
NULLABLE) à analysis_facts, user_statements et investment_theses, chacune
avec une FK vers analysis_sessions.id. Rien d'autre.

AUCUN backfill : les lignes existantes gardent analysis_session_id = NULL,
qui signifie « provenance de session non établie dans le modèle
historique ». On refuse de reconstruire cette provenance à partir de
user_id + ticker (+ dates, + company_analyses.created_at).

L'application ne renseigne pas encore ces colonnes (T1-B2) : l'ancien code
continue d'insérer ces lignes sans analysis_session_id, ce que la
nullabilité autorise.

FK sans ON DELETE / ON UPDATE (NO ACTION par défaut, comme les autres FK
d'Oryx) : supprimer une analysis_session encore référencée est refusé.
Leurs noms sont explicites mais identiques à ceux que PostgreSQL attribue
automatiquement (<table>_<colonne>_fkey), pour que le downgrade puisse les
retirer nommément. Aucun index ni UNIQUE.

Le downgrade retire les trois FK puis les trois colonnes et revient
exactement à 0002. Ce n'est pas la stratégie normale de rollback
production (on privilégie le rollback applicatif).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003_analysis_session_links"
down_revision: Union[str, Sequence[str], None] = "0002_analysis_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LINKED_TABLES = ("analysis_facts", "user_statements", "investment_theses")


def _fk_name(table: str) -> str:
    return f"{table}_analysis_session_id_fkey"


def upgrade() -> None:
    """Ajoute analysis_session_id UUID NULL + FK sur les trois tables."""
    for table in LINKED_TABLES:
        op.add_column(table, sa.Column("analysis_session_id", sa.Uuid(), nullable=True))
    for table in LINKED_TABLES:
        op.create_foreign_key(
            _fk_name(table),
            table,
            "analysis_sessions",
            ["analysis_session_id"],
            ["id"],
        )


def downgrade() -> None:
    """Retire les trois FK, puis les trois colonnes (retour exact à 0002)."""
    for table in LINKED_TABLES:
        op.drop_constraint(_fk_name(table), table, type_="foreignkey")
    for table in LINKED_TABLES:
        op.drop_column(table, "analysis_session_id")
