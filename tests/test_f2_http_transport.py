"""Transport HTTP F2 (R1 PR 3), sans PostgreSQL : CSRF, provenance,
cookies, corps JSON, sélecteurs, IP client, durées des cookies.

Le moteur est un double qui ÉCHOUE s'il est appelé alors qu'une
validation de transport aurait dû refuser la requête : aucun appel au
moteur F2 avant validation complète.
"""
import json
import logging
import pickle
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.auth_f2.crypto import Redacted
from core.auth_f2.service import CandidateCredential, Outcome, SessionCredential, SessionPrincipal
from core.auth_f2_http import csrf
from core.auth_f2_http.client_ip import ClientIpUnavailable, PeerAddressResolver, TrustedProxyResolver
from core.auth_f2_http.cookies import (
    CSRF_BINDING,
    CookieHeaderRejected,
    active_name,
    candidate_name,
    parse_cookie_headers,
)
from core.auth_f2_http.isolated import make_app
from core.auth_f2_http.provenance import ProvenanceRejected, request_origin, validate_allowed_origins
from core.auth_f2_http.routes import Dependencies
from tests.f2_http_support import ORIGIN, Browser, StaticIp, csrf_keyring, set_cookies
from tests.test_f2_http_contract import CAND, GEN, OP, REQ, SESS, StubService, issued, secret

B1 = secret(1).reveal()
B2 = secret(2).reveal()


class FakeClock:
    def __init__(self, wall=1_800_000_000.0):
        self.wall = wall
        self.mono = 1000.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono


class Env:
    def __init__(self, *, resolver=None, keyring=None, clock=None):
        self.service = StubService()
        self.clock = clock or FakeClock()
        self.resolver = resolver or StaticIp()
        self.keyring = keyring or csrf_keyring()
        self.app = make_app(Dependencies(self.service, self.keyring, self.resolver, frozenset({ORIGIN}),
                                         self.clock.time, self.clock.monotonic))
        self.browser = Browser(self.app)

    def ready(self):
        self.browser.bootstrap()
        return self


@pytest.fixture
def env():
    return Env().ready()


def assert_refused(env, response, status, code):
    assert (response.status_code, response.json()) == (status, {"error": code})
    assert response.headers["cache-control"] == "no-store"
    assert env.service.calls == []


# --------------------------------------------------------------------------
# Protocole CSRF
# --------------------------------------------------------------------------

NOW = 1_800_000_000


def test_csrf_token_shape_and_round_trip():
    ring = csrf_keyring()
    token = csrf.issue_token(ring, B1, ORIGIN, NOW)
    version, exp, nonce, mac = token.split(".")
    assert version == "c1" and exp == str(NOW + 1800) and len(nonce) == 22 and len(mac) == 43
    assert csrf.verify_token(ring, token, B1, ORIGIN, NOW)
    assert csrf.issue_token(ring, B1, ORIGIN, NOW) != token  # nonce aléatoire


def test_csrf_expiry_boundaries():
    ring = csrf_keyring()
    token = csrf.issue_token(ring, B1, ORIGIN, NOW)
    assert csrf.verify_token(ring, token, B1, ORIGIN, NOW + 1799)
    assert not csrf.verify_token(ring, token, B1, ORIGIN, NOW + 1800)  # now >= exp : expiré
    assert not csrf.verify_token(ring, token, B1, ORIGIN, NOW - 1)  # au-delà de la durée d'émission


def test_csrf_binding_and_origin_are_signed():
    ring = csrf_keyring()
    token = csrf.issue_token(ring, B1, ORIGIN, NOW)
    assert not csrf.verify_token(ring, token, B2, ORIGIN, NOW)
    assert not csrf.verify_token(ring, token, B1, "https://other.test", NOW)
    assert not csrf.verify_token(ring, token, None, ORIGIN, NOW)
    assert not csrf.verify_token(ring, token, B1[:-1] + "F", ORIGIN, NOW)  # liaison non canonique


def _flip(value: str) -> str:
    return ("B" if value[0] != "B" else "C") + value[1:]


