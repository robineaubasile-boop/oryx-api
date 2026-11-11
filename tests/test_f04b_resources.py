"""F04-B / B05 — ressources fournisseurs à propriété explicite (sans réseau).

Propriété, ordre de construction et de fermeture, fermeture unique et
idempotente, nettoyage des constructions partielles, ressources injectées
jamais fermées ; contrats conservés des cinq fournisseurs (Supabase, Resend :
httpx ; EODHD, FMP : requests, sessions isolées par appel ; Claude :
anthropic, max_retries=0) ; même trousseau F2 et même trousseau CSRF
acceptés par les deux compositions isolées avec le moteur partagé.
Aucun appel réseau : transports simulés, URL PostgreSQL non joignable.
"""
import json
import ssl
import threading

import httpx
import httpx2
import pytest
import requests
import sqlalchemy as sa

from core import decryptage_providers as providers
from core.auth_f2 import http_providers
from core.auth_f2.crypto import Redacted
from core.auth_f2.providers import ProviderUncertain
from core.f2_runtime import resources as r
from core.f2_runtime.config import load_f2_config
from tests import f04b_support as s


@pytest.fixture
def config():
    return load_f2_config(s.environ())


class RecordingTransport(httpx.MockTransport):
    def __init__(self, handler=None):
        self.requests, self.closed = [], 0
        super().__init__(handler or self._ok)

    def _ok(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"id": "x"})

    def close(self):
        self.closed += 1


class FakeResponse:
    def __init__(self, status=200, body=b"[]", error=None):
        self.status_code, self._body, self._error, self.closed = status, body, error, False

    def iter_content(self, size):
        if self._error is not None:
            raise self._error
        yield self._body

    def close(self):
        self.closed = True


class FakeSession:
    """Session Requests simulée : enregistre ses appels et sa fermeture."""

    def __init__(self, response=None, error=None, gate=None):
        self.response = response or FakeResponse()
        self.error, self.gate, self.calls, self.closed = error, gate, [], 0
        self.users = set()

    def get(self, url, **kwargs):
        self.users.add(threading.get_ident())
        self.calls.append((url, kwargs))
        if self.gate is not None:
            self.gate()
        if self.error is not None:
            raise self.error
        return self.response

    def close(self):
        self.closed += 1


class SessionFactory:
    def __init__(self, **kwargs):
        self.kwargs, self.sessions, self.lock = kwargs, [], threading.Lock()

    def __call__(self):
        session = FakeSession(**self.kwargs)
        with self.lock:
            self.sessions.append(session)
        return session


def _build(config, **kwargs):
    kwargs.setdefault("supabase_transport", RecordingTransport())
    kwargs.setdefault("resend_transport", RecordingTransport())
    kwargs.setdefault("session_factory", SessionFactory())
    return r.build_f2_resources(config, **kwargs)


# --------------------------------------------------------------------------
# Propriété, ordre, fermeture
# --------------------------------------------------------------------------

def test_resources_are_built_in_order_and_owned(config):
    resources = _build(config)
    assert resources.owner.names() == r.STAGES == ("engine", "supabase", "resend", "eodhd", "fmp", "claude")
    assert resources.owner.owned() == r.STAGES
    assert isinstance(resources.engine, sa.engine.Engine) and resources.engine.pool.checkedout() == 0
    assert type(resources.supabase) is http_providers.HttpSupabaseOtpProvider
    assert type(resources.mailer) is http_providers.HttpResendMailer
    assert type(resources.eodhd) is providers.EodhdTransport and type(resources.fmp) is providers.FmpTransport
    assert type(resources.claude) is providers.ClaudeTransport and resources.claude.max_retries == 0
    assert repr(resources) == "F2Resources(<private>)"
    for value in s.SECRET_VALUES:
        assert value not in repr(resources) + repr(resources.owner)
    report = resources.close()
    assert report.ok and report.closed == tuple(reversed(r.STAGES)) and report.borrowed == ()


def test_close_runs_each_closer_once_in_reverse_order_and_is_idempotent(config, monkeypatch):
    calls = []
    real_engine = r.create_f2_engine

    def engine_factory(url):
        engine = real_engine(url)
        dispose = engine.dispose
        engine.dispose = lambda *a, **k: (calls.append("engine"), dispose(*a, **k))
        return engine

    monkeypatch.setattr(r, "create_f2_engine", engine_factory)
    for cls, name in ((http_providers.HttpSupabaseOtpProvider, "supabase"), (http_providers.HttpResendMailer, "resend"),
                      (providers.ClaudeTransport, "claude")):
        original = cls.close
        monkeypatch.setattr(cls, "close", lambda self, _o=original, _n=name: (calls.append(_n), _o(self))[1])
    resources = _build(config)
    first = resources.close()
    second = resources.close()
    assert calls == ["claude", "resend", "supabase", "engine"]
    assert first is second and resources.closed
    assert first.closed == ("claude", "fmp", "eodhd", "resend", "supabase", "engine")


