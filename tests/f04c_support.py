"""Banc d'essai F04-C (ACT-L1 / F04-C V0.3) — tests seulement.

Le VRAI point d'entrée (core.f2_runtime.entrypoint.F2Envelope) est démarré
par son VRAI lifespan contre un PostgreSQL jetable : configuration F04-B
synthétique (tests/f04b_support.py, aucune valeur réelle), moteur F04-B réel
(create_f2_engine), précontrôle réel, compositions réelles, porte F-02A
réelle. Seuls les fournisseurs sont des doubles (aucun réseau), injectés par
resources_factory dans une F2Resources de type exact.

Observation INDÉPENDANTE du protocole testé (Journal) :

- registre et enveloppe : leur `observer` (ADMITTED, JOB_STARTED, SEALED,
  STATE…), horodaté et attribué au fil appelant ;
- SUBMIT / CALLABLE_ENTER / CALLABLE_EXIT : RegisteredSubmitter.__call__
  enveloppé (le callable métier n'est observé que s'il est RÉELLEMENT
  invoqué dans le fil) ;
- PHYSICAL_ENTER / PHYSICAL_EXIT : anyio.to_thread.run_sync enveloppé (tout
  fil AnyIO qui exécute quelque chose, soumetteur suivi ou non) ;
- RESOURCE_CLOSED : fermeture réelle du moteur F04-B, avec
  pool.checkedout() et recensement physique des fils actifs
  (sys._current_frames) à cet instant ;
- REQUEST_* : sonde ASGI extérieure à l'enveloppe (libellé x-f04c-label).

check_invariants(journal) confronte ces observations (voir sa docstring).
"""
import asyncio
import contextvars
import functools
import json
import socket
import struct
import sys
import threading
import time
import traceback
import uuid

import anyio.to_thread
import sqlalchemy as sa

from core.auth_f2 import abuse
from core.auth_f2.service import F2AuthService
from core.authz_f2.runner import AuthorizedTransactionRunner
from core.f2_runtime import entrypoint as ep
from core.f2_runtime import work_registry as wr
from core.f2_runtime.config import load_f2_config
from core.f2_runtime.database import create_f2_engine
from core.f2_runtime.resources import F2Resources, ResourceOwner
from tests import f04b_support as synthetic
from tests.authz_f2_pr9b2_support import FakeClaude, FakeClock, FakeEodhd, FakeFmp
from tests.authz_f2_support import active_cookie, csrf_pair
from tests.f2_support import FakeMailer, FakeSupabase, clean_f2, create_profile_with_identity

ORIGIN = synthetic.ORIGIN
HOST = ORIGIN.split("://", 1)[1]
EMAIL = "f04c-alice@example.com"
USER = "f04c-user-a"
ME = "/api/v2/me"
ME_LEVEL = "/api/v2/me/level"
PORTFOLIO = "/api/v2/me/portfolio"
THESES = "/api/v2/me/theses"
TURNS = "/api/v2/me/decryptage/turns"
REPLAY = "/api/v2/me/decryptage/replay"
ACK = "/api/v2/me/decryptage/ack"
AUTH = "/api/auth/f2"

REQ = contextvars.ContextVar("f04c_req", default=None)


# --------------------------------------------------------------------------
# Journal et instrumentation
# --------------------------------------------------------------------------

class Journal:
    def __init__(self):
        self._lock = threading.Lock()
        self.events = []
        self._seq = 0

    def rec(self, kind, **fields):
        with self._lock:
            event = {"seq": self._seq, "t": time.monotonic(), "kind": kind, "ident": threading.get_ident(),
                     "thread": threading.current_thread().name, **fields}
            self._seq += 1
            self.events.append(event)
        return event

    def observe(self, kind, fields):
        """observer(kind, fields) du registre et de l'enveloppe."""
        self.rec(kind, **fields)

    def of(self, kind, **match):
        with self._lock:
            return [e for e in self.events if e["kind"] == kind and all(e.get(k) == v for k, v in match.items())]

    def wait_for(self, kind, timeout=15, **match):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.of(kind, **match)
            if found:
                return found[0]
            time.sleep(0.005)
        raise AssertionError(f"{kind} {match} non observé")

    def timeline(self):
        with self._lock:
            events = list(self.events)
        if not events:
            return []
        t0 = events[0]["t"]
        out = []
        for e in events:
            extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("seq", "t", "kind", "ident", "thread"))
            out.append(f"{e['seq']:>4} +{(e['t'] - t0) * 1000:9.1f}ms {e['kind']:<20} {extra} [{e['thread']}]")
        return out


