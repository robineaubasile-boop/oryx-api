"""R1 PR 2 : journal anti-abus F2 étendu (EXPAND, variante A de C.2.1-A).

Revision ID: 0018_f2_abuse_journal
Revises: 0017_oryx_auth_foundations
Create Date: 2026-10-22

Le fichier 0017 n'est pas modifié. Cette révision étend auth_abuse_events
(tables vides et dormantes depuis 0017) et crée une table singleton :

1. auth_abuse_events, quatre nouveaux types d'événements :
   - otp_verification_attempt : vérification OTP ADMISE par IP, avec ou
     sans demande identifiable (otp_request_id facultatif) ; budget de 30
     par 5 minutes et par IP ;
   - otp_email_block : blocage e-mail durable (ajout seul), lié à la
     demande du cinquième échec ; blocked_until = occurred_at + 15 minutes
     imposé par CHECK ; ni prolongé ni effacé (aucune mise à jour) ;
   - otp_request_refused / otp_verification_refused : marqueurs de refus
     de quota. occurred_at = début de la tranche de 5 minutes (CHECK
     d'alignement date_bin) et index UNIQUE PARTIEL
     (subject_id, event_kind, occurred_at) : AU PLUS UN marqueur par sujet,
     type et tranche, quels que soient la concurrence et les frontières
     des fenêtres (garantie SQL, pas une convention applicative).
   Le CHECK de liaison est un SUR-ENSEMBLE de celui de 0017 : otp_request
   et otp_failure exigent toujours otp_request_id ; invitation_attempt et
   resume_attempt l'interdisent toujours ; otp_email_block l'exige ; les
   marqueurs l'interdisent ; otp_verification_attempt l'accepte sans
   l'exiger. Toute ligne valide sous 0017 reste valide.
   Colonne ajoutée : blocked_until TIMESTAMPTZ NULL (aucune réécriture).

2. auth_abuse_key_state : ligne unique (id = 1) qui fixe la version de
   K_abuse d'ENREGISTREMENT (current_version) et la version précédente
   encore COMPTÉE pendant une rotation (previous_version, depuis
   previous_demoted_at). Les quotas additionnent les sujets des deux
   versions ; une instance dont le trousseau ne contient pas ces deux
   versions refuse toute opération (refus fermé). Aucune clé n'y figure,
   seulement des identifiants de version. Aucune ligne n'est écrite par la
   migration (initialisation explicite, dormante).

Aucun trigger, aucune fonction persistée, aucun ENUM, aucun
server_default, aucune donnée écrite hors alembic_version.

Garde-fous de verrouillage (SET LOCAL, transaction Alembic) : 5 s
d'attente de verrou, 15 s par instruction, 15 s d'inactivité. Le verrou
ACCESS EXCLUSIVE porte sur auth_abuse_events, vide et lue par aucun code
actif ; la validation des CHECK parcourt une table vide.

Downgrade CONTRÔLÉ : refuse (exception, aucune suppression) si
auth_abuse_events contient une ligne d'un nouveau type ou si
auth_abuse_key_state n'est pas vide ; sinon retour exact à 0017 (CHECK
d'origine restaurés à l'identique).
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0018_f2_abuse_journal"
down_revision: Union[str, Sequence[str], None] = "0017_oryx_auth_foundations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCK_GUARDS = (
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL statement_timeout = '15s'",
    "SET LOCAL idle_in_transaction_session_timeout = '15s'",
)
EVENTS = "auth_abuse_events"
KEY_STATE = "auth_abuse_key_state"
KIND_CHECK = "ck_auth_abuse_events_event_kind"
LINK_CHECK = "ck_auth_abuse_events_otp_link"
BLOCK_CHECK = "ck_auth_abuse_events_block"
BUCKET_CHECK = "ck_auth_abuse_events_refusal_bucket"
REFUSAL_INDEX = "uq_auth_abuse_events_refusal_bucket"

OLD_KIND = "event_kind IN ('otp_request', 'otp_failure', 'invitation_attempt', 'resume_attempt')"
OLD_LINK = "(event_kind IN ('otp_request', 'otp_failure')) = (otp_request_id IS NOT NULL)"
NEW_KINDS = ("otp_verification_attempt", "otp_email_block", "otp_request_refused", "otp_verification_refused")
NEW_KIND = (
    "event_kind IN ('otp_request', 'otp_failure', 'invitation_attempt', 'resume_attempt', "
    "'otp_verification_attempt', 'otp_email_block', 'otp_request_refused', 'otp_verification_refused')"
)
NEW_LINK = (
    "(event_kind NOT IN ('otp_request', 'otp_failure', 'otp_email_block') OR otp_request_id IS NOT NULL) "
    "AND (event_kind NOT IN ('invitation_attempt', 'resume_attempt', 'otp_request_refused', "
    "'otp_verification_refused') OR otp_request_id IS NULL)"
)
BLOCK = (
    "(event_kind = 'otp_email_block') = (blocked_until IS NOT NULL) "
    "AND (blocked_until IS NULL OR blocked_until - occurred_at = interval '15 minutes')"
)
REFUSAL_KINDS = "event_kind IN ('otp_request_refused', 'otp_verification_refused')"
BUCKET = (
    "event_kind NOT IN ('otp_request_refused', 'otp_verification_refused') "
    "OR occurred_at = date_bin(interval '5 minutes', occurred_at, timestamptz '2000-01-01 00:00:00+00')"
)
VERSION_FORMAT = "'^[A-Za-z0-9._-]{1,64}$'"
DOWNGRADE_MESSAGE = (
    "R1 PR 2 : downgrade de 0018_f2_abuse_journal refusé, des données F2 l'exigent "
    "(événements anti-abus de 0018 ou état de clé anti-abus présents). Aucune donnée supprimée."
)


def upgrade() -> None:
    """Étend auth_abuse_events et crée auth_abuse_key_state (rien d'autre)."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.drop_constraint(KIND_CHECK, EVENTS, type_="check")
    op.drop_constraint(LINK_CHECK, EVENTS, type_="check")
    op.add_column(EVENTS, sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(KIND_CHECK, EVENTS, NEW_KIND)
    op.create_check_constraint(LINK_CHECK, EVENTS, NEW_LINK)
    op.create_check_constraint(BLOCK_CHECK, EVENTS, BLOCK)
    op.create_check_constraint(BUCKET_CHECK, EVENTS, BUCKET)
    op.create_index(
        REFUSAL_INDEX, EVENTS, ["subject_id", "event_kind", "occurred_at"], unique=True,
        postgresql_where=sa.text(REFUSAL_KINDS),
    )
    op.create_table(
        KEY_STATE,
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("current_version", sa.String(), nullable=False),
        sa.Column("previous_version", sa.String(), nullable=True),
        sa.Column("previous_demoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_auth_abuse_key_state_singleton"),
        sa.CheckConstraint(f"current_version ~ {VERSION_FORMAT}", name="ck_auth_abuse_key_state_current_version"),
        sa.CheckConstraint(
            f"previous_version IS NULL OR previous_version ~ {VERSION_FORMAT}",
            name="ck_auth_abuse_key_state_previous_version",
        ),
        sa.CheckConstraint(
            "(previous_version IS NULL) = (previous_demoted_at IS NULL) "
            "AND (previous_version IS NULL OR previous_version <> current_version)",
            name="ck_auth_abuse_key_state_previous",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Retour exact à 0017, seulement si aucune donnée de 0018 n'existe."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.execute(f"LOCK TABLE {EVENTS} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"LOCK TABLE {KEY_STATE} IN ACCESS EXCLUSIVE MODE")
    blocking = (
        f"SELECT 1 FROM {EVENTS} WHERE event_kind NOT IN "
        "('otp_request', 'otp_failure', 'invitation_attempt', 'resume_attempt') OR blocked_until IS NOT NULL "
        f"UNION ALL SELECT 1 FROM {KEY_STATE}"
    )
    if context.is_offline_mode():
        message = DOWNGRADE_MESSAGE.replace("'", "''")
        op.execute(f"DO $$ BEGIN IF EXISTS ({blocking}) THEN RAISE EXCEPTION '{message}'; END IF; END $$")
    elif op.get_bind().execute(sa.text(f"SELECT EXISTS ({blocking})")).scalar_one():
        raise RuntimeError(DOWNGRADE_MESSAGE)
    op.drop_table(KEY_STATE)
    op.drop_index(REFUSAL_INDEX, table_name=EVENTS)
    op.drop_constraint(BUCKET_CHECK, EVENTS, type_="check")
    op.drop_constraint(BLOCK_CHECK, EVENTS, type_="check")
    op.drop_constraint(LINK_CHECK, EVENTS, type_="check")
    op.drop_constraint(KIND_CHECK, EVENTS, type_="check")
    op.drop_column(EVENTS, "blocked_until")
    op.create_check_constraint(KIND_CHECK, EVENTS, OLD_KIND)
    op.create_check_constraint(LINK_CHECK, EVENTS, OLD_LINK)
