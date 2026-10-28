"""Concurrence réelle du portefeuille F2 versionné (R1 PR 7, plan
PORTFOLIO_MUTATION).

Connexions PostgreSQL indépendantes (threads, une connexion par acteur ;
processus spawn pour le même triplet), points d'interruption déterministes
(Pause : la transaction détient ses verrous jusqu'au relâchement), attente
constatée dans pg_stat_activity, oracles lus par une connexion
indépendante, pg_stat_database.deadlocks inchangé (fixture env).

Couvre, dans les deux ordres quand ils diffèrent : CREATE / CREATE, même
triplet (threads et processus), CREATE / UPDATE, UPDATE / UPDATE effectifs
(un succès, un conflit), no-op / no-op, UPDATE / DELETE, DELETE / rejeu,
deux comptes indépendants, mutation de profil PR 6, résolution de session,
révocation (avant, pendant, après), connexion OTP, liaison e-mail,
lecteurs F2, expiration pendant l'attente du profil, de la session, de la
génération, du reçu et de la position, et l'écriture historique (limite
explicite de la frontière F2), puis une charge mixte.
"""
import multiprocessing
import threading
import time
import uuid

import pytest
import sqlalchemy as sa

from core.auth_f2.service import F2AuthService
from core.authz_f2 import commands as c
from core.authz_f2.runner import AUTHORIZED, NOT_FOUND, REVISION_CONFLICT, SESSION_INVALID
from tests.authz_f2_portfolio_worker import worker
from tests.authz_f2_support import (
    BOB,
    USER_A,
    USER_B,
    AuthzEnv,
    create_command,
    delete_command,
    replace_command,
)
from tests.f2_support import ISSUER, FakeMailer, FakeSupabase, prepare_schema, scalar
from tests.test_authz_f2_concurrency import CONCURRENT_APP, Background, Blocker, Pause, concurrent_f2, deadlocks, \
    wait_until, waiting
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

CTX = multiprocessing.get_context("spawn")
IDLE = lambda point: None  # noqa: E731 — runner sans point d'interruption


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
def sessions(env):
    return env.confirmed(), env.confirmed("alice2@example.com", USER_A)


def positions_of(env, user_id=USER_A):
    return env.count("portfolio_positions", "user_id = :u", u=user_id)


def held_then_contended(env, first, second, first_command, second_command, point="executed"):
    """`first` s'arrête à `point` en détenant ses verrous ; `second` doit
    réellement attendre ; puis relâchement. (résultat 1, résultat 2)."""
    pause = Pause(point)
    holder = Background(lambda: env.runner(hook=pause).run(first.credential(), first_command))
    holder.start()
    assert pause.reached.wait(10)
    contender = Background(lambda: env.runner(hook=IDLE).run(second.credential(), second_command))
    contender.start()
    try:
        wait_until(lambda: env.waiting_on_lock() == 1, message="le second n'attend pas")
    finally:
        pause.release.set()
    return holder.outcome(), contender.outcome()


# --------------------------------------------------------------------------
# PR 7 contre PR 7
# --------------------------------------------------------------------------

def test_two_creates_with_distinct_operation_ids_both_create(env, sessions):
    won, other = held_then_contended(env, *sessions, create_command(), create_command())
    assert won.status == other.status == AUTHORIZED and won.value.position_id != other.value.position_id
    assert positions_of(env) == 2 and len(env.receipts()) == 2


def test_same_triplet_in_two_sessions_creates_once_and_replays(env, sessions):
    command = create_command()
    won, replay = held_then_contended(env, *sessions, command, command)
    assert won.status == replay.status == AUTHORIZED
    assert replay.value.body == won.value.body and positions_of(env) == 1 and len(env.receipts()) == 1