def test_csrf_tampering_and_non_canonical_encodings_are_refused():
    ring = csrf_keyring()
    version, exp, nonce, mac = csrf.issue_token(ring, B1, ORIGIN, NOW).split(".")
    candidates = [
        f"{version}.{exp}.{nonce}.{_flip(mac)}", f"{version}.{exp}.{_flip(nonce)}.{mac}",
        f"{version}.{int(exp) - 1}.{nonce}.{mac}", f"{version}.0{exp}.{nonce}.{mac}",
        f"{version}.{exp}.{nonce}.{mac}.", f"{version}.{exp}.{nonce}", f"{version}.{exp}.{nonce}.{mac}=",
        f"{version}.{exp}.{nonce}=.{mac}", f"{version}.{exp}.{nonce[:-1]}x.{mac}", f"{version}.+{exp}.{nonce}.{mac}",
        f"{version}.{exp}.{nonce}.{mac[:-1]}", "", None, 12, "é" * 10, "a" * 500,
        f"{version}.{exp}.{nonce}.{mac[:-1] + ('B' if mac[-1] == 'A' else 'A')}",
    ]
    # Bits de remplissage parasites : même octets décodés, encodage différent.
    last = mac[-1]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    sibling = alphabet[(alphabet.index(last) & ~3) | ((alphabet.index(last) + 1) & 3)]
    candidates.append(f"{version}.{exp}.{nonce}.{mac[:-1]}{sibling}")
    for token in candidates:
        assert not csrf.verify_token(ring, token, B1, ORIGIN, NOW), token


def test_csrf_unknown_version_is_refused_without_substitution():
    old, new = csrf_keyring(("c1",)), csrf.CsrfKeyring({"c2": b"\x01" * 32}, "c2")
    token = csrf.issue_token(old, B1, ORIGIN, NOW)
    assert not csrf.verify_token(new, token, B1, ORIGIN, NOW)
    forged = token.replace("c1.", "c2.", 1)
    assert not csrf.verify_token(old, forged, B1, ORIGIN, NOW)


def test_csrf_explicit_rotation():
    k1, k2 = b"\x11" * 32, b"\x22" * 32
    before = csrf.CsrfKeyring({"c1": k1}, "c1")
    during = csrf.CsrfKeyring({"c1": k1, "c2": k2}, "c2")
    after = csrf.CsrfKeyring({"c2": k2}, "c2")
    old_token = csrf.issue_token(before, B1, ORIGIN, NOW)
    assert csrf.verify_token(during, old_token, B1, ORIGIN, NOW)
    new_token = csrf.issue_token(during, B1, ORIGIN, NOW)
    assert new_token.startswith("c2.")
    assert csrf.verify_token(after, new_token, B1, ORIGIN, NOW)
    assert not csrf.verify_token(after, old_token, B1, ORIGIN, NOW)


def test_csrf_message_framing_is_unambiguous():
    nonce = b"\x00" * 16
    a = csrf._message("ab", "1", nonce, b"\x01" * 32, "https://x.test")
    b = csrf._message("a", "b1", nonce, b"\x01" * 32, "https://x.test")
    assert a != b and a.startswith(b"ORYX-F2-CSRF\x00\x00\x0foryx-f2-csrf-v1")


@pytest.mark.parametrize("keys,current", [
    ({}, "c1"), ({"c1": b"\x01" * 31}, "c1"), ({"c.1": b"\x01" * 32}, "c.1"), ({"c1": b"\x01" * 32}, "c2"),
    ({"c1": b"\x01" * 32, "c2": b"\x01" * 32}, "c1"), ({"c1": "x" * 32}, "c1"), ({"": b"\x01" * 32}, ""),
])
def test_csrf_keyring_validation(keys, current):
    with pytest.raises(csrf.CsrfConfigError):
        csrf.CsrfKeyring(keys, current)


def test_csrf_keyring_never_reveals_keys():
    ring = csrf.CsrfKeyring({"c1": b"\x7a" * 32}, "c1")
    assert "z" * 8 not in repr(ring) and "7a" not in repr(ring)
    with pytest.raises(TypeError):
        pickle.dumps(ring)


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

class Headers:
    def __init__(self, pairs):
        self.pairs = pairs

    def getlist(self, name):
        return [v for k, v in self.pairs if k == name]


ALLOWED = frozenset({ORIGIN})


@pytest.mark.parametrize("pairs", [
    [("origin", ORIGIN)], [("origin", ORIGIN), ("sec-fetch-site", "same-origin")],
    [("origin", ORIGIN), ("sec-fetch-site", "same-site")],
    [("sec-fetch-site", "same-origin"), ("host", "oryx.test")],
    [("sec-fetch-site", "same-origin"), ("host", "oryx.test"), ("referer", ORIGIN + "/app?x=1")],
    [("sec-fetch-site", "same-origin"), ("host", "oryx.test"), ("referer", "https://oryx.test:443/")],
])
def test_legitimate_provenance(pairs):
    assert request_origin(Headers(pairs), ALLOWED) == ORIGIN


