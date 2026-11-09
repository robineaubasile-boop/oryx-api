"""F-02A V0.2 — politique de routage fermée (tests sans base de données).

1. inventaire RÉEL de api:app (routes déclarées ET automatiques FastAPI,
   chaque méthode) == registre legacy ; matrice figée indépendante ;
2. décision pour chaque gabarit et chaque méthode (y compris HEAD, OPTIONS,
   méthodes inattendues, casse) ;
3. contournements HTTP : chemins non canoniques, encodage-pourcent, double
   encodage, traversée, barres obliques, casse, root_path, path incohérent,
   chemin absolu, octets de contrôle ou non ASCII ;
4. contrat de refus : statut, corps exact, en-têtes no-store, aucun cookie,
   aucune redirection, aucun CORS, corps vide pour HEAD, aucun écho ;
5. aucun appel d'application, aucune lecture du corps, aucune base, aucun
   réseau, aucun fournisseur pendant les refus (gardes actives et
   sous-processus surveillant les modules chargés) ;
6. composition : api:app et toute application hors politique refusées.
"""
import asyncio
import json
import subprocess
import sys

import pytest

from core.route_policy import gate as g
from core.route_policy import registry as reg
from core.route_policy.canonical import MAX_PATH_BYTES, canonical_segments
from tests.test_authz_f2_http import NO_STORE
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

# --------------------------------------------------------------------------
# Matrice figée (écrite indépendamment du registre)
# --------------------------------------------------------------------------

A = "A"; B = "B"; C_KEEP = "C-health"; C_F05 = "C-web-v2"; C_AUDIT = "C-audit"; D_AUTH = "D-auth"  # noqa: E702
D_ME = "D-me"; DOCS = "docs"  # noqa: E702
FROZEN = {
    ("GET", "/api/user/{user_id}"): A, ("PUT", "/api/user/{user_id}/level"): A,
    ("GET", "/api/user/{user_id}/portfolio"): A, ("POST", "/api/user/{user_id}/portfolio"): A,
    ("DELETE", "/api/user/{user_id}/portfolio/{position_id}"): A,
    ("PUT", "/api/user/{user_id}/portfolio/{position_id}"): A, ("GET", "/api/user/{user_id}/theses"): A,
    ("DELETE", "/api/user/{user_id}/theses/{ticker}"): A, ("POST", "/decryptage"): A,
    ("POST", "/api/runtime/assistant-deliveries/{assistant_turn_id}/ack"): A, ("POST", "/web-chat"): A,
    ("POST", "/web-v2/portfolio-analyze"): A, ("POST", "/education"): A, ("POST", "/coach"): A,
    ("POST", "/checklist"): A,
    ("GET", "/web"): B, ("POST", "/analyze"): B, ("POST", "/analyze-etf"): B, ("POST", "/pedagogie/lookup"): B,
    ("GET", "/health"): C_KEEP, ("GET", "/web-v2"): C_F05, ("GET", "/web-v2/search"): C_AUDIT,
    ("GET", "/web-v2/fx-rate"): C_AUDIT,
    **{(m, p): DOCS for p in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc") for m in ("GET", "HEAD")},
    ("GET", "/api/auth/f2/csrf/bootstrap"): D_AUTH,
    **{("POST", "/api/auth/f2/" + s): D_AUTH for s in (
        "sign-in/start", "sign-in/resend", "sign-in/verify", "sign-in/resume", "session/resolve", "logout",
        "invitation/begin", "invitation/complete", "invitation/resume", "email-link/start", "email-link/resend",
        "email-link/verify", "email-link/result")},
    ("GET", "/api/v2/me"): D_ME, ("PUT", "/api/v2/me/level"): D_ME, ("GET", "/api/v2/me/portfolio"): D_ME,
    ("POST", "/api/v2/me/portfolio"): D_ME, ("GET", "/api/v2/me/portfolio/{position_id}"): D_ME,
    ("PUT", "/api/v2/me/portfolio/{position_id}"): D_ME, ("DELETE", "/api/v2/me/portfolio/{position_id}"): D_ME,
    ("GET", "/api/v2/me/analysis-overview"): D_ME, ("GET", "/api/v2/me/theses"): D_ME,
    ("GET", "/api/v2/me/theses/{thesis_id}"): D_ME, ("POST", "/api/v2/me/decryptage/turns"): D_ME,
    ("POST", "/api/v2/me/decryptage/replay"): D_ME, ("POST", "/api/v2/me/decryptage/ack"): D_ME,
}
CATEGORY_OF = {A: reg.LEGACY_RETIRED, B: reg.DISABLED, C_KEEP: reg.PUBLIC_MAINTAINED, C_F05: reg.PUBLIC_PENDING_F05,
               C_AUDIT: reg.PUBLIC_PENDING_AUDIT, D_AUTH: reg.F2_AUTH, D_ME: reg.F2_PERSONAL,
               DOCS: reg.FRAMEWORK_DOCS}
METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT", "PROPFIND", "get", "Post",
           "FOO", "", None)
