"""R1 PR 9-C3 — capacité locale, annulation et concurrence de l'orchestration
HTTP F2 Décrypter, contre un VRAI PostgreSQL (16 et 18 en CI).

1. deux places par instance : troisième requête refusée AVANT B1 ; replay et
   ACK accessibles pendant la saturation ;
2. propriété d'une place : annulation avant le démarrage, course annulation /
   démarrage dans les deux sens, annulation pendant B2 et après la
   finalisation ; place rendue exactement une fois, aucune fuite ;
3. concurrence durable : deux soumissions de la même intention, deux
   compositions indépendantes (deux « processus » : moteurs, runners, places
   distincts) sur la même base — B1 empêche une double attribution active ;
   deux ACK concurrents : une seule capture.
"""
import asyncio
import threading
import time
import uuid

import pytest

from core.authz_f2_http import decryptage as c3
from tests.authz_f2_pr9a_support import Background, wait_until
from tests.authz_f2_pr9b2_support import FakeClaude, FakeEodhd
from tests.authz_f2_pr9c1b_support import schema  # noqa: F401 — fixture
from tests.authz_f2_pr9c3_support import (ACK, REPLAY, TURNS, Gate, HttpEnv, Recorder, body_of, c3_headers,
                                          identity_fields, post, turn_fields)
from tests.authz_f2_support import USER_A, raw_request
from tests.f2_support import service_engine
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = HttpEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


@pytest.fixture
def capacities(monkeypatch):
    return Recorder(monkeypatch, c3._TurnCapacity)


def gated_app(env, gate, **kwargs):
    clock = env.transports()
    env.claude = FakeClaude(clock, actions={"create_message": gate})
    return env.c3_app(**kwargs)


def in_background(app, issued, fields):
    worker = Background(lambda: post(app, TURNS, body_of(fields), c3_headers(issued)))
    worker.start()
    return worker


def ids(response):
    data = response.json()
    return identity_fields(data["reservation_id"], data["assistant_turn_id"])


def asgi_turn(app, issued, fields):
    """Appel ASGI brut (coroutine) d'un POST /turns, pour l'annuler."""
    headers = [("host", "oryx.test")] + list(c3_headers(issued).items())
    body = body_of(fields)

    async def call():
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        sent = []

        async def receive():
            if messages:
                return messages.pop(0)
            await asyncio.Event().wait()  # aucune déconnexion signalée : l'annulation vient de l'appelant

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "https", "path": TURNS, "raw_path": TURNS.encode(), "query_string": b"", "root_path": "",
                 "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
                 "client": ("192.0.2.10", 40000), "server": ("oryx.test", 443)}
        await app(scope, receive, send)
        return sent

    return call()


# --------------------------------------------------------------------------
# 1. Deux places, troisième refusée, replay et ACK pendant la saturation
# --------------------------------------------------------------------------

