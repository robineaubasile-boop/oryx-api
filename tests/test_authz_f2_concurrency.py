"""Concurrence réelle de la frontière d'autorisation (R1 PR 5, C.5.2 §5.3-5.5, BF2).

Chaque scénario utilise de VRAIES connexions PostgreSQL concurrentes
(threads, une connexion par acteur) : un bloqueur tient un verrou du plan,
le runner attend réellement (pg_stat_activity.wait_event_type = 'Lock'),
puis l'état change (révocation committée, échéance dépassée) avant que le
verrou ne soit libéré. Aucune erreur d'interblocage n'est tolérée
(pg_stat_database.deadlocks inchangé).

Couvre : révocation entre phase A et phase B, révocation pendant l'attente
d'un verrou, expiration pendant l'attente du verrou users et d'un verrou
métier, révocation concurrente d'une mutation déjà autorisée (elle attend
le COMMIT, puis plus rien n'est autorisé), compatibilité du plan de verrous
avec les transactions F2 (résolution, connexion OTP, révocation) dans les
deux sens, et charge mixte.

R1 PR 6 (plan PROFILE_MUTATION, CAS sur users.level_revision), avec des
points d'interruption déterministes (Pause : la transaction détient ses
verrous jusqu'au relâchement) et l'attente constatée dans pg_stat_activity :
deux sessions d'un même utilisateur depuis une même révision (jamais deux
changements d'état), dans les deux ordres ; deux demandes identiques sans
changement ; deux utilisateurs indépendants ; mutations de profil et de
portefeuille dans les deux ordres ; expiration pendant l'attente du profil,
de la session et de la génération ; révocation avant, pendant et après la
frontière d'autorisation ; transactions F2 dans les deux sens ; écriture
historique concurrente ; charge mixte sans interblocage.
"""
import threading
import time

import pytest
import sqlalchemy as sa

from core.auth_f2.service import F2AuthService
from core.authz_f2 import commands as c
from core.authz_f2 import runner as runner_module
from core.authz_f2.runner import AUTHORIZED, SESSION_INVALID
from tests.authz_f2_support import USER_A, AuthzEnv, replace_command
from tests.f2_support import ISSUER, FakeMailer, FakeSupabase, prepare_schema, rows, scalar
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

CONCURRENT_APP = "oryx-authz-concurrent-f2"


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = AuthzEnv(pg_url)
    environment.deadlocks_before = deadlocks(environment)
    yield environment
    assert deadlocks(environment) == environment.deadlocks_before
    environment.dispose()


@pytest.fixture
def alice(env):
    issued = env.confirmed()
    ids = env.seed_resources()
    return issued, ids[(USER_A, "positions")][0]


def deadlocks(env):
    return scalar(env.observer, "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")


class Background(threading.Thread):
    def __init__(self, fn):
        super().__init__(daemon=True)
        self.fn = fn
        self.result = self.error = None

    def run(self):
        try:
            self.result = self.fn()
        except BaseException as exc:  # noqa: BLE001 — rapporté au test
            self.error = exc

    def outcome(self, timeout=30):
        self.join(timeout)
        assert not self.is_alive(), "acteur bloqué"
        if self.error is not None:
            raise self.error
        return self.result


def wait_until(predicate, timeout=4.0, message="condition non atteinte"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


class Blocker:
    """Connexion tierce qui tient un verrou jusqu'à release()."""

    def __init__(self, env, sql, **params):
        self.conn = env.observer.connect()
        self.tx = self.conn.begin()
        self.conn.execute(sa.text(sql), params)

    def release(self):
        self.tx.rollback()
        self.conn.close()


def quantity(env, position_id):
    return scalar(env.observer, "SELECT quantity FROM portfolio_positions WHERE id = :i", i=position_id)


def concurrent_f2(env, pg_url):  # noqa: F811
    """Second moteur F2 (connexions et doubles propres, même trousseau et
    même base) pour agir PENDANT qu'une transaction du runner est ouverte."""
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})
    supabase, mailer = FakeSupabase(), FakeMailer()
    assert supabase.issuer == ISSUER
    return engine, F2AuthService(engine, env.keyring, supabase, mailer), supabase, mailer