PARAMS = {"user_id": "42", "position_id": "7", "ticker": "AIR.PA", "assistant_turn_id":
          "11111111-2222-4333-8444-555555555555", "thesis_id": "3"}


def concrete(template):
    out = template
    for name, value in PARAMS.items():
        out = out.replace("{" + name + "}", value)
    return out


TEMPLATES = sorted({p for _, p in FROZEN})
RETIRED_PATHS = sorted({concrete(p) for (m, p), c in FROZEN.items() if c == A})


def expected_action(category, method, methods):
    if category == A:
        return g.RETIRED
    if category in (B, C_F05, C_AUDIT, DOCS):
        return g.NOT_FOUND
    if method not in methods:
        return g.METHOD_NOT_ALLOWED
    return {C_KEEP: g.HEALTH, D_AUTH: g.FORWARD_F2_AUTH, D_ME: g.FORWARD_F2_PERSONAL}[category]


# --------------------------------------------------------------------------
# Pilote ASGI brut et applications sentinelles
# --------------------------------------------------------------------------

class Sent:
    def __init__(self, messages, received):
        self.messages = messages
        self.received = received
        start = next(m for m in messages if m["type"] == "http.response.start")
        self.status = start["status"]
        self.headers = [(bytes(k).decode("latin-1"), bytes(v).decode("latin-1")) for k, v in start["headers"]]
        self.body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")

    def get_list(self, name):
        return [v for k, v in self.headers if k.lower() == name]


def call(app, method, raw_path, *, path=None, headers=(), body=b"", query=b"", root_path="", scope_extra=None, drop=()):
    async def run():
        received = []

        async def receive():
            received.append(1)
            return {"type": "http.request", "body": body, "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "https", "path": path if path is not None else raw_path.decode("latin-1"),
                 "raw_path": raw_path, "query_string": query, "root_path": root_path,
                 "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers],
                 "client": ("192.0.2.1", 1), "server": ("oryx.test", 443)}
        if scope_extra:
            scope.update(scope_extra)
        for key in drop:
            del scope[key]
        await app(scope, receive, send)
        return Sent(sent, received)

    return asyncio.run(run())


class _Route:
    def __init__(self, method, path):
        self.methods = {method}
        self.path = path


class Sentinel:
    """Application ASGI inspectable exposant exactement des routes F2 ;
    enregistre chaque appel et répond 299."""

    openapi_url = docs_url = redoc_url = None
    user_middleware = ()

    def __init__(self, category, extra_headers=()):
        self.routes = [_Route(m, p) for m, p in sorted(reg.pairs_of(category))]
        self.calls = []
        self.extra_headers = list(extra_headers)

    async def __call__(self, scope, receive, send):
        self.calls.append((scope["type"], scope.get("method"), scope.get("path"), scope.get("raw_path")))
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 299,
                    "headers": [(b"content-type", b"text/plain"), (b"set-cookie", b"f2=1")] + self.extra_headers})
        await send({"type": "http.response.body", "body": b"downstream"})


def sentinel_gate(extra_headers=()):
    auth, personal = Sentinel(reg.F2_AUTH, extra_headers), Sentinel(reg.F2_PERSONAL, extra_headers)
    return g.RoutePolicyGate(f2_auth_app=auth, f2_personal_app=personal), auth, personal


def assert_gate_response(response, status, body, method="GET"):
    assert response.status == status, (status, response.status)
    assert response.body == (b"" if method == "HEAD" else body)
    assert response.get_list("content-type") == ["application/json"]
    assert response.get_list("content-length") == [str(len(body))]
    for name, value in NO_STORE.items():
        assert response.get_list(name) == [value], name
    assert response.get_list("set-cookie") == []
    assert response.get_list("location") == []
    assert not [k for k, _ in response.headers if k.lower().startswith("access-control-")]
    assert response.received == []  # corps de la requête jamais lu


# --------------------------------------------------------------------------
# 1. Inventaire et matrice figée
# --------------------------------------------------------------------------

INVENTORY = r'''
import json, socket
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(OSError("réseau interdit"))
import api
pairs = sorted({(m, r.path) for r in api.app.routes for m in r.methods})
print(json.dumps({"pairs": pairs, "kinds": sorted({type(r).__name__ for r in api.app.routes}),
                  "middleware": [m.cls.__name__ for m in api.app.user_middleware],
                  "docs": [api.app.openapi_url, api.app.docs_url, api.app.redoc_url,
                           api.app.swagger_ui_oauth2_redirect_url]}))
'''


