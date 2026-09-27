"""T1-C2 : suppression définitive de company_analyses (CONTRACT).

Revision ID: 0004_drop_company_analyses
Revises: 0003_analysis_session_links
Create Date: 2026-09-27

Depuis T1-C1, l'application ne lit ni n'écrit plus company_analyses :
AnalysisSession est l'unique identité des analyses. Cette migration
supprime physiquement la table obsolète, et rien d'autre : aucune autre
table, contrainte ou donnée n'est touchée, aucun backfill.

Garde-fou destructif : avant le DROP, la table est verrouillée (ACCESS
EXCLUSIVE, pour qu'aucune écriture ne puisse s'intercaler entre la
vérification et le DROP) puis on vérifie qu'elle est vide. Si elle
contient au moins une ligne, la migration échoue (RuntimeError) sans rien
supprimer ; la transaction Alembic est annulée et la base reste en 0003.
En mode offline (--sql), la même vérification est émise sous forme d'un
bloc DO qui lève une exception.

DROP TABLE sans CASCADE : si un objet inattendu dépend encore de
company_analyses (FK entrante, vue...), PostgreSQL refuse le DROP et la
migration échoue proprement au lieu de supprimer cet objet.

Le downgrade recrée uniquement la STRUCTURE de company_analyses telle
qu'elle existait en 0003 (identique à 0001 : PK id SERIAL, FK user_id ->
users.id sans ON DELETE, aucun index ni UNIQUE). Il ne peut pas restaurer
de lignes : c'est acceptable, la table était contractuellement vide avant
l'upgrade (garde-fou ci-dessus, et préflight production
COMPANY_ANALYSES=0).
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_drop_company_analyses"
down_revision: Union[str, Sequence[str], None] = "0003_analysis_session_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "company_analyses"
NOT_EMPTY_MESSAGE = (
    "T1-C2 : company_analyses n'est pas vide, DROP TABLE annulé. "
    "La table doit être vide avant la migration 0004_drop_company_analyses."
)


def upgrade() -> None:
    """Vérifie que company_analyses est vide, puis la supprime (sans CASCADE)."""
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    if context.is_offline_mode():
        message = NOT_EMPTY_MESSAGE.replace("'", "''")
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {TABLE}) THEN "
            f"RAISE EXCEPTION '{message}'; END IF; END $$"
        )
    else:
        rows = op.get_bind().execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar_one()
        if rows:
            raise RuntimeError(f"{NOT_EMPTY_MESSAGE} ({rows} ligne(s) présente(s))")
    op.drop_table(TABLE)


def downgrade() -> None:
    """Recrée la structure de company_analyses (0003), sans ses anciennes lignes."""
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("current_step", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
