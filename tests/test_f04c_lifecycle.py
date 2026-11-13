"""ACT-L1 / F04-C V0.3 — double registre réel : Q1-01 à Q1-10 rejoués avec
l'approche B (freeze § 8.C), sur le VRAI point d'entrée, PostgreSQL jetable,
vrai serveur Uvicorn 0.27.0 (127.0.0.1) quand le scénario l'exige.

Chaque scénario vérifie ses effets (statuts, base, registre, place C3) ET
les invariants J1-J6 du journal indépendant (tests/f04c_support.py), puis
écrit sa chronologie (save_evidence). Q1-10 « signaux réels » : voir
tests/test_f04c_signals.py.
"""
import asyncio
import json
import threading
import time

import anyio
import httpx
import pytest
import sqlalchemy as sa

from core.authz_f2_http import decryptage as c3
from core.f2_runtime import entrypoint as ep
from tests import f04c_support as f
from tests.authz_f2_pr9b2_support import FakeClaude
from tests.authz_f2_pr9c3_support import Recorder
from tests.f2_support import create_profile_with_identity, prepare_schema
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

BOB = "f04c-bob@example.com"


def second_identity(bench):
    """Seconde identité réelle : l'appel Auth du scénario ne heurte pas
    l'anti-abus de la session de test (même e-mail, même seconde)."""
    create_profile_with_identity(bench.admin, bench.supabase, BOB, "f04c-user-b")
    return BOB


SEVEN_SITES = {
    "auth_f2_http/routes.py:call",              # 1. core/auth_f2_http/routes.py (call)
    "authz_f2_http/routes.py:me",               # 2. GET /me
    "authz_f2_http/routes.py:me_level",         # 3. PUT /me/level
    "authz_f2_http/portfolio.py:run",           # 4. portefeuille
    "authz_f2_http/consultation.py:run",        # 5. consultations
    "authz_f2_http/decryptage.py:_submit",      # 6. /turns
    "authz_f2_http/decryptage.py:single",       # 7. /replay et /ack
}