def test_close_continues_after_a_failing_closer_and_reports_it(config, monkeypatch):
    def boom(self):
        raise OSError("synthetic-close-failure synthetic-password")

    monkeypatch.setattr(http_providers.HttpResendMailer, "close", boom)
    resources = _build(config)
    report = resources.close()
    assert report.failed == (("resend", "OSError"),)
    assert report.closed == ("claude", "fmp", "eodhd", "supabase", "engine")
    assert "synthetic-password" not in repr(report)
    assert resources.close() is report  # aucune seconde tentative


def test_owned_clients_are_really_closed_when_nothing_is_injected(config):
    resources = r.build_f2_resources(config)
    supabase_client, resend_client = resources.supabase._client, resources.mailer._client  # observation seulement
    sdk_client = resources.claude._client
    assert not supabase_client.is_closed and not resend_client.is_closed and not sdk_client.is_closed()
    resources.close()
    assert supabase_client.is_closed and resend_client.is_closed and sdk_client.is_closed()
    resources.supabase.close()
    resources.claude.close()  # idempotents


def test_injected_resources_are_never_closed(config, pg_free_engine):
    supabase_transport, resend_transport = RecordingTransport(), RecordingTransport()
    claude_client = httpx2.Client(transport=httpx2.MockTransport(lambda request: httpx2.Response(500)))
    disposed = []
    pg_free_engine.dispose = lambda *a, **k: disposed.append(1)
    resources = r.build_f2_resources(config, engine=pg_free_engine, supabase_transport=supabase_transport,
                                     resend_transport=resend_transport, claude_http_client=claude_client,
                                     session_factory=SessionFactory())
    assert resources.engine is pg_free_engine
    assert resources.owner.owned() == ("supabase", "resend", "eodhd", "fmp", "claude")
    report = resources.close()
    assert report.borrowed == ("engine",) and "engine" not in report.closed
    assert disposed == [] and supabase_transport.closed == 0 and resend_transport.closed == 0
    assert not claude_client.is_closed
    claude_client.close()


@pytest.fixture
def pg_free_engine():
    engine = sa.create_engine("postgresql+psycopg2://u:p@127.0.0.1:1/oryx_never_test")
    yield engine


@pytest.mark.parametrize("stage", ["engine", "supabase", "resend", "eodhd", "fmp", "claude"])
def test_a_partial_build_closes_what_was_built_in_reverse_order(config, monkeypatch, stage):
    closed = []
    for cls, name in ((http_providers.HttpSupabaseOtpProvider, "supabase"), (http_providers.HttpResendMailer, "resend"),
                      (providers.ClaudeTransport, "claude")):
        original = cls.close
        monkeypatch.setattr(cls, "close", lambda self, _o=original, _n=name: (closed.append(_n), _o(self))[1])
    real_engine = r.create_f2_engine

    def engine_factory(url):
        engine = real_engine(url)
        dispose = engine.dispose
        engine.dispose = lambda *a, **k: (closed.append("engine"), dispose(*a, **k))
        return engine

    monkeypatch.setattr(r, "create_f2_engine", engine_factory)

    def explode(*args, **kwargs):
        raise RuntimeError("synthetic construction failure sk-ant-synthetic-key")

    target = {"engine": "create_f2_engine", "supabase": "HttpSupabaseOtpProvider", "resend": "HttpResendMailer",
              "eodhd": "EodhdTransport", "fmp": "FmpTransport", "claude": "ClaudeTransport"}[stage]
    monkeypatch.setattr(r, target, explode)
    with pytest.raises(r.F2ResourceBuildError) as info:
        _build(config)
    error = info.value
    assert error.stage == stage and error.cause_type == "RuntimeError"
    assert error.__cause__ is None and "sk-ant-synthetic-key" not in str(error)
    built = r.STAGES[:r.STAGES.index(stage)]
    assert error.cleanup.closed == tuple(reversed(built)) and error.cleanup.ok
    with_closer = [name for name in reversed(built) if name in ("engine", "supabase", "resend", "claude")]
    assert closed == with_closer  # chaque ressource construite fermée une fois, en ordre inverse


