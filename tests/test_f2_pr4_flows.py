"""Parcours HTTP de R1 PR 4 contre un vrai PostgreSQL jetable : application
isolée complète (moteur F2 réel, admission HTTP réelle), doubles Supabase /
Resend sans réseau qui échouent si une transaction est ouverte pendant
leur appel.

Couvre :
- invitation : begin (A), complete (B), resume (C), consommation unique,
  profil créé une fois, aucune identité implicite, cookie candidat
  conservé sans renouvellement, aucune nouvelle session à la reprise ;
- quotas métier : ouvertures 3 / h (3 admises, 4e refusée) et 12 au total
  (12 admises, 13e refusée), sous concurrence ; reprise 10 / 5 min par
  opération (10 admises, 11e refusée, lignes durables comptées) ;
  justificatif validé AVANT tout débit ;
- reprise typée : /sign-in/resume et /invitation/resume n'acceptent que
  leur type ; l'entrée interne historique reste compatible ;
- liaison e-mail : start / resend / verify sans cookie ni session, aucune
  fusion de profils ;
- résultat de liaison (T2, T5) : session invalide -> 401 identique quelle
  que soit l'opération ; session authentique non autorisée -> 409
  identique ; révocation entre les deux phases et expiration pendant
  l'attente du verrou -> 401 ; aucun débit sans justificatif valide ;
  aucune confirmation de livraison ; quota 10 / 11.
"""
import threading
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from core.auth_f2.crypto import Redacted
from core.auth_f2.providers import ProviderRejected
from core.auth_f2.service import CandidateCredential
from core.auth_f2_http.contract_pr4 import expires_at_text
from core.auth_f2_http.cookies import active_name, candidate_name
from tests.f2_http_support import set_cookies
from tests.f2_pr4_support import (
    ALICE,
    IP_A,
    IP_B,
    Pr4Env,
    activate,
    begin,
    complete,
    link_resend,
    link_result,
    link_start,
    link_verify,
    linked,
    resume,
)
from tests.f2_support import create_profile_with_identity, prepare_schema
from tests.test_f2_http_contract import secret
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0016_portfolio_idempotency import _wait_for_lock_waiter

BOB = "bob@example.com"


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


def comparable(response):
    headers = sorted((k, v) for k, v in response.headers.items() if k != "date")
    return response.status_code, response.content, headers


def session_of(env, generation_id):
    return env.rows("SELECT s.* FROM oryx_sessions s JOIN oryx_session_generations g ON g.session_id = s.id "
                    "WHERE g.id = :g", g=generation_id)[0]


# --------------------------------------------------------------------------
# Invitation : begin, complete, resume
# --------------------------------------------------------------------------

def test_invitation_activation_end_to_end(env):
    browser = env.browser(env.app())
    token = env.invitation()
    started = begin(browser, token)
    assert started.status_code == 202
    body = started.json()
    assert set(body) == {"status", "operation_id", "candidate_id"} and body["status"] == "activation_started"
    (cand,) = set_cookies(started)
    assert cand["name"] == candidate_name(uuid.UUID(body["candidate_id"]))
    assert cand["attrs"].keys() == {"max-age", "path", "secure", "httponly", "samesite"}
    assert 899 <= int(cand["attrs"]["max-age"]) <= 900
    assert token not in started.text and cand["value"] not in started.text
    # Aucune consommation au démarrage.
    assert env.rows("SELECT status FROM invitations")[0]["status"] == "pending"

    done = complete(browser, body)
    assert done.status_code == 200
    result = done.json()
    session = session_of(env, result["generation_id"])
    assert result == {"status": "invitation_activated", "operation_id": body["operation_id"],
                      "generation_id": result["generation_id"],
                      "expires_at": expires_at_text(session["absolute_expires_at"])}
    (active,) = set_cookies(done)  # seul l'actif est posé ; candidat conservé
    assert active["name"] == active_name(uuid.UUID(result["generation_id"]))
    assert 1295990 <= int(active["attrs"]["max-age"]) <= 1296000
    assert browser.jar[cand["name"]] == cand["value"]
    assert session["auth_method"] == "invitation_activation" and session["auth_identity_id"] is None
    assert env.count("users") == 1 and env.count("oryx_sessions") == 1 and env.count("auth_identities") == 0
    assert env.rows("SELECT status FROM invitations")[0]["status"] == "accepted"
    assert env.supabase.generate_calls == [] and env.mailer.sent == []


