"""GET /api/v2/me (R1 PR 5) : transport, provenance, statuts et parcours
de bout en bout sur la composition de test F2 + PR 5.

Application personnelle ISOLÉE (core.authz_f2_http.isolated) autour du
vrai AuthorizedTransactionRunner ; sessions émises par le vrai moteur F2,
par appel direct ou par les routes HTTP F2 de la composition de test
(tests/authz_f2_support.py). Aucun réseau, aucune clé réelle.
"""
import asyncio
import json
import time
import uuid

import httpx
import pytest
import sqlalchemy as sa

from core.auth_f2.crypto import b64url_encode
from core.auth_f2_http import csrf
from core.auth_f2_http.provenance import validate_allowed_origins
from core.authz_f2 import runner as runner_module
from core.authz_f2_http import routes as routes_module
from core.authz_f2_http.isolated import PersonalHttpConfigError, build_isolated_personal_app, make_personal_app
from core.authz_f2_http.routes import MAX_BODY_BYTES, PersonalDependencies
from tests.authz_f2_support import (
    ALICE,
    BOB,
    ME,
    ME_LEVEL,
    USER_A,
    USER_B,
    AuthzEnv,
    PersonalBrowser,
    active_cookie,
    compose_test_app,
    csrf_pair,
    level_body,
    level_headers,
    me_headers,
    me_request,
    personal_app,
    put_level,
    raw_level_headers,
    raw_request,
)
from tests.f2_http_support import ORIGIN, csrf_keyring
from tests.f2_support import create_profile_with_identity, prepare_schema
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

NO_STORE = {"cache-control": "no-store", "pragma": "no-cache", "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer"}


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = AuthzEnv(pg_url)
    yield environment
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


def assert_private(response):
    for name, value in NO_STORE.items():
        assert response.headers.get_list(name) == [value], name
    assert response.headers.get_list("set-cookie") == []
    assert "access-control-allow-origin" not in response.headers


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json() == {"error": code}
    assert_private(response)


def get_me(env, issued, *, app=None, **kwargs):
    headers = kwargs.pop("headers", None) or me_headers(issued.generation_id, active_cookie(issued))
    return me_request(app or personal_app(env), headers=headers, **kwargs)


# --------------------------------------------------------------------------
# Succès
# --------------------------------------------------------------------------

@pytest.mark.parametrize("provenance", [{"fetch_site": "same-origin"},
                                        {"origin": ORIGIN, "fetch_site": "same-origin"},
                                        {"origin": ORIGIN, "fetch_site": None}])
def test_me_returns_the_server_profile(env, alice, provenance):
    headers = me_headers(alice.generation_id, active_cookie(alice), **provenance)
    response = get_me(env, alice, headers=headers)
    assert response.status_code == 200, response.text
    # R1 PR 6 : level_revision ajouté, en chaîne décimale canonique.
    assert response.json() == {"user_id": USER_A, "level": "debutant", "level_revision": "0"}
    assert response.content == b'{"level":"debutant","level_revision":"0","user_id":"user-a"}'
    assert response.headers["content-type"] == "application/json"
    assert_private(response)
    for secret in (alice.active_secret.reveal(), str(alice.session_id), str(alice.generation_id)):
        assert secret not in response.text


def test_level_comes_from_postgresql_and_no_client_identifier_is_honoured(env, alice):
    env.confirmed(BOB, USER_B)
    with env.observer.begin() as conn:
        conn.execute(sa.text("UPDATE users SET level = 'avance' WHERE id = :u"), {"u": USER_A})
    response = get_me(env, alice, headers=me_headers(alice.generation_id, active_cookie(alice),
                                                     extra={"x-oryx-user-id": USER_B, "x-user-id": USER_B}))
    assert response.json() == {"user_id": USER_A, "level": "avance", "level_revision": "0"}


# --------------------------------------------------------------------------
# Provenance (403)
# --------------------------------------------------------------------------

PROVENANCE_REFUSALS = {
    "no_origin_no_fetch_site": {"fetch_site": None},
    "fetch_site_same_site": {"fetch_site": "same-site"},
    "fetch_site_cross_site": {"fetch_site": "cross-site"},
    "fetch_site_none": {"fetch_site": "none"},
    "foreign_origin": {"origin": "https://evil.test"},
    "null_origin": {"origin": "null"},
    "http_origin": {"origin": "http://oryx.test"},
    "origin_with_path": {"origin": ORIGIN + "/"},
    "foreign_origin_same_site_header": {"origin": "https://evil.test", "fetch_site": "same-origin"},
    "foreign_referer": {"extra": {"referer": "https://evil.test/page"}},
    "http_referer": {"extra": {"referer": "http://oryx.test/page"}},
    "referer_with_credentials": {"extra": {"referer": "https://user:pw@oryx.test/"}},
    "unreadable_referer": {"extra": {"referer": "https://[oryx.test/"}},
    "foreign_host": {"extra": {"host": "evil.test"}},
    "uppercase_host": {"extra": {"host": "ORYX.TEST"}},
    "explicit_443_host": {"extra": {"host": "oryx.test:443"}},
    "host_with_other_port": {"extra": {"host": "oryx.test:8443"}},
}


@pytest.mark.parametrize("case", sorted(PROVENANCE_REFUSALS))
def test_provenance_refusals(env, alice, case):
    headers = me_headers(alice.generation_id, active_cookie(alice), **PROVENANCE_REFUSALS[case])
    assert_error(get_me(env, alice, headers=headers), 403, "origin_refused")
    assert env.hook_calls == []


@pytest.mark.parametrize("name,value", [("origin", ORIGIN), ("sec-fetch-site", "same-origin"),
                                        ("referer", ORIGIN + "/"), ("host", "oryx.test")])
def test_duplicated_provenance_headers_are_refused(env, alice, name, value):
    headers = list(me_headers(alice.generation_id, active_cookie(alice), origin=ORIGIN).items())
    headers += [(name, value), (name, value)]
    assert_error(get_me(env, alice, headers=headers), 403, "origin_refused")


def test_valid_referer_is_accepted(env, alice):
    headers = me_headers(alice.generation_id, active_cookie(alice), extra={"referer": ORIGIN + "/portefeuille?x=1"})
    assert get_me(env, alice, headers=headers).status_code == 200


def test_no_permissive_cors(env, alice):
    response = me_request(personal_app(env), method="OPTIONS", headers={
        "origin": "https://evil.test", "access-control-request-method": "GET",
        "access-control-request-headers": "x-oryx-generation-id"})
    assert response.status_code == 405
    assert_private(response)


# --------------------------------------------------------------------------
# Transport (400 / 413) et sélecteur
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cookie", [
    "__Host-oryx-active-{g}={s}; __Host-oryx-active-{g}={s}",  # nom dupliqué
    "__Host-oryx-active-{g}=pas-un-secret",
    "__Host-oryx-active-{G}={s}",  # UUID non canonique
    "__Host-oryx-inconnu={s}",
])
def test_malformed_cookie_header_is_invalid_request(env, alice, cookie):
    value = cookie.format(g=alice.generation_id, G=str(alice.generation_id).upper(), s=alice.active_secret.reveal())
    assert_error(get_me(env, alice, headers=me_headers(alice.generation_id, value)), 400, "invalid_request")


def test_two_cookie_headers_are_invalid_request(env, alice):
    headers = list(me_headers(alice.generation_id).items()) + [("cookie", active_cookie(alice)), ("cookie", "a=b")]
    assert_error(get_me(env, alice, headers=headers), 400, "invalid_request")


@pytest.mark.parametrize("path", [ME + "?user_id=user-b", ME + "?x", ME + "?generation_id=x"])
def test_query_is_invalid_request(env, alice, path):
    assert_error(get_me(env, alice, path=path), 400, "invalid_request")
    assert env.hook_calls == []


@pytest.mark.parametrize("content,status,code", [
    (b"{}", 400, "invalid_request"),
    (json.dumps({"user_id": USER_B}).encode(), 400, "invalid_request"),
    (b"x" * MAX_BODY_BYTES, 400, "invalid_request"),
    (b"x" * (MAX_BODY_BYTES + 1), 413, "request_too_large"),
])
def test_body_is_refused(env, alice, content, status, code):
    assert_error(get_me(env, alice, content=content), status, code)
    assert env.hook_calls == []


@pytest.mark.parametrize("size,status,code", [(10, 400, "invalid_request"),
                                              (MAX_BODY_BYTES + 10, 413, "request_too_large")])
def test_chunked_body_is_refused(env, alice, size, status, code):
    async def chunks():
        yield b"y" * size

    assert_error(get_me(env, alice, content=chunks()), status, code)


