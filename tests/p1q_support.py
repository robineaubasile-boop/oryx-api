"""Support des tests locaux du banc P1-Q (ACT-L1 / F04-D) : chargement des
modules isolés de diagnostics/p1q et pilotage d'un vrai serveur Uvicorn en
sous-processus (boucle locale, port éphémère, environnement minimal)."""
import io
import ipaddress
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


# --------------------------------------------------------------------------
# Campagne SYNTHÉTIQUE complète (tests de l'analyseur, correctif C6)
#
# Enregistrements au schéma exact de la sonde, sérialisés par p1q_prober.Run
# (empreintes réelles). Topologie FICTIVE choisie pour que l'analyseur doive
# conclure GO-CANDIDATE ; ce n'est en rien une prédiction du comportement de
# Railway. Chaque test négatif retire ou altère UN élément requis.
# --------------------------------------------------------------------------

NOMINAL = {4: "g4:aaaaaaaaaaaaaaaa", 6: "g6:bbbbbbbbbbbbbbbb"}
EGRESS_B = "g4:cccccccccccccccc"
PUBLIC_PEER, PUBLIC_SERVER = ("100.64.1.2", "shared_cgnat", 4), ("10.250.0.5", "private_rfc1918", 4)
PRIVATE_PEER = {4: ("10.251.0.9", "private_rfc1918", 4), 6: ("fd12::9", "unique_local", 6)}
PRIVATE_SERVER = {4: ("10.251.0.5", "private_rfc1918", 4), 6: ("fd12::5", "unique_local", 6)}
PORT = 8080


def _addr(text, category, version, port=None):
    out = {"parsed": True, "text": text, "category": category, "version": version, "canonical": True,
           "sensitive": category == "global"}
    if port is not None:
        out["port"] = port
    return out


def _xri(texts):
    values = []
    for text in texts:
        if text.startswith("g4:") or text.startswith("g6:"):
            values.append(_addr(text, "global", int(text[1])))
        else:
            address = ipaddress.ip_address(text)
            values.append(_addr(text, p1q_redact.category(address), address.version))
    return {"count": len(values), "values": values}


def observation(*, boot, mode="strict", env=None, raw, server, xri=(), self_xri=None, case=None,
                effective=None, headers=None):
    raw_d, server_d = _addr(*raw, port=40000), _addr(*server, port=PORT)
    eff = dict(raw_d) if effective is None else _addr(effective, "documentation", 4, port=0)
    return {"p1q": "receiver-observation", "protocol": p1q_prober.PROTOCOL_VERSION, "boot_id": boot,
            "mode": mode, "case": case,
            "uvicorn": {"proxy_headers": mode != "strict", "forwarded_allow_ips_env": env,
                        "forwarded_allow_ips_env_present": env is not None},
            "raw_client": raw_d, "effective_client": eff, "effective_equals_raw": effective is None,
            "server": server_d, "x_real_ip": _xri(xri),
            "self_match": None if self_xri is None else {"valid": True, "x_real_ip": self_xri},
            "headers": headers or {}, "simulation": {},
            "inventory": {"local_ipv4": [_addr("10.250.0.5", "private_rfc1918", 4),
                                         _addr("10.251.0.5", "private_rfc1918", 4)],
                          "local_ipv6": [_addr("fd12::5", "unique_local", 6)],
                          "private_domain_resolution": {"ipv4": [_addr("10.251.0.5", "private_rfc1918", 4)],
                                                        "ipv6": [_addr("fd12::5", "unique_local", 6)]}}}


def record(run_id, role, target, family, case_id, attempt, *, outcome="observed", obs=None, **extra):
    q = case_id.split("-")[0] if case_id != "*" else "*"
    rec = {"p1q": "prober-result", "protocol": p1q_prober.PROTOCOL_VERSION, "run_id": run_id, "role": role,
           "target": target, "target_kind": {"primary": "railway_public" if role == "public" else "railway_private",
                                             "public": "railway_public"}.get(target, "ip_literal"),
           "target_port": 443 if target == "public" or role == "public" else PORT,
           "tls": target == "public" or role == "public", "family": family, "case": case_id, "q": q,
           "attempt": attempt, "self_committed_versions": [4, 6] if role == "public" else [], "outcome": outcome}
    if obs is not None:
        rec["observation"] = obs
        rec.update({"status": 200, "receiver_header": True})
    rec.update(extra)
    return rec


class Session:
    def __init__(self, run_id, role, records):
        self.run_id, self.role, self.records = run_id, role, records

    def drop(self, predicate):
        before = len(self.records)
        self.records = [r for r in self.records if not predicate(r)]
        assert len(self.records) < before, "aucun enregistrement retiré"
        return self

    def edit(self, predicate, change):
        touched = 0
        for r in self.records:
            if predicate(r):
                change(r)
                touched += 1
        assert touched, "aucun enregistrement modifié"
        return self

    def lines(self):
        out = io.StringIO()
        run = p1q_prober.Run(role=self.role, out=out, prefix=True)
        run.run_id = self.run_id
        for r in self.records:
            r["run_id"] = self.run_id
            run.emit(r)
        run.finish()
        return out.getvalue().splitlines()


def _identify(run_id, role, target, family, boot, raw, server, mode="strict", env=None):
    return record(run_id, role, target, family, "Q0-ID", 1,
                  obs=observation(boot=boot, mode=mode, env=env, raw=raw, server=server), identification="identified")


