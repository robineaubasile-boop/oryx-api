"""Admission HTTP de R1 PR 4 (C.4.5 §2.2, §2.3, T3, T4) contre un vrai
PostgreSQL jetable.

- Budgets commun (60 / 5 min) et propres à une action (begin 20 / 1 h ;
  complete, resume, result 30 / 5 min) : N admis, N+1 refusé ; fenêtres
  glissantes à la microseconde (horloge contrôlée) ; IP indépendantes ;
  IPv6 comptée par /64 ; rotation de K_abuse sans remise à zéro.
- Une admission = exactement un probe ; un refus = aucune ligne.
- Par les routes : opérations et invitations inconnues comptées, refus
  anticipés du moteur OTP comptés, refus métier après admission comptés,
  refus d'admission sans moteur.
- Pannes : échec avant COMMIT (aucun probe, aucun moteur), panne après
  COMMIT ou COMMIT incertain (probe conservé, aucun moteur, aucune
  annulation), délai de verrou, état de clé absent ou incompatible.
"""
import threading
import time
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.abuse import KEY_STATE_LOCK_KEY, AbuseUnavailable
from core.auth_f2.http_admission import AdmissionUnavailable, HttpAdmission
from core.auth_f2.policy import HTTP_ACTION_BUDGETS, HTTP_ACTIONS, HTTP_COMMON_LIMIT, HTTP_POLICY_VERSION
from core.auth_f2_http.cookies import active_name, candidate_name
from tests.f2_pr4_support import (
    ALICE,
    IP_A,
    IP_B,
    Pr4Env,
    activate,
    begin,
    complete,
    link_result,
    link_start,
    link_verify,
)
from tests.f2_support import FixedClock, SimulatedCrash, make_keyring, prepare_schema
from tests.test_f2_http_contract import secret
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

US = timedelta(microseconds=1)


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = Pr4Env(pg_url)
    yield environment
    environment.dispose()


def admission(env, *, clock=None, keyring=None, hook=None):
    kwargs = {"fault_hook": hook or env.hook}
    if clock is not None:
        kwargs["clock"] = clock
    return HttpAdmission(env.engine, keyring or env.keyring, **kwargs)


# --------------------------------------------------------------------------
# Budgets et fenêtres
# --------------------------------------------------------------------------

def test_common_budget_admits_sixty_then_refuses(env):
    env.init_abuse()
    gate = admission(env)
    # Actions sans budget propre : seul le budget commun s'applique.
    actions = ["email_link_start", "email_link_resend", "email_link_verify"]
    results = [gate.admit(actions[i % 3], IP_A) for i in range(HTTP_COMMON_LIMIT)]
    assert results == [True] * HTTP_COMMON_LIMIT
    assert env.probes() == HTTP_COMMON_LIMIT
    for action in HTTP_ACTIONS:
        assert gate.admit(action, IP_A) is False
    assert env.probes() == HTTP_COMMON_LIMIT  # un refus n'écrit rien
    assert env.count("auth_abuse_events", "event_kind <> 'auth_http_probe_attempt'") == 0
    assert gate.admit("email_link_start", IP_B) is True


@pytest.mark.parametrize("action", sorted(HTTP_ACTION_BUDGETS))
def test_action_budget_admits_n_then_refuses_n_plus_one(env, action):
    env.init_abuse()
    gate = admission(env)
    limit, _ = HTTP_ACTION_BUDGETS[action]
    assert [gate.admit(action, IP_A) for _ in range(limit)] == [True] * limit
    assert gate.admit(action, IP_A) is False
    assert env.probes(action) == limit
    # Le budget commun n'est pas épuisé : une autre action passe encore.
    other = "email_link_verify"
    assert gate.admit(other, IP_A) is True
    assert env.probes() == limit + 1


def test_actions_without_own_budget_only_use_the_common_one(env):
    env.init_abuse()
    gate = admission(env)
    assert [gate.admit("email_link_verify", IP_A) for _ in range(HTTP_COMMON_LIMIT)] == [True] * HTTP_COMMON_LIMIT
    assert gate.admit("email_link_verify", IP_A) is False


