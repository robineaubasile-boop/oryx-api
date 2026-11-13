"""Sonde Q2 F04-C (ACT-L1 / F04-C V0.3, freeze § 7 et § 8.D) — exécutée par
tests/test_f04c_legacy_engine.py dans un sous-processus Python NEUF par
scénario, environnement contrôlé.

L'instrumentation est posée AVANT tout import de code Oryx : graphe d'import
(importeur direct), sqlalchemy.create_engine (3 chemins), événements de pool
(connect / checkout / close) et Engine.engine_connect, Engine.dispose,
Session.__init__ (moteur lié), psycopg2.connect, socket.connect ; après
import de core.db (s'il a lieu) : espions core.db.get_db / SessionLocal.

Les doubles fournisseurs sont EXTRAITS du source de tests/f2_support.py et
tests/authz_f2_pr9b2_support.py (exécutés sans importer ces modules, qui
chargent core.db et api) : la sonde n'importe aucun module de test qui
fausserait l'observation.

usage : python f04c_q2_probe.py <scénario> <sortie.json>
"""
import gc
import json
import os
import sys
import threading
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
scenario, out_path = sys.argv[1], sys.argv[2]

REPORT = {"scenario": scenario, "env_has_DATABASE_URL": "DATABASE_URL" in os.environ,
          "events": [], "import_edges": [], "errors": []}
PHASE = {"name": "bootstrap"}


def ev(kind, **fields):
    REPORT["events"].append({"phase": PHASE["name"], "kind": kind, **fields})


def caller(skip_files=("f04c_q2_probe.py", "sqlalchemy", "importlib", "<frozen")):
    for frame in reversed(traceback.extract_stack()[:-2]):
        if not any(s in frame.filename for s in skip_files):
            return f"{frame.filename.replace(REPO + '/', '')}:{frame.lineno} ({frame.name})"
    return "?"


# --------------------------------------------------------------------------
# 1. Graphe d'import : importeur direct de chaque module core.* / api / tests.*
# --------------------------------------------------------------------------
import builtins  # noqa: E402

_real_import = builtins.__import__
WATCH = ("core", "api", "tests")


def _importer_name(frame):
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        if not name.startswith(("importlib", "_frozen_importlib")):
            return name
        frame = frame.f_back
    return "?"


def tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split(".")[0] in WATCH and name not in sys.modules:
        importer = (globals or {}).get("__name__", "?")
        REPORT["import_edges"].append([importer, name])
    elif level > 0:
        pass
    module = _real_import(name, globals, locals, fromlist, level)
    if fromlist and level == 0 and name.split(".")[0] in WATCH:
        for item in fromlist:
            full = f"{name}.{item}"
            if full in sys.modules and [((globals or {}).get("__name__", "?")), full] not in REPORT["import_edges"]:
                REPORT["import_edges"].append([(globals or {}).get("__name__", "?"), full])
    return module


builtins.__import__ = tracking_import

# --------------------------------------------------------------------------
# 2. SQLAlchemy : create_engine, Engine.__init__, pool, Session ; psycopg2 ;
#    socket. Tout est posé AVANT l'import de core.*.
# --------------------------------------------------------------------------
import socket  # noqa: E402

import psycopg2  # noqa: E402
import sqlalchemy  # noqa: E402
import sqlalchemy.engine  # noqa: E402
import sqlalchemy.engine.create  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine.base import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import Pool  # noqa: E402

ENGINES = {}  # id(engine) -> infos
POOL_OWNER = {}  # id(pool) -> engine label


def relabel(engine, label):
    ENGINES[id(engine)]["label"] = label
    for pool_id, owner in list(POOL_OWNER.items()):
        if owner == ENGINES[id(engine)]["label"] or pool_id == id(engine.pool):
            POOL_OWNER[pool_id] = label
    POOL_OWNER[id(engine.pool)] = label


def label_of(engine):
    info = ENGINES.get(id(engine))
    return info["label"] if info else f"unknown-{id(engine):x}"


_real_create_engine = sqlalchemy.engine.create.create_engine


