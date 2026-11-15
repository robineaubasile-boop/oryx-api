"""E0 — e0-origin réel : routes, préfixe gardé, observations, confidentialité (spécification V0.2-R3 § 2.2, § 6.2).

Serveur Uvicorn RÉEL en sous-processus (boucle locale). La réponse de refus est
comparée OCTET POUR OCTET aux constantes de F04-C § 4, extraites par AST du
code figé (lecture comme données, sans import).
"""
import ast
import asyncio
import hashlib
import json
import os
import select
import socket
import threading
import time

import pytest

from tests import e0_support as s

import e0_attest as at  # noqa: E402
import e0_ipcompare as cmp  # noqa: E402
import e0_jsonl as jl  # noqa: E402
import e0_main  # noqa: E402
import e0_origin as og  # noqa: E402
import e0_version as ver  # noqa: E402

TID = "AAAAAAAAAAAAAAAAAAAAAA"
GUARDED = "/e0/guarded/" + TID
POST_KEY_HEX = "0f" * 32
POST_KEY_ID = cmp.key_id(bytes.fromhex(POST_KEY_HEX))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    s.block_external_network(monkeypatch)


def att_header(value):
    return (ver.ATTESTATION_HEADER, value)


def records_of(origin, route=None):
    return [r for r in origin.records("origin-record") if route is None or r["route"] == route]


# --------------------------------------------------------------------------
# Constantes F04-C extraites statiquement (sans import)
# --------------------------------------------------------------------------

def _module_constant(relative, name):
    tree = ast.parse((s.REPO_ROOT / relative).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} introuvable dans {relative}")


def f04c_maintenance_messages(method):
    body = _module_constant("core/f2_runtime/entrypoint.py", "MAINTENANCE_BODY")
    status = _module_constant("core/f2_runtime/entrypoint.py", "MAINTENANCE_STATUS")
    no_store = _module_constant("core/route_policy/gate.py", "NO_STORE_HEADERS")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))]
    headers += [(k.encode("ascii"), v.encode("ascii")) for k, v in no_store]
    return [{"type": "http.response.start", "status": status, "headers": headers},
            {"type": "http.response.body", "body": b"" if method == "HEAD" else body}]


def test_refusal_constants_equal_the_frozen_f04c_values():
    assert ver.REFUSAL_BODY == _module_constant("core/f2_runtime/entrypoint.py", "MAINTENANCE_BODY")
    assert ver.REFUSAL_STATUS == _module_constant("core/f2_runtime/entrypoint.py", "MAINTENANCE_STATUS")
    assert ver.REFUSAL_NO_STORE_HEADERS == _module_constant("core/route_policy/gate.py", "NO_STORE_HEADERS")


# --------------------------------------------------------------------------
# Application en processus (messages ASGI exacts)
# --------------------------------------------------------------------------

def in_process_app(phase="2", **extra):
    config = og.config_from_environment(s.origin_environment(phase, **extra))
    lines = []
    return og.OriginApp(config, bench="sha256-tree:test", emit=lines.append), lines


def call(app, *, method="GET", path=GUARDED, headers=(), body=b""):
    raw_path, _, query = path.partition("?")
    scope = {"type": "http", "method": method, "path": raw_path, "raw_path": raw_path.encode("latin-1"),
             "query_string": query.encode("latin-1"), "http_version": "1.1", "scheme": "http",
             "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in
                         [("host", s.ORIGIN_HOST), *headers]], "client": ("127.0.0.1", 50000)}
    received = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        if received:
            return received.pop(0)
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "DELETE"])
def test_guarded_refusal_is_byte_for_byte_the_f04c_maintenance_response(method):
    app, _ = in_process_app()
    assert call(app, method=method) == f04c_maintenance_messages(method)
    assert call(app, method=method, headers=[att_header("v1.garbage")]) == f04c_maintenance_messages(method)


def test_guarded_refusal_does_not_read_the_body():
    app, lines = in_process_app()
    call(app, method="POST", body=b"x" * 1000)
    record = json.loads(lines[-1])
    assert record["route"] == "guarded" and record["status"] == 503 and record["body"] == {"read": False}


