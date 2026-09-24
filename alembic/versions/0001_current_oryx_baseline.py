"""Baseline : schéma historique d'Oryx avant Alembic.

Revision ID: 0001_current_oryx_baseline
Revises:
Create Date: 2026-09-24

Reproduit à l'identique le schéma produit par Base.metadata.create_all()
+ _migrate_add_columns() (core/db.py) à partir de core/models.py :
users, portfolio_positions, company_analyses, investment_theses,
analysis_facts, user_statements.

Volontairement :
- aucun server_default : les default=/onupdate=datetime.utcnow des modèles
  sont des defaults applicatifs SQLAlchemy, pas des DEFAULT SQL ;
- timestamps en TIMESTAMP WITHOUT TIME ZONE (sa.DateTime()), pas TIMESTAMPTZ ;
- aucun index, UNIQUE ou CHECK : les modèles n'en déclarent pas ;
- contraintes (PK, FK) non nommées, comme create_all : PostgreSQL leur
  attribue les mêmes noms automatiques (users_pkey,
  portfolio_positions_user_id_fkey, ...).

NE PAS exécuter sur la base Railway existante : elle a été créée par
create_all(). Elle sera inspectée (T0-C) puis marquée avec
`alembic stamp 0001_current_oryx_baseline` (T0-D). Voir alembic/README.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001_current_oryx_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crée les 6 tables historiques d'Oryx sur une base vide."""
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "portfolio_positions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("purchase_price", sa.Float(), nullable=True),
        sa.Column("target_percent", sa.Float(), nullable=True),
        sa.Column("envelope", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "company_analyses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("current_step", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "investment_theses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("thesis_text", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "analysis_facts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("fact_date", sa.DateTime(), nullable=True),
        sa.Column("fact_type", sa.String(), nullable=False),
        sa.Column("fact_value", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "user_statements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("step", sa.String(), nullable=True),
        sa.Column("statement_date", sa.DateTime(), nullable=True),
        sa.Column("statement_text", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Refuse de descendre sous la baseline.

    0001_current_oryx_baseline est le plancher historique d'Oryx : un
    downgrade ferait DROP TABLE sur toutes les données (utilisateurs,
    portefeuilles, analyses). Les migrations futures peuvent redescendre
    jusqu'à cette révision, jamais en dessous.
    """
    raise RuntimeError(
        "Downgrade interdit : 0001_current_oryx_baseline est la baseline "
        "historique d'Oryx. Descendre en dessous supprimerait toutes les "
        "tables et leurs données. Aucune révision antérieure n'existe."
    )