@pytest.mark.parametrize("value", ["12", "abc", str(MAX_BODY_BYTES + 1)])
def test_announced_content_length_is_checked(env, alice, value):
    headers = me_headers(alice.generation_id, active_cookie(alice), extra={"content-length": value})
    response = get_me(env, alice, headers=headers)
    assert response.status_code in (400, 413)
    assert_error(response, 413 if value == str(MAX_BODY_BYTES + 1) else 400,
                 "request_too_large" if value == str(MAX_BODY_BYTES + 1) else "invalid_request")


@pytest.mark.parametrize("selector", [None, "", "abc", "{G}", " {g}", "{g} ", "{{{g}}}", "{g}x"])
def test_malformed_selector_is_invalid_request(env, alice, selector):
    headers = me_headers(None, active_cookie(alice))
    if selector is not None:
        headers["x-oryx-generation-id"] = selector.format(g=alice.generation_id,
                                                          G=str(alice.generation_id).upper())
    assert_error(get_me(env, alice, headers=headers), 400, "invalid_request")
    assert env.hook_calls == []


def test_duplicated_selector_is_invalid_request(env, alice):
    headers = list(me_headers(None, active_cookie(alice)).items())
    headers += [("x-oryx-generation-id", str(alice.generation_id))] * 2
    assert_error(get_me(env, alice, headers=headers), 400, "invalid_request")


def test_designated_cookie_absent_is_session_invalid_without_choosing_another(env, alice):
    other = env.confirmed(BOB, USER_B)
    # Le navigateur détient un AUTRE cookie actif valide : jamais choisi.
    headers = me_headers(alice.generation_id, active_cookie(other))
    assert_error(get_me(env, alice, headers=headers), 401, "session_invalid")
    assert env.hook_calls == []
    assert_error(get_me(env, alice, headers=me_headers(alice.generation_id)), 401, "session_invalid")


# --------------------------------------------------------------------------
# Priorités des contrôles
# --------------------------------------------------------------------------