def register_engine(engine, site):
    n = len(ENGINES) + 1
    origin = "core.db (legacy, import)" if "core/db.py" in site else (
        "core.f2_runtime.database (F04-B)" if "f2_runtime/database.py" in site else site)
    ENGINES[id(engine)] = {"label": f"E{n}", "origin": origin, "site": site, "url": engine.url.render_as_string(
        hide_password=True), "pool": type(engine.pool).__name__, "phase": PHASE["name"]}
    POOL_OWNER[id(engine.pool)] = f"E{n}"
    ev("engine_created", engine=f"E{n}", origin=origin, site=site)


def recording_create_engine(*args, **kwargs):
    # Engine.__init__ n'est PAS enveloppé : SQLAlchemy introspecte sa signature.
    site = caller()
    ev("create_engine_call", site=site, poolclass=getattr(kwargs.get("poolclass"), "__name__", None),
       isolation_level=kwargs.get("isolation_level"))
    engine = _real_create_engine(*args, **kwargs)
    register_engine(engine, site)
    return engine


sqlalchemy.create_engine = recording_create_engine
sqlalchemy.engine.create_engine = recording_create_engine
sqlalchemy.engine.create.create_engine = recording_create_engine


@event.listens_for(Pool, "connect")
def _on_connect(dbapi_conn, record):
    ev("pool_physical_connect", engine=POOL_OWNER.get(id(record._ConnectionRecord__pool), "?"), site=caller())


@event.listens_for(Pool, "checkout")
def _on_checkout(dbapi_conn, record, proxy):
    ev("pool_checkout", engine=POOL_OWNER.get(id(record._ConnectionRecord__pool), "?"))


@event.listens_for(Pool, "close")
def _on_close(dbapi_conn, record):
    ev("pool_physical_close", engine=POOL_OWNER.get(id(record._ConnectionRecord__pool), "?"))


@event.listens_for(Engine, "engine_connect")
def _on_engine_connect(conn):
    ev("engine_connect", engine=label_of(conn.engine), site=caller())


_real_dispose = Engine.dispose


def recording_dispose(self, *a, **k):
    ev("engine_dispose", engine=label_of(self), site=caller())
    result = _real_dispose(self, *a, **k)
    POOL_OWNER[id(self.pool)] = label_of(self)  # nouveau pool après dispose
    return result


Engine.dispose = recording_dispose

_real_session_init = Session.__init__


def recording_session_init(self, *args, **kwargs):
    bind = kwargs.get("bind", args[0] if args else None)
    engine = getattr(bind, "engine", bind)
    ev("orm_session", bind_kind=type(bind).__name__, engine=label_of(engine) if engine is not None else None,
       cls=type(self).__name__, site=caller())
    _real_session_init(self, *args, **kwargs)


Session.__init__ = recording_session_init

_real_pg_connect = psycopg2.connect


def recording_pg_connect(*args, **kwargs):
    ev("psycopg2_connect", host=kwargs.get("host"), port=kwargs.get("port"), dbname=kwargs.get("dbname"),
       site=caller())
    return _real_pg_connect(*args, **kwargs)


psycopg2.connect = recording_pg_connect
_real_sock_connect = socket.socket.connect


def recording_sock_connect(self, address):
    ev("socket_connect", address=list(address) if isinstance(address, tuple) else str(address), site=caller())
    return _real_sock_connect(self, address)


socket.socket.connect = recording_sock_connect


def modules_snapshot():
    mods = sorted(m for m in sys.modules if m.split(".")[0] in ("core", "api"))
    return {"api": "api" in sys.modules, "core.db": "core.db" in sys.modules,
            "core.models": "core.models" in sys.modules, "core_modules_count": len(mods),
            "f2_runtime": [m for m in mods if m.startswith("core.f2_runtime")],
            "legacy_engine_attr": (repr(type(sys.modules["core.db"].engine).__name__)
                                   if "core.db" in sys.modules else None),
            "legacy_SessionLocal_is_None": (sys.modules["core.db"].SessionLocal is None
                                            if "core.db" in sys.modules else None)}


def chain_to(target):
    """Plus courte chaîne d'import observée vers target."""
    parents = {}
    for importer, name in REPORT["import_edges"]:
        parents.setdefault(name, importer)
    chain, node = [target], target
    while node in parents and parents[node] not in chain:
        node = parents[node]
        chain.append(node)
    return list(reversed(chain))


