"""R1-D1 : lease persistée des ObservationEvaluationRun (EXPAND).

Revision ID: 0014_evaluation_run_leases
Revises: 0013_decryptage_conv_affinity
Create Date: 2026-10-07

Migration du chantier R1-D1 (« Local Evaluation Runtime ») : le worker
oryx-evaluation-worker exécute les appels LLM d'un run HORS de toute
transaction ; la propriété d'exécution d'un run running / candidate est
donc portée par une lease persistée, vérifiée par core/observation_service.py
avant toute mutation du worker (renouvellement, persistance du résultat,
complétion, échec).

Étend UNIQUEMENT observation_evaluation_runs :

- colonne lease_token UUID NULL : jeton opaque du worker qui détient
  l'exécution du run ;
- colonne lease_expires_at TIMESTAMPTZ NULL : fin de validité de la lease
  (horloge PostgreSQL). Une lease expirée est reprenable (recovery du MÊME
  run, jamais un nouveau run) ;
- CHECK ck_observation_evaluation_runs_lease_pair : lease_token et
  lease_expires_at sont NULL ensemble ou renseignés ensemble ;
- CHECK ck_observation_evaluation_runs_lease_running : seul un run running
  peut porter une lease (complete / fail l'effacent).

La lease ne change NI execution_status NI interpretation_status (aucun
statut « leased ») ; aucune colonne worker_id, heartbeat_at ni
attempt_count. Aucune colonne supprimée ni renommée, aucune autre table
modifiée, aucun index, aucun trigger, aucun server_default. Aucune donnée
écrite hors alembic_version : AUCUN backfill (les runs historiques gardent
NULL / NULL et respectent les deux CHECK, validés à l'ajout).

Downgrade : retour exact à 0013 (CHECK puis colonnes supprimés) ; les leases
en cours sont perdues (un run running redevient non leasé).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0014_evaluation_run_leases"
down_revision: Union[str, Sequence[str], None] = "0013_decryptage_conv_affinity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNS = "observation_evaluation_runs"
TOKEN = "lease_token"
EXPIRES = "lease_expires_at"
LEASE_PAIR_CHECK = "ck_observation_evaluation_runs_lease_pair"
LEASE_RUNNING_CHECK = "ck_observation_evaluation_runs_lease_running"

LEASE_PAIR = "(lease_token IS NULL) = (lease_expires_at IS NULL)"
LEASE_RUNNING = "lease_token IS NULL OR execution_status = 'running'"


def upgrade() -> None:
    """Ajoute lease_token, lease_expires_at et leurs deux CHECK (rien d'autre)."""
    op.add_column(RUNS, sa.Column(TOKEN, sa.Uuid(), nullable=True))
    op.add_column(RUNS, sa.Column(EXPIRES, sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(LEASE_PAIR_CHECK, RUNS, LEASE_PAIR)
    op.create_check_constraint(LEASE_RUNNING_CHECK, RUNS, LEASE_RUNNING)


def downgrade() -> None:
    """Retour exact à 0013."""
    op.drop_constraint(LEASE_RUNNING_CHECK, RUNS, type_="check")
    op.drop_constraint(LEASE_PAIR_CHECK, RUNS, type_="check")
    op.drop_column(RUNS, EXPIRES)
    op.drop_column(RUNS, TOKEN)