def test_the_real_api_inventory_is_exactly_the_legacy_registry():
    out = subprocess.run([sys.executable, "-c", INVENTORY], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({}), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    real = {tuple(p) for p in result["pairs"]}
    assert real == reg.legacy_inventory()
    assert len(real) == 31  # 23 couples déclarés + 8 couples automatiques (GET/HEAD documentation)
    # Toute route de api:app est une Route / APIRoute avec méthodes (aucun Mount, aucune websocket).
    assert result["kinds"] == ["APIRoute", "Route"]
    assert result["middleware"] == ["CORSMiddleware"]
    assert result["docs"] == ["/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"]


def test_the_registry_is_exactly_the_frozen_policy():
    actual = {(r.method, r.template): r.category for r in reg.RULES}
    assert actual == {key: CATEGORY_OF[c] for key, c in FROZEN.items()}
    assert len(reg.RULES) == len(FROZEN) == 58
    counts = {c: sum(1 for v in FROZEN.values() if v == c) for c in set(FROZEN.values())}
    assert counts == {A: 15, B: 4, C_KEEP: 1, C_F05: 1, C_AUDIT: 2, D_AUTH: 14, D_ME: 13, DOCS: 8}
    assert sum(1 for (m, p) in reg.legacy_inventory() if not p.startswith(("/docs", "/redoc", "/openapi"))) == 23
    assert reg.POLICY_VERSION == "F-02A-V0.2"
    assert len([p for (_, p), c in FROZEN.items() if c == A and p.startswith("/api/user/")]) == 8


def test_registry_sources_are_exact():
    for rule in reg.RULES:
        expected = {A: reg.SOURCE_API, B: reg.SOURCE_API, C_KEEP: reg.SOURCE_API, C_F05: reg.SOURCE_API,
                    C_AUDIT: reg.SOURCE_API, DOCS: reg.SOURCE_FRAMEWORK, D_AUTH: reg.SOURCE_F2_AUTH,
                    D_ME: reg.SOURCE_F2_PERSONAL}[FROZEN[(rule.method, rule.template)]]
        assert rule.source == expected, rule


# --------------------------------------------------------------------------
# 2. Décision pour chaque gabarit et chaque méthode
# --------------------------------------------------------------------------

@pytest.mark.parametrize("template", TEMPLATES)
def test_every_template_and_every_method_has_its_frozen_decision(template):
    methods = {m for (m, p) in FROZEN if p == template}
    [category] = {FROZEN[(m, template)] for m in methods}
    raw = concrete(template).encode("ascii")
    for method in METHODS + tuple(sorted(methods)):
        decision = g.decide(method, raw)
        assert decision.action == expected_action(category, method, methods), (method, template, decision)
        if decision.action != g.NOT_FOUND:
            assert decision.template == template
        if decision.action == g.METHOD_NOT_ALLOWED:
            assert decision.allow == tuple(sorted(methods))


@pytest.mark.parametrize("path", RETIRED_PATHS)
def test_a_retired_path_is_410_whatever_the_method(path):
    gate, auth, personal = sentinel_gate()
    for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "PROPFIND", "get", "FOO"):
        response = call(gate, method, path.encode(), headers=[("cookie", "session=alice"),
                                                             ("origin", "https://evil.test"),
                                                             ("access-control-request-method", "POST")],
                        body=b'{"user_id":"alice@example.com"}')
        assert_gate_response(response, 410, b'{"error":"legacy_personal_api_retired"}', method)
        if method != "HEAD":
            assert json.loads(response.body) == {"error": "legacy_personal_api_retired"}
    assert auth.calls == personal.calls == []


def test_retired_parameters_accept_any_canonical_segment():
    for raw in (b"/api/user/alice", b"/api/user/0", b"/api/user/user-a_b.c~d", b"/api/user/1/theses/BRK.B",
                b"/api/runtime/assistant-deliveries/not-a-uuid/ack", b"/api/user/ABC/portfolio/x"):
        assert g.decide("GET", raw).action == g.RETIRED, raw


def test_disabled_pending_and_documentation_routes_are_404_whatever_the_method():
    gate, auth, personal = sentinel_gate()
    for template in TEMPLATES:
        category = FROZEN[next(k for k in FROZEN if k[1] == template)]
        if category not in (B, C_F05, C_AUDIT, DOCS):
            continue
        for method in ("GET", "HEAD", "POST", "OPTIONS", "DELETE", "FOO"):
            assert_gate_response(call(gate, method, template.encode()), 404, b'{"detail":"Not Found"}', method)
    assert auth.calls == personal.calls == []


def test_health_is_served_by_the_gate_itself():
    gate, auth, personal = sentinel_gate()
    response = call(gate, "GET", b"/health", query=b"x=1")
    assert_gate_response(response, 200, b'{"status":"ok"}')
    assert json.loads(response.body) == {"status": "ok"}  # contrat historique de GET /health
    for method in ("HEAD", "POST", "OPTIONS", "PUT", "DELETE", "get"):
        response = call(gate, method, b"/health", headers=[("origin", "https://evil.test"),
                                                           ("access-control-request-method", "GET")])
        assert_gate_response(response, 405, b'{"detail":"Method Not Allowed"}', method)
        assert response.get_list("allow") == ["GET"]
    assert auth.calls == personal.calls == []