def instrument_legacy_names():
    """Après import : enveloppe core.db.get_db / SessionLocal (appel = usage)."""
    if "core.db" not in sys.modules:
        return
    db = sys.modules["core.db"]
    real_get_db = db.get_db

    def get_db_spy():
        ev("legacy_get_db_called", site=caller())
        yield from real_get_db()

    db.get_db = get_db_spy
    if db.SessionLocal is not None:
        real_sl = db.SessionLocal

        class SessionLocalSpy:
            def __call__(self, *a, **k):
                ev("legacy_SessionLocal_called", site=caller())
                return real_sl(*a, **k)

            def __getattr__(self, item):
                return getattr(real_sl, item)

        db.SessionLocal = SessionLocalSpy()


def alive_engines():
    out = []
    for obj in gc.get_objects():
        if isinstance(obj, Engine) and id(obj) not in ENGINES and type(obj) is Engine:
            ev("engine_not_created_by_create_engine", site="gc")
        if isinstance(obj, Engine):
            pool = obj.pool
            out.append({"engine": label_of(obj), "pool": type(pool).__name__,
                        "checkedout": getattr(pool, "checkedout", lambda: None)(),
                        "checkedin": getattr(pool, "checkedin", lambda: None)()})
    return out


def counts_by_engine(kind, phase=None):
    out = {}
    for e in REPORT["events"]:
        if e["kind"] == kind and (phase is None or e["phase"] == phase):
            out[e["engine"]] = out.get(e["engine"], 0) + 1
    return out




def instrument_after_import():
    instrument_legacy_names()


SYNTHETIC_KEYS = None


def synthetic_environ(**overrides):
    """Données pures de tests/f04b_support.py (n'importe rien d'Oryx sauf
    core.auth_f2.keys.PURPOSES, sans core.db)."""
    from tests.f04b_support import environ
    return environ(**overrides)


def extract(path, start_marker, end_marker, namespace):
    text = open(os.path.join(REPO, path), encoding="utf-8").read()
    chunk = text[text.index(start_marker):text.index(end_marker)]
    exec(compile(chunk, f"{path}[extrait]", "exec"), namespace)
    return namespace


def fakes():
    import copy
    import itertools
    import re
    from datetime import timedelta

    import sqlalchemy as sa

    from core.auth_f2.providers import GeneratedOtp, MailSent, ProviderRejected, VerifiedIdentity

    ns = {"itertools": itertools, "re": re, "timedelta": timedelta, "sa": sa, "GeneratedOtp": GeneratedOtp,
          "MailSent": MailSent, "ProviderRejected": ProviderRejected, "VerifiedIdentity": VerifiedIdentity,
          "copy": copy, "__name__": "f04c_q2_fakes"}
    extract("tests/f2_support.py", "ISSUER = ", "def fake_key(", ns)
    extract("tests/f2_support.py", "class FakeSupabase:", "class SimulatedCrash", ns)
    extract("tests/authz_f2_pr9b2_support.py", "MODEL = ", "def reply(", ns)
    return ns


def run_lifespan(envelope, steps):
    """Démarre l'enveloppe par son VRAI lifespan, exécute steps(envelope) dans
    la même boucle, puis l'arrête ; rend (startup, shutdown|None)."""
    import asyncio

    async def main():
        inbox, outbox = asyncio.Queue(), asyncio.Queue()
        task = asyncio.ensure_future(envelope({"type": "lifespan", "asgi": {"version": "3.0"}}, inbox.get, outbox.put))
        await inbox.put({"type": "lifespan.startup"})
        started = await asyncio.wait_for(outbox.get(), 60)
        if started["type"] != "lifespan.startup.complete":
            await task
            return started, None
        PHASE["name"] = "f2_operations"
        await steps(envelope)
        PHASE["name"] = "shutdown"
        await inbox.put({"type": "lifespan.shutdown"})
        stopped = await asyncio.wait_for(outbox.get(), 60)
        await task
        return started, stopped

    return asyncio.run(main())