@pytest.mark.parametrize("pairs", [
    [("origin", "https://evil.test")], [("origin", "null")], [("origin", ORIGIN + "/")], [("origin", "HTTPS://ORYX.TEST")],
    [("origin", "http://oryx.test")], [("origin", ORIGIN), ("origin", ORIGIN)],
    [("origin", ORIGIN), ("sec-fetch-site", "cross-site")], [("origin", ORIGIN), ("sec-fetch-site", "none")],
    [("origin", ORIGIN), ("sec-fetch-site", "same-origin"), ("sec-fetch-site", "same-origin")],
    [("host", "oryx.test")], [("sec-fetch-site", "cross-site"), ("host", "oryx.test")],
    [("sec-fetch-site", "none"), ("host", "oryx.test")], [("sec-fetch-site", "same-site"), ("host", "oryx.test")],
    [("sec-fetch-site", "same-origin"), ("host", "evil.test")], [("sec-fetch-site", "same-origin")],
    [("sec-fetch-site", "same-origin"), ("host", "oryx.test"), ("referer", "https://evil.test/")],
    [("sec-fetch-site", "same-origin"), ("host", "oryx.test"), ("referer", "http://oryx.test/")],
    [("sec-fetch-site", "same-origin"), ("host", "oryx.test"), ("referer", "https://u:p@oryx.test/")],
    [("sec-fetch-site", "same-origin"), ("host", "oryx.test"), ("referer", "not a url")],
    [("sec-fetch-site", "same-origin"), ("host", "oryx.test"), ("referer", ORIGIN), ("referer", ORIGIN)],
    [("sec-fetch-site", "same-origin"), ("host", "ORYX.TEST")],
    [("sec-fetch-site", "same-origin"), ("host", "oryx.test"), ("host", "oryx.test")],
])
def test_hostile_unknown_or_ambiguous_provenance_is_refused(pairs):
    with pytest.raises(ProvenanceRejected):
        request_origin(Headers(pairs), ALLOWED)


@pytest.mark.parametrize("origins", [[], "https://oryx.test", ["http://oryx.test"], ["https://oryx.test/"],
                                     ["https://oryx.test:443"], ["https://ORYX.test"], [ORIGIN, ORIGIN], [None]])
def test_allowed_origins_validation(origins):
    with pytest.raises(ValueError):
        validate_allowed_origins(origins)


def test_bootstrap_without_origin_from_same_origin_page(env):
    browser = Browser(env.app)
    response = browser.request("GET", "/csrf/bootstrap", origin=None,
                               headers={"sec-fetch-site": "same-origin", "referer": ORIGIN + "/web-v2"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"csrf_token", "expires_in"}
    (binding,) = set_cookies(response)
    assert binding["name"] == CSRF_BINDING
    assert binding["attrs"] == {"max-age": "86400", "path": "/", "secure": "", "httponly": "", "samesite": "Lax"}
    assert "domain" not in binding["raw"].lower()
    assert env.service.calls == []


@pytest.mark.parametrize("origin,headers", [
    ("https://evil.test", {}), (None, {}), (None, {"sec-fetch-site": "cross-site"}),
    (None, {"sec-fetch-site": "same-origin", "referer": "https://evil.test/"}), ("null", {}),
])
def test_bootstrap_refuses_hostile_or_unknown_provenance(env, origin, headers):
    browser = Browser(env.app)
    response = browser.request("GET", "/csrf/bootstrap", origin=origin, headers=headers)
    assert_refused(env, response, 403, "origin_refused")
    assert set_cookies(response) == []


def test_bootstrap_reuses_the_binding_so_tabs_keep_their_tokens(env):
    first = env.browser.csrf
    binding = env.browser.jar[CSRF_BINDING]
    env.browser.bootstrap()
    assert env.browser.jar[CSRF_BINDING] == binding
    env.service.result = Outcome("rejected", "invalid_email")
    for token in (first, env.browser.csrf):
        env.browser.csrf = token
        assert env.browser.start("x").status_code == 422


def test_mutation_with_origin_absent_but_same_origin_attested(env):
    env.service.result = Outcome("rejected", "invalid_email")
    response = env.browser.start("x", origin=None, headers={"sec-fetch-site": "same-origin"})
    assert response.status_code == 422
    response = env.browser.start("x", origin=None)
    assert response.status_code == 403


@pytest.mark.parametrize("origin,headers", [
    ("https://evil.test", {}), (None, {}), (None, {"sec-fetch-site": "cross-site"}),
    (ORIGIN, {"sec-fetch-site": "cross-site"}),
])
def test_cross_site_mutations_are_refused_before_the_engine(env, origin, headers):
    response = env.browser.start("alice@example.com", origin=origin, headers=headers)
    assert_refused(env, response, 403, "origin_refused")


# --------------------------------------------------------------------------
# CSRF sur les routes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,body", [
    ("/sign-in/start", {"email": "alice@example.com"}), ("/sign-in/resend", REQ),
    ("/sign-in/verify", {**REQ, "code": "12345678"}), ("/sign-in/resume", REQ),
    ("/session/resolve", {"generation_id": str(GEN)}), ("/logout", {"generation_id": str(GEN)}),
])
def test_every_mutation_requires_a_valid_csrf_token(env, path, body):
    env.browser.jar[candidate_name(CAND)] = B1
    env.browser.jar[active_name(GEN)] = B2
    assert_refused(env, env.browser.post(path, body, csrf=False), 403, "csrf_invalid")
    env.browser.csrf, token = "c1.1.AAAAAAAAAAAAAAAAAAAAAA." + "A" * 43, env.browser.csrf
    assert_refused(env, env.browser.post(path, body), 403, "csrf_invalid")
    env.browser.csrf = token
    assert_refused(env, env.browser.post(path, body, headers={"x-oryx-csrf": token + "x"}), 403, "csrf_invalid")
    binding = env.browser.jar.pop(CSRF_BINDING)
    assert_refused(env, env.browser.post(path, body), 403, "csrf_invalid")
    env.browser.jar[CSRF_BINDING] = B2 if binding != B2 else B1  # autre liaison
    assert_refused(env, env.browser.post(path, body), 403, "csrf_invalid")