def public_session(run_id, boot, *, mode="strict", env=None, questions=("Q1", "Q2", "Q3", "Q4", "Q7")):
    records = []
    for family in (4, 6):
        records.append(_identify(run_id, "public", "primary", family, boot, PUBLIC_PEER, PUBLIC_SERVER, mode, env))
        for case in p1q_prober.cases_for("public"):
            if case.q not in questions:
                continue
            for attempt in range(1, case.repeat + 1):
                effective = None
                if mode == "trust_all_negative_control" and case.id in ("Q7-01", "Q7-02", "Q7-03", "Q7-04"):
                    effective = p1q_prober.F4A
                o = observation(boot=boot, mode=mode, env=env, raw=PUBLIC_PEER, server=PUBLIC_SERVER,
                                xri=[NOMINAL[family]], self_xri=[True], case=case.id, effective=effective,
                                headers={"x-railway-edge": [{"value": "railway/europe-west4", "length": 20}]})
                records.append(record(run_id, "public", "primary", family, case.id, attempt, obs=o))
    return Session(run_id, "public", records)


def private_session(run_id, boot, *, mode="strict", env=None, questions=None, bypass=True, q6p=True):
    records = []
    forged_by_case = {c.id: dict((k.lower(), v) for k, v in c.headers).get("x-real-ip") for c in p1q_prober.CASES}
    for family in (4, 6):
        records.append(_identify(run_id, "private", "primary", family, boot, PRIVATE_PEER[family],
                                 PRIVATE_SERVER[family], mode, env))
        for case in p1q_prober.cases_for("private"):
            if questions is not None and case.q not in questions:
                continue
            for attempt in range(1, case.repeat + 1):
                if case.imitate_edge:
                    xri = [forged_by_case.get(case.id) or p1q_prober.F4A]
                else:
                    forged = forged_by_case.get(case.id)
                    xri = [forged] if forged and p1q_redact.parse_plain(forged) is not None else []
                effective = None
                if mode == "trust_all_negative_control" and case.id in ("Q7-01", "Q7-02", "Q7-03", "Q7-04"):
                    effective = p1q_prober.F4A
                o = observation(boot=boot, mode=mode, env=env, raw=PRIVATE_PEER[family],
                                server=PRIVATE_SERVER[family], xri=xri, case=case.id, effective=effective)
                records.append(record(run_id, "private", "primary", family, case.id, attempt, obs=o,
                                      connect_address=PRIVATE_SERVER[family][0]))
    if bypass:
        # Q6-B : tentative vers l'adresse locale de A atteinte par l'edge, RÉELLEMENT refusée.
        literal = {"target_kind": "ip_literal", "target_address": PUBLIC_SERVER[0], "target_port": PORT}
        records.append(record(run_id, "private", "extra:0", 4, "Q0-ID", 1, outcome="connect_error",
                              error="ConnectionRefusedError", error_class="connect_refused", errno=111,
                              connect_address=PUBLIC_SERVER[0], identification="target_not_receiver", **literal))
        records.append(record(run_id, "private", "extra:0", 4, "*", 0, outcome="aborted",
                              reason="target_not_receiver", cases_not_run=list(p1q_prober.BYPASS_CASES), **literal))
    if q6p:
        records.append(_identify(run_id, "private", "public", 4, boot, PUBLIC_PEER, PUBLIC_SERVER, mode, env))
        for case_id in p1q_prober.Q6P_CASES:
            for attempt in range(1, p1q_analyze.CATALOGUE[case_id].repeat + 1):
                o = observation(boot=boot, mode=mode, env=env, raw=PUBLIC_PEER, server=PUBLIC_SERVER,
                                xri=[EGRESS_B], case=case_id)
                records.append(record(run_id, "private", "public", 4, case_id, attempt, obs=o))
        records.append(record(run_id, "private", "public", 6, "*", 0, outcome="family_unavailable",
                              error="gaierror", cases_not_run=list(p1q_prober.Q6P_CASES)))
    return Session(run_id, "private", records)


def complete_campaign():
    """Sessions couvrant TOUTES les exigences : deux démarrages stricts complets
    (public Q1-Q4/Q7, privé Q5-Q7 + Q6-P + Q6-B) et les configurations Q7 (b)-(d)."""
    return {
        "pub1": public_session("pub1", "boot1"),
        "priv1": private_session("priv1", "boot1"),
        "pub2": public_session("pub2", "boot2"),
        "priv2": private_session("priv2", "boot2"),
        "pub_b": public_session("pub_b", "boot3", env="*", questions=("Q7",)),
        "priv_b": private_session("priv_b", "boot3", env="*", questions=("Q7",), bypass=False, q6p=False),
        "pub_c": public_session("pub_c", "boot4", mode="uvicorn_defaults", questions=("Q7",)),
        "priv_c": private_session("priv_c", "boot4", mode="uvicorn_defaults", questions=("Q7",), bypass=False,
                                  q6p=False),
        "pub_d": public_session("pub_d", "boot5", mode="trust_all_negative_control", questions=("Q7",)),
        "priv_d": private_session("priv_d", "boot5", mode="trust_all_negative_control", questions=("Q7",),
                                  bypass=False, q6p=False),
    }


def campaign_report(sessions, *, extra_lines=(), mutate_lines=None, tmp_path=None):
    lines = [line for s in sessions.values() for line in s.lines()] + list(extra_lines)
    if mutate_lines is not None:
        lines = mutate_lines(lines)
    path = tmp_path / "campaign.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p1q_analyze.analyze_files([str(path)])