def test_a_partial_build_survives_a_failing_cleanup(config, monkeypatch):
    monkeypatch.setattr(http_providers.HttpSupabaseOtpProvider, "close",
                        lambda self: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(r, "ClaudeTransport", lambda *a, **k: (_ for _ in ()).throw(ValueError("y")))
    with pytest.raises(r.F2ResourceBuildError) as info:
        _build(config)
    assert info.value.stage == "claude"
    assert info.value.cleanup.failed == (("supabase", "OSError"),)
    assert info.value.cleanup.closed == ("fmp", "eodhd", "resend", "engine")


def test_the_registry_refuses_late_or_duplicate_registrations():
    owner = r.ResourceOwner()
    owner.own("a", object(), None)
    with pytest.raises(r.F2ResourceError):
        owner.own("a", object(), None)
    with pytest.raises(TypeError):
        owner.own("b", object(), "not callable")
    owner.close()
    with pytest.raises(r.F2ResourceError):
        owner.borrow("c", object())


def test_close_is_single_even_under_concurrent_calls():
    count, owner = [], r.ResourceOwner()
    owner.own("x", object(), lambda: count.append(1))
    barrier = threading.Barrier(8)
    reports = []

    def close():
        barrier.wait()
        reports.append(owner.close())

    threads = [threading.Thread(target=close) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert count == [1] and len({id(rep) for rep in reports}) == 1


def test_build_refuses_wrong_inputs(config):
    with pytest.raises(TypeError):
        r.build_f2_resources(object())
    with pytest.raises(TypeError):
        r.build_f2_resources(config, engine=sa.create_engine("sqlite://"))
    with pytest.raises(TypeError):
        r.build_f2_resources(config, session_factory="nope")


def test_the_module_never_uses_private_adapter_attributes_to_close():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(r))
    private = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr.startswith("_")
               and not n.attr.startswith("__")}
    assert private <= {"_lock", "_entries", "_report", "_register", "_owner"}, private


# --------------------------------------------------------------------------
# Supabase et Resend (httpx) : contrats conservés
# --------------------------------------------------------------------------

def test_supabase_and_resend_keep_their_http_contract(config):
    supabase_transport, resend_transport = RecordingTransport(), RecordingTransport()
    resources = _build(config, supabase_transport=supabase_transport, resend_transport=resend_transport)
    resources.supabase.generate_email_otp("a@oryx-f04b.example")
    resources.mailer.send_otp(to="a@oryx-f04b.example", code="12345678", idempotency_key="idem-1")
    generate, = supabase_transport.requests
    assert (generate.method, generate.url.path) == ("POST", "/auth/v1/admin/generate_link")
    assert str(generate.url).startswith(s.SUPABASE_URL + "/")
    assert generate.headers["apikey"] == "synthetic-supabase-service-key"
    assert json.loads(generate.content) == {"type": "magiclink", "email": "a@oryx-f04b.example"}
    resources.supabase.verify_email_otp("a@oryx-f04b.example", "12345678")
    verify = supabase_transport.requests[-1]
    assert verify.url.path == "/auth/v1/verify" and verify.headers["apikey"] == "synthetic-supabase-verify-key"
    send, = resend_transport.requests
    assert str(send.url) == "https://api.resend.com/emails" and send.headers["Idempotency-Key"] == "idem-1"
    assert send.headers["Authorization"] == "Bearer re_synthetic_resend_key"
    assert json.loads(send.content)["from"] == "Oryx <no-reply@oryx-f04b.example>"
    assert resources.supabase.issuer == s.SUPABASE_URL + "/auth/v1"
    resources.close()


def test_owned_httpx_clients_keep_tls_no_redirect_no_env_and_timeouts(config):
    resources = r.build_f2_resources(config)
    for client in (resources.supabase._client, resources.mailer._client):  # observation seulement
        assert client.follow_redirects is False and client.trust_env is False
        assert client.timeout == httpx.Timeout(http_providers.TIMEOUT_SECONDS)
        context = client._transport._pool._ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    resources.close()


def test_transport_errors_are_still_uncertain_and_closing_is_explicit(config):
    def broken(request):
        raise httpx.ConnectError("refused")

    transport = RecordingTransport(broken)
    provider = http_providers.HttpSupabaseOtpProvider(base_url=s.SUPABASE_URL, service_key=Redacted("k"),
                                                      api_key=Redacted("v"), transport=transport)
    assert provider.generate_email_otp("a@b.example") == ProviderUncertain("transport")
    provider.close()
    assert transport.closed == 0  # transport injecté : jamais fermé par l'adaptateur
    owned = http_providers.HttpResendMailer(api_key=Redacted("k"), sender="a@b.example")
    owned.close()
    owned.close()
    assert owned._client.is_closed


