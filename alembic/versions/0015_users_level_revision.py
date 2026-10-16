"""R1 PR 1 : révision versionnée du niveau utilisateur (EXPAND).

Revision ID: 0015_users_level_revision
Revises: 0014_evaluation_run_leases
Create Date: 2026-10-16

Ajoute à users UNE colonne, level_revision BIGINT NOT NULL DEFAULT 0, et le
CHECK ck_users_level_revision_non_negative (level_revision >= 0).

C'est le seul server_default de la chaîne Alembic, volontairement : l'ancien
backend insère dans users sans connaître cette colonne (GET /api/user/{id},
POST /api/user/{id}/portfolio, PUT /api/user/{id}/level). Depuis
PostgreSQL 11, ADD COLUMN avec une constante par défaut ne réécrit pas la
table ; les lignes existantes valent 0 sans aucun UPDATE (aucun backfill).

Les anciennes routes ne sont pas modifiées : PUT /level ne fait pas avancer
la révision. level_revision ne fait foi que pour le futur contrat versionné,
dont l'activation exige la fermeture ou l'adaptation de ces routes.

SET LOCAL lock_timeout = '5s' : si le verrou ACCESS EXCLUSIVE sur users
n'est pas obtenu, la transaction Alembic échoue entièrement au lieu de
bloquer les requêtes de l'application. Le réglage vaut jusqu'à la fin de la
transaction (0016 et 0017 comprises).

Aucun index, aucune autre table modifiée, aucun trigger. Identifiant de
révision de 25 caractères (alembic_version.version_num = VARCHAR(32)).

Downgrade : supprime le CHECK puis la colonne (retour exact à 0014).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0015_users_level_revision"
down_revision: Union[str, Sequence[str], None] = "0014_evaluation_run_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
CHECK_NAME = "ck_users_level_revision_non_negative"


def upgrade() -> None:
    """Ajoute users.level_revision et son CHECK (rien d'autre)."""
    op.execute(LOCK_TIMEOUT)
    op.add_column(
        "users",
        sa.Column("level_revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(CHECK_NAME, "users", "level_revision >= 0")


def downgrade() -> None:
    """Retour exact à 0014."""
    op.execute(LOCK_TIMEOUT)
    op.drop_constraint(CHECK_NAME, "users", type_="check")
    op.drop_column("users", "level_revision")