def test_common_budget_counts_every_action_and_specific_ones_filter(env):
    env.init_abuse()
    gate = admission(env)
    for _ in range(20):
        assert gate.admit("invitation_begin", IP_A)
    for _ in range(30):
        assert gate.admit("invitation_complete", IP_A)
    assert gate.admit("invitation_begin", IP_A) is False  # 20 / h
    assert gate.admit("invitation_complete", IP_A) is False  # 30 / 5 min
    for _ in range(10):
        assert gate.admit("invitation_resume", IP_A)
    assert gate.admit("email_link_result", IP_A) is False  # commun : 60
    rows = env.rows("SELECT http_action, policy_version, auth_operation_id, otp_request_id, blocked_until "
                    "FROM auth_abuse_events")
    assert len(rows) == 60
    assert {(r["policy_version"], r["auth_operation_id"], r["otp_request_id"], r["blocked_until"]) for r in rows} == {
        (HTTP_POLICY_VERSION, None, None, None)}


def test_sliding_windows_to_the_microsecond(env):
    env.init_abuse()
    clock = FixedClock(env.observer)
    gate = admission(env, clock=clock)
    t0 = clock.now
    for _ in range(30):
        assert gate.admit("email_link_result", IP_A)
    assert gate.admit("email_link_result", IP_A) is False
    clock.now = t0 + timedelta(minutes=5) - US
    assert gate.admit("email_link_result", IP_A) is False
    clock.now = t0 + timedelta(minutes=5)  # ]t - 5 min, t] : les 30 sortent
    assert gate.admit("email_link_result", IP_A) is True
    # invitation_begin : 1 h.
    t1 = clock.advance(minutes=1)
    for _ in range(20):
        assert gate.admit("invitation_begin", IP_B)
    clock.now = t1 + timedelta(hours=1) - US
    assert gate.admit("invitation_begin", IP_B) is False
    clock.now = t1 + timedelta(hours=1)
    assert gate.admit("invitation_begin", IP_B) is True


def test_ips_are_counted_separately_and_ipv6_per_64(env):
    env.init_abuse()
    gate = admission(env)
    for _ in range(30):
        assert gate.admit("invitation_resume", "2001:db8:1:2::1")
    assert gate.admit("invitation_resume", "2001:db8:1:2:ffff::9") is False  # même /64
    assert gate.admit("invitation_resume", "2001:db8:1:3::1") is True       # autre /64
    for _ in range(30):
        assert gate.admit("invitation_resume", "::ffff:198.51.100.7")
    assert gate.admit("invitation_resume", "198.51.100.7") is False  # IPv4 mappée = même sujet
    subjects = env.rows("SELECT DISTINCT s.scope_kind FROM auth_abuse_subjects s JOIN auth_abuse_events e "
                        "ON e.subject_id = s.id")
    assert [r["scope_kind"] for r in subjects] == ["ip"]


def test_rotation_never_resets_the_budgets(env):
    both = make_keyring({**{p: ("t1",) for p in ("candidate", "derive", "active", "otp", "invitation")},
                         "abuse": ("v1", "v2")}, {**{p: "t1" for p in ("candidate", "derive", "active", "otp",
                                                                        "invitation")}, "abuse": "v1"})
    abuse.initialize_abuse_key_state(env.engine, both, "v1")
    gate = admission(env, keyring=both)
    for _ in range(29):
        assert gate.admit("invitation_complete", IP_A)
    abuse.rotate_abuse_key(env.engine, both, "v2")
    assert gate.admit("invitation_complete", IP_A) is True  # 30e, enregistrée sous v2
    assert gate.admit("invitation_complete", IP_A) is False  # v1 + v2 comptées
    versions = env.rows("SELECT s.hash_key_version AS v, count(*) AS n FROM auth_abuse_events e JOIN "
                        "auth_abuse_subjects s ON s.id = e.subject_id GROUP BY 1 ORDER BY 1")
    assert [(r["v"], r["n"]) for r in versions] == [("v1", 29), ("v2", 1)]


def test_unknown_action_and_unusable_ip_are_refused_without_writing(env):
    env.init_abuse()
    gate = admission(env)
    with pytest.raises(ValueError):
        gate.admit("sign_in_start", IP_A)
    for ip in ("", "not-an-ip", " 198.51.100.1", "fe80::1%eth0", None):
        with pytest.raises(AdmissionUnavailable):
            gate.admit("invitation_begin", ip)
    assert env.count("auth_abuse_events") == 0 and env.count("auth_abuse_subjects") == 0


