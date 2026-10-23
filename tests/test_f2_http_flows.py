"""Parcours HTTP F2 (R1 PR 3) contre un vrai PostgreSQL jetable.

Application ISOLÉE (core.auth_f2_http.isolated) autour du vrai
F2AuthService de PR 2, doubles Supabase / Resend qui vérifient qu'aucune
transaction n'est ouverte pendant leurs appels (tests/f2_support.py), ou
adaptateurs HTTP réels sur httpx.MockTransport. Aucun réseau, aucune clé
réelle.

Couvre : connexion de bout en bout, cookies candidats et actifs, codes
incorrects et mal formés (compteurs PR 2 conservés), quotas, réponses
fournisseurs incertaines, renvoi explicite, reprise canonique après perte
de réponse (aucun nouvel appel fournisseur, même session / génération /
secret), frontières temporelles (horloge contrôlée), absence de profil
implicite, refus comparables, plusieurs comptes dans un navigateur,
plusieurs appareils, révocation ciblée, concurrence et réponses tardives.
"""
import asyncio
import json
import re
import threading
import uuid
from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.crypto import Redacted
from core.auth_f2.http_providers import HttpResendMailer, HttpSupabaseOtpProvider
from core.auth_f2.policy import VERIFY_IP_LIMIT
from core.auth_f2.providers import ProviderRejected, ProviderUncertain, VerifiedIdentity
from core.auth_f2_http.cookies import active_name, candidate_name
from core.auth_f2_http.isolated import F2HttpConfigError, build_isolated_f2_app
from tests.f2_http_support import F2, ORIGIN, Browser, StaticIp, active_cookies_set, csrf_keyring, set_cookies
from tests.f2_support import (
    APP_NAME,
    F2_TABLES,
    FakeMailer,
    FakeSupabase,
    FixedClock,
    TransactionProbe,
    clean_f2,
    create_profile_with_identity,
    make_keyring,
    prepare_schema,
    rows,
    scalar,
    service_engine,
)
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

ALICE = "alice@example.com"
BOB = "bob@example.com"
NOBODY = "nobody@example.com"
US = timedelta(microseconds=1)


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


class Env:
    def __init__(self, pg_url):
        self.observer = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
        clean_f2(self.observer)
        self.engine = service_engine(pg_url)
        self.keyring = make_keyring()
        probe = TransactionProbe(self.engine, self.observer)
        self.supabase, self.mailer = FakeSupabase(probe), FakeMailer(probe)
        self.clock = None
        self.hooks = {}
        self.ip = StaticIp()

    def hook(self, point):
        action = self.hooks.get(point)
        if action is not None:
            action()

    def app(self, *, fixed_clock=False, init_abuse=True, supabase=None, mailer=None):
        if init_abuse:
            abuse.initialize_abuse_key_state(self.engine, self.keyring, self.keyring.current_version("abuse"))
        if fixed_clock and self.clock is None:
            self.clock = FixedClock(self.observer)
        return build_isolated_f2_app(
            engine=self.engine, keyring=self.keyring, supabase=supabase or self.supabase,
            mailer=mailer or self.mailer, csrf_keyring=csrf_keyring(), ip_resolver=self.ip,
            allowed_origins=[ORIGIN], clock=self.clock if fixed_clock else None, fault_hook=self.hook)

    def browser(self, app):
        browser = Browser(app)
        browser.bootstrap()
        return browser

    def count(self, table, where="TRUE", **params):
        return scalar(self.observer, f"SELECT count(*) FROM {table} WHERE {where}", **params)

    def dispose(self):
        self.engine.dispose()
        self.observer.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = Env(pg_url)
    yield environment
    environment.dispose()


def sign_in(env, browser, email=ALICE):
    start = browser.start(email)
    assert start.status_code == 202, start.text
    body = start.json()
    verify = browser.verify(body, env.mailer.last_code(email))
    assert verify.status_code == 200, verify.text
    return body, verify.json()


def no_secret_in(response, *values):
    for value in values:
        assert value not in response.text, value
    assert "user_id" not in response.text and "subject" not in response.text


# --------------------------------------------------------------------------
# Parcours nominal et cookies
# --------------------------------------------------------------------------

