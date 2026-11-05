"""R1 PR 9-C2 — verrous et concurrence réelle du replay et de l'ACK F2, contre
un VRAI PostgreSQL (16 et 18 en CI).

Vraies connexions distinctes (threads, une connexion par acteur),
synchronisation déterministe : points d'interruption du runner (Pause) ou du
parcours historique (LegacyPause) qui gardent la transaction et ses verrous
ouverts, attente CONSTATÉE dans pg_stat_activity (wait_event_type = 'Lock') et
pg_locks, jamais un sleep arbitraire. pg_stat_database.deadlocks est relevé
avant / après chaque test (aucun interblocage toléré) et aucun 503 n'est
accepté là où un succès est attendu.

Matrice : deux ACK C2 du même utilisateur ; ACK C2 contre ACK historique (deux
ordres) ; ACK C2 contre finalisation C1-B (deux ordres) ; replay pendant un
ACK (C2 et historique) ; deux utilisateurs ; ordre réel des verrous mesuré
(journal des instructions) et profil des verrous détenus (sondes NOWAIT).
"""
import logging

import pytest
import sqlalchemy as sa

from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import decryptage_finalization as dfin
from core.authz_f2.runner import AUTHORIZED
from tests.authz_f2_pr9a_support import BOB_EMAIL, LEGACY_APP, USER_A, USER_B, Background, Pause, legacy_ack, \
    wait_until
from tests.authz_f2_pr9b1_support import CONV
from tests.authz_f2_pr9c1b_support import next_turn, schema  # noqa: F401 — fixture
from tests.authz_f2_pr9c2_support import CanonicalEnv
from tests.f2_support import APP_NAME
from tests.test_authz_f2_pr9a_concurrency import LegacyPause, waited_relations, waiting_pids
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture


@pytest.fixture
def env(pg_url, schema, caplog):  # noqa: F811
    caplog.set_level(logging.WARNING, logger="oryx.authz_f2")
    environment = CanonicalEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before, "interblocage PostgreSQL détecté"
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


def ok(outcome):
    assert outcome.status == AUTHORIZED, outcome
    return outcome.value


def no_unavailable(caplog):
    assert "status=unavailable" not in caplog.text, caplog.text


def start_paused(env, issued, command, point="executed", **kwargs):
    pause = Pause(point)
    worker = Background(lambda: env.c2(issued, command, hook=pause, **kwargs))
    worker.start()
    assert pause.reached.wait(15), "point d'interruption non atteint"
    return pause, worker


def single_capture(env, done):
    link = env.link(done.assistant_turn_id)
    assert link["capture_state"] == "captured" and link["response_action"] == "opened_event"
    assert len([e for e in env.events() if e["status"] == "open"]) == 1  # un seul event ouvert par la réponse
    assert env.delivery(done.assistant_turn_id)["status"] == "delivered"


def test_two_c2_acks_of_one_user_are_serialized_on_users_and_capture_once(env, alice, caplog):
    done = env.finalized(alice)
    second = env.confirmed("alice2@example.com", USER_A)
    pause, holder = start_paused(env, alice, done.ack())
    contender = Background(lambda: env.c2(second, done.ack()))
    contender.start()
    wait_until(lambda: env.waiting() == 1, message="le second ACK n'attend pas")
    [waiter] = waiting_pids(env)
    assert waited_relations(env, waiter["pid"]) == {"users"}  # attente au rang users, avant tout verrou métier
    pause.release.set()
    first, other = ok(holder.outcome()), ok(contender.outcome())
    assert first == other == bridge.DeliveryAcknowledged(done.assistant_turn_id, "delivered", "opened_event")
    single_capture(env, done)
    no_unavailable(caplog)


