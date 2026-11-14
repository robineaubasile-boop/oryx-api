"""Support des tests locaux du banc P1-Q (ACT-L1 / F04-D) : chargement des
modules isolés de diagnostics/p1q et pilotage d'un vrai serveur Uvicorn en
sous-processus (boucle locale, port éphémère, environnement minimal)."""
import json
import os
import select
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = REPO_ROOT / "diagnostics" / "p1q"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import p1q_analyze  # noqa: E402,F401
import p1q_guard  # noqa: E402,F401
import p1q_prober  # noqa: E402,F401
import p1q_receiver  # noqa: E402,F401
import p1q_redact  # noqa: E402,F401


def minimal_env(**extra):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra)
    return env


class RunningReceiver:
    def __init__(self, proc, port, startup):
        self.proc = proc
        self.port = port
        self.startup = startup
        self.stdout = ""
        self.stderr = ""


@contextmanager
def receiver_process(mode="strict", **env_extra):
    env = minimal_env(P1Q_ROLE="receiver", P1Q_UVICORN_MODE=mode, PORT="0", P1Q_LISTEN="v4",
                      P1Q_TTL_SECONDS="120", **env_extra)
    proc = subprocess.Popen([sys.executable, str(BENCH_DIR / "p1q_main.py")], cwd=str(BENCH_DIR), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 20
    first = ""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.5)
        if ready:
            first = proc.stdout.readline()
            break
        if proc.poll() is not None:
            break
    if not first.startswith("P1Q-STARTUP "):
        proc.kill()
        out, err = proc.communicate(timeout=10)
        raise AssertionError(f"récepteur non démarré : {first!r} {out!r} {err!r}")
    startup = json.loads(first.split(" ", 1)[1])
    port = startup["inventory"]["listeners"][0]["port"]
    running = RunningReceiver(proc, port, startup)
    _wait_listening(port)
    try:
        yield running
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate(timeout=10)
        running.stdout, running.stderr = first + out, err


def _wait_listening(port):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("port d'écoute injoignable")


def raw_request(port, lines, *, method="GET", path="/p1q/observe", host="127.0.0.1"):
    """Requête HTTP/1.1 écrite octet par octet ; rend (statut, en-têtes, corps)."""
    head = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close", *lines]
    payload = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1")
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(payload)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    head_bytes, _, body = raw.partition(b"\r\n\r\n")
    head_lines = head_bytes.decode("latin-1").split("\r\n")
    status = int(head_lines[0].split(" ")[1])
    headers = {}
    for line in head_lines[1:]:
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    return status, headers, body


def observe(port, lines):
    status, headers, body = raw_request(port, lines)
    assert status == 200, (status, body)
    return json.loads(body), body


# --------------------------------------------------------------------------
# « Edge » TLS simulé (tests locaux seulement) : ce n'est PAS un modèle de
# Railway. Il sert à vérifier la chaîne sonde publique -> récepteur ->
# analyseur dans les deux sens (réécriture correcte -> PASS ; conservation
# d'un X-Real-IP forgé -> FAIL).
# --------------------------------------------------------------------------


def make_localhost_certificate(directory):
    key, cert = directory / "edge.key", directory / "edge.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
           check=True, capture_output=True)
    return key, cert


class FakeEdge:
    def __init__(self, upstream_port, key, cert, *, client_ip, overwrite=True):
        self.upstream_port = upstream_port
        self.client_ip = client_ip
        self.overwrite = overwrite
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(cert), str(key))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.stopped = False

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stopped = True
        self.sock.close()

    def _rewrite(self, head):
        lines = head.split(b"\r\n")
        out, dropping = [lines[0]], False
        for line in lines[1:]:
            if line[:1] in (b" ", b"\t"):
                if not dropping:
                    out.append(line)
                continue
            name = line.split(b":", 1)[0].strip().lower()
            dropping = self.overwrite and name == b"x-real-ip"
            if not dropping:
                out.append(line)
        if self.overwrite or not any(l.split(b":", 1)[0].strip().lower() == b"x-real-ip" for l in lines[1:]):
            out.append(b"X-Real-IP: " + self.client_ip.encode())
        return b"\r\n".join(out)

    def _serve(self):
        while not self.stopped:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(raw, server_side=True) as conn:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    head = data.split(b"\r\n\r\n", 1)[0]
                    with socket.create_connection(("127.0.0.1", self.upstream_port), timeout=10) as up:
                        up.sendall(self._rewrite(head) + b"\r\n\r\n")
                        while True:
                            chunk = up.recv(65536)
                            if not chunk:
                                break
                            conn.sendall(chunk)
            except (OSError, ssl.SSLError):
                continue