def test_two_places_a_third_turn_is_refused_before_b1_and_replay_ack_stay_available(env, alice, capacities):
    clock = env.transports()
    app_free = env.c3_app()
    done = post(app_free, TURNS, body_of(turn_fields()), c3_headers(alice))  # livraison antérieure
    assert done.status_code == 200
    gate = Gate()
    env.claude = FakeClaude(clock, actions={"create_message": gate})
    app = env.c3_app()
    capacity = capacities.instances[-1]
    assert capacity.size == c3.MAX_CONCURRENT_TURNS == 2
    # Conversations distinctes : l'ACK ci-dessous ancre CONV, ce qui changerait
    # légitimement le contexte d'un tour en cours sur CONV (409 à C1-B).
    first = in_background(app, alice, turn_fields(conversation_key="conv-c3-one"))
    second = in_background(app, alice, turn_fields(conversation_key="conv-c3-two"))
    wait_until(lambda: gate.count == 2, message="deux orchestrations attendues dans B2")
    assert capacity.in_use == 2
    reservations = env.count("decryptage_turn_reservations")
    env.hook_calls.clear()
    third = post(app, TURNS, body_of(turn_fields()), c3_headers(alice))
    assert third.status_code == 503 and third.json() == {"error": "capacity_unavailable"}
    assert env.hook_calls == [] and env.count("decryptage_turn_reservations") == reservations  # aucun appel B1
    # Replay et ACK ne consomment aucune place.
    replay = post(app, REPLAY, body_of(ids(done)), c3_headers(alice))
    assert replay.status_code == 200 and replay.content == done.content
    acked = post(app, ACK, body_of(ids(done)), c3_headers(alice))
    assert acked.json() == {"assistant_turn_id": done.json()["assistant_turn_id"], "delivery_status": "delivered"}
    assert capacity.in_use == 2
    gate.release.set()
    assert first.outcome().status_code == 200 and second.outcome().status_code == 200
    wait_until(lambda: capacity.in_use == 0)
    assert capacity.released == 2  # chaque place rendue exactement une fois
    assert post(app, TURNS, body_of(turn_fields()), c3_headers(alice)).status_code == 200  # aucune fuite
    assert (capacity.in_use, capacity.released) == (0, 3)


# --------------------------------------------------------------------------
# 2. Propriété d'une place
# --------------------------------------------------------------------------

def test_a_submission_cancelled_before_the_worker_starts_returns_the_place_once(env, alice, capacities,
                                                                                 monkeypatch):
    app = env.c3_app()
    capacity = capacities.instances[-1]
    started = []

    async def never_started(work):
        started.append(work)
        raise asyncio.CancelledError()  # annulation pendant l'attente de l'exécuteur

    monkeypatch.setattr(c3, "_submit", never_started)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asgi_turn(app, alice, turn_fields()))
    assert (capacity.in_use, capacity.released) == (0, 1)
    [work] = started
    assert work() is None  # un démarrage tardif ne fait plus rien
    assert (capacity.in_use, capacity.released) == (0, 1) and env.hook_calls == []
    assert env.count("decryptage_turn_reservations") == 0


def test_cancellation_winning_the_race_against_the_start_does_nothing(env, alice, capacities, monkeypatch):
    app = env.c3_app()
    capacity = capacities.instances[-1]
    late = []

    async def cancelled_then_started(work):
        thread = threading.Thread(target=lambda: late.append(work()))  # démarre APRÈS l'abandon
        late.append(thread)
        raise asyncio.CancelledError()

    monkeypatch.setattr(c3, "_submit", cancelled_then_started)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asgi_turn(app, alice, turn_fields()))
    thread = late[0]
    thread.start()
    thread.join(10)
    assert late[1] is None  # le fil a constaté l'abandon : aucun effet
    assert (capacity.in_use, capacity.released) == (0, 1)
    assert env.hook_calls == [] and env.count("decryptage_turn_reservations") == 0


def test_a_start_winning_the_race_keeps_the_place_until_the_worker_ends(env, alice, capacities, monkeypatch):
    gate = Gate()
    app = gated_app(env, gate)
    capacity = capacities.instances[-1]
    workers = []

    async def started_then_cancelled(work):
        thread = Background(work)
        workers.append(thread)
        thread.start()
        await asyncio.to_thread(gate.reached.wait, 10)  # le fil possède la place (dans B2)
        raise asyncio.CancelledError()

    monkeypatch.setattr(c3, "_submit", started_then_cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asgi_turn(app, alice, turn_fields()))
    assert (capacity.in_use, capacity.released) == (1, 0)  # jamais libérée par l'annulation
    gate.release.set()
    reply = workers[0].outcome()
    assert reply.status == 200
    assert (capacity.in_use, capacity.released) == (0, 1)
    [row] = env.reservations()
    assert row["state"] == "finalized"  # aucun COMMIT présumé annulé