def waiting(env, application_name):
    return scalar(env.observer, "SELECT count(*) FROM pg_stat_activity WHERE application_name = :a "
                                "AND wait_event_type = 'Lock'", a=application_name)


# --------------------------------------------------------------------------
# Révocation
# --------------------------------------------------------------------------

def test_revocation_between_phase_a_and_phase_b_is_refused(env, alice):
    issued, target = alice
    env.hooks["prevalidated"] = lambda: env.service.revoke_session(issued.session_id, "security")
    outcome = env.runner().run(issued.credential(), replace_command(target, quantity="50"))
    assert outcome.status == SESSION_INVALID
    assert quantity(env, target) == 3.0
    assert env.hook_calls == ["prevalidated", "locked"]


def test_revocation_committed_while_waiting_for_a_lock_is_refused(env, alice):
    issued, target = alice
    blocker = Blocker(env, "SELECT id FROM users WHERE id = :u FOR UPDATE", u=USER_A)
    try:
        actor = Background(lambda: env.runner().run(issued.credential(), replace_command(target, quantity="50")))
        actor.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="le runner n'attend pas le verrou users")
        assert env.hook_calls == ["prevalidated"]
        # La révocation ne dépend pas du verrou users : elle committe pendant l'attente.
        assert env.service.revoke_session(issued.session_id, "security") is True
    finally:
        blocker.release()
    assert actor.outcome().status == SESSION_INVALID
    assert quantity(env, target) == 3.0


def test_concurrent_revocation_waits_for_an_authorized_mutation_then_nothing_more_is_authorized(env, alice, pg_url):  # noqa: F811
    issued, target = alice
    engine, f2, _, _ = concurrent_f2(env, pg_url)
    revocation = Background(lambda: f2.revoke_session(issued.session_id, "security"))

    def during_authorized():
        revocation.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 1, message="la révocation n'attend pas")
        time.sleep(0.2)
        assert revocation.is_alive()  # bloquée par oryx_sessions FOR SHARE du runner

    env.hooks["authorized"] = during_authorized
    try:
        outcome = env.runner().run(issued.credential(), replace_command(target, quantity="51"))
        assert outcome.status == AUTHORIZED
        assert revocation.outcome() is True
    finally:
        engine.dispose()
    assert quantity(env, target) == 51.0  # committée AVANT la révocation
    (session,) = rows(env.observer, "SELECT revoked_at FROM oryx_sessions WHERE id = :s", s=issued.session_id)
    updated_at = scalar(env.observer, "SELECT updated_at FROM portfolio_positions WHERE id = :i", i=target)
    assert session["revoked_at"].replace(tzinfo=None) - session["revoked_at"].utcoffset() > updated_at
    env.hooks.clear()
    again = env.runner().run(issued.credential(), replace_command(target, 1, quantity="52"))
    assert again.status == SESSION_INVALID and quantity(env, target) == 51.0


# --------------------------------------------------------------------------
# Expiration pendant une attente de verrou (BF2)
# --------------------------------------------------------------------------

def _expire_while_waiting(env, issued, target, blocker_sql, **params):
    env.expire_at(issued.session_id, "clock_timestamp() + interval '1200 milliseconds'")
    blocker = Blocker(env, blocker_sql, **params)
    try:
        actor = Background(lambda: env.runner().run(issued.credential(), replace_command(target, quantity="60")))
        actor.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="le runner n'attend pas")
        hooks_while_waiting = list(env.hook_calls)
        wait_until(lambda: scalar(env.observer, "SELECT absolute_expires_at <= clock_timestamp() FROM oryx_sessions "
                                                "WHERE id = :s", s=issued.session_id), timeout=3.0)
        assert actor.is_alive()
    finally:
        blocker.release()
    assert actor.outcome().status == SESSION_INVALID
    assert quantity(env, target) == 3.0
    return hooks_while_waiting


def test_expiry_while_waiting_for_the_user_lock_is_refused(env, alice):
    issued, target = alice
    hooks = _expire_while_waiting(env, issued, target, "SELECT id FROM users WHERE id = :u FOR UPDATE", u=USER_A)
    assert hooks == ["prevalidated"]