def test_same_triplet_in_independent_processes_creates_once(env, pg_url):  # noqa: F811
    issued = env.confirmed()
    operation_id = str(uuid.uuid4())
    count = 4
    barrier, queue = CTX.Barrier(count), CTX.Queue()
    args = {"generation_id": str(issued.generation_id), "secret": issued.active_secret.reveal(),
            "operation_id": operation_id, "kind": "create"}
    processes = [CTX.Process(target=worker, args=(pg_url, args, barrier, queue)) for _ in range(count)]
    for process in processes:
        process.start()
    results = [queue.get(timeout=180) for _ in processes]
    for process in processes:
        process.join(60)
    assert all(r[0] == "ok" for r in results), results
    assert {r[1] for r in results} == {AUTHORIZED}
    assert len({r[2] for r in results}) == 1  # même corps canonique pour tous
    pids = [set(r[3]) for r in results]
    assert all(pids) and all(not (a & b) for i, a in enumerate(pids) for b in pids[i + 1:])
    assert positions_of(env) == 1 and len(env.receipts()) == 1


@pytest.mark.parametrize("first_is_create", [True, False])
def test_create_and_update_of_another_position_both_succeed(env, sessions, first_is_create):
    target = env.insert_position(quantity=4.0)
    commands = [create_command(), replace_command(target, 0, quantity="5", purchase_price=None, target_percent=None)]
    if not first_is_create:
        commands.reverse()
    first, second = held_then_contended(env, *sessions, *commands)
    assert first.status == second.status == AUTHORIZED
    assert positions_of(env) == 2 and env.position(target)["position_revision"] == 1


@pytest.mark.parametrize("first_wins", [0, 1])
def test_two_effective_updates_from_one_revision_one_success_one_conflict(env, sessions, first_wins):
    target = env.insert_position(revision=7, quantity=4.0, purchase_price=100.0, target_percent=10.0)
    first, second = sessions[first_wins], sessions[1 - first_wins]
    won, lost = held_then_contended(env, first, second, replace_command(target, 7, quantity="5"),
                                    replace_command(target, 7, quantity="6"))
    assert won.status == AUTHORIZED and won.value.view.position_revision == 8
    assert lost.status == REVISION_CONFLICT and lost.value is None
    row = env.position(target)
    assert (row["q"], row["position_revision"]) == (5.0, 8) and len(env.receipts()) == 1


def test_two_noops_with_distinct_operation_ids_both_succeed(env, sessions):
    target = env.insert_position(revision=2, quantity=4.0, purchase_price=100.0, target_percent=10.0)
    before = env.position(target)
    a, b = held_then_contended(env, *sessions, replace_command(target, 2, quantity="4"),
                               replace_command(target, 2, quantity="4"))
    assert a.status == b.status == AUTHORIZED
    assert a.value.view.position_revision == b.value.view.position_revision == 2
    assert env.position(target) == before and len(env.receipts()) == 2


@pytest.mark.parametrize("delete_first", [True, False])
def test_update_against_delete(env, sessions, delete_first):
    target = env.insert_position(quantity=4.0, purchase_price=100.0, target_percent=10.0)
    update, delete = replace_command(target, 0, quantity="5"), delete_command(target, 0)
    if delete_first:
        deleted, updated = held_then_contended(env, *sessions, delete, update)
        assert deleted.status == AUTHORIZED and updated.status == NOT_FOUND  # suppression gagnante
        assert env.position(target) is None
    else:
        updated, deleted = held_then_contended(env, *sessions, update, delete)
        assert updated.status == AUTHORIZED and deleted.status == REVISION_CONFLICT
        assert env.position(target)["position_revision"] == 1


def test_delete_against_its_own_replay(env, sessions):
    target = env.insert_position(revision=3)
    command = delete_command(target, 3)
    won, replay = held_then_contended(env, *sessions, command, command)
    assert won.status == replay.status == AUTHORIZED and replay.value.body == won.value.body
    assert env.position(target) is None and len(env.receipts()) == 1