@pytest.fixture(scope="module")
def migrated(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()
    return pg_url


@pytest.fixture
def journal(monkeypatch):
    j = f.Journal()
    f.install_tracing(monkeypatch, j)
    return j


@pytest.fixture
def capacities(monkeypatch):
    return Recorder(monkeypatch, c3._TurnCapacity)


def make_bench(migrated, journal, monkeypatch, **kwargs):
    bench = f.Bench(migrated, journal, **kwargs)
    bench.install_runner_hook(monkeypatch)
    return bench


@pytest.fixture
def bench(migrated, journal, monkeypatch):
    environment = make_bench(migrated, journal, monkeypatch)
    yield environment
    environment.dispose()


def assert_clean(name, bench, **observations):
    result = f.save_evidence(name, bench.journal, outcome=bench.envelope.outcome,
                             snapshot=bench.envelope.registry.snapshot().__dict__, **observations)
    assert f.violated(result) == {}, (name, f.violated(result))
    snap = bench.envelope.registry.snapshot()
    assert snap.violations == 0 and snap.admissions_active == 0 and snap.jobs_non_terminal == 0
    assert snap.admitted_total == snap.released_total
    assert snap.registered_total == snap.finished_total + snap.abandoned_total


class Http:
    """Client HTTP réel vers Uvicorn (cookies gérés explicitement : __Host-)."""

    def __init__(self, bench, port):
        self.bench = bench
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=60)
        self.cookies = {}

    def request(self, method, path, *, label=None, json_body=None, content=None, headers=None):
        sent = {"host": f.HOST, "sec-fetch-site": "same-origin"}
        if self.cookies:
            sent["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if json_body is not None:
            content = json.dumps(json_body).encode()
            sent["content-type"] = "application/json"
        if label:
            sent["x-f04c-label"] = label
        sent.update(headers or {})
        response = self.client.request(method, path, headers=sent, content=content)
        for raw in response.headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            value = rest.split(";", 1)[0]
            if "Max-Age=0" in raw:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return response

    def sign_in(self):
        boot = self.request("GET", f.AUTH + "/csrf/bootstrap", label="bootstrap")
        assert boot.status_code == 200, boot.text
        self.token = boot.json()["csrf_token"]
        mutation = {"origin": f.ORIGIN, "x-oryx-csrf": self.token}
        start = self.request("POST", f.AUTH + "/sign-in/start", label="sign-in-start", headers=mutation,
                             json_body={"email": f.EMAIL})
        assert start.status_code == 202, start.text
        body = start.json()
        verify = self.request("POST", f.AUTH + "/sign-in/verify", label="sign-in-verify", headers=mutation,
                              json_body={"operation_id": body["operation_id"], "candidate_id": body["candidate_id"],
                                         "code": self.bench.mailer.last_code(f.EMAIL)})
        assert verify.status_code == 200, verify.text
        self.generation = verify.json()["generation_id"]
        resolve = self.request("POST", f.AUTH + "/session/resolve", label="resolve", headers=mutation,
                               json_body={"generation_id": self.generation})
        assert resolve.status_code == 200, resolve.text
        self.cookies = {k: v for k, v in self.cookies.items() if not k.startswith("__Host-oryx-candidate-")}

    def personal(self, method, path, label, body=None):
        headers = {"x-oryx-generation-id": self.generation}
        if method != "GET":
            headers.update({"origin": f.ORIGIN, "x-oryx-csrf": self.token, "content-type": "application/json"})
        return self.request(method, path, label=label, headers=headers, content=body)


def live(bench, **kwargs):
    return f.LiveServer(f.Probe(bench.envelope, bench.journal), bench.journal, **kwargs).start()


# ==========================================================================
# Q1-01 — normal, à travers Uvicorn : les SEPT sites passent par le soumetteur
# ==========================================================================

def test_q1_01_normal_journey_uses_the_registered_submitter_at_the_seven_sites(bench, capacities):
    server = live(bench)
    assert bench.envelope.state == ep.OPEN
    http = Http(bench, server.port)
    http.sign_in()
    statuses = {
        "me": http.personal("GET", f.ME, "me").status_code,
        "level": http.personal("PUT", f.ME_LEVEL, "level", f.level_body()).status_code,
        "portfolio": http.personal("GET", f.PORTFOLIO, "portfolio").status_code,
        "theses": http.personal("GET", f.THESES, "theses").status_code,
    }
    turn = http.personal("POST", f.TURNS, "turns", f.turn_body())
    statuses["turns"] = turn.status_code
    statuses["replay"] = http.personal("POST", f.REPLAY, "replay", f.identity_body(turn.content)).status_code
    statuses["ack"] = http.personal("POST", f.ACK, "ack", f.identity_body(turn.content)).status_code
    server.request_exit()
    assert server.exited.wait(30)
    sites = {e["site"] for e in bench.journal.of("SUBMIT")}
    cap = capacities.instances[-1]
    assert statuses == {"me": 200, "level": 200, "portfolio": 200, "theses": 200, "turns": 200, "replay": 200,
                        "ack": 200}, statuses
    assert sites == SEVEN_SITES, sites
    assert bench.envelope.outcome == ep.GRACEFUL and bench.envelope.state == ep.CLOSED
    assert bench.level() == "avance" and (cap.in_use, cap.released) == (0, 1)
    assert bench.journal.of("RESOURCE_CLOSED")[0]["checked_out"] == 0
    assert_clean("Q1-01", bench, statuses=statuses, sites=sorted(sites))


# ==========================================================================
# Q1-02 — annulation AVANT soumission
# ==========================================================================

@pytest.mark.parametrize("cancel", ["asyncio_native", "anyio_scope"])
def test_q1_02_cancelled_before_submission_registers_nothing(bench, cancel):
    issued = bench.issue_session()
    app = f.Probe(bench.envelope, bench.journal)

    async def main():
        life = f.Lifespan(bench.envelope)
        assert (await life.startup())["type"] == "lifespan.startup.complete"
        hold = asyncio.Event()
        headers = f.mutation_headers(bench, issued, "victim")
        if cancel == "asyncio_native":
            task = asyncio.ensure_future(f.asgi_call(app, "PUT", f.ME_LEVEL, headers, f.level_body(), hold=hold))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            async def run():
                await f.asgi_call(app, "PUT", f.ME_LEVEL, headers, f.level_body(), hold=hold)

            async with anyio.create_task_group() as tg:
                tg.start_soon(run)
                await asyncio.sleep(0.2)
                tg.cancel_scope.cancel()
        await life.shutdown_begin()
        return await life.shutdown_result()

    assert asyncio.run(main()) == {"type": "lifespan.shutdown.complete"}
    assert bench.journal.of("SUBMIT") == [] and bench.journal.of("JOB_REGISTERED") == []
    assert bench.level() == "debutant" and bench.envelope.outcome == ep.GRACEFUL
    assert_clean(f"Q1-02-{cancel}", bench)


def test_q1_02_tcp_reset_before_the_body_registers_nothing(bench):
    issued = bench.issue_session()
    server = live(bench)
    raw = f.http_bytes("PUT", f.ME_LEVEL, f.mutation_headers(bench, issued, "victim"), f.level_body())
    client = f.RawClient(server.port)
    client.send(raw[:-5])
    bench.journal.wait_for("ADMITTED")
    time.sleep(0.2)
    client.reset()
    bench.journal.wait_for("ADMISSION_RELEASED")
    server.request_exit()
    assert server.exited.wait(30)
    assert bench.journal.of("JOB_REGISTERED") == [] and bench.level() == "debutant"
    assert bench.envelope.outcome == ep.GRACEFUL
    assert_clean("Q1-02-tcp", bench)


# ==========================================================================
# Q1-03 — annulation après inscription, avant démarrage du fil
# ==========================================================================

def test_q1_03_waiting_for_a_thread_token_then_cancelled_is_abandoned_and_never_runs(bench):
    issued = bench.issue_session()
    hold = f.Gate()
    bench.hooks["locked"] = hold
    app = f.Probe(bench.envelope, bench.journal)

    async def main():
        life = f.Lifespan(bench.envelope)
        await life.startup()
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1
        occupy = asyncio.ensure_future(f.asgi_call(app, "GET", f.ME, f.get_headers(issued, "occupy")))
        await asyncio.to_thread(hold.reached.wait, 10)
        bench.hooks.clear()
        victim = asyncio.ensure_future(f.asgi_call(app, "PUT", f.ME_LEVEL, f.mutation_headers(bench, issued, "victim"),
                                                   f.level_body()))
        while not bench.journal.of("JOB_REGISTERED") or len(bench.journal.of("JOB_REGISTERED")) < 2:
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.1)
        victim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim
        hold.release.set()
        await occupy
        await asyncio.sleep(0.2)
        await life.shutdown_begin()
        return await life.shutdown_result()

    assert asyncio.run(main()) == {"type": "lifespan.shutdown.complete"}
    snap = bench.envelope.registry.snapshot()
    assert (snap.registered_total, snap.finished_total, snap.abandoned_total) == (2, 1, 1)
    assert [e["req"] for e in bench.journal.of("CALLABLE_ENTER")] == ["occupy"] and bench.level() == "debutant"
    assert_clean("Q1-03-limiter", bench)