@pytest.fixture
def trace(monkeypatch):
    """Ordre RÉELLEMENT exécuté des contrôles de GET /me (provenance,
    cookies, sélecteur, corps) et transactions ouvertes par le runner : les
    cas qui partagent un même statut et un même code sont départagés par
    cette trace, pas par la seule réponse HTTP."""
    calls = []

    def record(name, fn):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)
        return wrapper

    def record_async(name, fn):
        async def wrapper(*args, **kwargs):
            calls.append(name)
            return await fn(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(routes_module, "personal_get_origin", record("provenance", routes_module.personal_get_origin))
    monkeypatch.setattr(routes_module, "parse_cookie_headers", record("cookies", routes_module.parse_cookie_headers))
    monkeypatch.setattr(routes_module, "_generation_id", record("selector", routes_module._generation_id))
    monkeypatch.setattr(routes_module, "_refuse_body", record_async("body", routes_module._refuse_body))
    monkeypatch.setattr(runner_module, "transaction", record("transaction", runner_module.transaction))
    return calls


BAD_COOKIE = "__Host-oryx-active-x=y"
BIG = b"x" * 5000


def test_check_priorities(env, alice, trace):
    """Ordre contractuel (C.5.2 §7.4) : provenance -> cookies -> sélecteur
    -> query -> corps -> cookie désigné -> runner. Le premier contrôle en
    échec détermine la réponse ; les contrôles suivants ne s'exécutent pas."""
    good = active_cookie(alice)
    gen = alice.generation_id
    cases = [
        # (nom, en-têtes, chemin, corps, réponse attendue, contrôles exécutés)
        ("provenance+selecteur+corps", me_headers(None, good, fetch_site="cross-site"), ME, BIG,
         (403, "origin_refused"), ["provenance"]),
        ("cookie+selecteur", me_headers(None, BAD_COOKIE), ME, None,
         (400, "invalid_request"), ["provenance", "cookies"]),
        ("selecteur_absent+corps_trop_grand", me_headers(None, good), ME, BIG,
         (400, "invalid_request"), ["provenance", "cookies", "selector"]),
        ("selecteur_invalide+query", me_headers("abc", good), ME + "?user_id=user-b", None,
         (400, "invalid_request"), ["provenance", "cookies", "selector"]),
        ("selecteur_valide+query", me_headers(gen, good), ME + "?q", BIG,
         (400, "invalid_request"), ["provenance", "cookies", "selector"]),
        ("selecteur_valide+corps_trop_grand", me_headers(gen, good), ME, BIG,
         (413, "request_too_large"), ["provenance", "cookies", "selector", "body"]),
        ("selecteur_valide+corps_court", me_headers(gen, good), ME, b"{}",
         (400, "invalid_request"), ["provenance", "cookies", "selector", "body"]),
        ("selecteur_valide+cookie_absent", me_headers(uuid.uuid4(), good), ME, None,
         (401, "session_invalid"), ["provenance", "cookies", "selector", "body"]),
    ]
    app = personal_app(env)
    for name, headers, path, content, (status, code), executed in cases:
        trace.clear()
        assert_error(me_request(app, headers=headers, path=path, content=content), status, code)
        assert trace == executed, name  # aucune transaction PostgreSQL pour un refus HTTP
    assert env.hook_calls == []

    trace.clear()
    response = me_request(app, headers=me_headers(gen, good))
    assert response.status_code == 200
    assert response.json() == {"user_id": USER_A, "level": "debutant", "level_revision": "0"}
    assert_private(response)
    assert trace == ["provenance", "cookies", "selector", "body", "transaction", "transaction"]  # phases A et B


def test_missing_selector_with_oversized_body_is_400_not_413(env, alice, trace):
    """Régression P1 : provenance valide, cookies valides, sélecteur absent,
    corps de 5 000 octets -> 400 invalid_request (jamais 413), sans lecture
    du corps ni transaction PostgreSQL."""
    response = me_request(personal_app(env), headers=me_headers(None, active_cookie(alice)), content=BIG)
    assert response.status_code == 400
    assert response.content == b'{"error":"invalid_request"}'
    assert_private(response)
    assert trace == ["provenance", "cookies", "selector"]
    assert "transaction" not in trace and env.hook_calls == []


# --------------------------------------------------------------------------
# 401 / 503
# --------------------------------------------------------------------------

def test_inadmissible_credentials_share_one_401(env):
    responses = []
    issued = env.confirmed()
    wrong = f"__Host-oryx-active-{issued.generation_id}={b64url_encode(bytes(32))}"
    responses.append(get_me(env, issued, headers=me_headers(issued.generation_id, wrong)))
    pending = env.sign_in(BOB, USER_B)
    responses.append(get_me(env, pending))
    assert env.generation(pending.generation_id)["delivery_status"] == "pending"
    revoked = env.confirmed("alice2@example.com", USER_A)
    env.service.revoke_session(revoked.session_id, "admin")
    responses.append(get_me(env, revoked))
    expired = env.confirmed("alice3@example.com", USER_A)
    env.expire_at(expired.session_id, "clock_timestamp()")
    responses.append(get_me(env, expired))
    unknown = f"__Host-oryx-active-{uuid.uuid4()}={b64url_encode(bytes(32))}"
    responses.append(me_request(personal_app(env), headers=me_headers(unknown.split("=")[0][19:], unknown)))
    for response in responses:
        assert_error(response, 401, "session_invalid")
    assert len({(r.content, tuple(sorted(r.headers.items()))) for r in responses}) == 1


def test_unconfigured_authorizer_is_unavailable(env, alice):
    app = make_personal_app(PersonalDependencies(None, validate_allowed_origins([ORIGIN])))
    assert_error(get_me(env, alice, app=app), 503, "temporarily_unavailable")


def test_database_outage_is_unavailable(env, alice):
    down = sa.create_engine("postgresql+psycopg2://postgres@127.0.0.1:1/oryx_down_test", poolclass=sa.pool.NullPool)
    app = build_isolated_personal_app(engine=down, keyring=env.keyring, allowed_origins=[ORIGIN],
                                      csrf_keyring=csrf_keyring())
    assert_error(get_me(env, alice, app=app), 503, "temporarily_unavailable")


def test_incoherent_profile_is_unavailable_and_not_created(env, alice):
    with env.observer.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": USER_A})
    assert_error(get_me(env, alice), 503, "temporarily_unavailable")
    assert env.count("users") == 0


def test_invalid_composition_is_refused(env):
    for origins in (["*"], ["http://oryx.test"], [], "https://oryx.test"):
        with pytest.raises(PersonalHttpConfigError):
            build_isolated_personal_app(engine=env.engine, keyring=env.keyring, allowed_origins=origins,
                                        csrf_keyring=csrf_keyring())
    with pytest.raises(PersonalHttpConfigError):
        build_isolated_personal_app(engine=env.engine, keyring={"active": {}}, allowed_origins=[ORIGIN],
                                    csrf_keyring=csrf_keyring())


@pytest.mark.parametrize("method,path,status", [
    ("POST", ME, 405), ("PUT", ME, 405), ("DELETE", ME, 405), ("PATCH", ME, 405), ("HEAD", ME, 405),
    ("GET", ME + "/x", 404), ("GET", "/api/v2/user/user-a", 404), ("GET", "/api/user/user-a", 404),
    ("GET", "/api/auth/f2/csrf/bootstrap", 404), ("GET", "/api/v2/me/level", 405),
])
def test_only_get_me_and_put_level_exist_in_the_personal_application(env, alice, method, path, status):
    response = me_request(personal_app(env), method=method, path=path,
                          headers=me_headers(alice.generation_id, active_cookie(alice)))
    assert response.status_code == status
    assert_private(response)
    assert env.hook_calls == []
    app = personal_app(env)
    # R1 PR 7 : plus les cinq routes /api/v2/me/portfolio
    # (tests/test_authz_f2_portfolio_http.py) ; R1 PR 8 : plus les trois
    # lectures (tests/test_authz_f2_consultation_http.py), aucune autre.
    portfolio = "/api/v2/me/portfolio"
    assert sorted((sorted(r.methods), r.path) for r in app.routes if hasattr(r, "methods")) == sorted([
        (["GET"], ME), (["PUT"], ME_LEVEL), (["GET"], portfolio), (["POST"], portfolio),
        (["GET"], portfolio + "/{position_id}"), (["PUT"], portfolio + "/{position_id}"),
        (["DELETE"], portfolio + "/{position_id}"), (["GET"], "/api/v2/me/analysis-overview"),
        (["GET"], "/api/v2/me/theses"), (["GET"], "/api/v2/me/theses/{thesis_id}")])


def test_get_me_creates_nothing_and_confirms_nothing(env, alice):
    tables = ("users", "oryx_sessions", "oryx_session_generations", "portfolio_positions", "auth_operations")
    before = {t: env.count(t) for t in tables}
    generation = dict(env.generation(alice.generation_id))
    for _ in range(3):
        assert get_me(env, alice).status_code == 200
    assert {t: env.count(t) for t in tables} == before
    assert dict(env.generation(alice.generation_id)) == generation


# --------------------------------------------------------------------------
# Parcours de bout en bout (composition de test F2 + PR 5)
# --------------------------------------------------------------------------

@pytest.fixture
def composed(env):
    return compose_test_app(env)


def http_sign_in(env, browser, email=ALICE, user_id=USER_A):
    create_profile_with_identity(env.engine, env.supabase, email, user_id)
    start = browser.start(email)
    assert start.status_code == 202, start.text
    verify = browser.verify(start.json(), env.mailer.last_code(email))
    assert verify.status_code == 200, verify.text
    return verify.json()["generation_id"]


def browser_for(app):
    browser = PersonalBrowser(app)
    browser.bootstrap()
    return browser


def test_pending_then_resolve_then_me(env, composed):
    browser = browser_for(composed)
    generation_id = http_sign_in(env, browser)
    pending = browser.me(generation_id)
    assert_error(pending, 401, "session_invalid")
    assert env.generation(generation_id)["delivery_status"] == "pending"
    assert browser.actives()  # /me n'efface aucun cookie
    resolved = browser.resolve(generation_id)
    assert resolved.status_code == 200, resolved.text
    confirmed_at = env.generation(generation_id)["delivery_confirmed_at"]
    for _ in range(2):
        response = browser.me(generation_id)
        assert response.status_code == 200
        assert response.json() == {"user_id": USER_A, "level": "debutant", "level_revision": "0"}
        assert_private(response)
    assert env.generation(generation_id)["delivery_confirmed_at"] == confirmed_at


def test_resolve_then_revocation_then_me_is_refused(env, composed):
    browser = browser_for(composed)
    generation_id = http_sign_in(env, browser)
    assert browser.resolve(generation_id).status_code == 200
    assert browser.me(generation_id).status_code == 200
    session_id = env.generation(generation_id)["session_id"]
    assert env.service.revoke_session(session_id, "admin") is True
    assert_error(browser.me(generation_id), 401, "session_invalid")
    assert browser.actives()  # nettoyage ciblé : propre aux routes F2 (resolve, logout)


def test_logout_then_replayed_cookie_is_refused(env, composed):
    browser = browser_for(composed)
    generation_id = http_sign_in(env, browser)
    assert browser.resolve(generation_id).status_code == 200
    saved = browser.cookie_header()
    assert browser.logout(generation_id).status_code == 200
    replay = me_request(composed, headers=me_headers(generation_id, saved))
    assert_error(replay, 401, "session_invalid")


def test_resolve_then_expiry_then_me_is_refused(env, composed):
    browser = browser_for(composed)
    generation_id = http_sign_in(env, browser)
    assert browser.resolve(generation_id).status_code == 200
    env.expire_at(env.generation(generation_id)["session_id"], "clock_timestamp() - interval '1 microsecond'")
    assert_error(browser.me(generation_id), 401, "session_invalid")


def test_resolved_generation_does_not_authorize_another_generation(env, composed):
    browser = browser_for(composed)
    gen_a = http_sign_in(env, browser)
    gen_b = http_sign_in(env, browser, "alice2@example.com", USER_A)  # second justificatif, jamais résolu
    assert browser.resolve(gen_a).status_code == 200
    assert browser.me(gen_a).status_code == 200
    assert_error(browser.me(gen_b), 401, "session_invalid")
    # Sélecteur B avec le seul cookie de A : jamais de choix implicite.
    only_a = "; ".join(f"{k}={v}" for k, v in browser.jar.items() if not k.endswith(gen_b))
    assert_error(me_request(composed, headers=me_headers(gen_b, only_a)), 401, "session_invalid")


def test_two_accounts_in_one_browser_stay_separate(env, composed):
    browser = browser_for(composed)
    gen_a = http_sign_in(env, browser)
    gen_b = http_sign_in(env, browser, BOB, USER_B)
    for generation_id in (gen_a, gen_b):
        assert browser.resolve(generation_id).status_code == 200
    assert browser.me(gen_a).json()["user_id"] == USER_A
    assert browser.me(gen_b).json()["user_id"] == USER_B


def test_resolve_keeps_its_pr3_csrf_protocol_in_the_composition(env, composed):
    browser = browser_for(composed)
    generation_id = http_sign_in(env, browser)
    refused = browser.resolve(generation_id, csrf=False)
    assert refused.status_code == 403 and refused.json() == {"error": "csrf_invalid"}
    assert env.generation(generation_id)["delivery_status"] == "pending"
    assert_error(browser.me(generation_id), 401, "session_invalid")


def test_composition_routes_nothing_else(env, composed, alice):
    for path in ("/api/user/user-a", "/api/v2", "/api/v2x/me", "/health"):
        response = me_request(composed, path=path, headers=me_headers(alice.generation_id, active_cookie(alice)))
        assert response.status_code == 404 and response.headers.get_list("set-cookie") == []


# ==========================================================================
# R1 PR 6 : PUT /api/v2/me/level
# ==========================================================================

MAX_REVISION = 2**63 - 1


def put(env, issued=None, *, app=None, body=None, **kwargs):
    return put_level(app or personal_app(env), issued, body=body, **kwargs)


def assert_raw_private(response):
    for name, value in NO_STORE.items():
        assert response.get_list(name) == [value], name
    assert response.get_list("set-cookie") == []
    assert response.get_list("access-control-allow-origin") == []


def assert_raw_error(response, status, code):
    assert response.status_code == status, response.content
    assert response.json() == {"error": code}
    assert_raw_private(response)


# --------------------------------------------------------------------------
# Succès, matrice CAS, bornes
# --------------------------------------------------------------------------

def test_put_level_changes_level_and_increments_revision(env, alice):
    env.set_profile("debutant", 7)
    response = put(env, alice, body=level_body("avance", "7"))
    assert response.status_code == 200, response.text
    assert response.content == b'{"level":"avance","level_revision":"8","user_id":"user-a"}'
    assert response.headers["content-type"] == "application/json"
    assert_private(response)
    assert env.profile()[:2] == ("avance", 8)
    read = get_me(env, alice)
    assert read.json() == {"user_id": USER_A, "level": "avance", "level_revision": "8"}


@pytest.mark.parametrize("requested,expected,status,body,after", [
    ("avance", "7", 200, {"user_id": USER_A, "level": "avance", "level_revision": "8"}, ("avance", 8)),
    ("debutant", "7", 200, {"user_id": USER_A, "level": "debutant", "level_revision": "7"}, ("debutant", 7)),
    ("avance", "6", 409, {"error": "level_revision_conflict"}, ("debutant", 7)),
    ("debutant", "6", 409, {"error": "level_revision_conflict"}, ("debutant", 7)),
    ("avance", "8", 409, {"error": "level_revision_conflict"}, ("debutant", 7)),
])
def test_put_level_cas_matrix(env, alice, requested, expected, status, body, after):
    env.set_profile("debutant", 7)
    before = env.profile()
    response = put(env, alice, body=level_body(requested, expected))
    assert response.status_code == status and response.json() == body
    assert_private(response)
    assert env.profile()[:2] == after
    if after == before[:2]:
        assert env.profile() == before  # aucune écriture


def test_put_level_cycle_two_increments_and_lost_response_retry_conflicts(env, alice):
    app = personal_app(env)
    assert put(env, alice, app=app, body=level_body("avance", "0")).json()["level_revision"] == "1"
    assert put(env, alice, app=app, body=level_body("debutant", "1")).json()["level_revision"] == "2"
    # Rejeu de la première requête (réponse perdue) : conflit, aucune restitution.
    replay = put(env, alice, app=app, body=level_body("avance", "0"))
    assert_error(replay, 409, "level_revision_conflict")
    assert env.profile()[:2] == ("debutant", 2)


@pytest.mark.parametrize("start", [2**53 - 1, 2**53, 2**53 + 1])
def test_put_level_around_two_to_the_53(env, alice, start):
    env.set_profile("debutant", start)
    response = put(env, alice, body=level_body("avance", str(start)))
    assert response.status_code == 200
    assert response.json()["level_revision"] == str(start + 1)  # chaîne exacte, aucune perte de précision
    assert env.profile()[:2] == ("avance", start + 1)
    assert get_me(env, alice).json()["level_revision"] == str(start + 1)


def test_put_level_at_maximum_revision(env, alice, caplog):
    env.set_profile("debutant", MAX_REVISION)
    before = env.profile()
    same = put(env, alice, body=level_body("debutant", str(MAX_REVISION)))
    assert same.status_code == 200 and same.json()["level_revision"] == "9223372036854775807"
    with caplog.at_level("DEBUG"):
        change = put(env, alice, body=level_body("avance", str(MAX_REVISION)))
    assert_error(change, 503, "temporarily_unavailable")
    assert env.profile() == before
    assert any("alert=level_revision_exhausted" in r.getMessage() for r in caplog.records)
    assert str(MAX_REVISION) not in caplog.text
    assert_error(put(env, alice, body=level_body("avance", str(MAX_REVISION + 1))), 400, "invalid_request")


def test_get_me_refuses_an_incoherent_profile(env, alice):
    env.set_profile("intermediaire", 3)
    before = env.profile()
    assert_error(get_me(env, alice), 503, "temporarily_unavailable")
    assert_error(put(env, alice, body=level_body("avance", "3")), 503, "temporarily_unavailable")
    assert env.profile() == before  # aucune correction silencieuse


def test_put_level_never_creates_a_missing_profile(env, alice):
    with env.observer.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": USER_A})
    assert_error(put(env, alice), 503, "temporarily_unavailable")
    assert env.count("users") == 0


def test_put_level_ignores_any_client_identifier(env, alice):
    env.confirmed(BOB, USER_B)
    before_b = env.profile(USER_B)
    response = put(env, alice, extra={"x-oryx-user-id": USER_B, "x-user-id": USER_B})
    assert response.status_code == 200 and response.json()["user_id"] == USER_A
    assert env.profile(USER_B) == before_b
    body = json.dumps({"level": "avance", "expected_revision": "1", "user_id": USER_B}).encode()
    assert_error(put(env, alice, body=body), 400, "invalid_request")
    assert env.profile(USER_B) == before_b


def test_get_me_reads_no_write_and_sets_no_cookie_after_mutations(env, alice):
    assert put(env, alice).status_code == 200
    before = env.profile()
    generation = dict(env.generation(alice.generation_id))
    for _ in range(3):
        response = get_me(env, alice)
        assert response.json()["level_revision"] == "1"
        assert_private(response)
    assert env.profile() == before and dict(env.generation(alice.generation_id)) == generation


# --------------------------------------------------------------------------
# Provenance (403)
# --------------------------------------------------------------------------

PUT_PROVENANCE_REFUSALS = {
    "no_origin_even_same_origin_fetch": {"origin": None},
    "no_origin_no_fetch_site": {"origin": None, "fetch_site": None},
    "foreign_origin": {"origin": "https://evil.test"},
    "null_origin": {"origin": "null"},
    "http_origin": {"origin": "http://oryx.test"},
    "origin_with_path": {"origin": ORIGIN + "/"},
    "uppercase_origin": {"origin": "https://ORYX.test"},
    "fetch_site_same_site": {"fetch_site": "same-site"},
    "fetch_site_cross_site": {"fetch_site": "cross-site"},
    "fetch_site_none": {"fetch_site": "none"},
    "foreign_referer": {"extra": {"referer": "https://evil.test/page"}},
    "http_referer": {"extra": {"referer": "http://oryx.test/page"}},
    "referer_with_credentials": {"extra": {"referer": "https://user:pw@oryx.test/"}},
    "foreign_host": {"extra": {"host": "evil.test"}},
    "uppercase_host": {"extra": {"host": "ORYX.TEST"}},
    "explicit_443_host": {"extra": {"host": "oryx.test:443"}},
}


@pytest.mark.parametrize("case", sorted(PUT_PROVENANCE_REFUSALS))
def test_put_provenance_refusals(env, alice, case):
    before = env.profile()
    assert_error(put(env, alice, **PUT_PROVENANCE_REFUSALS[case]), 403, "origin_refused")
    assert env.hook_calls == [] and env.profile() == before


def test_put_origin_must_equal_the_host_derived_origin(env, alice):
    """Deux origines autorisées : une Origin autorisée mais différente de
    https://<Host> est refusée."""
    app = build_isolated_personal_app(engine=env.engine, keyring=env.keyring, csrf_keyring=csrf_keyring(),
                                      allowed_origins=[ORIGIN, "https://other.test"], fault_hook=env.hook)
    assert_error(put_level(app, alice, origin="https://other.test"), 403, "origin_refused")
    assert put_level(app, alice).status_code == 200


@pytest.mark.parametrize("name,value", [("origin", ORIGIN), ("sec-fetch-site", "same-origin"),
                                        ("referer", ORIGIN + "/"), ("host", "oryx.test")])
def test_put_duplicated_provenance_headers_are_refused(env, alice, name, value):
    headers = list(level_headers(alice).items()) + [(name, value), (name, value)]
    assert_error(put(env, headers=headers), 403, "origin_refused")


@pytest.mark.parametrize("extra", [{}, {"referer": ORIGIN + "/profil?x=1"}])
def test_put_accepted_provenance(env, alice, extra):
    assert put(env, alice, extra=extra).status_code == 200
    env.set_profile("debutant", 0)
    assert put(env, alice, fetch_site=None).status_code == 200  # Sec-Fetch-Site facultatif si Origin exact


# --------------------------------------------------------------------------
# CSRF (403 / 503 / construction)
# --------------------------------------------------------------------------

def _token(*, origin=ORIGIN, binding=None, now=None, keyring=None):
    binding = binding or csrf.new_binding()
    token = csrf.issue_token(keyring or csrf_keyring(), binding, origin, int(time.time() if now is None else now))
    return binding, token


def _csrf_headers(alice, binding, token):
    return level_headers(alice, cookie=f"{active_cookie(alice)}; __Host-oryx-csrf-binding={binding}",
                         csrf_token=token)


def test_put_csrf_refusals(env, alice):
    binding, token = _token()
    other_binding, _ = _token()
    _, foreign_origin_token = _token(origin="https://evil.test", binding=binding)
    _, expired = _token(binding=binding, now=time.time() - csrf.TOKEN_TTL_SECONDS - 1)
    _, future = _token(binding=binding, now=time.time() + csrf.TOKEN_TTL_SECONDS + 60)
    _, unknown_version = _token(binding=binding, keyring=csrf.CsrfKeyring({"c9": bytes([0xEE]) * 32}, "c9"))
    cases = {
        "absent": level_headers(alice, csrf_token=None),
        "garbage": _csrf_headers(alice, binding, "x.y.z.w"),
        "other_binding": _csrf_headers(alice, other_binding, token),
        "foreign_origin": _csrf_headers(alice, binding, foreign_origin_token),
        "expired": _csrf_headers(alice, binding, expired),
        "beyond_ttl": _csrf_headers(alice, binding, future),
        "unknown_version": _csrf_headers(alice, binding, unknown_version),
        "binding_cookie_absent": level_headers(alice, cookie=active_cookie(alice), csrf_token=token),
        "duplicated": list(_csrf_headers(alice, binding, token).items()) + [("x-oryx-csrf", token)],
    }
    before = env.profile()
    for name, headers in cases.items():
        assert_error(put(env, headers=headers), 403, "csrf_invalid")
        assert env.hook_calls == [], name
    assert env.profile() == before
    assert put(env, headers=_csrf_headers(alice, binding, token)).status_code == 200


def test_put_csrf_token_from_a_rotated_keyring(env, alice):
    """Rotation PR 3 : l'ancienne version reste acceptée tant qu'elle est
    dans le trousseau, refusée une fois retirée."""
    k1, k2 = bytes([0xC0]) * 32, bytes([0xC5]) * 32
    binding, token = _token(keyring=csrf.CsrfKeyring({"c1": k1}, "c1"))
    rotated = build_isolated_personal_app(engine=env.engine, keyring=env.keyring, allowed_origins=[ORIGIN],
                                          csrf_keyring=csrf.CsrfKeyring({"c2": k2, "c1": k1}, "c2"))
    assert put_level(rotated, headers=_csrf_headers(alice, binding, token)).status_code == 200
    retired = build_isolated_personal_app(engine=env.engine, keyring=env.keyring, allowed_origins=[ORIGIN],
                                          csrf_keyring=csrf.CsrfKeyring({"c2": k2}, "c2"))
    assert_error(put_level(retired, headers=_csrf_headers(alice, binding, token)), 403, "csrf_invalid")


@pytest.mark.parametrize("wall_clock", [
    lambda: (_ for _ in ()).throw(OSError("horloge en panne")),
    lambda: float("nan"),
    lambda: float("inf"),
    lambda: "maintenant",
])
def test_put_failing_csrf_clock_is_unavailable(env, alice, wall_clock):
    app = personal_app(env, wall_clock=wall_clock)
    assert_error(put_level(app, alice), 503, "temporarily_unavailable")
    # Même sans jeton : la dépendance défaillante prime (aucun 403 trompeur).
    assert_error(put_level(app, alice, csrf_token=None), 503, "temporarily_unavailable")
    assert env.hook_calls == []
    assert get_me(env, alice, app=app).status_code == 200  # GET ne dépend pas du CSRF


@pytest.mark.parametrize("deps", ["no_keyring", "no_clock", "wrong_keyring"])
def test_put_unavailable_csrf_dependency_is_503_even_with_a_bad_selector(env, alice, deps):
    runner = env.runner()
    origins = validate_allowed_origins([ORIGIN])
    app = make_personal_app({
        "no_keyring": PersonalDependencies(runner, origins, None, time.time),
        "no_clock": PersonalDependencies(runner, origins, csrf_keyring(), None),
        "wrong_keyring": PersonalDependencies(runner, origins, {"c1": b"x" * 32}, time.time),
    }[deps])
    assert_error(put_level(app, alice), 503, "temporarily_unavailable")
    assert_error(put_level(app, alice, generation_id="pas-un-uuid"), 503, "temporarily_unavailable")
    assert env.hook_calls == []


def test_invalid_csrf_composition_fails_construction(env):
    base = {"engine": env.engine, "keyring": env.keyring, "allowed_origins": [ORIGIN]}
    for bad in (None, {"c1": bytes([0xC0]) * 32}, "csrf"):
        with pytest.raises(PersonalHttpConfigError):
            build_isolated_personal_app(csrf_keyring=bad, **base)
    with pytest.raises(TypeError):
        build_isolated_personal_app(**base)  # trousseau CSRF obligatoire, jamais implicite
    with pytest.raises(PersonalHttpConfigError):
        build_isolated_personal_app(csrf_keyring=csrf_keyring(), wall_clock="now", **base)
    f2_key = env.keyring.key("active", "t1")
    for purpose in ("active", "abuse", "otp"):
        shared = csrf.CsrfKeyring({"c1": env.keyring.key(purpose, "t1")}, "c1")
        with pytest.raises(PersonalHttpConfigError):
            build_isolated_personal_app(csrf_keyring=shared, **base)
    with pytest.raises(PersonalHttpConfigError):
        build_isolated_personal_app(csrf_keyring=csrf.CsrfKeyring({"c1": bytes([0xC0]) * 32, "c2": f2_key}, "c1"),
                                    **base)


# --------------------------------------------------------------------------
# Sélecteur, query, Content-Type, framing, taille
# --------------------------------------------------------------------------

@pytest.mark.parametrize("selector", [None, "", "abc", "{G}", " {g}", "{g}x"])
def test_put_malformed_selector_is_invalid_request(env, alice, selector):
    headers = level_headers(alice)
    headers.pop("x-oryx-generation-id")
    if selector is not None:
        headers["x-oryx-generation-id"] = selector.format(g=alice.generation_id, G=str(alice.generation_id).upper())
    assert_error(put(env, headers=headers), 400, "invalid_request")
    assert env.hook_calls == []


def test_put_duplicated_selector_is_invalid_request(env, alice):
    headers = list(level_headers(alice).items()) + [("x-oryx-generation-id", str(alice.generation_id))]
    assert_error(put(env, headers=headers), 400, "invalid_request")


@pytest.mark.parametrize("cookie", ["__Host-oryx-active-{g}=pas-un-secret", "__Host-oryx-inconnu={s}",
                                    "__Host-oryx-active-{g}={s}; __Host-oryx-active-{g}={s}"])
def test_put_malformed_cookie_header_is_invalid_request(env, alice, cookie):
    value = cookie.format(g=alice.generation_id, s=alice.active_secret.reveal())
    assert_error(put(env, alice, cookie=value), 400, "invalid_request")


@pytest.mark.parametrize("path", [ME_LEVEL + "?user_id=user-b", ME_LEVEL + "?x", ME_LEVEL + "?level=avance"])
def test_put_query_is_invalid_request(env, alice, path):
    assert_error(put(env, alice, path=path), 400, "invalid_request")
    assert env.hook_calls == []


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/jsonx", "application/json;",
                                          "application/json; charset=latin-1", "application/json; charset=utf8",
                                          "application/json; charset=utf-8; charset=utf-8",
                                          "application/json; boundary=x", "application/x-www-form-urlencoded",
                                          "multipart/form-data"])