def test_sign_in_end_to_end_through_http(env):
    user_id = create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE)
    body = start.json()
    assert start.status_code == 202 and body["status"] == "code_sent"
    (candidate,) = set_cookies(start)
    assert candidate["name"] == candidate_name(body["candidate_id"])
    assert candidate["attrs"].keys() == {"max-age", "path", "secure", "httponly", "samesite"}
    assert 890 <= int(candidate["attrs"]["max-age"]) <= 900 and candidate["attrs"]["samesite"] == "Lax"
    secret = candidate["value"]
    stored = rows(env.observer, "SELECT * FROM auth_preauth_candidates")[0]
    assert str(stored["id"]) == body["candidate_id"] and secret not in json.dumps({k: str(v) for k, v in stored.items()})
    code = env.mailer.last_code(ALICE)

    verify = browser.verify(body, code)
    result = verify.json()
    assert verify.status_code == 200 and result["status"] == "authenticated"
    (active,) = active_cookies_set(verify)
    assert active["name"] == active_name(result["generation_id"])
    assert 15 * 86400 - 10 <= int(active["attrs"]["max-age"]) == result["expires_in"] <= 15 * 86400
    # Candidat conservé (ni effacé ni renouvelé) : reprise possible si la
    # réponse de succès est perdue.
    assert [c["name"] for c in set_cookies(verify)] == [active["name"]]
    assert browser.candidates() == {candidate["name"]: secret}
    session = rows(env.observer, "SELECT s.*, g.delivery_status FROM oryx_sessions s JOIN oryx_session_generations g "
                                 "ON g.session_id = s.id")[0]
    assert session["user_id"] == user_id and session["delivery_status"] == "pending"
    expires_in_db = (session["absolute_expires_at"] - scalar(env.observer, "SELECT clock_timestamp()")).total_seconds()
    assert int(active["attrs"]["max-age"]) <= expires_in_db

    resolve = browser.resolve(result["generation_id"])
    assert resolve.json() == {"status": "active", "generation_id": result["generation_id"]}
    assert rows(env.observer, "SELECT delivery_status FROM oryx_session_generations")[0]["delivery_status"] == "confirmed"
    for response in (start, verify, resolve):
        assert response.headers["cache-control"] == "no-store"
        no_secret_in(response, code, user_id, env.supabase.subject_for(ALICE), ALICE, active["value"], secret)


def test_unknown_identity_never_creates_a_profile(env):
    browser = env.browser(env.app())
    users = env.count("users")
    start = browser.start(NOBODY).json()
    response = browser.verify(start, env.mailer.last_code(NOBODY))
    assert (response.status_code, response.json()) == (401, {"error": "sign_in_failed"})
    assert active_cookies_set(response) == []
    assert env.count("users") == users and env.count("oryx_sessions") == 0 and env.count("auth_identities") == 0


def _comparable(response):
    headers = {k: v for k, v in response.headers.items() if k not in ("date",)}
    return response.status_code, response.content, len(response.content), headers


def test_identity_absence_is_not_disclosed_by_comparable_refusals(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    app = env.app()
    observed = {}
    # Identité absente (preuve valide, aucun profil lié).
    b1 = env.browser(app)
    s1 = b1.start(NOBODY).json()
    observed["identity_not_linked"] = b1.verify(s1, env.mailer.last_code(NOBODY))
    # Refus certain du fournisseur après correspondance locale.
    b2 = env.browser(app)
    s2 = b2.start(ALICE).json()
    env.supabase.verify_script.append(ProviderRejected("otp_expired"))
    observed["provider_rejected"] = b2.verify(s2, env.mailer.last_code(ALICE))
    # Preuve d'une autre adresse (non applicable).
    env.ip.ip = "203.0.113.8"
    b3 = env.browser(app)
    s3 = b3.start(BOB).json()
    env.supabase.verify_script.append(VerifiedIdentity(subject=env.supabase.subject_for(ALICE), email=ALICE))
    observed["proof_not_applicable"] = b3.verify(s3, env.mailer.last_code(BOB))
    assert len({repr(_comparable(r)) for r in observed.values()}) == 1
    status, content, _, _ = _comparable(observed["identity_not_linked"])
    assert (status, json.loads(content)) == (401, {"error": "sign_in_failed"})
    assert all(set_cookies(r) == [] for r in observed.values())
    # Les justificatifs candidats restent dans le navigateur dans les trois cas.
    assert all(b.candidates() for b in (b1, b2, b3))
    # Limite documentée (héritée de PR 2) : l'opération d'une identité non
    # liée est close, celle d'un refus fournisseur reste ouverte ; un renvoi
    # ultérieur ne répond donc pas de la même façon (docs/f2_r1_pr3.md §8).
    assert rows(env.observer, "SELECT status FROM auth_operations WHERE id = :i", i=s1["operation_id"])[0][
        "status"] == "failed"


# --------------------------------------------------------------------------
# Codes incorrects, mal formés, quotas (compteurs PR 2 conservés)
# --------------------------------------------------------------------------

def test_wrong_and_malformed_codes_are_counted_by_the_engine(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE).json()
    good = env.mailer.last_code(ALICE)
    wrong = f"{(int(good) + 1) % 100000000:08d}"
    for code in (wrong, "abc", "1" * 300, wrong):
        response = browser.verify(start, code)
        assert (response.status_code, response.json()) == (401, {"error": "code_incorrect"})
    request = rows(env.observer, "SELECT * FROM auth_otp_requests")[0]
    assert request["failure_count"] == 4
    assert env.count("auth_otp_verification_attempts") == 2  # code mal formé : aucune tentative
    assert env.count("auth_abuse_events", "event_kind = 'otp_failure'") == 4
    # 5e échec : demande épuisée ET blocage e-mail (5 échecs / 15 min) ; le
    # blocage prime dans la réponse du moteur.
    response = browser.verify(start, "x")
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})
    assert rows(env.observer, "SELECT status FROM auth_otp_requests")[0]["status"] == "exhausted"
    response = browser.verify(start, good)
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})
    assert env.supabase.verify_calls == [] and env.count("oryx_sessions") == 0