@pytest.mark.parametrize("stage", ["before_check", "after_check"])
@pytest.mark.parametrize("shutdown_first", [False, True])
def test_q1_03_cancelled_inside_the_anyio_window_never_invokes_the_callable(bench, monkeypatch, stage,
                                                                            shutdown_first):
    """Fenêtre EXISTANTE d'anyio (avant / après `future.cancelled()`) : sous
    l'approche A, le travail démarrait hors suivi et commitait (Q1 NO-GO-A).
    Avec B, l'abandon précède le démarrage : le wrapper refuse, aucun effet.
    shutdown_first : l'arrêt complet (ressources fermées) a lieu AVANT que le
    fil ne reprenne (course Q1-09b)."""
    issued = bench.issue_session()
    reached, release = f.install_worker_window(monkeypatch, bench.journal, stage)
    app = f.Probe(bench.envelope, bench.journal)

    async def main():
        life = f.Lifespan(bench.envelope)
        await life.startup()
        victim = asyncio.ensure_future(f.asgi_call(app, "PUT", f.ME_LEVEL, f.mutation_headers(bench, issued, "victim"),
                                                   f.level_body()))
        await asyncio.to_thread(reached.wait, 10)
        victim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim
        if shutdown_first:
            await life.shutdown_begin()
            done = await life.shutdown_result()
            release.set()
            await asyncio.sleep(0.5)
            return done
        release.set()
        await asyncio.sleep(0.5)
        await life.shutdown_begin()
        return await life.shutdown_result()

    assert asyncio.run(main()) == {"type": "lifespan.shutdown.complete"}
    assert bench.journal.of("JOB_ABANDONED") and bench.journal.of("CALLABLE_ENTER") == []
    if stage == "after_check":
        assert bench.journal.of("JOB_START_REFUSED")  # le fil a pris l'élément : refus, callable non invoqué
    else:
        assert bench.journal.of("ANYIO_SKIPPED_CANCELLED_FUTURE")
    assert bench.level() == "debutant"  # aucun commit tardif d'un travail abandonné
    assert_clean(f"Q1-03-window-{stage}{'-after-shutdown' if shutdown_first else ''}", bench)