def test_csrf_token_expires_on_the_route(env):
    env.service.result = Outcome("rejected", "invalid_email")
    env.clock.advance(1799)
    assert env.browser.start("x").status_code == 422
    env.clock.advance(1)
    env.service.calls.clear()
    assert_refused(env, env.browser.start("x"), 403, "csrf_invalid")


def test_duplicate_csrf_headers_are_refused(env):
    sent = [("x-oryx-csrf", env.browser.csrf), ("x-oryx-csrf", env.browser.csrf)]
    import asyncio

    import httpx

    async def go():
        transport = httpx.ASGITransport(app=env.app, client=("192.0.2.1", 1))
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            return await client.post("/api/auth/f2/sign-in/start", headers=sent + [
                ("origin", ORIGIN), ("cookie", env.browser.cookie_header()), ("content-type", "application/json")],
                content=b'{"email":"x"}')
    response = asyncio.run(go())
    assert_refused(env, response, 403, "csrf_invalid")


def test_a_csrf_token_is_not_an_authentication(env):
    response = env.browser.resolve(str(GEN))
    assert_refused(env, response, 401, "session_invalid")


# --------------------------------------------------------------------------
# En-tête Cookie
# --------------------------------------------------------------------------

def cid(n):
    return f"{n:08x}-abcd-4000-8000-00000000000a"


@pytest.mark.parametrize("header", [
    "a=" + "x" * 8200,
    "; ".join(f"__Host-oryx-candidate-{cid(i)}={B1}" for i in range(17)),
    f"__Host-oryx-candidate-{cid(1)}={B1}; __Host-oryx-candidate-{cid(1)}={B2}",
    f"__Host-oryx-active-{cid(1)}={B1}; __Host-oryx-active-{cid(1)}={B1}",
    f"__Host-oryx-csrf-binding={B1}; __Host-oryx-csrf-binding={B2}",
    f"__Host-oryx-candidate-{cid(1).upper()}={B1}", f"__Host-oryx-candidate-{cid(1)[:-1]}={B1}",
    f"__Host-oryx-candidate-{{{cid(1)}}}={B1}", f"__Host-oryx-other={B1}", f"__Host-oryx-candidate-{cid(1)}",
    f"__HOST-oryx-active-{cid(1)}={B1}", f"__host-oryx-active-{cid(1)}={B1}",
    f"__Host-oryx-candidate-{cid(1)}={B1[:-1]}", f"__Host-oryx-candidate-{cid(1)}=\"{B1}\"",
    f"__Host-oryx-candidate-{cid(1)}={B1[:-1]}F", f"__Host-oryx-candidate-{cid(1)}=",
])
def test_ambiguous_or_oversized_cookie_headers_are_refused(header):
    with pytest.raises(CookieHeaderRejected):
        parse_cookie_headers([header])