def test_put_content_type_is_strict(env, alice, content_type):
    assert_error(put(env, alice, content_type=content_type), 415, "invalid_request")
    assert env.hook_calls == []


def test_put_two_content_type_headers_are_refused(env, alice):
    headers = list(level_headers(alice).items()) + [("content-type", "application/json")]
    assert_error(put(env, headers=headers), 415, "invalid_request")


@pytest.mark.parametrize("content_type", ["application/json; charset=utf-8", "application/json;charset=UTF-8",
                                          "Application/JSON"])
def test_put_accepted_content_types(env, alice, content_type):
    assert put(env, alice, content_type=content_type).status_code == 200


def raw_put(env, alice, *, content_length=None, extra=(), chunks=None, app=None, **kwargs):
    headers = raw_level_headers(alice, **kwargs)
    if content_length is not None:
        headers += [("content-length", v) for v in (content_length if isinstance(content_length, list)
                                                   else [content_length])]
    headers += list(extra)
    return raw_request(app or personal_app(env), headers=headers,
                       chunks=[level_body()] if chunks is None else chunks)


def test_raw_put_with_exact_content_length_succeeds(env, alice):
    body = level_body()
    response = raw_put(env, alice, content_length=str(len(body)), chunks=[body[:5], body[5:]])
    assert response.status_code == 200 and response.json()["level_revision"] == "1"
    assert_raw_private(response)


