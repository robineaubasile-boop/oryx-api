"""V0-T08 Lot 2B-1 : cohorte pré-J0, calendrier d'admission et sessions
d'activité produit — socle PostgreSQL DORMANT (EXPAND). Révision R1 (audit
de la PR #249, non fusionnée) : registre des clés d'interaction et clôture
volontaire.

Revision ID: 0023_analytics_cohort_sessions
Revises: 0022_product_analytics_v1
Create Date: 2026-11-22

Migration purement additive : quatre tables nouvelles, aucune table, colonne,
contrainte ni index existant n'est modifié ; les fichiers 0001 à 0022 ne
sont pas modifiés.

1. product_campaign_cohorts : définition de la cohorte d'une campagne
   (une au plus, PK = campaign_id, FK product_campaigns), version de
   politique de cohorte, nombre de places (CHECK = 5 : décision Vague 0
   figée, cohorte fixe sans remplacement) et date limite d'admission
   admission_closes_at (aucun J0 à partir de cet instant), postérieure à la
   création. La compatibilité avec close_by_at (dernier J0 + 12 jours + 30
   minutes de finalisation) est inter-tables : imposée par les services à la
   définition et à l'activation (aucun trigger).

2. product_cohort_seats : les places sélectionnées, présentes que J0 ait
   lieu ou non. Identifiant technique (UUID), seat_number 1 à 5 UNIQUE par
   campagne (donc au plus cinq places en base), référence CANONIQUE à
   l'invitation F2 (FK invitations.id, UNIQUE par campagne) sans aucune
   copie de donnée personnelle (ni e-mail, ni nom, ni token, ni hash).

   Rattachement à un utilisateur (user_id, UNIQUE par campagne : une même
   identité n'occupe jamais deux places) prouvé EN BASE par trois FK
   composites vers auth_operations : l'opération de rattachement est
   l'activation (kind 'invitation_activation') COMMITTED de CETTE invitation
   pour CET utilisateur (colonnes binding_operation_kind / _status figées par
   CHECK, binding_committed_at = committed_at de l'opération). Les colonnes
   du rattachement sont toutes présentes ou toutes absentes.

   Autorisation de participation : trace DISTINCTE (authorization_recorded_at,
   notice_version), présente ensemble, possible seulement sur une place
   rattachée et postérieure au rattachement ; jamais déduite d'une connexion
   ou d'une activation d'invitation. Champ technique, pas une preuve
   juridique de consentement.

   admission_failure_first_at : premier échec TECHNIQUE (non métier) d'une
   tentative d'admission, marque d'incertitude du J0 ultérieur ; seulement
   après l'autorisation.

3. product_activity_sessions : sessions d'activité produit (visites),
   DISTINCTES des sessions d'authentification F2 (aucune FK vers
   oryx_sessions). Rattachées à une participation (FK composite vers
   product_campaign_participants). Environnement de navigation identifié
   par une empreinte SHA-256 (environment_digest, 64 hexadécimaux), jamais
   par l'identifiant brut. Inactivité de trente minutes imposée en base
   (idle_expires_at = last_activity_at + 30 minutes). Fin de visite
   (finalized_at, end_reason, présents ensemble) :
   - 'inactivity' : au plus tôt à l'expiration (finalized_at >=
     idle_expires_at) ;
   - 'logout' (déconnexion volontaire) : AVANT l'expiration et jamais avant
     la dernière activité (last_activity_at <= finalized_at <
     idle_expires_at) ; aucune activité n'est déplacée ni inventée.
   Une seule session OUVERTE par (campagne, utilisateur, environnement)
   (index unique partiel). La borne last_activity_at < window_end_at
   (aucune session ne prolonge les douze jours) est inter-tables : imposée
   par le service.

4. product_activity_session_interactions : registre des clés d'interaction
   ACCEPTÉES (idempotence des rejeux, même anciens ou concurrents). PK
   (campaign_id, user_id, interaction_key) : une clé n'est acceptée qu'une
   fois par participation ; son rejeu renvoie la visite d'origine, sans
   ouverture ni prolongation. Contenu minimal : la clé aléatoire, son type
   (interaction_kind 'activity' ou 'close'), l'empreinte d'environnement,
   la session concernée et l'instant serveur d'enregistrement ; aucune
   donnée de contenu. FK composite vers la session (même campagne, même
   utilisateur, même environnement) ; session absente seulement pour une
   clôture sans visite ouverte (rejeu sûr). Ce n'est PAS le registre
   provisoire des preuves métier du lot 2B-2.

Aucun backfill, aucune ligne insérée, aucun trigger, aucune fonction
persistée, aucun ENUM, aucune extension, aucun server_default. Aucune route,
aucune collecte : tables vides et dormantes.

Garde-fous de verrouillage (SET LOCAL, transaction Alembic) : 5 s d'attente
de verrou, 15 s par instruction, 15 s d'inactivité. Les FK vers invitations,
auth_operations, users, product_campaigns et product_campaign_participants
prennent des verrous brefs sur ces tables à la création (tables existantes
non réécrites).

Downgrade PROTÉGÉ, dans une seule transaction : LOCK des quatre tables IN
ACCESS EXCLUSIVE MODE dans un ordre déterministe (cohortes, places,
sessions, interactions), refus (exception, rien n'est supprimé) si l'une
d'elles contient une ligne — contrôlées dans l'ordre interactions, sessions,
places, cohortes (message propre à chaque table) — sinon suppression des
tables enfants puis parentes
(retour exact à 0022, sans CASCADE). En mode offline, le contrôle est un
bloc DO évalué à l'exécution du script.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0023_analytics_cohort_sessions"
down_revision: Union[str, Sequence[str], None] = "0022_product_analytics_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCK_GUARDS = (
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL statement_timeout = '15s'",
    "SET LOCAL idle_in_transaction_session_timeout = '15s'",
)
COHORTS = "product_campaign_cohorts"
SEATS = "product_cohort_seats"
SESSIONS = "product_activity_sessions"
INTERACTIONS = "product_activity_session_interactions"
TABLES = (COHORTS, SEATS, SESSIONS, INTERACTIONS)

WAVE_0_SEAT_COUNT = 5
BINDING_OPERATION_KIND = "invitation_activation"
BINDING_OPERATION_STATUS = "committed"
SESSION_INACTIVITY = "30 minutes"
SESSION_END_REASONS = ("inactivity", "logout")
INTERACTION_KINDS = ("activity", "close")

# Expressions exactes, recopiées par core/models.py (concordance testée).
COHORT_CHECKS = (
    ("ck_product_campaign_cohorts_seat_count", "seat_count = 5"),
    ("ck_product_campaign_cohorts_admission", "admission_closes_at > created_at"),
)
SEAT_CHECKS = (
    ("ck_product_cohort_seats_seat_number", "seat_number BETWEEN 1 AND 5"),
    ("ck_product_cohort_seats_binding_kind",
     "binding_operation_kind IS NULL OR binding_operation_kind = 'invitation_activation'"),
    ("ck_product_cohort_seats_binding_status",
     "binding_operation_status IS NULL OR binding_operation_status = 'committed'"),
    ("ck_product_cohort_seats_binding",
     "(user_id IS NULL) = (binding_operation_id IS NULL) "
     "AND (user_id IS NULL) = (binding_operation_kind IS NULL) "
     "AND (user_id IS NULL) = (binding_operation_status IS NULL) "
     "AND (user_id IS NULL) = (binding_committed_at IS NULL) "
     "AND (user_id IS NULL) = (bound_at IS NULL)"),
    ("ck_product_cohort_seats_bound_at",
     "bound_at IS NULL OR (bound_at >= binding_committed_at AND bound_at >= created_at)"),
    ("ck_product_cohort_seats_authorization",
     "(authorization_recorded_at IS NULL) = (notice_version IS NULL) "
     "AND (authorization_recorded_at IS NULL OR (bound_at IS NOT NULL AND authorization_recorded_at >= bound_at))"),
    ("ck_product_cohort_seats_admission_failure",
     "admission_failure_first_at IS NULL OR (authorization_recorded_at IS NOT NULL "
     "AND admission_failure_first_at >= authorization_recorded_at)"),
)
SESSION_CHECKS = (
    ("ck_product_activity_sessions_environment_digest", "environment_digest ~ '^[0-9a-f]{64}$'"),
    ("ck_product_activity_sessions_activity", "last_activity_at >= opened_at"),
    ("ck_product_activity_sessions_idle_expiry", "idle_expires_at = last_activity_at + interval '30 minutes'"),
    ("ck_product_activity_sessions_end", "(finalized_at IS NULL) = (end_reason IS NULL)"),
    ("ck_product_activity_sessions_end_reason", "end_reason IS NULL OR end_reason IN ('inactivity', 'logout')"),
    ("ck_product_activity_sessions_finalized",
     "finalized_at IS NULL "
     "OR (end_reason = 'inactivity' AND finalized_at >= idle_expires_at) "
     "OR (end_reason = 'logout' AND finalized_at >= last_activity_at AND finalized_at < idle_expires_at)"),
)
INTERACTION_CHECKS = (
    ("ck_product_activity_session_interactions_kind", "interaction_kind IN ('activity', 'close')"),
    ("ck_product_activity_session_interactions_environment_digest", "environment_digest ~ '^[0-9a-f]{64}$'"),
    ("ck_product_activity_session_interactions_session",
     "session_id IS NOT NULL OR interaction_kind = 'close'"),
)

COHORT_CAMPAIGN_FK = "product_campaign_cohorts_campaign_id_fkey"
SEAT_COHORT_FK = "product_cohort_seats_campaign_id_fkey"
SEAT_INVITATION_FK = "product_cohort_seats_invitation_id_fkey"
SEAT_USER_FK = "product_cohort_seats_user_id_fkey"
SEAT_BINDING_INVITATION_FK = "product_cohort_seats_binding_invitation_fkey"
SEAT_BINDING_USER_FK = "product_cohort_seats_binding_user_fkey"
SEAT_BINDING_COMMIT_FK = "product_cohort_seats_binding_commit_fkey"
SEAT_NUMBER_UNIQUE = "uq_product_cohort_seats_campaign_seat_number"
SEAT_INVITATION_UNIQUE = "uq_product_cohort_seats_campaign_invitation"
SEAT_USER_UNIQUE = "uq_product_cohort_seats_campaign_user"
SESSION_PARTICIPANT_FK = "product_activity_sessions_participant_fkey"
SESSION_IDENTITY_UNIQUE = "uq_product_activity_sessions_identity"
INTERACTION_PRIMARY_KEY = "product_activity_session_interactions_pkey"
INTERACTION_PARTICIPANT_FK = "product_activity_session_interactions_participant_fkey"
INTERACTION_SESSION_FK = "product_activity_session_interactions_session_fkey"
SESSION_OPEN_UNIQUE = "uq_product_activity_sessions_open_environment"
SESSION_OPEN_WHERE = "finalized_at IS NULL"
SESSION_IDLE_INDEX = "ix_product_activity_sessions_open_idle_expiry"

# Ordre déterministe des verrous : la table parente, puis ses dépendantes.
DOWNGRADE_LOCK = f"LOCK TABLE {COHORTS}, {SEATS}, {SESSIONS}, {INTERACTIONS} IN ACCESS EXCLUSIVE MODE"
# Ordre des contrôles : chaque table examinée à part (message propre).
BLOCKING_ORDER = (INTERACTIONS, SESSIONS, SEATS, COHORTS)


def downgrade_message(table: str) -> str:
    return (f"V0-T08 Lot 2B-1 : downgrade de 0023_analytics_cohort_sessions refusé, la table {table} contient "
            "des données. Aucune donnée supprimée.")


def upgrade() -> None:
    """Quatre tables analytiques vides et leurs index ; rien d'autre."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.create_table(
        COHORTS,
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("cohort_policy_version", sa.String(length=64), nullable=False),
        sa.Column("seat_count", sa.SmallInteger(), nullable=False),
        sa.Column("admission_closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *(sa.CheckConstraint(expression, name=name) for name, expression in COHORT_CHECKS),
        sa.ForeignKeyConstraint(["campaign_id"], ["product_campaigns.id"], name=COHORT_CAMPAIGN_FK),
        sa.PrimaryKeyConstraint("campaign_id", name="product_campaign_cohorts_pkey"),
    )
    op.create_table(
        SEATS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("seat_number", sa.SmallInteger(), nullable=False),
        sa.Column("invitation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("binding_operation_id", sa.Uuid(), nullable=True),
        sa.Column("binding_operation_kind", sa.String(length=32), nullable=True),
        sa.Column("binding_operation_status", sa.String(length=16), nullable=True),
        sa.Column("binding_committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorization_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notice_version", sa.String(length=64), nullable=True),
        sa.Column("admission_failure_first_at", sa.DateTime(timezone=True), nullable=True),
        *(sa.CheckConstraint(expression, name=name) for name, expression in SEAT_CHECKS),
        sa.ForeignKeyConstraint(["campaign_id"], [f"{COHORTS}.campaign_id"], name=SEAT_COHORT_FK),
        sa.ForeignKeyConstraint(["invitation_id"], ["invitations.id"], name=SEAT_INVITATION_FK),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=SEAT_USER_FK),
        sa.ForeignKeyConstraint(["binding_operation_id", "invitation_id"],
                                ["auth_operations.id", "auth_operations.invitation_id"],
                                name=SEAT_BINDING_INVITATION_FK),
        sa.ForeignKeyConstraint(["binding_operation_id", "user_id", "binding_operation_kind"],
                                ["auth_operations.id", "auth_operations.user_id", "auth_operations.kind"],
                                name=SEAT_BINDING_USER_FK),
        sa.ForeignKeyConstraint(
            ["binding_operation_id", "binding_operation_kind", "binding_operation_status", "binding_committed_at"],
            ["auth_operations.id", "auth_operations.kind", "auth_operations.status", "auth_operations.committed_at"],
            name=SEAT_BINDING_COMMIT_FK),
        sa.PrimaryKeyConstraint("id", name="product_cohort_seats_pkey"),
        sa.UniqueConstraint("campaign_id", "seat_number", name=SEAT_NUMBER_UNIQUE),
        sa.UniqueConstraint("campaign_id", "invitation_id", name=SEAT_INVITATION_UNIQUE),
        sa.UniqueConstraint("campaign_id", "user_id", name=SEAT_USER_UNIQUE),
    )
    op.create_table(
        SESSIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("environment_digest", sa.String(length=64), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=16), nullable=True),
        *(sa.CheckConstraint(expression, name=name) for name, expression in SESSION_CHECKS),
        sa.ForeignKeyConstraint(["campaign_id", "user_id"],
                                ["product_campaign_participants.campaign_id", "product_campaign_participants.user_id"],
                                name=SESSION_PARTICIPANT_FK),
        sa.PrimaryKeyConstraint("id", name="product_activity_sessions_pkey"),
        sa.UniqueConstraint("id", "campaign_id", "user_id", "environment_digest", name=SESSION_IDENTITY_UNIQUE),
    )
    op.create_index(SESSION_OPEN_UNIQUE, SESSIONS, ["campaign_id", "user_id", "environment_digest"], unique=True,
                    postgresql_where=sa.text(SESSION_OPEN_WHERE))
    op.create_index(SESSION_IDLE_INDEX, SESSIONS, ["campaign_id", "idle_expires_at"],
                    postgresql_where=sa.text(SESSION_OPEN_WHERE))
    op.create_table(
        INTERACTIONS,
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("interaction_key", sa.Uuid(), nullable=False),
        sa.Column("interaction_kind", sa.String(length=16), nullable=False),
        sa.Column("environment_digest", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        *(sa.CheckConstraint(expression, name=name) for name, expression in INTERACTION_CHECKS),
        sa.ForeignKeyConstraint(["campaign_id", "user_id"],
                                ["product_campaign_participants.campaign_id", "product_campaign_participants.user_id"],
                                name=INTERACTION_PARTICIPANT_FK),
        sa.ForeignKeyConstraint(["session_id", "campaign_id", "user_id", "environment_digest"],
                                [f"{SESSIONS}.id", f"{SESSIONS}.campaign_id", f"{SESSIONS}.user_id",
                                 f"{SESSIONS}.environment_digest"],
                                name=INTERACTION_SESSION_FK),
        sa.PrimaryKeyConstraint("campaign_id", "user_id", "interaction_key", name=INTERACTION_PRIMARY_KEY),
    )


def downgrade() -> None:
    """Retour exact à 0022, seulement si les quatre tables sont vides."""
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
    op.drop_table(INTERACTIONS)
    op.drop_table(SESSIONS)
    op.drop_table(SEATS)
    op.drop_table(COHORTS)