def install_tracing(monkeypatch, journal):
    """SUBMIT / CALLABLE_* (soumetteur suivi) et PHYSICAL_* (tout fil AnyIO)."""
    original_call = wr.RegisteredSubmitter.__call__
    original_run_sync = anyio.to_thread.run_sync

    async def traced_call(self, func, *args, **kwargs):
        caller = sys._getframe(1)
        site = f"{caller.f_code.co_filename.rsplit('/', 2)[-2]}/{caller.f_code.co_filename.rsplit('/', 1)[-1]}" \
               f":{caller.f_code.co_name}"
        req = REQ.get()
        journal.rec("SUBMIT", req=req, site=site)
        if kwargs:
            func = functools.partial(func, **kwargs)

        def body(*inner):
            journal.rec("CALLABLE_ENTER", req=req, site=site)
            try:
                return func(*inner)
            finally:
                journal.rec("CALLABLE_EXIT", req=req, site=site)

        return await original_call(self, body, *args)

    async def traced_run_sync(func, *args, **kwargs):
        req = REQ.get()

        def physical(*inner):
            journal.rec("PHYSICAL_ENTER", req=req)
            try:
                return func(*inner)
            finally:
                journal.rec("PHYSICAL_EXIT", req=req)

        return await original_run_sync(physical, *args, **kwargs)

    monkeypatch.setattr(wr.RegisteredSubmitter, "__call__", traced_call)
    monkeypatch.setattr(anyio.to_thread, "run_sync", traced_run_sync)


def install_worker_window(monkeypatch, journal, stage):
    """Élargit DÉTERMINISTIQUEMENT une fenêtre EXISTANTE d'anyio 4.15.1
    (WorkerThread.run) : 'before_check' (élément pris dans la file, avant
    `future.cancelled()`) ou 'after_check' (après ce test, avant
    context.run). Copie fidèle du corps d'origine + une attente unique.
    Rend (atteint, relâche)."""
    from anyio._backends import _asyncio as backend

    reached, release = threading.Event(), threading.Event()
    armed = {"on": True}

    def run(self):
        with backend.claim_worker_thread(backend.AsyncIOBackend, self.loop):
            while True:
                item = self.queue.get()
                if item is None:
                    return
                context, func, args, future, cancel_scope = item
                if armed["on"] and stage == "before_check":
                    armed["on"] = False
                    journal.rec("ANYIO_WINDOW", stage=stage)
                    reached.set()
                    release.wait(30)
                if not future.cancelled():
                    if armed["on"] and stage == "after_check":
                        armed["on"] = False
                        journal.rec("ANYIO_WINDOW", stage=stage)
                        reached.set()
                        release.wait(30)
                    result = None
                    exception = None
                    backend.threadlocals.current_cancel_scope = cancel_scope
                    try:
                        result = context.run(func, *args)
                    except BaseException as exc:  # noqa: BLE001
                        exception = exc
                    finally:
                        del backend.threadlocals.current_cancel_scope
                    try:
                        self.loop.call_soon_threadsafe(self._report_result, future, result, exception)
                    except RuntimeError:
                        if not self.loop.is_closed():
                            raise
                    del result, exception
                else:
                    journal.rec("ANYIO_SKIPPED_CANCELLED_FUTURE")
                self.queue.task_done()
                del item, context, func, args, future, cancel_scope

    monkeypatch.setattr(backend.WorkerThread, "run", run)
    return reached, release


IGNORED_THREADS = ("MainThread", "f04c-", "uvicorn-loop")
# Cadres sans effet métier : attente de file, mécanique AnyIO / threading,
# protocole du registre, enveloppes d'observation de ce banc.
_MECHANICS = ("threading.py", "queue.py", "anyio/_backends/_asyncio.py", "anyio/to_thread.py", "/asyncio/",
              "core/f2_runtime/work_registry.py", "tests/f04c_support.py", "concurrent/futures/thread.py")