def test_f2_routes_are_forwarded_unchanged_and_wrong_methods_are_405_at_the_gate():
    gate, auth, personal = sentinel_gate()
    for (method, template), category in FROZEN.items():
        if category not in (D_AUTH, D_ME):
            continue
        target = auth if category == D_AUTH else personal
        raw = concrete(template).encode()
        before = len(target.calls)
        response = call(gate, method, raw, query=b"limit=1")
        assert (response.status, response.body) == (299, b"downstream")
        assert response.get_list("set-cookie") == ["f2=1"]  # réponse F2 inchangée
        assert target.calls[before:] == [("http", method, raw.decode(), raw)]
    auth_calls, personal_calls = len(auth.calls), len(personal.calls)
    for template in sorted({p for (m, p), c in FROZEN.items() if c in (D_AUTH, D_ME)}):
        allowed = sorted(m for (m, p) in FROZEN if p == template)
        for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "FOO"):
            if method in allowed:
                continue
            response = call(gate, method, concrete(template).encode(),
                            headers=[("origin", "https://evil.test"), ("access-control-request-method", "POST")])
            assert_gate_response(response, 405, b'{"detail":"Method Not Allowed"}', method)
            assert response.get_list("allow") == [", ".join(allowed)]
    assert (len(auth.calls), len(personal.calls)) == (auth_calls, personal_calls)


def test_uncomposed_f2_applications_answer_404():
    gate = g.RoutePolicyGate(f2_auth_app=None, f2_personal_app=None)
    for (method, template), category in FROZEN.items():
        if category in (D_AUTH, D_ME):
            assert_gate_response(call(gate, method, concrete(template).encode()), 404, b'{"detail":"Not Found"}')


# --------------------------------------------------------------------------
# 3. Contournements HTTP
# --------------------------------------------------------------------------

def _variants(path: str):
    raw = path.encode()
    head, _, tail = raw.rpartition(b"/")
    yield raw + b"/"
    yield b"/" + raw
    yield raw.replace(b"/", b"//", 1)
    yield raw.upper()
    yield raw[:1] + raw[1:2].upper() + raw[2:]
    yield head + b"/./" + tail
    yield head + b"/x/../" + tail
    yield raw + b"/."
    yield raw + b"/.."
    yield raw.replace(b"/", b"\\")
    yield raw.replace(b"/", b"%2F")
    yield raw.replace(b"/", b"%2f")
    yield raw.replace(b"/", b"%252F")
    yield head + b"/" + b"".join(b"%%%02X" % c for c in tail)
    yield head + b"/" + b"".join(b"%%%02x" % c for c in tail)
    yield head + b"/%25" + b"".join(b"%02X" % c for c in tail[:1]) + tail[1:]
    yield raw + b";jsessionid=1"
    yield raw + b";"
    yield raw + b"?x=1"
    yield raw + b"#frag"
    yield raw + b"%00"
    yield raw + b"\x00"
    yield raw + b" "
    yield raw + b"\t"
    yield raw + b"%20"
    yield raw + b"+"
    yield raw + "é".encode()
    yield raw + b"\xff"
    yield raw[1:]
    yield b"https://oryx.test" + raw
    yield b"//oryx.test" + raw
    yield b"/" + b"a" * MAX_PATH_BYTES + raw


ALL_CONCRETE = sorted({concrete(p) for p in TEMPLATES})


@pytest.mark.parametrize("path", ALL_CONCRETE)
def test_non_canonical_variants_are_never_routed(path):
    gate, auth, personal = sentinel_gate()
    for raw in _variants(path):
        decision = g.decide("GET", raw)
        assert decision.action in (g.NOT_FOUND, g.RETIRED), (raw, decision)
        if decision.action == g.RETIRED:  # seule une variante canonique d'un autre chemin retiré
            assert canonical_segments(raw) is not None, raw
        for method in ("GET", "POST", "HEAD"):
            response = call(gate, method, raw, path=raw.decode("latin-1"))
            assert response.status in (404, 410), (raw, response.status)
            assert response.get_list("set-cookie") == [] and response.get_list("location") == []
    assert auth.calls == personal.calls == []


def test_non_canonical_retired_paths_are_404_not_transformed():
    for raw in (b"/api/user/42/", b"/api/user/%34%32", b"/API/user/42", b"/api//user/42", b"/api/user/42/.",
                b"/decryptage/", b"/Decryptage", b"/decryptage%2F", b"/api/user/42%2Ftheses", b"/web-chat;x"):
        decision = g.decide("POST", raw)
        assert decision.action == g.NOT_FOUND and decision.reason in ("non_canonical", "unknown"), raw


