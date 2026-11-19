"""V0-T08 Lot 1 : socle PostgreSQL DORMANT de l'analytics produit (EXPAND).

Revision ID: 0022_product_analytics_v1
Revises: 0021_decryptage_reservations
Create Date: 2026-11-19

Migration purement additive : trois tables nouvelles, aucune table, colonne,
contrainte ni index existant n'est modifié ; les fichiers 0001 à 0021 ne
sont pas modifiés.

1. product_campaigns : campagne de mesure (code UNIQUE, statut fermé
   planned / active / closed / cancelled, durée de conservation explicite
   > 0 sans valeur implicite, versions de politique de conservation et de
   mesure). CHECK nommés : statut, durée, chronologie created_at <=
   activated_at <= closed_at <= close_by_at, et une campagne closed porte
   activated_at et closed_at.

2. product_campaign_participants : inscription d'un utilisateur à une
   campagne (PK composite (campaign_id, user_id) : une seule inscription par
   campagne, plusieurs campagnes possibles), fenêtre d'observation
   semi-ouverte [window_start_at, window_end_at) non vide, statut fermé
   enrolled / completed / withdrawn. authorization_recorded_at et
   notice_version sont des champs techniques documentaires, jamais une
   preuve juridique de consentement.

3. product_activity_events : un événement d'activité produit (sept types
   fermés, trois sources, quatre outcomes, quatre statuts de livraison dont
   « unconfirmed »). Aucune donnée financière, aucun texte de question,
   prompt, réponse IA, donnée pédagogique ni secret. Unicités : clé
   d'idempotence par utilisateur, un seul session_started par (user_id,
   visit_id) (index unique partiel), une seule ligne par opération métier
   canonique d'un propriétaire (user_id, source_operation_kind,
   source_operation_id) lorsqu'elles sont renseignées (index unique partiel,
   scindé par propriétaire comme le contrat Portfolio (user_id,
   operation_type, operation_id) ; la paire est entièrement présente ou
   entièrement absente). Conservation : retention_scopes (document JSONB
   versionné retention-scopes-v1), next_scope_expires_at <= purge_after_at.

Garanties SQL réelles : uniquement les CHECK, FK, PK, UNIQUE et index
ci-dessous. Le CHECK de retention_scopes ne porte que sur la structure
EXTÉRIEURE (objet aux seules clés version et scopes, version égale à la
chaîne JSON "retention-scopes-v1", scopes tableau non vide) : il ne valide
ni le contenu de chaque finalité, ni l'existence transactionnelle des
campaign_id référencés, ni la cohérence des échéances avec les deux
colonnes, ni l'immutabilité historique (raccourcissement seul, aucune
finalité rétroactive) : ces invariants
relèvent des futurs services d'écriture. Les transitions d'état des
campagnes, l'interdiction de réouverture et l'immutabilité des politiques
après activation relèvent aussi de services futurs.

Aucun backfill, aucune ligne insérée (ni campagne, ni participant, ni
événement, ni utilisateur), aucun trigger, aucune fonction persistée, aucun
ENUM, aucune extension, aucun server_default ; aucune donnée écrite hors
alembic_version. Aucune route, aucune collecte : les tables restent vides
et dormantes tant qu'un lot ultérieur ne les raccorde pas.

Garde-fous de verrouillage (SET LOCAL, transaction Alembic) : 5 s d'attente
de verrou, 15 s par instruction, 15 s d'inactivité. Un verrou DDL qui
n'est pas obtenu fait échouer la migration (jamais masqué).

Downgrade PROTÉGÉ, dans une seule transaction : LOCK des trois tables IN
ACCESS EXCLUSIVE MODE dans un ordre déterministe (product_campaigns,
product_campaign_participants, product_activity_events), refus (exception,
rien n'est supprimé) si l'une d'elles contient une ligne, contrôlées dans
l'ordre événements, participants, campagnes (message propre à chaque
table), sinon suppression des tables enfants puis parente (retour exact à
0021, sans CASCADE). En mode offline, le contrôle est un bloc DO évalué à
l'exécution du script.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0022_product_analytics_v1"
down_revision: Union[str, Sequence[str], None] = "0021_decryptage_reservations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCK_GUARDS = (
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL statement_timeout = '15s'",
    "SET LOCAL idle_in_transaction_session_timeout = '15s'",
)
CAMPAIGNS = "product_campaigns"
PARTICIPANTS = "product_campaign_participants"
EVENTS = "product_activity_events"
TABLES = (CAMPAIGNS, PARTICIPANTS, EVENTS)

CAMPAIGN_STATUSES = ("planned", "active", "closed", "cancelled")
PARTICIPATION_STATUSES = ("enrolled", "completed", "withdrawn")
EVENT_TYPES = ("session_started", "education_used", "coach_used", "decrypter_used", "portfolio_used",
               "profile_viewed", "view_opened")
SOURCES = ("client_signal", "server_business", "server_visit")
OUTCOMES = ("in_progress", "succeeded", "failed", "indeterminate")
DELIVERY_STATUSES = ("not_applicable", "pending", "confirmed", "unconfirmed")
RETENTION_SCOPES_VERSION = "retention-scopes-v1"


# Expressions exactes, recopiées par core/models.py (concordance testée).
CAMPAIGN_CHECKS = (
    ("ck_product_campaigns_status", "status IN ('planned', 'active', 'closed', 'cancelled')"),
    ("ck_product_campaigns_retention_days", "retention_days > 0"),
    ("ck_product_campaigns_close_by_at", "close_by_at >= created_at"),
    ("ck_product_campaigns_activated_at", "activated_at IS NULL OR activated_at >= created_at"),
    ("ck_product_campaigns_closed_at",
     "closed_at IS NULL OR ((activated_at IS NULL OR closed_at >= activated_at) AND closed_at <= close_by_at)"),
    ("ck_product_campaigns_closed", "status <> 'closed' OR (activated_at IS NOT NULL AND closed_at IS NOT NULL)"),
)
PARTICIPANT_CHECKS = (
    ("ck_product_campaign_participants_participation_status",
     "participation_status IN ('enrolled', 'completed', 'withdrawn')"),
    ("ck_product_campaign_participants_window", "window_end_at > window_start_at"),
)
EVENT_CHECKS = (
    ("ck_product_activity_events_event_type",
     "event_type IN ('session_started', 'education_used', 'coach_used', 'decrypter_used', 'portfolio_used', "
     "'profile_viewed', 'view_opened')"),
    ("ck_product_activity_events_source", "source IN ('client_signal', 'server_business', 'server_visit')"),
    ("ck_product_activity_events_outcome",
     "outcome IN ('in_progress', 'succeeded', 'failed', 'indeterminate')"),
    ("ck_product_activity_events_delivery_status",
     "delivery_status IN ('not_applicable', 'pending', 'confirmed', 'unconfirmed')"),
    ("ck_product_activity_events_source_operation",
     "(source_operation_kind IS NULL) = (source_operation_id IS NULL)"),
    ("ck_product_activity_events_timestamps",
     "recorded_at >= occurred_at AND updated_at >= recorded_at "
     "AND (resolved_at IS NULL OR resolved_at >= occurred_at) "
     "AND (delivered_at IS NULL OR delivered_at >= occurred_at)"),
    ("ck_product_activity_events_retention_window", "next_scope_expires_at <= purge_after_at"),
    # Structure EXTÉRIEURE seulement. Les CASE garantissent que l'opérateur -
    # n'est appliqué qu'à un objet et jsonb_array_length qu'à un tableau
    # (l'ordre d'évaluation d'un AND n'est pas garanti par PostgreSQL). La
    # version est comparée par égalité JSONB EXACTE à la chaîne JSON
    # "retention-scopes-v1" (ni tableau, ni objet, ni autre type) ; COALESCE
    # rend le CHECK faux, jamais inconnu, quand la clé est absente.
    ("ck_product_activity_events_retention_scopes",
     "CASE WHEN jsonb_typeof(retention_scopes) = 'object' "
     "THEN (retention_scopes - 'version' - 'scopes') = '{}'::jsonb "
     "AND COALESCE((retention_scopes -> 'version') = '\"retention-scopes-v1\"'::jsonb, false) "
     "AND CASE WHEN jsonb_typeof(retention_scopes -> 'scopes') = 'array' "
     "THEN jsonb_array_length(retention_scopes -> 'scopes') > 0 ELSE false END "
     "ELSE false END"),
)

CAMPAIGN_CODE_UNIQUE = "uq_product_campaigns_code"
PARTICIPANT_CAMPAIGN_FK = "product_campaign_participants_campaign_id_fkey"
PARTICIPANT_USER_FK = "product_campaign_participants_user_id_fkey"
PARTICIPANT_USER_WINDOW_INDEX = "ix_product_campaign_participants_user_window"
EVENT_USER_FK = "product_activity_events_user_id_fkey"
EVENT_IDEMPOTENCY_UNIQUE = "uq_product_activity_events_user_idempotency_key"
EVENT_USER_OCCURRED_INDEX = "ix_product_activity_events_user_occurred"
EVENT_USER_VISIT_INDEX = "ix_product_activity_events_user_visit"
EVENT_NEXT_EXPIRY_INDEX = "ix_product_activity_events_next_scope_expires_at"
EVENT_PURGE_INDEX = "ix_product_activity_events_purge_after_at"
EVENT_SESSION_START_UNIQUE = "uq_product_activity_events_visit_session_start"
EVENT_SESSION_START_WHERE = "event_type = 'session_started'"
EVENT_SOURCE_OPERATION_UNIQUE = "uq_product_activity_events_source_operation"
EVENT_SOURCE_OPERATION_WHERE = "source_operation_kind IS NOT NULL AND source_operation_id IS NOT NULL"

# Ordre déterministe des verrous : la table parente, puis ses dépendantes.
DOWNGRADE_LOCK = f"LOCK TABLE {CAMPAIGNS}, {PARTICIPANTS}, {EVENTS} IN ACCESS EXCLUSIVE MODE"
# Ordre des contrôles : chaque table est examinée à part (message propre),
# les dépendantes d'abord, de sorte que chaque garde est observable.
BLOCKING_ORDER = (EVENTS, PARTICIPANTS, CAMPAIGNS)


def downgrade_message(table: str) -> str:
    return (f"V0-T08 Lot 1 : downgrade de 0022_product_analytics_v1 refusé, la table {table} contient des "
            "données. Aucune donnée supprimée.")


def upgrade() -> None:
    """Trois tables analytiques vides et leurs index ; rien d'autre."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.create_table(
        CAMPAIGNS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_by_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_days", sa.SmallInteger(), nullable=False),
        sa.Column("retention_policy_version", sa.String(length=64), nullable=False),
        sa.Column("measurement_policy_version", sa.String(length=64), nullable=False),
        *(sa.CheckConstraint(expression, name=name) for name, expression in CAMPAIGN_CHECKS),
        sa.PrimaryKeyConstraint("id", name="product_campaigns_pkey"),
        sa.UniqueConstraint("code", name=CAMPAIGN_CODE_UNIQUE),
    )
    op.create_table(
        PARTICIPANTS,
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("participation_status", sa.String(length=16), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorization_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notice_version", sa.String(length=64), nullable=True),
        *(sa.CheckConstraint(expression, name=name) for name, expression in PARTICIPANT_CHECKS),
        sa.ForeignKeyConstraint(["campaign_id"], [f"{CAMPAIGNS}.id"], name=PARTICIPANT_CAMPAIGN_FK),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=PARTICIPANT_USER_FK),
        sa.PrimaryKeyConstraint("campaign_id", "user_id", name="product_campaign_participants_pkey"),
    )
    op.create_index(PARTICIPANT_USER_WINDOW_INDEX, PARTICIPANTS, ["user_id", "window_start_at", "window_end_at"])
    op.create_table(
        EVENTS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("visit_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("surface", sa.String(length=32), nullable=False),
        sa.Column("executed_route", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("source_operation_kind", sa.String(length=64), nullable=True),
        sa.Column("source_operation_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("delivery_status", sa.String(length=24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_scopes", postgresql.JSONB(), nullable=False),
        sa.Column("next_scope_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purge_after_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        *(sa.CheckConstraint(expression, name=name) for name, expression in EVENT_CHECKS),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=EVENT_USER_FK),
        sa.PrimaryKeyConstraint("id", name="product_activity_events_pkey"),
        sa.UniqueConstraint("user_id", "idempotency_key", name=EVENT_IDEMPOTENCY_UNIQUE),
    )
    op.create_index(EVENT_USER_OCCURRED_INDEX, EVENTS,
                    ["user_id", sa.text("occurred_at DESC"), sa.text("id DESC")])
    op.create_index(EVENT_USER_VISIT_INDEX, EVENTS, ["user_id", "visit_id"])
    op.create_index(EVENT_NEXT_EXPIRY_INDEX, EVENTS, ["next_scope_expires_at"])
    op.create_index(EVENT_PURGE_INDEX, EVENTS, ["purge_after_at"])
    op.create_index(EVENT_SESSION_START_UNIQUE, EVENTS, ["user_id", "visit_id"], unique=True,
                    postgresql_where=sa.text(EVENT_SESSION_START_WHERE))
    op.create_index(EVENT_SOURCE_OPERATION_UNIQUE, EVENTS, ["user_id", "source_operation_kind", "source_operation_id"],
                    unique=True, postgresql_where=sa.text(EVENT_SOURCE_OPERATION_WHERE))


def downgrade() -> None:
    """Retour exact à 0021, seulement si les trois tables sont vides."""
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
    op.drop_table(EVENTS)
    op.drop_table(PARTICIPANTS)
    op.drop_table(CAMPAIGNS)