def test_cookie_parsing_keeps_only_exact_oryx_names():
    header = (f"theme=dark; __Host-oryx-candidate-{cid(1)}={B1}; other=1; other=2; "
              f"__Host-oryx-active-{cid(2)}={B2};__Host-oryx-csrf-binding={B1}; junk")
    cookies = parse_cookie_headers([header])
    assert dict(cookies.candidates) == {uuid.UUID(cid(1)): B1}
    assert dict(cookies.actives) == {uuid.UUID(cid(2)): B2}
    assert cookies.csrf_binding == B1 and cookies.count == 3
    assert parse_cookie_headers(["; ".join(f"__Host-oryx-candidate-{cid(i)}={B1}" for i in range(16))]).count == 16
    with pytest.raises(CookieHeaderRejected):
        parse_cookie_headers(["a=1", "b=2"])


def test_invalid_cookie_header_is_refused_before_the_engine(env):
    env.browser.jar[f"__Host-oryx-candidate-{cid(1).upper()}"] = B1
    assert_refused(env, env.browser.start("alice@example.com"), 400, "invalid_request")


def test_start_refuses_to_exceed_the_cookie_limit(env):
    for i in range(15):
        env.browser.jar[candidate_name(uuid.UUID(cid(i)))] = B1
    assert_refused(env, env.browser.start("alice@example.com"), 409, "credential_limit")


def test_bootstrap_refuses_a_new_binding_beyond_the_limit(env):
    browser = Browser(env.app)
    for i in range(16):
        browser.jar[candidate_name(uuid.UUID(cid(i)))] = B1
    response = browser.request("GET", "/csrf/bootstrap", origin=None, headers={"sec-fetch-site": "same-origin"})
    assert_refused(env, response, 409, "credential_limit")


# --------------------------------------------------------------------------
# Corps JSON
# --------------------------------------------------------------------------

@pytest.mark.parametrize("content,ctype,status", [
    (b'{"email":"a@b.c"}', "text/plain", 415), (b'{"email":"a@b.c"}', "application/json; charset=latin-1", 415),
    (b'{"email":"a@b.c"}', "application/x-www-form-urlencoded", 415), (b'{"email":"a@b.c"}', None, 415),
    (b'{"email":"' + b"a" * 4100 + b'"}', "application/json", 413),
    (b"{", "application/json", 400), (b'{"email":"a","email":"b"}', "application/json", 400),
    (b'{"email":"a","x":1}', "application/json", 400), (b"{}", "application/json", 400),
    (b'{"email":1}', "application/json", 400), (b'{"email":null}', "application/json", 400),
    (b'["email"]', "application/json", 400), (b'{"email":NaN}', "application/json", 400),
    (b'{"email":"' + b"a" * 321 + b'"}', "application/json", 400), (b'\xff\xfe{}', "application/json", 400),
    (b'\xef\xbb\xbf{"email":"a"}', "application/json", 400), (b"", "application/json", 400),
    (b"[" * 2000 + b"]" * 2000, "application/json", 400),
])
def test_body_schema_and_size_are_validated_before_the_engine(env, content, ctype, status):
    headers = {"content-type": ctype} if ctype else {}
    response = env.browser.request("POST", "/sign-in/start", content=content, headers=headers)
    code = {415: "invalid_request", 413: "request_too_large", 400: "invalid_request"}[status]
    assert_refused(env, response, status, code)


def test_body_limit_holds_without_content_length(env):
    async def chunks():
        for _ in range(5):
            yield b" " * 1000
        yield b'{"email":"a@b.c"}'
    import asyncio

    import httpx

    async def go():
        transport = httpx.ASGITransport(app=env.app, client=("192.0.2.1", 1))
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            return await client.post("/api/auth/f2/sign-in/start", content=chunks(), headers={
                "origin": ORIGIN, "x-oryx-csrf": env.browser.csrf, "cookie": env.browser.cookie_header(),
                "content-type": "application/json"})
    response = asyncio.run(go())
    assert "content-length" not in response.request.headers
    assert_refused(env, response, 413, "request_too_large")


@pytest.mark.parametrize("field,value", [
    ("operation_id", "AAAAAAAA-1111-4111-8111-111111111111"), ("operation_id", "{" + str(OP) + "}"), ("operation_id", str(OP).replace("-", "")),
    ("candidate_id", "not-a-uuid"), ("candidate_id", 7), ("operation_id", None), ("candidate_id", str(CAND) + " "),
])
def test_identifiers_must_be_canonical_uuids(env, field, value):
    env.browser.jar[candidate_name(CAND)] = B1
    assert_refused(env, env.browser.post("/sign-in/resume", {**REQ, field: value}), 400, "invalid_request")