@pytest.mark.parametrize("value", ["", "+40", "-1", " 40", "40 ", "4 0", "0x28", "40.0", "1e2",
                                   "٤٠".encode("utf-8"), b"\xd9\xa4", "４０"])
def test_raw_put_malformed_content_length_is_invalid_request(env, alice, value):
    value = value.encode("utf-8") if isinstance(value, str) else value
    assert_raw_error(raw_put(env, alice, content_length=[value]), 400, "invalid_request")
    assert env.hook_calls == []


def test_raw_put_duplicated_content_length_is_invalid_request_even_if_equal(env, alice):
    size = str(len(level_body()))
    assert_raw_error(raw_put(env, alice, content_length=[size, size]), 400, "invalid_request")


@pytest.mark.parametrize("encoding", ["chunked", "identity", "gzip, chunked", ""])
def test_raw_put_any_transfer_encoding_is_invalid_request(env, alice, encoding):
    size = str(len(level_body()))
    assert_raw_error(raw_put(env, alice, extra=[("transfer-encoding", encoding)]), 400, "invalid_request")
    assert_raw_error(raw_put(env, alice, content_length=size, extra=[("transfer-encoding", encoding)]),
                     400, "invalid_request")


def test_put_chunked_client_body_is_invalid_request(env, alice):
    async def chunks():
        yield level_body()

    assert_error(put(env, alice, body=chunks()), 400, "invalid_request")


