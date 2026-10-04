"""R1-B : idempotence et identité des CognitiveEvents (EXPAND).

Revision ID: 0010_r1b_event_idempotence
Revises: 0009_competency_inference_state
Create Date: 2026-10-04

Rend la couche T2 robuste aux retries, à la concurrence et aux collisions
d'identité, sans brancher l'application (aucune route, aucun Web-V2, aucun
T3) :

A. conversation_identities : registre MINIMAL d'appartenance
   conversation_key -> exactement un user_id (R1-A). Trois colonnes et
   rien d'autre : conversation_key VARCHAR (PK, défense finale sous
   concurrence), user_id VARCHAR NOT NULL (FK users.id), created_at
   TIMESTAMPTZ NOT NULL. Ce n'est PAS un système de conversation : ni
   messages, ni historique, ni surface, ni titre, ni lifecycle.

B. cognitive_events : quatre colonnes VARCHAR NULLABLE event_dedup_key,
   event_builder_version, admission_version, event_schema_version. NULL =
   ligne legacy pré-R1-B (aucune n'est backfillée ni réécrite : aucune
   version « v1 » artificielle, aucun recalcul de clé) ; toute ligne créée
   par le service R1-B les renseigne toutes les quatre (garantie
   applicative, core/cognitive_capture.py).

C. cognitive_events.task_kind devient NULLABLE : une démonstration
   cognitive spontanée peut ne correspondre à aucune tâche imposée ; aucune
   fausse valeur (« spontaneous », « unknown »...) n'est inventée pour la
   base.

D. UNIQUE(event_dedup_key) nommée uq_cognitive_events_event_dedup_key :
   défense finale de l'idempotence d'ouverture. PostgreSQL autorise
   plusieurs NULL : les lignes legacy restent intactes.

Aucune autre table modifiée, aucun index supplémentaire (seuls ceux
implicites de la PK et de l'UNIQUE), aucun trigger, aucune fonction, aucun
ENUM, aucun server_default (timestamps générés par l'application), FK sans
ON DELETE / ON UPDATE (NO ACTION). Aucune donnée écrite hors
alembic_version.

Identifiant de révision court (26 caractères) : alembic_version.version_num
est un VARCHAR(32).

Downgrade : supprime l'UNIQUE, restaure task_kind NOT NULL, retire les
quatre colonnes puis supprime conversation_identities (retour exact à
0009). Limite assumée : si des événements R1-B à task_kind NULL existent,
le retour à NOT NULL échoue (aucun backfill artificiel n'est fait pour le
rendre permissif) ; ce n'est pas la stratégie normale de rollback
production (on privilégie le rollback applicatif).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0010_r1b_event_idempotence"
down_revision: Union[str, Sequence[str], None] = "0009_competency_inference_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_EVENT_COLUMNS = ("event_dedup_key", "event_builder_version", "admission_version", "event_schema_version")


def upgrade() -> None:
    """Registre conversation_identities, quatre colonnes d'identité /
    versioning, task_kind nullable, UNIQUE(event_dedup_key)."""
    op.create_table(
        "conversation_identities",
        sa.Column("conversation_key", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("conversation_key"),
    )
    for column in NEW_EVENT_COLUMNS:
        op.add_column("cognitive_events", sa.Column(column, sa.String(), nullable=True))
    op.alter_column("cognitive_events", "task_kind", existing_type=sa.String(), nullable=True)
    op.create_unique_constraint("uq_cognitive_events_event_dedup_key", "cognitive_events", ["event_dedup_key"])


def downgrade() -> None:
    """Retour exact à 0009 (échoue si un événement a task_kind NULL)."""
    op.drop_constraint("uq_cognitive_events_event_dedup_key", "cognitive_events", type_="unique")
    op.alter_column("cognitive_events", "task_kind", existing_type=sa.String(), nullable=False)
    for column in reversed(NEW_EVENT_COLUMNS):
        op.drop_column("cognitive_events", column)
    op.drop_table("conversation_identities")