def busy_threads():
    """Recensement PHYSIQUE : fils vivants qui exécutent du code métier ou de
    test (tout cadre hors mécanique AnyIO / threading / registre)."""
    frames = sys._current_frames()
    me = threading.get_ident()
    out = []
    for thread in threading.enumerate():
        if thread.ident == me or thread.name.startswith(IGNORED_THREADS):
            continue
        frame = frames.get(thread.ident)
        if frame is None:
            continue
        stack = traceback.extract_stack(frame)
        business = [f for f in stack if not f.filename.endswith(_MECHANICS)
                    and not any(m in f.filename for m in _MECHANICS)]
        if business:
            out.append(f"{thread.name}:{business[-1].name}")
    return out


def _spans(events):
    """Exécutions physiques par fil : [(enter, exit|None, [événements du fil entre les deux])]."""
    spans, current = [], {}
    for e in events:
        ident = e["ident"]
        if e["kind"] == "PHYSICAL_ENTER":
            current[ident] = [e, None, []]
            spans.append(current[ident])
        elif ident in current:
            if e["kind"] == "PHYSICAL_EXIT":
                current.pop(ident)[1] = e
            else:
                current[ident][2].append(e)
    return spans


def check_invariants(journal):
    """Invariants F04-C (freeze § 5, § 8.C) sur le journal. Rend {nom: [violations]}.

    J1 aucun callable métier hors job STARTED : dans une exécution physique,
       tout CALLABLE_ENTER suit un JOB_STARTED (sinon travail invisible) ;
       toute exécution physique sans job (soumetteur non suivi) est invisible ;
    J2 aucun callable après un abandon (JOB_START_REFUSED puis CALLABLE_ENTER) ;
    J3 FINISHED après la fin réelle : JOB_FINISHED suit CALLABLE_EXIT ;
    J4 aucune fermeture prématurée : RESOURCE_CLOSED après SEALED, après
       toute ADMISSION_RELEASED, tout JOB_FINISHED / JOB_ABANDONED et toute
       fin d'exécution physique non couverte par un job ; aucun fil métier
       actif (recensement) ni connexion empruntée à cet instant ; après
       SEALED, rien n'est admis ni inscrit, et aucun callable n'est invoqué ;
    J5 comptes : une transition terminale par job, une libération par
       admission, aucune VIOLATION signalée par le registre ;
    J6 issue honnête : GRACEFUL seulement si J4 tient ; DRAIN_TIMEOUT sans
       fermeture."""
    events = list(journal.events)
    out = {f"J{i}": [] for i in range(1, 7)}
    for enter, exit_, inner in _spans(events):
        kinds = [e["kind"] for e in inner]
        started = "JOB_STARTED" in kinds
        refused = "JOB_START_REFUSED" in kinds
        for i, e in enumerate(inner):
            if e["kind"] == "CALLABLE_ENTER":
                if refused and kinds.index("JOB_START_REFUSED") < i:
                    out["J2"].append(f"seq {e['seq']} : callable invoqué après abandon")
                if "JOB_STARTED" not in kinds[:i]:
                    out["J1"].append(f"seq {e['seq']} : callable hors job STARTED (req={e.get('req')})")
            if e["kind"] == "JOB_FINISHED" and "CALLABLE_EXIT" not in kinds[:i] and "CALLABLE_ENTER" in kinds[:i]:
                out["J3"].append(f"seq {e['seq']} : FINISHED avant la fin du callable")
        if not started and not refused:
            out["J1"].append(f"exécution physique seq {enter['seq']} sans job (req={enter.get('req')})")
    closed = [e for e in events if e["kind"] == "RESOURCE_CLOSED"]
    sealed = [e["seq"] for e in events if e["kind"] == "SEALED"]
    for c in closed:
        for e in events:
            if e["kind"] in ("ADMISSION_RELEASED", "CALLABLE_EXIT", "JOB_FINISHED", "JOB_ABANDONED",
                             "CALLABLE_ENTER") and e["seq"] > c["seq"]:
                out["J4"].append(f"{e['kind']} seq {e['seq']} après RESOURCE_CLOSED seq {c['seq']}")
        for enter, exit_, inner in _spans(events):
            covered = any(x["kind"] in ("JOB_FINISHED", "JOB_START_REFUSED") and x["seq"] < c["seq"] for x in inner)
            if enter["seq"] < c["seq"] and (exit_ is None or exit_["seq"] > c["seq"]) and not covered:
                out["J4"].append(f"exécution physique seq {enter['seq']} active à la fermeture")
        if not sealed or sealed[0] > c["seq"]:
            out["J4"].append("RESOURCE_CLOSED sans SEALED préalable")
        if c.get("busy"):
            out["J4"].append(f"fils actifs à la fermeture : {c['busy']}")
        if c.get("checked_out"):
            out["J4"].append(f"connexions empruntées à la fermeture : {c['checked_out']}")
    if sealed:
        for e in events:
            if e["seq"] > sealed[0] and e["kind"] in ("ADMITTED", "JOB_REGISTERED", "CALLABLE_ENTER"):
                out["J4"].append(f"{e['kind']} seq {e['seq']} après SEALED")
    terminal = {}
    for e in events:
        if e["kind"] in ("JOB_FINISHED", "JOB_ABANDONED"):
            terminal[e["job"]] = terminal.get(e["job"], 0) + 1
    out["J5"] += [f"job {j} : {n} transitions terminales" for j, n in terminal.items() if n > 1]
    released = {}
    for e in events:
        if e["kind"] == "ADMISSION_RELEASED":
            released[e["admission"]] = released.get(e["admission"], 0) + 1
    out["J5"] += [f"admission {a} libérée {n} fois" for a, n in released.items() if n > 1]
    out["J5"] += [f"VIOLATION {e}" for e in events if e["kind"] == "VIOLATION"]
    for o in (e for e in events if e["kind"] == "OUTCOME"):
        if o["outcome"] == ep.GRACEFUL and out["J4"]:
            out["J6"].append("issue GRACEFUL alors que J4 est violé")
        if o["outcome"] == ep.DRAIN_TIMEOUT and any(c["seq"] < o["seq"] for c in closed):
            out["J6"].append("DRAIN_TIMEOUT avec fermeture des ressources")
    return out