@pytest.mark.parametrize("announced,sent", [(100, 40), (10, 40), (1, 0), (0, 40)])
def test_raw_put_length_discrepancy_is_invalid_request(env, alice, announced, sent):
    body = level_body()[:sent] if sent <= len(level_body()) else level_body()
    if sent and len(body) != sent:
        body = body.ljust(sent)
    assert_raw_error(raw_put(env, alice, content_length=str(announced), chunks=[body] if body else []),
                     400, "invalid_request")
    assert env.hook_calls == []


def test_raw_put_disconnect_is_invalid_request(env, alice):
    body = level_body()
    response = raw_request(personal_app(env), headers=raw_level_headers(alice) + [
        ("content-length", str(len(body)))], chunks=[body[:10]], disconnect=True)
    assert_raw_error(response, 400, "invalid_request")
    assert env.hook_calls == []


def _padded(size):
    body = level_body()
    return body[:-1] + b" " * (size - len(body)) + b"}"


def test_raw_put_size_bound_is_4096(env, alice):
    exact = _padded(MAX_BODY_BYTES)
    assert len(exact) == 4096
    response = raw_put(env, alice, content_length=str(len(exact)), chunks=[exact])
    assert response.status_code == 200, response.content
    too_big = _padded(MAX_BODY_BYTES + 1)
    assert_raw_error(raw_put(env, alice, content_length=str(len(too_big)), chunks=[too_big]),
                     413, "request_too_large")


@pytest.mark.parametrize("announced", ["4097", "1000000", "99999999999999999999999999999", "0004097"])
def test_raw_put_announced_size_above_4096_is_413_without_reading(env, alice, announced):
    assert_raw_error(raw_put(env, alice, content_length=announced, chunks=[]), 413, "request_too_large")


def test_raw_put_streamed_size_above_4096_without_length_is_413(env, alice):
    chunks = [b" " * 2048, b" " * 2048, b" " + level_body()]
    assert_raw_error(raw_put(env, alice, chunks=chunks), 413, "request_too_large")
    assert env.hook_calls == []


def test_raw_put_without_content_length_but_bounded_stream_is_accepted(env, alice):
    response = raw_put(env, alice, chunks=[level_body()])
    assert response.status_code == 200


@pytest.fixture
def body_buffers(monkeypatch):
    """Tampons créés par _read_body, remplacés par un bytearray qui consigne
    la longueur après CHAQUE ajout : preuve déterministe (sans mesure
    mémoire) que la taille est jugée avant l'accumulation."""
    buffers = []

    class RecordingBuffer(bytearray):
        def __init__(self, *args):
            super().__init__(*args)
            self.lengths = []
            buffers.append(self)

        def __iadd__(self, chunk):
            result = super().__iadd__(chunk)
            self.lengths.append(len(self))
            return result

    monkeypatch.setattr(routes_module, "bytearray", RecordingBuffer, raising=False)
    return buffers


ONE_MIB = b" " * (1024 * 1024)


@pytest.mark.parametrize("content_length,status,code", [
    (None, 413, "request_too_large"),        # sans Content-Length : borne de 4096
    ("100", 400, "invalid_request"),          # Content-Length valide dépassé : discordance
    (str(MAX_BODY_BYTES), 400, "invalid_request"),
])
def test_single_huge_asgi_chunk_is_refused_before_buffering(env, alice, body_buffers, monkeypatch, content_length,
                                                            status, code):
    transactions = []
    monkeypatch.setattr(runner_module, "transaction",
                        lambda *a, **k: transactions.append("transaction") or (_ for _ in ()).throw(AssertionError()))
    before = env.profile()
    response = raw_put(env, alice, content_length=content_length, chunks=[ONE_MIB])
    assert_raw_error(response, status, code)
    (buffer,) = body_buffers
    assert buffer.lengths == [] and len(buffer) == 0  # le fragment de 1 Mio n'est jamais copié dans le tampon
    assert transactions == [] and env.hook_calls == []  # runner jamais appelé
    assert env.profile() == before  # aucune écriture PostgreSQL


@pytest.mark.parametrize("content_length,chunks,status,code,lengths", [
    (None, [b" " * 4000, b" " * 200], 413, "request_too_large", [4000]),
    (None, [b" " * 4096, b" "], 413, "request_too_large", [4096]),
    ("4000", [b" " * 3000, ONE_MIB], 400, "invalid_request", [3000]),
    ("10", [b" " * 10, b" "], 400, "invalid_request", [10]),
])
def test_cumulative_size_is_judged_before_each_append(env, alice, body_buffers, content_length, chunks, status, code,
                                                      lengths):
    before = env.profile()
    assert_raw_error(raw_put(env, alice, content_length=content_length, chunks=chunks), status, code)
    (buffer,) = body_buffers
    assert buffer.lengths == lengths and max(lengths) <= MAX_BODY_BYTES
    assert env.hook_calls == [] and env.profile() == before


def test_accepted_body_buffer_never_exceeds_the_bound(env, alice, body_buffers):
    exact = _padded(MAX_BODY_BYTES)
    response = raw_put(env, alice, content_length=str(MAX_BODY_BYTES), chunks=[exact[:1000], exact[1000:]])
    assert response.status_code == 200
    (buffer,) = body_buffers
    # Le message ASGI final (corps vide) est aussi ajouté : longueur inchangée.
    assert buffer.lengths[:2] == [1000, MAX_BODY_BYTES] and max(buffer.lengths) == MAX_BODY_BYTES


def test_size_refusals_are_judged_after_content_type_and_framing(env, alice):
    big = _padded(5000)
    # Content-Type invalide + taille excessive : 415.
    assert_raw_error(raw_put(env, alice, content_type="text/plain", content_length="5000", chunks=[big]),
                     415, "invalid_request")
    assert_raw_error(raw_put(env, alice, content_type="text/plain", chunks=[big]), 415, "invalid_request")
    # Framing invalide + taille excessive : 400.
    assert_raw_error(raw_put(env, alice, content_length=["5000", "5000"], chunks=[big]), 400, "invalid_request")
    assert_raw_error(raw_put(env, alice, content_length="5000", extra=[("transfer-encoding", "chunked")],
                             chunks=[big]), 400, "invalid_request")
    assert_raw_error(raw_put(env, alice, content_length="+5000", chunks=[big]), 400, "invalid_request")
    assert_raw_error(raw_put(env, alice, content_length="10", chunks=[big]), 400, "invalid_request")
    # Taille valide annoncée > 4096 : 413.
    assert_raw_error(raw_put(env, alice, content_length="5000", chunks=[big]), 413, "request_too_large")
    assert env.hook_calls == []


# --------------------------------------------------------------------------
# JSON et champs
# --------------------------------------------------------------------------

BAD_BODIES = {
    "invalid_utf8": b'{"level":"avance","expected_revision":"\xff"}',
    "utf8_bom": b"\xef\xbb\xbf" + level_body(),
    "utf16": json.dumps({"level": "avance", "expected_revision": "0"}).encode("utf-16"),
    "empty": b"",
    "array": b'["avance","0"]',
    "string": b'"avance"',
    "null": b"null",
    "missing_revision": b'{"level":"avance"}',
    "missing_level": b'{"expected_revision":"0"}',
    "extra_key": b'{"level":"avance","expected_revision":"0","x":1}',
    "user_id": b'{"level":"avance","expected_revision":"0","user_id":"user-b"}',
    "duplicated_level": b'{"level":"debutant","level":"avance","expected_revision":"0"}',
    "duplicated_revision": b'{"level":"avance","expected_revision":"1","expected_revision":"0"}',
    "nan": b'{"level":"avance","expected_revision":NaN}',
    "infinity": b'{"level":"avance","expected_revision":Infinity}',
    "trailing_garbage": level_body() + b"x",
    "two_documents": level_body() + level_body(),
    "nested": b'{"level":{"v":"avance"},"expected_revision":"0"}',
}


@pytest.mark.parametrize("case", sorted(BAD_BODIES))
def test_put_malformed_json_is_invalid_request(env, alice, case):
    assert_error(put(env, alice, body=BAD_BODIES[case]), 400, "invalid_request")
    assert env.hook_calls == []


@pytest.mark.parametrize("level", ["intermediaire", "Avance", "AVANCE", " avance", "avance ", "", "débutant",
                                   "debutant\u0000", 1, None, True, ["avance"]])
def test_put_unknown_level_is_invalid_request(env, alice, level):
    body = json.dumps({"level": level, "expected_revision": "0"}).encode()
    assert_error(put(env, alice, body=body), 400, "invalid_request")
    assert env.hook_calls == []