# --------------------------------------------------------------------------
# Pannes de l'admission (T3, T4)
# --------------------------------------------------------------------------

def test_failure_before_commit_leaves_no_probe(env):
    env.init_abuse()
    env.hooks["admission.before_commit"] = lambda: (_ for _ in ()).throw(RuntimeError("insert lost"))
    with pytest.raises(RuntimeError):
        admission(env).admit("invitation_begin", IP_A)
    assert env.probes() == 0


def test_failure_after_commit_keeps_the_probe(env):
    env.init_abuse()
    env.hooks["admission.committed"] = lambda: (_ for _ in ()).throw(SimulatedCrash())
    with pytest.raises(SimulatedCrash):
        admission(env).admit("invitation_begin", IP_A)
    assert env.probes("invitation_begin") == 1


def test_missing_or_incompatible_key_state_refuses(env):
    with pytest.raises(AbuseUnavailable):
        admission(env).admit("invitation_begin", IP_A)  # état de clé non initialisé
    env.init_abuse()
    other = make_keyring({**{p: ("t1",) for p in ("candidate", "derive", "active", "otp", "invitation")},
                          "abuse": ("zz",)})
    with pytest.raises(AbuseUnavailable):
        admission(env, keyring=other).admit("invitation_begin", IP_A)
    assert env.probes() == 0


def test_lock_timeout_refuses_without_probe(env):
    env.init_abuse()
    holder = env.observer.connect()
    trans = holder.begin()
    holder.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": KEY_STATE_LOCK_KEY})  # rotation en cours
    try:
        started = time.monotonic()
        with pytest.raises(sa.exc.OperationalError):
            admission(env).admit("invitation_begin", IP_A)
        assert time.monotonic() - started < 9
    finally:
        trans.rollback()
        holder.close()
    assert env.probes() == 0


# --------------------------------------------------------------------------
# Par les routes : admission avant le moteur, toujours comptée
# --------------------------------------------------------------------------

def test_unknown_invitation_and_operations_consume_probes(env):
    browser = env.browser(env.app())
    response = begin(browser, secret(3).reveal())
    assert (response.status_code, response.json()) == (409, {"error": "invitation_unavailable"})
    fake = {"operation_id": str(uuid.uuid4()), "candidate_id": str(uuid.uuid4())}
    browser.jar[candidate_name(uuid.UUID(fake["candidate_id"]))] = secret(4).reveal()
    assert complete(browser, fake).json() == {"error": "activation_unavailable"}
    assert browser.post("/invitation/resume", fake).json() == {"error": "resume_unavailable"}
    generation = uuid.uuid4()
    browser.jar[active_name(generation)] = secret(5).reveal()
    assert link_result(browser, str(generation), fake["operation_id"]).json() == {"error": "session_invalid"}
    assert {a: env.probes(a) for a in ("invitation_begin", "invitation_complete", "invitation_resume",
                                       "email_link_result")} == {
        "invitation_begin": 1, "invitation_complete": 1, "invitation_resume": 1, "email_link_result": 1}
    assert env.count("auth_operations") == 0 and env.resume_attempts() == 0


def test_otp_early_exits_are_admitted_and_counted(env):
    browser = env.browser(env.app())
    _, done = activate(env, browser)
    generation = done["generation_id"]
    # Refus anticipés du moteur OTP : e-mail invalide, opération inconnue.
    assert link_start(browser, generation, "not-an-email").json() == {"error": "invalid_email"}
    assert link_verify(browser, generation, str(uuid.uuid4()), "12345678").json() == {
        "error": "operation_unavailable"}
    assert browser.post("/email-link/resend", {"generation_id": generation, "operation_id": str(uuid.uuid4())}
                        ).json() == {"error": "operation_unavailable"}
    assert (env.probes("email_link_start"), env.probes("email_link_verify"), env.probes("email_link_resend")) == (
        1, 1, 1)
    # Le moteur OTP garde en plus ses propres quotas (PR 2).
    assert env.count("auth_abuse_events", "event_kind = 'otp_verification_attempt'") == 1


