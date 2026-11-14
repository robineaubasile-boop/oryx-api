"""ACT-L1 / F04-D / P1-Q — récepteur de diagnostic derrière un VRAI serveur
Uvicorn 0.27.0 (version verrouillée de production), en boucle locale.

Ces tests sont la partie de Q4 / Q7 réalisable SANS Railway : ils montrent ce
que voit l'application selon la configuration d'Uvicorn quand le pair TCP est
127.0.0.1. Ils ne qualifient NI l'edge Railway NI le réseau privé : ces essais
restent NON EXÉCUTÉS (docs/act_l1_f04d_p1q_protocol_v01.md § 8).
"""
import hashlib
import hmac
import ipaddress
import json
import secrets

from tests.p1q_support import p1q_receiver, raw_request, observe, receiver_process

F4A, F4B, F4C = "203.0.113.7", "198.51.100.23", "192.0.2.44"
GLOBAL_SAMPLES = ("8.8.8.8", "1.1.1.1", "9.9.9.9", "8.8.4.4", "2606:4700:4700::1111")
FORGED = [f"X-Real-IP: {F4A}", f"X-Forwarded-For: {F4B}", f"Forwarded: for={F4C}", "X-Forwarded-Proto: https"]


def test_strict_mode_keeps_the_tcp_peer_whatever_the_proxy_headers():
    with receiver_process("strict") as rx:
        doc, _ = observe(rx.port, FORGED)
    assert doc["mode"] == "strict" and doc["uvicorn"]["proxy_headers"] is False
    assert doc["uvicorn"]["version"] == "0.27.0"
    assert doc["raw_client"]["text"] == doc["effective_client"]["text"] == "127.0.0.1"
    assert doc["effective_equals_raw"] is True
    assert doc["scheme"] == "http"  # X-Forwarded-Proto ignoré
    assert [v["text"] for v in doc["x_real_ip"]["values"]] == [F4A]


def test_strict_mode_is_independent_of_forwarded_allow_ips_env():
    with receiver_process("strict", FORWARDED_ALLOW_IPS="*") as rx:
        doc, _ = observe(rx.port, FORGED + [f"X-Forwarded-For: {F4A}"])
    assert doc["uvicorn"]["forwarded_allow_ips_env_present"] is True
    assert doc["uvicorn"]["forwarded_allow_ips_env"] == "*"
    assert doc["uvicorn"]["proxy_middleware_installed"] is False
    assert doc["effective_equals_raw"] is True and doc["effective_client"]["text"] == "127.0.0.1"


def test_uvicorn_defaults_rewrite_the_client_from_a_forged_xff_when_the_peer_is_loopback():
    """Témoin du risque F04-A § 7.1 : réglages par défaut (ceux d'api.py) avec
    un pair 127.0.0.1 : scope["client"] vient de l'en-tête forgé, y compris
    une valeur non IP, et un doublon est résolu par le DERNIER en-tête."""
    with receiver_process("uvicorn_defaults") as rx:
        forged, _ = observe(rx.port, [f"X-Forwarded-For: {F4A}"])
        not_ip, _ = observe(rx.port, ["X-Forwarded-For: attacker"])
        dup, _ = observe(rx.port, [f"X-Forwarded-For: {F4A}", f"X-Forwarded-For: {F4B}"])
        proto, _ = observe(rx.port, ["X-Forwarded-Proto: https"])
    assert forged["uvicorn"]["proxy_headers"] is True and forged["uvicorn"]["forwarded_allow_ips"] == "127.0.0.1"
    assert forged["raw_client"]["text"] == "127.0.0.1"
    assert forged["effective_client"]["text"] == F4A and forged["effective_equals_raw"] is False
    assert not_ip["effective_client"] == {"parsed": False, "text": "attacker", "length": 8, "port": 0}
    assert dup["effective_client"]["text"] == F4B
    assert proto["scheme"] == "https" and proto["effective_equals_raw"] is True


def test_uvicorn_defaults_do_not_trust_a_non_listed_peer_even_with_env_unset():
    """FORWARDED_ALLOW_IPS absent : seul 127.0.0.1 est approuvé. La simulation
    du middleware RÉEL sur un pair non approuvé ne réécrit rien."""
    import asyncio

    scope = {"type": "http", "headers": [(b"x-forwarded-for", F4A.encode())], p1q_receiver.RAW_CLIENT_KEY:
             ("10.250.0.3", 4000)}
    client, _ = asyncio.run(p1q_receiver._simulate_proxy_headers(scope, "127.0.0.1"))
    assert client == ("10.250.0.3", 4000)
    client_all, _ = asyncio.run(p1q_receiver._simulate_proxy_headers(scope, "*"))
    assert client_all == (F4A, 0)