def test_independent_accounts_do_not_wait_for_each_other(env):
    a, b = env.confirmed(), env.confirmed(BOB, USER_B)
    pause = Pause("executed")
    holder = Background(lambda: env.runner(hook=pause).run(a.credential(), create_command()))
    holder.start()
    try:
        assert pause.reached.wait(10)
        started = time.monotonic()
        other = env.runner(hook=IDLE).run(b.credential(), create_command())
        assert time.monotonic() - started < 3 and env.waiting_on_lock() == 0
        assert other.status == AUTHORIZED
    finally:
        pause.release.set()
    assert holder.outcome().status == AUTHORIZED
    assert positions_of(env, USER_A) == positions_of(env, USER_B) == 1


# --------------------------------------------------------------------------
# Expiration et révocation pendant les attentes (BF2)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("table", ["users", "oryx_sessions", "oryx_session_generations", "receipt", "position"])
def test_expiry_while_waiting_is_judged_at_t_authorization(env, table):
    issued = env.confirmed()
    target = env.insert_position(quantity=4.0, purchase_price=100.0, target_percent=10.0)
    op = uuid.uuid4()
    if table == "receipt":  # un reçu existant (rejeu) verrouillé par un tiers
        assert env.runner().run(issued.credential(), replace_command(target, 0, op, quantity="5")).status == \
            AUTHORIZED
        env.hook_calls.clear()
    before = (env.position(target), env.receipts())
    sql = {"users": "SELECT id FROM users WHERE id = :u FOR UPDATE",
           "oryx_sessions": "SELECT id FROM oryx_sessions WHERE id = :s FOR UPDATE",
           "oryx_session_generations": "SELECT id FROM oryx_session_generations WHERE id = :g FOR UPDATE",
           "receipt": "SELECT 1 FROM portfolio_write_idempotency WHERE operation_id = :o FOR UPDATE",
           "position": "SELECT id FROM portfolio_positions WHERE id = :i FOR UPDATE"}[table]
    env.expire_at(issued.session_id, "clock_timestamp() + interval '1200 milliseconds'")
    blocker = Blocker(env, sql, u=USER_A, s=issued.session_id, g=issued.generation_id, o=op, i=target)
    try:
        actor = Background(lambda: env.runner().run(issued.credential(), replace_command(target, 0, op,
                                                                                         quantity="5")))
        actor.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message=f"le runner n'attend pas {table}")
        assert env.hook_calls == ["prevalidated"]  # attente avant T_authorization
        wait_until(lambda: scalar(env.observer, "SELECT absolute_expires_at <= clock_timestamp() FROM oryx_sessions "
                                                "WHERE id = :s", s=issued.session_id), timeout=3.0)
        assert actor.is_alive()
    finally:
        blocker.release()
    assert actor.outcome().status == SESSION_INVALID  # même pour un rejeu : aucune restitution
    assert (env.position(target), env.receipts()) == before
    assert env.hook_calls == ["prevalidated", "locked"]


def test_revocation_between_phases_and_while_waiting_for_the_position(env):
    issued = env.confirmed()
    target = env.insert_position(quantity=4.0)
    env.hooks["prevalidated"] = lambda: env.service.revoke_session(issued.session_id, "security")
    assert env.runner().run(issued.credential(), delete_command(target, 0)).status == SESSION_INVALID
    env.hooks.clear()
    second = env.confirmed("alice2@example.com", USER_A)
    blocker = Blocker(env, "SELECT id FROM portfolio_positions WHERE id = :i FOR UPDATE", i=target)
    try:
        actor = Background(lambda: env.runner().run(second.credential(), delete_command(target, 0)))
        actor.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="le runner n'attend pas la position")
        # Session déjà verrouillée FOR SHARE par le runner : la révocation
        # (oryx_sessions FOR UPDATE) attend son COMMIT.
        revocation = Background(lambda: env.service.revoke_session(second.session_id, "security"))
        revocation.start()
        time.sleep(0.3)
        assert revocation.is_alive()
    finally:
        blocker.release()
    assert actor.outcome().status == AUTHORIZED  # autorisée avant la révocation, qui attendait
    assert revocation.outcome() is True
    assert env.position(target) is None
    assert env.runner().run(second.credential(), c.ListPositions()).status == SESSION_INVALID