def test_refused_admission_never_reaches_the_engine(env):
    app = env.app()
    browser = env.browser(app)
    token = env.invitation()
    gate = admission(env)
    for _ in range(HTTP_COMMON_LIMIT):
        assert gate.admit("email_link_verify", IP_A)
    response = begin(browser, token)
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})
    assert response.headers.get_list("set-cookie") == []
    assert env.count("auth_operations") == 0 and env.probes() == HTTP_COMMON_LIMIT
    # Depuis une autre IP, la même invitation reste utilisable.
    other = env.browser(app, IP_B)
    assert begin(other, token).status_code == 202


def test_business_refusal_after_admission_keeps_the_probe(env):
    browser = env.browser(env.app())
    token = env.invitation()
    started, _ = activate(env, browser, token)
    again = begin(browser, token)  # invitation consommée
    assert again.json() == {"error": "invitation_unavailable"}
    assert env.probes("invitation_begin") == 2 and env.probes("invitation_complete") == 1


def test_crash_after_admission_commit_creates_nothing_else(env):
    """T3 : probe committé puis panne avant le moteur."""
    browser = env.browser(env.app())
    token = env.invitation()
    env.hooks["admission.committed"] = lambda: (_ for _ in ()).throw(RuntimeError("process lost"))
    response = begin(browser, token)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert response.headers.get_list("set-cookie") == []
    assert env.probes("invitation_begin") == 1
    assert env.count("auth_operations") == 0 and env.count("auth_preauth_candidates") == 0
    assert env.count("oryx_sessions") == 0 and env.count("auth_identities") == 0
    assert env.supabase.generate_calls == [] and env.mailer.sent == []
    # Aucune annulation rétroactive : la demande suivante est une nouvelle admission.
    env.hooks.clear()
    assert begin(browser, token).status_code == 202
    assert env.probes("invitation_begin") == 2


@pytest.mark.parametrize("point", ["admission.before_commit", "admission.state_locked"])
def test_failed_admission_never_calls_the_engine(env, point):
    """T4 : échec d'insertion ou de verrou avant COMMIT."""
    browser = env.browser(env.app())
    _, done = activate(env, browser)
    env.hooks[point] = lambda: (_ for _ in ()).throw(sa.exc.OperationalError("x", {}, Exception("lock")))
    response = link_start(browser, done["generation_id"])
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert env.probes("email_link_start") == 0
    assert env.count("auth_operations", "kind = 'email_linking'") == 0
    assert env.supabase.generate_calls == [] and env.mailer.sent == []


def test_uncertain_commit_never_calls_the_engine(env):
    """T4 : COMMIT dont l'issue n'est pas confirmée à l'appelant."""
    browser = env.browser(env.app())
    _, done = activate(env, browser)
    env.hooks["admission.committed"] = lambda: (_ for _ in ()).throw(sa.exc.OperationalError(
        "COMMIT", {}, Exception("connection lost")))
    response = link_start(browser, done["generation_id"])
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert env.count("auth_operations", "kind = 'email_linking'") == 0
    assert env.supabase.generate_calls == [] and env.mailer.sent == []


def test_key_unavailable_refuses_every_route_before_the_engine(env):
    app = env.app(init_abuse=False)
    browser = env.browser(app)
    token = env.invitation()
    response = begin(browser, token)
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert env.count("auth_operations") == 0 and env.count("auth_abuse_events") == 0


def test_rotation_lock_timeout_through_the_route(env):
    browser = env.browser(env.app())
    token = env.invitation()
    holder = env.observer.connect()
    trans = holder.begin()
    holder.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": KEY_STATE_LOCK_KEY})
    result = {}

    def call():
        result["response"] = begin(browser, token)

    thread = threading.Thread(target=call)
    thread.start()
    thread.join(timeout=30)
    trans.rollback()
    holder.close()
    assert (result["response"].status_code, result["response"].json()) == (503, {"error": "temporarily_unavailable"})
    assert env.count("auth_operations") == 0 and env.probes() == 0


def test_link_result_route_counts_probes_without_debiting_unknown_operations(env):
    browser = env.browser(env.app())
    _, done = activate(env, browser)
    for _ in range(3):
        response = link_result(browser, done["generation_id"], str(uuid.uuid4()))
        assert (response.status_code, response.json()) == (409, {"error": "result_unavailable"})
    assert env.probes("email_link_result") == 3 and env.resume_attempts() == 0
    assert ALICE not in str(env.rows("SELECT * FROM auth_abuse_events"))