def test_issue_interval_is_enforced_without_provider_call(env):
    browser = env.browser(env.app())
    assert browser.start(ALICE).status_code == 202
    response = browser.start(ALICE)
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})
    assert set_cookies(response) == []
    assert env.supabase.generate_calls == [ALICE]


def test_verification_ip_quota_counts_unknown_operations(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE).json()
    other = {**start, "operation_id": "00000000-0000-4000-8000-0000000000ff"}
    for _ in range(VERIFY_IP_LIMIT):
        response = browser.verify(other, "12345678")
        assert (response.status_code, response.json()) == (409, {"error": "operation_unavailable"})
    response = browser.verify(start, env.mailer.last_code(ALICE))
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})
    assert env.supabase.verify_calls == []


# --------------------------------------------------------------------------
# Incertitudes fournisseurs et renvoi explicite
# --------------------------------------------------------------------------

def test_uncertain_generation_keeps_the_candidate_and_needs_an_explicit_resend(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    app = env.app(fixed_clock=True)
    browser = env.browser(app)
    env.supabase.generate_script.append(ProviderUncertain("http_503"))
    start = browser.start(ALICE)
    body = start.json()
    assert (start.status_code, body["error"]) == (503, "delivery_uncertain")
    assert set(body) == {"error", "operation_id", "candidate_id"}
    assert "sent" not in start.text
    assert [c["name"] for c in set_cookies(start)] == [candidate_name(body["candidate_id"])]
    assert env.supabase.generate_calls == [ALICE] and env.mailer.sent == []  # aucune relance automatique
    response = browser.resend(body)
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})  # 60 s entre émissions
    env.clock.advance(seconds=60)
    response = browser.resend(body)
    assert response.json() == {"status": "code_sent", **{k: body[k] for k in ("operation_id", "candidate_id")}}
    assert set_cookies(response) == []  # le renvoi ne renouvelle jamais le cookie candidat
    assert len(env.supabase.generate_calls) == 2 and len(env.mailer.sent) == 1
    assert browser.verify(body, env.mailer.last_code(ALICE)).json()["status"] == "authenticated"


def test_uncertain_mail_send_is_never_announced_as_sent(env):
    browser = env.browser(env.app())
    env.mailer.script.append(ProviderUncertain("http_500"))
    start = browser.start(ALICE)
    assert (start.status_code, start.json()["error"]) == (503, "delivery_uncertain")
    assert "sent" not in start.text and browser.candidates()
    assert len(env.mailer.sent) == 1 and len(env.supabase.generate_calls) == 1


def test_certain_generation_failure_keeps_the_candidate(env):
    browser = env.browser(env.app())
    env.supabase.generate_script.append(ProviderRejected("email_address_invalid"))
    start = browser.start(ALICE)
    assert (start.status_code, start.json()["error"]) == (503, "delivery_failed")
    assert "sent" not in start.text
    assert browser.candidates() and env.mailer.sent == []


def test_uncertain_verification_never_issues_a_session(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE).json()
    env.supabase.verify_script.append(ProviderUncertain("transport"))
    response = browser.verify(start, env.mailer.last_code(ALICE))
    assert (response.status_code, response.json()) == (503, {"error": "verification_uncertain"})
    response = browser.verify(start, env.mailer.last_code(ALICE))
    assert (response.status_code, response.json()) == (503, {"error": "verification_uncertain"})
    assert env.count("oryx_sessions") == 0 and browser.actives() == {}
    assert len(env.supabase.verify_calls) == 1


# --------------------------------------------------------------------------
# Reprise canonique après perte de réponse
# --------------------------------------------------------------------------

def test_lost_verify_response_is_resumed_exactly_without_provider_call(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE).json()
    lost = browser.verify(start, env.mailer.last_code(ALICE), store=False)  # réponse perdue
    (lost_cookie,) = active_cookies_set(lost)
    assert browser.actives() == {} and browser.candidates()
    calls = (len(env.supabase.generate_calls), len(env.supabase.verify_calls), len(env.mailer.sent))

    resumed = browser.resume(start)
    assert resumed.status_code == 200 and resumed.json()["status"] == "resumed"
    assert resumed.json()["generation_id"] == lost.json()["generation_id"]
    (cookie,) = active_cookies_set(resumed)
    assert (cookie["name"], cookie["value"]) == (lost_cookie["name"], lost_cookie["value"])
    assert int(cookie["attrs"]["max-age"]) <= int(lost_cookie["attrs"]["max-age"]) - 300
    assert (len(env.supabase.generate_calls), len(env.supabase.verify_calls), len(env.mailer.sent)) == calls
    assert env.count("oryx_sessions") == 1 and env.count("oryx_session_generations") == 1
    assert browser.candidates() and candidate_name(start["candidate_id"]) not in [
        c["name"] for c in set_cookies(resumed)]  # candidat ni effacé ni renouvelé
    # Une reprise ne sélectionne rien : la session n'est utilisée que sur
    # désignation explicite de sa génération.
    assert browser.resolve(resumed.json()["generation_id"]).json()["status"] == "active"


