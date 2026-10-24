"""R1 PR 4 : admission HTTP F2 dans le journal anti-abus (EXPAND, C.4.5 §2.1).

Revision ID: 0019_f2_http_admission
Revises: 0018_f2_abuse_journal
Create Date: 2026-10-24

Les fichiers 0017 et 0018 ne sont pas modifiés. Cette révision étend
auth_abuse_events (dormante) et rien d'autre :

1. Nouveau type d'événement auth_http_probe_attempt : une admission HTTP
   PR 4 sur le sujet IP de l'appelant (une ligne par admission, aucune
   ligne pour un refus). Colonne ajoutée http_action VARCHAR NULL
   (aucune réécriture), qui désigne l'une des sept routes PR 4.

2. CHECK ck_auth_abuse_events_http_probe :
   - http_action est renseignée SI ET SEULEMENT SI l'événement est un
     probe : tout événement historique (types de 0017 et 0018) garde
     http_action IS NULL ;
   - http_action appartient à la liste fermée des sept actions ;
   - un probe n'a ni auth_operation_id, ni otp_request_id, ni
     blocked_until, et porte policy_version = 'f2-pr4-http-v1'.

3. CHECK de type et de liaison OTP remplacés par des SUR-ENSEMBLES
   stricts de ceux de 0018 : seul auth_http_probe_attempt est ajouté à la
   liste des types, et à la liste des types qui INTERDISENT
   otp_request_id (un probe lié à une demande OTP est refusé par les deux
   CHECK). Pour chaque type historique, l'expression est inchangée : toute
   ligne valide sous 0018 reste valide, aucune ligne invalide sous 0018 ne
   le devient. ck_auth_abuse_events_resume_link, ck_auth_abuse_events_block,
   ck_auth_abuse_events_refusal_bucket, les FK, les index (dont l'index
   unique partiel des marqueurs de refus) et auth_abuse_key_state sont
   conservés tels quels.

Aucun trigger, aucune fonction persistée, aucun ENUM, aucun
server_default, aucune nouvelle table, aucune donnée écrite hors
alembic_version.

Garde-fous de verrouillage (SET LOCAL, transaction Alembic) : 5 s
d'attente de verrou, 15 s par instruction, 15 s d'inactivité.

Downgrade CONTRÔLÉ : verrou ACCESS EXCLUSIVE sur auth_abuse_events AVANT
le contrôle (aucune insertion concurrente entre contrôle et suppression),
puis refus (exception, aucune suppression) s'il existe un probe ou une
http_action renseignée ; sinon retour exact à 0018 (CHECK de 0018
restaurés à l'identique, colonne retirée).
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0019_f2_http_admission"
down_revision: Union[str, Sequence[str], None] = "0018_f2_abuse_journal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCK_GUARDS = (
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL statement_timeout = '15s'",
    "SET LOCAL idle_in_transaction_session_timeout = '15s'",
)
EVENTS = "auth_abuse_events"
KIND_CHECK = "ck_auth_abuse_events_event_kind"
LINK_CHECK = "ck_auth_abuse_events_otp_link"
HTTP_CHECK = "ck_auth_abuse_events_http_probe"
PROBE = "auth_http_probe_attempt"
HTTP_POLICY_VERSION = "f2-pr4-http-v1"
HTTP_ACTIONS = (
    "invitation_begin", "invitation_complete", "invitation_resume", "email_link_start", "email_link_resend",
    "email_link_verify", "email_link_result",
)

# Expressions de 0018, restaurées à l'identique au downgrade.
OLD_KIND = (
    "event_kind IN ('otp_request', 'otp_failure', 'invitation_attempt', 'resume_attempt', "
    "'otp_verification_attempt', 'otp_email_block', 'otp_request_refused', 'otp_verification_refused')"
)
OLD_LINK = (
    "(event_kind NOT IN ('otp_request', 'otp_failure', 'otp_email_block') OR otp_request_id IS NOT NULL) "
    "AND (event_kind NOT IN ('invitation_attempt', 'resume_attempt', 'otp_request_refused', "
    "'otp_verification_refused') OR otp_request_id IS NULL)"
)
NEW_KIND = (
    "event_kind IN ('otp_request', 'otp_failure', 'invitation_attempt', 'resume_attempt', "
    "'otp_verification_attempt', 'otp_email_block', 'otp_request_refused', 'otp_verification_refused', "
    "'auth_http_probe_attempt')"
)
NEW_LINK = (
    "(event_kind NOT IN ('otp_request', 'otp_failure', 'otp_email_block') OR otp_request_id IS NOT NULL) "
    "AND (event_kind NOT IN ('invitation_attempt', 'resume_attempt', 'otp_request_refused', "
    "'otp_verification_refused', 'auth_http_probe_attempt') OR otp_request_id IS NULL)"
)
HTTP_PROBE = (
    "(event_kind = 'auth_http_probe_attempt') = (http_action IS NOT NULL) "
    "AND (http_action IS NULL OR http_action IN ('invitation_begin', 'invitation_complete', 'invitation_resume', "
    "'email_link_start', 'email_link_resend', 'email_link_verify', 'email_link_result')) "
    "AND (event_kind <> 'auth_http_probe_attempt' OR (auth_operation_id IS NULL AND otp_request_id IS NULL "
    "AND blocked_until IS NULL AND policy_version = 'f2-pr4-http-v1'))"
)
DOWNGRADE_MESSAGE = (
    "R1 PR 4 : downgrade de 0019_f2_http_admission refusé, des données F2 l'exigent "
    "(probes d'admission HTTP ou http_action présents). Aucune donnée supprimée."
)


def upgrade() -> None:
    """Ajoute le type auth_http_probe_attempt et la colonne http_action."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.drop_constraint(KIND_CHECK, EVENTS, type_="check")
    op.drop_constraint(LINK_CHECK, EVENTS, type_="check")
    op.add_column(EVENTS, sa.Column("http_action", sa.String(), nullable=True))
    op.create_check_constraint(KIND_CHECK, EVENTS, NEW_KIND)
    op.create_check_constraint(LINK_CHECK, EVENTS, NEW_LINK)
    op.create_check_constraint(HTTP_CHECK, EVENTS, HTTP_PROBE)


def downgrade() -> None:
    """Retour exact à 0018, seulement si aucune donnée de 0019 n'existe."""
    for guard in LOCK_GUARDS:
        op.execute(guard)
    op.execute(f"LOCK TABLE {EVENTS} IN ACCESS EXCLUSIVE MODE")
    blocking = f"SELECT 1 FROM {EVENTS} WHERE event_kind = '{PROBE}' OR http_action IS NOT NULL"
    if context.is_offline_mode():
        message = DOWNGRADE_MESSAGE.replace("'", "''")
        op.execute(f"DO $$ BEGIN IF EXISTS ({blocking}) THEN RAISE EXCEPTION '{message}'; END IF; END $$")
    elif op.get_bind().execute(sa.text(f"SELECT EXISTS ({blocking})")).scalar_one():
        raise RuntimeError(DOWNGRADE_MESSAGE)
    op.drop_constraint(HTTP_CHECK, EVENTS, type_="check")
    op.drop_constraint(LINK_CHECK, EVENTS, type_="check")
    op.drop_constraint(KIND_CHECK, EVENTS, type_="check")
    op.drop_column(EVENTS, "http_action")
    op.create_check_constraint(KIND_CHECK, EVENTS, OLD_KIND)
    op.create_check_constraint(LINK_CHECK, EVENTS, OLD_LINK)