def test_expiry_while_waiting_for_a_business_lock_is_refused(env, alice):
    issued, target = alice
    hooks = _expire_while_waiting(env, issued, target, "SELECT id FROM portfolio_positions WHERE id = :i FOR UPDATE",
                                  i=target)
    # users, session et génération déjà verrouillés ; l'attente porte sur
    # la ligne métier, avant T_authorization.
    assert hooks == ["prevalidated"]
    assert env.hook_calls == ["prevalidated", "locked"]


# --------------------------------------------------------------------------
# Compatibilité avec l'ordre global des verrous F2
# --------------------------------------------------------------------------

def test_f2_transactions_wait_behind_an_authorized_mutation_without_deadlock(env, alice, pg_url):  # noqa: F811
    issued, target = alice
    engine, f2, supabase, mailer = concurrent_f2(env, pg_url)
    supabase.subjects["alice3@example.com"] = "00000000-0000-4000-8000-0000000a1103"
    with env.observer.begin() as conn:  # deuxième identité du même profil : connexion OTP concurrente
        conn.execute(sa.text(
            "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, verified_email_normalized, "
            "email_verified_at, created_at) VALUES (gen_random_uuid(), :u, 'supabase', :i, :s, :e, now(), now())"),
            {"u": USER_A, "i": ISSUER, "s": supabase.subject_for("alice3@example.com"), "e": "alice3@example.com"})
    start = f2.start_sign_in(email="alice3@example.com", client_ip="203.0.113.77")
    assert start.status == "issued"
    actors = {
        # authenticate_session : génération FOR UPDATE (attend notre FOR SHARE).
        "resolve": Background(lambda: f2.authenticate_session(issued.credential())),
        # verify : users FOR KEY SHARE (attend notre users FOR UPDATE), FK de la nouvelle session.
        "sign_in": Background(lambda: f2.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                                       code=mailer.last_code("alice3@example.com"),
                                                       client_ip="203.0.113.77")),
    }

    def during_authorized():
        for actor in actors.values():
            actor.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 2, message="les transactions F2 n'attendent pas")

    env.hooks["authorized"] = during_authorized
    try:
        assert env.runner().run(issued.credential(), replace_command(target, quantity="70")).status == AUTHORIZED
        assert actors["resolve"].outcome().user_id == USER_A
        assert actors["sign_in"].outcome().status == "authenticated"
    finally:
        engine.dispose()
    assert quantity(env, target) == 70.0


def test_mutation_waits_behind_an_f2_finalization_holding_the_user_rank(env, alice, pg_url):  # noqa: F811
    issued, target = alice
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})
    supabase, mailer = FakeSupabase(), FakeMailer()
    paused, resume = threading.Event(), threading.Event()

    def f2_hook(point):
        if point == "verify.finalizing":  # users FOR KEY SHARE, opération, candidat, OTP, identité verrouillés
            paused.set()
            assert resume.wait(10)

    f2 = F2AuthService(engine, env.keyring, supabase, mailer, fault_hook=f2_hook)
    supabase.subjects["alice4@example.com"] = "00000000-0000-4000-8000-0000000a1104"
    with env.observer.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, verified_email_normalized, "
            "email_verified_at, created_at) VALUES (gen_random_uuid(), :u, 'supabase', :i, :s, :e, now(), now())"),
            {"u": USER_A, "i": ISSUER, "s": supabase.subject_for("alice4@example.com"), "e": "alice4@example.com"})
    start = f2.start_sign_in(email="alice4@example.com", client_ip="203.0.113.78")
    sign_in = Background(lambda: f2.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                                  code=mailer.last_code("alice4@example.com"), client_ip="203.0.113.78"))
    try:
        sign_in.start()
        assert paused.wait(10)
        mutation = Background(lambda: env.runner().run(issued.credential(), replace_command(target, quantity="71")))
        mutation.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="la mutation n'attend pas le rang users")
        resume.set()
        assert sign_in.outcome().status == "authenticated"
        assert mutation.outcome().status == AUTHORIZED
    finally:
        resume.set()
        engine.dispose()
    assert quantity(env, target) == 71.0