def observer_engine(url):
    import sqlalchemy as sa

    engine = sa.create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1),
                              poolclass=sa.pool.NullPool)
    relabel(engine, "OBSERVER")
    return engine


def backends_left(observer):
    """Connexions clientes de la base hors observateur (comparées avant /
    après : seules celles ouvertes PAR CE PROCESSUS peuvent varier)."""
    import sqlalchemy as sa

    with observer.connect() as conn:
        return conn.execute(sa.text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND backend_type = 'client backend'")).scalar()


def import_entrypoint():
    PHASE["name"] = "import"
    import core.f2_runtime.entrypoint as entry
    REPORT["after_import"] = modules_snapshot()
    return entry


def startup_only(env):
    entry = import_entrypoint()
    PHASE["name"] = "startup"
    envelope = entry.F2Envelope(environ=env)

    async def nothing(_):
        return None

    started, stopped = run_lifespan(envelope, nothing)
    REPORT["startup"] = started
    REPORT["shutdown"] = stopped
    REPORT["failure"] = envelope.failure
    REPORT["after_startup"] = modules_snapshot()
    REPORT["chain_core_db"] = chain_to("core.db") if "core.db" in sys.modules else None
    instrument_after_import()
    return envelope


def q2_01():
    import_entrypoint()


def q2_02():
    import_entrypoint()


def q2_03():
    startup_only(synthetic_environ(DATABASE_URL=os.environ["Q2_INVALID_URL"]))


def q2_04():
    startup_only(synthetic_environ(DATABASE_URL=os.environ["DATABASE_URL"]))


def prepare_database(config, url, ns):
    import sqlalchemy as sa

    from core.auth_f2 import abuse

    PHASE["name"] = "setup_observer"
    observer = observer_engine(url)
    with observer.begin() as conn:
        name, comment = conn.execute(sa.text(
            "SELECT current_database(), shobj_description(oid, 'pg_database') FROM pg_database "
            "WHERE datname = current_database()")).one()
        assert comment == "oryx-disposable-test-db" and name.endswith("_test"), "base non jetable"
        conn.execute(sa.text("TRUNCATE auth_operation_results, oryx_session_generations, oryx_sessions, "
                             "auth_abuse_events, auth_abuse_subjects, auth_abuse_key_state, "
                             "auth_otp_verification_attempts, auth_otp_requests, auth_preauth_candidates, "
                             "auth_operations, invitations, auth_identities, users CASCADE"))
    abuse.initialize_abuse_key_state(observer, config.keyring, config.keyring.current_version("abuse"))
    supabase, mailer = ns["FakeSupabase"](), ns["FakeMailer"]()
    with observer.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('q2-user', 'debutant')"))
        conn.execute(sa.text(
            "INSERT INTO auth_identities (id, user_id, provider, issuer, subject, verified_email_normalized, "
            "email_verified_at, created_at) VALUES (gen_random_uuid(), 'q2-user', 'supabase', :i, :s, :e, now(), now())"),
            {"i": ns["ISSUER"], "s": supabase.subject_for("q2@example.com"), "e": "q2@example.com"})
    REPORT["backends_before"] = backends_left(observer)
    return observer, supabase, mailer


def fake_resources_factory(supabase, mailer, ns, *, tamper=None):
    def factory(config):
        from core.f2_runtime.database import create_f2_engine
        from core.f2_runtime.resources import F2Resources, ResourceOwner

        owner = ResourceOwner()
        engine = create_f2_engine(config.database_url)
        owner.own("engine", engine, engine.dispose)
        clock = ns["FakeClock"]()
        transports = {"supabase": supabase, "mailer": mailer, "eodhd": ns["FakeEodhd"](clock),
                      "fmp": ns["FakeFmp"](clock), "claude": ns["FakeClaude"](clock)}
        for name, resource in transports.items():
            owner.own(name, resource, None)
        return F2Resources(owner, engine=engine, **transports)
    return factory


async def journey(envelope, mailer, statuses):
    import uuid

    import httpx

    host = "oryx-f04b.example"
    origin = "https://" + host
    transport = httpx.ASGITransport(app=envelope, client=("192.0.2.10", 40000))
    async with httpx.AsyncClient(transport=transport, base_url=origin) as client:
        boot = await client.get("/api/auth/f2/csrf/bootstrap", headers={"sec-fetch-site": "same-origin"})
        statuses["bootstrap"] = boot.status_code
        token = boot.json()["csrf_token"]
        binding_name = next(k for k in client.cookies.keys() if "csrf" in k)
        binding = client.cookies[binding_name]
        base = {"origin": origin, "x-oryx-csrf": token, "sec-fetch-site": "same-origin"}
        start = await client.post("/api/auth/f2/sign-in/start", json={"email": "q2@example.com"}, headers=base)
        statuses["sign_in_start"] = start.status_code
        sb = start.json()
        verify = await client.post("/api/auth/f2/sign-in/verify", headers=base, json={
            "operation_id": sb["operation_id"], "candidate_id": sb["candidate_id"],
            "code": mailer.last_code("q2@example.com")})
        statuses["sign_in_verify"] = verify.status_code
        gen = verify.json()["generation_id"]
        resolve = await client.post("/api/auth/f2/session/resolve", headers=base, json={"generation_id": gen})
        statuses["session_resolve"] = resolve.status_code
        actives = {k: v for k, v in client.cookies.items() if k.startswith("__Host-oryx-active-")}
        cookie = "; ".join([f"{binding_name}={binding}"] + [f"{k}={v}" for k, v in actives.items()])
        client.cookies.clear()
        get = {"sec-fetch-site": "same-origin", "x-oryx-generation-id": gen, "cookie": cookie}
        mut = {**base, "x-oryx-generation-id": gen, "cookie": cookie, "content-type": "application/json"}
        statuses["me"] = (await client.get("/api/v2/me", headers=get)).status_code
        statuses["me_level"] = (await client.put("/api/v2/me/level", headers=mut, content=json.dumps(
            {"level": "avance", "expected_revision": "0"}))).status_code
        turn = await client.post("/api/v2/me/decryptage/turns", headers=mut, content=json.dumps(
            {"conversation_key": "conv-q2", "client_turn_id": str(uuid.uuid4()), "ticker_input": "MC.PA",
             "question": "Pourquoi ?", "context": "", "last_method_id": None}))
        statuses["turns"] = turn.status_code
        data = turn.json()
        ids = json.dumps({"reservation_id": data["reservation_id"], "conversation_key": "conv-q2",
                          "assistant_turn_id": data["assistant_turn_id"]})
        statuses["replay"] = (await client.post("/api/v2/me/decryptage/replay", headers=mut, content=ids)).status_code
        statuses["ack"] = (await client.post("/api/v2/me/decryptage/ack", headers=mut, content=ids)).status_code
        statuses["portfolio"] = (await client.get("/api/v2/me/portfolio", headers=get)).status_code
        statuses["theses"] = (await client.get("/api/v2/me/theses", headers=get)).status_code


def q2_05():
    import sqlalchemy as sa

    entry = import_entrypoint()
    from core.f2_runtime.config import load_f2_config

    url = os.environ["DATABASE_URL"]
    env = synthetic_environ(DATABASE_URL=url)
    ns = fakes()
    observer, supabase, mailer = prepare_database(load_f2_config(env), url, ns)
    REPORT["before_startup"] = modules_snapshot()
    PHASE["name"] = "startup"
    envelope = entry.F2Envelope(environ=env, resources_factory=fake_resources_factory(supabase, mailer, ns))
    statuses = {}

    async def steps(env_):
        instrument_after_import()
        REPORT["after_startup"] = modules_snapshot()
        REPORT["chain_core_db"] = chain_to("core.db")
        REPORT["engine_refs"] = engine_refs_f2()
        await journey(env_, mailer, statuses)
        REPORT["f2_engine_label"] = label_of(env_.resources.engine)

    started, stopped = run_lifespan(envelope, steps)
    REPORT.update(startup=started, shutdown=stopped, outcome=envelope.outcome, statuses=statuses)
    PHASE["name"] = "verification"
    with observer.connect() as conn:
        REPORT["db_effects"] = {
            "level": conn.execute(sa.text("SELECT level FROM users WHERE id='q2-user'")).scalar(),
            "delivered": conn.execute(sa.text(
                "SELECT count(*) FROM assistant_deliveries WHERE status='delivered'")).scalar()}
    REPORT["backends_after"] = backends_left(observer)
    observer.dispose()


def q2_06():
    entry = import_entrypoint()
    from core.f2_runtime.config import load_f2_config
    from core.f2_runtime.resources import build_f2_resources

    url = os.environ["DATABASE_URL"]
    env = synthetic_environ(DATABASE_URL=url)
    ns = fakes()
    observer, supabase, mailer = prepare_database(load_f2_config(env), url, ns)
    # (a) échec de construction des fournisseurs (étape resources, nettoyage F04-B)
    PHASE["name"] = "resources_failure"
    envelope = entry.F2Envelope(environ=env, resources_factory=lambda c: build_f2_resources(
        c, claude_http_client=object()))

    async def nothing(_):
        return None

    a_start, _ = run_lifespan(envelope, nothing)
    REPORT["resources_failure"] = {"startup": a_start, "failure": envelope.failure,
                                   "cleanup": str(envelope.close_report), "core.db": "core.db" in sys.modules}
    # (b) échec APRÈS précontrôle, à la composition (import de core.db effectué)
    PHASE["name"] = "composition_failure"
    import core.auth_f2_http.isolated as auth_iso
    original = auth_iso.build_isolated_f2_app

    def with_docs(**kwargs):
        app = original(**kwargs)
        app.docs_url = "/docs"
        return app

    auth_iso.build_isolated_f2_app = with_docs
    envelope = entry.F2Envelope(environ=env, resources_factory=fake_resources_factory(supabase, mailer, ns))
    b_start, _ = run_lifespan(envelope, nothing)
    instrument_after_import()
    REPORT["composition_failure"] = {"startup": b_start, "failure": envelope.failure,
                                     "cleanup": str(envelope.close_report), "core.db": "core.db" in sys.modules,
                                     "chain_core_db": chain_to("core.db") if "core.db" in sys.modules else None}
    REPORT["backends_after"] = backends_left(observer)
    observer.dispose()


def engine_refs_f2():
    from core.auth_f2.http_admission import HttpAdmission
    from core.auth_f2.service import F2AuthService
    from core.authz_f2.runner import AuthorizedTransactionRunner

    refs = {}
    for obj in gc.get_objects():
        t = type(obj)
        if t in (F2AuthService, HttpAdmission, AuthorizedTransactionRunner):
            refs.setdefault(t.__name__, []).append(label_of(getattr(obj, "_engine", None)))
    return refs


SCENARIOS = {"Q2-01": q2_01, "Q2-02": q2_02, "Q2-03": q2_03, "Q2-04": q2_04, "Q2-05": q2_05, "Q2-06": q2_06}

try:
    SCENARIOS[scenario]()
except BaseException as exc:  # noqa: BLE001
    REPORT["errors"].append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}")