def violated(result):
    return {k: v for k, v in result.items() if v}


# --------------------------------------------------------------------------
# Banc : base jetable préparée, enveloppe réelle, doubles fournisseurs
# --------------------------------------------------------------------------

class Bench:
    """Enveloppe F04-C réelle sur base jetable (schéma déjà migré)."""

    def __init__(self, pg_url, journal, *, drain_timeout=None, claude=None):
        self.journal = journal
        self.environ = synthetic.environ(DATABASE_URL=pg_url)
        self.config = load_f2_config(self.environ)
        url = self.config.database_url
        self.admin = sa.create_engine(url, poolclass=sa.pool.NullPool)
        clean_f2(self.admin)
        with self.admin.begin() as conn:
            conn.execute(sa.text("DELETE FROM auth_abuse_key_state"))
        abuse.initialize_abuse_key_state(self.admin, self.config.keyring,
                                         self.config.keyring.current_version("abuse"))
        self.supabase, self.mailer = FakeSupabase(), FakeMailer()
        create_profile_with_identity(self.admin, self.supabase, EMAIL, USER)
        self.clock = FakeClock()
        self.eodhd, self.fmp = FakeEodhd(self.clock), FakeFmp(self.clock)
        self.claude = claude or FakeClaude(self.clock)
        self.engine = None
        self.closed = []
        self.hooks = {}
        self.envelope = ep.F2Envelope(environ=self.environ, drain_timeout=drain_timeout,
                                      resources_factory=self.resources, observer=journal.observe)

    def resources(self, config):
        owner = ResourceOwner()
        engine = create_f2_engine(config.database_url)

        def close_engine():
            self.journal.rec("RESOURCE_CLOSED", checked_out=engine.pool.checkedout(), busy=busy_threads())
            self.closed.append("engine")
            engine.dispose()

        owner.own("engine", engine, close_engine)
        for name, resource in (("supabase", self.supabase), ("resend", self.mailer), ("eodhd", self.eodhd),
                               ("fmp", self.fmp), ("claude", self.claude)):
            owner.own(name, resource, None)
        self.engine = engine
        return F2Resources(owner, engine=engine, supabase=self.supabase, mailer=self.mailer, eodhd=self.eodhd,
                           fmp=self.fmp, claude=self.claude)

    def install_runner_hook(self, monkeypatch):
        """Points de panne du runner (prevalidated, locked, …) pour les seuls
        runners construits par la composition F04-C (fault_hook absent)."""
        original = AuthorizedTransactionRunner.__init__
        bench = self

        def init(runner, engine, keyring, **kwargs):
            if kwargs.get("fault_hook") is None:
                kwargs["fault_hook"] = bench.hook
            original(runner, engine, keyring, **kwargs)

        monkeypatch.setattr(AuthorizedTransactionRunner, "__init__", init)

    def hook(self, point):
        action = self.hooks.get(point)
        if action is not None:
            action()

    def issue_session(self):
        """Session F2 réelle (moteur F2 de TEST, même base, mêmes clés)."""
        service = F2AuthService(self.admin, self.config.keyring, self.supabase, self.mailer)
        start = service.start_sign_in(email=EMAIL, client_ip="203.0.113.9")
        assert start.status == "issued", start
        done = service.verify_sign_in(operation_id=start.operation_id, candidate=start.candidate,
                                      code=self.mailer.last_code(EMAIL), client_ip="203.0.113.9")
        assert done.status == "authenticated", done
        assert service.authenticate_session(done.session.credential()) is not None
        return done.session

    def level(self):
        with self.admin.connect() as conn:
            return conn.execute(sa.text("SELECT level FROM users WHERE id = :u"), {"u": USER}).scalar()

    def count(self, table):
        with self.admin.connect() as conn:
            return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar()

    def dispose(self):
        self.admin.dispose()
        if self.engine is not None:
            self.engine.dispose()


