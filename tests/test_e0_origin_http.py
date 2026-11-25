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
    # valeur illisible : empreinte HMAC du démarrage, jamais un SHA-256 nu (renversable si c'est une IP)
    assert set(second) == {"authenticated", "parsed", "hmac_boot"} and second["parsed"] is False
    assert len(second["hmac_boot"]) == 32 and hashlib.sha256(b"%%%").hexdigest() not in origin.stdout
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
    assert record["evidence_completeness"] == "truncated" and "ip_headers" in record["omitted"]
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


# ==========================================================================
# Audit PC-1 R1 — non-régression des trois défauts P1
# ==========================================================================

GLOBAL_V4 = "8.8.8.8"
GLOBAL_V6 = "2001:4860:4860::8888"


def _ip_forms(ip):
    """Formes sous lesquelles une adresse brute pourrait fuir."""
    forms = {ip, ip.replace(".", "%2E"), ip.replace(".", "%2e"), ip.replace(":", "%3A"), ip.replace(":", "%3a")}
    if "." in ip:
        forms.add(".".join(part.zfill(3) for part in ip.split(".")))
    return {f for f in forms if f}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def assert_no_raw_ip(text_blobs, records, ips=(GLOBAL_V4, GLOBAL_V6)):
    """Aucune forme brute ni encodée de l'adresse : texte, JSON, et décodage
    base64url de chaque `target_b64` (l'encodage n'est pas une anonymisation)."""
    decoded = []
    for record in records:
        for blob in _strings(record):
            decoded.append(blob)
        if isinstance(record.get("target_b64"), str):
            decoded.append(at.b64url_decode_canonical(record["target_b64"]).decode("ascii"))
    corpus = "\n".join(list(text_blobs) + decoded)
    for ip in ips:
        for form in _ip_forms(ip):
            assert form not in corpus, form
        assert at.b64url_encode(ip.encode()) not in corpus


@pytest.mark.parametrize("ip", [GLOBAL_V4, GLOBAL_V6])
@pytest.mark.parametrize("header", ["x-e0-run", "x-e0-seq", "x-e0-case"])
def test_p1a_declarative_correlation_values_never_persist_a_global_ip(header, ip):
    """Reproduction de l'audit : `x-e0-run: 8.8.8.8` apparaissait en clair."""
    with s.origin_process() as origin:
        status, _, body = s.request(origin.port, target="/e0/observe", headers=[(header, ip)])
    assert status == 200
    record = records_of(origin)[0]
    field = {"x-e0-run": "run", "x-e0-seq": "run_seq", "x-e0-case": "test_id"}[header]
    value = record[field]
    assert value["invalid"] is True and len(value["hmac_boot"]) == 32 and header in record["redactions"]
    if header == "x-e0-case":
        assert record["plain_count_case"] == 0  # jamais une clé de compteur
    assert_no_raw_ip([origin.stdout, body.decode()], records_of(origin) + [json.loads(body)["observation"]])


def test_p1a_valid_correlation_identifiers_stay_exploitable():
    with s.origin_process() as origin:
        s.request(origin.port, target="/e0/observe", headers=[("x-e0-run", "run-7f3a.2"), ("x-e0-seq", "12"),
                                                              ("x-e0-case", "IP-01"), ("x-e0-run", "dup")])
        s.request(origin.port, target="/e0/observe", headers=[("x-e0-run", "203.0.113.9"), ("x-e0-case", "IP-01")])
    first, second = records_of(origin)
    assert first["run"]["invalid"] is True and first["run"]["count"] == 2  # doublon : jamais exploitable
    assert (first["run_seq"], first["test_id"]) == ("12", "IP-01")
    assert second["run"] == "203.0.113.9"  # documentation : objet de la mesure, autorisé en clair
    assert second["redactions"] == []


@pytest.mark.parametrize("target", [
    "/e0/observe/8.8.8.8", "/e0/observe?ip=8.8.8.8", "/e0/observe?ip=8%2E8%2E8%2E8", "/e0/observe?ip=8%252E8%252E8%252E8",
    "/e0/observe?ip=008.008.008.008", "/x/2001:4860:4860::8888", "/x?v6=2001%3A4860%3A4860%3A%3A8888",
    "/e0/guarded/" + TID + "?ip=8.8.8.8",
])
def test_p1a_targets_and_queries_never_persist_a_global_ip(target):
    with s.origin_process() as origin:
        status, _, body = s.request(origin.port, target=target)
    record = records_of(origin)[0]
    assert record["target_redacted"] is True and "target" in record["redactions"]
    assert len(record["target_hmac_boot"]) == 32 and record["target_len"] == len(target)
    blobs = [origin.stdout] + ([body.decode()] if status == 200 else [])
    observations = [json.loads(body)["observation"]] if status == 200 else []
    assert_no_raw_ip(blobs, records_of(origin) + observations)