PHASE["name"] = "final"
gc.collect()
REPORT["final_modules"] = modules_snapshot()
REPORT["alive_engines_after_cleanup"] = alive_engines()
REPORT["threads_alive"] = sorted(t.name for t in threading.enumerate())
REPORT["engines"] = list(ENGINES.values())
REPORT["create_engine_calls"] = [e for e in REPORT["events"] if e["kind"] == "create_engine_call"]
REPORT["physical_connects_by_engine"] = counts_by_engine("pool_physical_connect")
REPORT["checkouts_by_engine"] = counts_by_engine("pool_checkout")
REPORT["checkouts_in_f2_window"] = counts_by_engine("pool_checkout", "f2_operations")
REPORT["engine_connect_by_engine"] = counts_by_engine("engine_connect")
REPORT["orm_sessions"] = [e for e in REPORT["events"] if e["kind"] == "orm_session"]
REPORT["legacy_calls"] = [e for e in REPORT["events"] if e["kind"].startswith("legacy_")]
REPORT["psycopg2_connects"] = [e for e in REPORT["events"] if e["kind"] == "psycopg2_connect"]
REPORT["socket_connects"] = [e for e in REPORT["events"] if e["kind"] == "socket_connect"]
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(REPORT, fh, indent=1, ensure_ascii=False, default=str)