@pytest.mark.parametrize("path", ["/e0/guarded", "/e0/guarded/", GUARDED, GUARDED + "/extra", GUARDED + "?x=1",
                                  "/e0/guarded?tid=1", "/e0/%67uarded/" + TID, "/e0/guarded%2F" + TID,
                                  "/%65%30/guarded/" + TID])
def test_guarded_prefix_is_routed_to_the_verifier_never_to_the_catch_all(path):
    raw = path.split("?")[0]
    decoded = raw.replace("%67", "g").replace("%2F", "/").replace("%65", "e").replace("%30", "0")
    assert og.route_of("GET", raw.encode("latin-1"), decoded) == "guarded"
    app, lines = in_process_app()
    raw_path, _, query = path.partition("?")
    scope_path = decoded
    sent = []

    async def run():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await app({"type": "http", "method": "GET", "path": scope_path, "raw_path": raw_path.encode(),
                   "query_string": query.encode(), "headers": [(b"host", s.ORIGIN_HOST.encode())],
                   "http_version": "1.1", "scheme": "http", "client": None}, receive, send)

    asyncio.run(run())
    assert sent[0]["status"] == 503 and sent[1]["body"] == ver.REFUSAL_BODY
    assert json.loads(lines[-1])["route"] == "guarded"


@pytest.mark.parametrize("path, route", [("/e0/guardedX/" + TID, "catch_all"), ("/e0", "catch_all"),
                                         ("/e0/observe", "observe"), ("/x/e0/guarded", "catch_all"),
                                         ("/e0//guarded", "catch_all")])
def test_non_guarded_paths(path, route):
    assert og.route_of("GET", path.encode(), path) == route