def test_q1_03_turns_window_abandon_keeps_the_c3_place_contract(bench, monkeypatch, capacities):
    issued = bench.issue_session()
    reached, release = f.install_worker_window(monkeypatch, bench.journal, "after_check")
    app = f.Probe(bench.envelope, bench.journal)

    async def main():
        life = f.Lifespan(bench.envelope)
        await life.startup()
        victim = asyncio.ensure_future(f.asgi_call(app, "POST", f.TURNS, f.mutation_headers(bench, issued, "turn"),
                                                   f.turn_body()))
        await asyncio.to_thread(reached.wait, 10)
        victim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim
        release.set()
        await asyncio.sleep(0.5)
        await life.shutdown_begin()
        return await life.shutdown_result()

    assert asyncio.run(main())["type"] == "lifespan.shutdown.complete"
    cap = capacities.instances[-1]
    assert (cap.in_use, cap.released) == (0, 1)  # place rendue UNE fois (abandon du slot par la requête)
    assert bench.count("decryptage_turn_reservations") == 0 and bench.claude.methods() == []  # aucun B1
    assert_clean("Q1-03-turns-window", bench, capacity=(cap.in_use, cap.released))


# ==========================================================================
# Q1-04 — annulation pendant que le fil est réellement actif
# ==========================================================================

@pytest.mark.parametrize("cancel", ["asyncio_native", "anyio_scope"])
def test_q1_04_cancelled_while_the_thread_holds_a_transaction(bench, cancel):
    issued = bench.issue_session()
    hold = f.Gate()
    bench.hooks["locked"] = hold  # transaction ouverte, connexion empruntée, verrou de ligne
    app = f.Probe(bench.envelope, bench.journal)
    observed = {}

    async def main():
        life = f.Lifespan(bench.envelope)
        await life.startup()
        headers = f.mutation_headers(bench, issued, "victim")
        if cancel == "asyncio_native":
            victim = asyncio.ensure_future(f.asgi_call(app, "PUT", f.ME_LEVEL, headers, f.level_body()))
            await asyncio.to_thread(hold.reached.wait, 10)
            victim.cancel()
            with pytest.raises(asyncio.CancelledError):
                await victim
        else:
            scope = anyio.CancelScope()

            async def run():
                with scope:
                    await f.asgi_call(app, "PUT", f.ME_LEVEL, headers, f.level_body())

            victim = asyncio.ensure_future(run())
            await asyncio.to_thread(hold.reached.wait, 10)
            scope.cancel()
            await asyncio.sleep(0.2)
            observed["request_still_running"] = not victim.done()
        snap = bench.envelope.registry.snapshot()
        observed.update(admissions=snap.admissions_active, jobs_started=snap.jobs_started,
                        checked_out=bench.engine.pool.checkedout())
        await life.shutdown_begin()
        await asyncio.sleep(0.5)
        observed["closed_while_blocked"] = list(bench.closed)
        observed["state_while_blocked"] = bench.envelope.state
        bench.hooks.clear()
        hold.release.set()
        if not victim.done():
            await victim
        return await life.shutdown_result()

    assert asyncio.run(main()) == {"type": "lifespan.shutdown.complete"}
    assert observed["jobs_started"] == 1 and observed["checked_out"] == 1
    assert observed["closed_while_blocked"] == [] and observed["state_while_blocked"] == ep.DRAINING
    if cancel == "asyncio_native":
        assert observed["admissions"] == 0  # la requête est sortie, le JOB reste suivi
    else:
        assert observed["request_still_running"] and observed["admissions"] == 1
    assert bench.level() == "avance"  # le fil, suivi jusqu'au bout, a commité AVANT la fermeture
    finished = bench.journal.of("JOB_FINISHED")[-1]["seq"]
    assert bench.journal.of("RESOURCE_CLOSED")[0]["seq"] > finished
    assert_clean(f"Q1-04-{cancel}", bench, **observed)


def test_q1_04_turns_cancelled_during_b2_keeps_job_and_place(migrated, journal, monkeypatch, capacities):
    gate = f.Gate()
    bench = make_bench(migrated, journal, monkeypatch)
    bench.claude.actions["create_message"] = gate
    try:
        issued = bench.issue_session()
        app = f.Probe(bench.envelope, journal)
        observed = {}

        async def main():
            life = f.Lifespan(bench.envelope)
            await life.startup()
            victim = asyncio.ensure_future(f.asgi_call(app, "POST", f.TURNS, f.mutation_headers(bench, issued, "turn"),
                                                       f.turn_body()))
            await asyncio.to_thread(gate.reached.wait, 10)
            victim.cancel()
            with pytest.raises(asyncio.CancelledError):
                await victim
            cap = capacities.instances[-1]
            observed["place_in_use"] = cap.in_use
            observed["jobs_started"] = bench.envelope.registry.snapshot().jobs_started
            await life.shutdown_begin()
            await asyncio.sleep(0.3)
            observed["closed_while_blocked"] = list(bench.closed)
            gate.release.set()
            return await life.shutdown_result()

        assert asyncio.run(main())["type"] == "lifespan.shutdown.complete"
        cap = capacities.instances[-1]
        assert observed == {"place_in_use": 1, "jobs_started": 1, "closed_while_blocked": []}
        assert (cap.in_use, cap.released) == (0, 1)
        with bench.admin.connect() as conn:
            assert conn.execute(sa.text("SELECT state FROM decryptage_turn_reservations")).scalar() == "finalized"
        assert_clean("Q1-04-turns", bench, **observed)
    finally:
        bench.dispose()


