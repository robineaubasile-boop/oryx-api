"""Concurrence réelle des lectures R1 PR 8 (BF2, plan READ inchangé).

VRAIES connexions PostgreSQL concurrentes (threads, une connexion par
acteur), points d'interruption déterministes du runner (Pause : la
transaction de phase B garde ses verrous jusqu'au relâchement) et attentes
constatées dans pg_stat_activity / pg_locks, jamais de simples sleep pour
synchroniser. Aucun interblocage toléré (pg_stat_database.deadlocks).

Couvre : GET pendant la création d'une thèse par le VRAI chemin Décrypter
(apply_construction_these_progress, session verrouillée FOR UPDATE), pendant
la suppression historique (DELETE /api/user/{user_id}/theses/{ticker}),
pendant un changement de statut d'AnalysisSession et pendant un ACK de
livraison (lock_delivery_for_ack + acknowledge_delivery), dans les deux
sens (écrivain ouvert pendant la lecture, lecture ouverte pendant
l'écrivain) ; aucun verrou de ligne métier pris par une lecture ; instantané
cohérent PAR INSTRUCTION sous bascules concurrentes ; expiration et
révocation pendant l'attente du verrou users ; révocation concurrente
d'une lecture autorisée ; charge mixte sans interblocage ni refus parasite.
"""
import threading
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core.assistant_delivery import acknowledge_delivery, lock_delivery_for_ack
from core.authz_f2 import commands as c
from core.authz_f2.runner import AUTHORIZED, NOT_FOUND, SESSION_INVALID
from core.decryptage_progress import apply_construction_these_progress
from tests.authz_f2_consultation_support import insert_session, insert_thesis
from tests.authz_f2_support import USER_A, AuthzEnv, replace_command
from tests.f2_support import APP_NAME, prepare_schema, rows, scalar
from tests.test_authz_f2_concurrency import CONCURRENT_APP, Background, Blocker, Pause, concurrent_f2, deadlocks, \
    wait_until, waiting
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

OVERVIEW = c.ListAnalysisOverviewPage(50, None)
THESES = c.ListThesesPage(50, None, None, None)


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
    return env.confirmed()


def read(env, issued, command, hook=None):
    return env.runner(hook=hook).run(issued.credential(), command)


def overview(env, issued):
    outcome = read(env, issued, OVERVIEW)
    assert outcome.status == AUTHORIZED, outcome
    return {e.ticker: e for e in outcome.value.entries}


def thesis_ids(env, issued):
    outcome = read(env, issued, THESES)
    assert outcome.status == AUTHORIZED, outcome
    return [t.thesis_id for t in outcome.value.entries]


class OpenWriter:
    """Transaction applicative ouverte sur une connexion tierce (ORM),
    committée ou annulée explicitement."""

    def __init__(self, env):
        self.conn = env.observer.connect()
        self.tx = self.conn.begin()
        self.db = Session(bind=self.conn)

    def commit(self):
        self.db.flush()
        self.tx.commit()
        self.close()

    def close(self):
        self.db.close()
        self.conn.close()


def decryptage_final_turn(db, ticker="AIR.PA"):
    """Tour swot_final accepté par le VRAI chemin Décrypter : session
    in_progress (risques) relue FOR UPDATE, completed, thèse insérée."""
    return apply_construction_these_progress(db, user_id=USER_A, ticker=ticker, step="swot_final",
                                             thesis_text="Thèse finale.", data=None, user_contribution=True,
                                             answered_step="risques")


def historical_delete(db, ticker="AIR.PA"):
    """Effet exact de DELETE /api/user/{user_id}/theses/{ticker} (api.py)."""
    db.execute(sa.text("DELETE FROM investment_theses WHERE user_id = :u AND ticker = :t"), {"u": USER_A, "t": ticker})
    db.execute(sa.text("UPDATE analysis_sessions SET status = 'abandoned', updated_at = now() WHERE user_id = :u "
                       "AND ticker = :t AND status = 'in_progress'"), {"u": USER_A, "t": ticker})


def runner_pid(env):
    return scalar(env.observer, "SELECT pid FROM pg_stat_activity WHERE application_name = :a "
                                "AND state = 'idle in transaction'", a=APP_NAME)


# --------------------------------------------------------------------------
# Décrypter : création de thèse
# --------------------------------------------------------------------------

