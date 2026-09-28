"""T3-A : couche de persistance des observations pédagogiques (EXPAND).

Revision ID: 0006_observation_layer
Revises: 0005_cognitive_support_traces
Create Date: 2026-09-28

Strictement additive : crée uniquement observation_evaluation_runs,
pedagogical_observations et l'index unique partiel « un seul run active par
événement » (niveau 3 du futur système pédagogique). Aucune table existante
n'est modifiée (cognitive_events et support_traces inchangées), aucune
donnée n'est migrée, backfillée ni seedée, et l'application ne lit ni
n'écrit encore ces tables (lifecycle transactionnel en T3-B, évaluateur
plus tard).

observation_evaluation_runs : interprétation versionnée complète d'un
cognitive_event (event_id FK cognitive_events.id) sous une combinaison
donnée de versions.
- execution_status (running / completed / failed) et interpretation_status
  (candidate / active / superseded / obsolete) : VARCHAR + CHECK nommés
  ck_observation_evaluation_runs_execution_status et
  ck_observation_evaluation_runs_interpretation_status. Un run completed
  sans observation = évaluation réussie n'ayant rien observé ; failed =
  échec technique (failure_code facultatif) ; aucun run = jamais évalué ;
- trigger : vocabulaire extensible, VARCHAR sans CHECK ;
- re_evaluates_run_id : FK auto-référente nullable (un premier run n'en
  réévalue aucun) ;
- evaluation_dedup_key : UNIQUE nommée
  uq_observation_evaluation_runs_dedup_key ;
- pedagogical_taxonomy_release_id : UUID nullable SANS clé étrangère.
  Référence logique réservée à T4 : la table pedagogical_taxonomy_releases
  n'existe pas encore ; la migration T4 ajoutera la FK réelle ;
- uq_observation_evaluation_runs_one_active_event : index UNIQUE PARTIEL
  sur event_id WHERE interpretation_status = 'active'. PostgreSQL garantit
  ainsi au plus un run active par événement, y compris sous concurrence ;
  candidate / superseded / obsolete restent multiples.

pedagogical_observations : unité probante locale et atomique (une seule
competency_code) rattachée à un run (evaluation_run_id FK
observation_evaluation_runs.id ; event_id n'est pas dupliqué).
- vocabulaires fermés en VARCHAR + CHECK nommés
  ck_pedagogical_observations_<colonne> : competency_code (C1..C12),
  observation_role, elicitation_mode, support_level, polarity,
  evidence_strength, local_stage (none / discovery / comprehension /
  application ; mastery volontairement ABSENT : jamais un stade local),
  contradiction_scope, error_type, capability_localization,
  integrity_status (valid / invalidated ; la supersession appartient au
  run). local_stage, contradiction_scope et error_type sont nullables ;
  task_kind est extensible (pas de CHECK) ;
- ck_pedagogical_observations_polarity_stage_scope : supportive =>
  local_stage NOT NULL et contradiction_scope NULL ; contradictory =>
  local_stage NULL et contradiction_scope NOT NULL ;
- ck_pedagogical_observations_integrity_invalidation : valid =>
  invalidated_at et invalidation_reason NULL ; invalidated => les deux
  NOT NULL ;
- uq_pedagogical_observations_run_ordinal : UNIQUE(evaluation_run_id,
  ordinal), ordre stable des observations d'un run. Aucune unicité sur
  competency_code : un run peut produire plusieurs observations sur la
  même compétence ;
- primary_user_action, contributive_user_actions, source_contribution_refs,
  residual_cognitive_work : JSONB NOT NULL sans schéma interne en base ;
- observation_text et invalidation_reason : TEXT (texte libre, jamais
  tronqué).

Conventions (identiques à 0005) : id UUID généré par l'application (aucun
server_default), timestamps TIMESTAMPTZ sans server_default, pas d'ENUM
PostgreSQL, FK sans ON DELETE / ON UPDATE (NO ACTION), aucun trigger,
aucune fonction. Seuls index : PK, ceux implicites des deux contraintes
UNIQUE et l'index unique partiel.

Identifiant de révision court (22 caractères) : alembic_version.version_num
est un VARCHAR(32).

Le downgrade supprime pedagogical_observations, puis l'index partiel, puis
observation_evaluation_runs, et revient exactement à 0005 sans toucher aux
autres tables. Ce n'est pas la stratégie normale de rollback production
(on privilégie le rollback applicatif).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0006_observation_layer"
down_revision: Union[str, Sequence[str], None] = "0005_cognitive_support_traces"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Crée observation_evaluation_runs, son index unique partiel, puis
    pedagogical_observations (et rien d'autre)."""
    op.create_table(
        "observation_evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("execution_status", sa.String(), nullable=False),
        sa.Column("interpretation_status", sa.String(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("re_evaluates_run_id", sa.Uuid(), nullable=True),
        sa.Column("evaluation_dedup_key", sa.String(), nullable=False),
        sa.Column("normalization_version", sa.String(), nullable=False),
        sa.Column("local_stage_version", sa.String(), nullable=False),
        # Référence logique T4 : volontairement sans FK (table cible
        # inexistante) ; la migration T4 ajoutera la contrainte.
        sa.Column("pedagogical_taxonomy_release_id", sa.Uuid(), nullable=True),
        sa.Column("capability_mapping_version", sa.String(), nullable=False),
        sa.Column("evaluation_schema_version", sa.String(), nullable=False),
        sa.Column("evaluator_version", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("prompt_spec_version", sa.String(), nullable=True),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("output_fingerprint", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.CheckConstraint(
            "execution_status IN ('running', 'completed', 'failed')",
            name="ck_observation_evaluation_runs_execution_status",
        ),
        sa.CheckConstraint(
            "interpretation_status IN ('candidate', 'active', 'superseded', 'obsolete')",
            name="ck_observation_evaluation_runs_interpretation_status",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["cognitive_events.id"]),
        sa.ForeignKeyConstraint(["re_evaluates_run_id"], ["observation_evaluation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evaluation_dedup_key",
            name="uq_observation_evaluation_runs_dedup_key",
        ),
    )
    op.create_index(
        "uq_observation_evaluation_runs_one_active_event",
        "observation_evaluation_runs",
        ["event_id"],
        unique=True,
        postgresql_where=sa.text("interpretation_status = 'active'"),
    )
    op.create_table(
        "pedagogical_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("competency_code", sa.String(), nullable=False),
        sa.Column("observation_role", sa.String(), nullable=False),
        sa.Column("task_kind", sa.String(), nullable=True),
        sa.Column("primary_user_action", postgresql.JSONB(), nullable=False),
        sa.Column("contributive_user_actions", postgresql.JSONB(), nullable=False),
        sa.Column("elicitation_mode", sa.String(), nullable=False),
        sa.Column("support_level", sa.String(), nullable=False),
        sa.Column("source_contribution_refs", postgresql.JSONB(), nullable=False),
        sa.Column("residual_cognitive_work", postgresql.JSONB(), nullable=False),
        sa.Column("polarity", sa.String(), nullable=False),
        sa.Column("evidence_strength", sa.String(), nullable=False),
        sa.Column("local_stage", sa.String(), nullable=True),
        sa.Column("contradiction_scope", sa.String(), nullable=True),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("observation_text", sa.Text(), nullable=False),
        sa.Column("capability_localization", sa.String(), nullable=False),
        sa.Column("integrity_status", sa.String(), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "competency_code IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', "
            "'C7', 'C8', 'C9', 'C10', 'C11', 'C12')",
            name="ck_pedagogical_observations_competency_code",
        ),
        sa.CheckConstraint(
            "observation_role IN ('primary', 'secondary')",
            name="ck_pedagogical_observations_observation_role",
        ),
        sa.CheckConstraint(
            "elicitation_mode IN ('prompted', 'spontaneous')",
            name="ck_pedagogical_observations_elicitation_mode",
        ),
        sa.CheckConstraint(
            "support_level IN ('none', 'hinted', 'guided', 'answer_given')",
            name="ck_pedagogical_observations_support_level",
        ),
        sa.CheckConstraint(
            "polarity IN ('supportive', 'contradictory')",
            name="ck_pedagogical_observations_polarity",
        ),
        sa.CheckConstraint(
            "evidence_strength IN ('weak', 'medium', 'strong')",
            name="ck_pedagogical_observations_evidence_strength",
        ),
        # mastery volontairement absent : jamais un stade local.
        sa.CheckConstraint(
            "local_stage IN ('none', 'discovery', 'comprehension', 'application')",
            name="ck_pedagogical_observations_local_stage",
        ),
        sa.CheckConstraint(
            "contradiction_scope IN ('recognition', 'comprehension', 'application', 'undetermined')",
            name="ck_pedagogical_observations_contradiction_scope",
        ),
        sa.CheckConstraint(
            "error_type IN ('conceptual', 'procedural', 'execution', 'factual_premise')",
            name="ck_pedagogical_observations_error_type",
        ),
        sa.CheckConstraint(
            "capability_localization IN ('localized', 'competency_only')",
            name="ck_pedagogical_observations_capability_localization",
        ),
        sa.CheckConstraint(
            "integrity_status IN ('valid', 'invalidated')",
            name="ck_pedagogical_observations_integrity_status",
        ),
        sa.CheckConstraint(
            "(polarity = 'supportive' AND local_stage IS NOT NULL AND contradiction_scope IS NULL) "
            "OR (polarity = 'contradictory' AND local_stage IS NULL AND contradiction_scope IS NOT NULL)",
            name="ck_pedagogical_observations_polarity_stage_scope",
        ),
        sa.CheckConstraint(
            "(integrity_status = 'valid' AND invalidated_at IS NULL AND invalidation_reason IS NULL) "
            "OR (integrity_status = 'invalidated' AND invalidated_at IS NOT NULL "
            "AND invalidation_reason IS NOT NULL)",
            name="ck_pedagogical_observations_integrity_invalidation",
        ),
        sa.ForeignKeyConstraint(["evaluation_run_id"], ["observation_evaluation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evaluation_run_id", "ordinal",
            name="uq_pedagogical_observations_run_ordinal",
        ),
    )


def downgrade() -> None:
    """Supprime pedagogical_observations, l'index partiel puis
    observation_evaluation_runs (retour exact à 0005)."""
    op.drop_table("pedagogical_observations")
    op.drop_index(
        "uq_observation_evaluation_runs_one_active_event",
        table_name="observation_evaluation_runs",
        postgresql_where=sa.text("interpretation_status = 'active'"),
    )
    op.drop_table("observation_evaluation_runs")
