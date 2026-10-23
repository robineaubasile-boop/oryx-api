"""Outils communs des tests F2 (R1 PR 2) : trousseau FICTIF, doubles
Supabase / Resend, horloge contrôlée, schéma jetable.

Aucune clé, aucun secret, aucune URL de production. Les doubles vérifient
à chaque appel qu'aucune connexion du service n'est empruntée (pool) ni
en transaction côté serveur (pg_stat_activity) : aucun appel fournisseur
pendant une transaction ou un verrou PostgreSQL.
"""
import itertools
import re
from datetime import timedelta

import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.keys import PURPOSES, Keyring
from core.auth_f2.providers import (
    GeneratedOtp,
    MailSent,
    ProviderRejected,
    VerifiedIdentity,
)
from core.auth_f2.service import F2AuthService
from tests.pg_guard import assert_disposable_connection
from tests.test_migration_0002_analysis_sessions import _reset_schema, _run_alembic

ISSUER = "https://oryx-auth-fake.supabase.co/auth/v1"
APP_NAME = "oryx-f2-service-test"
F2_TABLES = (
    "auth_operation_results", "oryx_session_generations", "oryx_sessions", "auth_abuse_events",
    "auth_abuse_subjects", "auth_abuse_key_state", "auth_otp_verification_attempts", "auth_otp_requests",
    "auth_preauth_candidates", "auth_operations", "invitations", "auth_identities",
)


def fake_key(purpose: str, version: str) -> bytes:
    """Clé fictive déterministe, distincte par usage et par version."""
    seed = (PURPOSES.index(purpose) + 1) * 16 + sum(version.encode()) % 16
    return bytes([seed % 256]) * 31 + bytes([len(version) % 256])


def make_keyring(versions=None, current=None) -> Keyring:
    versions = versions or {p: ("t1",) for p in PURPOSES}
    current = current or {p: versions[p][0] for p in PURPOSES}
    return Keyring({p: {v: fake_key(p, v) for v in versions[p]} for p in PURPOSES}, current)


class FixedClock:
    """Horloge contrôlée (tests de frontières) : rend un instant fixe que
    les tests avancent ; sinon l'horloge PostgreSQL réelle."""

    def __init__(self, conn_engine):
        with conn_engine.connect() as c:
            self.now = c.execute(sa.text("SELECT date_trunc('second', clock_timestamp())")).scalar_one()

    def __call__(self, conn):
        return self.now

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)
        return self.now


class TransactionProbe:
    """Échoue si le service tient une connexion ou une transaction pendant
    un appel fournisseur."""

    def __init__(self, service_engine, observer_engine):
        self.service_engine = service_engine
        self.observer_engine = observer_engine
        self.checks = 0

    def assert_idle(self):
        self.checks += 1
        assert self.service_engine.pool.checkedout() == 0, "connexion empruntée pendant un appel fournisseur"
        with self.observer_engine.connect() as c:
            busy = c.execute(sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = :a AND state <> 'idle'"
            ), {"a": APP_NAME}).scalar_one()
        assert busy == 0, "transaction PostgreSQL ouverte pendant un appel fournisseur"


class FakeSupabase:
    """Double de Supabase : generateLink rend un code de huit chiffres
    (ou une issue scriptée), verifyOtp accepte le dernier code généré pour
    l'adresse (ou rend une issue scriptée). Aucun réseau."""

    issuer = ISSUER

    def __init__(self, probe=None):
        self.probe = probe
        self.codes = {}
        self.subjects = {}
        self.generate_calls = []
        self.verify_calls = []
        self.generate_script = []
        self.verify_script = []
        self._counter = itertools.count(12345678)

    def subject_for(self, email):
        return self.subjects.setdefault(email, f"00000000-0000-4000-8000-{len(self.subjects) + 1:012d}")

    def generate_email_otp(self, email):
        if self.probe:
            self.probe.assert_idle()
        self.generate_calls.append(email)
        if self.generate_script:
            scripted = self.generate_script.pop(0)
            if isinstance(scripted, BaseException):
                raise scripted
            if not isinstance(scripted, GeneratedOtp) or scripted.code is None:
                return scripted
        code = f"{next(self._counter) % 100000000:08d}"
        self.codes[email] = code
        return GeneratedOtp(code=code)

    def verify_email_otp(self, email, code):
        if self.probe:
            self.probe.assert_idle()
        self.verify_calls.append((email, code))
        if self.verify_script:
            scripted = self.verify_script.pop(0)
            if isinstance(scripted, BaseException):
                raise scripted
            if scripted is not None:
                return scripted
        if self.codes.get(email) == code:
            del self.codes[email]
            return VerifiedIdentity(subject=self.subject_for(email), email=email)
        return ProviderRejected("otp_expired")


class FakeMailer:
    def __init__(self, probe=None):
        self.probe = probe
        self.sent = []
        self.script = []

    def send_otp(self, *, to, code, idempotency_key):
        if self.probe:
            self.probe.assert_idle()
        self.sent.append((to, code, idempotency_key))
        if self.script:
            scripted = self.script.pop(0)
            if isinstance(scripted, BaseException):
                raise scripted
            return scripted
        return MailSent()

    def last_code(self, to):
        return [code for t, code, _ in self.sent if t == to][-1]


class SimulatedCrash(BaseException):
    """Arrêt brutal simulé du processus (non intercepté par le service)."""


def prepare_schema(pg_url, engine):
    _reset_schema(engine)
    _run_alembic(pg_url, "upgrade", "head")


def clean_f2(engine):
    with engine.begin() as conn:
        assert_disposable_connection(conn)
        conn.execute(sa.text(f"TRUNCATE {', '.join(F2_TABLES)}, users CASCADE"))


def service_engine(pg_url):
    return sa.create_engine(pg_url, pool_size=5, max_overflow=0, connect_args={"application_name": APP_NAME})


def build_service(engine, observer, keyring=None, *, clock=None, fault_hook=None, init_abuse=True):
    keyring = keyring or make_keyring()
    probe = TransactionProbe(engine, observer)
    supabase, mailer = FakeSupabase(probe), FakeMailer(probe)
    if init_abuse:
        abuse.initialize_abuse_key_state(engine, keyring, keyring.current_version("abuse"))
    kwargs = {"fault_hook": fault_hook}
    if clock is not None:
        kwargs["clock"] = clock
    return F2AuthService(engine, keyring, supabase, mailer, **kwargs), supabase, mailer


def create_profile_with_identity(engine, supabase, email, user_id="legacy-user-1"):
    """Profil existant lié à une identité Supabase (préalable d'une
    connexion OTP : aucune inscription implicite)."""
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant') ON CONFLICT DO NOTHING"),
                     {"u": user_id})
        conn.execute(sa.text(
            "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, verified_email_normalized, "
            "email_verified_at, created_at) VALUES (gen_random_uuid(), :u, 'supabase', :i, :s, :e, now(), now())"),
            {"u": user_id, "i": ISSUER, "s": supabase.subject_for(email), "e": email})
    return user_id


def rows(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(sa.text(sql), params).mappings().all()


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar_one()


SECRET_PATTERNS = (re.compile(r"\b[0-9]{8}\b"), re.compile(r"[A-Za-z0-9_-]{43}"))
