"""ACT-L1 / F04-D / P1-Q — ciblage du diagnostic (correctif C5).

La sonde n'envoie de requête d'essai qu'à un récepteur P1-Q identifié, sur le
seul chemin /p1q/observe, vers des hôtes Railway explicitement admis ; les
cibles Q6-B sont limitées aux adresses publiées par le récepteur lui-même.
"""
import io
import json
import socket
import threading

import pytest

from tests.p1q_support import p1q_prober, receiver_process


@pytest.mark.parametrize("url, expected", [
    ("https://p1q-receiver-p1q.up.railway.app", "railway_public"),
    ("https://P1Q-Receiver.up.railway.app/", "railway_public"),
    ("http://p1q-receiver.railway.internal:8080", "railway_private"),
    ("http://[fd12::5]:8080", "ip_literal"),
    ("http://10.250.0.5:8080", "ip_literal"),
    ("http://100.64.0.3:8080", "ip_literal"),
])
def test_admitted_targets(url, expected):
    assert p1q_prober.parse_target("t", url, expected=expected).kind == expected


@pytest.mark.parametrize("url, expected", [
    ("https://example.com", "railway_public"),                              # hors Railway
    ("https://up.railway.app", "railway_public"),                           # suffixe seul
    ("https://p1q.up.railway.app.attacker.example", "railway_public"),      # suffixe usurpé
    ("http://p1q.up.railway.app", "railway_public"),                        # sans TLS
    ("https://p1q.up.railway.app/p1q/observe", "railway_public"),           # chemin imposé
    ("https://p1q.up.railway.app/admin", "railway_public"),
    ("https://p1q.up.railway.app/?x=1", "railway_public"),
    ("https://p1q.up.railway.app/#f", "railway_public"),
    ("https://user:pw@p1q.up.railway.app", "railway_public"),
    ("https://oryx-api-production.up.railway.app", "railway_public"),       # production
    ("http://p1q.up.railway.app:8080", "railway_private"),                  # public pris pour privé
    ("http://postgres.railway.internal.example:5432", "railway_private"),
    ("https://p1q.railway.internal", "railway_private"),
    ("http://127.0.0.1:8080", "railway_private"),                           # boucle locale sans option
    ("http://localhost:8080", "railway_public"),
    ("http://8.8.8.8:8080", "ip_literal"),                                  # Q6-B global
    ("http://p1q.railway.internal:8080", "ip_literal"),                     # Q6-B non littéral
    ("https://10.250.0.5:8080", "ip_literal"),
    ("http://127.0.0.1:8080", "ip_literal"),
])
def test_refused_targets(url, expected):
    with pytest.raises(ValueError):
        p1q_prober.parse_target("t", url, expected=expected)


def test_loopback_requires_the_local_test_flag_and_is_refused_on_railway(capsys):
    assert p1q_prober.parse_target("t", "http://127.0.0.1:1", expected="railway_private",
                                   allow_loopback=True).kind == "loopback"
    assert p1q_prober.main(["--role", "private", "--target", "http://127.0.0.1:1"], {}) == 4
    with pytest.raises(SystemExit):
        p1q_prober.main(["--role", "private", "--target", "http://127.0.0.1:1", "--allow-loopback"],
                        {"RAILWAY_ENVIRONMENT_NAME": "p1q"})


def test_q6b_targets_are_private_role_only_and_on_the_primary_port(capsys):
    with pytest.raises(SystemExit):
        p1q_prober.main(["--role", "public", "--target", "https://p1q.up.railway.app",
                         "--extra-target", "http://10.250.0.5:443"], {})
    assert p1q_prober.main(["--role", "private", "--target", "http://p1q.railway.internal:8080",
                            "--extra-target", "http://10.250.0.5:9999"], {}) == 4


class CountingServer:
    """Serveur TCP qui compte les connexions et répond comme un service non P1-Q."""

    def __init__(self, host="127.0.0.1", port=0, body=b"hello"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self.requests = []
        self.body = body
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            with conn:
                data = conn.recv(65536)
                self.requests.append(data)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                             % (len(self.body), self.body))