def test_revocation_after_authorization_waits_for_the_commit(env, pg_url):  # noqa: F811
    issued = env.confirmed()
    engine, f2, _, _ = concurrent_f2(env, pg_url)
    revocation = Background(lambda: f2.revoke_session(issued.session_id, "security"))

    def during_receipt():
        revocation.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 1, message="la révocation n'attend pas")

    env.hooks["receipt_written"] = during_receipt
    command = create_command()
    try:
        assert env.runner().run(issued.credential(), command).status == AUTHORIZED
        assert revocation.outcome() is True
    finally:
        engine.dispose()
    env.hooks.clear()
    assert positions_of(env) == 1
    assert env.runner().run(issued.credential(), command).status == SESSION_INVALID  # rejeu : même contrôle


# --------------------------------------------------------------------------
# PR 7 contre PR 2 à PR 6 et contre l'historique
# --------------------------------------------------------------------------

@pytest.mark.parametrize("profile_first", [True, False])
def test_profile_and_portfolio_mutations_in_both_orders(env, sessions, profile_first):
    target = env.insert_position(quantity=4.0, purchase_price=100.0, target_percent=10.0)
    profile, portfolio = c.UpdateProfileLevel("avance", 0), replace_command(target, 0, quantity="5")
    commands = (profile, portfolio) if profile_first else (portfolio, profile)
    first, second = held_then_contended(env, *sessions, *commands)
    assert first.status == second.status == AUTHORIZED
    assert env.profile()[:2] == ("avance", 1) and env.position(target)["position_revision"] == 1


def test_f2_resolution_and_otp_sign_in_wait_behind_a_portfolio_mutation(env, pg_url):  # noqa: F811
    issued = env.confirmed()
    engine, f2, supabase, mailer = concurrent_f2(env, pg_url)
    supabase.subjects["alice7@example.com"] = "00000000-0000-4000-8000-0000000a1107"
    with env.observer.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, verified_email_normalized, "
            "email_verified_at, created_at) VALUES (gen_random_uuid(), :u, 'supabase', :i, :s, :e, now(), now())"),
            {"u": USER_A, "i": ISSUER, "s": supabase.subject_for("alice7@example.com"), "e": "alice7@example.com"})
    start = f2.start_sign_in(email="alice7@example.com", client_ip="203.0.113.81")
    assert start.status == "issued"
    actors = {
        "resolve": Background(lambda: f2.authenticate_session(issued.credential())),
        "sign_in": Background(lambda: f2.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                                       code=mailer.last_code("alice7@example.com"),
                                                       client_ip="203.0.113.81")),
    }

    def during_receipt():
        for actor in actors.values():
            actor.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 2, message="les transactions F2 n'attendent pas")

    env.hooks["receipt_written"] = during_receipt
    try:
        assert env.runner().run(issued.credential(), create_command()).status == AUTHORIZED
        assert actors["resolve"].outcome().user_id == USER_A
        assert actors["sign_in"].outcome().status == "authenticated"
    finally:
        engine.dispose()
    assert positions_of(env) == 1


def test_portfolio_mutation_waits_behind_an_otp_finalization_holding_the_user_rank(env, pg_url):  # noqa: F811
    issued = env.confirmed()
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})
    supabase, mailer = FakeSupabase(), FakeMailer()
    paused, resume = threading.Event(), threading.Event()

    def f2_hook(point):
        if point == "verify.finalizing":
            paused.set()
            assert resume.wait(10)

    f2 = F2AuthService(engine, env.keyring, supabase, mailer, fault_hook=f2_hook)
    supabase.subjects["alice8@example.com"] = "00000000-0000-4000-8000-0000000a1108"
    with env.observer.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, verified_email_normalized, "
            "email_verified_at, created_at) VALUES (gen_random_uuid(), :u, 'supabase', :i, :s, :e, now(), now())"),
            {"u": USER_A, "i": ISSUER, "s": supabase.subject_for("alice8@example.com"), "e": "alice8@example.com"})
    start = f2.start_sign_in(email="alice8@example.com", client_ip="203.0.113.82")
    sign_in = Background(lambda: f2.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                                  code=mailer.last_code("alice8@example.com"),
                                                  client_ip="203.0.113.82"))
    try:
        sign_in.start()
        assert paused.wait(10)
        mutation = Background(lambda: env.runner().run(issued.credential(), create_command()))
        mutation.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="la mutation n'attend pas le rang users")
        resume.set()
        assert sign_in.outcome().status == "authenticated"
        assert mutation.outcome().status == AUTHORIZED
    finally:
        resume.set()
        engine.dispose()
    assert positions_of(env) == 1