# ==========================================================================
# Q1-05 / Q1-06 — vraie déconnexion TCP (RST) pendant /turns, /replay, /ack
# ==========================================================================

def test_q1_05_tcp_reset_during_turns(migrated, journal, monkeypatch, capacities):
    gate = f.Gate()
    bench = make_bench(migrated, journal, monkeypatch)
    bench.claude.actions["create_message"] = gate
    try:
        issued = bench.issue_session()
        server = live(bench)
        client = f.RawClient(server.port)
        client.send(f.http_bytes("POST", f.TURNS, f.mutation_headers(bench, issued, "turn"), f.turn_body()))
        assert gate.reached.wait(10)
        client.reset()
        time.sleep(0.3)
        snap = bench.envelope.registry.snapshot()
        server.request_exit()
        time.sleep(0.5)
        closed_while_blocked = list(bench.closed)
        gate.release.set()
        assert server.exited.wait(30)
        cap = capacities.instances[-1]
        assert snap.jobs_started == 1 and closed_while_blocked == [] and bench.envelope.outcome == ep.GRACEFUL
        assert (cap.in_use, cap.released) == (0, 1)
        with bench.admin.connect() as conn:
            assert conn.execute(sa.text("SELECT state FROM decryptage_turn_reservations")).scalar() == "finalized"
        assert_clean("Q1-05", bench, jobs_started_after_reset=snap.jobs_started)
    finally:
        bench.dispose()


@pytest.mark.parametrize("route", ["replay", "ack"])
def test_q1_06_tcp_reset_during_replay_and_ack(bench, route):
    issued = bench.issue_session()

    async def deliver():
        life = f.Lifespan(bench.envelope)
        await life.startup()
        sent = await f.asgi_call(bench.envelope, "POST", f.TURNS, f.mutation_headers(bench, issued), f.turn_body())
        assert f.status_of_messages(sent) == 200
        await life.shutdown_begin()
        await life.shutdown_result()
        return f.body_of_messages(sent)

    reply = asyncio.run(deliver())
    # Nouvelle enveloppe (une enveloppe fermée ne redémarre jamais).
    bench.envelope = ep.F2Envelope(environ=bench.environ, resources_factory=bench.resources,
                                   observer=bench.journal.observe)
    bench.journal.events.clear()
    bench.closed.clear()
    hold = f.Gate()
    bench.hooks["locked"] = hold
    server = live(bench)
    client = f.RawClient(server.port)
    path = f.REPLAY if route == "replay" else f.ACK
    client.send(f.http_bytes("POST", path, f.mutation_headers(bench, issued, route), f.identity_body(reply)))
    assert hold.reached.wait(10)
    client.reset()
    time.sleep(0.3)
    snap = bench.envelope.registry.snapshot()
    checked_out = bench.engine.pool.checkedout()
    server.request_exit()
    time.sleep(0.5)
    closed_while_blocked = list(bench.closed)
    bench.hooks.clear()
    hold.release.set()
    assert server.exited.wait(30)
    assert snap.jobs_started == 1 and checked_out == 1 and closed_while_blocked == []
    assert bench.envelope.outcome == ep.GRACEFUL
    with bench.admin.connect() as conn:
        status = conn.execute(sa.text("SELECT status FROM assistant_deliveries")).scalar()
    assert status == ("delivered" if route == "ack" else "pending")
    assert_clean(f"Q1-06-{route}", bench, checked_out_after_reset=checked_out)


# ==========================================================================
# Q1-07 — Auth et Profil simultanés, arrêt pendant leur exécution
# ==========================================================================