def test_negative_control_trusts_everything_and_is_detected():
    with receiver_process("trust_all_negative_control") as rx:
        doc, _ = observe(rx.port, [f"X-Forwarded-For: {F4A}, {F4B}"])
    assert doc["uvicorn"]["forwarded_allow_ips"] == "*"
    assert doc["effective_client"]["text"] == F4A  # premier élément avec « * »
    assert doc["effective_equals_raw"] is False


def test_simulation_predicts_the_real_middleware():
    lines = [f"X-Forwarded-For: {F4A}, {F4B}"]
    with receiver_process("strict") as rx:
        strict, _ = observe(rx.port, lines)
    with receiver_process("uvicorn_defaults") as rx:
        defaults, _ = observe(rx.port, lines)
    with receiver_process("trust_all_negative_control") as rx:
        trust_all, _ = observe(rx.port, lines)
    sim = strict["simulation"]
    assert sim["uvicorn_trust_127_0_0_1"]["client"]["text"] == defaults["effective_client"]["text"] == F4B
    assert sim["uvicorn_trust_all"]["client"]["text"] == trust_all["effective_client"]["text"] == F4A


def test_ambiguous_inputs_are_reported_exactly_as_received():
    with receiver_process("strict") as rx:
        dup, _ = observe(rx.port, [f"X-Real-IP: {F4A}", f"X-Real-IP: {F4B}"])
        folded, _ = observe(rx.port, [f"X-Real-IP: {F4A}", f" {F4B}"])
        latin, _ = observe(rx.port, [f"X-Real-IP: {F4A}é"])
        mapped, _ = observe(rx.port, ["X-Real-IP: ::ffff:203.0.113.7"])
        upper, _ = observe(rx.port, ["X-Real-IP: 2001:DB8:0:0:0:0:0:7"])
        spaced, _ = observe(rx.port, [f"X-Real-IP:    {F4A}   "])
    assert dup["x_real_ip"]["count"] == 2 and [v["text"] for v in dup["x_real_ip"]["values"]] == [F4A, F4B]
    assert dup["header_names"].count("x-real-ip") == 2
    # h11 0.16 accepte l'obs-fold et joint les lignes par une espace : valeur illisible.
    assert folded["x_real_ip"]["values"] == [{"parsed": False, "text": f"{F4A} {F4B}", "length": 25}]
    assert latin["x_real_ip"]["values"][0]["parsed"] is False
    assert mapped["x_real_ip"]["values"][0]["category"] == "ipv4_mapped"
    assert mapped["x_real_ip"]["values"][0]["mapped_inner"]["text"] == F4A
    assert upper["x_real_ip"]["values"][0]["canonical"] is False
    assert upper["x_real_ip"]["values"][0]["text"] == "2001:db8::7"
    # h11 retire les espaces autour de la valeur : l'application ne les voit pas.
    assert spaced["x_real_ip"]["values"][0]["text"] == F4A


def test_no_global_address_cookie_or_token_leaves_the_receiver():
    secret_cookie, secret_token = "sess-" + secrets.token_hex(8), "tok-" + secrets.token_hex(8)
    lines = [f"X-Real-IP: {GLOBAL_SAMPLES[0]}", f"X-Forwarded-For: {GLOBAL_SAMPLES[1]}, {GLOBAL_SAMPLES[2]}",
             f'Forwarded: for="[{GLOBAL_SAMPLES[4]}]:443"', f"Cookie: __Host-x={secret_cookie}",
             f"Authorization: Bearer {secret_token}", f"User-Agent: probe/{GLOBAL_SAMPLES[3]}",
             f"X-Envoy-External-Address: {GLOBAL_SAMPLES[0]}:8443"]
    with receiver_process("uvicorn_defaults") as rx:
        doc, body = observe(rx.port, lines)
    text = body.decode("ascii")
    for needle in GLOBAL_SAMPLES + (secret_cookie, secret_token, "2606:4700"):
        assert needle not in text, needle
        assert needle not in rx.stdout and needle not in rx.stderr, needle
    # Réécriture par défaut (pair 127.0.0.1) : valeur pseudonymisée, jamais en clair.
    assert doc["effective_client"]["text"].startswith("g4:")
    assert doc["effective_client"]["text"] == doc["x_forwarded_for"]["entries"][0][1]["text"]
    assert doc["x_real_ip"]["values"][0]["text"] != doc["effective_client"]["text"]
    assert doc["sensitive_headers_present"] == {"cookie": 1, "authorization": 1}
    assert doc["other_headers"]["user-agent"] == {"count": 1, "max_length": len(f"probe/{GLOBAL_SAMPLES[3]}")}
    # Aucune sortie par requête : seule la ligne de démarrage.
    assert [line for line in rx.stdout.splitlines() if line.strip()] == [rx.stdout.splitlines()[0]]
    assert rx.stdout.startswith("P1Q-STARTUP ")