class Gate:
    """Bloque un appel (fournisseur simulé ou point du runner) jusqu'à release."""

    def __init__(self):
        self.reached = threading.Event()
        self.release = threading.Event()
        self.count = 0

    def __call__(self, *args, **kwargs):
        self.count += 1
        self.reached.set()
        assert self.release.wait(60), "porte non relâchée"


# --------------------------------------------------------------------------
# Requêtes
# --------------------------------------------------------------------------

def get_headers(issued, label=None):
    headers = {"host": HOST, "sec-fetch-site": "same-origin", "x-oryx-generation-id": str(issued.generation_id),
               "cookie": active_cookie(issued)}
    if label:
        headers["x-f04c-label"] = label
    return headers


def mutation_headers(bench, issued, label=None):
    binding, token = csrf_pair(ORIGIN, keyring=bench.config.csrf_keyring)
    headers = {"host": HOST, "origin": ORIGIN, "sec-fetch-site": "same-origin", "x-oryx-csrf": token,
               "x-oryx-generation-id": str(issued.generation_id), "cookie": f"{active_cookie(issued)}; {binding}",
               "content-type": "application/json"}
    if label:
        headers["x-f04c-label"] = label
    return headers


def level_body(level="avance", revision="0"):
    return json.dumps({"level": level, "expected_revision": revision}).encode()


def turn_body(conversation_key="conv-f04c"):
    return json.dumps({"conversation_key": conversation_key, "client_turn_id": str(uuid.uuid4()),
                       "ticker_input": "MC.PA", "question": "Pourquoi ?", "context": "",
                       "last_method_id": None}).encode()


def identity_body(reply, conversation_key="conv-f04c"):
    data = json.loads(reply)
    return json.dumps({"reservation_id": data["reservation_id"], "conversation_key": conversation_key,
                       "assistant_turn_id": data["assistant_turn_id"]}).encode()


def scope_of(method, path, headers, *, raw_path=None):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https",
            "path": path, "raw_path": raw_path if raw_path is not None else path.encode(), "query_string": b"",
            "root_path": "", "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
            "client": ("192.0.2.10", 40000), "server": (HOST, 443)}


class Probe:
    """Sonde ASGI EXTÉRIEURE à l'enveloppe : libellé de requête et
    chronologie serveur (SERVER_CALL_BEGIN / END, HTTP_CANCELLED)."""

    def __init__(self, app, journal):
        self.app, self.journal = app, journal

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        label = dict(scope.get("headers") or []).get(b"x-f04c-label")
        req = label.decode() if label else None
        REQ.set(req)
        self.journal.rec("SERVER_CALL_BEGIN", req=req, path=scope["path"])

        async def observed_send(message):
            if message["type"] == "http.response.start":
                self.journal.rec("RESPONSE_START", req=req, status=message["status"])
            await send(message)

        try:
            return await self.app(scope, receive, observed_send)
        except asyncio.CancelledError as exc:
            self.journal.rec("HTTP_CANCELLED", req=req, msg=str(exc.args[0]) if exc.args else "")
            raise
        finally:
            self.journal.rec("SERVER_CALL_END", req=req)