@pytest.mark.parametrize("moment", ["during_b2", "after_finalization"])
def test_cancelling_the_request_never_stops_the_worker_nor_frees_the_place_early(env, alice, capacities, moment):
    clock = env.transports()
    gate = Gate()
    if moment == "during_b2":
        env.claude = FakeClaude(clock, actions={"create_message": gate})
    else:
        seen = []

        def after_claude():
            def hook_after(point):
                if point == "prevalidated":
                    seen.append(point)
                    if len(seen) == 2:  # replay C2, après le COMMIT de C1-B
                        gate()

            env.hooks.clear()
            env.c3_hook = hook_after

        env.c3_hook = lambda point: None
        env.claude = FakeClaude(clock, actions={"create_message": after_claude})
    hook = (lambda point: env.c3_hook(point)) if moment == "after_finalization" else None
    app = env.c3_app(hook=hook)
    capacity = capacities.instances[-1]
    fields = turn_fields()
    cancelled = threading.Event()
    result = {}

    async def main():
        task = asyncio.ensure_future(asgi_turn(app, alice, fields))
        await asyncio.to_thread(gate.reached.wait, 10)
        task.cancel()  # déconnexion / annulation de la coroutine
        cancelled.set()
        try:
            await task
            result["outcome"] = "completed"
        except asyncio.CancelledError:
            result["outcome"] = "cancelled"

    runner = Background(lambda: asyncio.run(main()))
    runner.start()
    assert cancelled.wait(10)
    time.sleep(0.2)
    assert (capacity.in_use, capacity.released) == (1, 0)  # le fil reste propriétaire
    if moment == "after_finalization":
        assert env.reservations()[0]["state"] == "finalized"
    gate.release.set()
    runner.outcome()
    wait_until(lambda: capacity.in_use == 0)
    assert capacity.released == 1
    assert result["outcome"] == "cancelled"
    [row] = env.reservations()
    assert row["state"] == "finalized"  # le travail serveur a abouti malgré la déconnexion
    again = post(env.c3_app(), TURNS, body_of(fields), c3_headers(alice))  # même intention : C2, aucun fournisseur
    assert again.status_code == 200 and env.claude.methods() == ["create_message"]


def test_a_worker_exception_is_a_closed_refusal_and_returns_the_place(env, alice, capacities, monkeypatch):
    app = env.c3_app()
    capacity = capacities.instances[-1]

    def explode(self, *args):
        raise RuntimeError("secret interne")

    monkeypatch.setattr(c3._Orchestration, "turn", explode)
    response = post(app, TURNS, body_of(turn_fields()), c3_headers(alice))
    assert response.status_code == 503 and response.json() == {"error": "temporarily_unavailable"}
    assert b"secret" not in response.content
    assert (capacity.in_use, capacity.released) == (0, 1)


# --------------------------------------------------------------------------
# 3. Concurrence durable
# --------------------------------------------------------------------------

def test_two_submissions_of_one_intention_share_one_attribution(env, alice):
    gate = Gate()
    app = gated_app(env, gate)
    fields = turn_fields()
    first = in_background(app, alice, fields)
    assert gate.reached.wait(10)
    second = post(app, TURNS, body_of(fields), c3_headers(alice))
    assert second.status_code == 202 and second.json()["status"] == "in_progress"
    gate.release.set()
    done = first.outcome()
    assert done.status_code == 200
    again = post(app, TURNS, body_of(fields), c3_headers(alice))
    assert again.content == done.content
    [row] = env.reservations()
    assert (row["lease_epoch"], row["attempts_used"]) == (1, 1) and env.claude.methods() == ["create_message"]


