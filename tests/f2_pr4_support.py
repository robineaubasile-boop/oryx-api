"""Outils des tests PostgreSQL de R1 PR 4 : application isolée complète
(moteur F2 réel, admission HTTP réelle), un navigateur simulé par IP (pair
TCP lu par PeerAddressResolver), parcours d'invitation et de liaison
e-mail, lecture des lignes durables.

Doubles Supabase / Resend de tests/f2_support.py : ils échouent si une
connexion du service est empruntée ou si une transaction PostgreSQL est
ouverte pendant leur appel. Aucun réseau, aucune clé réelle.
"""
import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.service import F2AuthService
from core.auth_f2_http.client_ip import PeerAddressResolver
from core.auth_f2_http.isolated import build_isolated_f2_app
from tests.f2_http_support import ORIGIN, Browser, csrf_keyring
from tests.f2_support import (
    FakeMailer,
    FakeSupabase,
    FixedClock,
    TransactionProbe,
    clean_f2,
    make_keyring,
    rows,
    scalar,
    service_engine,
)

IP_A = "198.51.100.1"
IP_B = "198.51.100.2"
ALICE = "alice@example.com"


class Pr4Env:
    def __init__(self, pg_url, keyring=None):
        self.pg_url = pg_url
        self.observer = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
        clean_f2(self.observer)
        self.engine = service_engine(pg_url)
        self.keyring = keyring or make_keyring()
        probe = TransactionProbe(self.engine, self.observer)
        self.supabase, self.mailer = FakeSupabase(probe), FakeMailer(probe)
        self.clock = None
        self.hooks = {}

    def hook(self, point):
        action = self.hooks.get(point)
        if action is not None:
            action()

    def init_abuse(self):
        abuse.initialize_abuse_key_state(self.engine, self.keyring, self.keyring.current_version("abuse"))

    def app(self, *, fixed_clock=False, init_abuse=True, keyring=None):
        if init_abuse:
            self.init_abuse()
        if fixed_clock and self.clock is None:
            self.clock = FixedClock(self.observer)
        return build_isolated_f2_app(
            engine=self.engine, keyring=keyring or self.keyring, supabase=self.supabase, mailer=self.mailer,
            csrf_keyring=csrf_keyring(), ip_resolver=PeerAddressResolver(), allowed_origins=[ORIGIN],
            clock=self.clock if fixed_clock else None, fault_hook=self.hook)

    def admin(self):
        """Service d'administration (création d'invitation) : hors HTTP."""
        kwargs = {"clock": self.clock} if self.clock is not None else {}
        return F2AuthService(self.engine, self.keyring, self.supabase, self.mailer, **kwargs)

    def browser(self, app, ip=IP_A):
        browser = Browser(app, peer=(ip, 40000))
        browser.bootstrap()
        return browser

    def invitation(self):
        return self.admin().create_invitation(cohort="pr4-test").token.reveal()

    def count(self, table, where="TRUE", **params):
        return scalar(self.observer, f"SELECT count(*) FROM {table} WHERE {where}", **params)

    def probes(self, action=None):
        if action is None:
            return self.count("auth_abuse_events", "event_kind = 'auth_http_probe_attempt'")
        return self.count("auth_abuse_events", "event_kind = 'auth_http_probe_attempt' AND http_action = :a", a=action)

    def resume_attempts(self, operation_id=None):
        if operation_id is None:
            return self.count("auth_abuse_events", "event_kind = 'resume_attempt'")
        return self.count("auth_abuse_events", "event_kind = 'resume_attempt' AND auth_operation_id = :o",
                          o=operation_id)

    def rows(self, sql, **params):
        return rows(self.observer, sql, **params)

    def dispose(self):
        self.engine.dispose()
        self.observer.dispose()


def begin(browser, token):
    return browser.post("/invitation/begin", {"token": token})


def complete(browser, begin_body):
    return browser.post("/invitation/complete", {"operation_id": begin_body["operation_id"],
                                                 "candidate_id": begin_body["candidate_id"]})


def resume(browser, begin_body):
    return browser.post("/invitation/resume", {"operation_id": begin_body["operation_id"],
                                               "candidate_id": begin_body["candidate_id"]})


def activate(env, browser, token=None):
    """Invitation -> begin -> complete ; (corps begin, corps complete)."""
    started = begin(browser, token or env.invitation())
    assert started.status_code == 202, started.text
    done = complete(browser, started.json())
    assert done.status_code == 200, done.text
    return started.json(), done.json()


def link_start(browser, generation_id, email=ALICE):
    return browser.post("/email-link/start", {"generation_id": generation_id, "email": email})


def link_resend(browser, generation_id, operation_id):
    return browser.post("/email-link/resend", {"generation_id": generation_id, "operation_id": operation_id})


def link_verify(browser, generation_id, operation_id, code):
    return browser.post("/email-link/verify", {"generation_id": generation_id, "operation_id": operation_id,
                                               "code": code})


def link_result(browser, generation_id, operation_id):
    return browser.post("/email-link/result", {"generation_id": generation_id, "operation_id": operation_id})


def linked(env, browser, email=ALICE):
    """Activation puis liaison e-mail vérifiée ; (generation_id, operation_id
    de la liaison, corps begin)."""
    started, done = activate(env, browser)
    generation_id = done["generation_id"]
    start = link_start(browser, generation_id, email)
    assert start.status_code == 202, start.text
    operation_id = start.json()["operation_id"]
    verify = link_verify(browser, generation_id, operation_id, env.mailer.last_code(email))
    assert verify.status_code == 200, verify.text
    return generation_id, operation_id, started