def test_get_while_decryptage_creates_a_thesis(env, alice):
    session = insert_session(env, USER_A, "AIR.PA", current_step="risques")
    writer = OpenWriter(env)
    try:
        outcome = decryptage_final_turn(writer.db)
        assert outcome.analysis_session.status == "completed"
        writer.db.flush()  # session FOR UPDATE, UPDATE et INSERT non committés
        # Lecture pendant l'écrivain : jamais bloquée, état committé antérieur.
        entry = overview(env, alice)["AIR.PA"]
        assert (entry.state, entry.analysis_session_id, entry.current_step) == (c.IN_PROGRESS, session, "risques")
        assert thesis_ids(env, alice) == []
        assert env.waiting_on_lock() == 0
        writer.commit()
    except BaseException:
        writer.close()
        raise
    entry = overview(env, alice)["AIR.PA"]
    (thesis,) = thesis_ids(env, alice)
    assert (entry.state, entry.latest_thesis_id) == (c.THESIS_AVAILABLE, thesis)
    assert read(env, alice, c.ReadThesis(thesis)).value.analysis_session_id == session  # provenance vérifiée


def test_decryptage_commits_while_a_read_is_authorized_and_the_read_sees_it(env, alice):
    """Lecture arrêtée APRÈS l'autorisation (users, session, génération
    verrouillés) : le chemin Décrypter n'attend pas et committe ; la lecture
    SQL métier, exécutée ensuite, voit l'état committé (READ COMMITTED)."""
    insert_session(env, USER_A, "AIR.PA", current_step="risques")
    pause = Pause("authorized")
    reader = Background(lambda: read(env, alice, OVERVIEW, hook=pause))
    reader.start()
    try:
        assert pause.reached.wait(10)
        writer = OpenWriter(env)
        decryptage_final_turn(writer.db)
        writer.commit()  # aucune attente derrière la lecture autorisée
    finally:
        pause.release.set()
    outcome = reader.outcome()
    assert outcome.status == AUTHORIZED
    (entry,) = outcome.value.entries
    assert entry.state == c.THESIS_AVAILABLE


# --------------------------------------------------------------------------
# Suppression historique et changement de statut
# --------------------------------------------------------------------------

def test_get_while_the_historical_delete_is_open(env, alice):
    thesis = insert_thesis(env, USER_A, "AIR.PA")
    insert_session(env, USER_A, "AIR.PA")
    writer = OpenWriter(env)
    try:
        historical_delete(writer.db)
        assert overview(env, alice)["AIR.PA"].state == c.IN_PROGRESS
        assert thesis_ids(env, alice) == [thesis]
        assert read(env, alice, c.ReadThesis(thesis)).status == AUTHORIZED
        writer.commit()
    except BaseException:
        writer.close()
        raise
    assert overview(env, alice) == {}  # thèses supprimées, session abandoned
    assert read(env, alice, c.ReadThesis(thesis)).status == NOT_FOUND


def test_status_change_while_a_read_holds_its_transaction(env, alice):
    """Lecture arrêtée APRÈS son instruction métier, transaction ouverte :
    aucun verrou de ligne métier, l'écrivain passe immédiatement ; la
    lecture rend l'instantané de SON instruction."""
    session = insert_session(env, USER_A, "AIR.PA", current_step="moat")
    thesis = insert_thesis(env, USER_A, "AIR.PA")
    pause = Pause("executed")
    reader = Background(lambda: read(env, alice, OVERVIEW, hook=pause))
    reader.start()
    try:
        assert pause.reached.wait(10)
        pid = runner_pid(env)
        locks = rows(env.observer, "SELECT l.locktype, c.relname, l.mode FROM pg_locks l LEFT JOIN pg_class c "
                                   "ON c.oid = l.relation WHERE l.pid = :p AND l.granted", p=pid)
        business = {(r["relname"], r["mode"]) for r in locks
                    if r["relname"] in ("investment_theses", "analysis_sessions")}
        assert business <= {("investment_theses", "AccessShareLock"), ("analysis_sessions", "AccessShareLock")}
        assert not [r for r in locks if r["locktype"] == "tuple"]
        with env.observer.begin() as conn:  # FOR UPDATE NOWAIT : aucun verrou de ligne détenu par la lecture
            conn.execute(sa.text("SELECT id FROM analysis_sessions WHERE id = :s FOR UPDATE NOWAIT"), {"s": session})
            conn.execute(sa.text("SELECT id FROM investment_theses WHERE id = :t FOR UPDATE NOWAIT"), {"t": thesis})
            conn.execute(sa.text("UPDATE analysis_sessions SET status = 'abandoned' WHERE id = :s"), {"s": session})
    finally:
        pause.release.set()
    outcome = reader.outcome()
    assert outcome.value.entries[0].state == c.IN_PROGRESS  # instantané pris au début de l'instruction
    assert overview(env, alice)["AIR.PA"].state == c.THESIS_AVAILABLE


# --------------------------------------------------------------------------
# ACK
# --------------------------------------------------------------------------

