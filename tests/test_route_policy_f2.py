"""F-02A V0.2 — porte de routage devant les VRAIES compositions F2 isolées,
contre un VRAI PostgreSQL (16 et 18 en CI), transports simulés, aucun réseau.

1. les compositions réelles exposent exactement les 27 routes F2 du registre
   (14 d'authentification, 13 personnelles) et la porte les accepte ;
2. contrats F2 inchangés : chaque réponse F2 est identique OCTET POUR OCTET
   (statut, en-têtes, corps) avec ou sans la porte ; un parcours navigateur
   complet (invitation, liaison e-mail OTP, résolution, profil, niveau,
   portefeuille, Décrypter C3, replay, ACK) traverse la porte ;
3. les refus (410 legacy, 404 désactivés / candidats / inconnus / non
   canoniques, 405) n'appellent ni le runner, ni le moteur F2, ni un
   fournisseur, n'écrivent rien et ne posent aucun cookie, même avec une
   session F2 valide ;
4. aucun deuxième autoriseur : l'autorisation reste celle du runner unique
   (les refus 401 / 403 F2 traversent la porte inchangés).
"""
import asyncio
import uuid

import httpx
import pytest

from core.route_policy import gate as g
from core.route_policy import registry as reg
from tests.authz_f2_pr9a_support import BOB_EMAIL, USER_B
from tests.authz_f2_pr9c1b_support import schema  # noqa: F401 — fixture
from tests.authz_f2_pr9c3_support import ACK, REPLAY, TURNS, HttpEnv, body_of, c3_headers, identity_fields, \
    turn_fields
from tests.authz_f2_support import ME_LEVEL, PersonalBrowser, f2_app, level_body, level_headers, personal_app, \
    portfolio_headers, position_body, raw_request
from tests.f2_http_support import ORIGIN, PEER
from tests.f2_pr4_support import activate, link_start, link_verify
from tests.test_act_l1_pr1_identity import durable_state, personal_requests
from tests.test_authz_f2_http import NO_STORE
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_route_policy import RETIRED_PATHS, _variants, concrete

CAROL = "carol@example.com"


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
def apps(env):
    auth, personal = f2_app(env), env.c3_app()
    return auth, personal, g.RoutePolicyGate(f2_auth_app=auth, f2_personal_app=personal)


def request(app, method, path, headers=None, content=None):
    async def run():
        transport = httpx.ASGITransport(app=app, client=PEER)
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            return await client.request(method, path, headers=headers or {}, content=content)
    return asyncio.run(run())


def snapshot(response):
    return response.status_code, list(response.headers.multi_items()), response.content


def calls(env):
    return (list(env.hook_calls), env.provider_calls(), len(env.supabase.generate_calls),
            len(env.supabase.verify_calls), len(env.mailer.sent))


def untouched(env, before, before_calls):
    assert calls(env) == before_calls
    assert env.hook_calls == [] and env.provider_calls() == []
    assert durable_state(env) == before


def routes_of(app):
    return {(method, route.path) for route in app.routes for method in route.methods}


# --------------------------------------------------------------------------
# 1. Inventaire F2 réel
# --------------------------------------------------------------------------

def test_real_f2_compositions_expose_exactly_the_registered_routes(env):
    assert routes_of(f2_app(env)) == reg.pairs_of(reg.F2_AUTH)
    assert len(reg.pairs_of(reg.F2_AUTH)) == 14
    assert routes_of(env.c3_app()) == reg.pairs_of(reg.F2_PERSONAL)
    assert len(reg.pairs_of(reg.F2_PERSONAL)) == 13
    assert routes_of(personal_app(env)) < reg.pairs_of(reg.F2_PERSONAL)  # sans C3 : 10 routes
    assert len(routes_of(personal_app(env))) == 10
    for auth, personal in ((f2_app(env), env.c3_app()), (f2_app(env), personal_app(env)), (None, env.c3_app()),
                           (f2_app(env), None)):
        assert isinstance(g.RoutePolicyGate(f2_auth_app=auth, f2_personal_app=personal), g.RoutePolicyGate)
    with pytest.raises(g.RouteGateConfigError):
        g.RoutePolicyGate(f2_auth_app=env.c3_app(), f2_personal_app=f2_app(env))  # catégories inversées


# --------------------------------------------------------------------------
# 2. Contrats F2 inchangés
# --------------------------------------------------------------------------