@pytest.mark.parametrize("linking_first", [True, False])
def test_email_linking_finalization_and_portfolio_mutation_in_both_orders(env, pg_url, linking_first):  # noqa: F811
    """Liaison e-mail (PR 4) : sa finalisation prend users FOR UPDATE du
    profil invité, comme PORTFOLIO_MUTATION. Aucun cycle dans les deux ordres."""
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})
    supabase, mailer = FakeSupabase(), FakeMailer()
    paused, resume = threading.Event(), threading.Event()

    def f2_hook(point):
        if point == "verify.finalizing" and linking_first:
            paused.set()
            assert resume.wait(10)

    f2 = F2AuthService(engine, env.keyring, supabase, mailer, fault_hook=f2_hook)
    token = env.service.create_invitation(cohort="pr7").token
    begin = env.service.begin_invitation_activation(token=token)
    session = env.service.complete_invitation_activation(operation_id=begin.operation_id,
                                                         candidate=begin.candidate).session
    assert env.service.authenticate_session(session.credential()) is not None
    user_id = scalar(env.observer, "SELECT user_id FROM oryx_sessions WHERE id = :s", s=session.session_id)
    supabase.subjects["linked@example.com"] = "00000000-0000-4000-8000-0000000a1109"
    linking = f2.start_email_linking(session=session.credential(), email="linked@example.com",
                                     client_ip="203.0.113.83")
    assert linking.status == "issued", linking
    verify = Background(lambda: f2.verify_email_linking(operation_id=linking.operation_id, session=session.credential(),
                                                        code=mailer.last_code("linked@example.com"),
                                                        client_ip="203.0.113.83"))
    try:
        if linking_first:
            verify.start()
            assert paused.wait(10)
            mutation = Background(lambda: env.runner(hook=IDLE).run(session.credential(), create_command()))
            mutation.start()
            wait_until(lambda: env.waiting_on_lock() == 1, message="la mutation n'attend pas la liaison")
            resume.set()
            assert verify.outcome().status == "email_linked"
            assert mutation.outcome().status == AUTHORIZED
        else:
            def during_receipt():
                verify.start()
                wait_until(lambda: waiting(env, CONCURRENT_APP) == 1, message="la liaison n'attend pas")

            env.hooks["receipt_written"] = during_receipt
            assert env.runner().run(session.credential(), create_command()).status == AUTHORIZED
            assert verify.outcome().status == "email_linked"
    finally:
        resume.set()
        engine.dispose()
    assert positions_of(env, user_id) == 1
    assert env.count("auth_identities", "user_id = :u", u=user_id) == 1


def test_f2_readers_wait_only_behind_the_user_rank(env, sessions):
    target = env.insert_position(quantity=4.0)
    a1, a2 = sessions
    pause = Pause("receipt_written")
    holder = Background(lambda: env.runner(hook=pause).run(a1.credential(), delete_command(target, 0)))
    holder.start()
    assert pause.reached.wait(10)
    # Plan READ : users FOR KEY SHARE attend la mutation (users FOR UPDATE), borné par lock_timeout.
    reader = Background(lambda: env.runner(hook=IDLE).run(a2.credential(), c.ReadPosition(target)))
    reader.start()
    try:
        wait_until(lambda: env.waiting_on_lock() == 1, message="la lecture n'attend pas")
    finally:
        pause.release.set()
    assert holder.outcome().status == AUTHORIZED
    assert reader.outcome().status == NOT_FOUND  # relue après le COMMIT : suppression visible


