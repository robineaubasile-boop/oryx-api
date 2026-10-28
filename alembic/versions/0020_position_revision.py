"""R1 PR 7 : révision versionnée des positions de portefeuille (EXPAND).

Revision ID: 0020_position_revision
Revises: 0019_f2_http_admission
Create Date: 2026-10-26

Ajoute à portfolio_positions UNE colonne, position_revision BIGINT NOT NULL
DEFAULT 0, et le CHECK ck_portfolio_positions_position_revision_non_negative
(position_revision >= 0). Les fichiers 0001 à 0019 ne sont pas modifiés ;
portfolio_write_idempotency (0016) est réutilisée telle quelle.

Comme users.level_revision (0015), c'est un server_default volontaire :
l'ancien backend (POST /api/user/{user_id}/portfolio) insère sans connaître
cette colonne. Depuis PostgreSQL 11, ADD COLUMN avec une constante par
défaut ne réécrit pas la table : les lignes existantes valent 0 sans aucun
UPDATE (aucun backfill). La révision 0 est une origine technique, pas la
preuve qu'une position n'a jamais été modifiée. Les champs financiers
historiques (Float / binary64) ne sont pas touchés.

Les anciennes routes ne sont pas modifiées : elles écrivent sans faire
avancer la révision (limite documentée dans docs/f2_r1_pr7.md).

Aucun index, aucun trigger, aucune fonction persistée, aucune donnée écrite
hors alembic_version. Identifiant de révision de 22 caractères.

Garde-fous de verrouillage (SET LOCAL, transaction Alembic) : 5 s d'attente
de verrou, 15 s par instruction, 15 s d'inactivité.

Downgrade PROTÉGÉ, dans une seule transaction de maintenance :

  1. LOCK portfolio_positions IN ACCESS EXCLUSIVE MODE (EN PREMIER : un
     writer PR 7 qui détient déjà un verrou sur les positions termine
     avant ; un writer qui n'a encore verrouillé que des reçus attend
     ensuite les positions sans bloquer l'étape 2) ;
  2. LOCK portfolio_write_idempotency IN SHARE MODE (aucune insertion,
     modification ni suppression de reçu concurrente) ;
  3. refus (exception, aucune modification) s'il existe un reçu ou une
     position de révision différente de 0 ;
  4. sinon retrait du CHECK puis de la colonne (retour exact à 0019).

En mode offline, le contrôle est émis sous forme d'un bloc DO évalué à
l'exécution du script (aucune donnée n'est inspectée à la génération).
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0020_position_revision"
down_revision: Union[str, Sequence[str], None] = "0019_f2_http_admission"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCK_GUARDS = (
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL statement_timeout = '15s'",
    "SET LOCAL idle_in_transaction_session_timeout = '15s'",
)
POSITIONS = "portfolio_positions"
RECEIPTS = "portfolio_write_idempotency"
COLUMN = "position_revision"
CHECK_NAME = "ck_portfolio_positions_position_revision_non_negative"
CHECK = "position_revision >= 0"
DOWNGRADE_LOCKS = (
    f"LOCK TABLE {POSITIONS} IN ACCESS EXCLUSIVE MODE",
    f"LOCK TABLE {RECEIPTS} IN SHARE MODE",
)
BLOCKING = (f"SELECT 1 FROM {RECEIPTS} UNION ALL "
            f"SELECT 1 FROM {POSITIONS} WHERE {COLUMN} <> 0")
DOWNGRADE_MESSAGE = (
    "R1 PR 7 : downgrade de 0020_position_revision refusé, des données versionnées l'exigent "
    "(reçus d'idempotence ou révisions de position non nulles). Aucune donnée supprimée."
)


def upgrade() -> None:
    """Ajoute portfolio_positions.position_revision et son CHECK (rien d'autre)."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.add_column(POSITIONS, sa.Column(COLUMN, sa.BigInteger(), server_default=sa.text("0"), nullable=False))
    op.create_check_constraint(CHECK_NAME, POSITIONS, CHECK)


def downgrade() -> None:
    """Retour exact à 0019, seulement sans reçu ni révision non nulle."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    for lock in DOWNGRADE_LOCKS:
        op.execute(lock)
    if context.is_offline_mode():
        message = DOWNGRADE_MESSAGE.replace("'", "''")
        op.execute(f"DO $$ BEGIN IF EXISTS ({BLOCKING}) THEN RAISE EXCEPTION '{message}'; END IF; END $$")
    elif op.get_bind().execute(sa.text(f"SELECT EXISTS ({BLOCKING})")).scalar_one():
        raise RuntimeError(DOWNGRADE_MESSAGE)
    op.drop_constraint(CHECK_NAME, POSITIONS, type_="check")
    op.drop_column(POSITIONS, COLUMN)