def test_complete_is_consumed_once_and_replay_creates_nothing(env):
    browser = env.browser(env.app())
    started, done = activate(env, browser)
    again = complete(browser, started)
    assert (again.status_code, again.json()) == (409, {"error": "activation_unavailable"})
    assert set_cookies(again) == []
    assert env.count("users") == 1 and env.count("oryx_sessions") == 1
    # La reprise explicite rend la même session canonique.
    resumed = resume(browser, started)
    assert resumed.status_code == 200
    assert resumed.json()["generation_id"] == done["generation_id"]


def test_resume_returns_the_same_canonical_session_and_keeps_the_candidate(env):
    browser = env.browser(env.app())
    started, done = activate(env, browser)
    active = browser.jar[active_name(uuid.UUID(done["generation_id"]))]
    candidate_cookie = browser.jar[candidate_name(uuid.UUID(started["candidate_id"]))]
    browser.jar.pop(active_name(uuid.UUID(done["generation_id"])))  # réponse perdue
    for _ in range(2):
        response = resume(browser, started)
        assert response.status_code == 200
        assert response.json() == {"status": "resumed", "operation_id": started["operation_id"],
                                   "generation_id": done["generation_id"], "expires_at": done["expires_at"]}
        (cookie,) = set_cookies(response)
        assert cookie["value"] == active and int(cookie["attrs"]["max-age"]) <= 1295700
        assert browser.jar[candidate_name(uuid.UUID(started["candidate_id"]))] == candidate_cookie
    assert env.count("oryx_sessions") == 1 and env.count("oryx_session_generations") == 1
    assert env.resume_attempts(started["operation_id"]) == 2


def test_concurrent_completions_issue_one_session(env):
    app = env.app()
    browser = env.browser(app)
    started = begin(browser, env.invitation()).json()
    responses = []

    def go():
        responses.append(complete(browser, started))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409]
    assert env.count("users") == 1 and env.count("oryx_sessions") == 1
    assert env.rows("SELECT status FROM invitations")[0]["status"] == "accepted"


# --------------------------------------------------------------------------
# Quotas d'ouverture par invitation
# --------------------------------------------------------------------------

def test_invitation_openings_three_per_hour_and_twelve_in_total(env):
    app = env.app(fixed_clock=True)
    token = env.invitation()
    browsers = [env.browser(app, f"198.51.100.{i + 10}") for i in range(6)]
    statuses = []
    for hour in range(5):
        for i in range(4):
            statuses.append(begin(browsers[(hour + i) % 6], token).status_code)
        env.clock.advance(hours=1)
    # Par heure : 3 admises, la 4e refusée ; à la 5e heure, plafond total atteint.
    assert statuses == [202, 202, 202, 409] * 4 + [409] * 4
    assert env.count("auth_operations", "kind = 'invitation_activation'") == 12
    # Toutes les opérations conservées comptent, même expirées.
    assert env.count("auth_operations", "expires_at <= :t", t=env.clock.now) == 12
    refused = begin(browsers[0], token)
    assert (refused.status_code, refused.json()) == (409, {"error": "invitation_unavailable"})
    assert set_cookies(refused) == []


def test_invitation_hourly_window_boundary(env):
    app = env.app(fixed_clock=True)
    token = env.invitation()
    browser = env.browser(app)
    t0 = env.clock.now
    assert [begin(browser, token).status_code for _ in range(4)] == [202, 202, 202, 409]
    env.clock.now = t0 + timedelta(hours=1) - timedelta(microseconds=1)
    assert begin(browser, token).status_code == 409
    env.clock.now = t0 + timedelta(hours=1)
    assert begin(browser, token).status_code == 202