@pytest.mark.parametrize("code", [12345678, None, ["12345678"], {"c": 1}])
def test_code_must_be_a_json_string(env, code):
    env.browser.jar[candidate_name(CAND)] = B1
    assert_refused(env, env.browser.post("/sign-in/verify", {**REQ, "code": code}), 400, "invalid_request")


@pytest.mark.parametrize("code", ["1234", "abcdefgh", " 12345678", "١٢٣٤٥٦٧٨", "1" * 200, ""])
def test_malformed_code_strings_reach_the_engine_unchanged(env, code):
    """Le moteur compte un code mal formé comme un échec (PR 2) : la couche
    HTTP ne l'intercepte pas (seules les bornes de transport s'appliquent)."""
    env.browser.jar[candidate_name(CAND)] = B1
    env.service.result = Outcome("rejected", "code_malformed", OP)
    response = env.browser.post("/sign-in/verify", {**REQ, "code": code})
    assert (response.status_code, response.json()) == (401, {"error": "code_incorrect"})
    assert env.service.calls[0][1]["code"] == code


# --------------------------------------------------------------------------
# Sélecteurs explicites, sans repli
# --------------------------------------------------------------------------

def test_candidate_is_selected_by_its_exact_identifier_only(env):
    other = uuid.UUID(cid(9))
    env.browser.jar[candidate_name(other)] = B2
    # Un seul cookie candidat présent, mais d'un autre identifiant : aucun repli.
    assert_refused(env, env.browser.post("/sign-in/verify", {**REQ, "code": "12345678"}), 401, "credential_missing")
    env.browser.jar[candidate_name(CAND)] = B1
    env.service.result = Outcome("rejected", "code_incorrect", OP)
    env.browser.post("/sign-in/verify", {**REQ, "code": "12345678"})
    ((name, kwargs),) = env.service.calls
    assert kwargs["candidate"] == CandidateCredential(CAND, Redacted(B1))
    assert kwargs["operation_id"] == OP


def test_session_is_selected_by_its_exact_generation_only(env):
    other = uuid.UUID(cid(9))
    env.browser.jar[active_name(other)] = B2
    assert_refused(env, env.browser.resolve(str(GEN)), 401, "session_invalid")
    env.browser.jar[active_name(GEN)] = B1
    env.service.result = SessionPrincipal("user-1", SESS, GEN, "supabase_email_sign_in", None)
    response = env.browser.resolve(str(GEN))
    assert response.json() == {"status": "active", "generation_id": str(GEN)}
    assert env.service.calls == [("authenticate", {"credential": SessionCredential(GEN, Redacted(B1))})]


def test_resolve_refuses_a_principal_of_another_generation(env):
    env.browser.jar[active_name(GEN)] = B1
    env.service.result = SessionPrincipal("user-1", SESS, uuid.uuid4(), "supabase_email_sign_in", None)
    response = env.browser.resolve(str(GEN))
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert active_name(GEN) in env.browser.jar


def test_invalid_session_clears_only_its_own_cookie(env):
    other = uuid.UUID(cid(9))
    env.browser.jar[active_name(GEN)] = B1
    env.browser.jar[active_name(other)] = B2
    env.service.result = None
    response = env.browser.resolve(str(GEN))
    assert (response.status_code, response.json()) == (401, {"error": "session_invalid"})
    assert [c["name"] for c in set_cookies(response)] == [active_name(GEN)]
    assert active_name(GEN) not in env.browser.jar and env.browser.jar[active_name(other)] == B2


def test_logout_revokes_exactly_the_authenticated_session(env):
    other = uuid.UUID(cid(9))
    env.browser.jar[active_name(GEN)] = B1
    env.browser.jar[active_name(other)] = B2
    principal = SessionPrincipal("user-1", SESS, GEN, "supabase_email_sign_in", None)
    env.service.result = lambda kw: principal if "credential" in kw else True
    response = env.browser.logout(str(GEN))
    assert response.json() == {"status": "logged_out"}
    assert env.service.calls[1] == ("revoke", {"session_id": SESS, "reason": "logout"})
    assert active_name(GEN) not in env.browser.jar and active_name(other) in env.browser.jar


def test_logout_engine_failure_keeps_the_cookie(env):
    env.browser.jar[active_name(GEN)] = B1
    principal = SessionPrincipal("user-1", SESS, GEN, "supabase_email_sign_in", None)

    def answer(kw):
        if "credential" in kw:
            return principal
        raise RuntimeError("db down")
    env.service.result = answer
    response = env.browser.logout(str(GEN))
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert set_cookies(response) == [] and active_name(GEN) in env.browser.jar


# --------------------------------------------------------------------------
# IP client
# --------------------------------------------------------------------------