def test_traversal_and_double_encoding_never_reach_an_allowed_route():
    targets = (b"/health", b"/api/v2/me", b"/api/auth/f2/logout", b"/api/v2/me/portfolio/1")
    attempts = []
    for target in targets:
        attempts += [b"/api/user/1/.." + target, b"/web/.." + target, b"/x/%2e%2e" + target,
                     b"/x/%252e%252e" + target, b"/x/..%2F" + target[1:], b"/." + target, b"/%2e" + target,
                     target.replace(b"/", b"%2F"), target.replace(b"/", b"%252F"), target + b"%23",
                     target + b"%3F", target + b"%00"]
    for raw in attempts:
        decision = g.decide("GET", raw)
        assert decision.action == g.NOT_FOUND, (raw, decision)


def test_unknown_paths_are_404():
    assert g.decide("GET", b"/").action == g.NOT_FOUND  # racine : segment vide, non canonique
    for raw in (b"/healthz", b"/health.json", b"/api", b"/api/user", b"/api/v2", b"/api/v2/me/x",
                b"/api/v2/me/portfolio/1/x", b"/api/user/1/portfolio/2/3", b"/api/auth/f2", b"/api/auth/f2/logout/x",
                b"/web-v2/x", b"/static/app.js", b"/index.html", b"/favicon.ico", b"/docs/x", b"/openapi.yaml",
                b"/api/v2/me/decryptage", b"/api/runtime/assistant-deliveries/1"):
        assert g.decide("GET", raw) == g.Decision(g.NOT_FOUND, "unknown"), raw


def test_scope_inconsistencies_are_404():
    gate, auth, personal = sentinel_gate()
    cases = [
        dict(raw_path=b"/api/v2/me", root_path="/prefix"),
        dict(raw_path=b"/health", root_path="/"),
        dict(raw_path=b"/api/v2/me", path="/api/v2/me/"),
        dict(raw_path=b"/api/v2/me", path="/health"),
        dict(raw_path=b"/health", path="/api/v2/me"),
        dict(raw_path=b"/api/user/1", path="/api/v2/me"),
    ]
    for case in cases:
        response = call(gate, "GET", case["raw_path"], path=case.get("path"), root_path=case.get("root_path", ""))
        assert_gate_response(response, 404, b'{"detail":"Not Found"}')
    for raw in (None, "/api/v2/me", bytearray(b"/api/v2/me"), memoryview(b"/health")):
        response = call(gate, "GET", b"/api/v2/me", path="/api/v2/me", scope_extra={"raw_path": raw})
        assert_gate_response(response, 404, b'{"detail":"Not Found"}')
    for key in ("raw_path", "path"):
        assert_gate_response(call(gate, "GET", b"/health", drop=(key,)), 404, b'{"detail":"Not Found"}')
    assert auth.calls == personal.calls == []


def test_query_strings_never_change_the_decision():
    gate, auth, personal = sentinel_gate()
    for query in (b"path=/api/v2/me", b"redirect=/api/v2/me", b"_method=GET", b"%2F%2E%2E", b"x=" + b"a" * 5000):
        assert call(gate, "POST", b"/decryptage", query=query).status == 410
        assert call(gate, "GET", b"/web", query=query).status == 404
        assert call(gate, "GET", b"/health", query=query).status == 200
    assert auth.calls == personal.calls == []


def test_method_override_headers_are_ignored():
    gate, auth, personal = sentinel_gate()
    for header in ("x-http-method-override", "x-http-method", "x-method-override"):
        response = call(gate, "POST", b"/health", headers=[(header, "GET")])
        assert response.status == 405
        response = call(gate, "GET", b"/api/user/1", headers=[(header, "OPTIONS")])
        assert response.status == 410
    assert auth.calls == personal.calls == []


# --------------------------------------------------------------------------
# 4. Contrat de refus
# --------------------------------------------------------------------------

PERSONAL = "alice.martin@example.com"


def test_refusals_never_echo_request_data_nor_set_cookies():
    gate, auth, personal = sentinel_gate()
    headers = [("cookie", f"__Host-oryx-active-x=SECRET; email={PERSONAL}"), ("authorization", "Bearer SECRET"),
               ("x-user-id", PERSONAL), ("origin", "https://evil.test")]
    for method, raw in [("GET", b"/api/user/alice.martin"), ("PUT", b"/api/user/alice.martin/level"),
                        ("POST", b"/web-chat"), ("POST", b"/coach"), ("POST", b"/analyze"),
                        ("GET", b"/web-v2/search"), ("GET", b"/nope")]:
        response = call(gate, method, raw, headers=headers, query=f"email={PERSONAL}".encode(),
                        body=json.dumps({"email": PERSONAL, "portfolio": [1, 2]}).encode())
        dump = repr(response.headers).encode() + response.body
        for needle in (b"alice", b"SECRET", b"example.com", b"portfolio", b"evil"):
            assert needle not in dump, (raw, needle)
        assert response.get_list("set-cookie") == []
        assert response.get_list("vary") == []
        assert response.received == []
    assert auth.calls == personal.calls == []