def test_q1_07_concurrent_auth_and_profile_jobs_both_block_the_closure(bench):
    issued = bench.issue_session()
    supabase_gate, profile_gate = f.Gate(), f.Gate()
    original = bench.supabase.generate_email_otp

    def gated(email):
        supabase_gate()
        return original(email)

    bench.supabase.generate_email_otp = gated
    bench.hooks["locked"] = profile_gate
    email = second_identity(bench)
    server = live(bench)
    http = Http(bench, server.port)
    boot = http.request("GET", f.AUTH + "/csrf/bootstrap")
    token = boot.json()["csrf_token"]
    results = {}

    def auth_call():
        results["auth"] = http.request("POST", f.AUTH + "/sign-in/start", label="auth", json_body={"email": email},
                                       headers={"origin": f.ORIGIN, "x-oryx-csrf": token}).status_code

    def profile_call():
        client = f.RawClient(server.port)
        client.send(f.http_bytes("GET", f.ME, f.get_headers(issued, "profile")))
        results["profile"] = f.status_of(client.read_all())

    threads = [f.driver(auth_call, "f04c-auth")[0], f.driver(profile_call, "f04c-profile")[0]]
    assert supabase_gate.reached.wait(10) and profile_gate.reached.wait(10)
    snap = bench.envelope.registry.snapshot()
    server.request_exit()
    time.sleep(1.0)
    closed_while_blocked = list(bench.closed)
    try:
        f.RawClient(server.port).send(f.http_bytes("GET", f.ME, f.get_headers(issued)))
        late = "accepted"
    except OSError as exc:
        late = type(exc).__name__
    bench.hooks.clear()
    supabase_gate.release.set()
    profile_gate.release.set()
    for t in threads:
        t.join(30)
    assert server.exited.wait(30)
    assert snap.jobs_started == 2 and snap.admissions_active == 2 and closed_while_blocked == []
    assert late == "ConnectionRefusedError" and results == {"auth": 202, "profile": 200}
    assert bench.envelope.outcome == ep.GRACEFUL
    assert_clean("Q1-07", bench, results=results, late=late)


# ==========================================================================
# Q1-08 — exception contrôlée pendant une opération fournisseur simulée
# ==========================================================================

def test_q1_08_provider_exceptions_are_closed_refusals_and_terminal_jobs(migrated, journal, monkeypatch,
                                                                       capacities):
    def boom():
        raise RuntimeError("panne fournisseur simulée")

    bench = make_bench(migrated, journal, monkeypatch)
    bench.claude.actions["create_message"] = boom
    try:
        issued = bench.issue_session()
        bench.supabase.generate_script.append(RuntimeError("panne Supabase simulée"))
        email = second_identity(bench)
        server = live(bench)
        http = Http(bench, server.port)
        token = http.request("GET", f.AUTH + "/csrf/bootstrap").json()["csrf_token"]
        auth = http.request("POST", f.AUTH + "/sign-in/start", label="auth", json_body={"email": email},
                            headers={"origin": f.ORIGIN, "x-oryx-csrf": token})
        client = f.RawClient(server.port)
        client.send(f.http_bytes("POST", f.TURNS, f.mutation_headers(bench, issued, "turn"), f.turn_body()))
        raw = client.read_all()
        server.request_exit()
        assert server.exited.wait(30)
        cap = capacities.instances[-1]
        assert auth.status_code == 503 and auth.json() == {"error": "temporarily_unavailable"}
        assert f.status_of(raw) == 503 and raw.endswith(b'{"error":"provider_unavailable"}')
        assert (cap.in_use, cap.released) == (0, 1) and bench.envelope.outcome == ep.GRACEFUL
        assert_clean("Q1-08", bench)
    finally:
        bench.dispose()


# ==========================================================================
# Q1-09 — admission fermée pendant une course de soumission
# ==========================================================================

def test_q1_09_an_admission_without_thread_blocks_closure_then_submits_during_draining(bench):
    issued = bench.issue_session()
    app = f.Probe(bench.envelope, bench.journal)
    observed = {}

    async def main():
        life = f.Lifespan(bench.envelope)
        await life.startup()
        hold = asyncio.Event()
        inflight = asyncio.ensure_future(f.asgi_call(app, "PUT", f.ME_LEVEL,
                                                     f.mutation_headers(bench, issued, "inflight"), f.level_body(),
                                                     hold=hold))
        await asyncio.sleep(0.1)
        await life.shutdown_begin()
        await asyncio.sleep(0.3)
        snap = bench.envelope.registry.snapshot()
        observed.update(state=bench.envelope.state, admissions=snap.admissions_active, jobs=snap.jobs_non_terminal,
                        closed=list(bench.closed))
        late = await f.asgi_call(app, "GET", f.ME, f.get_headers(issued, "late"))
        observed["late"] = (f.status_of_messages(late), f.body_of_messages(late))
        health = await f.asgi_call(app, "GET", "/health", {"host": f.HOST})
        observed["health"] = f.status_of_messages(health)
        hold.set()  # le corps arrive : la soumission a lieu PENDANT DRAINING
        observed["inflight"] = f.status_of_messages(await inflight)
        return await life.shutdown_result()

    assert asyncio.run(main()) == {"type": "lifespan.shutdown.complete"}
    assert observed["state"] == ep.DRAINING and observed["admissions"] == 1 and observed["jobs"] == 0
    assert observed["closed"] == []  # jobs == 0 mais admission vivante : aucune fermeture
    assert observed["late"] == (503, ep.MAINTENANCE_BODY) and observed["health"] == 200
    assert observed["inflight"] == 200 and bench.level() == "avance"
    registered = bench.journal.of("JOB_REGISTERED")[0]["seq"]
    closed = bench.journal.of("ADMISSIONS_CLOSED")[0]["seq"]
    assert registered > closed  # soumis après DRAINING, dans le cadre de son admission
    assert_clean("Q1-09", bench, **{k: v for k, v in observed.items() if k != "late"})