def test_p1a_safe_target_keeps_correlation_and_guarded_prefix():
    target = "/e0/guarded/" + TID + "?ip=8.8.8.8"
    with s.origin_process() as origin:
        s.request(origin.port, target=target)
    record = records_of(origin)[0]
    safe = at.b64url_decode_canonical(record["target_b64"]).decode()
    assert safe.startswith("/e0/guarded/" + TID + "?ip=g4:") and record["tid"] == TID


def test_p1a_attestation_target_with_ip_is_verified_raw_and_persisted_redacted():
    target = "/e0/guarded/" + TID + "?ip=8.8.8.8"
    valid = s.attestation(t=target)
    with s.origin_process() as origin:
        accepted = s.request(origin.port, target=target, headers=[att_header(valid)])
        # La rédaction ne rend jamais conforme une cible différente de la cible signée :
        disguised = s.request(origin.port, target="/e0/guarded/" + TID + "?ip=g4:0000000000000000",
                              headers=[att_header(s.attestation(t=target))])
        forged = s.attestation(seed=s.RFC_SEED_OTHER, t=target, kid=s.kid_of(s.RFC_SEED_2, "oryx-e0-2"))
        refused = s.request(origin.port, target=target, headers=[att_header(forged)])
    assert accepted[0] == 200 and disguised[0] == 503 and refused[0] == 503
    first, second, third = records_of(origin, "guarded")
    assert first["attestation"]["att_policy_accepted"] is True and "attestation.t" in first["redactions"]
    assert first["attestation"]["t"].startswith("/e0/guarded/" + TID + "?ip=g4:")
    assert second["attestation"]["refusal"] == "bind_target" and not second["attestation"]["att_policy_accepted"]
    assert third["attestation"]["att_crypto_valid"] is False and third["attestation"]["refusal"] == "sig_invalid"
    assert_no_raw_ip([origin.stdout], records_of(origin))


def test_p1a_headers_cookies_small_bodies_and_diagnostics_never_persist_a_global_ip():
    diag = at.b64url_encode(json.dumps({"class": "V", "codes": ["8.8.8.8"], "phase": "2001:4860:4860::8888"}).encode())
    with s.origin_process() as origin:
        status, _, body = s.request(
            origin.port, method="POST", target="/e0/observe", body=b'{"ip":"8.8.8.8"}',
            headers=[("Referer", "https://e0.example/?u=8%2E8%2E8%2E8"), ("Origin", "https://8.8.8.8"),
                     ("Sec-Fetch-Site", "2001:4860:4860::8888"), ("X-Forwarded-Proto", "8.8.8.8"),
                     ("CF-Worker", "008.008.008.008"), ("X-Forwarded-For", "8%2E8%2E8%2E8"),
                     ("Cookie", "8.8.8.8"), ("x-e0-edge-diag", diag), ("x-e0-edge-diag-extra", "x")])
        raw_edge = s.request(origin.port, target="/e0/observe", headers=[("x-e0-edge-diag", "8.8.8.8")])
    assert status == 200 and raw_edge[0] == 200
    record = records_of(origin)[0]
    assert {"referer", "origin", "sec-fetch-site", "xfp", "cf-worker", "x-forwarded-for", "cookie",
            "edge_diag"} <= set(record["redactions"])
    assert "sha256" not in record["cookie"] and len(record["cookie"]["hmac_boot"]) == 32
    assert record["body"]["redacted"] is True and "sha256" not in record["body"]
    assert_no_raw_ip([origin.stdout, body.decode(), raw_edge[2].decode()],
                     records_of(origin) + [json.loads(body)["observation"]])


