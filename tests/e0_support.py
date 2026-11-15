"""Support des tests locaux du banc E0 (ACT-L1 / F04-D / P1-B, spécification V0.2-R3 § 6.1).

- chargement des modules isolés de diagnostics/e0/py (aucun module applicatif) ;
- garde réseau : toute connexion hors boucle locale est refusée pendant les tests ;
- clés de test = graines PUBLIQUES de la RFC 8032 § 7.1 (jamais des clés de campagne) ;
- serveur Uvicorn RÉEL de l'origine en sous-processus (boucle locale, port éphémère,
  environnement minimal respectant la liste blanche de l'origine) ;
- requêtes HTTP/1.1 écrites octet par octet.
"""
import ipaddress
import json
import os
import secrets
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
BENCH_ROOT = REPO_ROOT / "diagnostics" / "e0"
BENCH_PY = BENCH_ROOT / "py"
CONFIG_DIR = BENCH_ROOT / "config"
VECTORS_DIR = BENCH_ROOT / "vectors"
if str(BENCH_PY) not in sys.path:
    sys.path.insert(0, str(BENCH_PY))

import e0_attest  # noqa: E402
import e0_ed25519  # noqa: E402
import e0_keysets  # noqa: E402

PUBLIC_HOST = "app.e0-zone.example"
ORIGIN_HOST = "e0-origin-test.up.railway.app"
RFC_SEED_1A = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"   # RFC 8032 TEST 2
RFC_SEED_2 = "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7"    # RFC 8032 TEST 3
RFC_SEED_OTHER = "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42"  # RFC 8032 TEST SHA(abc)
SIGNABLE_V4 = "192.0.0.9"
SIGNABLE_V6 = "2001:1::1"


def load_json(directory: Path, name: str):
    with open(directory / name, encoding="utf-8") as handle:
        return json.load(handle)


def config(name: str):
    return load_json(CONFIG_DIR, name)


def vectors(name: str):
    return load_json(VECTORS_DIR, name)


# --------------------------------------------------------------------------
# Garde réseau
# --------------------------------------------------------------------------

def _is_loopback(address) -> bool:
    if isinstance(address, tuple) and address:
        host = address[0]
    else:
        return isinstance(address, (str, bytes))  # socket Unix
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def block_external_network(monkeypatch):
    """Refuse toute connexion sortante hors boucle locale (fixture des tests E0)."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):
        if not _is_loopback(address):
            raise OSError("réseau sortant interdit pendant les tests E0")
        return original_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _is_loopback(address):
            raise OSError("réseau sortant interdit pendant les tests E0")
        return original_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


# --------------------------------------------------------------------------
# Clés et attestations de test
# --------------------------------------------------------------------------

def private_der(seed_hex: str) -> bytes:
    return e0_ed25519.pkcs8_from_seed(bytes.fromhex(seed_hex))


def public_key(seed_hex: str) -> bytes:
    return e0_ed25519.public_from_private(private_der(seed_hex))


def key_record(seed_hex: str, aud: str) -> dict:
    return e0_keysets.public_record(aud, public_key(seed_hex))


def kid_of(seed_hex: str, aud: str) -> str:
    return key_record(seed_hex, aud)["kid"]


def keyset_environment(phase: str) -> dict:
    """Ensembles de clés canoniques d'une phase : 1a = clé 1a courante ;
    1b/1c/2 = clé 2 courante, clé 1a retirée versée à l'audit."""
    if phase == "1a":
        return {"E0_VERIFY_KEYS": json.dumps([key_record(RFC_SEED_1A, "oryx-e0-1a")])}
    retired = key_record(RFC_SEED_1A, "oryx-e0-1a")
    return {"E0_VERIFY_KEYS": json.dumps([key_record(RFC_SEED_2, "oryx-e0-2")]),
            "E0_AUDIT_PUBKEYS": json.dumps([retired]),
            "E0_RETIRED_KIDS": json.dumps([retired["kid"]])}


def nonce() -> str:
    return e0_attest.b64url_encode(secrets.token_bytes(16))


def attestation_fields(*, aud="oryx-e0-2", seed=RFC_SEED_2, ts_ms=None, ip=SIGNABLE_V4, method="GET",
                       h=PUBLIC_HOST, hi=ORIGIN_HOST, t="/e0/guarded/AAAAAAAAAAAAAAAAAAAAAA", nonce_value=None,
                       kid=None) -> dict:
    return {"v": "1", "aud": aud, "kid": kid or kid_of(seed, aud),
            "ts": str(int(time.time() * 1000) if ts_ms is None else ts_ms),
            "nonce": nonce_value or nonce(), "ip": ip, "c": "V", "m": method, "h": h, "hi": hi, "t": t}