def test_every_personal_route_is_byte_identical_through_the_gate(env, alice, apps):
    _, personal, gate = apps
    invited = env.invited()  # 403 identity_link_required (aucun appel de moteur métier)
    revoked = env.confirmed(BOB_EMAIL, USER_B)
    env.service.revoke_session(revoked.session_id, "logout")
    for issued in (invited, revoked):
        for method, _, path, headers, content in personal_requests(issued):
            direct = request(personal, method, path, headers, content)
            through = request(gate, method, path, headers, content)
            assert snapshot(through) == snapshot(direct), (method, path)
            assert direct.status_code in (401, 403), (method, path, direct.status_code)
    # Lectures autorisées (aucun effet) : identiques octet pour octet.
    for method, _, path, headers, content in personal_requests(alice):
        if method == "GET":
            direct = request(personal, method, path, headers, content)
            through = request(gate, method, path, headers, content)
            assert snapshot(through) == snapshot(direct), path
            assert direct.status_code in (200, 404), (path, direct.status_code)


def test_every_auth_route_refusal_is_byte_identical_through_the_gate(env, apps):
    auth, _, gate = apps
    before_calls = calls(env)
    for method, path in sorted(reg.pairs_of(reg.F2_AUTH)):
        for headers in ({}, {"origin": "https://evil.test", "content-type": "application/json"},
                        {"origin": ORIGIN, "sec-fetch-site": "same-origin", "content-type": "application/json"}):
            content = b"{}" if method == "POST" else None
            direct = request(auth, method, path, headers, content)
            through = request(gate, method, path, headers, content)
            if path.endswith("/csrf/bootstrap") and direct.status_code == 200:
                # Jeton et cookie de liaison aléatoires : mêmes statut, noms d'en-têtes et forme.
                assert through.status_code == 200 and set(through.json()) == set(direct.json())
                assert [k for k, _ in through.headers.multi_items()] == [k for k, _ in direct.headers.multi_items()]
            else:
                assert snapshot(through) == snapshot(direct), (method, path, headers)
    assert calls(env) == before_calls


def test_wrong_methods_and_unknown_f2_paths_keep_their_status_and_body(env, alice, apps):
    auth, personal, gate = apps
    for app, category in ((auth, reg.F2_AUTH), (personal, reg.F2_PERSONAL)):
        templates = {p for _, p in reg.pairs_of(category)}
        for template in sorted(templates):
            allowed = {m for m, p in reg.pairs_of(category) if p == template}
            for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
                if method in allowed:
                    continue
                path = concrete(template)
                direct, through = request(app, method, path), request(gate, method, path)
                assert through.status_code == direct.status_code == 405, (method, path)
                if method != "HEAD":  # HEAD : la porte n'émet aucun corps (le serveur l'élide de toute façon)
                    assert through.content == direct.content == b'{"detail":"Method Not Allowed"}', (method, path)
                else:
                    assert through.content == b""
                for name, value in NO_STORE.items():
                    assert through.headers.get_list(name) == direct.headers.get_list(name) == [value]
                assert through.headers.get_list("set-cookie") == direct.headers.get_list("set-cookie") == []
                assert set(through.headers["allow"].split(", ")) == allowed
        unknown = {reg.F2_AUTH: ("/api/auth/f2/x", "/api/auth/f2/logout/x", "/api/auth/f2/sign-in"),
                   reg.F2_PERSONAL: ("/api/v2/me/x", "/api/v2/me/portfolio/1/x", "/api/v2/me/decryptage")}
        for path in unknown[category]:
            direct, through = request(app, "GET", path), request(gate, "GET", path)
            assert (through.status_code, through.content) == (direct.status_code, direct.content) == (
                404, b'{"detail":"Not Found"}')
            for name, value in NO_STORE.items():
                assert through.headers.get_list(name) == direct.headers.get_list(name) == [value]
    assert env.hook_calls == []


