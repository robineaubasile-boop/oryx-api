"""V0-T08 Lot 2B-2, bloc A : registre provisoire des preuves d'utilisation
significative et attributions définitives des événements — socle PostgreSQL
DORMANT (EXPAND).

Revision ID: 0024_product_activity_proofs
Revises: 0023_analytics_cohort_sessions
Create Date: 2026-11-23

Migration purement additive : deux tables nouvelles et trois contraintes
UNIQUE composites ajoutées à des tables analytiques existantes ; aucune
colonne, aucune ligne, aucune autre contrainte ni aucun index existant n'est
modifié ; les fichiers 0001 à 0023 ne sont pas modifiés ; aucune table F2
n'est touchée (aucune FK vers une table F2).

1. Contraintes UNIQUE composites ajoutées (cibles de FK composites ; chacune
   contient la clé primaire de sa table, donc ne peut entrer en conflit avec
   aucune ligne existante ni future et n'interdit aucun usage 0022 / 0023) :
   - product_activity_sessions (id, campaign_id, user_id) : la visite
     appartient à CETTE participation ;
   - product_activity_session_interactions (campaign_id, user_id,
     interaction_key, session_id, interaction_kind) : la clé d'interaction
     acceptée appartient à CETTE visite et est de CE type ;
   - product_activity_events (id, user_id, visit_id, event_type, surface,
     source_operation_kind, source_operation_id) : l'attribution concorde
     avec l'événement (propriétaire, visite, type, surface, opération).

2. product_activity_proof_registry : preuve PROVISOIRE d'une utilisation
   significative (ce n'est pas un journal statistique ; une ligne provisoire
   ne signifie jamais que l'opération a réussi). États fermés pending /
   certified / recorded / rejected / indeterminate ; formes de ligne
   imposées par CHECK (les TRANSITIONS relèvent des services B/C) :
   - pending : ni certification, ni résolution ;
   - certified : certification, sans résolution ;
   - recorded : certification, résolution et recorded_event_id ;
   - rejected / indeterminate : résolution et motif fermé, sans événement
     (une certification antérieure éventuelle est conservée) ;
   chronologie created_at <= certified_at <= resolved_at ; conservation
   purge_after_at = resolved_at + 168 heures (durée ABSOLUE, indépendante
   des changements d'heure) pour une preuve résolue, NULL sinon. Aucune
   purge automatique (lot 2C).

3. product_activity_event_attributions : attribution DÉFINITIVE d'un
   événement significatif à son propriétaire, sa visite, son interaction et
   son opération métier canonique ; certified_at obligatoire. Elle ne
   dépend pas du registre provisoire (aucune FK vers lui) et se conserve
   avec l'événement associé (aucune politique de conservation propre).

Rattachement garanti EN BASE, pour chacune des deux tables, par FK
composites :
   - (campaign_id, user_id) -> participation existante ;
   - (visit_id, campaign_id, user_id) -> visite réelle du même propriétaire ;
   - (campaign_id, user_id, interaction_key, visit_id, interaction_kind) ->
     interaction ACCEPTÉE de cette visite, interaction_kind = 'activity'
     imposé par CHECK (une clé 'close' ne peut jamais servir d'origine) ;
   - attribution seulement : (event_id, user_id, visit_id, event_type,
     surface, source_operation_kind, source_operation_id) -> événement
     concordant ;
   - registre seulement, FK nullable : (recorded_event_id, campaign_id,
     user_id, visit_id, interaction_key, event_type, surface,
     source_operation_kind, source_operation_id) -> attribution concordante
     (aucune preuve de A rattachée à l'attribution de B).
Couples (event_type, surface, source_operation_kind) fermés par CHECK :
   education_used / education / assistant_delivery ;
   coach_used / coach / assistant_delivery ;
   decrypter_used / decrypter / decryptage_delivery_ack ;
   portfolio_used / portfolio / portfolio_position_create | _update | _delete.
Unicité de l'opération canonique (user_id, source_operation_kind,
source_operation_id) dans chacune des deux tables, tant que les lignes
concernées sont conservées (aucune identité éternelle après purge).

LIMITE : ces contraintes prouvent l'IDENTITÉ et le RATTACHEMENT, jamais la
réussite métier. source, outcome, executed_route, confirmation de
livraison, instant admissible et autorisation temporelle restent vérifiés
par les services des blocs B/C.

Aucun backfill, aucune ligne insérée, aucun trigger, aucune fonction
persistée, aucun ENUM, aucune extension, aucun server_default, aucune
cascade. Aucune route, aucune collecte : tables vides et dormantes.

Garde-fous de verrouillage (SET LOCAL, transaction Alembic) : 5 s d'attente
de verrou, 15 s par instruction, 15 s d'inactivité. L'ajout des UNIQUE prend
un verrou ACCESS EXCLUSIVE bref sur les trois tables existantes (vides et
dormantes) ; les FK prennent un verrou SHARE ROW EXCLUSIVE sur les tables
référencées.

Downgrade PROTÉGÉ, dans une seule transaction : LOCK IN ACCESS EXCLUSIVE
MODE, en une instruction et dans un ordre déterministe (parents puis
dépendants), de toutes les tables dont le downgrade modifie le catalogue
(participations, événements, sessions, interactions, attributions,
registre) ; refus (exception, rien n'est supprimé) si le registre ou les
attributions contiennent une ligne — contrôlés dans cet ordre, message
propre à chaque table — sinon suppression du registre puis des
attributions, puis retrait des trois UNIQUE ajoutées (retour exact à 0023,
sans CASCADE). En mode offline, le contrôle est un bloc DO évalué à
l'exécution du script.
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

PROOF_STATUSES = ("pending", "certified", "recorded", "rejected", "indeterminate")
RESOLUTION_REASONS = ("no_new_effect", "source_unverified", "origin_unverified", "owner_conflict", "visit_conflict",
                      "operation_conflict", "window_expired", "finalization_expired", "not_authorized",
                      "technical_uncertainty")
PROOF_INTERACTION_KIND = "activity"
# Couples fermés (event_type, surface) -> natures d'opération canoniques.
OPERATION_RULES = (
    ("education_used", "education", ("assistant_delivery",)),
    ("coach_used", "coach", ("assistant_delivery",)),
    ("decrypter_used", "decrypter", ("decryptage_delivery_ack",)),
    ("portfolio_used", "portfolio", ("portfolio_position_create", "portfolio_position_update",
                                     "portfolio_position_delete")),
)
# Conservation du registre provisoire : 7 x 24 heures ABSOLUES après la
# résolution (jamais '7 days', qui suit les changements d'heure du fuseau
# de session).
REGISTRY_RETENTION = "168 hours"

# Expressions exactes, recopiées par core/models.py (concordance testée).
OPERATION_EXPRESSION = (
    "(event_type = 'education_used' AND surface = 'education' AND source_operation_kind = 'assistant_delivery') "
    "OR (event_type = 'coach_used' AND surface = 'coach' AND source_operation_kind = 'assistant_delivery') "
    "OR (event_type = 'decrypter_used' AND surface = 'decrypter' "
    "AND source_operation_kind = 'decryptage_delivery_ack') "
    "OR (event_type = 'portfolio_used' AND surface = 'portfolio' AND source_operation_kind IN "
    "('portfolio_position_create', 'portfolio_position_update', 'portfolio_position_delete'))"
)
REGISTRY_CHECKS = (
    ("ck_product_activity_proof_registry_status",
     "status IN ('pending', 'certified', 'recorded', 'rejected', 'indeterminate')"),
    ("ck_product_activity_proof_registry_interaction_kind", "interaction_kind = 'activity'"),
    ("ck_product_activity_proof_registry_operation", OPERATION_EXPRESSION),
    ("ck_product_activity_proof_registry_resolution_reason",
     "resolution_reason IS NULL OR resolution_reason IN ('no_new_effect', 'source_unverified', "
     "'origin_unverified', 'owner_conflict', 'visit_conflict', 'operation_conflict', 'window_expired', "
     "'finalization_expired', 'not_authorized', 'technical_uncertainty')"),
    ("ck_product_activity_proof_registry_state",
     "(status = 'pending' AND certified_at IS NULL AND resolved_at IS NULL AND recorded_event_id IS NULL "
     "AND resolution_reason IS NULL) "
     "OR (status = 'certified' AND certified_at IS NOT NULL AND resolved_at IS NULL AND recorded_event_id IS NULL "
     "AND resolution_reason IS NULL) "
     "OR (status = 'recorded' AND certified_at IS NOT NULL AND resolved_at IS NOT NULL "
     "AND recorded_event_id IS NOT NULL AND resolution_reason IS NULL) "
     "OR (status IN ('rejected', 'indeterminate') AND resolved_at IS NOT NULL AND recorded_event_id IS NULL "
     "AND resolution_reason IS NOT NULL)"),
    ("ck_product_activity_proof_registry_chronology",
     "(certified_at IS NULL OR certified_at >= created_at) "
     "AND (resolved_at IS NULL OR (resolved_at >= created_at "
     "AND (certified_at IS NULL OR resolved_at >= certified_at)))"),
    ("ck_product_activity_proof_registry_purge_after",
     "(resolved_at IS NULL AND purge_after_at IS NULL) "
     "OR (resolved_at IS NOT NULL AND purge_after_at IS NOT NULL "
     "AND purge_after_at = resolved_at + interval '168 hours')"),
)
ATTRIBUTION_CHECKS = (
    ("ck_product_activity_event_attributions_interaction_kind", "interaction_kind = 'activity'"),
    ("ck_product_activity_event_attributions_operation", OPERATION_EXPRESSION),
)

# Contraintes UNIQUE ajoutées aux tables existantes (cibles de FK).
SESSION_OWNER_UNIQUE = "uq_product_activity_sessions_owner"
SESSION_OWNER_COLUMNS = ("id", "campaign_id", "user_id")
INTERACTION_ORIGIN_UNIQUE = "uq_product_activity_session_interactions_origin"
INTERACTION_ORIGIN_COLUMNS = ("campaign_id", "user_id", "interaction_key", "session_id", "interaction_kind")
EVENT_ATTRIBUTION_UNIQUE = "uq_product_activity_events_attribution_identity"
EVENT_ATTRIBUTION_COLUMNS = ("id", "user_id", "visit_id", "event_type", "surface", "source_operation_kind",
                             "source_operation_id")
# Ordre de création (lignée 0022 puis 0023) ; le downgrade suit l'ordre inverse.
EXISTING_UNIQUES = (
    (EVENTS, EVENT_ATTRIBUTION_UNIQUE, EVENT_ATTRIBUTION_COLUMNS),
    (SESSIONS, SESSION_OWNER_UNIQUE, SESSION_OWNER_COLUMNS),
    (INTERACTIONS, INTERACTION_ORIGIN_UNIQUE, INTERACTION_ORIGIN_COLUMNS),
)

ATTRIBUTION_PRIMARY_KEY = "product_activity_event_attributions_pkey"
ATTRIBUTION_PARTICIPANT_FK = "product_activity_event_attributions_participant_fkey"
ATTRIBUTION_SESSION_FK = "product_activity_event_attributions_session_fkey"
ATTRIBUTION_INTERACTION_FK = "product_activity_event_attributions_interaction_fkey"
ATTRIBUTION_EVENT_FK = "product_activity_event_attributions_event_fkey"
ATTRIBUTION_SOURCE_OPERATION_UNIQUE = "uq_product_activity_event_attributions_source_operation"
ATTRIBUTION_PROOF_IDENTITY_UNIQUE = "uq_product_activity_event_attributions_proof_identity"
ATTRIBUTION_PROOF_IDENTITY_COLUMNS = ("event_id", "campaign_id", "user_id", "visit_id", "interaction_key",
                                      "event_type", "surface", "source_operation_kind", "source_operation_id")
ATTRIBUTION_VISIT_INDEX = "ix_product_activity_event_attributions_visit"

REGISTRY_PRIMARY_KEY = "product_activity_proof_registry_pkey"
REGISTRY_PARTICIPANT_FK = "product_activity_proof_registry_participant_fkey"
REGISTRY_SESSION_FK = "product_activity_proof_registry_session_fkey"
REGISTRY_INTERACTION_FK = "product_activity_proof_registry_interaction_fkey"
REGISTRY_ATTRIBUTION_FK = "product_activity_proof_registry_attribution_fkey"
REGISTRY_RECORDED_COLUMNS = ("recorded_event_id", "campaign_id", "user_id", "visit_id", "interaction_key",
                             "event_type", "surface", "source_operation_kind", "source_operation_id")
REGISTRY_SOURCE_OPERATION_UNIQUE = "uq_product_activity_proof_registry_source_operation"
REGISTRY_CAMPAIGN_STATUS_INDEX = "ix_product_activity_proof_registry_campaign_status"
REGISTRY_VISIT_INDEX = "ix_product_activity_proof_registry_visit"
REGISTRY_RECORDED_EVENT_INDEX = "ix_product_activity_proof_registry_recorded_event"
REGISTRY_RECORDED_EVENT_WHERE = "recorded_event_id IS NOT NULL"
REGISTRY_PURGE_INDEX = "ix_product_activity_proof_registry_purge_after_at"
REGISTRY_PURGE_WHERE = "purge_after_at IS NOT NULL"

PARTICIPANT_TARGET = [f"{PARTICIPANTS}.campaign_id", f"{PARTICIPANTS}.user_id"]
SESSION_SOURCE = ["visit_id", "campaign_id", "user_id"]
SESSION_TARGET = [f"{SESSIONS}.{c}" for c in SESSION_OWNER_COLUMNS]
INTERACTION_SOURCE = ["campaign_id", "user_id", "interaction_key", "visit_id", "interaction_kind"]
INTERACTION_TARGET = [f"{INTERACTIONS}.{c}" for c in INTERACTION_ORIGIN_COLUMNS]
EVENT_SOURCE = ["event_id", "user_id", "visit_id", "event_type", "surface", "source_operation_kind",
                "source_operation_id"]
EVENT_TARGET = [f"{EVENTS}.{c}" for c in EVENT_ATTRIBUTION_COLUMNS]
ATTRIBUTION_TARGET = [f"{ATTRIBUTIONS}.{c}" for c in ATTRIBUTION_PROOF_IDENTITY_COLUMNS]

# Ordre déterministe des verrous du downgrade (parents puis dépendants) :
# toutes les tables dont il modifie le catalogue (DROP TABLE d'une table
# référençante verrouille aussi la table référencée en ACCESS EXCLUSIVE).
DOWNGRADE_LOCK = (f"LOCK TABLE {PARTICIPANTS}, {EVENTS}, {SESSIONS}, {INTERACTIONS}, {ATTRIBUTIONS}, {REGISTRY} "
                  "IN ACCESS EXCLUSIVE MODE")
# Ordre des contrôles : la table dépendante d'abord (message propre).
BLOCKING_ORDER = (REGISTRY, ATTRIBUTIONS)


def downgrade_message(table: str) -> str:
    return (f"V0-T08 Lot 2B-2 : downgrade de 0024_product_activity_proofs refusé, la table {table} contient "
            "des données. Aucune donnée supprimée.")


def _identity_columns() -> list:
    """Colonnes communes aux deux tables : participation, visite,
    interaction et opération canonique."""
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


def upgrade() -> None:
    """Trois UNIQUE composites, deux tables vides et leurs index ; rien d'autre."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    for table, name, columns in EXISTING_UNIQUES:
        op.create_unique_constraint(name, table, list(columns))
    op.create_table(
        ATTRIBUTIONS,
        sa.Column("event_id", sa.Uuid(), nullable=False),
        *_identity_columns(),
        sa.Column("certified_at", sa.DateTime(timezone=True), nullable=False),
        *(sa.CheckConstraint(expression, name=name) for name, expression in ATTRIBUTION_CHECKS),
        sa.ForeignKeyConstraint(["campaign_id", "user_id"], PARTICIPANT_TARGET, name=ATTRIBUTION_PARTICIPANT_FK),
        sa.ForeignKeyConstraint(SESSION_SOURCE, SESSION_TARGET, name=ATTRIBUTION_SESSION_FK),
        sa.ForeignKeyConstraint(INTERACTION_SOURCE, INTERACTION_TARGET, name=ATTRIBUTION_INTERACTION_FK),
        sa.ForeignKeyConstraint(EVENT_SOURCE, EVENT_TARGET, name=ATTRIBUTION_EVENT_FK),
        sa.PrimaryKeyConstraint("event_id", name=ATTRIBUTION_PRIMARY_KEY),
        sa.UniqueConstraint("user_id", "source_operation_kind", "source_operation_id",
                            name=ATTRIBUTION_SOURCE_OPERATION_UNIQUE),
        sa.UniqueConstraint(*ATTRIBUTION_PROOF_IDENTITY_COLUMNS, name=ATTRIBUTION_PROOF_IDENTITY_UNIQUE),
    )
    op.create_index(ATTRIBUTION_VISIT_INDEX, ATTRIBUTIONS, ["campaign_id", "user_id", "visit_id"])
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
        sa.ForeignKeyConstraint(["campaign_id", "user_id"], PARTICIPANT_TARGET, name=REGISTRY_PARTICIPANT_FK),
        sa.ForeignKeyConstraint(SESSION_SOURCE, SESSION_TARGET, name=REGISTRY_SESSION_FK),
        sa.ForeignKeyConstraint(INTERACTION_SOURCE, INTERACTION_TARGET, name=REGISTRY_INTERACTION_FK),
        sa.ForeignKeyConstraint(list(REGISTRY_RECORDED_COLUMNS), ATTRIBUTION_TARGET, name=REGISTRY_ATTRIBUTION_FK),
        sa.PrimaryKeyConstraint("id", name=REGISTRY_PRIMARY_KEY),
        sa.UniqueConstraint("user_id", "source_operation_kind", "source_operation_id",
                            name=REGISTRY_SOURCE_OPERATION_UNIQUE),
    )
    op.create_index(REGISTRY_CAMPAIGN_STATUS_INDEX, REGISTRY, ["campaign_id", "status"])
    op.create_index(REGISTRY_VISIT_INDEX, REGISTRY, ["campaign_id", "user_id", "visit_id"])
    op.create_index(REGISTRY_RECORDED_EVENT_INDEX, REGISTRY, ["recorded_event_id"],
                    postgresql_where=sa.text(REGISTRY_RECORDED_EVENT_WHERE))
    op.create_index(REGISTRY_PURGE_INDEX, REGISTRY, ["purge_after_at"],
                    postgresql_where=sa.text(REGISTRY_PURGE_WHERE))


def downgrade() -> None:
    """Retour exact à 0023, seulement si les deux tables nouvelles sont vides."""
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
    for table, name, _ in reversed(EXISTING_UNIQUES):
        op.drop_constraint(name, table, type_="unique")