def test_pseudonyms_are_stable_within_a_boot_and_unlinkable_across_boots():
    line = [f"X-Real-IP: {GLOBAL_SAMPLES[0]}"]
    with receiver_process("strict") as rx:
        a, _ = observe(rx.port, line)
        b, _ = observe(rx.port, line)
    with receiver_process("strict") as rx:
        c, _ = observe(rx.port, line)
    assert a["x_real_ip"]["values"][0]["text"] == b["x_real_ip"]["values"][0]["text"]
    assert a["boot_id"] == b["boot_id"] != c["boot_id"]
    assert a["x_real_ip"]["values"][0]["text"] != c["x_real_ip"]["values"][0]["text"]


def test_self_commitment_matches_without_the_address_in_clear():
    key = secrets.token_bytes(32)

    def mac(text):
        return hmac.new(key, ipaddress.ip_address(text).packed, hashlib.sha256).hexdigest()

    lines = [f"X-P1Q-Self-Key: {key.hex()}", f"X-P1Q-Self-Mac: {mac('127.0.0.1')},{mac(F4A)}",
             f"X-Real-IP: {F4A}", f"X-Forwarded-For: {F4B}, {F4A}"]
    with receiver_process("strict") as rx:
        doc, body = observe(rx.port, lines)
        bad, _ = observe(rx.port, [f"X-P1Q-Self-Key: {key.hex()}", "X-P1Q-Self-Mac: zz"])
        absent, _ = observe(rx.port, [])
    assert doc["self_match"] == {"valid": True, "raw_client": True, "x_real_ip": [True],
                                 "x_forwarded_for": [[False, True]]}
    assert key.hex() not in body.decode() and mac(F4A) not in body.decode()
    assert doc["sensitive_headers_present"] == {"x-p1q-self-key": 1, "x-p1q-self-mac": 1}
    assert bad["self_match"] == {"valid": False}
    assert absent["self_match"] is None


def test_routes_methods_and_cache_policy_are_closed():
    with receiver_process("strict", P1Q_MAX_REQUESTS="2") as rx:
        health = raw_request(rx.port, [], path="/p1q/health")
        unknown = raw_request(rx.port, [], path="/")
        post = raw_request(rx.port, ["Content-Length: 0"], method="POST")
        head = raw_request(rx.port, [], method="HEAD")
        ok = raw_request(rx.port, [])
        exhausted = raw_request(rx.port, [])
    assert health[0] == 200 and json.loads(health[2]) == {"status": "ok"}
    assert unknown[0] == 404
    assert post[0] == 405 and post[1]["allow"] == ["GET, HEAD"]
    assert head[0] == 200 and head[2] == b""
    assert ok[0] == 200 and exhausted[0] == 503
    for response in (health, unknown, post, head, ok, exhausted):
        assert response[1]["cache-control"] == ["no-store"]
        assert "access-control-allow-origin" not in response[1] and "server" not in response[1]
        # Marque du récepteur sur TOUTE réponse : un refus de l'edge n'est jamais confondu avec elle.
        assert response[1]["x-p1q-receiver"] == [p1q_receiver.PROTOCOL_VERSION]


def test_listener_report_is_honest_about_ipv6():
    """IPv6 indisponible (ce bac à sable) : échec rapporté, jamais masqué ;
    disponible : écoute [::] en IPV6_V6ONLY sur le même port."""
    sockets, report = p1q_receiver.open_listeners(0, "dual")
    try:
        families = [entry["family"] for entry in report]
        assert families == ["ipv4", "ipv6"]
        v6 = report[1]
        assert ("error" in v6) or (v6["v6only"] is True and v6["port"] == report[0]["port"])
    finally:
        for sock in sockets:
            sock.close()


def test_receiver_refuses_a_non_isolated_environment():
    import subprocess
    import sys

    from tests.p1q_support import BENCH_DIR, minimal_env

    for name in ("DATABASE_URL", "ORYX_F2_ALLOWED_ORIGIN", "ANTHROPIC_API_KEY", "PGHOST", "SUPABASE_URL"):
        env = minimal_env(P1Q_ROLE="receiver", PORT="0", **{name: "synthetic-value"})
        result = subprocess.run([sys.executable, str(BENCH_DIR / "p1q_main.py")], cwd=str(BENCH_DIR), env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 3, name
        assert name in result.stderr and "synthetic-value" not in result.stderr + result.stdout
        assert "P1Q-STARTUP" not in result.stdout