def _delivery(env):
    """Livraison pending de A, au schéma courant (ACK réel)."""
    ids = env.seed_resources()
    with env.observer.begin() as conn:
        conn.execute(sa.text("UPDATE assistant_deliveries SET delivery_schema_version = 'assistant-delivery-v1' "
                             "WHERE id = :i"), {"i": ids[(USER_A, "delivery")]})
    return ids[(USER_A, "delivery")], ids[(USER_A, "conversation")]


def test_get_while_an_ack_is_open_and_ack_while_a_read_is_open(env, alice):
    turn, key = _delivery(env)
    thesis = insert_thesis(env, USER_A, "MC.PA")
    writer = OpenWriter(env)
    try:
        lock_delivery_for_ack(writer.db, assistant_turn_id=turn, user_id=USER_A, conversation_key=key)
        acknowledge_delivery(writer.db, assistant_turn_id=turn, user_id=USER_A, conversation_key=key)
        writer.db.flush()
        for command in (OVERVIEW, THESES, c.ReadThesis(thesis)):
            assert read(env, alice, command).status == AUTHORIZED
        writer.commit()
    except BaseException:
        writer.close()
        raise
    assert scalar(env.observer, "SELECT status FROM assistant_deliveries WHERE id = :i", i=turn) == "delivered"
    pause = Pause("executed")
    reader = Background(lambda: read(env, alice, THESES, hook=pause))
    reader.start()
    try:
        assert pause.reached.wait(10)
        writer = OpenWriter(env)  # ACK rejoué (idempotent) pendant la lecture ouverte : aucune attente
        acknowledge_delivery(writer.db, assistant_turn_id=turn, user_id=USER_A, conversation_key=key)
        writer.commit()
    finally:
        pause.release.set()
    assert reader.outcome().status == AUTHORIZED


# --------------------------------------------------------------------------
# Instantané cohérent par instruction
# --------------------------------------------------------------------------

def test_one_statement_never_mixes_two_snapshots(env, alice):
    """Un écrivain bascule atomiquement, en boucle, le ticker entre « session
    active » et « thèse seule » et déplace une thèse entre deux tickers ;
    chaque lecture (une instruction) voit toujours EXACTEMENT un état
    committé : ticker jamais absent, jamais deux fois, thèse jamais perdue."""
    session = insert_session(env, USER_A, "T")
    moving = insert_thesis(env, USER_A, "X")
    stop = threading.Event()
    errors = []

    def writer():
        flip = False
        try:
            while not stop.is_set():
                flip = not flip
                with env.observer.begin() as conn:
                    if flip:
                        conn.execute(sa.text("UPDATE analysis_sessions SET status = 'completed' WHERE id = :s"),
                                     {"s": session})
                        conn.execute(sa.text("INSERT INTO investment_theses (user_id, ticker, thesis_text) "
                                             "VALUES (:u, 'T', 'b')"), {"u": USER_A})
                        conn.execute(sa.text("UPDATE investment_theses SET ticker = 'Y' WHERE id = :i"), {"i": moving})
                    else:
                        conn.execute(sa.text("DELETE FROM investment_theses WHERE ticker = 'T'"))
                        conn.execute(sa.text("UPDATE analysis_sessions SET status = 'in_progress' WHERE id = :s"),
                                     {"s": session})
                        conn.execute(sa.text("UPDATE investment_theses SET ticker = 'X' WHERE id = :i"), {"i": moving})
        except BaseException as exc:  # noqa: BLE001 — rapporté au test
            errors.append(exc)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    states = set()
    try:
        for _ in range(150):
            view = overview(env, alice)
            assert set(view) in ({"T", "X"}, {"T", "Y"}), view  # thèse déplacée : un seul des deux tickers
            entry = view["T"]
            states.add(entry.state)
            assert (entry.state == c.IN_PROGRESS) == ("X" in view)  # même instant committé pour les deux
            listed = read(env, alice, THESES).value.entries
            assert [t.thesis_id for t in listed].count(moving) == 1
    finally:
        stop.set()
        thread.join(10)
    assert not errors and not thread.is_alive()
    assert states == {c.IN_PROGRESS, c.THESIS_AVAILABLE}


# --------------------------------------------------------------------------
# BF2 inchangé : expiration et révocation pendant l'attente
# --------------------------------------------------------------------------

@pytest.fixture
def statements(env):
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    sa.event.listen(env.engine, "before_cursor_execute", record)
    yield seen
    sa.event.remove(env.engine, "before_cursor_execute", record)