@pytest.mark.parametrize("revision", [
    0, 7, 7.0, -1, True, False, None, [], {},
    "", " 7", "7 ", "+7", "-7", "-0", "07", "00", "007", "1e3", "7.0", "0x7", "7_0", "1,000",
    "٧",          # chiffre arabo-indien
    "７",          # chiffre pleine chasse
    "²",          # exposant (str.isdigit() le juge chiffre)
    "१",          # chiffre devanagari
    "9223372036854775808", "18446744073709551616", "99999999999999999999", "10000000000000000000",
])
def test_put_non_canonical_revision_is_invalid_request(env, alice, revision):
    body = json.dumps({"level": "avance", "expected_revision": revision}).encode()
    assert_error(put(env, alice, body=body), 400, "invalid_request")
    assert env.hook_calls == []


@pytest.mark.parametrize("revision", ["0", "1", "9", "10", "9007199254740993", "9223372036854775807"])
def test_put_canonical_revisions_reach_the_cas(env, alice, revision):
    response = put(env, alice, body=level_body("avance", revision))
    if revision == "0":
        assert response.status_code == 200
    else:
        assert_error(response, 409, "level_revision_conflict")


# --------------------------------------------------------------------------
# Cookie désigné, runner, sessions (401 / 503)
# --------------------------------------------------------------------------

def test_put_designated_cookie_absent_is_session_invalid_without_choosing_another(env, alice):
    other = env.confirmed(BOB, USER_B)
    before_b = env.profile(USER_B)
    binding_cookie, token = csrf_pair()
    headers = level_headers(alice, cookie=f"{active_cookie(other)}; {binding_cookie}", csrf_token=token)
    assert_error(put(env, headers=headers), 401, "session_invalid")
    assert env.hook_calls == [] and env.profile(USER_B) == before_b


def test_put_session_refusals_are_401(env):
    issued = env.confirmed()
    wrong = f"__Host-oryx-active-{issued.generation_id}={b64url_encode(bytes(32))}"
    binding_cookie, token = csrf_pair()
    responses = [put(env, headers=level_headers(issued, cookie=f"{wrong}; {binding_cookie}", csrf_token=token))]
    pending = env.sign_in(BOB, USER_B)
    responses.append(put(env, pending))
    revoked = env.confirmed("alice2@example.com", USER_A)
    env.service.revoke_session(revoked.session_id, "admin")
    responses.append(put(env, revoked))
    expired = env.confirmed("alice3@example.com", USER_A)
    env.expire_at(expired.session_id, "clock_timestamp()")
    responses.append(put(env, expired))
    for response in responses:
        assert_error(response, 401, "session_invalid")
    assert env.profile()[:2] == ("debutant", 0) and env.profile(USER_B)[:2] == ("debutant", 0)
    assert env.generation(pending.generation_id)["delivery_status"] == "pending"


def test_put_unconfigured_runner_is_unavailable(env, alice):
    app = make_personal_app(PersonalDependencies(None, validate_allowed_origins([ORIGIN]), csrf_keyring(), time.time))
    assert_error(put_level(app, alice), 503, "temporarily_unavailable")
    # Le corps invalide reste un 400 : le runner n'est consulté qu'après le transport.
    assert_error(put_level(app, alice, body=b"{}"), 400, "invalid_request")


def test_put_database_outage_is_unavailable(env, alice):
    down = sa.create_engine("postgresql+psycopg2://postgres@127.0.0.1:1/oryx_down_test", poolclass=sa.pool.NullPool)
    app = build_isolated_personal_app(engine=down, keyring=env.keyring, allowed_origins=[ORIGIN],
                                      csrf_keyring=csrf_keyring())
    assert_error(put_level(app, alice), 503, "temporarily_unavailable")


@pytest.mark.parametrize("outcome", [
    runner_module.AuthorizationOutcome(runner_module.NOT_FOUND),
    runner_module.AuthorizationOutcome(runner_module.UNAVAILABLE),
    runner_module.AuthorizationOutcome("autre"),
    runner_module.AuthorizationOutcome(runner_module.AUTHORIZED, None, None),
    "pas un résultat",
])
def test_put_unexpected_runner_results_are_unavailable(env, alice, outcome):
    class Runner:
        def run(self, credential, command):
            assert type(command).__name__ == "UpdateProfileLevel"
            return outcome

    app = make_personal_app(PersonalDependencies(Runner(), validate_allowed_origins([ORIGIN]), csrf_keyring(),
                                                 time.time))
    assert_error(put_level(app, alice), 503, "temporarily_unavailable")


def test_put_result_is_verified_before_serialisation(env, alice):
    """Un succès incohérent (autre génération, autre utilisateur, autre
    niveau, révision hors de {attendue, attendue + 1}) n'est jamais rendu."""
    from core.authz_f2.commands import ProfileView
    from core.authz_f2.principal import AuthorizedPrincipal

    good = AuthorizedPrincipal(USER_A, alice.session_id, alice.generation_id, "supabase_email_sign_in")
    other_generation = AuthorizedPrincipal(USER_A, alice.session_id, uuid.uuid4(), "supabase_email_sign_in")
    cases = [
        (good, ProfileView(USER_B, "avance", 1)),
        (other_generation, ProfileView(USER_A, "avance", 1)),
        (good, ProfileView(USER_A, "debutant", 1)),
        (good, ProfileView(USER_A, "avance", 2)),
    ]
    for principal, view in cases:
        class Runner:
            def run(self, credential, command, principal=principal, view=view):
                return runner_module.AuthorizationOutcome(runner_module.AUTHORIZED, view, principal)

        app = make_personal_app(PersonalDependencies(Runner(), validate_allowed_origins([ORIGIN]), csrf_keyring(),
                                                     time.time))
        assert_error(put_level(app, alice, body=level_body("avance", "0")), 503, "temporarily_unavailable")


def test_put_runner_exception_is_unavailable(env, alice):
    class Runner:
        def run(self, credential, command):
            raise RuntimeError("panne imprévue")

    app = make_personal_app(PersonalDependencies(Runner(), validate_allowed_origins([ORIGIN]), csrf_keyring(),
                                                 time.time))
    assert_error(put_level(app, alice), 503, "temporarily_unavailable")


# --------------------------------------------------------------------------
# Priorités des contrôles de PUT
# --------------------------------------------------------------------------

@pytest.fixture
def put_trace(monkeypatch):
    calls = []

    def record(name, fn):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)
        return wrapper

    def record_async(name, fn):
        async def wrapper(*args, **kwargs):
            calls.append(name)
            return await fn(*args, **kwargs)
        return wrapper

    for name, attr in (("provenance", "personal_mutation_origin"), ("cookies", "parse_cookie_headers"),
                       ("csrf", "_check_csrf"), ("selector", "_generation_id"), ("content_type", "_check_content_type"),
                       ("framing", "_announced_length"), ("json", "_level_request")):
        monkeypatch.setattr(routes_module, attr, record(name, getattr(routes_module, attr)))
    monkeypatch.setattr(routes_module, "_read_body", record_async("body", routes_module._read_body))
    monkeypatch.setattr(runner_module, "transaction", record("transaction", runner_module.transaction))
    return calls


def test_put_check_priorities(env, alice, put_trace):
    """Ordre contractuel : provenance -> cookies -> CSRF -> sélecteur ->
    query -> Content-Type -> framing -> taille -> JSON (level puis
    expected_revision) -> cookie désigné -> runner. Le premier échec
    répond ; aucun contrôle suivant ne s'exécute."""
    big = _padded(5000)
    base = ["provenance", "cookies", "csrf", "selector"]
    bad_cookie = "__Host-oryx-active-x=y"
    cases = [
        ("provenance+tout", level_headers(alice, origin=None, csrf_token=None, generation_id="x", cookie=bad_cookie,
                                          content_type="text/plain"), ME_LEVEL + "?q", big,
         (403, "origin_refused"), ["provenance"]),
        ("cookies+csrf", level_headers(alice, cookie=bad_cookie, csrf_token=None), ME_LEVEL, None,
         (400, "invalid_request"), ["provenance", "cookies"]),
        ("csrf+selecteur", level_headers(alice, csrf_token=None, generation_id="x"), ME_LEVEL, None,
         (403, "csrf_invalid"), ["provenance", "cookies", "csrf"]),
        ("selecteur+query+content_type", level_headers(alice, generation_id="x", content_type="text/plain"),
         ME_LEVEL + "?q", big, (400, "invalid_request"), base),
        ("query+content_type+taille", level_headers(alice, content_type="text/plain"), ME_LEVEL + "?q", big,
         (400, "invalid_request"), base),
        ("content_type+taille", level_headers(alice, content_type="text/plain"), ME_LEVEL, big,
         (415, "invalid_request"), base + ["content_type"]),
        ("framing+taille", list(level_headers(alice).items()) + [("transfer-encoding", "chunked")], ME_LEVEL, big,
         (400, "invalid_request"), base + ["content_type", "framing"]),
        ("taille+json", level_headers(alice), ME_LEVEL, b"[" * 5000, (413, "request_too_large"),
         base + ["content_type", "framing"]),
        ("csrf_absent+cookie_designe_absent", level_headers(alice, cookie=csrf_pair()[0], csrf_token=None),
         ME_LEVEL, None, (403, "csrf_invalid"), ["provenance", "cookies", "csrf"]),
        ("json+cookie_designe_absent", level_headers(alice, generation_id=uuid.uuid4()), ME_LEVEL, b"{}",
         (400, "invalid_request"), base + ["content_type", "framing", "body", "json"]),
        ("level+revision", level_headers(alice), ME_LEVEL, json.dumps({"level": "x", "expected_revision": "07"})
         .encode(), (400, "invalid_request"), base + ["content_type", "framing", "body", "json"]),
        ("cookie_designe_absent", level_headers(alice, generation_id=uuid.uuid4()), ME_LEVEL, None,
         (401, "session_invalid"), base + ["content_type", "framing", "body", "json"]),
    ]
    app = personal_app(env)
    for name, headers, path, content, (status, code), executed in cases:
        put_trace.clear()
        response = me_request(app, method="PUT", path=path, headers=headers,
                              content=level_body() if content is None else content)
        assert response.status_code == status and response.json() == {"error": code}, name
        assert_private(response)
        assert put_trace == executed, name  # aucune transaction pour un refus HTTP
    assert env.hook_calls == []
    put_trace.clear()
    response = me_request(app, method="PUT", path=ME_LEVEL, headers=level_headers(alice), content=level_body())
    assert response.status_code == 200
    assert put_trace == base + ["content_type", "framing", "body", "json", "transaction", "transaction"]