def test_late_duplicate_verify_does_not_create_a_second_session(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE).json()
    code = env.mailer.last_code(ALICE)
    tab_b = Browser(browser.app)
    tab_b.jar, tab_b.csrf = dict(browser.jar), browser.csrf  # second onglet, mêmes cookies
    first = browser.verify(start, code)
    assert first.json()["status"] == "authenticated"
    late = tab_b.verify(start, code)
    assert (late.status_code, late.json()) == (409, {"error": "operation_unavailable"})
    assert active_cookies_set(late) == []
    resumed = tab_b.resume(start)
    assert resumed.json()["generation_id"] == first.json()["generation_id"]
    assert env.count("oryx_sessions") == 1


def test_resume_window_boundaries(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app(fixed_clock=True))
    start = browser.start(ALICE).json()
    browser.verify(start, env.mailer.last_code(ALICE), store=False)
    committed = rows(env.observer, "SELECT committed_at FROM auth_operation_results")[0]["committed_at"]
    env.clock.now = committed + timedelta(minutes=5) - US
    assert browser.resume(start, store=False).json()["status"] == "resumed"
    env.clock.now = committed + timedelta(minutes=5)
    response = browser.resume(start)
    assert (response.status_code, response.json()) == (409, {"error": "resume_unavailable"})
    assert active_cookies_set(response) == [] and browser.candidates()