def test_p1a_large_random_body_keeps_its_plain_sha256():
    payload = os.urandom(64 * 1024)
    with s.origin_process() as origin:
        s.request(origin.port, method="POST", target="/e0/observe", body=payload)
    assert records_of(origin)[0]["body"]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_p1a_redactor_detects_encoded_and_lenient_forms():
    from e0_redact import Redactor
    r = Redactor()
    for value in ("8.8.8.8", "x8.8.8.8y", "8%2E8%2E8%2E8", "8%252E8%252E8%252E8", "008.008.008.008",
                  "[2001:4860:4860::8888]:443", "2001%3A4860%3A4860%3A%3A8888", "for=8.8.8.8;proto=https"):
        assert r.contains_sensitive(value), value
        text, sensitive = r.safe_text(value)
        assert sensitive and "8.8.8.8" not in text and "8888" not in text.replace("g6:", "")[:0] + text.split("g6:")[0]
    for value in ("run-1", "203.0.113.9", "10.0.0.1", "/e0/guarded/" + TID, "e0-origin-a.up.railway.app:443",
                  "https://app.e0-zone.example/x"):
        assert not r.contains_sensitive(value), value
        assert r.safe_text(value) == (value, False)


# ----- P1-B : journalisation garantie et bornée -----

def _huge(n, char="a"):
    return char * n


def test_p1b_target_over_4000_chars_still_produces_a_bounded_record():
    target = "/e0/observe?" + _huge(4100)
    with s.origin_process() as origin:
        status, _, _ = s.request(origin.port, target=target)
    assert status == 200
    records = records_of(origin)
    assert len(records) == 1 and records[0]["evidence_completeness"] in ("truncated", "unusable")
    assert records[0]["target_len"] == len(target)
    assert all(len(line) + 1 <= 4096 for line in origin.log_lines())
    assert jl.verify_chain(origin.log_lines())["ok"]


def test_p1b_oversized_guarded_target_keeps_the_exact_refusal_and_a_record():
    target = "/e0/guarded/" + TID + "?" + _huge(4100)
    with s.origin_process() as origin:
        status, headers, body = s.request(origin.port, target=target)
    assert (status, body) == (503, ver.REFUSAL_BODY)
    record = records_of(origin, "guarded")[0]
    assert record["status"] == 503 and record["attestation"]["refusal"] == "att_absent"
    assert record["evidence_completeness"] in ("truncated", "unusable")
    assert all(len(line) + 1 <= 4096 for line in origin.log_lines())


def test_p1b_large_malformed_attestation_is_recorded_and_refused():
    with s.origin_process() as origin:
        status, _, _ = s.request(origin.port, target=GUARDED, headers=[att_header("v1." + _huge(3900, "A") + ".x")])
    record = records_of(origin, "guarded")[0]
    assert status == 503 and record["attestation"]["refusal"] == "grammar"
    assert record["attestation"]["att_policy_accepted"] is False


def test_p1b_several_large_fields_at_once():
    headers = [("Referer", "https://e0.example/" + _huge(3000)), ("Origin", "https://e0.example/" + _huge(3000)),
               (ver.ATTESTATION_HEADER, "v1." + _huge(3000, "B")), ("x-e0-run", _huge(3000, "r")),
               ("X-Forwarded-For", "10.0.0.1," * 300)]
    with s.origin_process() as origin:
        status, _, _ = s.request(origin.port, target=GUARDED + "?" + _huge(2500), headers=headers)
    assert status == 503
    lines = origin.log_lines()
    assert len(records_of(origin)) == 1 and all(len(line) + 1 <= 4096 for line in lines)
    assert jl.verify_chain(lines)["ok"]


def _app_with_emit(emit, clock=None):
    config = og.config_from_environment(s.origin_environment())
    return og.OriginApp(config, bench="sha256-tree:test", emit=emit, clock_ms=clock)


def test_p1b_first_write_failure_is_retried_once_and_the_chain_stays_intact():
    lines, failures = [], {"left": 1}

    def flaky(line):
        if failures["left"] and '"origin-record"' in line:
            failures["left"] -= 1
            raise OSError("EAGAIN simulé")
        lines.append(line)

    app = _app_with_emit(flaky)
    sent = call(app, path="/e0/observe")
    assert sent[0]["status"] == 200
    assert [json.loads(l)["e0"] for l in lines] == ["origin-record"] and app.emit_retries == 1
    assert jl.verify_chain(lines)["ok"]
    app.checkpoint("stop")
    assert json.loads(lines[-1])["emit_retries"] == 1


