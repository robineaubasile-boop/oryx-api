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

Garde-fous de verrouillage (SET LOCAL : ils valent jusqu'à la fin de la
transaction Alembic, 0016 et 0017 comprises, puis disparaissent) :

  - lock_timeout = '5s' borne l'ATTENTE du verrou ACCESS EXCLUSIVE sur
    users : si une transaction applicative le retient, la transaction
    Alembic échoue entièrement au lieu de bloquer l'application plus de
    5 s (les requêtes sur users mises en file derrière l'ALTER attendent
    au plus ce délai).
  - statement_timeout = '15s' borne l'EXÉCUTION de chaque instruction une
    fois le verrou obtenu, dont le parcours de users qui valide le CHECK
    (aucune réécriture : ~50 ms par million de lignes mesurés sur
    PostgreSQL 16 et 18). Supérieur à lock_timeout, qui reste le premier
    déclencheur pendant l'attente.
  - idle_in_transaction_session_timeout = '15s' borne la CONSERVATION du
    verrou si le client Alembic cesse de parler au serveur (conteneur
    pre-deploy supprimé, réseau coupé) : le serveur termine la session et
    libère le verrou au lieu d'attendre la détection TCP.

Ces trois délais sont appliqués par le serveur, indépendamment du client.
En cas de dépassement, toute la transaction (0015 à 0017) est annulée : la
base reste en 0014, le pre-deploy Railway échoue et l'ancienne version
continue de servir. Rien n'est modifié durablement (ni ALTER ROLE, ni
ALTER DATABASE, ni postgresql.conf). Une migration ultérieure appliquée dans
la même transaction hérite de ces délais ; si elle en exige d'autres, elle
les redéfinit par son propre SET LOCAL.

Le CHECK est validé sous le verrou que détient déjà ADD COLUMN. NOT VALID
puis VALIDATE CONSTRAINT n'allégerait ce verrou que dans une transaction
séparée, ce qui romprait l'atomicité de 0015 à 0017 pour un parcours de
quelques millisecondes ; le CHECK reste donc validé immédiatement.

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

# Garde-fous de verrouillage : voir la docstring (attente, exécution,
# conservation). Ordre fixe, vérifié par le SQL offline des tests.
LOCK_GUARDS = (
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL statement_timeout = '15s'",
    "SET LOCAL idle_in_transaction_session_timeout = '15s'",
)
CHECK_NAME = "ck_users_level_revision_non_negative"


def upgrade() -> None:
    """Ajoute users.level_revision et son CHECK (rien d'autre)."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.add_column(
        "users",
        sa.Column("level_revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(CHECK_NAME, "users", "level_revision >= 0")


def downgrade() -> None:
    """Retour exact à 0014."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.drop_constraint(CHECK_NAME, "users", type_="check")
    op.drop_column("users", "level_revision")