def test_mixed_load_produces_no_deadlock_and_no_spurious_refusal(env, alice, pg_url):  # noqa: F811
    issued, target = alice
    engine, f2, _, _ = concurrent_f2(env, pg_url)
    barrier = threading.Barrier(4)
    runner = env.runner()

    def mutate():
        barrier.wait(10)
        # Acteur unique du portefeuille : chaque remplacement effectif part
        # de la révision i (CAS) et la fait avancer d'une unité.
        return [runner.run(issued.credential(), replace_command(target, i, quantity=str(i + 1))).status
                for i in range(15)]

    def read():
        barrier.wait(10)
        return [runner.run(issued.credential(), c.ListPositions()).status for _ in range(15)]

    def resolve():
        barrier.wait(10)
        return ["ok" if f2.authenticate_session(issued.credential()) else "none" for _ in range(15)]

    def legacy_level():
        barrier.wait(10)
        out = []
        for i in range(15):
            with engine.begin() as conn:  # écriture historique du profil (FOR NO KEY UPDATE implicite)
                conn.execute(sa.text("UPDATE users SET level = :l WHERE id = :u"),
                             {"l": "debutant" if i % 2 else "intermediaire", "u": USER_A})
            out.append("ok")
        return out

    actors = [Background(fn) for fn in (mutate, read, resolve, legacy_level)]
    try:
        for actor in actors:
            actor.start()
        results = [actor.outcome(timeout=120) for actor in actors]
    finally:
        engine.dispose()
    assert results[0] == [AUTHORIZED] * 15 and results[1] == [AUTHORIZED] * 15
    assert results[2] == ["ok"] * 15 and results[3] == ["ok"] * 15
    assert quantity(env, target) == 15.0


# --------------------------------------------------------------------------
# R1 PR 6 : CAS concurrent sur users.level_revision (plan PROFILE_MUTATION)
# --------------------------------------------------------------------------

class Pause:
    """Point d'interruption déterministe d'un runner : à `point`, signale
    `reached` puis attend `release` (la transaction et ses verrous restent
    ouverts pendant l'attente)."""

    def __init__(self, point):
        self.point = point
        self.reached, self.release = threading.Event(), threading.Event()

    def __call__(self, point):
        if point == self.point:
            self.reached.set()
            assert self.release.wait(20), "pause non relâchée"


def profile_of(env, user_id=USER_A):
    return env.profile(user_id)[:2]


@pytest.fixture
def two_sessions(env):
    return env.confirmed(), env.confirmed("alice2@example.com", USER_A)


@pytest.mark.parametrize("first_wins", [0, 1])
@pytest.mark.parametrize("levels", [("avance", "avance"), ("avance", "debutant"), ("debutant", "avance")])
def test_two_sessions_from_one_revision_never_both_change_state(env, two_sessions, first_wins, levels):
    env.set_profile("debutant", 7)
    first, second = two_sessions[first_wins], two_sessions[1 - first_wins]
    pause = Pause("executed")
    holder = Background(lambda: env.runner(hook=pause).run(first.credential(), c.UpdateProfileLevel(levels[0], 7)))
    holder.start()
    assert pause.reached.wait(10)
    contender = Background(lambda: env.runner(hook=lambda point: None).run(second.credential(),
                                                                          c.UpdateProfileLevel(levels[1], 7)))
    contender.start()
    # Le second attend réellement le verrou users FOR UPDATE du premier.
    wait_until(lambda: env.waiting_on_lock() == 1, message="le second n'attend pas le profil")
    pause.release.set()
    won, lost = holder.outcome(), contender.outcome()
    expected_first = ("avance", 8) if levels[0] == "avance" else ("debutant", 7)
    assert won.status == AUTHORIZED and (won.value.level, won.value.level_revision) == expected_first
    if expected_first[1] == 8:
        # Le premier a changé l'état : la révision 7 est périmée pour le second.
        assert lost.status == runner_module.REVISION_CONFLICT and lost.value is None
    else:
        # Le premier n'a rien changé : le second part de la même révision.
        assert lost.status == AUTHORIZED
    # Exactement UN changement d'état committé (révision 7 -> 8), jamais deux.
    changed = [o for o in (won, lost) if o.status == AUTHORIZED and o.value.level_revision == 8]
    assert len(changed) == 1
    assert profile_of(env) == (changed[0].value.level, 8)