@pytest.mark.parametrize("path, valid", [("/e0/observe", False), (GUARDED, True), (GUARDED, False)])
def test_p1b_lost_collection_never_announces_success(path, valid):
    lines, stopped = [], []

    def broken(line):
        if '"origin-record"' in line:
            raise OSError("stdout fermé")
        lines.append(line)

    app = _app_with_emit(broken)
    app.on_exhausted = lambda: stopped.append(True)
    headers = [att_header(s.attestation(t=path))] if valid else []
    sent = call(app, path=path, headers=headers)
    assert sent[0]["status"] == 503 and json.loads(sent[1]["body"]) == {"error": "e0_collection_failed"}
    assert stopped and app.collection_failures == 1 and app.collection_broken
    assert lines == [] and app.writer.seq == 0  # aucune ligne fantôme dans la chaîne
    again = call(app, path="/e0/health")
    assert again[0]["status"] == 503


def test_p1b_unusable_form_is_bounded_by_construction_and_marks_incompleteness():
    app, lines = in_process_app()
    giant = {k: _huge(5000) for k in ("route", "run", "run_seq", "test_id", "tid", "eng_post", "cmp_key_post_id")}
    giant.update({"boot_id": app.boot_id, "status": 200, "origin_received_ms": 1, "origin_logged_ms": 2,
                  "attestation": {"att_present": True, "att_crypto_valid": True, "att_policy_accepted": False,
                                  "key_origin": _huge(5000), "refusal": _huge(5000), "kid": _huge(5000),
                                  "nonce": _huge(5000), "signed_ip": {"text": _huge(5000)}},
                  "target_b64": _huge(9000), "referer": [_huge(500)] * 20})
    minimal = app._unusable(giant)
    assert len(app.writer.build("origin-record", minimal)) + 1 <= 4096
    assert minimal["evidence_completeness"] == "unusable" and minimal["record_sha256"]
    assert minimal["attestation"]["att_crypto_valid"] is True  # jamais embelli ni effacé
    app._log_request(giant)
    assert json.loads(lines[-1])["evidence_completeness"] in ("truncated", "unusable")


def test_p1b_reduced_records_never_upgrade_the_evidence():
    with s.origin_process() as origin:
        forged = s.attestation(seed=s.RFC_SEED_OTHER, t=GUARDED + "?" + _huge(1900),
                               kid=s.kid_of(s.RFC_SEED_2, "oryx-e0-2"))
        s.request(origin.port, target=GUARDED + "?" + _huge(1900), headers=[att_header(forged)])
    record = records_of(origin)[0]
    assert record["evidence_completeness"] != "complete"
    assert record["attestation"]["att_crypto_valid"] is False and record["attestation"]["att_policy_accepted"] is False
    assert origin.records("origin-checkpoint")[-1]["reduced_records"] != {"truncated": 0, "unusable": 0}


def test_p1b_complete_records_are_marked_complete():
    with s.origin_process() as origin:
        s.request(origin.port, target="/e0/observe")
    assert records_of(origin)[0]["evidence_completeness"] == "complete"


# ----- P1-C : horodatage à la réception -----

def test_p1c_reception_time_is_captured_before_any_processing():
    ticks = iter(range(1_763_200_000_000, 1_763_200_100_000, 1_000))
    reads = []

    def clock():
        value = next(ticks)
        reads.append(value)
        return value

    lines = []
    app = _app_with_emit(lines.append, clock=clock)
    first_read_before_call = len(reads)
    signed = s.attestation(t=GUARDED, ts_ms=1_763_199_990_000, method="POST")
    sent = call(app, method="POST", path=GUARDED, headers=[att_header(signed)], body=b"corps")
    record = json.loads(lines[-1])
    assert sent[0]["status"] == 200 and record["attestation"]["att_policy_accepted"] is True
    # réception = PREMIÈRE lecture de l'horloge, avant la vérification (qui lit l'horloge ensuite),
    # la consommation du nonce, la lecture du corps et l'envoi
    assert record["origin_received_ms"] == reads[first_read_before_call]
    assert len(reads) - first_read_before_call >= 3   # réception, vérification, journalisation
    assert record["origin_logged_ms"] == reads[-1] > record["origin_received_ms"]
    assert record["attestation"]["ts"] == "1763199990000"  # indépendant : ts reste la valeur signée
    assert record["body"]["length"] == 5


