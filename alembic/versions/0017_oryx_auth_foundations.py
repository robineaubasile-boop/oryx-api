"""R1 PR 1 : fondations d'authentification et sessions Oryx F2 (EXPAND).

Revision ID: 0017_oryx_auth_foundations
Revises: 0016_portfolio_idempotency
Create Date: 2026-10-16

Crée onze tables vides et INACTIVES (aucune route, aucun service, aucun
frontend ne les lit ni ne les écrit ; aucun appel Supabase / Resend) :

  auth_identities                 identités externes (provider, issuer, subject)
  invitations                     invitations à usage unique, e-mail facultatif
  auth_operations                 opérations F2 : supabase_email_sign_in,
                                  invitation_activation, email_linking
  auth_preauth_candidates         candidats pré-authentification (génération g)
  auth_otp_requests               demandes OTP (empreinte HMAC du code attendu)
  auth_otp_verification_attempts  tentatives (HMAC local du code soumis)
  auth_abuse_subjects             sujets anti-abus pseudonymisés (verrou)
  auth_abuse_events               journal anti-abus en ajout seul
  oryx_sessions                   session canonique (15 jours absolus)
  oryx_session_generations        cookie actif immuable + preuve de livraison
  auth_operation_results          résultat canonique (reprise de 5 minutes)

Aucun secret en clair : *_hash / *_hmac = HMAC-SHA256 hexadécimal à clé
serveur versionnée (CHECK '^[0-9a-f]{64}$'). Aucun code OTP, secret de
session ni jeton fournisseur n'est stocké.

Les FK composites (MATCH SIMPLE) portent sur des colonnes immuables ou
écrites une seule fois ; seul le statut 'committed' d'une auth_operation,
terminal, est figé par auth_operation_results. Aucune FK ne porte sur le
statut d'un candidat : sa clôture ou sa révocation après liaison restent
possibles.

Unique cycle de FK, non obligatoire : auth_operations.initiating_session_id
(NULL hors email_linking) -> oryx_sessions, alors que oryx_sessions
référence son opération créatrice. La session référencée existe toujours
avant l'opération email_linking ; la FK est ajoutée par ALTER TABLE après la
création d'oryx_sessions et supprimée la première au downgrade.

Index sur chaque FK enfant non couverte par la première colonne d'une PK ou
d'un UNIQUE. Aucun trigger, aucune fonction, aucun ENUM, aucun
server_default, aucune donnée écrite hors alembic_version. Identifiant de
révision de 26 caractères.

Downgrade : retire la FK de session initiatrice puis supprime les onze
tables dans l'ordre inverse (retour exact à 0016).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0017_oryx_auth_foundations"
down_revision: Union[str, Sequence[str], None] = "0016_portfolio_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = (
    "auth_identities",
    "invitations",
    "auth_operations",
    "auth_preauth_candidates",
    "auth_otp_requests",
    "auth_otp_verification_attempts",
    "auth_abuse_subjects",
    "auth_abuse_events",
    "oryx_sessions",
    "oryx_session_generations",
    "auth_operation_results",
)
LINKING_SESSION_FK = "auth_operations_linking_session_fkey"
OTP_LIVE_STATUSES = "('issuing', 'issue_uncertain', 'issued', 'verifying', 'verify_uncertain', 'verified')"


def _tz(name, nullable):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _hex(table, column, nullable=False):
    expression = f"{column} ~ '^[0-9a-f]{{64}}$'"
    if nullable:
        expression = f"{column} IS NULL OR {expression}"
    return sa.CheckConstraint(expression, name=f"ck_{table}_{column}_format")


def _email(table, column):
    """column se termine par _normalized : CHECK nommé ck_<table>_<column>."""
    return sa.CheckConstraint(
        f"{column} IS NULL OR ({column} = lower(btrim({column})) AND {column} ~ '^[^@[:space:]]+@[^@[:space:]]+$')",
        name=f"ck_{table}_{column}",
    )


def upgrade() -> None:
    """Crée les onze tables F2, leurs index et la FK de session initiatrice."""
    op.create_table(
        "auth_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("verified_email_normalized", sa.String(), nullable=True),
        _tz("email_verified_at", True),
        _tz("created_at", False),
        _tz("last_authenticated_at", True),
        _tz("pii_redacted_at", True),
        sa.CheckConstraint("provider IN ('supabase')", name="ck_auth_identities_provider"),
        sa.CheckConstraint("issuer ~ '^https://'", name="ck_auth_identities_issuer"),
        sa.CheckConstraint("subject <> ''", name="ck_auth_identities_subject"),
        _email("auth_identities", "verified_email_normalized"),
        sa.CheckConstraint(
            "verified_email_normalized IS NULL OR email_verified_at IS NOT NULL",
            name="ck_auth_identities_email_verified",
        ),
        sa.CheckConstraint(
            "pii_redacted_at IS NULL OR verified_email_normalized IS NULL", name="ck_auth_identities_pii",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "issuer", "subject", name="uq_auth_identities_provider_issuer_subject"),
        sa.UniqueConstraint("id", "user_id", name="uq_auth_identities_id_user"),
        sa.UniqueConstraint(
            "id", "provider", "issuer", "subject", name="uq_auth_identities_id_provider_issuer_subject",
        ),
    )
    op.create_index("ix_auth_identities_user_id", "auth_identities", ["user_id"])

    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("hash_key_version", sa.String(), nullable=False),
        sa.Column("recipient_email_normalized", sa.String(), nullable=True),
        sa.Column("cohort", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("invited_by_user_id", sa.String(), nullable=True),
        _tz("created_at", False),
        _tz("expires_at", False),
        _tz("accepted_at", True),
        _tz("revoked_at", True),
        _tz("pii_redacted_at", True),
        _hex("invitations", "token_hash"),
        _email("invitations", "recipient_email_normalized"),
        sa.CheckConstraint("status IN ('pending', 'accepted', 'revoked')", name="ck_invitations_status"),
        sa.CheckConstraint("expires_at > created_at", name="ck_invitations_expiry"),
        sa.CheckConstraint(
            "(status = 'pending' AND accepted_at IS NULL AND revoked_at IS NULL) "
            "OR (status = 'accepted' AND accepted_at IS NOT NULL AND revoked_at IS NULL "
            "AND accepted_at <= expires_at) "
            "OR (status = 'revoked' AND revoked_at IS NOT NULL AND accepted_at IS NULL)",
            name="ck_invitations_state",
        ),
        sa.CheckConstraint(
            "pii_redacted_at IS NULL OR recipient_email_normalized IS NULL", name="ck_invitations_pii",
        ),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
    )
    op.create_index("ix_invitations_invited_by_user_id", "invitations", ["invited_by_user_id"])
    op.create_index(
        "uq_invitations_one_pending_email", "invitations", ["recipient_email_normalized"], unique=True,
        postgresql_where=sa.text("status = 'pending' AND recipient_email_normalized IS NOT NULL"),
    )

    op.create_table(
        "auth_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("email_normalized", sa.String(), nullable=True),
        sa.Column("invitation_id", sa.Uuid(), nullable=True),
        sa.Column("auth_identity_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("target_user_id", sa.String(), nullable=True),
        sa.Column("initiating_session_id", sa.Uuid(), nullable=True),
        sa.Column("initiating_session_method", sa.String(), nullable=True),
        sa.Column("proven_provider", sa.String(), nullable=True),
        sa.Column("proven_issuer", sa.String(), nullable=True),
        sa.Column("proven_subject", sa.String(), nullable=True),
        sa.Column("identity_resolution", sa.String(), nullable=True),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("operation_schema_version", sa.String(), nullable=False),
        _tz("created_at", False),
        _tz("updated_at", False),
        _tz("expires_at", False),
        _tz("committed_at", True),
        _tz("pii_redacted_at", True),
        sa.CheckConstraint(
            "kind IN ('supabase_email_sign_in', 'invitation_activation', 'email_linking')",
            name="ck_auth_operations_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'committed', 'failed', 'expired')", name="ck_auth_operations_status",
        ),
        _email("auth_operations", "email_normalized"),
        sa.CheckConstraint(
            "initiating_session_method = 'invitation_activation'",
            name="ck_auth_operations_initiating_session_method",
        ),
        sa.CheckConstraint("proven_provider IN ('supabase')", name="ck_auth_operations_proven_provider"),
        sa.CheckConstraint(
            "identity_resolution IN ('existing_identity', 'new_identity', 'conflict_other_profile')",
            name="ck_auth_operations_identity_resolution",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_operations_expiry"),
        sa.CheckConstraint(
            "committed_at IS NULL OR committed_at <= expires_at", name="ck_auth_operations_commit_in_time",
        ),
        sa.CheckConstraint(
            "(kind = 'invitation_activation') = (invitation_id IS NOT NULL)",
            name="ck_auth_operations_invitation_kind",
        ),
        sa.CheckConstraint(
            "kind <> 'invitation_activation' OR (email_normalized IS NULL AND auth_identity_id IS NULL "
            "AND proven_subject IS NULL AND identity_resolution IS NULL)",
            name="ck_auth_operations_invitation_fields",
        ),
        sa.CheckConstraint(
            "kind NOT IN ('supabase_email_sign_in', 'email_linking') OR email_normalized IS NOT NULL "
            "OR pii_redacted_at IS NOT NULL",
            name="ck_auth_operations_email_fields",
        ),
        sa.CheckConstraint(
            "(kind = 'email_linking') = (target_user_id IS NOT NULL) "
            "AND (kind = 'email_linking') = (initiating_session_id IS NOT NULL) "
            "AND (kind = 'email_linking') = (initiating_session_method IS NOT NULL)",
            name="ck_auth_operations_linking_fields",
        ),
        sa.CheckConstraint(
            "target_user_id IS NULL OR user_id IS NULL OR user_id = target_user_id",
            name="ck_auth_operations_linking_target",
        ),
        sa.CheckConstraint(
            "(proven_provider IS NULL) = (proven_issuer IS NULL) "
            "AND (proven_issuer IS NULL) = (proven_subject IS NULL) "
            "AND (identity_resolution IS NULL OR proven_subject IS NOT NULL)",
            name="ck_auth_operations_proven_identity",
        ),
        sa.CheckConstraint(
            "(status = 'committed') = (committed_at IS NOT NULL) "
            "AND (status <> 'committed' OR user_id IS NOT NULL) "
            "AND (status <> 'committed' OR kind = 'invitation_activation' "
            "OR (auth_identity_id IS NOT NULL AND identity_resolution IS NOT NULL "
            "AND identity_resolution IN ('existing_identity', 'new_identity') "
            "AND proven_provider IS NOT NULL AND proven_issuer IS NOT NULL AND proven_subject IS NOT NULL))",
            name="ck_auth_operations_commit",
        ),
        sa.CheckConstraint(
            "identity_resolution <> 'conflict_other_profile' "
            "OR (kind = 'email_linking' AND status = 'failed' AND auth_identity_id IS NULL)",
            name="ck_auth_operations_no_merge",
        ),
        sa.CheckConstraint("(status = 'failed') = (failure_code IS NOT NULL)", name="ck_auth_operations_failure"),
        sa.CheckConstraint("pii_redacted_at IS NULL OR email_normalized IS NULL", name="ck_auth_operations_pii"),
        sa.ForeignKeyConstraint(["invitation_id"], ["invitations.id"]),
        sa.ForeignKeyConstraint(["auth_identity_id"], ["auth_identities.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["auth_identity_id", "user_id"], ["auth_identities.id", "auth_identities.user_id"],
            name="auth_operations_identity_user_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["auth_identity_id", "proven_provider", "proven_issuer", "proven_subject"],
            ["auth_identities.id", "auth_identities.provider", "auth_identities.issuer", "auth_identities.subject"],
            name="auth_operations_proven_identity_fkey",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "kind", name="uq_auth_operations_id_kind"),
        sa.UniqueConstraint("id", "user_id", "kind", name="uq_auth_operations_id_user_kind"),
        sa.UniqueConstraint("id", "invitation_id", name="uq_auth_operations_id_invitation"),
        sa.UniqueConstraint("id", "auth_identity_id", name="uq_auth_operations_id_identity"),
        sa.UniqueConstraint("id", "kind", "status", "committed_at", name="uq_auth_operations_id_kind_commit"),
    )
    op.create_index("ix_auth_operations_invitation_id", "auth_operations", ["invitation_id"])
    op.create_index("ix_auth_operations_auth_identity_id", "auth_operations", ["auth_identity_id"])
    op.create_index("ix_auth_operations_user_id", "auth_operations", ["user_id"])
    op.create_index("ix_auth_operations_target_user_id", "auth_operations", ["target_user_id"])
    op.create_index("ix_auth_operations_initiating_session_id", "auth_operations", ["initiating_session_id"])
    op.create_index(
        "uq_auth_operations_one_committed_invitation", "auth_operations", ["invitation_id"], unique=True,
        postgresql_where=sa.text("status = 'committed'"),
    )

    op.create_table(
        "auth_preauth_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("auth_operation_id", sa.Uuid(), nullable=False),
        sa.Column("operation_kind", sa.String(), nullable=False),
        sa.Column("generation_no", sa.Integer(), nullable=False),
        sa.Column("candidate_hash", sa.String(), nullable=False),
        sa.Column("hash_key_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        _tz("created_at", False),
        _tz("authentication_expires_at", False),
        _tz("bound_at", True),
        _tz("closed_at", True),
        sa.CheckConstraint(
            "operation_kind IN ('supabase_email_sign_in', 'invitation_activation')",
            name="ck_auth_preauth_candidates_operation_kind",
        ),
        sa.CheckConstraint("generation_no >= 1", name="ck_auth_preauth_candidates_generation_no"),
        _hex("auth_preauth_candidates", "candidate_hash"),
        sa.CheckConstraint(
            "status IN ('open', 'bound', 'closed', 'superseded', 'expired', 'revoked')",
            name="ck_auth_preauth_candidates_status",
        ),
        sa.CheckConstraint(
            "authentication_expires_at > created_at "
            "AND authentication_expires_at - created_at <= interval '10 minutes'",
            name="ck_auth_preauth_candidates_authentication_window",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND bound_at IS NULL AND closed_at IS NULL) "
            "OR (status = 'bound' AND bound_at IS NOT NULL AND closed_at IS NULL) "
            "OR (status = 'closed' AND bound_at IS NOT NULL AND closed_at IS NOT NULL) "
            "OR (status IN ('superseded', 'expired') AND bound_at IS NULL AND closed_at IS NOT NULL) "
            "OR (status = 'revoked' AND closed_at IS NOT NULL)",
            name="ck_auth_preauth_candidates_state",
        ),
        sa.CheckConstraint(
            "(bound_at IS NULL OR (bound_at >= created_at AND bound_at <= authentication_expires_at)) "
            "AND (closed_at IS NULL OR closed_at >= created_at) "
            "AND (closed_at IS NULL OR bound_at IS NULL OR closed_at >= bound_at)",
            name="ck_auth_preauth_candidates_timeline",
        ),
        sa.ForeignKeyConstraint(
            ["auth_operation_id", "operation_kind"], ["auth_operations.id", "auth_operations.kind"],
            name="auth_preauth_candidates_operation_kind_fkey",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_hash", name="uq_auth_preauth_candidates_hash"),
        sa.UniqueConstraint(
            "auth_operation_id", "generation_no", name="uq_auth_preauth_candidates_operation_generation",
        ),
        sa.UniqueConstraint(
            "id", "generation_no", "auth_operation_id", name="uq_auth_preauth_candidates_id_generation_operation",
        ),
        sa.UniqueConstraint(
            "id", "auth_operation_id", "bound_at", name="uq_auth_preauth_candidates_id_operation_bound",
        ),
    )
    op.create_index(
        "uq_auth_preauth_candidates_one_open", "auth_preauth_candidates", ["auth_operation_id"], unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "uq_auth_preauth_candidates_one_ever_bound", "auth_preauth_candidates", ["auth_operation_id"], unique=True,
        postgresql_where=sa.text("bound_at IS NOT NULL"),
    )

    op.create_table(
        "auth_otp_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("auth_operation_id", sa.Uuid(), nullable=False),
        sa.Column("operation_kind", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("code_generation", sa.Integer(), nullable=False),
        sa.Column("email_normalized", sa.String(), nullable=True),
        sa.Column("expected_code_hmac", sa.String(), nullable=True),
        sa.Column("expected_code_hmac_key_version", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("failure_count", sa.SmallInteger(), nullable=False),
        _tz("requested_at", False),
        _tz("expires_at", False),
        _tz("provider_sent_at", True),
        _tz("verified_at", True),
        _tz("closed_at", True),
        _tz("pii_redacted_at", True),
        sa.CheckConstraint(
            "operation_kind IN ('supabase_email_sign_in', 'email_linking')",
            name="ck_auth_otp_requests_operation_kind",
        ),
        sa.CheckConstraint("purpose IN ('sign_in', 'email_linking')", name="ck_auth_otp_requests_purpose"),
        sa.CheckConstraint(
            "(operation_kind = 'supabase_email_sign_in' AND purpose = 'sign_in') "
            "OR (operation_kind = 'email_linking' AND purpose = 'email_linking')",
            name="ck_auth_otp_requests_purpose_kind",
        ),
        sa.CheckConstraint("code_generation >= 1", name="ck_auth_otp_requests_code_generation"),
        _email("auth_otp_requests", "email_normalized"),
        sa.CheckConstraint(
            "email_normalized IS NOT NULL OR pii_redacted_at IS NOT NULL", name="ck_auth_otp_requests_email_present",
        ),
        sa.CheckConstraint("pii_redacted_at IS NULL OR email_normalized IS NULL", name="ck_auth_otp_requests_pii"),
        _hex("auth_otp_requests", "expected_code_hmac", nullable=True),
        sa.CheckConstraint(
            "(expected_code_hmac IS NULL) = (expected_code_hmac_key_version IS NULL)",
            name="ck_auth_otp_requests_expected_code_pair",
        ),
        sa.CheckConstraint(
            "status NOT IN ('issued', 'verifying', 'verify_uncertain', 'verified', 'exhausted') "
            "OR expected_code_hmac IS NOT NULL",
            name="ck_auth_otp_requests_expected_code_required",
        ),
        sa.CheckConstraint(
            "status IN ('issuing', 'issue_uncertain', 'issued', 'send_failed', 'verifying', "
            "'verify_uncertain', 'verified', 'exhausted', 'expired', 'superseded')",
            name="ck_auth_otp_requests_status",
        ),
        sa.CheckConstraint("failure_count BETWEEN 0 AND 5", name="ck_auth_otp_requests_failures"),
        sa.CheckConstraint("status <> 'exhausted' OR failure_count = 5", name="ck_auth_otp_requests_exhausted"),
        sa.CheckConstraint(
            "expires_at > requested_at AND expires_at - requested_at <= interval '600 seconds'",
            name="ck_auth_otp_requests_ttl",
        ),
        sa.CheckConstraint(
            "status NOT IN ('issuing', 'issue_uncertain', 'send_failed') OR provider_sent_at IS NULL",
            name="ck_auth_otp_requests_issue",
        ),
        sa.CheckConstraint(
            "(status = 'verified') = (verified_at IS NOT NULL) "
            "AND (verified_at IS NULL OR verified_at <= expires_at)",
            name="ck_auth_otp_requests_verified",
        ),
        sa.CheckConstraint(
            "(status IN ('send_failed', 'exhausted', 'expired', 'superseded')) = (closed_at IS NOT NULL)",
            name="ck_auth_otp_requests_closed",
        ),
        sa.ForeignKeyConstraint(
            ["auth_operation_id", "operation_kind"], ["auth_operations.id", "auth_operations.kind"],
            name="auth_otp_requests_operation_kind_fkey",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("auth_operation_id", "code_generation", name="uq_auth_otp_requests_operation_generation"),
        sa.UniqueConstraint(
            "id", "code_generation", "purpose", "expires_at", "expected_code_hmac_key_version", "expected_code_hmac",
            name="uq_auth_otp_requests_attempt_target",
        ),
        sa.UniqueConstraint(
            "id", "auth_operation_id", "verified_at", "expires_at", name="uq_auth_otp_requests_proof_target",
        ),
    )
    op.create_index(
        "uq_auth_otp_requests_one_live", "auth_otp_requests", ["auth_operation_id"], unique=True,
        postgresql_where=sa.text(f"status IN {OTP_LIVE_STATUSES}"),
    )

    op.create_table(
        "auth_otp_verification_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("otp_request_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("code_generation", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        _tz("request_expires_at", False),
        sa.Column("code_hmac_key_version", sa.String(), nullable=False),
        sa.Column("expected_code_hmac", sa.String(), nullable=False),
        sa.Column("code_hmac", sa.String(), nullable=False),
        sa.Column("local_match", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        _tz("started_at", False),
        _tz("uncertain_since", True),
        _tz("resolved_at", True),
        sa.Column("provider_error_code", sa.String(), nullable=True),
        sa.CheckConstraint("attempt_no >= 1", name="ck_auth_otp_verification_attempts_attempt_no"),
        sa.CheckConstraint(
            "purpose IN ('sign_in', 'email_linking')", name="ck_auth_otp_verification_attempts_purpose",
        ),
        _hex("auth_otp_verification_attempts", "expected_code_hmac"),
        _hex("auth_otp_verification_attempts", "code_hmac"),
        sa.CheckConstraint(
            "state IN ('in_flight', 'uncertain', 'rejected', 'accepted', 'abandoned')",
            name="ck_auth_otp_verification_attempts_state",
        ),
        sa.CheckConstraint(
            "local_match = (code_hmac = expected_code_hmac)", name="ck_auth_otp_verification_attempts_local_match",
        ),
        sa.CheckConstraint(
            "state NOT IN ('in_flight', 'uncertain', 'accepted') OR local_match",
            name="ck_auth_otp_verification_attempts_external_requires_match",
        ),
        sa.CheckConstraint(
            "local_match OR (state = 'rejected' AND uncertain_since IS NULL AND provider_error_code IS NULL)",
            name="ck_auth_otp_verification_attempts_local_rejection",
        ),
        sa.CheckConstraint(
            "(state <> 'in_flight' OR uncertain_since IS NULL) "
            "AND (state <> 'uncertain' OR uncertain_since IS NOT NULL)",
            name="ck_auth_otp_verification_attempts_uncertainty",
        ),
        sa.CheckConstraint(
            "(state IN ('rejected', 'accepted', 'abandoned')) = (resolved_at IS NOT NULL) "
            "AND (resolved_at IS NULL OR resolved_at >= started_at) "
            "AND (uncertain_since IS NULL OR uncertain_since >= started_at)",
            name="ck_auth_otp_verification_attempts_resolution",
        ),
        sa.CheckConstraint(
            "started_at <= request_expires_at", name="ck_auth_otp_verification_attempts_started_in_ttl",
        ),
        sa.ForeignKeyConstraint(
            ["otp_request_id", "code_generation", "purpose", "request_expires_at", "code_hmac_key_version",
             "expected_code_hmac"],
            ["auth_otp_requests.id", "auth_otp_requests.code_generation", "auth_otp_requests.purpose",
             "auth_otp_requests.expires_at", "auth_otp_requests.expected_code_hmac_key_version",
             "auth_otp_requests.expected_code_hmac"],
            name="auth_otp_verification_attempts_request_fkey",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "otp_request_id", "attempt_no", name="uq_auth_otp_verification_attempts_request_attempt",
        ),
    )
    op.create_index(
        "uq_auth_otp_verification_attempts_one_unresolved", "auth_otp_verification_attempts", ["otp_request_id"],
        unique=True, postgresql_where=sa.text("state IN ('in_flight', 'uncertain')"),
    )
    op.create_index(
        "uq_auth_otp_verification_attempts_one_accepted", "auth_otp_verification_attempts", ["otp_request_id"],
        unique=True, postgresql_where=sa.text("state = 'accepted'"),
    )

    op.create_table(
        "auth_abuse_subjects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope_kind", sa.String(), nullable=False),
        sa.Column("scope_hmac", sa.String(), nullable=False),
        sa.Column("hash_key_version", sa.String(), nullable=False),
        _tz("created_at", False),
        sa.CheckConstraint("scope_kind IN ('email', 'ip')", name="ck_auth_abuse_subjects_scope_kind"),
        _hex("auth_abuse_subjects", "scope_hmac"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_kind", "hash_key_version", "scope_hmac", name="uq_auth_abuse_subjects_scope"),
    )

    op.create_table(
        "auth_abuse_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("event_kind", sa.String(), nullable=False),
        _tz("occurred_at", False),
        sa.Column("auth_operation_id", sa.Uuid(), nullable=True),
        sa.Column("otp_request_id", sa.Uuid(), nullable=True),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.CheckConstraint(
            "event_kind IN ('otp_request', 'otp_failure', 'invitation_attempt', 'resume_attempt')",
            name="ck_auth_abuse_events_event_kind",
        ),
        sa.CheckConstraint(
            "(event_kind IN ('otp_request', 'otp_failure')) = (otp_request_id IS NOT NULL)",
            name="ck_auth_abuse_events_otp_link",
        ),
        sa.CheckConstraint(
            "event_kind <> 'resume_attempt' OR auth_operation_id IS NOT NULL",
            name="ck_auth_abuse_events_resume_link",
        ),
        sa.ForeignKeyConstraint(["subject_id"], ["auth_abuse_subjects.id"]),
        sa.ForeignKeyConstraint(["auth_operation_id"], ["auth_operations.id"]),
        sa.ForeignKeyConstraint(["otp_request_id"], ["auth_otp_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_abuse_events_window", "auth_abuse_events", ["subject_id", "event_kind", "occurred_at"])
    op.create_index("ix_auth_abuse_events_auth_operation_id", "auth_abuse_events", ["auth_operation_id"])
    op.create_index("ix_auth_abuse_events_otp_request_id", "auth_abuse_events", ["otp_request_id"])

    op.create_table(
        "oryx_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("created_by_operation_id", sa.Uuid(), nullable=False),
        sa.Column("auth_method", sa.String(), nullable=False),
        sa.Column("invitation_id", sa.Uuid(), nullable=True),
        sa.Column("auth_identity_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        _tz("created_at", False),
        _tz("absolute_expires_at", False),
        _tz("revoked_at", True),
        sa.Column("revocation_reason", sa.String(), nullable=True),
        sa.Column("session_schema_version", sa.String(), nullable=False),
        sa.CheckConstraint(
            "auth_method IN ('supabase_email_sign_in', 'invitation_activation')",
            name="ck_oryx_sessions_auth_method",
        ),
        sa.CheckConstraint(
            "(auth_method = 'invitation_activation') = (invitation_id IS NOT NULL)",
            name="ck_oryx_sessions_invitation_provenance",
        ),
        sa.CheckConstraint(
            "(auth_method = 'supabase_email_sign_in') = (auth_identity_id IS NOT NULL)",
            name="ck_oryx_sessions_identity_provenance",
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_oryx_sessions_status"),
        sa.CheckConstraint(
            "absolute_expires_at - created_at = interval '15 days'", name="ck_oryx_sessions_absolute_ttl",
        ),
        sa.CheckConstraint(
            "revocation_reason IN ('logout', 'admin', 'security')", name="ck_oryx_sessions_revocation_reason",
        ),
        sa.CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL) AND (revoked_at IS NULL) = (revocation_reason IS NULL) "
            "AND (revoked_at IS NULL OR revoked_at >= created_at)",
            name="ck_oryx_sessions_revocation",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["created_by_operation_id", "user_id", "auth_method"],
            ["auth_operations.id", "auth_operations.user_id", "auth_operations.kind"],
            name="oryx_sessions_operation_holder_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_operation_id", "invitation_id"], ["auth_operations.id", "auth_operations.invitation_id"],
            name="oryx_sessions_operation_invitation_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_operation_id", "auth_identity_id"],
            ["auth_operations.id", "auth_operations.auth_identity_id"],
            name="oryx_sessions_operation_identity_fkey",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("created_by_operation_id", name="uq_oryx_sessions_created_by_operation"),
        sa.UniqueConstraint("id", "created_by_operation_id", name="uq_oryx_sessions_id_operation"),
        sa.UniqueConstraint("id", "user_id", "auth_method", name="uq_oryx_sessions_id_user_method"),
    )
    op.create_index("ix_oryx_sessions_user_id", "oryx_sessions", ["user_id"])
    op.create_foreign_key(
        LINKING_SESSION_FK, "auth_operations", "oryx_sessions",
        ["initiating_session_id", "target_user_id", "initiating_session_method"],
        ["id", "user_id", "auth_method"],
    )

    op.create_table(
        "oryx_session_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("auth_operation_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("generation_no", sa.Integer(), nullable=False),
        sa.Column("active_secret_hash", sa.String(), nullable=False),
        sa.Column("derivation_version", sa.String(), nullable=False),
        sa.Column("hash_key_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        _tz("issued_at", False),
        sa.Column("delivery_status", sa.String(), nullable=False),
        _tz("delivery_confirmed_at", True),
        sa.Column("delivery_proof_version", sa.String(), nullable=True),
        _tz("revoked_at", True),
        sa.Column("revocation_reason", sa.String(), nullable=True),
        sa.CheckConstraint("generation_no >= 1", name="ck_oryx_session_generations_generation_no"),
        _hex("oryx_session_generations", "active_secret_hash"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_oryx_session_generations_status"),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'confirmed')", name="ck_oryx_session_generations_delivery_status",
        ),
        sa.CheckConstraint(
            "(delivery_status = 'confirmed') = (delivery_confirmed_at IS NOT NULL) "
            "AND (delivery_confirmed_at IS NULL) = (delivery_proof_version IS NULL) "
            "AND (delivery_confirmed_at IS NULL OR delivery_confirmed_at >= issued_at) "
            "AND (delivery_confirmed_at IS NULL OR revoked_at IS NULL OR delivery_confirmed_at <= revoked_at)",
            name="ck_oryx_session_generations_delivery",
        ),
        sa.CheckConstraint(
            "revocation_reason IN ('logout', 'admin', 'security')",
            name="ck_oryx_session_generations_revocation_reason",
        ),
        sa.CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL) AND (revoked_at IS NULL) = (revocation_reason IS NULL) "
            "AND (revoked_at IS NULL OR revoked_at >= issued_at)",
            name="ck_oryx_session_generations_revocation",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "auth_operation_id"], ["oryx_sessions.id", "oryx_sessions.created_by_operation_id"],
            name="oryx_session_generations_session_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "generation_no", "auth_operation_id"],
            ["auth_preauth_candidates.id", "auth_preauth_candidates.generation_no",
             "auth_preauth_candidates.auth_operation_id"],
            name="oryx_session_generations_candidate_fkey",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", name="uq_oryx_session_generations_session"),
        sa.UniqueConstraint("candidate_id", name="uq_oryx_session_generations_candidate"),
        sa.UniqueConstraint("active_secret_hash", name="uq_oryx_session_generations_secret_hash"),
        sa.UniqueConstraint(
            "id", "session_id", "candidate_id", name="uq_oryx_session_generations_id_session_candidate",
        ),
    )

    op.create_table(
        "auth_operation_results",
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("operation_kind", sa.String(), nullable=False),
        sa.Column("operation_status", sa.String(), nullable=False),
        _tz("committed_at", False),
        _tz("resume_until", False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        _tz("candidate_bound_at", True),
        sa.Column("generation_id", sa.Uuid(), nullable=True),
        sa.Column("auth_identity_id", sa.Uuid(), nullable=True),
        sa.Column("otp_request_id", sa.Uuid(), nullable=True),
        _tz("otp_verified_at", True),
        _tz("otp_expires_at", True),
        sa.Column("result_version", sa.String(), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "operation_kind IN ('supabase_email_sign_in', 'invitation_activation', 'email_linking')",
            name="ck_auth_operation_results_operation_kind",
        ),
        sa.CheckConstraint("operation_status = 'committed'", name="ck_auth_operation_results_operation_status"),
        sa.CheckConstraint(
            "resume_until - committed_at = interval '5 minutes'", name="ck_auth_operation_results_resume_window",
        ),
        sa.CheckConstraint(
            "(operation_kind = 'email_linking') = (session_id IS NULL) "
            "AND (session_id IS NULL) = (candidate_id IS NULL) "
            "AND (candidate_id IS NULL) = (generation_id IS NULL) "
            "AND (candidate_id IS NULL) = (candidate_bound_at IS NULL)",
            name="ck_auth_operation_results_session_parts",
        ),
        sa.CheckConstraint(
            "candidate_bound_at IS NULL OR candidate_bound_at = committed_at",
            name="ck_auth_operation_results_candidate_bound_at",
        ),
        sa.CheckConstraint(
            "(operation_kind IN ('supabase_email_sign_in', 'email_linking')) = (auth_identity_id IS NOT NULL)",
            name="ck_auth_operation_results_identity",
        ),
        sa.CheckConstraint(
            "(operation_kind IN ('supabase_email_sign_in', 'email_linking')) = (otp_request_id IS NOT NULL) "
            "AND (otp_request_id IS NULL) = (otp_verified_at IS NULL) "
            "AND (otp_verified_at IS NULL) = (otp_expires_at IS NULL)",
            name="ck_auth_operation_results_otp_parts",
        ),
        sa.CheckConstraint(
            "otp_verified_at IS NULL OR (committed_at >= otp_verified_at AND committed_at <= otp_expires_at)",
            name="ck_auth_operation_results_otp_recent",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "operation_kind", "operation_status", "committed_at"],
            ["auth_operations.id", "auth_operations.kind", "auth_operations.status", "auth_operations.committed_at"],
            name="auth_operation_results_operation_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "operation_id"], ["oryx_sessions.id", "oryx_sessions.created_by_operation_id"],
            name="auth_operation_results_session_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "operation_id", "candidate_bound_at"],
            ["auth_preauth_candidates.id", "auth_preauth_candidates.auth_operation_id",
             "auth_preauth_candidates.bound_at"],
            name="auth_operation_results_candidate_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "session_id", "candidate_id"],
            ["oryx_session_generations.id", "oryx_session_generations.session_id",
             "oryx_session_generations.candidate_id"],
            name="auth_operation_results_generation_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "auth_identity_id"], ["auth_operations.id", "auth_operations.auth_identity_id"],
            name="auth_operation_results_identity_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["otp_request_id", "operation_id", "otp_verified_at", "otp_expires_at"],
            ["auth_otp_requests.id", "auth_otp_requests.auth_operation_id", "auth_otp_requests.verified_at",
             "auth_otp_requests.expires_at"],
            name="auth_operation_results_otp_fkey",
        ),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint("session_id", name="uq_auth_operation_results_session"),
        sa.UniqueConstraint("candidate_id", name="uq_auth_operation_results_candidate"),
        sa.UniqueConstraint("generation_id", name="uq_auth_operation_results_generation"),
        sa.UniqueConstraint("otp_request_id", name="uq_auth_operation_results_otp_request"),
    )


def downgrade() -> None:
    """Retour exact à 0016 : FK de session initiatrice, puis les onze tables
    dans l'ordre inverse (leurs index disparaissent avec elles)."""
    op.drop_table("auth_operation_results")
    op.drop_table("oryx_session_generations")
    op.drop_constraint(LINKING_SESSION_FK, "auth_operations", type_="foreignkey")
    for table in reversed(TABLES[:-2]):
        op.drop_table(table)