def test_two_identical_requests_without_change_both_succeed_at_equal_revision(env, two_sessions):
    env.set_profile("avance", 3)
    before = env.profile()
    pause = Pause("executed")
    a1, a2 = two_sessions
    holder = Background(lambda: env.runner(hook=pause).run(a1.credential(), c.UpdateProfileLevel("avance", 3)))
    holder.start()
    assert pause.reached.wait(10)
    contender = Background(lambda: env.runner(hook=lambda point: None).run(a2.credential(),
                                                                          c.UpdateProfileLevel("avance", 3)))
    contender.start()
    wait_until(lambda: env.waiting_on_lock() == 1, message="le second n'attend pas le profil")
    pause.release.set()
    for outcome in (holder.outcome(), contender.outcome()):
        assert outcome.status == AUTHORIZED and outcome.value == c.ProfileView(USER_A, "avance", 3)
    assert env.profile() == before  # aucune écriture


def test_independent_users_do_not_wait_for_each_other(env):
    a, b = env.confirmed(), env.confirmed("bob@example.com", "user-b")
    pause = Pause("executed")
    holder = Background(lambda: env.runner(hook=pause).run(a.credential(), c.UpdateProfileLevel("avance", 0)))
    holder.start()
    try:
        assert pause.reached.wait(10)
        started = time.monotonic()
        other = env.runner(hook=lambda point: None).run(b.credential(), c.UpdateProfileLevel("avance", 0))
        assert time.monotonic() - started < 3 and env.waiting_on_lock() == 0
        assert other.status == AUTHORIZED and other.value == c.ProfileView("user-b", "avance", 1)
    finally:
        pause.release.set()
    assert holder.outcome().value == c.ProfileView(USER_A, "avance", 1)


@pytest.mark.parametrize("profile_first", [True, False])
def test_profile_and_position_mutations_in_both_orders_without_deadlock(env, alice, profile_first):
    issued, target = alice
    pause = Pause("executed")
    profile = c.UpdateProfileLevel("avance", 0)
    position = replace_command(target, quantity="33")
    first, second = (profile, position) if profile_first else (position, profile)
    holder = Background(lambda: env.runner(hook=pause).run(issued.credential(), first))
    holder.start()
    assert pause.reached.wait(10)
    contender = Background(lambda: env.runner(hook=lambda point: None).run(issued.credential(), second))
    contender.start()
    wait_until(lambda: env.waiting_on_lock() == 1, message="la seconde mutation n'attend pas le rang users")
    pause.release.set()
    assert holder.outcome().status == contender.outcome().status == AUTHORIZED
    assert profile_of(env) == ("avance", 1) and quantity(env, target) == 33.0


@pytest.mark.parametrize("table,sql", [
    ("users", "SELECT id FROM users WHERE id = :u FOR UPDATE"),
    ("oryx_sessions", "SELECT id FROM oryx_sessions WHERE id = :s FOR UPDATE"),
    ("oryx_session_generations", "SELECT id FROM oryx_session_generations WHERE id = :g FOR UPDATE"),
])
def test_expiry_while_waiting_for_profile_session_or_generation_is_refused(env, table, sql):
    issued = env.confirmed()
    env.set_profile("debutant", 4)
    before = env.profile()
    env.expire_at(issued.session_id, "clock_timestamp() + interval '1200 milliseconds'")
    blocker = Blocker(env, sql, u=USER_A, s=issued.session_id, g=issued.generation_id)
    try:
        actor = Background(lambda: env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 4)))
        actor.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message=f"le runner n'attend pas {table}")
        assert env.hook_calls == ["prevalidated"]  # attente avant T_authorization
        wait_until(lambda: scalar(env.observer, "SELECT absolute_expires_at <= clock_timestamp() FROM oryx_sessions "
                                                "WHERE id = :s", s=issued.session_id), timeout=3.0)
        assert actor.is_alive()
    finally:
        blocker.release()
    assert actor.outcome().status == SESSION_INVALID
    assert env.profile() == before
    assert env.hook_calls == ["prevalidated", "locked"]