def test_p1c_reception_time_precedes_the_slow_wait_on_the_real_server():
    with s.origin_process() as origin:
        before = int(time.time() * 1000)
        s.request(origin.port, target="/e0/slow?d=1&n=1")
        after = int(time.time() * 1000)
        valid = s.attestation(t="/e0/observe")
        s.request(origin.port, target="/e0/observe", headers=[att_header(valid)])
    slow, observe = records_of(origin)
    assert before - 50 <= slow["origin_received_ms"] <= before + 500
    assert slow["origin_logged_ms"] - slow["origin_received_ms"] >= 1000 and slow["origin_logged_ms"] <= after + 50
    at_r01 = observe["origin_received_ms"] - int(observe["attestation"]["ts"])  # AT-R01 calculable
    assert -5000 <= at_r01 <= 5000


@pytest.mark.parametrize("encoded", [GLOBAL_V4, "8%2E8%2E8%2E8", "008.008.008.008", GLOBAL_V6,
                                     "2001%3A4860%3A4860%3A%3A8888", "8%252E8%252E8%252E8"])
def test_p1a_sweep_every_observed_field_and_every_output(encoded):
    """Balayage : la même adresse dans chaque en-tête observé, la méthode, l'hôte
    et la cible ; aucune ligne persistée (démarrage, enregistrements, points de
    contrôle) ni aucune réponse ne la contient, sous aucune forme."""
    names = (list(og.IP_HEADERS) + list(og.MARKER_HEADERS) + list(og.SEC_FETCH)
             + ["origin", "referer", "x-forwarded-proto", "cookie", "x-e0-run", "x-e0-seq", "x-e0-case",
                "x-e0-edge-diag", "x-e0-cmp-pre-key", "x-e0-cmp-pre-mac", ver.ATTESTATION_HEADER])
    headers = [(name, encoded) for name in names]
    with s.origin_process() as origin:
        responses = [s.request(origin.port, target="/e0/observe/" + encoded + "?q=" + encoded, headers=headers),
                     s.request(origin.port, target="/e0/guarded/" + TID + "/" + encoded, headers=headers),
                     s.request(origin.port, method="POST", target="/e0/slow?d=0", headers=headers,
                               body=encoded.encode()),
                     s.request(origin.port, target="/e0/observe", host=encoded)]
        try:
            responses.append(s.request(origin.port, method=encoded.replace("%", "") or "X", target="/e0/observe"))
        except (IndexError, ValueError):
            pass  # méthode refusée par le serveur HTTP : rien n'atteint l'origine
    texts = [origin.stdout] + [body.decode("latin-1") for _, _, body in responses]
    observations = []
    for status, _, body in responses:
        if status == 200 and body.startswith(b'{"observation"'):
            observations.append(json.loads(body)["observation"])
    lines = origin.log_lines()
    assert jl.verify_chain(lines)["ok"] and all(len(line) + 1 <= 4096 for line in lines)
    assert_no_raw_ip(texts, [json.loads(line) for line in lines] + observations)
    assert all(r["redactions"] for r in records_of(origin)[:4])  # les quatre requêtes porteuses de l'adresse


# ==========================================================================
# Audit PC-1 R2 — annulation de la tâche ASGI (asyncio.CancelledError)
# ==========================================================================

def _run_and_cancel(app, *, method, path, first_chunk, more_body):
    """Lance la requête comme une vraie tâche asyncio, attend qu'elle soit
    suspendue (lecture du corps ou attente /e0/slow), puis l'ANNULE — ce n'est
    pas un `http.disconnect` : `receive()` n'en émet jamais."""
    raw_path, _, query = path.partition("?")
    scope = {"type": "http", "method": method, "path": raw_path, "raw_path": raw_path.encode(),
             "query_string": query.encode(), "http_version": "1.1", "scheme": "http",
             "headers": [(b"host", s.ORIGIN_HOST.encode())], "client": ("127.0.0.1", 50000)}
    sent, outcome = [], {}

    async def main():
        suspended = asyncio.Event()
        chunks = [{"type": "http.request", "body": first_chunk, "more_body": more_body}]

        async def receive():
            if chunks:
                return chunks.pop(0)
            suspended.set()
            await asyncio.Event().wait()  # bloque indéfiniment : jamais de http.disconnect

        async def send(message):
            sent.append(message)

        task = asyncio.ensure_future(app(scope, receive, send))
        await asyncio.wait_for(suspended.wait(), timeout=5)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
            outcome["result"] = "returned"
        except asyncio.CancelledError:
            outcome["result"] = "cancelled"

    asyncio.run(main())
    return sent, outcome["result"]