def test_the_gate_never_redirects():
    gate, _, _ = sentinel_gate()
    for path in ALL_CONCRETE + ["/", "/web/", "/web-v2/", "/docs/", "/health/", "/api/v2/me/"]:
        for method in ("GET", "HEAD", "POST", "OPTIONS"):
            response = call(gate, method, path.encode())
            assert not 300 <= response.status < 400
            assert response.get_list("location") == []


def test_cors_is_never_emitted_nor_inherited():
    gate, auth, personal = sentinel_gate(extra_headers=[(b"access-control-allow-origin", b"*"),
                                                        (b"Access-Control-Allow-Credentials", b"true"),
                                                        (b"x-kept", b"1")])
    preflight = [("origin", "https://evil.test"), ("access-control-request-method", "POST"),
                 ("access-control-request-headers", "content-type")]
    for path in ALL_CONCRETE:
        response = call(gate, "OPTIONS", path.encode(), headers=preflight)
        assert response.status in (404, 405, 410), path
        assert not [k for k, _ in response.headers if k.lower().startswith("access-control-")]
    forwarded = call(gate, "GET", b"/api/v2/me", headers=preflight)
    assert forwarded.status == 299
    assert not [k for k, _ in forwarded.headers if k.lower().startswith("access-control-")]
    assert forwarded.get_list("x-kept") == ["1"] and forwarded.get_list("set-cookie") == ["f2=1"]
    assert [c[1] for c in personal.calls] == ["GET"] and auth.calls == []


def test_websocket_and_lifespan_are_handled_by_the_gate_alone():
    gate, auth, personal = sentinel_gate()

    async def run():
        sent = []

        async def send(message):
            sent.append(message)

        async def ws_receive():
            return {"type": "websocket.connect"}

        await gate({"type": "websocket", "path": "/api/v2/me", "raw_path": b"/api/v2/me", "root_path": "",
                    "headers": [], "query_string": b""}, ws_receive, send)
        messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

        async def ls_receive():
            return messages.pop(0)

        await gate({"type": "lifespan", "asgi": {"version": "3.0"}}, ls_receive, send)
        await gate({"type": "unknown"}, ws_receive, send)
        return sent

    assert asyncio.run(run()) == [{"type": "websocket.close", "code": 1008}, {"type": "lifespan.startup.complete"},
                                  {"type": "lifespan.shutdown.complete"}]
    assert auth.calls == personal.calls == []


# --------------------------------------------------------------------------
# 5. Isolement des refus (gardes actives)
# --------------------------------------------------------------------------

def _all_refusal_requests():
    for path in ALL_CONCRETE:
        for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "FOO"):
            decision = g.decide(method, path.encode())
            if decision.action not in (g.FORWARD_F2_AUTH, g.FORWARD_F2_PERSONAL, g.HEALTH):
                yield method, path.encode()
        for raw in _variants(path):
            yield "GET", raw


