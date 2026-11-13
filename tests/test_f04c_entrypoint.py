"""ACT-L1 / F04-C V0.3 — point d'entrée dormant : import, démarrage, composition,
lifespan aval, routage F-02A octet pour octet et maintenance.

Freeze § 3, § 4, § 6 et § 8.A / § 8.B. PostgreSQL jetable réel
(ORYX_TEST_DATABASE_URL) pour tout démarrage : le précontrôle F04-B est réel.
"""
import asyncio
import json
import subprocess
import sys

import pytest
import sqlalchemy as sa
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.auth_f2.http_admission import HttpAdmission
from core.auth_f2.service import F2AuthService
from core.authz_f2.runner import AuthorizedTransactionRunner
from core.f2_runtime import entrypoint as ep
from core.f2_runtime import work_registry as wr
from core.f2_runtime.resources import build_f2_resources
from core.route_policy import registry
from core.route_policy.gate import FORWARD_F2_AUTH, FORWARD_F2_PERSONAL, RoutePolicyGate, classify
from tests import f04b_support as synthetic
from tests import f04c_support as f
from tests.f2_support import prepare_schema
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture

FORWARD = (FORWARD_F2_AUTH, FORWARD_F2_PERSONAL)
MAINTENANCE_HEADERS = [(b"content-type", b"application/json"), (b"content-length", b"35"),
                       (b"cache-control", b"no-store"), (b"pragma", b"no-cache"),
                       (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]


@pytest.fixture(scope="module")
def migrated(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()
    return pg_url


@pytest.fixture
def journal():
    return f.Journal()


@pytest.fixture
def bench(migrated, journal):
    environment = f.Bench(migrated, journal)
    yield environment
    environment.dispose()


def no_secret(text):
    values = list(synthetic.SECRET_VALUES) + synthetic.all_key_strings()
    return not any(v in text for v in values) and "postgresql" not in text and "@" not in text


# --------------------------------------------------------------------------
# A. Import et dormance
# --------------------------------------------------------------------------

IMPORT_PROBE = r'''
import gc, json, os, socket, sys, threading
reads = []
class Spy(type(os.environ)):
    def __getitem__(self, key):
        reads.append(key)
        return super().__getitem__(key)
spy = Spy(os.environ._data, os.environ.encodekey, os.environ.decodekey, os.environ.encodevalue,
          os.environ.decodevalue)
os.environ = spy
attempts = []
socket.socket.connect = lambda self, address: attempts.append(repr(address))
import psycopg2
psycopg2.connect = lambda *a, **k: attempts.append("psycopg2")
before = threading.active_count()
import core.f2_runtime.entrypoint as entry
import sqlalchemy as sa
engines = sum(1 for o in gc.get_objects() if isinstance(o, sa.engine.Engine))
print(json.dumps({
    "state": entry.app.state,
    "loaded": sorted(m for m in ("api", "core.db", "core.models", "fastapi", "core.auth_f2_http.isolated",
                                 "core.authz_f2_http.isolated", "core.authz_f2.runner", "core.f2_runtime.config",
                                 "core.f2_runtime.resources", "core.f2_runtime.preflight") if m in sys.modules),
    "engines": engines, "attempts": attempts, "threads": threading.active_count() - before,
    "f2_reads": sorted({k for k in reads if k.startswith("ORYX_F2_") or k == "DATABASE_URL"}),
}))
'''


@pytest.mark.parametrize("with_configuration", [False, True])
def test_importing_the_entrypoint_reads_nothing_connects_nothing_and_loads_no_legacy(with_configuration):
    extra = synthetic.environ() if with_configuration else {}
    out = subprocess.run([sys.executable, "-c", IMPORT_PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env(extra), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result == {"state": ep.INERT, "loaded": [], "engines": 0, "attempts": [], "threads": 0, "f2_reads": []}


def test_no_application_module_imports_the_f04c_entrypoint():
    offenders = []
    for path in REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in ("tests", "docs") or ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        if str(relative) in ("core/f2_runtime/entrypoint.py", "core/f2_runtime/work_registry.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in text for marker in ("f2_runtime.entrypoint", "f2_runtime import entrypoint",
                                             "work_registry", "F2Envelope", "RegisteredSubmitter")):
            offenders.append(str(relative))
    assert offenders == []


def test_the_registry_module_has_no_import_effect():
    out = subprocess.run([sys.executable, "-c", "import threading, sys; import core.f2_runtime.work_registry; "
                          "print(threading.active_count(), 'core.db' in sys.modules, 'fastapi' in sys.modules)"],
                         cwd=REPO_ROOT, capture_output=True, text=True, env=_clean_env({}), timeout=60)
    assert out.stdout.split() == ["1", "False", "False"], out.stderr[-1000:]


# --------------------------------------------------------------------------
# A. Démarrage, composition, refus fermés
# --------------------------------------------------------------------------

def start(envelope):
    async def main():
        life = f.Lifespan(envelope)
        message = await life.startup()
        return message
    return asyncio.run(main())


class Captured:
    """Enregistre les arguments réels des constructeurs pendant le démarrage."""

    def __init__(self, monkeypatch):
        self.calls = {}
        for cls in (F2AuthService, HttpAdmission, AuthorizedTransactionRunner):
            self._wrap(monkeypatch, cls)
        import core.auth_f2_http.isolated as auth_iso
        import core.authz_f2_http.isolated as pers_iso
        for module, name in ((auth_iso, "build_isolated_f2_app"), (pers_iso, "build_isolated_personal_app")):
            self._wrap_builder(monkeypatch, module, name)

    def _wrap(self, monkeypatch, cls):
        original = cls.__init__
        calls = self.calls.setdefault(cls.__name__, [])

        def init(instance, engine, keyring, *args, **kwargs):
            calls.append({"engine": engine, "keyring": keyring, **kwargs})
            original(instance, engine, keyring, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", init)

    def _wrap_builder(self, monkeypatch, module, name):
        original = getattr(module, name)
        calls = self.calls.setdefault(name, [])

        def builder(**kwargs):
            app = original(**kwargs)
            calls.append({**kwargs, "app": app})
            return app

        monkeypatch.setattr(module, name, builder)


def test_a_successful_startup_composes_both_apps_with_one_engine_same_keys_and_one_submitter(bench, monkeypatch):
    captured = Captured(monkeypatch)
    envelope = bench.envelope
    assert envelope.state == ep.INERT
    assert start(envelope) == {"type": "lifespan.startup.complete"}
    assert envelope.state == ep.OPEN and envelope.outcome is None
    [auth_call] = captured.calls["build_isolated_f2_app"]
    [personal_call] = captured.calls["build_isolated_personal_app"]
    engine = envelope.resources.engine
    assert engine is bench.engine and auth_call["engine"] is engine and personal_call["engine"] is engine
    assert auth_call["keyring"] is personal_call["keyring"]  # MÊME objet Keyring Auth
    assert auth_call["csrf_keyring"] is personal_call["csrf_keyring"]  # MÊME objet CsrfKeyring
    assert auth_call["submitter"] is personal_call["submitter"] is envelope.submitter
    assert type(envelope.submitter) is wr.RegisteredSubmitter and envelope.submitter.registry is envelope.registry
    assert personal_call["decryptage"] is not None  # C3 obligatoire
    for name in ("F2AuthService", "HttpAdmission", "AuthorizedTransactionRunner"):
        assert [c["engine"] for c in captured.calls[name]] == [engine], name
        assert all(c["keyring"] is auth_call["keyring"] for c in captured.calls[name]), name
    for call, category in ((auth_call, registry.F2_AUTH), (personal_call, registry.F2_PERSONAL)):
        app = call["app"]
        assert ep.exposed_pairs(app) == registry.pairs_of(category)
        assert app.state.oryx_f2_submitter is envelope.submitter
        assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None
        assert not ep.downstream_lifespan_is_active(app)
    assert len(registry.pairs_of(registry.F2_AUTH)) == 14 and len(registry.pairs_of(registry.F2_PERSONAL)) == 13
    assert engine.pool.checkedout() == 0
    assert "api" not in sys.modules or not any(getattr(o, "__module__", "") == "api" for o in (
        auth_call["app"], personal_call["app"]))


def failed(bench):
    message = start(bench.envelope)
    assert message["type"] == "lifespan.startup.failed", message
    assert no_secret(message["message"]), message
    assert bench.envelope.state == ep.CLOSED and bench.envelope.outcome == ep.STARTUP_FAILED
    return message, bench.envelope.failure


def refuses_f2_afterwards(bench):
    """Toute requête F2 (ici GET /me, en-têtes quelconques) : 503 maintenance."""
    headers = {"host": f.HOST, "sec-fetch-site": "same-origin",
               "x-oryx-generation-id": "0d9a6c4e-1b2a-4c3d-8e9f-001122334455"}

    async def main():
        return await f.asgi_call(bench.envelope, "GET", f.ME, headers)

    sent = asyncio.run(main())
    assert f.status_of_messages(sent) == 503 and f.body_of_messages(sent) == ep.MAINTENANCE_BODY


def test_an_incomplete_configuration_fails_before_any_resource(migrated, journal):
    bench = f.Bench(migrated, journal)
    try:
        del bench.environ["ORYX_F2_FMP_API_KEY"]
        bench.envelope = ep.F2Envelope(environ=bench.environ, resources_factory=bench.resources,
                                       observer=journal.observe)
        _, failure = failed(bench)
        assert failure == ("config", "F2ConfigError") and bench.engine is None
        refuses_f2_afterwards(bench)
    finally:
        bench.dispose()


@pytest.mark.parametrize("url", ["postgresql://u:SECRET@127.0.0.1:notaport/x", "mysql://u:SECRET@127.0.0.1:1/x",
                                 "postgresql+asyncpg://u:SECRET@127.0.0.1:1/x", "garbage SECRET"])
def test_an_invalid_database_url_is_refused_at_the_config_stage(bench, url):
    bench.environ["DATABASE_URL"] = url
    message, failure = failed(bench)
    assert failure == ("config", "F2ConfigError") and "SECRET" not in message["message"]
    assert bench.engine is None


def test_an_incompatible_abuse_keyring_fails_at_preflight_and_closes_the_engine_once(bench):
    with bench.admin.begin() as conn:
        conn.execute(sa.text("DELETE FROM auth_abuse_key_state"))
    _, failure = failed(bench)
    assert failure == ("preflight", "F2PreflightError:abuse_key_state_unavailable")
    assert bench.closed == ["engine"] and len(bench.journal.of("RESOURCE_CLOSED")) == 1
    refuses_f2_afterwards(bench)


def test_a_database_outside_the_contract_fails_at_preflight(bench):
    with bench.admin.begin() as conn:
        head = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        conn.execute(sa.text("UPDATE alembic_version SET version_num = '0020_position_revision'"))
    try:
        _, failure = failed(bench)
        assert failure == ("preflight", "F2PreflightError:alembic_head_mismatch") and bench.closed == ["engine"]
    finally:
        with bench.admin.begin() as conn:
            conn.execute(sa.text("UPDATE alembic_version SET version_num = :h"), {"h": head})


def test_an_unreachable_server_fails_at_preflight(migrated, journal):
    bench = f.Bench(migrated, journal)
    try:
        bench.environ["DATABASE_URL"] = synthetic.FAKE_DATABASE_URL
        bench.envelope = ep.F2Envelope(environ=bench.environ, resources_factory=bench.resources,
                                       observer=journal.observe)
        _, failure = failed(bench)
        assert failure == ("preflight", "F2PreflightError:connection_failed") and bench.closed == ["engine"]
    finally:
        bench.dispose()


def test_an_aborted_provider_construction_is_cleaned_by_f04b(bench):
    bench.envelope = ep.F2Envelope(environ=bench.environ, observer=bench.journal.observe,
                                   resources_factory=lambda config: build_f2_resources(config,
                                                                                       claude_http_client=object()))
    _, failure = failed(bench)
    assert failure == ("resources", "F2ResourceBuildError")
    report = bench.envelope.close_report
    assert report.closed == ("fmp", "eodhd", "resend", "supabase", "engine") and report.failed == ()


def tamper(monkeypatch, which, change):
    import core.auth_f2_http.isolated as auth_iso
    import core.authz_f2_http.isolated as pers_iso
    module, name = (auth_iso, "build_isolated_f2_app") if which == "auth" else (pers_iso,
                                                                                "build_isolated_personal_app")
    original = getattr(module, name)

    def builder(**kwargs):
        return change(original, kwargs)

    monkeypatch.setattr(module, name, builder)


def _without_c3(original, kwargs):
    return original(**{**kwargs, "decryptage": None})


def _extra_route(original, kwargs):
    app = original(**kwargs)
    app.add_api_route("/api/v2/me/extra", lambda: None, methods=["GET"])
    return app


def _with_docs(original, kwargs):
    app = original(**kwargs)
    app.docs_url = "/docs"
    return app


def _with_cors(original, kwargs):
    app = original(**kwargs)
    app.add_middleware(CORSMiddleware, allow_origins=["*"])
    return app


def _unsupervised(original, kwargs):
    return original(**{**kwargs, "submitter": None})  # chemin de compatibilité NON supervisé


def _startup_handler(original, kwargs):
    app = original(**kwargs)
    app.router.on_startup.append(lambda: None)
    return app


def _router_handler(original, kwargs):
    app = original(**kwargs)
    router = APIRouter()
    router.add_event_handler("shutdown", lambda: None)
    app.include_router(router)
    return app


def _explicit_lifespan(original, kwargs):
    app = original(**kwargs)

    async def lifespan(_app):
        yield

    app.router.lifespan_context = lifespan
    return app


def _legacy(original, kwargs):
    import api
    return api.app


@pytest.mark.parametrize("which,change", [
    ("personal", _without_c3), ("personal", _extra_route), ("auth", _with_docs), ("auth", _with_cors),
    ("auth", _unsupervised), ("personal", _unsupervised), ("personal", _startup_handler),
    ("auth", _router_handler), ("personal", _explicit_lifespan), ("personal", _legacy)])
def test_an_out_of_contract_composition_fails_closed_and_closes_resources_once(bench, monkeypatch, which, change):
    tamper(monkeypatch, which, change)
    _, failure = failed(bench)
    assert failure == ("composition", "F2CompositionError"), failure
    assert bench.closed == ["engine"] and len(bench.journal.of("RESOURCE_CLOSED")) == 1
    assert bench.envelope.registry.admit() is None
    refuses_f2_afterwards(bench)


def test_a_second_lifespan_scope_starts_nothing_and_answers_once(bench):
    async def main():
        first = f.Lifespan(bench.envelope)
        assert await first.startup() == {"type": "lifespan.startup.complete"}
        second = f.Lifespan(bench.envelope)
        message = await second.startup()
        await second.task
        await first.shutdown_begin()
        done = await first.shutdown_result()
        return message, done

    second, done = asyncio.run(main())
    assert second == {"type": "lifespan.startup.failed", "message": "f2_lifespan_already_owned"}
    assert done == {"type": "lifespan.shutdown.complete"} and bench.envelope.outcome == ep.GRACEFUL
    assert len(bench.journal.of("RESOURCE_CLOSED")) == 1 and bench.closed == ["engine"]


# --------------------------------------------------------------------------
# A. Lifespan aval : critère de détection (FastAPI 0.110.0 / Starlette 0.36.3)
# --------------------------------------------------------------------------

def test_downstream_lifespan_detection_accepts_only_the_implicit_default():
    from starlette.routing import _DefaultLifespan

    plain = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    assert isinstance(plain.router.lifespan_context, _DefaultLifespan)
    assert ep.downstream_lifespan_is_active(plain) is False  # implicite : accepté

    async def lifespan(_app):
        yield

    assert ep.downstream_lifespan_is_active(FastAPI(lifespan=lifespan)) is True
    assert ep.downstream_lifespan_is_active(FastAPI(on_startup=[lambda: None])) is True
    assert ep.downstream_lifespan_is_active(FastAPI(on_shutdown=[lambda: None])) is True
    included = FastAPI()
    router = APIRouter(on_startup=[lambda: None])
    included.include_router(router)
    assert ep.downstream_lifespan_is_active(included) is True  # recopié par include_router
    ignored = FastAPI()
    ignored.include_router(APIRouter(lifespan=lifespan))
    # FastAPI 0.110.0 ne fusionne pas le lifespan d'un routeur inclus : il n'est
    # jamais exécuté, donc pas actif ; le contexte de l'application reste implicite.
    assert isinstance(ignored.router.lifespan_context, _DefaultLifespan)
    assert ep.downstream_lifespan_is_active(ignored) is False


# --------------------------------------------------------------------------
# B. Routage F-02A octet pour octet et maintenance
# --------------------------------------------------------------------------

def _concrete(template):
    return "/".join("0d9a6c4e-1b2a-4c3d-8e9f-001122334455" if s.startswith("{") else s
                    for s in template.split("/"))


METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "FOO")
EXTRA_PATHS = ("/", "/unknown", "/health", "/HEALTH", "/health/", "//health", "/api/v2/me/", "/api/v2/me/../me",
               "/api/v2/%2e%2e/me", "/api/v2/me;x", "/docs", "/openapi.json", "/web-v2", "/api/auth/f2",
               "/api/v2/me/decryptage/turns/")


def matrix():
    paths = sorted({_concrete(r.template) for r in registry.RULES} | set(EXTRA_PATHS))
    cases = [(m, p, p.encode(), "") for p in paths for m in METHODS]
    cases += [("GET", "/health", b"/health", "/prefix"), ("GET", "/api/v2/me", b"/api/v2/me", "/x"),
              ("GET", "/health", b"/health?x=1", ""), ("GET", "/api/v2/me", b"/api/v2/%6De", "")]
    return cases


async def _call(app, method, path, raw_path, root_path, headers=None):
    scope = f.scope_of(method, path, headers or {"host": f.HOST}, raw_path=raw_path)
    scope["root_path"] = root_path
    reads = []
    sent = []

    async def receive():
        reads.append(1)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent, reads


def _decision(method, path, raw_path, root_path):
    scope = f.scope_of(method, path, {}, raw_path=raw_path)
    scope["root_path"] = root_path
    return classify(scope).action


async def _compare_matrix(envelope, *, forward_expect):
    reference = RoutePolicyGate(f2_auth_app=None, f2_personal_app=None)
    compared = maintenance = 0
    for method, path, raw_path, root_path in matrix():
        action = _decision(method, path, raw_path, root_path)
        got, reads = await _call(envelope, method, path, raw_path, root_path)
        if action in FORWARD:
            if forward_expect == "maintenance":
                start_message = got[0]
                assert start_message["status"] == 503 and start_message["headers"] == MAINTENANCE_HEADERS, path
                assert got[1]["body"] == (b"" if method == "HEAD" else ep.MAINTENANCE_BODY)
                assert reads == []  # refus AVANT toute lecture du corps
                maintenance += 1
            continue
        expected, _ = await _call(reference, method, path, raw_path, root_path)
        assert got == expected, (method, raw_path, root_path)  # statut, en-têtes (ordre inclus), corps
        assert not any(k.startswith(b"access-control-") for m in got if m["type"] == "http.response.start"
                       for k, _ in m["headers"])
        compared += 1
    return compared, maintenance


def test_f02a_matrix_is_byte_identical_and_f2_is_in_maintenance_while_inert(bench):
    compared, maintenance = asyncio.run(_compare_matrix(bench.envelope, forward_expect="maintenance"))
    assert compared > 300 and maintenance == 27  # les 27 couples F2 exacts
    assert bench.engine is None  # INERT : rien construit


def test_f02a_matrix_is_byte_identical_in_open_and_draining(bench):
    async def main():
        life = f.Lifespan(bench.envelope)
        assert await life.startup() == {"type": "lifespan.startup.complete"}
        checkouts = bench.engine.pool.checkedout()
        open_compared, _ = await _compare_matrix(bench.envelope, forward_expect="delegated")
        hold = asyncio.Event()
        issued = await asyncio.to_thread(bench.issue_session)
        inflight = asyncio.ensure_future(f.asgi_call(bench.envelope, "PUT", f.ME_LEVEL,
                                                     f.mutation_headers(bench, issued), f.level_body(), hold=hold))
        await asyncio.sleep(0.1)
        await life.shutdown_begin()
        await asyncio.sleep(0.2)
        assert bench.envelope.state == ep.DRAINING
        draining_compared, maintenance = await _compare_matrix(bench.envelope, forward_expect="maintenance")
        health, _ = await _call(bench.envelope, "GET", "/health", b"/health", "")
        assert health[0]["status"] == 200 and health[1]["body"] == b'{"status":"ok"}'
        assert bench.closed == []  # une admission vivante (sans thread) empêche la fermeture
        hold.set()
        sent = await inflight
        result = await life.shutdown_result()
        return open_compared, draining_compared, maintenance, f.status_of_messages(sent), result, checkouts

    open_compared, draining_compared, maintenance, inflight_status, result, checkouts = asyncio.run(main())
    assert open_compared > 300 and draining_compared == open_compared and maintenance == 27
    assert inflight_status == 200  # admise avant DRAINING : traitée, jamais convertie en refus
    assert result == {"type": "lifespan.shutdown.complete"} and bench.envelope.outcome == ep.GRACEFUL
    assert checkouts == 0 and bench.level() == "avance"
    assert f.violated(f.check_invariants(bench.journal)) == {}


def test_websocket_is_closed_1008_in_every_state(bench):
    async def ws(app):
        sent = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        await app({"type": "websocket", "path": "/api/v2/me", "raw_path": b"/api/v2/me", "root_path": "",
                   "headers": []}, receive, send)
        return sent

    async def main():
        out = [await ws(bench.envelope)]
        life = f.Lifespan(bench.envelope)
        await life.startup()
        out.append(await ws(bench.envelope))
        await life.shutdown_begin()
        await life.shutdown_result()
        out.append(await ws(bench.envelope))
        return out

    assert asyncio.run(main()) == [[{"type": "websocket.close", "code": 1008}]] * 3


def test_maintenance_touches_no_database_and_no_provider(bench):
    issued = bench.issue_session()
    generated = list(bench.supabase.generate_calls)

    async def main():
        for method, path, body in (("GET", f.ME, b""), ("PUT", f.ME_LEVEL, f.level_body()),
                                   ("POST", f.TURNS, f.turn_body()), ("POST", f.AUTH + "/sign-in/start", b"{}")):
            sent = await f.asgi_call(bench.envelope, method, path, f.mutation_headers(bench, issued), body)
            assert f.status_of_messages(sent) == 503
            start_message = sent[0]
            assert start_message["headers"] == MAINTENANCE_HEADERS
            assert not any(k == b"set-cookie" for k, _ in start_message["headers"])

    asyncio.run(main())
    assert bench.engine is None and bench.claude.methods() == [] and bench.supabase.generate_calls == generated
    assert bench.level() == "debutant"