@pytest.mark.parametrize("case", ["body_read_observe", "body_read_guarded_accepted", "slow_wait_get",
                                  "slow_body_read_post"])
def test_r2_cancelled_task_still_produces_a_bounded_cancelled_record(case):
    ticks = iter(range(1_763_200_000_000, 1_763_200_900_000, 1_000))
    lines = []
    app = _app_with_emit(lines.append, clock=lambda: next(ticks))
    if case == "body_read_observe":
        kwargs = dict(method="POST", path="/e0/observe?ip=8.8.8.8", first_chunk=b'{"ip":"8.8.8.8"', more_body=True)
    elif case == "body_read_guarded_accepted":
        kwargs = dict(method="POST", path=GUARDED, first_chunk=b"x", more_body=True)
    elif case == "slow_wait_get":
        kwargs = dict(method="GET", path="/e0/slow?d=30&n=10", first_chunk=b"", more_body=False)
    else:
        kwargs = dict(method="POST", path="/e0/slow?d=30", first_chunk=b"abc", more_body=True)
    if case == "body_read_guarded_accepted":
        signed = s.attestation(t=GUARDED, ts_ms=1_763_200_000_000, method="POST")
        original_call = app.__call__

        async def with_attestation(scope, receive, send):
            scope["headers"] = scope["headers"] + [(ver.ATTESTATION_HEADER.encode(), signed.encode())]
            return await original_call(scope, receive, send)

        target = with_attestation
    else:
        target = app
    sent, result = _run_and_cancel(target, **kwargs)
    assert result == "cancelled"                        # l'annulation est PROPAGÉE
    assert sent == []                                   # aucune réponse, jamais un succès HTTP
    records = [json.loads(line) for line in lines if json.loads(line)["e0"] == "origin-record"]
    assert len(records) == 1                            # l'observation existe malgré l'annulation
    record = records[0]
    assert record["processing"] == "cancelled" and record["status"] is None
    assert record["evidence_completeness"] != "complete"
    assert record["origin_received_ms"] == 1_763_200_000_000
    assert all(len(line) + 1 <= 4096 for line in lines) and jl.verify_chain(lines)["ok"]
    assert_no_raw_ip(lines, records)
    if case == "body_read_guarded_accepted":
        assert record["attestation"]["att_policy_accepted"] is True  # observé, mais aucune réponse émise
    app.checkpoint("stop")
    assert json.loads(lines[-1])["cancelled_requests"] == 1


def test_r2_body_redaction_is_reflected_in_the_record_privacy_metadata():
    lines = []
    app = _app_with_emit(lines.append)
    call(app, method="POST", path="/e0/observe", body=b'{"ip":"8.8.8.8"}')
    record = json.loads(lines[-1])
    assert record["body"]["redacted"] is True and "body" in record["redactions"]
    call(app, method="POST", path="/e0/guarded", body=b"8.8.8.8")  # refusé : corps non lu, rien à rédiger
    assert "body" not in json.loads(lines[-1])["redactions"]


def test_r2_completed_requests_are_marked_completed():
    lines = []
    app = _app_with_emit(lines.append)
    call(app, path="/e0/observe")
    assert json.loads(lines[-1])["processing"] == "completed"


def test_r2_partial_stdout_write_then_retry_is_detected_never_claimed_unique():
    """Limite consignée : une écriture partiellement réussie suivie d'une
    exception, puis la nouvelle tentative, laissent un fragment avant la ligne
    complète. Le banc ne revendique AUCUNE unicité : la vérification de chaîne
    signale l'anomalie (collecte non intègre), elle ne l'efface pas."""
    stream, state = [], {"first": True}

    def partial_then_ok(line):
        if state["first"] and '"origin-record"' in line:
            state["first"] = False
            stream.append(line[:40])  # fragment réellement écrit
            raise OSError("écriture partielle")
        stream.append(line)

    app = _app_with_emit(partial_then_ok)
    sent = call(app, path="/e0/observe")
    assert sent[0]["status"] == 200 and app.emit_retries == 1
    report = jl.verify_chain(stream)
    assert report["ok"] is False and any("illisible" in p for p in report["problems"])
    complete = [l for l in stream if l.startswith("{") and l.endswith("}")]
    assert jl.verify_chain(complete)["ok"] is True  # la ligne engagée reste intacte et chaînée