@pytest.mark.parametrize("first", ["c2", "legacy"])
def test_c2_ack_against_legacy_ack_captures_once(env, alice, monkeypatch, caplog, first):
    done = env.finalized(alice)
    if first == "c2":
        pause, holder = start_paused(env, alice, done.ack())
        contender = Background(lambda: legacy_ack(env, CONV, done.assistant_turn_id))
        contender.start()
        wait_until(lambda: env.waiting(LEGACY_APP) == 1, message="l'ACK historique n'attend pas")
        assert waited_relations(env, waiting_pids(env, LEGACY_APP)[0]["pid"]) == {"assistant_deliveries"}
        pause.release.set()
        assert ok(holder.outcome()).response_action == "opened_event"
        assert contender.outcome()["delivery_status"] == "delivered"  # rejeu historique idempotent
    else:
        hold = LegacyPause(monkeypatch, "acknowledge_delivery")  # capture faite, COMMIT historique non encore fait
        holder = Background(lambda: legacy_ack(env, CONV, done.assistant_turn_id))
        holder.start()
        assert hold.reached.wait(15)
        contender = Background(lambda: env.c2(alice, done.ack()))
        contender.start()
        wait_until(lambda: env.waiting() == 1, message="l'ACK C2 n'attend pas")
        # Verrous users et réservation obtenus ; attente sur la livraison tenue
        # par l'ACK historique (lock_owned_delivery, après la preuve).
        assert waited_relations(env, waiting_pids(env)[0]["pid"]) == {"assistant_deliveries"}
        hold.release.set()
        assert holder.outcome()["delivery_status"] == "delivered"
        # Livraison relue delivered + captured sous verrou : attestation et V2
        # revérifiées, capture rejouée sans effet.
        assert ok(contender.outcome()) == bridge.DeliveryAcknowledged(done.assistant_turn_id, "delivered",
                                                                      "opened_event")
    single_capture(env, done)
    no_unavailable(caplog)


@pytest.mark.parametrize("first", ["c2_ack", "c1b_finalization"])
def test_c2_ack_against_a_concurrent_c1b_finalization(env, alice, caplog, first):
    done = env.finalized(alice)
    _, _, candidate = env.generate(alice, next_turn(CONV, question="Ils vendent du luxe.", context="h"))
    if first == "c2_ack":
        pause, holder = start_paused(env, alice, done.ack())
        contender = Background(lambda: env.finalize(alice, candidate))
        contender.start()
        wait_until(lambda: env.waiting() == 1, message="la finalisation n'attend pas")
        assert waited_relations(env, waiting_pids(env)[0]["pid"]) == {"users"}
        pause.release.set()
        assert ok(holder.outcome()).response_action == "opened_event"
        # L'ACK a ancré la conversation après la génération : contexte B2 changé,
        # rien n'est écrit (jamais une finalisation sur un contexte périmé).
        assert type(ok(contender.outcome())) is dfin.FinalizationContextChanged
    else:
        pause = Pause("executed")
        holder = Background(lambda: env.finalize(alice, candidate, hook=pause))
        holder.start()
        assert pause.reached.wait(15)
        contender = Background(lambda: env.c2(alice, done.ack()))
        contender.start()
        wait_until(lambda: env.waiting() == 1, message="l'ACK n'attend pas")
        assert waited_relations(env, waiting_pids(env)[0]["pid"]) == {"users"}
        pause.release.set()
        assert type(ok(holder.outcome())) is dfin.DecryptageFinalized
        assert ok(contender.outcome()).response_action == "opened_event"
    single_capture(env, done)
    no_unavailable(caplog)


@pytest.mark.parametrize("holder_kind", ["c2", "legacy"])
def test_a_replay_during_an_ack_reads_a_consistent_snapshot_without_waiting(env, alice, monkeypatch, caplog,
                                                                             holder_kind):
    done = env.finalized(alice)
    before = ok(env.c2(alice, done.replay()))
    if holder_kind == "c2":
        pause, holder = start_paused(env, alice, done.ack())
        release = pause.release
    else:
        hold = LegacyPause(monkeypatch, "acknowledge_delivery")
        holder = Background(lambda: legacy_ack(env, CONV, done.assistant_turn_id))
        holder.start()
        assert hold.reached.wait(15)
        release = hold.release
    try:
        during = ok(env.c2(alice, done.replay()))  # aucune attente : lecture sans verrou métier
        assert env.waiting() == 0
    finally:
        release.set()
    holder.outcome()
    after = ok(env.c2(alice, done.replay()))
    assert (during.delivery_status, during.payload_json) == ("pending", before.payload_json)  # instantané committé
    assert (after.delivery_status, after.payload_json) == ("delivered", before.payload_json)
    no_unavailable(caplog)