def test_profile_revocation_between_phase_a_and_phase_b_is_refused(env):
    issued = env.confirmed()
    before = env.profile()
    env.hooks["prevalidated"] = lambda: env.service.revoke_session(issued.session_id, "security")
    assert env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 0)).status == SESSION_INVALID
    assert env.profile() == before


def test_profile_revocation_while_waiting_for_the_profile_lock_is_refused(env):
    issued = env.confirmed()
    before = env.profile()
    blocker = Blocker(env, "SELECT id FROM users WHERE id = :u FOR UPDATE", u=USER_A)
    try:
        actor = Background(lambda: env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 0)))
        actor.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="le runner n'attend pas le profil")
        assert env.service.revoke_session(issued.session_id, "security") is True
    finally:
        blocker.release()
    assert actor.outcome().status == SESSION_INVALID
    assert env.profile() == before


def test_profile_revocation_after_authorization_waits_for_commit(env, pg_url):  # noqa: F811
    issued = env.confirmed()
    engine, f2, _, _ = concurrent_f2(env, pg_url)
    revocation = Background(lambda: f2.revoke_session(issued.session_id, "security"))

    def during_authorized():
        revocation.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 1, message="la révocation n'attend pas")
        assert revocation.is_alive()

    env.hooks["authorized"] = during_authorized
    try:
        assert env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 0)).status == AUTHORIZED
        assert revocation.outcome() is True
    finally:
        engine.dispose()
    assert profile_of(env) == ("avance", 1)
    env.hooks.clear()
    assert env.runner().run(issued.credential(), c.UpdateProfileLevel("debutant", 1)).status == SESSION_INVALID
    assert profile_of(env) == ("avance", 1)


def test_f2_transactions_wait_behind_a_profile_mutation_without_deadlock(env, pg_url):  # noqa: F811
    issued = env.confirmed()
    engine, f2, supabase, mailer = concurrent_f2(env, pg_url)
    supabase.subjects["alice5@example.com"] = "00000000-0000-4000-8000-0000000a1105"
    with env.observer.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, verified_email_normalized, "
            "email_verified_at, created_at) VALUES (gen_random_uuid(), :u, 'supabase', :i, :s, :e, now(), now())"),
            {"u": USER_A, "i": ISSUER, "s": supabase.subject_for("alice5@example.com"), "e": "alice5@example.com"})
    start = f2.start_sign_in(email="alice5@example.com", client_ip="203.0.113.79")
    assert start.status == "issued"
    actors = {
        "resolve": Background(lambda: f2.authenticate_session(issued.credential())),
        "sign_in": Background(lambda: f2.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                                       code=mailer.last_code("alice5@example.com"),
                                                       client_ip="203.0.113.79")),
    }

    def during_executed():
        for actor in actors.values():
            actor.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 2, message="les transactions F2 n'attendent pas")

    env.hooks["executed"] = during_executed
    try:
        assert env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 0)).status == AUTHORIZED
        assert actors["resolve"].outcome().user_id == USER_A
        assert actors["sign_in"].outcome().status == "authenticated"
    finally:
        engine.dispose()
    assert profile_of(env) == ("avance", 1)


def test_profile_mutation_waits_behind_an_f2_finalization_holding_the_user_rank(env, pg_url):  # noqa: F811
    issued = env.confirmed()
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})
    supabase, mailer = FakeSupabase(), FakeMailer()
    paused, resume = threading.Event(), threading.Event()

    def f2_hook(point):
        if point == "verify.finalizing":
            paused.set()
            assert resume.wait(10)

    f2 = F2AuthService(engine, env.keyring, supabase, mailer, fault_hook=f2_hook)
    supabase.subjects["alice6@example.com"] = "00000000-0000-4000-8000-0000000a1106"
    with env.observer.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, verified_email_normalized, "
            "email_verified_at, created_at) VALUES (gen_random_uuid(), :u, 'supabase', :i, :s, :e, now(), now())"),
            {"u": USER_A, "i": ISSUER, "s": supabase.subject_for("alice6@example.com"), "e": "alice6@example.com"})
    start = f2.start_sign_in(email="alice6@example.com", client_ip="203.0.113.80")
    sign_in = Background(lambda: f2.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                                  code=mailer.last_code("alice6@example.com"), client_ip="203.0.113.80"))
    try:
        sign_in.start()
        assert paused.wait(10)
        mutation = Background(lambda: env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 0)))
        mutation.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="la mutation n'attend pas le rang users")
        resume.set()
        assert sign_in.outcome().status == "authenticated"
        assert mutation.outcome().status == AUTHORIZED
    finally:
        resume.set()
        engine.dispose()
    assert profile_of(env) == ("avance", 1)