def test_refusals_touch_no_database_network_provider_or_application(monkeypatch):
    import socket

    import fastapi
    import psycopg2
    import sqlalchemy
    import starlette.applications
    import starlette.routing

    touched = []

    def forbid(name):
        def guard(*args, **kwargs):
            touched.append(name)
            raise AssertionError(f"{name} appelé pendant un refus")
        return guard

    monkeypatch.setattr(socket.socket, "connect", forbid("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", forbid("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", forbid("socket.getaddrinfo"))
    monkeypatch.setattr(psycopg2, "connect", forbid("psycopg2.connect"))
    monkeypatch.setattr(sqlalchemy.engine.Engine, "connect", forbid("Engine.connect"))
    monkeypatch.setattr(sqlalchemy, "create_engine", forbid("create_engine"))
    monkeypatch.setattr(fastapi.FastAPI, "__call__", forbid("FastAPI.__call__"))
    monkeypatch.setattr(starlette.applications.Starlette, "__call__", forbid("Starlette.__call__"))
    monkeypatch.setattr(starlette.routing.Router, "__call__", forbid("Router.__call__"))
    for module, attr in (("anthropic", "Anthropic"), ("requests", "Session")):
        mod = pytest.importorskip(module)
        monkeypatch.setattr(getattr(mod, attr), "__init__", forbid(f"{module}.{attr}"))
    gate, auth, personal = sentinel_gate()
    count = 0
    for method, raw in _all_refusal_requests():
        response = call(gate, method, raw, path=raw.decode("latin-1"), headers=[("cookie", "a=b")], body=b"{}")
        assert response.status in (404, 405, 410)
        count += 1
    assert count > 1500
    assert touched == [] and auth.calls == personal.calls == []


ISOLATION = r'''
import asyncio, json, socket, sys
attempts = []
def guard(*a, **k):
    attempts.append(repr(a[1:2]))
    raise OSError("réseau interdit")
socket.socket.connect = guard
socket.create_connection = guard
from core.route_policy import gate as g, registry as reg
out = []
async def run():
    gate = g.RoutePolicyGate(f2_auth_app=None, f2_personal_app=None)
    for rule in reg.RULES:
        for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "FOO"):
            path = rule.template.replace("{", "").replace("}", "")
            sent = []
            async def send(m): sent.append(m)
            async def receive(): raise AssertionError("corps lu")
            await gate({"type": "http", "method": method, "path": path, "raw_path": path.encode(), "root_path": "",
                        "query_string": b"", "headers": [(b"cookie", b"a=b")]}, receive, send)
            out.append([method, path, sent[0]["status"]])
asyncio.run(run())
WATCHED = ("api", "fastapi", "starlette", "pydantic", "sqlalchemy", "psycopg2", "anthropic", "requests", "httpx",
           "yfinance", "dotenv", "core.db", "core.models", "core.auth_f2", "core.auth_f2_http", "core.authz_f2",
           "core.authz_f2_http", "core.market_lookup", "core.data_fetcher", "core.decryptage_engine")
loaded = sorted(m for m in sys.modules if any(m == w or m.startswith(w + ".") for w in WATCHED))
mine = sorted(m for m in sys.modules if m.startswith("core."))
print(json.dumps({"out": out, "loaded": loaded, "core": mine, "attempts": attempts}))
'''


def test_the_policy_package_loads_no_application_framework_database_or_provider():
    out = subprocess.run([sys.executable, "-c", ISOLATION], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({}), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["loaded"] == [] and result["attempts"] == []
    assert result["core"] == ["core.route_policy", "core.route_policy.canonical", "core.route_policy.gate",
                              "core.route_policy.registry"]
    statuses = {}
    for method, path, status in result["out"]:
        statuses.setdefault(path, set()).add(status)
    assert len(result["out"]) == 7 * 58
    for (method, template), category in FROZEN.items():
        path = template.replace("{", "").replace("}", "")
        allowed = {A: {410}, B: {404}, C_F05: {404}, C_AUDIT: {404}, DOCS: {404}, C_KEEP: {200, 405},
                   D_AUTH: {404, 405}, D_ME: {404, 405}}[category]
        assert statuses[path] <= allowed, (path, statuses[path])


# --------------------------------------------------------------------------
# 6. Composition
# --------------------------------------------------------------------------

def test_the_legacy_application_can_never_be_composed():
    import api
    for kwargs in ({"f2_auth_app": api.app, "f2_personal_app": None},
                   {"f2_auth_app": None, "f2_personal_app": api.app}):
        with pytest.raises(g.RouteGateConfigError):
            g.RoutePolicyGate(**kwargs)


def test_out_of_policy_applications_are_refused():
    from fastapi import APIRouter, FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    def app_with(paths, *, docs=False, cors=False):
        app = FastAPI(**({} if docs else {"openapi_url": None, "docs_url": None, "redoc_url": None}))
        router = APIRouter()
        for method, path in paths:
            router.add_api_route(path, lambda: None, methods=[method], include_in_schema=False)
        app.include_router(router)
        if cors:
            app.add_middleware(CORSMiddleware, allow_origins=["*"])
        return app

    me = [("GET", "/api/v2/me")]
    assert g.RoutePolicyGate(f2_auth_app=None, f2_personal_app=app_with(me))
    refused = [
        dict(f2_auth_app=None, f2_personal_app=app_with(me + [("GET", "/health")])),
        dict(f2_auth_app=None, f2_personal_app=app_with(me + [("GET", "/api/user/{user_id}")])),
        dict(f2_auth_app=None, f2_personal_app=app_with(me + [("POST", "/api/v2/me")])),
        dict(f2_auth_app=None, f2_personal_app=app_with([("GET", "/api/v2/me/")])),
        dict(f2_auth_app=None, f2_personal_app=app_with(me, docs=True)),
        dict(f2_auth_app=None, f2_personal_app=app_with(me, cors=True)),
        dict(f2_auth_app=app_with(me), f2_personal_app=None),  # mauvaise catégorie
        dict(f2_auth_app=None, f2_personal_app=app_with([])),
        dict(f2_auth_app=None, f2_personal_app=lambda scope, receive, send: None),  # non inspectable
        dict(f2_auth_app=None, f2_personal_app=object()),
    ]
    mounted = app_with(me)
    mounted.mount("/api/v2/legacy", app_with([("GET", "/x")]))
    refused.append(dict(f2_auth_app=None, f2_personal_app=mounted))
    same = Sentinel(reg.F2_PERSONAL)
    refused.append(dict(f2_auth_app=same, f2_personal_app=same))
    for kwargs in refused:
        with pytest.raises(g.RouteGateConfigError):
            g.RoutePolicyGate(**kwargs)
    with pytest.raises(TypeError):
        g.RoutePolicyGate()  # interface explicite : aucun défaut
    with pytest.raises(TypeError):
        g.RoutePolicyGate(None, None)


def test_canonical_form_unit_cases():
    assert canonical_segments(b"/api/v2/me") == ("api", "v2", "me")
    assert canonical_segments(b"/a.b-c_d~e/0") == ("a.b-c_d~e", "0")
    assert canonical_segments(b"/" + b"a" * (MAX_PATH_BYTES - 1)) is not None
    for raw in (b"", b"/", b"//", b"a", b"/a/", b"/a//b", b"/.", b"/..", b"/a/./b", b"/a/../b", b"/%41", b"/a b",
                b"/a;b", b"/a?b", b"/a#b", b"/a\\b", b"/a+b", b"/a:b", b"/a@b", b"/a!b", b"/a*b", b"/a,b", b"/a=b",
                b"/a&b", b"/a'b", b"/a(b", b"/a$b", b"/\x7f", b"/\x80", b"/" + b"a" * MAX_PATH_BYTES, "/a", None,
                bytearray(b"/a"), 1):
        assert canonical_segments(raw) is None, raw


def test_gate_responses_use_the_f2_no_store_headers():
    assert dict(g.NO_STORE_HEADERS) == NO_STORE
    from core.auth_f2_http.routes import NO_STORE_HEADERS
    assert dict(g.NO_STORE_HEADERS) == NO_STORE_HEADERS
    assert json.loads(g.RETIRED_BODY) == {"error": g.RETIRED_CODE} == {"error": "legacy_personal_api_retired"}
    assert g.RETIRED_STATUS == 410


# --------------------------------------------------------------------------
# 7. Vrai serveur uvicorn (h11), requêtes HTTP brutes sur la boucle locale
# --------------------------------------------------------------------------

def _serve(app):
    import socket
    import threading

    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="critical", http="h11", ws="none",
                                           proxy_headers=False, access_log=False))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        threading.Event().wait(0.01)
    assert server.started
    return server, thread, sock.getsockname()[1]


def _raw_http(port, request_line, extra=b""):
    import socket
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        conn.sendall(request_line + b"\r\nHost: oryx.test\r\nConnection: close\r\n" + extra + b"\r\n")
        data = b""
        while chunk := conn.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = [tuple(x.strip().lower() for x in line.split(b":", 1)) for line in lines[1:]]
    if (b"transfer-encoding", b"chunked") in headers:
        decoded = b""
        while True:
            size, _, rest = body.partition(b"\r\n")
            if int(size, 16) == 0:
                break
            decoded, body = decoded + rest[:int(size, 16)], rest[int(size, 16) + 2:]
        body = decoded
    return status, headers, body


def test_a_real_uvicorn_server_applies_the_same_decisions():
    gate, auth, personal = sentinel_gate()
    server, thread, port = _serve(gate)
    try:
        cases = [
            (b"GET /api/user/42 HTTP/1.1", 410, b'{"error":"legacy_personal_api_retired"}'),
            (b"HEAD /api/user/42 HTTP/1.1", 410, b""),
            (b"OPTIONS /api/user/42 HTTP/1.1", 410, b'{"error":"legacy_personal_api_retired"}'),
            (b"PROPFIND /decryptage HTTP/1.1", 410, b'{"error":"legacy_personal_api_retired"}'),
            (b"POST /web-chat?session=1 HTTP/1.1", 410, b'{"error":"legacy_personal_api_retired"}'),
            (b"GET /api/user/%34%32 HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /api/user/42/ HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /api/v2/me/%2e%2e/me HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /api/v2/me/../me HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /api%2Fv2%2Fme HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /api%252Fv2%252Fme HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET //api/v2/me HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /api/v2/me;x=1 HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /API/V2/ME HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET http://oryx.test/health HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"OPTIONS * HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /web HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /docs HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /openapi.json HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"GET /web-v2/search?q=air HTTP/1.1", 404, b'{"detail":"Not Found"}'),
            (b"HEAD /health HTTP/1.1", 405, b""),
            (b"GET /health?x=1 HTTP/1.1", 200, b'{"status":"ok"}'),
            (b"PUT /api/v2/me HTTP/1.1", 405, b'{"detail":"Method Not Allowed"}'),
            (b"GET /api/v2/me?x=1 HTTP/1.1", 299, b"downstream"),
            (b"POST /api/auth/f2/logout HTTP/1.1", 299, b"downstream"),
        ]
        for line, status, body in cases:
            got_status, headers, got_body = _raw_http(port, line, b"Origin: https://evil.test\r\n"
                                                                   b"Access-Control-Request-Method: POST\r\n"
                                                                   b"Cookie: a=b\r\n")
            assert (got_status, got_body) == (status, body), line
            names = [k for k, _ in headers]
            assert not [k for k in names if k.startswith(b"access-control-")], line
            assert b"location" not in names
            if status != 299:
                assert b"set-cookie" not in names, line
                assert (b"cache-control", b"no-store") in headers, line
    finally:
        server.should_exit = True
        thread.join(10)
    assert [c[1:3] for c in personal.calls] == [("GET", "/api/v2/me")]
    assert [c[1:3] for c in auth.calls] == [("POST", "/api/auth/f2/logout")]