def test_catch_all_and_route_table_have_no_comparison_endpoint():
    source = (s.BENCH_PY / "e0_origin.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, (str, bytes))}
    paths = {v for v in literals if isinstance(v, bytes) and v.startswith(b"/e0/")}
    assert paths == {b"/e0/observe", b"/e0/slow", b"/e0/cookies", b"/e0/redirect", b"/e0/page", b"/e0/health"}
    assert not any("compare" in (v.decode() if isinstance(v, bytes) else v) for v in literals
                   if isinstance(v, (str, bytes)) and str(v).startswith(("/e0", "b'/e0")))
    app, _ = in_process_app(E0_CMP_KEY_POST=POST_KEY_HEX, E0_CMP_KEY_POST_ID=POST_KEY_ID)
    for path in ("/e0/compare", "/e0/ipcompare", "/e0/eng_post", "/e0/compare?ip=192.0.0.9"):
        sent = call(app, path=path, headers=[att_header(s.attestation(t=path))])
        document = json.loads(sent[1]["body"])
        assert document["observation"]["route"] == "catch_all"
        assert "eng_post" not in document["observation"] and "cmp_key_post_id" not in document["observation"]


def test_websocket_is_closed():
    app, _ = in_process_app()
    sent = []

    async def run():
        async def send(message):
            sent.append(message)
        await app({"type": "websocket", "path": "/", "headers": []}, None, send)

    asyncio.run(run())
    assert sent == [{"type": "websocket.close", "code": 1008}]


# --------------------------------------------------------------------------
# Serveur réel
# --------------------------------------------------------------------------

def test_health_identification_and_new_boot_id_per_start():
    with s.origin_process() as first:
        status, headers, body = s.request(first.port, target="/e0/health")
        assert status == 200 and s.header_values(headers, b"x-e0-origin") == [b"E0-V0.1c"]
        assert json.loads(body)["boot_id"] == first.startup["boot_id"]
        assert s.header_values(headers, b"server") == []
    with s.origin_process() as second:
        assert second.startup["boot_id"] != first.startup["boot_id"]
    assert first.startup["e0"] == "origin-startup" and first.startup["seq"] == 0
    assert first.startup["uvicorn"]["proxy_headers"] is False
    assert first.startup["openssl"]["ed25519"] is True


def test_real_server_guarded_accept_then_replay_refused_and_observations():
    with s.origin_process() as origin:
        valid = s.attestation(t=GUARDED)
        st1, _, body1 = s.request(origin.port, target=GUARDED, headers=[att_header(valid)])
        st2, h2, body2 = s.request(origin.port, target=GUARDED, headers=[att_header(valid)])
        st3, _, body3 = s.request(origin.port, target=GUARDED)
        forged = s.attestation(seed=s.RFC_SEED_OTHER, t=GUARDED, kid=s.kid_of(s.RFC_SEED_2, "oryx-e0-2"))
        st4, _, _ = s.request(origin.port, target=GUARDED, headers=[att_header(forged)])
    assert st1 == 200 and json.loads(body1)["tid"] == TID
    assert (st2, body2, st3, body3, st4) == (503, ver.REFUSAL_BODY, 503, ver.REFUSAL_BODY, 503)
    assert [v for k, v in h2 if k not in (b"date", b"connection")] == [
        b"application/json", b"35", b"no-store", b"no-cache", b"nosniff", b"no-referrer"]
    rec = records_of(origin, "guarded")
    att = [r["attestation"] for r in rec]
    assert [(a["att_present"], a["att_crypto_valid"], a["att_policy_accepted"], a["refusal"]) for a in att] == [
        (True, True, True, None), (True, True, False, "replay_in_boot"), (False, False, False, "att_absent"),
        (True, False, False, "sig_invalid")]
    assert [r["tid"] for r in rec] == [TID] * 4
    assert att[0]["t"] == GUARDED and att[0]["key_origin"] == "current" and att[0]["nonce"] == att[1]["nonce"]
    assert "att_issued_evidence" not in origin.stdout


def test_historical_key_is_recognised_for_audit_never_accepted():
    with s.origin_process(phase="2") as origin:
        old = s.attestation(seed=s.RFC_SEED_1A, aud="oryx-e0-1a", t=GUARDED)
        status, _, body = s.request(origin.port, target=GUARDED, headers=[att_header(old)])
    assert (status, body) == (503, ver.REFUSAL_BODY)
    att = records_of(origin, "guarded")[0]["attestation"]
    assert att["att_crypto_valid"] is True and att["key_origin"] == "audit"
    assert att["att_policy_accepted"] is False and att["refusal"] == "aud_not_current_phase"
    assert att["kid"] == s.kid_of(s.RFC_SEED_1A, "oryx-e0-1a")


def test_phase_1a_origin_accepts_only_the_1a_key():
    with s.origin_process(phase="1a") as origin:
        ok = s.request(origin.port, target=GUARDED,
                       headers=[att_header(s.attestation(seed=s.RFC_SEED_1A, aud="oryx-e0-1a", t=GUARDED))])
        ko = s.request(origin.port, target=GUARDED, headers=[att_header(s.attestation(t=GUARDED))])
    assert ok[0] == 200 and ko[0] == 503
    assert records_of(origin, "guarded")[1]["attestation"]["refusal"] == "aud_not_current_phase"


def test_routes_cookies_redirect_page_observe():
    with s.origin_process() as origin:
        cookies = s.request(origin.port, target="/e0/cookies")
        redirect = s.request(origin.port, target="/e0/redirect")
        page = s.request(origin.port, target="/e0/page")
        observe = s.request(origin.port, method="POST", target="/e0/observe?a=1", body=b"abc",
                            headers=[("Origin", "https://app.e0-zone.example"), ("Sec-Fetch-Site", "same-origin"),
                                     ("x-e0-run", "run-1"), ("x-e0-seq", "7"), ("x-e0-case", "RT-04")])
        catch_all = s.request(origin.port, target="/api/v2/me/portfolio/pos-1")
    assert s.header_values(cookies[1], b"set-cookie") == list(og.SYNTHETIC_COOKIES)
    assert redirect[0] == 302 and s.header_values(redirect[1], b"location") == [
        b"https://app.e0-zone.example/e0/observe?redirected=1"]
    assert page[0] == 200 and s.header_values(page[1], b"content-security-policy") == [b"default-src 'self'"]
    assert b"<script" not in page[2]
    observation = json.loads(observe[2])["observation"]
    assert observation["body"]["length"] == 3 and observation["body"]["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert (observation["run"], observation["run_seq"], observation["test_id"]) == ("run-1", "7", "RT-04")
    assert observation["origin"] == ["https://app.e0-zone.example"]
    assert observation["sec_fetch"] == {"sec-fetch-site": ["same-origin"]}
    assert json.loads(catch_all[2])["observation"]["route"] == "catch_all"
    assert at.b64url_decode_canonical(json.loads(catch_all[2])["observation"]["target_b64"]) == \
        b"/api/v2/me/portfolio/pos-1"


def test_no_global_ip_in_clear_and_cookie_hashed():
    secret_cookie = "__Host-e0-a=valeur-cookie-synthetique-a1b2"
    with s.origin_process() as origin:
        status, _, body = s.request(origin.port, target="/e0/observe", headers=[
            ("CF-Connecting-IP", "192.0.0.9"), ("X-Real-IP", "2001:1::1"), ("X-Forwarded-For", "192.31.196.7, 10.0.0.1"),
            ("Forwarded", "for=192.0.0.10"), ("True-Client-IP", "203.0.113.9"), ("Referer", "https://192.0.0.9/x"),
            ("Cookie", secret_cookie)])
    assert status == 200
    exposed = origin.stdout + body.decode()
    for raw in ("192.0.0.9", "2001:1::1", "192.31.196.7", "192.0.0.10", "valeur-cookie-synthetique-a1b2"):
        assert raw not in exposed, raw
    observation = json.loads(body)["observation"]
    assert observation["ip_headers"]["cf-connecting-ip"][0]["text"].startswith("g4:")
    assert observation["ip_headers"]["x-real-ip"][0]["text"].startswith("g6:")
    assert "203.0.113.9" in exposed and "10.0.0.1" in exposed  # documentation et privée : objet de la mesure
    assert observation["cookie"] == {"count": 1, "sha256": hashlib.sha256(secret_cookie.encode()).hexdigest()}


def test_attestation_header_and_comparison_key_never_logged():
    value = s.attestation(t="/e0/observe")
    with s.origin_process(E0_CMP_KEY_POST=POST_KEY_HEX, E0_CMP_KEY_POST_ID=POST_KEY_ID) as origin:
        s.request(origin.port, target="/e0/observe", headers=[att_header(value)])
    signature_part = value.split(".")[2]
    assert signature_part not in origin.stdout and value not in origin.stdout
    assert POST_KEY_HEX not in origin.stdout and POST_KEY_HEX not in origin.stderr


def test_eng_post_is_hmac_not_ip_and_no_raw_ip_persisted():
    for ip in (s.SIGNABLE_V4, s.SIGNABLE_V6):
        with s.origin_process(E0_CMP_KEY_POST=POST_KEY_HEX, E0_CMP_KEY_POST_ID=POST_KEY_ID) as origin:
            status, _, body = s.request(origin.port, target=GUARDED, headers=[att_header(s.attestation(t=GUARDED,
                                                                                                        ip=ip))])
        assert status == 200
        record = records_of(origin, "guarded")[0]
        expected = cmp.mac(bytes.fromhex(POST_KEY_HEX), ver.IPCOMPARE_POST_DOMAIN, ip)
        assert record["eng_post"] == expected and record["cmp_key_post_id"] == POST_KEY_ID
        assert record["attestation"]["signed_ip"]["text"].startswith("g4:" if ":" not in ip else "g6:")
        assert ip not in origin.stdout and ip.encode() not in body
        assert origin.startup["cmp_key_post_id"] == POST_KEY_ID


def test_eng_post_absent_without_valid_signature_or_key():
    forged = s.attestation(seed=s.RFC_SEED_OTHER, t=GUARDED, kid=s.kid_of(s.RFC_SEED_2, "oryx-e0-2"))
    with s.origin_process(E0_CMP_KEY_POST=POST_KEY_HEX, E0_CMP_KEY_POST_ID=POST_KEY_ID) as origin:
        s.request(origin.port, target=GUARDED, headers=[att_header(forged)])
    assert records_of(origin)[0]["eng_post"] is None
    with s.origin_process() as origin:
        s.request(origin.port, target=GUARDED, headers=[att_header(s.attestation(t=GUARDED))])
    record = records_of(origin)[0]
    assert record["eng_post"] is None and record["cmp_key_post_id"] is None


def test_proxy_headers_disabled_even_with_forwarded_allow_ips_star(monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    app, _ = in_process_app()
    config = e0_main.uvicorn_config(app)
    config.load()
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    assert config.proxy_headers is False and not isinstance(config.loaded_app, ProxyHeadersMiddleware)
    with s.origin_process() as origin:
        _, _, body = s.request(origin.port, target="/e0/observe", headers=[("X-Forwarded-For", "203.0.113.50"),
                                                                           ("X-Forwarded-Proto", "https")])
    peer = json.loads(body)["observation"]["peer"]
    assert peer["addr"]["text"] == "127.0.0.1"


def test_plain_http_counter():
    with s.origin_process() as origin:
        for _ in range(2):
            s.request(origin.port, target="/e0/observe", headers=[("X-Forwarded-Proto", "http"),
                                                                  ("x-e0-case", "HTTP-01")])
        s.request(origin.port, target="/e0/observe", headers=[("X-Forwarded-Proto", "https")])
    records = records_of(origin)
    assert [r["plain_count_total"] for r in records] == [1, 2, 2]
    assert [r["plain_count_case"] for r in records] == [1, 2, 0]
    assert origin.records("origin-checkpoint")[-1]["plain_count_by_case"] == {"HTTP-01": 2}


def test_cmp_pre_headers_reaching_the_origin_are_counted():
    with s.origin_process() as origin:
        s.request(origin.port, target="/e0/observe", headers=[("x-e0-cmp-pre-key", "ab" * 32),
                                                              ("x-e0-cmp-pre-mac", "cd" * 32)])
    record = records_of(origin)[0]
    assert record["cmp_pre_headers_at_origin"] == 2
    assert ("ab" * 32) not in origin.stdout and ("cd" * 32) not in origin.stdout


def test_edge_diag_is_marked_unauthenticated_and_filtered():
    diag = at.b64url_encode(json.dumps({"phase": "2", "class": "I-b", "codes": ["worker_marker"], "signed": False,
                                        "ip": "192.0.0.9", "asn": 13335}).encode())
    with s.origin_process() as origin:
        s.request(origin.port, target="/e0/observe", headers=[("x-e0-edge-diag", diag)])
        s.request(origin.port, target="/e0/observe", headers=[("x-e0-edge-diag", "%%%")])
    first, second = (r["edge_diag"] for r in records_of(origin))
    assert first["authenticated"] is False and first["parsed"] is True and "ip" not in first["fields"]
    assert first["fields"]["class"] == "I-b" and first["fields"]["asn"] == 13335
    assert second == {"authenticated": False, "parsed": False, "sha256": hashlib.sha256(b"%%%").hexdigest()}
    assert "192.0.0.9" not in origin.stdout


# --------------------------------------------------------------------------
# /e0/slow : aucun octet anticipé, corps de 384 Kio
# --------------------------------------------------------------------------

def test_slow_get_sends_no_byte_before_the_delay():
    with s.origin_process() as origin:
        sock = socket.create_connection(("127.0.0.1", origin.port), timeout=10)
        started = time.monotonic()
        sock.sendall(f"GET /e0/slow?d=2&n=1000 HTTP/1.1\r\nHost: {s.ORIGIN_HOST}\r\nConnection: close\r\n\r\n"
                     .encode())
        ready, _, _ = select.select([sock], [], [], 1.7)
        assert ready == [], "octet reçu avant l'échéance"
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
        elapsed = time.monotonic() - started
        sock.close()
    status, _, body = s.parse_response(b"".join(chunks))
    assert status == 200 and elapsed >= 2.0 and body == og.deterministic_body(1000)
    assert records_of(origin, "slow")[0]["extra"]["slow"]["client_disconnected"] is False


def test_slow_post_384_kib_body_length_and_digest():
    payload = os.urandom(384 * 1024)
    with s.origin_process() as origin:
        status, _, body = s.request(origin.port, method="POST", target="/e0/slow?d=0", body=payload)
    document = json.loads(body)
    assert status == 200 and document == {"length": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                                          "d": 0}


def test_slow_parameters_and_client_abort():
    with s.origin_process(E0_SLOW_MAX_S="5") as origin:
        bad = [s.request(origin.port, target=t)[0] for t in ("/e0/slow", "/e0/slow?d=6", "/e0/slow?d=1&d=1",
                                                              "/e0/slow?d=01", "/e0/slow?d=1&x=2",
                                                              "/e0/slow?d=1&n=600000")]
        sock = socket.create_connection(("127.0.0.1", origin.port), timeout=10)
        sock.sendall(f"GET /e0/slow?d=3 HTTP/1.1\r\nHost: {s.ORIGIN_HOST}\r\n\r\n".encode())
        time.sleep(0.5)
        sock.close()
        time.sleep(3.5)
    assert bad == [400] * 6
    aborted = records_of(origin, "slow")[-1]
    assert aborted["extra"]["slow"]["client_disconnected"] is True and aborted["status"] is None


def test_body_limit():
    with s.origin_process(E0_BODY_MAX="100") as origin:
        status, _, _ = s.request(origin.port, method="POST", target="/e0/observe", body=b"x" * 101)
    assert status == 413


# --------------------------------------------------------------------------
# Journal chaîné, points de contrôle, taille bornée
# --------------------------------------------------------------------------

def test_log_chain_checkpoints_and_tamper_detection():
    with s.origin_process() as origin:
        for index in range(100):
            s.request(origin.port, target=f"/e0/observe?i={index}")
    lines = origin.log_lines()
    report = jl.verify_chain(lines, expected_bench=origin.startup["bench"])
    assert report["ok"], report["problems"]
    kinds = [json.loads(line)["e0"] for line in lines]
    assert kinds[0] == "origin-startup" and kinds.count("origin-record") == 100
    assert [json.loads(line)["reason"] for line in lines if json.loads(line)["e0"] == "origin-checkpoint"] == \
        ["periodic", "stop"]
    assert all(len(line) + 1 <= 4096 for line in lines)
    assert origin.startup["bench"] == e0_main.bench_identity()
    external = report["head"]
    tampered = list(lines)
    record = json.loads(tampered[5])
    record["status"] = 201
    tampered[5] = jl.canonical(record)
    assert not jl.verify_chain(tampered)["ok"]
    # AC-02 : un journal entièrement rechaîné est cohérent… mais l'empreinte externe le rejette.
    writer = jl.ChainWriter(origin.startup["bench"])
    rechained = []
    for line in tampered:
        body = {k: v for k, v in json.loads(line).items() if k not in ("e0", "fmt", "protocol", "bench", "seq", "prev")}
        rechained.append(writer.append(json.loads(line)["e0"], body))
    assert jl.verify_chain(rechained)["ok"] is True
    rejected = jl.verify_chain(rechained, external_head=external)
    assert rejected["ok"] is False and rejected["external_head"] == "mismatch"
    assert jl.verify_chain(lines, external_head=external)["external_head"] == "match"


def test_oversized_observation_is_reduced_below_4_kib():
    headers = [(f"x-forwarded-for", "10.0.0.1," * 60)] * 6 + [("Referer", "https://e0.example/" + "a" * 500)] * 4
    with s.origin_process() as origin:
        status, _, _ = s.request(origin.port, target="/e0/observe", headers=headers)
    assert status == 200
    record = records_of(origin)[0]
    assert record["oversize"] is True and record["ip_headers"]["omitted"] is True
    assert all(len(line) + 1 <= 4096 for line in origin.log_lines())


def test_canonical_json_rules():
    assert jl.canonical({"b": 1, "a": [True, None, "é"]}) == '{"a":[true,null,"\\u00e9"],"b":1}'
    for bad in ({"x": 1.5}, {1: "x"}, {"x": object()}, {"x": float("nan")}):
        with pytest.raises(jl.CanonicalError):
            jl.canonical(bad)
    writer = jl.ChainWriter("bench")
    with pytest.raises(jl.CanonicalError):
        writer.append("t", {"seq": 3})
    with pytest.raises(jl.LineTooLong):
        jl.ChainWriter("bench", max_line_bytes=100).append("t", {"x": "a" * 200})
    assert jl.line_digest("abc") == hashlib.sha256(b"abc\n").hexdigest()
    report = jl.verify_chain([])
    assert report["ok"] is False and report["count"] == 0


# --------------------------------------------------------------------------
# AT-L05 : concurrence dans un processus
# --------------------------------------------------------------------------

def test_at_l05_in_memory_store_single_accept_under_threads():
    store = at.InMemoryNonceStore()
    barrier = threading.Barrier(16)
    results = []

    def worker():
        barrier.wait()
        results.append(store.consume("oryx-e0-2", "e0-2-0000000000000000", "n", 10, 0))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * 15 + [True]


def test_at_l05_real_server_accepts_one_of_concurrent_presentations():
    value = s.attestation(t=GUARDED)
    statuses = []
    with s.origin_process() as origin:
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            statuses.append(s.request(origin.port, target=GUARDED, headers=[att_header(value)])[0])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert sorted(statuses) == [200] + [503] * 7
    refusals = sorted(str(r["attestation"]["refusal"]) for r in records_of(origin, "guarded"))
    assert refusals == ["None"] + ["replay_in_boot"] * 7


def test_store_capacity_exhaustion_is_a_closed_refusal():
    store = at.InMemoryNonceStore(max_entries=2)
    assert store.consume("a", "k", "1", 100, 0) and store.consume("a", "k", "2", 100, 0)
    assert store.consume("a", "k", "3", 100, 0) is False


# --------------------------------------------------------------------------
# Budget, configuration, refus fermés au démarrage
# --------------------------------------------------------------------------

def test_request_budget_stops_the_server():
    with s.origin_process(E0_MAX_REQUESTS="2") as origin:
        first = s.request(origin.port, target="/e0/health")[0]
        second = s.request(origin.port, target="/e0/health")[0]
        third = s.request(origin.port, target="/e0/health")
        origin.proc.wait(timeout=15)
    assert (first, second, third[0]) == (200, 200, 503)
    assert json.loads(third[2]) == {"error": "e0_budget_exhausted"}


@pytest.mark.parametrize("override", [
    {"E0_PHASE": "3"}, {"E0_PHASE": None}, {"E0_VERIFY_KEYS": "[]"}, {"E0_PUBLIC_HOST": "e0-origin-x.up.railway.app"},
    {"E0_PUBLIC_HOST": "oryx-api-production.example"}, {"E0_TTL_SECONDS": "59"}, {"E0_TTL_SECONDS": "86401"},
    {"E0_W_MS": "1.5"}, {"E0_SLOW_CONCURRENCY": "9"}, {"E0_BODY_MAX": "524289"},
    {"E0_CMP_KEY_POST": POST_KEY_HEX}, {"E0_CMP_KEY_POST": POST_KEY_HEX, "E0_CMP_KEY_POST_ID": "cmp-0000000000000000"},
    {"E0_TLS_CERT_PEM_B64": "AA==", "E0_TLS_KEY_PEM_B64": "AA=="},
    {"E0_ROLE": "prober"},
])
def test_invalid_configuration_is_refused_at_startup(override):
    env = s.origin_environment()
    for name, value in override.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    code, out, err = s.run_origin_once(env)
    assert code in (2, 3) and "E0-ORIGIN" not in out
    assert POST_KEY_HEX not in err


def test_openssl_absence_refuses_to_start(monkeypatch, capsys):
    import e0_ed25519 as ed
    environment = s.origin_environment()  # construit AVANT de retirer l'outil
    monkeypatch.setattr(ed.shutil, "which", lambda *a, **k: None)
    ed._capability = None
    try:
        assert e0_main.run_origin(environment) == 3
    finally:
        monkeypatch.undo()
        ed.capability(refresh=True)
    assert "OpenSSL" in capsys.readouterr().err


def test_tls_mode_is_reserved_to_phase_1c(tmp_path):
    key, cert = s.make_localhost_certificate(tmp_path)
    import base64
    material = {"E0_TLS_CERT_PEM_B64": base64.b64encode(cert.read_bytes()).decode(),
                "E0_TLS_KEY_PEM_B64": base64.b64encode(key.read_bytes()).decode()}
    code, out, _ = s.run_origin_once(s.origin_environment("2", **material))
    assert code == 3 and "E0-ORIGIN" not in out
    with s.origin_process(phase="1c", tls=True, **material) as origin:
        status, _, body = s.request(origin.port, target="/e0/health", tls_context=s.client_tls_context(cert))
        plain = b"GET /e0/health HTTP/1.1\r\nHost: x.example\r\nConnection: close\r\n\r\n"
        try:
            answer = s.raw_exchange(origin.port, plain, timeout=5)
        except OSError:
            answer = b""
        assert not answer.startswith(b"HTTP/1.1 200")  # requête en clair jamais servie par l'origine TLS
    assert status == 200 and json.loads(body)["status"] == "ok" and origin.startup["tls"] is True
    assert not list(tmp_path.glob("e0-tls-*"))
    shm = "/dev/shm"
    if os.path.isdir(shm):
        assert not [n for n in os.listdir(shm) if n.startswith("e0-tls-")]