class Raising:
    def __call__(self, request):
        raise ClientIpUnavailable("non fiable")


@pytest.mark.parametrize("path,body", [
    ("/sign-in/start", {"email": "alice@example.com"}), ("/sign-in/resend", REQ),
    ("/sign-in/verify", {**REQ, "code": "12345678"})])
def test_unreliable_ip_is_refused_before_the_engine(path, body):
    env = Env(resolver=Raising()).ready()
    env.browser.jar[candidate_name(CAND)] = B1
    assert_refused(env, env.browser.post(path, body), 503, "temporarily_unavailable")


def test_ip_is_resolved_only_after_transport_validation():
    resolver = StaticIp()
    env = Env(resolver=resolver).ready()
    env.browser.start("alice@example.com", csrf=False)
    env.browser.post("/sign-in/start", {"email": "a", "x": 1})
    assert resolver.calls == 0
    env.service.result = Outcome("rejected", "invalid_email")
    env.browser.start("x")
    assert resolver.calls == 1 and env.service.calls[0][1]["client_ip"] == "203.0.113.7"


class Req:
    def __init__(self, peer, headers=()):
        self.client = type("C", (), {"host": peer})() if peer is not None else None
        self.headers = Headers(list(headers))


def test_peer_address_resolver():
    assert PeerAddressResolver()(Req("198.51.100.4")) == "198.51.100.4"
    assert PeerAddressResolver()(Req("2001:db8::1", [("x-forwarded-for", "1.2.3.4")])) == "2001:db8::1"
    for peer in (None, "testclient", "", " 1.2.3.4", "fe80::1%eth0"):
        with pytest.raises(ClientIpUnavailable):
            PeerAddressResolver()(Req(peer))


def test_trusted_proxy_resolver_ignores_forged_forwarding_headers():
    resolver = TrustedProxyResolver(["10.0.0.0/8", "fd00::/8"])
    xff = [("x-forwarded-for", "6.6.6.6, 198.51.100.4, 10.1.1.1")]
    assert resolver(Req("10.0.0.2", xff)) == "198.51.100.4"  # le plus à droite non approuvé
    assert resolver(Req("10.0.0.2", [("x-forwarded-for", "2001:db8::5")])) == "2001:db8::5"
    assert resolver(Req("fd00::1", [("x-forwarded-for", "198.51.100.4")])) == "198.51.100.4"
    refused = [
        Req("198.51.100.9", xff),  # pair non approuvé : en-tête forgé ignoré, refus
        Req("10.0.0.2"), Req("10.0.0.2", xff + xff), Req("10.0.0.2", [("x-forwarded-for", "")]),
        Req("10.0.0.2", [("x-forwarded-for", "10.2.2.2, 10.3.3.3")]),
        Req("10.0.0.2", [("x-forwarded-for", "198.51.100.4:443")]),
        Req("10.0.0.2", [("x-forwarded-for", "[2001:db8::5]")]),
        Req("10.0.0.2", [("x-forwarded-for", "unknown, 198.51.100.4")]),
        Req("10.0.0.2", [("x-forwarded-for", ",".join(["198.51.100.4"] * 17))]),
        Req("10.0.0.2", [("x-forwarded-for", "1" * 1025)]), Req(None, xff), Req("testclient", xff),
    ]
    for request in refused:
        with pytest.raises(ClientIpUnavailable):
            resolver(request)
    for bad in ([], "10.0.0.0/8", ["10.0.0.1/8"]):
        with pytest.raises(ValueError):
            TrustedProxyResolver(bad)


def test_trusted_proxy_resolver_on_the_route_refuses_a_direct_forged_request():
    env = Env(resolver=TrustedProxyResolver(["10.0.0.0/8"])).ready()
    response = env.browser.start("alice@example.com", headers={"x-forwarded-for": "198.51.100.4"})
    assert_refused(env, response, 503, "temporarily_unavailable")
    env.browser.peer = ("10.0.0.3", 1)
    env.service.result = Outcome("rejected", "invalid_email")
    env.browser.start("alice@example.com", headers={"x-forwarded-for": "6.6.6.6, 198.51.100.4"})
    assert env.service.calls[0][1]["client_ip"] == "198.51.100.4"


# --------------------------------------------------------------------------
# Durées des cookies (horloges injectées)
# --------------------------------------------------------------------------

def _after(env, seconds, outcome):
    def answer(kw):
        env.clock.advance(seconds)
        return outcome
    return answer