# --------------------------------------------------------------------------
# EODHD et FMP (requests) : sessions
# --------------------------------------------------------------------------

def test_session_and_factory_together_are_refused():
    for cls in (providers.EodhdTransport, providers.FmpTransport):
        with pytest.raises(ValueError):
            cls("k", session=FakeSession(), session_factory=SessionFactory())
        with pytest.raises(ValueError):
            cls("k", session_factory="not callable")


def test_historical_injected_session_is_unchanged():
    session = FakeSession(FakeResponse(body=b'[{"Code": "MC"}]'))
    transport = providers.EodhdTransport("k", session=session)
    assert transport.search("MC", timeout_s=5.0) == [{"Code": "MC"}]
    transport.realtime("MC", timeout_s=5.0)
    assert len(session.calls) == 2 and session.closed == 0  # session injectée : réutilisée, jamais fermée
    default = providers.FmpTransport("k")
    assert default._session is not None and default._factory is None  # session interne historique


@pytest.mark.parametrize("method, args", [("search", ("MC.PA",)), ("fundamentals", ("MC.PA",)),
                                          ("realtime", ("MC.PA",))])
def test_factory_mode_sends_exactly_the_historical_eodhd_request(method, args):
    historical, factory = FakeSession(), SessionFactory()
    getattr(providers.EodhdTransport("k", session=historical), method)(*args, timeout_s=7.0)
    getattr(providers.EodhdTransport("k", session_factory=factory), method)(*args, timeout_s=7.0)
    assert factory.sessions[0].calls == historical.calls
    assert factory.sessions[0].closed == 1


@pytest.mark.parametrize("method", ["profile", "ratios"])
def test_factory_mode_sends_exactly_the_historical_fmp_request(method):
    historical, factory = FakeSession(), SessionFactory()
    getattr(providers.FmpTransport("k", session=historical), method)("MC.PA", timeout_s=3.0)
    getattr(providers.FmpTransport("k", session_factory=factory), method)("MC.PA", timeout_s=3.0)
    assert factory.sessions[0].calls == historical.calls
    assert factory.sessions[0].closed == 1


@pytest.mark.parametrize("kwargs, expected", [
    ({"response": FakeResponse(status=404)}, None),
    ({"response": FakeResponse(status=500)}, ConnectionError),
    ({"response": FakeResponse(status=302)}, ConnectionError),
    ({"response": FakeResponse(body=b"[NaN]")}, ValueError),
    ({"error": requests.exceptions.ReadTimeout()}, TimeoutError),
    ({"error": requests.exceptions.ConnectionError()}, ConnectionError),
    ({"response": FakeResponse(error=requests.exceptions.ChunkedEncodingError())}, ConnectionError),
])
def test_factory_mode_keeps_the_error_classification_and_always_closes(kwargs, expected):
    historical = providers.FmpTransport("k", session=FakeSession(**kwargs))
    factory = SessionFactory(**kwargs)
    isolated = providers.FmpTransport("k", session_factory=factory)
    if expected is None:
        assert historical.profile("MC", timeout_s=5.0) is None and isolated.profile("MC", timeout_s=5.0) is None
    else:
        with pytest.raises(expected):
            historical.profile("MC", timeout_s=5.0)
        with pytest.raises(expected):
            isolated.profile("MC", timeout_s=5.0)
    assert [session.closed for session in factory.sessions] == [1]


def test_no_request_is_sent_without_time_budget_and_the_session_is_closed():
    factory = SessionFactory()
    with pytest.raises(TimeoutError):
        providers.EodhdTransport("k", session_factory=factory).search("MC", timeout_s=0)
    assert all(session.calls == [] for session in factory.sessions)
    assert all(session.closed == 1 for session in factory.sessions)


def test_each_concurrent_call_gets_its_own_session():
    barrier = threading.Barrier(4, timeout=10)
    factory = SessionFactory(gate=barrier.wait)
    eodhd = providers.EodhdTransport("k", session_factory=factory)
    fmp = providers.FmpTransport("k", session_factory=factory)
    calls = [lambda: eodhd.search("A", timeout_s=20.0), lambda: eodhd.fundamentals("B", timeout_s=20.0),
             lambda: fmp.profile("C", timeout_s=20.0), lambda: fmp.ratios("D", timeout_s=20.0)]
    errors = []

    def run(call):
        try:
            call()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert errors == []
    assert len(factory.sessions) == 4 and len({id(x) for x in factory.sessions}) == 4
    for session in factory.sessions:  # quatre appels simultanés (barrière) : une session chacun
        assert len(session.calls) == 1 and len(session.users) == 1 and session.closed == 1