def test_a_target_that_is_not_the_receiver_gets_only_the_identification_request(tmp_path):
    out = tmp_path / "wrong.jsonl"
    with CountingServer(body=b'{"status":"ok"}') as wrong:
        code = p1q_prober.main(["--role", "private", "--target", f"http://127.0.0.1:{wrong.port}", "--family", "4",
                                "--allow-loopback", "--delay", "0", "--out", str(out)], {})
    assert code == 0
    assert wrong.connections == 1
    assert b"X-P1Q-Case: Q0-ID" in wrong.requests[0] and b"X-Real-IP" not in wrong.requests[0]
    lines = out.read_text().splitlines()
    records = [json.loads(line) for line in lines[:-1]]
    assert records[0]["identification"] == "target_not_receiver"
    assert records[1]["outcome"] == "aborted" and records[1]["reason"] == "target_not_receiver"
    assert len(records[1]["cases_not_run"]) == len(p1q_prober.cases_for("private"))


def test_q6b_addresses_outside_the_receiver_inventory_are_never_contacted(tmp_path, monkeypatch):
    contacted = []
    real_send = p1q_prober.send_raw

    def spy(address, port, payload, **kwargs):
        contacted.append(address)
        return real_send(address, port, payload, **kwargs)

    monkeypatch.setattr(p1q_prober, "send_raw", spy)
    out = tmp_path / "bypass.jsonl"
    with receiver_process("strict") as rx:
        code = p1q_prober.main(["--role", "private", "--target", f"http://127.0.0.1:{rx.port}", "--family", "4",
                                "--allow-loopback", "--delay", "0", "--only", "Q5",
                                "--extra-target", f"http://127.0.0.3:{rx.port}",
                                "--extra-target", f"http://127.0.0.1:{rx.port}", "--out", str(out)], {})
    assert code == 0
    assert "127.0.0.3" not in contacted and set(contacted) == {"127.0.0.1"}
    records = [json.loads(line) for line in out.read_text().splitlines()[:-1]]
    refused = [r for r in records if r["target"] == "extra:0"]
    assert [r["outcome"] for r in refused] == ["refused_by_guard"]
    assert refused[0]["reason"] == "address_not_in_receiver_inventory"
    admitted = [r for r in records if r["target"] == "extra:1"]
    assert admitted[0]["case"] == "Q0-ID" and admitted[0]["identification"] == "identified"
    assert {r["case"] for r in admitted[1:]} == set(p1q_prober.BYPASS_CASES)
    assert all(r["target_address"] == "127.0.0.1" and r["target_port"] == rx.port for r in admitted)


def test_a_secondary_target_must_be_the_same_receiver_boot():
    with receiver_process("strict") as first, receiver_process("strict") as second:
        run = p1q_prober.Run(role="private", out=io.StringIO())
        primary = p1q_prober.parse_target("primary", f"http://127.0.0.1:{first.port}", expected="railway_private",
                                          allow_loopback=True)
        assert run.execute(primary, [p1q_prober.CATALOGUE_BY_ID["Q5-01"]], [4], delay=0) == 1
        other = p1q_prober.parse_target("public", f"http://127.0.0.1:{second.port}", expected="railway_private",
                                        allow_loopback=True)
        assert run.execute(other, [p1q_prober.CATALOGUE_BY_ID["Q5-01"]], [4], delay=0,
                           require_same_boot=True) == 0
    lines = run.out.getvalue().splitlines()
    assert '"identification":"target_not_same_receiver"' in lines[-2]
    assert '"reason":"target_not_same_receiver"' in lines[-1]


def test_every_request_targets_the_single_observe_path():
    for case in (*p1q_prober.CASES, p1q_prober.IDENTIFY):
        payload = p1q_prober.build_request(case, host="p1q.railway.internal:8080")
        assert payload.split(b"\r\n", 1)[0] == b"GET /p1q/observe HTTP/1.1"