def test_legacy_write_waits_behind_a_profile_mutation_and_is_invisible_to_the_revision(env, pg_url):  # noqa: F811
    """Écriture historique (PUT /api/user/{id}/level : UPDATE users SET
    level, sans révision) concurrente : elle attend le verrou du profil, ne
    crée aucun interblocage, puis écrit SANS faire avancer level_revision."""
    issued = env.confirmed()
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})

    def legacy():
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE users SET level = 'debutant' WHERE id = :u"), {"u": USER_A})

    writer = Background(legacy)

    def during_executed():
        writer.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 1, message="l'écriture historique n'attend pas")

    env.hooks["executed"] = during_executed
    try:
        assert env.runner().run(issued.credential(), c.UpdateProfileLevel("avance", 0)).status == AUTHORIZED
        writer.outcome()
    finally:
        engine.dispose()
    assert profile_of(env) == ("debutant", 1)  # niveau changé, révision inchangée par l'écriture historique


def test_mixed_profile_load_produces_no_deadlock_and_only_cas_outcomes(env, alice, pg_url):  # noqa: F811
    issued, target = alice
    second = env.confirmed("alice2@example.com", USER_A)
    engine, f2, _, _ = concurrent_f2(env, pg_url)
    barrier = threading.Barrier(6)
    runner = env.runner(hook=lambda point: None)

    def toggle(issued_session):
        barrier.wait(10)
        out = []
        for _ in range(12):
            current = runner.run(issued_session.credential(), c.ReadProfile())
            assert current.status == AUTHORIZED
            target_level = "debutant" if current.value.level == "avance" else "avance"
            result = runner.run(issued_session.credential(),
                                c.UpdateProfileLevel(target_level, current.value.level_revision))
            changed = result.status == AUTHORIZED and result.value.level_revision == current.value.level_revision + 1
            out.append((result.status, changed))
        return out

    def positions():
        barrier.wait(10)
        return [runner.run(issued.credential(), replace_command(target, i, quantity=str(i + 1))).status
                for i in range(12)]

    def resolve():
        barrier.wait(10)
        return ["ok" if f2.authenticate_session(issued.credential()) else "none" for _ in range(12)]

    def legacy_level():
        barrier.wait(10)
        for i in range(12):
            with engine.begin() as conn:
                conn.execute(sa.text("UPDATE users SET level = :l WHERE id = :u"),
                             {"l": "debutant" if i % 2 else "avance", "u": USER_A})
        return ["ok"] * 12

    def reads():
        barrier.wait(10)
        return [runner.run(second.credential(), c.ReadProfile()).status for _ in range(12)]

    actors = [Background(fn) for fn in (lambda: toggle(issued), lambda: toggle(second), positions, resolve,
                                        legacy_level, reads)]
    try:
        for actor in actors:
            actor.start()
        results = [actor.outcome(timeout=120) for actor in actors]
    finally:
        engine.dispose()
    toggles = results[0] + results[1]
    assert {status for status, _ in toggles} <= {AUTHORIZED, runner_module.REVISION_CONFLICT}
    assert results[2] == [AUTHORIZED] * 12 and results[3] == ["ok"] * 12 and results[5] == [AUTHORIZED] * 12
    increments = sum(1 for _, changed in toggles if changed)
    assert profile_of(env)[1] == increments  # chaque changement committé : exactement +1
    assert increments >= 1