def attestation(seed=RFC_SEED_2, **fields) -> str:
    """Attestation signée par la graine RFC `seed` (outil de test)."""
    return e0_attest.sign_fields(attestation_fields(seed=seed, **fields), private_der(seed))


# --------------------------------------------------------------------------
# Origine réelle en sous-processus
# --------------------------------------------------------------------------

def origin_environment(phase="2", **extra) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "PORT": "0",
           "E0_ROLE": "origin", "E0_PHASE": phase, "E0_PUBLIC_HOST": PUBLIC_HOST, "E0_TTL_SECONDS": "120"}
    env.update(keyset_environment(phase))
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


class RunningOrigin:
    def __init__(self, proc, port, startup, tls):
        self.proc = proc
        self.port = port
        self.startup = startup
        self.tls = tls
        self.stdout = ""
        self.stderr = ""

    def records(self, record_type=None) -> list:
        out = []
        for line in self.stdout.splitlines():
            if line.startswith("E0-ORIGIN "):
                record = json.loads(line[len("E0-ORIGIN "):])
                if record_type is None or record["e0"] == record_type:
                    out.append(record)
        return out

    def log_lines(self) -> list:
        return [line[len("E0-ORIGIN "):] for line in self.stdout.splitlines() if line.startswith("E0-ORIGIN ")]


def run_origin_once(env, timeout=30):
    """Lance l'origine ; rend (code, stdout, stderr) si elle s'arrête d'elle-même."""
    proc = subprocess.run([sys.executable, str(BENCH_PY / "e0_main.py")], cwd=str(BENCH_PY), env=env,
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


@contextmanager
def origin_process(phase="2", tls=False, **extra):
    """Origine réelle ; stdout et stderr sont drainés en continu par des fils
    dédiés (un tube plein bloquerait l'écriture du journal par le serveur)."""
    env = origin_environment(phase, **extra)
    proc = subprocess.Popen([sys.executable, str(BENCH_PY / "e0_main.py")], cwd=str(BENCH_PY), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out_lines, err_chunks = [], []
    first_line = threading.Event()

    def drain_stdout():
        for line in proc.stdout:
            out_lines.append(line)
            first_line.set()
        first_line.set()

    def drain_stderr():
        err_chunks.append(proc.stderr.read())

    readers = [threading.Thread(target=drain_stdout, daemon=True), threading.Thread(target=drain_stderr, daemon=True)]
    for reader in readers:
        reader.start()
    first_line.wait(timeout=30)
    first = out_lines[0] if out_lines else ""
    if not first.startswith("E0-ORIGIN "):
        proc.kill()
        proc.wait(timeout=10)
        for reader in readers:
            reader.join(timeout=10)
        raise AssertionError(f"origine non démarrée : {''.join(out_lines)!r} {''.join(err_chunks)!r}")
    startup = json.loads(first[len("E0-ORIGIN "):])
    port = startup["listeners"][0]["port"]
    running = RunningOrigin(proc, port, startup, tls)
    _wait_listening(port)
    try:
        yield running
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        for reader in readers:
            reader.join(timeout=10)
        running.stdout, running.stderr = "".join(out_lines), "".join(err_chunks)


def _wait_listening(port):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("port d'écoute injoignable")


def raw_exchange(port, payload: bytes, *, tls_context=None, timeout=15) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as plain:
        sock = tls_context.wrap_socket(plain, server_hostname="localhost") if tls_context else plain
        try:
            sock.sendall(payload)
            chunks = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        finally:
            if tls_context:
                sock.close()
    return b"".join(chunks)


def parse_response(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = []
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers.append((name.strip().lower(), value.strip()))
    return status, headers, body


def request(port, *, method="GET", target="/e0/observe", host=ORIGIN_HOST, headers=(), body=b"",
            tls_context=None):
    """Requête HTTP/1.1 brute ; rend (statut, [(nom, valeur)], corps)."""
    head = [f"{method} {target} HTTP/1.1", f"Host: {host}", "Connection: close"]
    head += [f"{name}: {value}" for name, value in headers]
    if body or method in ("POST", "PUT"):
        head.append(f"Content-Length: {len(body)}")
    payload = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body
    return parse_response(raw_exchange(port, payload, tls_context=tls_context))


def header_values(headers, name: bytes) -> list:
    return [v for k, v in headers if k == name]


def make_localhost_certificate(directory: Path):
    key, cert = directory / "origin.key", directory / "origin.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True)
    return key, cert


def client_tls_context(cert: Path):
    context = ssl.create_default_context(cafile=str(cert))
    return context