@pytest.mark.parametrize("command", [OVERVIEW, THESES, c.ReadThesis(1)], ids=["overview", "theses", "thesis"])
def test_expiry_while_waiting_for_the_user_lock_is_refused(env, alice, command, statements):
    env.expire_at(alice.session_id, "clock_timestamp() + interval '1200 milliseconds'")
    blocker = Blocker(env, "SELECT id FROM users WHERE id = :u FOR UPDATE", u=USER_A)  # conflit avec FOR KEY SHARE
    try:
        actor = Background(lambda: read(env, alice, command))
        actor.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="la lecture n'attend pas le verrou users")
        assert env.hook_calls == ["prevalidated"]
        wait_until(lambda: scalar(env.observer, "SELECT absolute_expires_at <= clock_timestamp() FROM oryx_sessions "
                                                "WHERE id = :s", s=alice.session_id), timeout=3.0)
        assert actor.is_alive()
    finally:
        blocker.release()
    assert actor.outcome().status == SESSION_INVALID
    assert not [s for s in statements if "investment_theses" in s or "analysis_sessions" in s]


def test_revocation_committed_while_waiting_for_the_user_lock_is_refused(env, alice):
    blocker = Blocker(env, "SELECT id FROM users WHERE id = :u FOR UPDATE", u=USER_A)
    try:
        actor = Background(lambda: read(env, alice, THESES))
        actor.start()
        wait_until(lambda: env.waiting_on_lock() == 1, message="la lecture n'attend pas le verrou users")
        assert env.service.revoke_session(alice.session_id, "security") is True
    finally:
        blocker.release()
    assert actor.outcome().status == SESSION_INVALID


def test_revocation_waits_for_an_authorized_read_then_nothing_more_is_read(env, alice, pg_url):  # noqa: F811
    insert_thesis(env, USER_A, "AIR.PA")
    engine, f2, _, _ = concurrent_f2(env, pg_url)
    revocation = Background(lambda: f2.revoke_session(alice.session_id, "security"))

    def during_authorized():
        revocation.start()
        wait_until(lambda: waiting(env, CONCURRENT_APP) == 1, message="la révocation n'attend pas")
        assert revocation.is_alive()  # bloquée par oryx_sessions FOR SHARE de la lecture

    env.hooks["authorized"] = during_authorized
    try:
        assert read(env, alice, THESES).status == AUTHORIZED
        assert revocation.outcome() is True
    finally:
        engine.dispose()
    env.hooks.clear()
    for command in (OVERVIEW, THESES, c.ReadThesis(1)):
        assert read(env, alice, command).status == SESSION_INVALID


# --------------------------------------------------------------------------
# Charge mixte
# --------------------------------------------------------------------------

def test_mixed_load_without_deadlock_or_spurious_refusal(env, alice):
    ids = env.seed_resources()
    position = ids[(USER_A, "positions")][0]
    insert_session(env, USER_A, "AIR.PA", current_step="risques")
    for index in range(20):
        insert_thesis(env, USER_A, f"T{index % 5}", created_at=None if index % 4 == 0 else "2026-01-01 00:00:00")
    stop = threading.Event()
    failures = []

    def loop(fn):
        def body():
            try:
                while not stop.is_set():
                    fn()
            except BaseException as exc:  # noqa: BLE001
                failures.append(exc)
        return threading.Thread(target=body, daemon=True)

    def reader():
        for command in (OVERVIEW, THESES, c.ListThesesPage(3, "T1", None, None), c.ReadThesis(1)):
            assert read(env, alice, command).status in (AUTHORIZED, NOT_FOUND)

    revision = [0]

    def portfolio():
        outcome = env.runner().run(alice.credential(), replace_command(position, revision[0], quantity=str(
            revision[0] + 2)))
        assert outcome.status == AUTHORIZED, outcome
        revision[0] += 1

    def profile():
        assert env.runner().run(alice.credential(), c.ReadProfile()).status == AUTHORIZED

    def historical_writer():
        with env.observer.begin() as conn:
            conn.execute(sa.text("INSERT INTO investment_theses (user_id, ticker, thesis_text) VALUES (:u, 'W', 'w')"),
                         {"u": USER_A})
            conn.execute(sa.text("DELETE FROM investment_theses WHERE ticker = 'W' AND id % 2 = 0"))

    threads = [loop(reader), loop(reader), loop(portfolio), loop(profile), loop(historical_writer)]
    for thread in threads:
        thread.start()
    try:
        wait_until(lambda: revision[0] >= 15 or failures, timeout=60, message="charge insuffisante")
    finally:
        stop.set()
        for thread in threads:
            thread.join(30)
    assert failures == []
    assert not any(t.is_alive() for t in threads)
    assert uuid.UUID(str(alice.session_id))  # la session reste valide
    assert read(env, alice, THESES).status == AUTHORIZED
