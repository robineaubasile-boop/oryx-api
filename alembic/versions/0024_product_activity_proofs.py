"""V0-T08 Lot 2B-2, bloc A : registre provisoire des preuves d'utilisation
significative et attributions définitives des événements — socle
PostgreSQL DORMANT (EXPAND).

Revision ID: 0024_product_activity_proofs
Revises: 0023_analytics_cohort_sessions
Create Date: 2026-11-23

Migration purement additive : deux tables nouvelles et trois contraintes
UNIQUE composites ajoutées à des tables analytiques existantes (aucune
colonne, aucune ligne, aucune contrainte ni index existant modifié ou
retiré) ; les fichiers 0001 à 0023 ne sont pas modifiés ; aucune table F2
n'est touchée.

1. product_activity_proof_registry : preuve PROVISOIRE qu'une interaction
   d'activité acceptée a déclenché une opération métier canonique. Un
   enregistrement n'est jamais en soi la preuve d'une réussite.
   - Rattachement imposé en base par trois FK composites : participation
     (campaign_id, user_id) ; visite (visit_id, campaign_id, user_id) ->
     product_activity_sessions (id, campaign_id, user_id) ; interaction
     ACCEPTÉE (campaign_id, user_id, interaction_key, visit_id,
     interaction_kind) -> product_activity_session_interactions
     (campaign_id, user_id, interaction_key, session_id, interaction_kind),
     la visite de la preuve étant la session_id de l'interaction.
     interaction_kind = 'activity' (CHECK) : une clôture ('close') n'est
     jamais l'origine d'une preuve.
   - Identité métier canonique : couples (event_type, surface,
     source_operation_kind) fermés (CHECK) ; une seule preuve par opération
     (user_id, source_operation_kind, source_operation_id), tant que la
     ligne est conservée (aucune mémoire après purge).
   - États fermés pending / certified / recorded / rejected / indeterminate
     et formes de lignes autorisées (CHECK) ; motifs de résolution fermés,
     purement techniques ; chronologie created_at <= certified_at <=
     resolved_at ; conservation : purge_after_at = resolved_at + 168 heures
     (durée ABSOLUE : un intervalle exprimé en heures ne dépend ni du fuseau
     de session ni des changements d'heure), NULL tant que la preuve n'est
     pas résolue. Les transitions entre états relèvent des blocs B/C.
   - recorded_event_id : FK composite nullable vers l'attribution
     définitive, avec concordance exacte du propriétaire, de la campagne,
     de la visite, de l'interaction et de l'identité canonique ; une
     attribution au plus par preuve (UNIQUE). Sens unique : le registre
     dépend de l'attribution, jamais l'inverse (le registre peut être purgé
     sans toucher à l'attribution).

2. product_activity_event_attributions : attribution DÉFINITIVE d'un
   événement significatif (PK event_id) à son propriétaire, sa campagne, sa
   visite, son interaction d'activité et son opération canonique. Mêmes FK
   composites (participation, visite, interaction 'activity') et même
   CHECK de cohérence ; FK composite vers product_activity_events (id,
   user_id, visit_id, event_type, surface, source_operation_kind,
   source_operation_id) : concordance exacte avec l'événement ; une seule
   attribution par opération canonique (UNIQUE), en complément de l'index
   partiel uq_product_activity_events_source_operation. Conservée avec son
   événement (aucune politique ni durée propre ; aucune cascade : la purge
   future supprime la preuve, puis l'attribution, puis l'événement).

Contraintes ajoutées aux tables existantes (strictement nécessaires : une
FK composite PostgreSQL exige une contrainte UNIQUE portant exactement ses
colonnes référencées). Chacune est un sur-ensemble d'une PK existante, donc
satisfaite par toute ligne historique, sans réécriture de données :
- uq_product_activity_sessions_owner (id, campaign_id, user_id) : cible de
  la FK « visite du même propriétaire » ;
- uq_product_activity_session_interactions_origin (campaign_id, user_id,
  interaction_key, session_id, interaction_kind) : cible de la FK
  « interaction acceptée, de cette visite, de type activity » ;
- uq_product_activity_events_attribution (id, user_id, visit_id,
  event_type, surface, source_operation_kind, source_operation_id) : cible
  de la FK « concordance événement / attribution ».

Ce que le schéma NE prouve PAS : qu'une transaction F2 a réussi, ni la
source, l'outcome, la route exécutée, la confirmation de livraison,
l'instant admissible ou l'autorisation temporelle (fenêtre J+12,
finalisation J+12 + 30 minutes). Ces vérifications relèvent des services
des blocs B/C ; les FK ne sont pas une preuve SQL de réussite métier.

Aucun backfill, aucune ligne insérée, aucun trigger, aucune fonction
persistée, aucun ENUM, aucune extension, aucun server_default. Aucune
route, aucun service, aucune collecte : tables vides et dormantes.

Garde-fous de verrouillage (SET LOCAL, transaction Alembic) : 5 s d'attente
de verrou, 15 s par instruction, 15 s d'inactivité. Les trois ADD
CONSTRAINT verrouillent brièvement product_activity_sessions,
product_activity_session_interactions et product_activity_events (tables
dormantes ; l'index UNIQUE est construit sur leurs lignes existantes, sans
les modifier).

Downgrade PROTÉGÉ, dans une seule transaction : LOCK IN ACCESS EXCLUSIVE
MODE, dans un ordre déterministe (tables parentes puis dépendantes), de
toutes les tables dont le schéma change ou qui perdent une FK entrante ;
refus (exception, rien n'est supprimé) si l'une des deux nouvelles tables
contient une ligne — registre puis attributions, message propre à chaque
table — sinon suppression du registre, puis des attributions, puis des
trois contraintes ajoutées (retour exact à 0023, sans CASCADE). En mode
offline, le contrôle est un bloc DO évalué à l'exécution du script.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0024_product_activity_proofs"
down_revision: Union[str, Sequence[str], None] = "0023_analytics_cohort_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCK_GUARDS = (
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL statement_timeout = '15s'",
    "SET LOCAL idle_in_transaction_session_timeout = '15s'",
)
PARTICIPANTS = "product_campaign_participants"
EVENTS = "product_activity_events"
SESSIONS = "product_activity_sessions"
INTERACTIONS = "product_activity_session_interactions"
ATTRIBUTIONS = "product_activity_event_attributions"
REGISTRY = "product_activity_proof_registry"
TABLES = (ATTRIBUTIONS, REGISTRY)

PROOF_INTERACTION_KIND = "activity"
PROOF_STATUSES = ("pending", "certified", "recorded", "rejected", "indeterminate")
RESOLUTION_REASONS = ("no_new_effect", "source_unverified", "origin_unverified", "owner_conflict", "visit_conflict",
                      "operation_conflict", "window_expired", "finalization_expired", "not_authorized",
                      "technical_uncertainty")
# Couples (event_type, surface) -> natures d'opération canoniques admises.
# assistant_delivery : futurs parcours Éducation et Coach (non activés) ;
# les quatre autres sont les natures F2 de la PR #250 (attestations).
OPERATION_RULES = (
    ("education_used", "education", ("assistant_delivery",)),
    ("coach_used", "coach", ("assistant_delivery",)),
    ("decrypter_used", "decrypter", ("decryptage_delivery_ack",)),
    ("portfolio_used", "portfolio", ("portfolio_position_create", "portfolio_position_update",
                                     "portfolio_position_delete")),
)
RETENTION_AFTER_RESOLUTION = "168 hours"

# Expressions exactes, recopiées par core/models.py (concordance testée).
OPERATION_CHECK_SQL = (
    "(event_type = 'education_used' AND surface = 'education' AND source_operation_kind = 'assistant_delivery') "
    "OR (event_type = 'coach_used' AND surface = 'coach' AND source_operation_kind = 'assistant_delivery') "
    "OR (event_type = 'decrypter_used' AND surface = 'decrypter' "
    "AND source_operation_kind = 'decryptage_delivery_ack') "
    "OR (event_type = 'portfolio_used' AND surface = 'portfolio' AND source_operation_kind IN "
    "('portfolio_position_create', 'portfolio_position_update', 'portfolio_position_delete'))"
)
REGISTRY_CHECKS = (
    ("ck_product_activity_proof_registry_interaction_kind", "interaction_kind = 'activity'"),
    ("ck_product_activity_proof_registry_operation", OPERATION_CHECK_SQL),
    ("ck_product_activity_proof_registry_status",
     "status IN ('pending', 'certified', 'recorded', 'rejected', 'indeterminate')"),
    ("ck_product_activity_proof_registry_resolution_reason",
     "resolution_reason IS NULL OR resolution_reason IN ('no_new_effect', 'source_unverified', 'origin_unverified', "
     "'owner_conflict', 'visit_conflict', 'operation_conflict', 'window_expired', 'finalization_expired', "
     "'not_authorized', 'technical_uncertainty')"),
    # Formes de lignes autorisées par état (les transitions : blocs B/C).
    # Une preuve rejetée ou indéterminée garde sa certification éventuelle.
    ("ck_product_activity_proof_registry_state",
     "(status = 'pending' AND certified_at IS NULL AND resolved_at IS NULL AND resolution_reason IS NULL "
     "AND recorded_event_id IS NULL) "
     "OR (status = 'certified' AND certified_at IS NOT NULL AND resolved_at IS NULL AND resolution_reason IS NULL "
     "AND recorded_event_id IS NULL) "
     "OR (status = 'recorded' AND certified_at IS NOT NULL AND resolved_at IS NOT NULL "
     "AND resolution_reason IS NULL AND recorded_event_id IS NOT NULL) "
     "OR (status IN ('rejected', 'indeterminate') AND resolved_at IS NOT NULL AND resolution_reason IS NOT NULL "
     "AND recorded_event_id IS NULL)"),
    ("ck_product_activity_proof_registry_chronology",
     "(certified_at IS NULL OR certified_at >= created_at) "
     "AND (resolved_at IS NULL OR resolved_at >= created_at) "
     "AND (resolved_at IS NULL OR certified_at IS NULL OR resolved_at >= certified_at)"),
    # Chaque branche est vraie ou fausse, jamais inconnue (IS NOT NULL
    # explicite) : une preuve résolue sans échéance est refusée.
    ("ck_product_activity_proof_registry_purge",
     "(resolved_at IS NULL AND purge_after_at IS NULL) "
     "OR (resolved_at IS NOT NULL AND purge_after_at IS NOT NULL "
     "AND purge_after_at = resolved_at + interval '168 hours')"),
)
ATTRIBUTION_CHECKS = (
    ("ck_product_activity_event_attributions_interaction_kind", "interaction_kind = 'activity'"),
    ("ck_product_activity_event_attributions_operation", OPERATION_CHECK_SQL),
)

# Contraintes ajoutées aux tables existantes (cibles des FK composites).
SESSION_OWNER_UNIQUE = "uq_product_activity_sessions_owner"
SESSION_OWNER_COLUMNS = ("id", "campaign_id", "user_id")
INTERACTION_ORIGIN_UNIQUE = "uq_product_activity_session_interactions_origin"
INTERACTION_ORIGIN_COLUMNS = ("campaign_id", "user_id", "interaction_key", "session_id", "interaction_kind")
EVENT_ATTRIBUTION_UNIQUE = "uq_product_activity_events_attribution"
EVENT_ATTRIBUTION_COLUMNS = ("id", "user_id", "visit_id", "event_type", "surface", "source_operation_kind",
                             "source_operation_id")
EXISTING_TABLE_UNIQUES = (
    (SESSION_OWNER_UNIQUE, SESSIONS, SESSION_OWNER_COLUMNS),
    (INTERACTION_ORIGIN_UNIQUE, INTERACTIONS, INTERACTION_ORIGIN_COLUMNS),
    (EVENT_ATTRIBUTION_UNIQUE, EVENTS, EVENT_ATTRIBUTION_COLUMNS),
)

# Nouvelles tables : contraintes et index nommés.
ATTRIBUTION_PRIMARY_KEY = "product_activity_event_attributions_pkey"
ATTRIBUTION_PARTICIPANT_FK = "product_activity_event_attributions_participant_fkey"
ATTRIBUTION_VISIT_FK = "product_activity_event_attributions_visit_fkey"
ATTRIBUTION_INTERACTION_FK = "product_activity_event_attributions_interaction_fkey"
ATTRIBUTION_EVENT_FK = "product_activity_event_attributions_event_fkey"
ATTRIBUTION_OPERATION_UNIQUE = "uq_product_activity_event_attributions_operation"
ATTRIBUTION_PROOF_UNIQUE = "uq_product_activity_event_attributions_proof"
ATTRIBUTION_INTERACTION_INDEX = "ix_product_activity_event_attributions_interaction"
ATTRIBUTION_VISIT_INDEX = "ix_product_activity_event_attributions_visit"
REGISTRY_PRIMARY_KEY = "product_activity_proof_registry_pkey"
REGISTRY_PARTICIPANT_FK = "product_activity_proof_registry_participant_fkey"
REGISTRY_VISIT_FK = "product_activity_proof_registry_visit_fkey"
REGISTRY_INTERACTION_FK = "product_activity_proof_registry_interaction_fkey"
REGISTRY_RECORDED_EVENT_FK = "product_activity_proof_registry_recorded_event_fkey"
REGISTRY_OPERATION_UNIQUE = "uq_product_activity_proof_registry_operation"
REGISTRY_RECORDED_EVENT_UNIQUE = "uq_product_activity_proof_registry_recorded_event"
REGISTRY_CAMPAIGN_STATUS_INDEX = "ix_product_activity_proof_registry_campaign_status"
REGISTRY_INTERACTION_INDEX = "ix_product_activity_proof_registry_interaction"
REGISTRY_VISIT_INDEX = "ix_product_activity_proof_registry_visit"
REGISTRY_PURGE_INDEX = "ix_product_activity_proof_registry_purge_after_at"
REGISTRY_PURGE_WHERE = "purge_after_at IS NOT NULL"

OPERATION_COLUMNS = ("user_id", "source_operation_kind", "source_operation_id")
# Identité complète d'une attribution, référencée par le registre.
PROOF_IDENTITY = ("campaign_id", "user_id", "visit_id", "interaction_key", "interaction_kind", "event_type",
                  "surface", "source_operation_kind", "source_operation_id")
INTERACTION_FK_COLUMNS = ("campaign_id", "user_id", "interaction_key", "visit_id", "interaction_kind")
EVENT_FK_COLUMNS = ("event_id", "user_id", "visit_id", "event_type", "surface", "source_operation_kind",
                    "source_operation_id")

# Ordre déterministe des verrous : tables parentes, puis dépendantes.
DOWNGRADE_LOCK = (f"LOCK TABLE {PARTICIPANTS}, {EVENTS}, {SESSIONS}, {INTERACTIONS}, {ATTRIBUTIONS}, {REGISTRY} "
                  "IN ACCESS EXCLUSIVE MODE")
# Ordre des contrôles : chaque table examinée à part (message propre).
BLOCKING_ORDER = (REGISTRY, ATTRIBUTIONS)


def downgrade_message(table: str) -> str:
    return (f"V0-T08 Lot 2B-2 : downgrade de 0024_product_activity_proofs refusé, la table {table} contient "
            "des données. Aucune donnée supprimée.")


def _identity_columns() -> list:
    return [
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("visit_id", sa.Uuid(), nullable=False),
        sa.Column("interaction_key", sa.Uuid(), nullable=False),
        sa.Column("interaction_kind", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("surface", sa.String(length=32), nullable=False),
        sa.Column("source_operation_kind", sa.String(length=64), nullable=False),
        sa.Column("source_operation_id", sa.Uuid(), nullable=False),
    ]


def _origin_foreign_keys(prefix: str) -> list:
    """Participation, visite du même propriétaire, interaction acceptée de
    cette visite (sa session_id) et de type activity."""
    return [
        sa.ForeignKeyConstraint(["campaign_id", "user_id"],
                                [f"{PARTICIPANTS}.campaign_id", f"{PARTICIPANTS}.user_id"],
                                name=f"{prefix}_participant_fkey"),
        sa.ForeignKeyConstraint(["visit_id", "campaign_id", "user_id"],
                                [f"{SESSIONS}.{c}" for c in SESSION_OWNER_COLUMNS],
                                name=f"{prefix}_visit_fkey"),
        sa.ForeignKeyConstraint(list(INTERACTION_FK_COLUMNS),
                                [f"{INTERACTIONS}.{c}" for c in INTERACTION_ORIGIN_COLUMNS],
                                name=f"{prefix}_interaction_fkey"),
    ]


def upgrade() -> None:
    """Trois UNIQUE composites, deux tables vides et leurs index ; rien
    d'autre."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    for name, table, columns in EXISTING_TABLE_UNIQUES:
        op.create_unique_constraint(name, table, list(columns))
    op.create_table(
        ATTRIBUTIONS,
        sa.Column("event_id", sa.Uuid(), nullable=False),
        *_identity_columns(),
        sa.Column("certified_at", sa.DateTime(timezone=True), nullable=False),
        *(sa.CheckConstraint(expression, name=name) for name, expression in ATTRIBUTION_CHECKS),
        *_origin_foreign_keys(ATTRIBUTIONS),
        sa.ForeignKeyConstraint(list(EVENT_FK_COLUMNS), [f"{EVENTS}.{c}" for c in EVENT_ATTRIBUTION_COLUMNS],
                                name=ATTRIBUTION_EVENT_FK),
        sa.PrimaryKeyConstraint("event_id", name=ATTRIBUTION_PRIMARY_KEY),
        sa.UniqueConstraint(*OPERATION_COLUMNS, name=ATTRIBUTION_OPERATION_UNIQUE),
        sa.UniqueConstraint("event_id", *PROOF_IDENTITY, name=ATTRIBUTION_PROOF_UNIQUE),
    )
    op.create_index(ATTRIBUTION_INTERACTION_INDEX, ATTRIBUTIONS, ["campaign_id", "user_id", "interaction_key"])
    op.create_index(ATTRIBUTION_VISIT_INDEX, ATTRIBUTIONS, ["visit_id"])
    op.create_table(
        REGISTRY,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_identity_columns(),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("certified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_event_id", sa.Uuid(), nullable=True),
        sa.Column("resolution_reason", sa.String(length=64), nullable=True),
        sa.Column("purge_after_at", sa.DateTime(timezone=True), nullable=True),
        *(sa.CheckConstraint(expression, name=name) for name, expression in REGISTRY_CHECKS),
        *_origin_foreign_keys(REGISTRY),
        sa.ForeignKeyConstraint(["recorded_event_id", *PROOF_IDENTITY],
                                [f"{ATTRIBUTIONS}.{c}" for c in ("event_id", *PROOF_IDENTITY)],
                                name=REGISTRY_RECORDED_EVENT_FK),
        sa.PrimaryKeyConstraint("id", name=REGISTRY_PRIMARY_KEY),
        sa.UniqueConstraint(*OPERATION_COLUMNS, name=REGISTRY_OPERATION_UNIQUE),
        sa.UniqueConstraint("recorded_event_id", name=REGISTRY_RECORDED_EVENT_UNIQUE),
    )
    op.create_index(REGISTRY_CAMPAIGN_STATUS_INDEX, REGISTRY, ["campaign_id", "status"])
    op.create_index(REGISTRY_INTERACTION_INDEX, REGISTRY, ["campaign_id", "user_id", "interaction_key"])
    op.create_index(REGISTRY_VISIT_INDEX, REGISTRY, ["visit_id"])
    op.create_index(REGISTRY_PURGE_INDEX, REGISTRY, ["purge_after_at"],
                    postgresql_where=sa.text(REGISTRY_PURGE_WHERE))


def downgrade() -> None:
    """Retour exact à 0023, seulement si les deux nouvelles tables sont
    vides."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.execute(DOWNGRADE_LOCK)
    if context.is_offline_mode():
        branches = []
        for position, table in enumerate(BLOCKING_ORDER):
            keyword = "IF" if position == 0 else "ELSIF"
            message = downgrade_message(table).replace("'", "''")
            branches.append(f"{keyword} EXISTS (SELECT 1 FROM {table}) THEN RAISE EXCEPTION '{message}';")
        op.execute(f"DO $$ BEGIN {' '.join(branches)} END IF; END $$")
    else:
        bind = op.get_bind()
        for table in BLOCKING_ORDER:
            if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one():
                raise RuntimeError(downgrade_message(table))
    op.drop_table(REGISTRY)
    op.drop_table(ATTRIBUTIONS)
    for name, table, _ in reversed(EXISTING_TABLE_UNIQUES):
        op.drop_constraint(name, table, type_="unique")