def _concurrent_begins(env, app, token, count):
    browsers = [env.browser(app, f"198.51.100.{i + 50}") for i in range(count)]
    barrier = threading.Barrier(count)
    responses = []

    def go(b):
        barrier.wait(timeout=30)
        responses.append(begin(b, token))

    threads = [threading.Thread(target=go, args=(b,)) for b in browsers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return sorted(r.status_code for r in responses)


def test_concurrent_openings_never_exceed_three_per_hour(env):
    app = env.app()
    token = env.invitation()
    assert _concurrent_begins(env, app, token, 5) == [202, 202, 202, 409, 409]
    assert env.count("auth_operations") == 3 and env.count("auth_preauth_candidates") == 3


def test_concurrent_openings_never_exceed_twelve_in_total(env):
    app = env.app(fixed_clock=True)
    token = env.invitation()
    browser = env.browser(app)
    for _ in range(3):
        assert [begin(browser, token).status_code for _ in range(3)] == [202] * 3
        env.clock.advance(hours=1)
        # Cookies candidats expirés (15 min) : le navigateur les a oubliés.
        for name in list(browser.candidates()):
            browser.jar.pop(name)
    assert begin(browser, token).status_code == 202  # 10 ouvertures
    env.clock.advance(hours=1)
    # L'heure permet 3 ouvertures, le total seulement 2.
    assert _concurrent_begins(env, app, token, 4) == [202, 202, 409, 409]
    assert env.count("auth_operations") == 12


def test_unavailable_invitations_share_one_public_answer(env):
    app = env.app(fixed_clock=True)
    browser = env.browser(app)
    revoked = env.admin().create_invitation(cohort="c")
    env.admin().revoke_invitation(revoked.invitation_id)
    consumed = env.invitation()
    activate(env, browser, consumed)
    exhausted = env.invitation()
    for _ in range(3):
        begin(browser, exhausted)
    expired = env.invitation()
    answers = [comparable(begin(browser, t)) for t in (revoked.token.reveal(), consumed, exhausted,
                                                       secret(8).reveal(), "not-a-token")]
    env.clock.advance(days=7)
    answers.append(comparable(begin(browser, expired)))
    assert len(set(map(repr, answers))) == 1
    assert answers[0][0] == 409 and answers[0][1] == b'{"error":"invitation_unavailable"}'


# --------------------------------------------------------------------------
# Quota de reprise par opération
# --------------------------------------------------------------------------

def test_resume_quota_admits_ten_then_refuses(env):
    browser = env.browser(env.app())
    started, _ = activate(env, browser)
    other = env.browser(browser.app, IP_B)
    other.jar.update({k: v for k, v in browser.jar.items() if k.startswith("__Host-oryx-candidate-")})
    for i in range(10):
        assert resume(browser if i % 2 else other, started).status_code == 200
    for b in (browser, other):
        refused = resume(b, started)
        assert (refused.status_code, refused.json()) == (429, {"error": "rate_limited"})
        assert set_cookies(refused) == []
    assert env.resume_attempts(started["operation_id"]) == 10  # toutes IP confondues, jamais 11
    assert env.count("oryx_sessions") == 1


def test_resume_quota_window_slides(env):
    app = env.app(fixed_clock=True)
    browser = env.browser(app)
    started, _ = activate(env, browser)
    t0 = env.clock.now
    for _ in range(10):
        assert resume(browser, started).status_code == 200
    env.clock.now = t0 + timedelta(minutes=5) - timedelta(microseconds=2)
    assert resume(browser, started).status_code == 429
    assert env.resume_attempts() == 10


def test_resume_without_valid_credential_never_debits(env):
    app = env.app()
    browser = env.browser(app)
    started, _ = activate(env, browser)
    op, cand = started["operation_id"], uuid.UUID(started["candidate_id"])
    thief = env.browser(app, IP_B)
    thief.jar[candidate_name(cand)] = secret(6).reveal()  # faux secret, vrai sélecteur
    for _ in range(15):
        response = resume(thief, started)
        assert (response.status_code, response.json()) == (409, {"error": "resume_unavailable"})
    # Autre candidat (autre opération) présenté pour cette opération.
    other_started, _ = activate(env, thief)
    mixed = thief.post("/invitation/resume", {"operation_id": op, "candidate_id": other_started["candidate_id"]})
    assert mixed.json() == {"error": "resume_unavailable"}
    # Opération inconnue, UUID isolé.
    lone = thief.post("/invitation/resume", {"operation_id": str(uuid.uuid4()),
                                             "candidate_id": other_started["candidate_id"]})
    assert lone.json() == {"error": "resume_unavailable"}
    assert env.resume_attempts(op) == 0 and env.resume_attempts() == 0
    assert resume(browser, started).status_code == 200  # le budget du titulaire est intact


def test_resume_after_window_grants_nothing(env):
    app = env.app(fixed_clock=True)
    browser = env.browser(app)
    started, _ = activate(env, browser)
    env.clock.advance(minutes=5)
    response = resume(browser, started)
    assert (response.status_code, response.json()) == (409, {"error": "resume_unavailable"})
    assert env.resume_attempts() == 0


def test_resume_routes_are_typed(env):
    app = env.app()
    browser = env.browser(app)
    started, done = activate(env, browser)
    via_sign_in = browser.post("/sign-in/resume", {"operation_id": started["operation_id"],
                                                    "candidate_id": started["candidate_id"]})
    assert (via_sign_in.status_code, via_sign_in.json()) == (409, {"error": "resume_unavailable"})
    assert env.resume_attempts() == 0
    # Connexion PR 3 : /invitation/resume refuse une opération de connexion.
    create_profile_with_identity(env.engine, env.supabase, BOB, user_id="legacy-bob")
    sign_in = browser.start(BOB).json()
    assert browser.verify(sign_in, env.mailer.last_code(BOB)).status_code == 200
    cross = browser.post("/invitation/resume", {"operation_id": sign_in["operation_id"],
                                                "candidate_id": sign_in["candidate_id"]})
    assert (cross.status_code, cross.json()) == (409, {"error": "resume_unavailable"})
    assert env.resume_attempts() == 0
    # Connexion PR 3 inchangée : /sign-in/resume reprend sa propre opération.
    legit = browser.post("/sign-in/resume", {"operation_id": sign_in["operation_id"],
                                             "candidate_id": sign_in["candidate_id"]})
    assert legit.status_code == 200 and set(legit.json()) == {"status", "generation_id", "expires_in"}
    # Entrée interne historique : reprise d'invitation sans type, sans écriture.
    cookie = browser.jar[candidate_name(uuid.UUID(started["candidate_id"]))]
    internal = env.admin().resume_result(operation_id=uuid.UUID(started["operation_id"]), candidate=CandidateCredential(
        uuid.UUID(started["candidate_id"]), Redacted(cookie)))
    assert internal.status == "resumed" and str(internal.session.generation_id) == done["generation_id"]
    assert env.resume_attempts() == 0
    with pytest.raises(ValueError):
        env.admin().resume_result(operation_id=uuid.uuid4(), candidate=None, expected_kind="email_linking")


# --------------------------------------------------------------------------
# Liaison e-mail : start, resend, verify
# --------------------------------------------------------------------------

def test_email_linking_end_to_end_without_session_or_cookie(env):
    app = env.app(fixed_clock=True)
    browser = env.browser(app)
    started, done = activate(env, browser)
    generation = done["generation_id"]
    jar = dict(browser.jar)
    start = link_start(browser, generation)
    assert (start.status_code, set(start.json())) == (202, {"status", "operation_id"})
    assert start.json()["status"] == "code_sent" and set_cookies(start) == []
    op = start.json()["operation_id"]
    env.clock.advance(seconds=61)
    resent = link_resend(browser, generation, op)
    assert (resent.status_code, resent.json()) == (202, {"status": "code_sent", "operation_id": op})
    assert len(env.mailer.sent) == 2 and set_cookies(resent) == []
    verify = link_verify(browser, generation, op, env.mailer.last_code(ALICE))
    assert (verify.status_code, verify.json()) == (200, {"status": "email_linked", "operation_id": op})
    assert set_cookies(verify) == [] and browser.jar == jar
    user = session_of(env, generation)["user_id"]
    identity = env.rows("SELECT * FROM auth_identities")[0]
    assert identity["user_id"] == user and identity["verified_email_normalized"] == ALICE
    assert env.count("oryx_sessions") == 1 and env.count("users") == 1
    result = link_result(browser, generation, op)
    assert (result.status_code, result.json()) == (200, {"status": "email_linked", "operation_id": op})
    assert set_cookies(result) == [] and browser.jar == jar
    # La consultation ne confirme aucune livraison de session.
    generation_row = env.rows("SELECT * FROM oryx_session_generations")[0]
    assert generation_row["delivery_status"] == "pending" and generation_row["delivery_confirmed_at"] is None
    assert env.count("oryx_sessions") == 1 and env.count("auth_identities") == 1
    assert env.count("auth_operations") == 2
    assert env.resume_attempts(op) == 1


def test_delivery_failure_reports_the_operation_without_cookie(env):
    browser = env.browser(env.app(fixed_clock=True))
    _, done = activate(env, browser)
    env.mailer.script = [ProviderRejected("rejected")]
    start = link_start(browser, done["generation_id"])
    op = start.json()["operation_id"]
    assert (start.status_code, start.json()) == (503, {"error": "delivery_failed", "operation_id": op})
    assert set_cookies(start) == []
    env.clock.advance(seconds=61)
    assert link_resend(browser, done["generation_id"], op).status_code == 202


def test_wrong_code_and_other_session_on_verify(env):
    app = env.app()
    browser = env.browser(app)
    _, done = activate(env, browser)
    op = link_start(browser, done["generation_id"]).json()["operation_id"]
    wrong = link_verify(browser, done["generation_id"], op, "00000000")
    assert (wrong.status_code, wrong.json()) == (401, {"error": "code_incorrect"})
    intruder = env.browser(app, IP_B)
    _, other = activate(env, intruder)
    stolen = link_verify(intruder, other["generation_id"], op, env.mailer.last_code(ALICE))
    assert (stolen.status_code, stolen.json()) == (409, {"error": "operation_unavailable"})
    assert env.count("auth_identities") == 0


def test_linking_never_merges_profiles(env):
    legacy = create_profile_with_identity(env.engine, env.supabase, ALICE, user_id="legacy-alice")
    browser = env.browser(env.app())
    _, done = activate(env, browser)
    op = link_start(browser, done["generation_id"]).json()["operation_id"]
    verify = link_verify(browser, done["generation_id"], op, env.mailer.last_code(ALICE))
    assert (verify.status_code, verify.json()) == (409, {"error": "linking_unavailable"})
    assert [r["user_id"] for r in env.rows("SELECT user_id FROM auth_identities")] == [legacy]
    assert link_result(browser, done["generation_id"], op).json() == {"error": "result_unavailable"}


def test_a_linked_profile_cannot_start_again(env):
    browser = env.browser(env.app())
    generation, _, _ = linked(env, browser)
    again = link_start(browser, generation, BOB)
    assert (again.status_code, again.json()) == (409, {"error": "profile_already_linked"})


def test_invalid_session_on_linking_routes(env):
    browser = env.browser(env.app())
    _, done = activate(env, browser)
    browser.jar[active_name(uuid.UUID(done["generation_id"]))] = secret(3).reveal()
    response = link_start(browser, done["generation_id"])
    assert (response.status_code, response.json()) == (401, {"error": "session_invalid"})
    assert env.count("auth_operations", "kind = 'email_linking'") == 0


# --------------------------------------------------------------------------
# Résultat de liaison : confidentialité, sessions, quota (T2, T5)
# --------------------------------------------------------------------------

def _operations_of_every_kind(env, app):
    """Identifiants d'opération variés : inconnue, connexion, liaison en
    attente, liaison committée, liaison d'un autre profil."""
    owner = env.browser(app)
    generation, committed, started = linked(env, owner)
    pending_browser = env.browser(app, IP_B)
    _, pending_done = activate(env, pending_browser)
    pending = link_start(pending_browser, pending_done["generation_id"], BOB).json()["operation_id"]
    create_profile_with_identity(env.engine, env.supabase, "carol@example.com", user_id="legacy-carol")
    sign_in = owner.start("carol@example.com").json()["operation_id"]
    return owner, generation, {"unknown": str(uuid.uuid4()), "sign_in": sign_in, "invitation": started["operation_id"],
                               "pending": pending, "committed": committed}


def test_invalid_session_answer_never_depends_on_the_operation(env):
    app = env.app(fixed_clock=True)
    owner, generation, ops = _operations_of_every_kind(env, app)
    real = owner.jar[active_name(uuid.UUID(generation))]
    answers = {}
    owner.jar[active_name(uuid.UUID(generation))] = secret(4).reveal()  # faux secret
    for name, op in ops.items():
        answers[name] = comparable(link_result(owner, generation, op))
    fake_generation = uuid.uuid4()
    owner.jar[active_name(fake_generation)] = real
    for name, op in ops.items():
        answers["gen-" + name] = comparable(link_result(owner, str(fake_generation), op))
    env.clock.advance(minutes=6)  # résultat expiré : toujours 401 avec un faux secret
    answers["expired"] = comparable(link_result(owner, generation, ops["committed"]))
    assert len(set(map(repr, answers.values()))) == 1
    assert answers["committed"][0] == 401 and answers["committed"][1] == b'{"error":"session_invalid"}'
    assert env.resume_attempts() == 0


def test_authentic_but_unauthorized_sessions_get_one_identical_409(env):
    app = env.app(fixed_clock=True)
    owner, generation, ops = _operations_of_every_kind(env, app)
    intruder = env.browser(app, "198.51.100.77")
    _, other = activate(env, intruder)
    answers = {}
    for name, op in ops.items():
        if name != "committed":
            answers[name] = comparable(link_result(owner, generation, op))
        answers["other-" + name] = comparable(link_result(intruder, other["generation_id"], op))
    # Autre session du même profil : connexion OTP après la liaison.
    same_profile = env.browser(app, "198.51.100.78")
    env.clock.advance(seconds=61)  # intervalle d'émission OTP de PR 2
    sign_in = same_profile.start(ALICE).json()
    verified = same_profile.verify(sign_in, env.mailer.last_code(ALICE))
    assert verified.status_code == 200
    assert session_of(env, verified.json()["generation_id"])["user_id"] == session_of(env, generation)["user_id"]
    answers["same-profile"] = comparable(link_result(same_profile, verified.json()["generation_id"],
                                                     ops["committed"]))
    env.clock.advance(minutes=5)  # at == resume_until : hors délai
    answers["expired"] = comparable(link_result(owner, generation, ops["committed"]))
    assert len(set(map(repr, answers.values()))) == 1
    assert answers["expired"][:2] == (409, b'{"error":"result_unavailable"}')
    assert env.resume_attempts() == 0
    assert "carol" not in repr(answers) and "user_id" not in repr(answers)


def test_revocation_between_the_two_phases_is_detected(env):
    browser = env.browser(env.app())
    generation, op, _ = linked(env, browser)
    session_id = session_of(env, generation)["id"]
    env.hooks["result.prevalidated"] = lambda: env.admin().revoke_session(session_id, "security")
    response = link_result(browser, generation, op)
    assert (response.status_code, response.json()) == (401, {"error": "session_invalid"})
    env.hooks.clear()
    assert link_result(browser, generation, op).json() == {"error": "session_invalid"}
    assert env.resume_attempts() == 0


def test_session_expiry_during_the_lock_wait_is_detected(env):
    app = env.app(fixed_clock=True)
    browser = env.browser(app)
    generation, op, _ = linked(env, browser)
    expires = session_of(env, generation)["absolute_expires_at"]
    holder = env.observer.connect()
    trans = holder.begin()
    holder.execute(sa.text("SELECT id FROM auth_operations WHERE id = :o FOR UPDATE"), {"o": op})
    result = {}

    def call():
        result["response"] = link_result(browser, generation, op)

    thread = threading.Thread(target=call)
    thread.start()
    _wait_for_lock_waiter(env.observer)
    env.clock.now = expires  # la session expire pendant l'attente du verrou
    trans.rollback()
    holder.close()
    thread.join(timeout=30)
    assert (result["response"].status_code, result["response"].json()) == (401, {"error": "session_invalid"})
    assert env.resume_attempts() == 0


def test_result_quota_admits_ten_then_refuses(env):
    app = env.app()
    browser = env.browser(app)
    generation, op, _ = linked(env, browser)
    other_ip = env.browser(app, IP_B)
    other_ip.jar.update(browser.actives())  # même cookie actif, autre IP (et autre liaison CSRF)
    for i in range(10):
        response = link_result(other_ip if i % 2 else browser, generation, op)
        assert response.status_code == 200, response.text
    for b in (browser, other_ip):
        refused = link_result(b, generation, op)
        assert (refused.status_code, refused.json()) == (429, {"error": "rate_limited"})
    assert env.resume_attempts(op) == 10
    assert env.count("oryx_sessions") == 1 and env.count("auth_operations") == 2
