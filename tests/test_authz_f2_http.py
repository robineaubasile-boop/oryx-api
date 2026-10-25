"""GET /api/v2/me (R1 PR 5) : transport, provenance, statuts et parcours
de bout en bout sur la composition de test F2 + PR 5.

Application personnelle ISOLÉE (core.authz_f2_http.isolated) autour du
vrai AuthorizedTransactionRunner ; sessions émises par le vrai moteur F2,
par appel direct ou par les routes HTTP F2 de la composition de test
(tests/authz_f2_support.py). Aucun réseau, aucune clé réelle.
"""
import json
import uuid

import pytest
import sqlalchemy as sa

from core.auth_f2.crypto import b64url_encode
from core.auth_f2_http.provenance import validate_allowed_origins
from core.authz_f2_http.isolated import PersonalHttpConfigError, build_isolated_personal_app, make_personal_app
from core.authz_f2_http.routes import MAX_BODY_BYTES, PersonalDependencies
from tests.authz_f2_support import (
    ALICE,
    BOB,
    ME,
    USER_A,
    USER_B,
    AuthzEnv,
    PersonalBrowser,
    active_cookie,
    compose_test_app,
    me_headers,
    me_request,
    personal_app,
)
from tests.f2_http_support import ORIGIN
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
    assert response.json() == {"user_id": USER_A, "level": "debutant"}
    assert response.headers["content-type"] == "application/json"
    assert_private(response)
    for secret in (alice.active_secret.reveal(), str(alice.session_id), str(alice.generation_id)):
        assert secret not in response.text
    assert "level_revision" not in response.text


def test_level_comes_from_postgresql_and_no_client_identifier_is_honoured(env, alice):
    env.confirmed(BOB, USER_B)
    with env.observer.begin() as conn:
        conn.execute(sa.text("UPDATE users SET level = 'avance' WHERE id = :u"), {"u": USER_A})
    response = get_me(env, alice, headers=me_headers(alice.generation_id, active_cookie(alice),
                                                     extra={"x-oryx-user-id": USER_B, "x-user-id": USER_B}))
    assert response.json() == {"user_id": USER_A, "level": "avance"}


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

def test_check_priorities(env, alice):
    bad_cookie = "__Host-oryx-active-x=y"
    cases = [
        # (en-têtes, chemin, corps) -> erreur attendue (la première dans l'ordre figé)
        (me_headers(None, bad_cookie, fetch_site="cross-site"), ME + "?q", b"x" * 9999, (403, "origin_refused")),
        (me_headers(None, bad_cookie), ME + "?q", b"x" * 9999, (400, "invalid_request")),
        (me_headers(None, active_cookie(alice)), ME + "?q", b"x" * 9999, (400, "invalid_request")),
        (me_headers(None, active_cookie(alice)), ME, b"x" * 9999, (413, "request_too_large")),
        (me_headers("abc", None), ME, None, (400, "invalid_request")),
        (me_headers(uuid.uuid4(), None), ME, None, (401, "session_invalid")),
    ]
    for headers, path, content, (status, code) in cases:
        assert_error(me_request(personal_app(env), headers=headers, path=path, content=content), status, code)
    assert env.hook_calls == []


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
    app = build_isolated_personal_app(engine=down, keyring=env.keyring, allowed_origins=[ORIGIN])
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
            build_isolated_personal_app(engine=env.engine, keyring=env.keyring, allowed_origins=origins)
    with pytest.raises(PersonalHttpConfigError):
        build_isolated_personal_app(engine=env.engine, keyring={"active": {}}, allowed_origins=[ORIGIN])


@pytest.mark.parametrize("method,path,status", [
    ("POST", ME, 405), ("PUT", ME, 405), ("DELETE", ME, 405), ("PATCH", ME, 405), ("HEAD", ME, 405),
    ("GET", ME + "/x", 404), ("GET", "/api/v2/user/user-a", 404), ("GET", "/api/user/user-a", 404),
    ("GET", "/api/auth/f2/csrf/bootstrap", 404),
])
def test_only_get_me_exists_in_the_personal_application(env, alice, method, path, status):
    response = me_request(personal_app(env), method=method, path=path,
                          headers=me_headers(alice.generation_id, active_cookie(alice)))
    assert response.status_code == status
    assert_private(response)
    assert env.hook_calls == []


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
        assert response.status_code == 200 and response.json() == {"user_id": USER_A, "level": "debutant"}
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