def test_legacy_writers_are_outside_the_f2_boundary(env, pg_url):  # noqa: F811
    """Contre-exemple explicite : l'insertion historique (FK users -> KEY
    SHARE) attend la mutation PR 7, mais l'UPDATE historique d'une autre
    ligne ne l'attend pas et n'avance aucune révision."""
    issued = env.confirmed()
    target = env.insert_position(quantity=4.0)
    other = env.insert_position(quantity=7.0)
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args={"application_name": CONCURRENT_APP})

    def legacy_insert():
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO portfolio_positions (user_id, ticker, quantity) VALUES (:u, 'MC.PA', 1)"),
                         {"u": USER_A})

    def legacy_update():
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE portfolio_positions SET quantity = 8 WHERE id = :i"), {"i": other})

    inserter = Background(legacy_insert)

    def during_receipt():
        inserter.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 1, message="l'insertion historique n'attend pas")
        started = time.monotonic()
        legacy_update()  # aucune attente : hors frontière F2
        assert time.monotonic() - started < 3

    env.hooks["receipt_written"] = during_receipt
    try:
        assert env.runner().run(issued.credential(), replace_command(target, 0, quantity="5", purchase_price=None,
                                                                     target_percent=None)).status == AUTHORIZED
        inserter.outcome()
    finally:
        engine.dispose()
    assert env.position(other)["q"] == 8.0 and env.position(other)["position_revision"] == 0
    assert positions_of(env) == 3


def test_mixed_load_produces_no_deadlock_and_only_closed_outcomes(env, sessions, pg_url):  # noqa: F811
    a1, a2 = sessions
    target = env.insert_position(quantity=1.0, purchase_price=None, target_percent=None)
    engine, f2, _, _ = concurrent_f2(env, pg_url)
    barrier = threading.Barrier(6)
    runner = env.runner(hook=IDLE)

    def toggle(issued):
        barrier.wait(10)
        out = []
        for _ in range(10):
            current = runner.run(issued.credential(), c.ReadPosition(target))
            assert current.status == AUTHORIZED
            quantity = "2" if current.value.quantity == "1" else "1"
            result = runner.run(issued.credential(), replace_command(target, current.value.position_revision,
                                                                     quantity=quantity, purchase_price=None,
                                                                     target_percent=None))
            out.append(result.status)
        return out

    def creates():
        barrier.wait(10)
        statuses = []
        for _ in range(10):
            command = create_command()
            statuses += [runner.run(a1.credential(), command).status, runner.run(a2.credential(), command).status]
        return statuses

    def profile():
        barrier.wait(10)
        out = []
        for _ in range(10):
            current = runner.run(a2.credential(), c.ReadProfile()).value
            level = "avance" if current.level == "debutant" else "debutant"
            out.append(runner.run(a2.credential(), c.UpdateProfileLevel(level, current.level_revision)).status)
        return out

    def resolve():
        barrier.wait(10)
        return ["ok" if f2.authenticate_session(a1.credential()) else "none" for _ in range(10)]

    def reads():
        barrier.wait(10)
        return [runner.run(a2.credential(), c.ListPositions()).status for _ in range(10)]

    actors = [Background(fn) for fn in (lambda: toggle(a1), lambda: toggle(a2), creates, profile, resolve, reads)]
    try:
        for actor in actors:
            actor.start()
        results = [actor.outcome(timeout=180) for actor in actors]
    finally:
        engine.dispose()
    toggles = results[0] + results[1]
    assert set(toggles) <= {AUTHORIZED, REVISION_CONFLICT}
    assert results[2] == [AUTHORIZED] * 20 and set(results[3]) <= {AUTHORIZED, REVISION_CONFLICT}
    assert results[4] == ["ok"] * 10 and results[5] == [AUTHORIZED] * 10
    increments = env.position(target)["position_revision"]
    assert increments == sum(1 for s in toggles if s == AUTHORIZED)  # chaque succès : exactement +1
    assert positions_of(env) == 11 and len(env.receipts()) == 10 + increments