# ==========================================================================
# Q1-10 — arrêt avec un fil volontairement bloqué (Uvicorn réel, fil dédié)
# ==========================================================================

@pytest.mark.parametrize("drain_timeout", [None, 1.5])
def test_q1_10_uvicorn_graceful_timeout_cancels_the_request_but_never_the_job(migrated, journal, monkeypatch,
                                                                             drain_timeout):
    bench = make_bench(migrated, journal, monkeypatch, drain_timeout=drain_timeout)
    try:
        issued = bench.issue_session()
        hold = f.Gate()
        bench.hooks["locked"] = hold
        server = live(bench, timeout_graceful_shutdown=1.0)
        client = f.RawClient(server.port)
        client.send(f.http_bytes("PUT", f.ME_LEVEL, f.mutation_headers(bench, issued, "victim"), f.level_body()))
        assert hold.reached.wait(10)
        server.request_exit()
        journal.wait_for("HTTP_CANCELLED", req="victim", timeout=15)  # Uvicorn annule la tâche de requête
        time.sleep(2.5)
        observed = {"closed_while_blocked": list(bench.closed), "level_while_blocked": bench.level(),
                    "outcome_while_blocked": bench.envelope.outcome,
                    "snapshot": bench.envelope.registry.snapshot().__dict__}
        bench.hooks.clear()
        hold.release.set()
        assert server.exited.wait(30)
        journal.wait_for("JOB_FINISHED")
        time.sleep(0.3)
        assert observed["closed_while_blocked"] == [] and observed["level_while_blocked"] == "debutant"
        assert observed["snapshot"]["jobs_started"] == 1 and observed["snapshot"]["admissions_active"] == 0
        if drain_timeout is None:
            assert bench.envelope.outcome == ep.GRACEFUL  # fermeture APRÈS la fin réelle du fil
            assert journal.of("RESOURCE_CLOSED")[0]["seq"] > journal.of("JOB_FINISHED")[0]["seq"]
        else:
            assert observed["outcome_while_blocked"] == ep.DRAIN_TIMEOUT  # délai dépassé, honnête
            assert bench.envelope.outcome == ep.DRAIN_TIMEOUT and journal.of("RESOURCE_CLOSED") == []
        assert bench.level() == "avance"  # écriture du fil suivi (client : 500 — COMMIT incertain, F04-E)
        result = f.save_evidence(f"Q1-10-thread-drain_{drain_timeout}", journal, outcome=bench.envelope.outcome,
                                 **{k: v for k, v in observed.items() if k != "snapshot"})
        assert f.violated(result) == {}
    finally:
        bench.dispose()


# ==========================================================================
# Négatifs : le banc DÉTECTE un soumetteur non suivi et un fil brut
# ==========================================================================

def test_negative_an_unsupervised_submitter_is_detected(bench, monkeypatch):
    """Soumetteur défectueux (contourne le registre, run_in_threadpool direct) :
    l'enveloppe ferme pendant qu'un fil travaille ; le banc DOIT le voir."""
    from starlette.concurrency import run_in_threadpool

    from core.f2_runtime import work_registry as wr

    async def leaky(self, func, *args, **kwargs):
        return await run_in_threadpool(func, *args, **kwargs)

    issued = bench.issue_session()
    hold = f.Gate()
    bench.hooks["locked"] = hold
    app = f.Probe(bench.envelope, bench.journal)

    async def main():
        life = f.Lifespan(bench.envelope)
        await life.startup()
        monkeypatch.setattr(wr.RegisteredSubmitter, "__call__", leaky)
        victim = asyncio.ensure_future(f.asgi_call(app, "PUT", f.ME_LEVEL, f.mutation_headers(bench, issued, "x"),
                                                   f.level_body()))
        await asyncio.to_thread(hold.reached.wait, 10)
        victim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim
        await life.shutdown_begin()
        done = await life.shutdown_result()
        bench.hooks.clear()
        hold.release.set()
        await asyncio.sleep(0.5)
        return done

    asyncio.run(main())
    import gc

    gc.collect()  # la connexion rendue à l'ANCIEN pool (fermé trop tôt) n'est libérée qu'au ramasse-miettes
    result = f.save_evidence("NEG-unsupervised-submitter", bench.journal)
    assert result["J1"] and result["J4"] and result["J6"], result  # détecté : invisible + fermeture prématurée


