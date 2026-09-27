"""T2-A : couche de données cognitive_events + support_traces (EXPAND).

Revision ID: 0005_cognitive_support_traces
Revises: 0004_drop_company_analyses
Create Date: 2026-09-27

Strictement additive : crée uniquement les tables cognitive_events et
support_traces (niveau 2 du futur système pédagogique). Aucune table
existante n'est modifiée, aucune donnée n'est migrée ni backfillée, et
l'application ne lit ni n'écrit encore ces tables (la capture arrivera en
T2-B).

Couche descriptive/historique : elle conserve ce que l'utilisateur a
réellement vu (stimulus_snapshot), ce qu'il a réellement produit
(user_work_snapshot), les aides réellement visibles fournies par Oryx
(support_traces) et leur ordre (sequence_no). Elle ne porte aucune
évaluation (compréhension, niveau, compétence, score, confiance,
progression). Une support_trace n'est jamais une preuve de compétence.

cognitive_events : une capsule cohérente d'un problème cognitif, pouvant
couvrir plusieurs messages/tours (événement != message).
- status VARCHAR + CHECK nommé ck_cognitive_events_status
  (open / finalized / abandoned) : seul vocabulaire fermé. Seuls les
  événements finalized seront évaluables plus tard (T3) ; open et
  abandoned ne sont pas des preuves pédagogiques ;
- event_origin (ex. education, coach, decryptage, academy, rallye,
  portfolio) et task_kind (ex. explain_concept, interpret_metric,
  compare_options, apply_formula, identify_risk, build_thesis) sont des
  vocabulaires extensibles : VARCHAR sans CHECK ;
- stimulus_snapshot / user_work_snapshot en JSONB NOT NULL, sans schéma
  interne imposé en base ;
- analysis_session_id NULL : un événement peut exister hors de Décrypte.

support_traces : une aide réellement visible par l'utilisateur pendant un
cognitive_event. support_kind (ex. rephrase, hint, formula,
worked_example, explanation, feedback, answer_reveal, data_provided) est
extensible : VARCHAR sans CHECK. support_payload (JSONB) ne contient que ce
qui a été effectivement montré, jamais de raisonnement interne du modèle,
de prompt système ni de diagnostic non montré.
UNIQUE(cognitive_event_id, sequence_no) nommée
uq_support_traces_event_sequence : ordre des aides non ambigu au sein
d'un événement (son index est le seul créé par cette migration).

Conventions (identiques à analysis_sessions) : id UUID généré par
l'application (aucun server_default), timestamps TIMESTAMPTZ sans
server_default, pas d'ENUM PostgreSQL, FK sans ON DELETE / ON UPDATE (NO
ACTION) : supprimer un utilisateur, une analysis_session ou un
cognitive_event encore référencé est refusé. Aucun index supplémentaire,
aucun trigger : l'immutabilité après finalized/abandoned relèvera du
service T2-B.

Identifiant de révision volontairement court (29 caractères) :
alembic_version.version_num est un VARCHAR(32) (créé par Alembic, y
compris par le `stamp` de production) ; un identifiant plus long ferait
échouer l'upgrade à l'écriture de la version, et élargir alembic_version
toucherait une table hors du périmètre de cette migration.

Le downgrade supprime support_traces puis cognitive_events et revient
exactement à 0004, sans toucher aux autres tables. Ce n'est pas la
stratégie normale de rollback production (on privilégie le rollback
applicatif).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0005_cognitive_support_traces"
down_revision: Union[str, Sequence[str], None] = "0004_drop_company_analyses"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crée cognitive_events puis support_traces (et rien d'autre)."""
    op.create_table(
        "cognitive_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("analysis_session_id", sa.Uuid(), nullable=True),
        sa.Column("event_origin", sa.String(), nullable=False),
        sa.Column("task_kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("conversation_key", sa.String(), nullable=True),
        sa.Column("stimulus_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("user_work_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'finalized', 'abandoned')",
            name="ck_cognitive_events_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["analysis_session_id"], ["analysis_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "support_traces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cognitive_event_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("support_kind", sa.String(), nullable=False),
        sa.Column("support_payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cognitive_event_id"], ["cognitive_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cognitive_event_id", "sequence_no",
            name="uq_support_traces_event_sequence",
        ),
    )


def downgrade() -> None:
    """Supprime support_traces puis cognitive_events (retour exact à 0004)."""
    op.drop_table("support_traces")
    op.drop_table("cognitive_events")