def test_candidate_and_operation_expiry_boundaries(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    app = env.app(fixed_clock=True)
    created = env.clock.now
    browser = env.browser(app)
    start = browser.start(ALICE).json()
    code = env.mailer.last_code(ALICE)
    env.clock.now = created + timedelta(minutes=10)
    response = browser.verify(start, code)
    assert (response.status_code, response.json()) == (409, {"error": "operation_unavailable"})
    assert env.supabase.verify_calls == []
    # Juste avant l'échéance : admis (nouvelle opération, même frontière).
    env.clock.now = created + timedelta(minutes=20)
    start = browser.start(ALICE).json()
    env.clock.advance(minutes=10, microseconds=-1)
    assert browser.verify(start, env.mailer.last_code(ALICE)).json()["status"] == "authenticated"


def test_resume_is_refused_for_a_revoked_session(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE).json()
    lost = browser.verify(start, env.mailer.last_code(ALICE), store=False)
    other_device = env.browser(browser.app)
    (cookie,) = active_cookies_set(lost)
    other_device.jar[cookie["name"]] = cookie["value"]
    assert other_device.logout(lost.json()["generation_id"]).json() == {"status": "logged_out"}
    response = browser.resume(start)
    assert (response.status_code, response.json()) == (409, {"error": "resume_unavailable"})


# --------------------------------------------------------------------------
# Plusieurs comptes, plusieurs appareils, révocation ciblée
# --------------------------------------------------------------------------

def test_two_accounts_in_one_browser_are_never_selected_implicitly(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE, "user-alice")
    create_profile_with_identity(env.engine, env.supabase, BOB, "user-bob")
    browser = env.browser(env.app())
    _, alice = sign_in(env, browser, ALICE)
    _, bob = sign_in(env, browser, BOB)
    assert len(browser.actives()) == 2
    users = {r["id"]: r["user_id"] for r in rows(env.observer, "SELECT g.id, s.user_id FROM oryx_session_generations g "
                                                                "JOIN oryx_sessions s ON s.id = g.session_id")}
    assert {users[uuid.UUID(alice["generation_id"])], users[uuid.UUID(bob["generation_id"])]} == {
        "user-alice", "user-bob"}
    # Aucun sélecteur : refus, jamais « le premier » ni « le dernier » cookie.
    response = browser.post("/session/resolve", {})
    assert (response.status_code, response.json()) == (400, {"error": "invalid_request"})
    assert browser.logout(alice["generation_id"]).json() == {"status": "logged_out"}
    assert list(browser.actives()) == [active_name(bob["generation_id"])]
    response = browser.resolve(alice["generation_id"])
    assert (response.status_code, response.json()) == (401, {"error": "session_invalid"})
    assert browser.resolve(bob["generation_id"]).json()["generation_id"] == bob["generation_id"]


def test_multi_device_logout_revokes_only_that_device(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    app = env.app(fixed_clock=True)
    phone, laptop = env.browser(app), env.browser(app)
    _, on_phone = sign_in(env, phone)
    env.clock.advance(seconds=61)
    _, on_laptop = sign_in(env, laptop)
    assert on_phone["generation_id"] != on_laptop["generation_id"]
    assert laptop.logout(on_laptop["generation_id"]).json() == {"status": "logged_out"}
    assert laptop.actives() == {}
    assert phone.resolve(on_phone["generation_id"]).json()["status"] == "active"
    statuses = {r["status"] for r in rows(env.observer, "SELECT status FROM oryx_sessions")}
    assert statuses == {"active", "revoked"}
    # Le portable ne peut pas révoquer la session du téléphone : il n'en a
    # pas le secret ; un identifiant seul n'est jamais une preuve.
    response = laptop.logout(on_phone["generation_id"])
    assert (response.status_code, response.json()) == (401, {"error": "session_invalid"})
    laptop.jar[active_name(on_phone["generation_id"])] = "A" * 43  # sélecteur exact, secret inconnu
    response = laptop.logout(on_phone["generation_id"])
    assert (response.status_code, response.json()) == (401, {"error": "session_invalid"})
    assert phone.resolve(on_phone["generation_id"]).json()["status"] == "active"


def test_forged_active_cookie_is_cleared_without_revocation(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    _, result = sign_in(env, browser)
    name = active_name(result["generation_id"])
    real = browser.jar[name]
    browser.jar[name] = ("B" if real[0] != "B" else "C") + real[1:]
    response = browser.resolve(result["generation_id"])
    assert (response.status_code, response.json()) == (401, {"error": "session_invalid"})
    assert name not in browser.jar
    assert rows(env.observer, "SELECT status FROM oryx_sessions")[0]["status"] == "active"
    browser.jar[name] = real
    assert browser.resolve(result["generation_id"]).json()["status"] == "active"


def test_a_candidate_cannot_be_used_for_another_operation(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    app = env.app()
    alice_browser = env.browser(app)
    alice = alice_browser.start(ALICE).json()
    env.ip.ip = "203.0.113.9"
    bob_browser = env.browser(app)
    bob = bob_browser.start(BOB).json()
    bob_browser.jar[candidate_name(alice["candidate_id"])] = "A" * 43  # sélecteur sans secret valide
    mixed = {"operation_id": alice["operation_id"], "candidate_id": bob["candidate_id"]}
    response = bob_browser.verify(mixed, env.mailer.last_code(ALICE))
    assert (response.status_code, response.json()) == (409, {"error": "operation_unavailable"})
    response = bob_browser.verify(alice, env.mailer.last_code(ALICE))
    assert (response.status_code, response.json()) == (409, {"error": "operation_unavailable"})
    assert env.count("oryx_sessions") == 0 and env.supabase.verify_calls == []


def test_session_expiry_clears_the_cookie(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app(fixed_clock=True))
    _, result = sign_in(env, browser)
    expires = rows(env.observer, "SELECT absolute_expires_at FROM oryx_sessions")[0]["absolute_expires_at"]
    env.clock.now = expires - US
    assert browser.resolve(result["generation_id"]).json()["status"] == "active"
    env.clock.now = expires
    response = browser.resolve(result["generation_id"])
    assert (response.status_code, response.json()) == (401, {"error": "session_invalid"})
    assert browser.actives() == {}


# --------------------------------------------------------------------------
# Concurrence
# --------------------------------------------------------------------------

def test_concurrent_verification_of_the_same_code_issues_one_session(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE).json()
    code = env.mailer.last_code(ALICE)
    tab_b = Browser(browser.app)
    tab_b.jar, tab_b.csrf = dict(browser.jar), browser.csrf
    seen = {}

    def second_tab_while_first_is_at_the_provider():
        env.hooks.pop("verify.reserved")
        seen["b"] = tab_b.verify(start, code)

    env.hooks["verify.reserved"] = second_tab_while_first_is_at_the_provider
    first = browser.verify(start, code)
    assert first.json()["status"] == "authenticated"
    assert (seen["b"].status_code, seen["b"].json()) == (409, {"error": "verification_in_progress"})
    assert active_cookies_set(seen["b"]) == []
    assert env.count("oryx_sessions") == 1 and len(env.supabase.verify_calls) == 1


def test_parallel_requests_never_produce_two_sessions(env):
    # Doubles sans sonde de pool : les requêtes parallèles tiennent
    # légitimement des connexions pendant l'appel fournisseur des autres.
    supabase, mailer = FakeSupabase(), FakeMailer()
    create_profile_with_identity(env.engine, supabase, ALICE)
    browser = env.browser(env.app(supabase=supabase, mailer=mailer))
    start = browser.start(ALICE).json()
    code = mailer.last_code(ALICE)
    headers = {"origin": ORIGIN, "x-oryx-csrf": browser.csrf, "cookie": browser.cookie_header(),
               "content-type": "application/json"}
    payload = json.dumps({"operation_id": start["operation_id"], "candidate_id": start["candidate_id"],
                          "code": code}).encode()
    barrier = threading.Barrier(4)

    def one():
        async def go():
            transport = httpx.ASGITransport(app=browser.app, client=("192.0.2.1", 1))
            async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
                barrier.wait()
                return await client.post(F2 + "/sign-in/verify", headers=headers, content=payload)
        return asyncio.run(go())

    results = []
    threads = [threading.Thread(target=lambda: results.append(one())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    generations = {r.json()["generation_id"] for r in results if r.status_code == 200}
    assert len(generations) == 1, [r.text for r in results]
    assert env.count("oryx_sessions") == 1
    for r in results:
        assert r.status_code == 200 or r.json()["error"] in ("verification_in_progress", "operation_unavailable")
        if r.status_code != 200:
            assert active_cookies_set(r) == []


# --------------------------------------------------------------------------
# Composition : rien n'est fait à la construction ; état anti-abus explicite
# --------------------------------------------------------------------------

def test_building_the_isolated_app_touches_nothing(env):
    env.app(init_abuse=False)
    for table in F2_TABLES:
        assert env.count(table) == 0, table
    assert env.supabase.generate_calls == [] and env.mailer.sent == []
    busy = scalar(env.observer, "SELECT count(*) FROM pg_stat_activity WHERE application_name = :a", a=APP_NAME)
    assert busy == 0


def test_abuse_key_state_is_never_initialized_implicitly(env):
    browser = env.browser(env.app(init_abuse=False))
    response = browser.start(ALICE)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert env.count("auth_abuse_key_state") == 0 and env.count("auth_operations") == 0
    assert env.supabase.generate_calls == [] and set_cookies(response) == []


def test_composition_validation(env):
    base = dict(engine=env.engine, keyring=env.keyring, supabase=env.supabase, mailer=env.mailer,
                csrf_keyring=csrf_keyring(), ip_resolver=StaticIp(), allowed_origins=[ORIGIN])
    from core.auth_f2_http.csrf import CsrfKeyring
    clashing = CsrfKeyring({"c1": env.keyring.key("otp", "t1")}, "c1")
    broken = [
        {"engine": sa.create_engine("sqlite://")}, {"engine": None}, {"keyring": None},
        {"csrf_keyring": clashing}, {"csrf_keyring": None}, {"ip_resolver": None}, {"allowed_origins": []},
        {"allowed_origins": ["http://oryx.test"]}, {"allowed_origins": ORIGIN}, {"supabase": object()},
        {"mailer": object()},
    ]
    for override in broken:
        with pytest.raises(F2HttpConfigError):
            build_isolated_f2_app(**{**base, **override})


# --------------------------------------------------------------------------
# Adaptateurs HTTP réels (PR 2) sur transport simulé
# --------------------------------------------------------------------------

def test_real_provider_adapters_through_mock_transports(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    subject = env.supabase.subject_for(ALICE)
    seen = []

    def supabase_handler(request):
        seen.append(("supabase", request.url.host, request.url.path))
        body = json.loads(request.content)
        if request.url.path == "/auth/v1/admin/generate_link":
            return httpx.Response(200, json={"email_otp": "04815162", "email": body["email"]})
        if body["token"] == "04815162":
            return httpx.Response(200, json={"access_token": "tok", "user": {
                "id": subject, "aud": "authenticated", "email": ALICE, "email_confirmed_at": "2026-10-23T00:00:00Z"}})
        return httpx.Response(403, json={"error_code": "otp_expired"})

    def resend_handler(request):
        seen.append(("resend", request.url.host, request.url.path))
        return httpx.Response(200, json={"id": "email-1"})

    supabase = HttpSupabaseOtpProvider(base_url="https://oryx-auth-fake.supabase.co", service_key=Redacted("svc"),
                                       api_key=Redacted("anon"), transport=httpx.MockTransport(supabase_handler))
    mailer = HttpResendMailer(api_key=Redacted("re_test"), sender="Oryx <noreply@oryx.test>",
                              transport=httpx.MockTransport(resend_handler))
    browser = env.browser(env.app(supabase=supabase, mailer=mailer))
    start = browser.start(ALICE)
    assert start.json()["status"] == "code_sent"
    verify = browser.verify(start.json(), "04815162")
    assert verify.json()["status"] == "authenticated"
    assert seen == [("supabase", "oryx-auth-fake.supabase.co", "/auth/v1/admin/generate_link"),
                    ("resend", "api.resend.com", "/emails"), ("supabase", "oryx-auth-fake.supabase.co",
                                                               "/auth/v1/verify")]
    for response in (start, verify):
        no_secret_in(response, "04815162", "tok", subject, "svc", "re_test")



# --------------------------------------------------------------------------
# Correctif d'audit P1.1 : le cookie candidat survit au succès
# --------------------------------------------------------------------------

def _candidate_cookie(response):
    return [c for c in set_cookies(response) if c["name"].startswith("__Host-oryx-candidate-")]


def test_successful_verification_keeps_the_candidate_cookie_unchanged(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE)
    (issued,) = _candidate_cookie(start)
    verify = browser.verify(start.json(), env.mailer.last_code(ALICE))
    assert verify.json()["status"] == "authenticated"
    assert _candidate_cookie(verify) == []  # ni effacé, ni renouvelé
    assert browser.candidates() == {issued["name"]: issued["value"]}


def test_partially_used_success_response_can_still_be_resumed(env):
    """Réponse reçue mais exploitée partiellement (cookie actif perdu par le
    navigateur ou le frontend) : la reprise canonique rend exactement la
    même session, sans appel fournisseur."""
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE).json()
    verify = browser.verify(start, env.mailer.last_code(ALICE))
    (active,) = active_cookies_set(verify)
    del browser.jar[active["name"]]
    calls = (len(env.supabase.generate_calls), len(env.supabase.verify_calls), len(env.mailer.sent))
    resumed = browser.resume(start)
    assert resumed.json()["status"] == "resumed"
    assert resumed.json()["generation_id"] == verify.json()["generation_id"]
    assert browser.jar[active["name"]] == active["value"]
    assert (len(env.supabase.generate_calls), len(env.supabase.verify_calls), len(env.mailer.sent)) == calls
    assert env.count("oryx_sessions") == 1


def test_successful_resume_keeps_the_candidate_and_repeats_exactly(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app())
    start = browser.start(ALICE).json()
    lost = browser.verify(start, env.mailer.last_code(ALICE), store=False)
    (expected,) = active_cookies_set(lost)
    calls = (len(env.supabase.generate_calls), len(env.supabase.verify_calls), len(env.mailer.sent))
    first = browser.resume(start, store=False)  # cette réponse-là aussi est perdue
    second = browser.resume(start)
    for response in (first, second):
        assert response.json()["status"] == "resumed"
        (cookie,) = active_cookies_set(response)
        assert (cookie["name"], cookie["value"]) == (expected["name"], expected["value"])
        assert _candidate_cookie(response) == []
    assert browser.candidates()
    assert (len(env.supabase.generate_calls), len(env.supabase.verify_calls), len(env.mailer.sent)) == calls
    assert env.count("oryx_sessions") == 1 and env.count("oryx_session_generations") == 1


def test_expired_resume_window_grants_nothing_more(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app(fixed_clock=True))
    start = browser.start(ALICE).json()
    code = env.mailer.last_code(ALICE)
    assert browser.verify(start, code).json()["status"] == "authenticated"
    committed = rows(env.observer, "SELECT committed_at FROM auth_operation_results")[0]["committed_at"]
    env.clock.now = committed + timedelta(minutes=5)
    calls = (len(env.supabase.generate_calls), len(env.supabase.verify_calls), len(env.mailer.sent))
    assert browser.candidates()  # le cookie est encore là, mais ne vaut plus rien
    for response in (browser.resume(start), browser.verify(start, code), browser.resend(start)):
        assert response.status_code == 409, response.text
        assert active_cookies_set(response) == [] and _candidate_cookie(response) == []
    assert (len(env.supabase.generate_calls), len(env.supabase.verify_calls), len(env.mailer.sent)) == calls
    assert env.count("oryx_sessions") == 1 and env.count("oryx_session_generations") == 1


def test_candidate_cookie_is_set_once_and_never_extended(env):
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    browser = env.browser(env.app(fixed_clock=True))
    start = browser.start(ALICE)
    body = start.json()
    env.clock.advance(seconds=61)
    responses = [start, browser.resend(body)]
    responses.append(browser.verify(body, env.mailer.last_code(ALICE)))
    generation = responses[-1].json()["generation_id"]
    responses += [browser.resume(body), browser.resolve(generation), browser.bootstrap(), browser.logout(generation)]
    issued = [c for r in responses for c in _candidate_cookie(r)]
    assert len(issued) == 1 and issued[0]["name"] == candidate_name(body["candidate_id"])
    assert int(issued[0]["attrs"]["max-age"]) <= 900
    # Après déconnexion, la reprise est refusée (session révoquée) malgré le cookie.
    response = browser.resume(body)
    assert (response.status_code, response.json()) == (409, {"error": "resume_unavailable"})


# --------------------------------------------------------------------------
# Correctif d'audit P1.2 : réponses comparées, identité liée ou non
# --------------------------------------------------------------------------

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _shape(response):
    """Statut, corps (UUID masqués), taille, en-têtes pertinents, cookies
    (noms sans identifiant, attributs) : ce qu'un observateur compare."""
    headers = {k: v for k, v in response.headers.items() if k not in ("date", "set-cookie")}
    cookies = sorted((UUID_RE.sub("<id>", c["name"]), tuple(sorted(c["attrs"].items()))) for c in set_cookies(response))
    return response.status_code, UUID_RE.sub("<id>", response.text), len(response.content), headers, cookies


def _operation(env, operation_id):
    op = rows(env.observer, "SELECT status FROM auth_operations WHERE id = :i", i=operation_id)[0]
    request = rows(env.observer, "SELECT status, failure_count FROM auth_otp_requests WHERE auth_operation_id = :i "
                                 "ORDER BY code_generation DESC LIMIT 1", i=operation_id)[0]
    return op["status"], request["status"], request["failure_count"]


@pytest.fixture
def pair(env):
    """Deux navigateurs, deux adresses : l'une liée à un profil Oryx, l'autre
    non. Même IP de test : seuls l'adresse et l'état d'identité diffèrent."""
    create_profile_with_identity(env.engine, env.supabase, ALICE)
    app = env.app(fixed_clock=True)
    linked, unknown = env.browser(app), env.browser(app)
    return env, {"linked": (linked, ALICE), "unknown": (unknown, NOBODY)}


def _start_both(pair):
    env, browsers = pair
    starts = {k: b.start(email) for k, (b, email) in browsers.items()}
    assert _shape(starts["linked"]) == _shape(starts["unknown"])  # démarrage : identique
    return {k: r.json() for k, r in starts.items()}


@pytest.mark.parametrize("code", ["wrong", "abc", "1" * 100, ""])
def test_wrong_or_malformed_code_does_not_depend_on_identity(pair, code):
    env, browsers = pair
    starts = _start_both(pair)
    responses, states = {}, {}
    for key, (browser, email) in browsers.items():
        good = env.mailer.last_code(email)
        submitted = f"{(int(good) + 1) % 100000000:08d}" if code == "wrong" else code
        responses[key] = browser.verify(starts[key], submitted)
        states[key] = _operation(env, starts[key]["operation_id"])
    assert _shape(responses["linked"]) == _shape(responses["unknown"])
    assert (responses["linked"].status_code, responses["linked"].json()) == (401, {"error": "code_incorrect"})
    assert states["linked"] == states["unknown"] == ("pending", "issued", 1)  # compteur PR 2 conservé
    env.clock.advance(seconds=61)
    resends = {k: b.resend(starts[k]) for k, (b, _) in browsers.items()}
    assert _shape(resends["linked"]) == _shape(resends["unknown"])
    assert resends["linked"].status_code == 202
    assert env.supabase.verify_calls == []


def test_uncertain_provider_answer_does_not_depend_on_identity(pair):
    env, browsers = pair
    starts = _start_both(pair)
    responses = {}
    for key, (browser, email) in browsers.items():
        env.supabase.verify_script.append(ProviderUncertain("transport"))
        responses[key] = browser.verify(starts[key], env.mailer.last_code(email))
    assert _shape(responses["linked"]) == _shape(responses["unknown"])
    assert (responses["linked"].status_code, responses["linked"].json()) == (503, {"error": "verification_uncertain"})
    assert {_operation(env, s["operation_id"])[:2] for s in starts.values()} == {("pending", "verify_uncertain")}
    assert env.count("oryx_sessions") == 0


@pytest.mark.parametrize("answer", ["provider_rejected", "proof_not_applicable"])
def test_certain_provider_refusals_do_not_depend_on_identity(pair, answer):
    env, browsers = pair
    starts = _start_both(pair)
    responses = {}
    for key, (browser, email) in browsers.items():
        env.supabase.verify_script.append(
            ProviderRejected("otp_expired") if answer == "provider_rejected"
            else VerifiedIdentity(subject=env.supabase.subject_for("other@example.com"), email="other@example.com"))
        responses[key] = browser.verify(starts[key], env.mailer.last_code(email))
    assert _shape(responses["linked"]) == _shape(responses["unknown"])
    assert (responses["linked"].status_code, responses["linked"].json()) == (401, {"error": "sign_in_failed"})
    assert {_operation(env, s["operation_id"])[:2] for s in starts.values()} == {("pending", "expired")}
    assert env.count("oryx_sessions") == 0


def test_correct_code_without_identity_matches_a_provider_refusal_then_diverges(pair):
    """Code correct : identité liée -> session ; identité inconnue -> même
    réponse publique qu'un refus fournisseur certain. Divergence ULTÉRIEURE
    héritée de PR 2 (moteur non modifié, arbitrage au §10 de
    docs/f2_r1_pr3.md) : l'opération sans identité est close, un renvoi est
    refusé ; après un refus fournisseur, elle reste ouverte."""
    env, browsers = pair
    starts = _start_both(pair)
    linked, _ = browsers["linked"]
    unknown, _ = browsers["unknown"]
    no_identity = unknown.verify(starts["unknown"], env.mailer.last_code(NOBODY))
    env.supabase.verify_script.append(ProviderRejected("otp_expired"))
    rejected = linked.verify(starts["linked"], env.mailer.last_code(ALICE))
    assert _shape(no_identity) == _shape(rejected)
    assert (no_identity.status_code, no_identity.json()) == (401, {"error": "sign_in_failed"})
    assert env.count("oryx_sessions") == 0 and env.count("users") == 1  # aucun profil implicite
    assert _operation(env, starts["unknown"]["operation_id"])[0] == "failed"
    assert _operation(env, starts["linked"]["operation_id"])[0] == "pending"
    env.clock.advance(seconds=61)
    after_no_identity = unknown.resend(starts["unknown"])
    after_rejected = linked.resend(starts["linked"])
    assert (after_no_identity.status_code, after_no_identity.json()) == (409, {"error": "operation_unavailable"})
    assert after_rejected.status_code == 202
    # Code correct et identité liée : seul cas qui délivre une session.
    ok = linked.verify(starts["linked"], env.mailer.last_code(ALICE))
    assert ok.json()["status"] == "authenticated" and len(active_cookies_set(ok)) == 1