def test_negative_a_raw_thread_is_detected_by_the_physical_census(bench, monkeypatch):
    from core.f2_runtime import work_registry as wr

    release = threading.Event()

    async def rogue(self, func, *args, **kwargs):
        threading.Thread(target=lambda: release.wait(10), name="rogue", daemon=True).start()
        raise wr.SubmissionRefused("fil brut lancé hors registre")

    issued = bench.issue_session()

    async def main():
        life = f.Lifespan(bench.envelope)
        await life.startup()
        monkeypatch.setattr(wr.RegisteredSubmitter, "__call__", rogue)
        await f.asgi_call(bench.envelope, "GET", f.ME, f.get_headers(issued))
        await life.shutdown_begin()
        return await life.shutdown_result()

    asyncio.run(main())
    release.set()
    [closed] = bench.journal.of("RESOURCE_CLOSED")
    result = f.save_evidence("NEG-raw-thread", bench.journal)
    assert any(b.startswith("rogue:") for b in closed["busy"]) and result["J4"], closed


# ==========================================================================
# C3 : deux places, troisième refusée avant B1, replay / ack disponibles
# ==========================================================================

def test_c3_keeps_two_places_behind_the_envelope(migrated, journal, monkeypatch, capacities):
    gate = f.Gate()
    bench = make_bench(migrated, journal, monkeypatch)
    try:
        issued = bench.issue_session()
        app = f.Probe(bench.envelope, journal)
        observed = {}

        async def main():
            life = f.Lifespan(bench.envelope)
            await life.startup()
            done = await f.asgi_call(app, "POST", f.TURNS, f.mutation_headers(bench, issued, "done"),
                                     f.turn_body("conv-done"))
            assert f.status_of_messages(done) == 200
            bench.claude.actions["create_message"] = gate
            first = asyncio.ensure_future(f.asgi_call(app, "POST", f.TURNS, f.mutation_headers(bench, issued, "one"),
                                                      f.turn_body("conv-one")))
            second = asyncio.ensure_future(f.asgi_call(app, "POST", f.TURNS,
                                                       f.mutation_headers(bench, issued, "two"),
                                                       f.turn_body("conv-two")))
            while gate.count < 2:
                await asyncio.sleep(0.01)
            reservations = bench.count("decryptage_turn_reservations")
            third = await f.asgi_call(app, "POST", f.TURNS, f.mutation_headers(bench, issued, "three"),
                                      f.turn_body("conv-three"))
            observed["third"] = (f.status_of_messages(third), f.body_of_messages(third))
            observed["no_b1"] = bench.count("decryptage_turn_reservations") == reservations
            ids = f.identity_body(f.body_of_messages(done), "conv-done")
            observed["replay"] = f.status_of_messages(await f.asgi_call(app, "POST", f.REPLAY,
                                                                        f.mutation_headers(bench, issued), ids))
            observed["ack"] = f.status_of_messages(await f.asgi_call(app, "POST", f.ACK,
                                                                     f.mutation_headers(bench, issued), ids))
            observed["jobs_started"] = bench.envelope.registry.snapshot().jobs_started
            gate.release.set()
            observed["blocked"] = [f.status_of_messages(await first), f.status_of_messages(await second)]
            await life.shutdown_begin()
            return await life.shutdown_result()

        assert asyncio.run(main()) == {"type": "lifespan.shutdown.complete"}
        cap = capacities.instances[-1]
        assert cap.size == c3.MAX_CONCURRENT_TURNS == 2
        assert observed["third"] == (503, b'{"error":"capacity_unavailable"}') and observed["no_b1"]
        assert observed["replay"] == 200 and observed["ack"] == 200 and observed["jobs_started"] == 2
        assert observed["blocked"] == [200, 200] and (cap.in_use, cap.released) == (0, 3)
        assert_clean("C3-two-places", bench, **{k: v for k, v in observed.items() if k != "third"})
    finally:
        bench.dispose()