def test_f04b_sessions_ignore_the_environment_and_never_retry(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    session = providers.isolated_session()
    assert type(session) is requests.Session and session.trust_env is False and session.verify is True
    for prefix in ("https://", "http://"):
        retries = session.get_adapter(prefix + "eodhd.com").max_retries
        assert retries.total == 0 and retries.read in (0, False) and retries.redirect in (0, None, False)
    assert providers.isolated_session() is not session
    session.close()


def test_built_market_transports_use_one_session_per_call(config):
    factory = SessionFactory()
    resources = _build(config, session_factory=factory)
    resources.eodhd.search("MC", timeout_s=5.0)
    resources.eodhd.search("MC", timeout_s=5.0)
    resources.fmp.profile("MC", timeout_s=5.0)
    assert len(factory.sessions) == 3 and all(x.closed == 1 for x in factory.sessions)
    url, kwargs = factory.sessions[0].calls[0]
    assert url == "https://eodhd.com/api/search/MC" and kwargs["params"]["api_token"] == "synthetic-eodhd-key"
    assert factory.sessions[2].calls[0][1]["params"]["apikey"] == "synthetic-fmp-key"
    resources.close()


def test_default_resources_use_the_isolated_session_factory(config, monkeypatch):
    created = []

    def spy():
        session = providers.isolated_session()
        created.append(session)
        session.get = lambda url, **kw: FakeResponse(body=b"[]")
        return session

    monkeypatch.setattr(r, "isolated_session", spy)
    resources = r.build_f2_resources(config)
    resources.fmp.ratios("MC", timeout_s=5.0)
    assert len(created) == 1 and created[0].trust_env is False
    resources.close()


# --------------------------------------------------------------------------
# Claude (anthropic)
# --------------------------------------------------------------------------

def test_claude_transport_keeps_one_request_and_no_retry():
    seen = []

    def overloaded(request):
        seen.append(request)
        return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}})

    client = httpx2.Client(transport=httpx2.MockTransport(overloaded))
    transport = providers.ClaudeTransport("sk-test", http_client=client)
    with pytest.raises(ConnectionError):
        transport.create_message(model="m", system="s", user="u", max_tokens=10, timeout_s=30.0)
    assert len(seen) == 1 and transport.max_retries == 0
    transport.close()
    assert not client.is_closed  # client injecté : comportement historique, jamais fermé ici
    client.close()


def test_owned_claude_client_is_closed_once():
    transport = providers.ClaudeTransport("sk-test")
    transport.close()
    transport.close()
    assert transport._client.is_closed()  # observation seulement


# --------------------------------------------------------------------------
# Un trousseau F2, un trousseau CSRF, un moteur : deux compositions
# --------------------------------------------------------------------------

def test_both_isolated_compositions_accept_the_same_keyrings_and_engine(config):
    from core.auth_f2_http.client_ip import PeerAddressResolver
    from core.auth_f2_http.isolated import build_isolated_f2_app
    from core.authz_f2_http.decryptage import DecryptageHttpConfig
    from core.authz_f2_http.isolated import build_isolated_personal_app

    resources = _build(config)
    auth_app = build_isolated_f2_app(engine=resources.engine, keyring=config.keyring, supabase=resources.supabase,
                                     mailer=resources.mailer, csrf_keyring=config.csrf_keyring,
                                     ip_resolver=PeerAddressResolver(), allowed_origins=config.allowed_origins)
    personal_app = build_isolated_personal_app(
        engine=resources.engine, keyring=config.keyring, allowed_origins=config.allowed_origins,
        csrf_keyring=config.csrf_keyring, portfolio_keyring=config.portfolio_keyring,
        decryptage=DecryptageHttpConfig(decryptage_keyring=config.decryptage_keyring, eodhd=resources.eodhd,
                                        fmp=resources.fmp, claude=resources.claude, model=config.decryptage_model))

    def pairs(app):
        return {(m, route.path) for route in app.routes for m in getattr(route, "methods", ())}

    assert len(pairs(auth_app)) == 14 and len(pairs(personal_app)) == 13
    assert resources.engine.pool.checkedout() == 0  # aucune connexion ouverte par les constructions
    resources.close()