def test_two_independent_compositions_on_one_database_never_hold_two_active_leases(env, alice, pg_url):  # noqa: F811
    gate = Gate()
    app_a = gated_app(env, gate)
    claude_a = env.claude
    other = service_engine(pg_url)  # second « processus » : moteur, runner, orchestrateur, places distincts
    try:
        clock_b = env.transports()
        app_b = env.c3_app(engine=other)
        claude_b = env.claude
        fields = turn_fields()
        first = in_background(app_a, alice, fields)
        assert gate.reached.wait(10)
        concurrent = [post(app_b, TURNS, body_of(fields), c3_headers(alice)) for _ in range(3)]
        assert {r.status_code for r in concurrent} == {202}
        assert {r.json()["status"] for r in concurrent} == {"in_progress"}
        gate.release.set()
        done = first.outcome()
        assert done.status_code == 200
        replayed = post(app_b, TURNS, body_of(fields), c3_headers(alice))
        assert replayed.content == done.content
        assert claude_a.methods() == ["create_message"] and claude_b.methods() == []
        [row] = env.reservations()
        assert (row["lease_epoch"], row["attempts_used"]) == (1, 1)
        assert clock_b is not None
    finally:
        other.dispose()


def test_two_concurrent_acks_capture_once(env, alice):
    app = env.c3_app()
    done = post(app, TURNS, body_of(turn_fields()), c3_headers(alice))
    assert done.status_code == 200
    second = env.confirmed("alice2@example.com", USER_A)  # deux sessions du même propriétaire
    barrier = threading.Barrier(2)

    def ack(issued):
        barrier.wait(10)
        return post(app, ACK, body_of(ids(done)), c3_headers(issued))

    workers = [Background(lambda: ack(alice)), Background(lambda: ack(second))]
    for worker in workers:
        worker.start()
    responses = [worker.outcome() for worker in workers]
    assert [r.status_code for r in responses] == [200, 200] and responses[0].content == responses[1].content
    delivery_id = uuid.UUID(done.json()["assistant_turn_id"])
    link = env.link(delivery_id)
    assert link["capture_state"] == "captured" and link["response_action"] == "opened_event"
    assert len([e for e in env.events() if e["status"] == "open"]) == 1  # un seul event ouvert : une capture
    assert env.delivery(delivery_id)["status"] == "delivered"
    before = env.full_snapshot()
    assert post(app, ACK, body_of(ids(done)), c3_headers(alice)).status_code == 200
    assert env.full_snapshot() == before  # ACK rejoué : aucune seconde capture


def test_a_slow_provider_never_holds_a_transaction(env, alice):
    clock = env.transports()
    observed = []
    env.eodhd = FakeEodhd(clock, actions={"fundamentals": lambda: observed.append(env.open_f2_transactions())})
    env.claude = FakeClaude(clock, actions={"create_message": lambda: observed.append(env.open_f2_transactions())})
    app = env.c3_app()
    assert post(app, TURNS, body_of(turn_fields()), c3_headers(alice)).status_code == 200
    assert observed == [0, 0]  # aucune transaction F2 ouverte pendant EODHD ni Claude
    assert env.reservations()[0]["user_id"] == USER_A


def test_the_event_loop_is_never_blocked_by_the_orchestration(env, alice):
    gate = Gate()
    app = gated_app(env, gate)
    ticks = []

    async def main():
        async def ticker():
            while not gate.release.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        tick = asyncio.ensure_future(ticker())
        request = asyncio.ensure_future(asgi_turn(app, alice, turn_fields()))
        await asyncio.to_thread(gate.reached.wait, 10)
        before = len(ticks)
        await asyncio.sleep(0.3)
        progressed = len(ticks) - before
        gate.release.set()
        await request
        await tick
        return progressed

    assert asyncio.run(main()) >= 5  # la boucle continue de tourner pendant B2


def test_raw_asgi_helper_matches_the_http_client(env, alice):
    app = env.c3_app()
    response = raw_request(app, method="POST", path=REPLAY, headers=[("host", "oryx.test")] + list(
        c3_headers(alice).items()), chunks=[body_of(identity_fields(uuid.uuid4(), uuid.uuid4()))])
    assert response.status_code == 404 and response.json() == {"error": "not_found"}