async def asgi_call(app, method, path, headers, body=b"", *, hold=None):
    """Appel ASGI direct ; hold (asyncio.Event) : corps livré seulement après."""
    sent = []
    state = {"delivered": False}

    async def receive():
        if not state["delivered"]:
            if hold is not None:
                await hold.wait()
            state["delivered"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    await app(scope_of(method, path, headers), receive, send)
    return sent


def status_of_messages(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def body_of_messages(sent):
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


class Lifespan:
    """Pilote ASGI lifespan direct (même boucle que les requêtes)."""

    def __init__(self, app):
        self.app = app
        self.inbox = asyncio.Queue()
        self.outbox = asyncio.Queue()
        self.task = None

    async def _receive(self):
        return await self.inbox.get()

    async def _send(self, message):
        await self.outbox.put(message)

    async def startup(self):
        self.task = asyncio.ensure_future(self.app({"type": "lifespan", "asgi": {"version": "3.0"}},
                                                   self._receive, self._send))
        await self.inbox.put({"type": "lifespan.startup"})
        return await asyncio.wait_for(self.outbox.get(), 60)

    async def shutdown_begin(self):
        await self.inbox.put({"type": "lifespan.shutdown"})

    async def shutdown_result(self, timeout=60):
        return await asyncio.wait_for(self.outbox.get(), timeout)


# --------------------------------------------------------------------------
# Uvicorn 0.27.0 réel (fil dédié, 127.0.0.1) et client TCP brut
# --------------------------------------------------------------------------

class LiveServer:
    def __init__(self, app, journal, *, timeout_graceful_shutdown=None):
        import uvicorn

        self.journal = journal
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        config = uvicorn.Config(app, lifespan="on", log_level="warning", access_log=False, http="h11",
                                proxy_headers=False, timeout_graceful_shutdown=timeout_graceful_shutdown)
        self.server = uvicorn.Server(config)
        self.exited = threading.Event()
        self.thread = threading.Thread(target=self._run, name="uvicorn-loop", daemon=True)

    def _run(self):
        try:
            asyncio.run(self.server.serve(sockets=[self.sock]))
        finally:
            self.journal.rec("SERVER_EXITED")
            self.exited.set()

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started and not self.exited.is_set():
            assert time.monotonic() < deadline, "uvicorn non démarré"
            time.sleep(0.01)
        return self

    def request_exit(self):
        """Effet de Server.handle_exit(SIGTERM) : should_exit = True."""
        self.journal.rec("SHUTDOWN_REQUESTED")
        self.server.should_exit = True


def http_bytes(method, path, headers, body=b""):
    lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in headers.items()]
    if body or method in ("POST", "PUT"):
        lines.append(f"content-length: {len(body)}")
    lines.append("connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


class RawClient:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=60)

    def send(self, data):
        self.sock.sendall(data)

    def read_all(self, timeout=60):
        self.sock.settimeout(timeout)
        chunks = []
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def reset(self):
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.sock.close()


def status_of(raw):
    return int(raw.split(b"\r\n", 1)[0].split(b" ")[1]) if raw else None


def driver(fn, name="f04c-driver"):
    result = {}

    def run():
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread, result


# --------------------------------------------------------------------------
# Preuves (chronologies)
# --------------------------------------------------------------------------

def evidence_dir():
    """<racine>/reports/f04c lorsque reports/ existe (jobs CI : artefact
    reports-postgres-NN), sinon un répertoire temporaire. Suffixe .log : ces
    journaux de TEST citent des routes et des sites F2 (ce ne sont pas des
    modules applicatifs)."""
    import pathlib
    import tempfile

    root = pathlib.Path(__file__).resolve().parents[1] / "reports"
    target = root / "f04c" if root.is_dir() else pathlib.Path(tempfile.gettempdir()) / "oryx-f04c-evidence"
    target.mkdir(parents=True, exist_ok=True)
    return target


def save_evidence(name, journal, **observations):
    result = check_invariants(journal)
    payload = {"scenario": name, "invariants": {k: ("OK" if not v else "VIOLATED") for k, v in result.items()},
               "violations": result, "observations": observations, "timeline": journal.timeline()}
    (evidence_dir() / f"{name}.log").write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str),
                                                encoding="utf-8")
    return result