def test_put_level_is_validated_before_expected_revision(env, alice, monkeypatch):
    seen = []
    real = routes_module.valid_level

    def spy(value):
        seen.append(("level", value))
        return real(value)

    monkeypatch.setattr(routes_module, "valid_level", spy)
    body = json.dumps({"expected_revision": "07", "level": "x"}).encode()  # clés dans l'ordre inverse
    assert_error(put(env, alice, body=body), 400, "invalid_request")
    assert seen == [("level", "x")]


# --------------------------------------------------------------------------
# Routage, en-têtes, journaux
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,status", [
    ("GET", ME_LEVEL, 405), ("POST", ME_LEVEL, 405), ("PATCH", ME_LEVEL, 405), ("DELETE", ME_LEVEL, 405),
    ("PUT", ME, 405), ("PUT", ME_LEVEL + "/x", 404), ("PUT", "/api/v2/me/levels", 404),
    ("PUT", "/api/user/user-a/level", 404), ("PUT", "/api/v2/user/user-a/level", 404),
])
def test_put_routing_refusals_keep_private_headers(env, alice, method, path, status):
    response = me_request(personal_app(env), method=method, path=path, headers=level_headers(alice),
                          content=level_body())
    assert response.status_code == status
    assert_private(response)
    assert env.hook_calls == [] and env.profile()[:2] == ("debutant", 0)


def test_put_preflight_gets_no_permissive_cors(env):
    response = me_request(personal_app(env), method="OPTIONS", path=ME_LEVEL, headers={
        "origin": "https://evil.test", "access-control-request-method": "PUT",
        "access-control-request-headers": "x-oryx-csrf, x-oryx-generation-id, content-type"})
    assert response.status_code == 405
    assert_private(response)
    assert "access-control-allow-methods" not in response.headers


def test_put_never_leaks_secrets_in_responses_or_logs(env, alice, caplog):
    binding, token = _token()
    headers = _csrf_headers(alice, binding, token)
    marker_body = json.dumps({"level": "avance", "expected_revision": "0", "marker": "SECRET-BODY-MARKER"}).encode()
    responses = []
    with caplog.at_level("DEBUG"):
        responses.append(put(env, headers=headers, body=marker_body))
        responses.append(put(env, headers=headers, body=level_body("avance", "5")))
        responses.append(put(env, headers=dict(headers, **{"x-oryx-csrf": token + "x"})))
        responses.append(put(env, headers=headers))
        responses.append(get_me(env, alice))
    statuses = [r.status_code for r in responses]
    assert statuses == [400, 409, 403, 200, 200]
    secrets = (alice.active_secret.reveal(), token, binding, "SECRET-BODY-MARKER", str(alice.session_id))
    for text in [caplog.text] + [r.text for r in responses]:
        for secret in secrets:
            assert secret not in text
    assert "SELECT" not in caplog.text and "UPDATE" not in caplog.text


# --------------------------------------------------------------------------
# Parcours de bout en bout (composition de test : bootstrap CSRF de PR 3)
# --------------------------------------------------------------------------

def test_end_to_end_bootstrap_resolve_get_and_put(env, composed):
    browser = browser_for(composed)  # GET /api/auth/f2/csrf/bootstrap (application F2)
    generation_id = http_sign_in(env, browser)
    assert_error(browser.put_level(generation_id, "avance", "0"), 401, "session_invalid")  # pending
    assert browser.resolve(generation_id).status_code == 200
    me = browser.me(generation_id)
    assert me.json() == {"user_id": USER_A, "level": "debutant", "level_revision": "0"}
    jar = dict(browser.jar)
    changed = browser.put_level(generation_id, "avance", me.json()["level_revision"])
    assert changed.status_code == 200 and changed.json() == {"user_id": USER_A, "level": "avance",
                                                             "level_revision": "1"}
    assert_private(changed)
    assert browser.jar == jar  # aucun cookie posé ni effacé
    assert_error(browser.put_level(generation_id, "debutant", "0"), 409, "level_revision_conflict")
    assert browser.me(generation_id).json()["level_revision"] == "1"
    assert_error(browser.put_level(generation_id, "debutant", "1", headers={"x-oryx-csrf": "absent"}), 403,
                 "csrf_invalid")
    assert browser.put_level(generation_id, "debutant", "1").json()["level_revision"] == "2"
    assert browser.logout(generation_id).status_code == 200
    replay = me_request(composed, method="PUT", path=ME_LEVEL, content=level_body("avance", "2"), headers={
        "origin": ORIGIN, "x-oryx-csrf": browser.csrf, "x-oryx-generation-id": generation_id,
        "content-type": "application/json",
        "cookie": f"__Host-oryx-active-{generation_id}={jar[f'__Host-oryx-active-{generation_id}']}; "
                  f"__Host-oryx-csrf-binding={jar['__Host-oryx-csrf-binding']}"})
    assert_error(replay, 401, "session_invalid")
    assert env.profile()[:2] == ("debutant", 2)


# --------------------------------------------------------------------------
# Contournement historique (bloque l'activation publique)
# --------------------------------------------------------------------------

def test_legacy_level_route_bypasses_the_versioned_contract(env, alice):
    """Test NÉGATIF documentant un contournement CONNU, non corrigé par PR 6.

    La route historique PUT /api/user/{user_id}/level du vrai api:app
    écrit users.level sans incrémenter level_revision, pour un user_id
    choisi par le client. Le CAS de F2 ne peut donc PAS détecter cette
    écriture intermédiaire : une demande F2 fondée sur la révision 7 réussit
    alors que le niveau a changé entre-temps. Ce test explique le blocage
    de l'activation publique de F2 ; il ne déclare pas le système sûr."""
    import api
    import core.db
    from sqlalchemy.orm import sessionmaker

    env.set_profile("debutant", 7)
    sessions = sessionmaker(bind=env.observer)

    def override_get_db():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    async def legacy_put(user_id, level):
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.put(f"/api/user/{user_id}/level", json={"level": level})

    api.app.dependency_overrides[core.db.get_db] = override_get_db
    try:
        # 1. Le client observe (debutant, 7) par F2.
        assert get_me(env, alice).json() == {"user_id": USER_A, "level": "debutant", "level_revision": "7"}
        # 2. Écriture historique, sans authentification F2 : (avance, 7).
        legacy = asyncio.run(legacy_put(USER_A, "avance"))
        assert legacy.status_code == 200 and legacy.json() == {"success": True}
    finally:
        api.app.dependency_overrides.clear()
    assert env.profile()[:2] == ("avance", 7)  # niveau changé, révision INCHANGÉE
    # 3. Requête F2 fondée sur la révision 7 : le CAS ne voit rien.
    response = put(env, alice, body=level_body("debutant", "7"))
    # 4. Succès : l'écriture historique intermédiaire n'est pas détectée.
    assert response.status_code == 200
    assert response.json() == {"user_id": USER_A, "level": "debutant", "level_revision": "8"}
    # Une demande « identique » à la révision 7 aurait aussi réussi sans
    # conflit : la révision seule ne fait foi que si TOUTES les écritures
    # passent par la frontière F2. Activation publique interdite tant que
    # la route historique existe.