@pytest.mark.parametrize("elapsed,expected", [(0, 900), (0.4, 899), (30.5, 869), (898.9, 1)])
def test_candidate_cookie_lives_at_most_15_minutes_from_request_entry(env, elapsed, expected):
    env.service.result = _after(env, elapsed, Outcome("issued", None, OP, CandidateCredential(CAND, secret(5))))
    response = env.browser.start("alice@example.com")
    (cookie,) = set_cookies(response)
    assert cookie["name"] == candidate_name(CAND) and cookie["attrs"]["max-age"] == str(expected)
    assert cookie["attrs"] == {"max-age": str(expected), "path": "/", "secure": "", "httponly": "", "samesite": "Lax"}


def test_candidate_cookie_is_not_set_when_its_lifetime_is_spent(env):
    env.service.result = _after(env, 899.5, Outcome("issued", None, OP, CandidateCredential(CAND, secret(5))))
    response = env.browser.start("alice@example.com")
    assert response.status_code == 503 and set_cookies(response) == []


def test_resend_never_renews_the_candidate_cookie(env):
    env.browser.jar[candidate_name(CAND)] = B1
    env.service.result = Outcome("issued", None, OP)
    response = env.browser.post("/sign-in/resend", REQ)
    assert response.json()["status"] == "code_sent" and set_cookies(response) == []


@pytest.mark.parametrize("status,elapsed,expected", [
    ("authenticated", 0, 15 * 86400), ("authenticated", 2.5, 15 * 86400 - 3),
    ("resumed", 0, 15 * 86400 - 300), ("resumed", 1.2, 15 * 86400 - 302)])
def test_active_cookie_never_outlives_the_database_expiry(env, status, elapsed, expected):
    env.browser.jar[candidate_name(CAND)] = B1
    expires = datetime.fromtimestamp(env.clock.wall, timezone.utc) + timedelta(days=15, seconds=elapsed)
    session = issued(status, absolute_expires_at=expires)
    env.service.result = _after(env, elapsed, Outcome(status, None, OP, session=session))
    path = "/sign-in/verify" if status == "authenticated" else "/sign-in/resume"
    body = {**REQ, "code": "12345678"} if status == "authenticated" else REQ
    response = env.browser.post(path, body)
    active = [c for c in set_cookies(response) if c["name"] == active_name(GEN)][0]
    assert int(active["attrs"]["max-age"]) == expected == response.json()["expires_in"]
    assert int(active["attrs"]["max-age"]) <= (expires - datetime.fromtimestamp(env.clock.wall, timezone.utc)
                                               ).total_seconds()


def test_active_cookie_is_bounded_by_the_wall_clock_too(env):
    env.browser.jar[candidate_name(CAND)] = B1
    expires = datetime.fromtimestamp(env.clock.wall, timezone.utc) + timedelta(hours=1)
    env.service.result = Outcome("resumed", None, OP, session=issued("resumed", absolute_expires_at=expires))
    response = env.browser.post("/sign-in/resume", REQ)
    assert response.json()["expires_in"] == 3600


# --------------------------------------------------------------------------
# En-têtes, journaux, routes inconnues
# --------------------------------------------------------------------------

def test_unknown_routes_and_methods_are_not_cached(env):
    for method, path in (("GET", "/nope"), ("GET", "/sign-in/start"), ("POST", "/csrf/bootstrap"),
                         ("OPTIONS", "/sign-in/start")):
        response = env.browser.request(method, path)
        assert response.status_code in (404, 405)
        assert response.headers["cache-control"] == "no-store"
        assert set_cookies(response) == []


def test_logs_and_bodies_never_contain_secrets(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.service.result = Outcome("issued", None, OP, CandidateCredential(CAND, secret(5)))
    start = env.browser.start("alice@example.com")
    env.service.result = Outcome("authenticated", None, OP, session=issued())
    verify = env.browser.post("/sign-in/verify", {**REQ, "code": "87654321"})
    text = caplog.text
    for value in (secret(5).reveal(), secret(9).reveal(), "87654321", "alice@example.com", "user-1",
                  env.browser.jar[CSRF_BINDING], env.browser.csrf):
        assert value not in text
    for response in (start, verify):
        assert secret(5).reveal() not in response.text and secret(9).reveal() not in response.text
        assert "user" not in response.text


def test_engine_exceptions_are_503_without_detail(env, caplog):
    env.service.result = RuntimeError("password=hunter2 alice@example.com")
    response = env.browser.start("alice@example.com")
    assert (response.status_code, response.json()) == (503, {"error": "temporarily_unavailable"})
    assert "hunter2" not in caplog.text and "alice" not in caplog.text


def test_bootstrap_creates_nothing(env):
    for _ in range(3):
        env.browser.bootstrap()
    assert env.service.calls == []