def test_two_users_are_never_serialized(env, alice, caplog):
    bob = env.confirmed(BOB_EMAIL, USER_B)
    mine, theirs = env.finalized(alice), env.finalized(bob, conversation_key="conv-bob")
    pause, holder = start_paused(env, alice, mine.ack())
    try:
        assert ok(env.c2(bob, theirs.ack())).response_action == "opened_event"  # aboutit pendant la pause
        assert ok(env.c2(bob, theirs.replay())).delivery_status == "delivered"
        assert env.waiting() == 0
    finally:
        pause.release.set()
    ok(holder.outcome())
    no_unavailable(caplog)


def test_measured_lock_order_of_a_c2_ack(env, alice):
    """Ordre RÉEL des instructions verrouillantes d'un premier ACK C2 :
    users -> session -> génération -> réservation -> (preuve sans verrou) ->
    livraison -> lien -> AnalysisSession -> events (ordre de la capture
    historique, inchangé)."""
    done = env.finalized(alice)
    log = []
    sa.event.listen(env.engine, "before_cursor_execute",
                    lambda conn, cur, st, *a: log.append(" ".join(st.split())))
    ok(env.c2(alice, done.ack()))
    locking = [s for s in log if " FOR " in s and ("UPDATE" in s.split(" FOR ", 1)[1] or
                                                   "SHARE" in s.split(" FOR ", 1)[1])]

    def first(fragment):
        return next(i for i, s in enumerate(locking) if fragment in s)

    order = [first("FROM users WHERE id = "), first("oryx_sessions"), first("oryx_session_generations"),
             first("FROM decryptage_turn_reservations WHERE id = "), first("FROM assistant_deliveries"),
             first("FROM decryptage_cognitive_links"), first("FROM analysis_sessions"),
             first("FROM cognitive_events")]
    assert order == sorted(order), locking
    assert "FOR NO KEY UPDATE" in locking[order[0]]
    proof = next(i for i, s in enumerate(log) if s.startswith("WITH o AS"))
    assert log.index(locking[order[3]]) < proof < log.index(locking[order[4]])  # preuve entre réservation et livraison
    assert " FOR " not in log[proof]


def test_measured_lock_profile_while_an_ack_is_paused(env, alice):
    """Pendant un ACK C2 suspendu juste avant le COMMIT : verrous EXCLUSIFS
    sur la réservation propre, la livraison et le lien ; users en FOR NO
    KEY UPDATE (une écriture avec FK vers users n'attend pas)."""
    done = env.finalized(alice)
    pause, holder = start_paused(env, alice, done.ack())
    try:
        probes = {}
        for name, sql, params in (
                ("users_key_share", "SELECT 1 FROM users WHERE id = :u FOR KEY SHARE NOWAIT", {"u": USER_A}),
                ("users_update", "SELECT 1 FROM users WHERE id = :u FOR UPDATE NOWAIT", {"u": USER_A}),
                ("reservation", "SELECT 1 FROM decryptage_turn_reservations WHERE id = :i FOR KEY SHARE NOWAIT",
                 {"i": done.reservation_id}),
                ("delivery", "SELECT 1 FROM assistant_deliveries WHERE id = :i FOR KEY SHARE NOWAIT",
                 {"i": done.assistant_turn_id}),
                ("link", "SELECT 1 FROM decryptage_cognitive_links WHERE assistant_delivery_id = :i "
                         "FOR KEY SHARE NOWAIT", {"i": done.assistant_turn_id})):
            with env.observer.begin() as conn:
                try:
                    conn.execute(sa.text(sql), params)
                    probes[name] = "granted"
                except sa.exc.OperationalError as exc:
                    probes[name] = exc.orig.pgcode
        # FOR UPDATE (réservation, livraison, lien) exclut même FOR KEY SHARE ;
        # FOR NO KEY UPDATE (users) laisse passer FOR KEY SHARE (FK).
        assert probes == {"users_key_share": "granted", "users_update": "55P03", "reservation": "55P03",
                          "delivery": "55P03", "link": "55P03"}
        assert ok(env.c2(alice, done.replay())).delivery_status == "pending"  # le replay n'attend rien
    finally:
        pause.release.set()
    ok(holder.outcome())
    assert env.waiting(APP_NAME) == 0