def test_the_full_browser_journey_crosses_the_gate(env, apps):
    _, _, gate = apps
    browser = PersonalBrowser(gate)
    browser.bootstrap()
    _, done = activate(env, browser, env.service.create_invitation(cohort="f-02a").token.reveal())
    generation_id = done["generation_id"]
    assert browser.resolve(generation_id).status_code == 200
    assert browser.me(generation_id).json() == {"error": "identity_link_required"}
    start = link_start(browser, generation_id, CAROL)
    assert start.status_code == 202, start.text
    verify = link_verify(browser, generation_id, start.json()["operation_id"], env.mailer.last_code(CAROL))
    assert (verify.status_code, verify.json()["status"]) == (200, "email_linked")
    me = browser.me(generation_id)
    assert me.status_code == 200, me.text
    level = browser.put_level(generation_id, "avance", me.json()["level_revision"])
    assert level.status_code == 200, level.text
    assert browser.logout(generation_id).status_code == 200
    assert browser.me(generation_id).status_code == 401


def test_portfolio_and_decryptage_mutations_cross_the_gate(env, alice, apps):
    _, _, gate = apps
    created = request(gate, "POST", "/api/v2/me/portfolio",
                      portfolio_headers(alice, operation_id=uuid.uuid4()), position_body())
    assert created.status_code == 201, created.text
    for name, value in NO_STORE.items():
        assert created.headers.get_list(name) == [value]
    level = request(gate, "PUT", ME_LEVEL, level_headers(alice), level_body("avance", "0"))
    assert level.status_code == 200, level.text
    first = request(gate, "POST", TURNS, c3_headers(alice), body_of(turn_fields()))
    assert first.status_code == 200, first.content[:300]
    data = first.json()
    ids = body_of(identity_fields(data["reservation_id"], data["assistant_turn_id"]))
    replayed = request(gate, "POST", REPLAY, c3_headers(alice), ids)
    assert replayed.status_code == 200 and replayed.content == first.content
    acked = request(gate, "POST", ACK, c3_headers(alice), ids)
    assert acked.json() == {"assistant_turn_id": data["assistant_turn_id"], "delivery_status": "delivered"}
    assert env.provider_calls() == ["search", "fundamentals", "realtime", "create_message"]
    assert all(not k.lower().startswith("access-control-") for r in (created, first, acked)
               for k in r.headers.keys())


# --------------------------------------------------------------------------
# 3. Refus sans effet, même avec une session F2 valide
# --------------------------------------------------------------------------

def test_refusals_with_a_valid_f2_session_touch_nothing(env, alice, apps):
    _, _, gate = apps
    env.insert_position(env.owner(alice))
    before, before_calls = durable_state(env), calls(env)
    valid = {**level_headers(alice), **c3_headers(alice)}
    count = 0
    for path in RETIRED_PATHS:
        for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"):
            response = request(gate, method, path, valid, b'{"level":"avance"}' if method != "HEAD" else None)
            assert response.status_code == 410, (method, path)
            assert response.content == (b"" if method == "HEAD" else b'{"error":"legacy_personal_api_retired"}')
            assert response.headers.get_list("set-cookie") == []
            count += 1
    for path in ("/web", "/analyze", "/analyze-etf", "/pedagogie/lookup", "/web-v2", "/web-v2/search",
                 "/web-v2/fx-rate", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/inconnu"):
        for method in ("GET", "POST", "HEAD"):
            response = request(gate, method, path, valid)
            assert response.status_code == 404 and response.headers.get_list("set-cookie") == []
            count += 1
    for template in sorted({p for _, p in reg.pairs_of(reg.F2_AUTH, reg.F2_PERSONAL)}):
        for raw in _variants(concrete(template)):
            # Appel ASGI brut : un client HTTP normaliserait lui-même « /./ » ou « /../ ».
            response = raw_request(gate, method="POST", path=raw.decode("latin-1"), headers=list(valid.items()),
                                   chunks=[b"{}"])
            assert response.status_code in (404, 410), raw
            assert response.get_list("set-cookie") == []
            count += 1
    assert count > 200
    untouched(env, before, before_calls)


def test_cors_preflights_never_reach_f2_and_never_answer_cors(env, alice, apps):
    _, _, gate = apps
    before, before_calls = durable_state(env), calls(env)
    for method, path in sorted(reg.pairs_of(reg.F2_AUTH, reg.F2_PERSONAL)):
        response = request(gate, "OPTIONS", concrete(path), {
            "origin": "https://evil.test", "access-control-request-method": method,
            "access-control-request-headers": "content-type,x-oryx-csrf"})
        assert response.status_code == 405
        assert not [k for k in response.headers.keys() if k.lower().startswith("access-control-")]
    untouched(env, before, before_calls)
