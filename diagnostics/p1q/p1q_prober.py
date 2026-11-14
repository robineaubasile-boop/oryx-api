"""P1-Q — sonde de diagnostic (client HTTP/1.1 brut) : Q1 à Q7.

ACT-L1 / F04-D / P1-Q, protocole docs/act_l1_f04d_p1q_protocol_v01.md.
Code TEMPORAIRE, ISOLÉ : bibliothèque standard seulement.

Deux usages :
- public (Q1-Q4, Q7) : lancée par l'expérimentateur depuis son poste, vers le
  domaine public Railway du récepteur (TLS, port 443) ;
- private (Q5, Q6, et Q2-Q4 / Q7 rejoués sans edge) : lancée par le
  service B dans le même projet / environnement Railway, vers
  http://<RAILWAY_PRIVATE_DOMAIN du récepteur>:<port>.

Le client écrit les requêtes octet par octet (socket brute) : doublons,
valeurs multiples ou mal formées sont envoyés EXACTEMENT comme décrits par le
catalogue, sans normalisation par une bibliothèque HTTP.

Toutes les valeurs forgées appartiennent aux plages de DOCUMENTATION
(RFC 5737 : 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24 ; RFC 3849 :
2001:db8::/32) ou à des plages non routables : aucune adresse réelle d'un
tiers n'est usurpée.

Sortie : une ligne JSON par essai (« JSONL »), préfixée par « P1Q-RESULT »
avec --prefix (journaux Railway), suivie d'une ligne « P1Q-DIGEST »
(empreinte SHA-256 des lignes de résultat, pour vérifier une collecte
complète). Les observations sont déjà pseudonymisées par le récepteur ; la
sonde n'émet en clair aucune adresse globale (adresses de connexion publique
non rapportées).

Adresse de l'expérimentateur : --self-ip (répétable) n'est JAMAIS envoyée en
clair ; la sonde envoie une clé aléatoire par exécution et l'engagement
HMAC-SHA256 de chaque adresse ; le récepteur rend seulement des booléens.
"""
import argparse
import hashlib
import json
import os
import secrets
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from p1q_guard import (EXIT_ENVIRONMENT_NOT_ISOLATED, EXIT_PRODUCTION_TARGET, check_target_host,  # noqa: E402
                       forbidden_variables)
from p1q_redact import Redactor, category, commitment, parse_plain  # noqa: E402

PROTOCOL_VERSION = "P1Q-V0.1"
OBSERVE_PATH = "/p1q/observe"
MAX_RESPONSE_BYTES = 1 << 20
USER_AGENT = "p1q-prober/0.1"

# Valeurs forgées : documentation / non routables uniquement.
F4A, F4B, F4C = "203.0.113.7", "198.51.100.23", "192.0.2.44"
F6A, F6B = "2001:db8::7", "2001:db8:abcd::23"
MAPPED_F4A = "::ffff:203.0.113.7"
FORGED_VALUES = (F4A, F4B, F4C, F6A, F6B, MAPPED_F4A, "10.1.2.3", "127.0.0.1", "::1", "100.64.0.9",
                 "fd00::9", "fe80::1")

PUBLIC, PRIVATE = "public", "private"


@dataclass(frozen=True)
class Case:
    id: str
    q: str
    scopes: frozenset
    title: str
    headers: tuple = ()          # (nom, valeur) envoyés tels quels, dans l'ordre
    raw_lines: tuple = ()        # lignes brutes supplémentaires (obs-fold, octets non ASCII)
    repeat: int = 1
    imitate_edge: bool = False   # Q6 : imitation complète d'une requête edge
    host_override: str = field(default=None)


BOTH = frozenset({PUBLIC, PRIVATE})
ONLY_PUBLIC = frozenset({PUBLIC})
ONLY_PRIVATE = frozenset({PRIVATE})


def _long_xff(n):
    return ", ".join(f"198.51.100.{i % 250 + 1}" for i in range(n))


CASES = (
    # Q1 — public nominal
    Case("Q1-01", "Q1", ONLY_PUBLIC, "requête nominale, aucun en-tête de proxy fourni", repeat=5),
    # Q2 — X-Real-IP forgé
    Case("Q2-01", "Q2", BOTH, "X-Real-IP IPv4 forgé", (("X-Real-IP", F4A),)),
    Case("Q2-02", "Q2", BOTH, "X-Real-IP IPv6 forgé", (("X-Real-IP", F6A),)),
    Case("Q2-03", "Q2", BOTH, "X-Real-IP boucle locale", (("X-Real-IP", "127.0.0.1"),)),
    Case("Q2-04", "Q2", BOTH, "X-Real-IP privée RFC 1918", (("X-Real-IP", "10.1.2.3"),)),
    Case("Q2-05", "Q2", BOTH, "X-Real-IP casse inhabituelle du nom", (("x-REAL-ip", F4A),)),
    Case("Q2-06", "Q2", BOTH, "X-Real-IP avec espaces", (("X-Real-IP", f"  {F4A}  "),)),
    Case("Q2-07", "Q2", BOTH, "X-Real-IP liste de deux valeurs", (("X-Real-IP", f"{F4A}, {F4B}"),)),
    Case("Q2-08", "Q2", BOTH, "X-Real-IP vide", (("X-Real-IP", ""),)),
    # Q3 — X-Forwarded-For / Forwarded / apparentés
    Case("Q3-01", "Q3", BOTH, "X-Forwarded-For simple forgé", (("X-Forwarded-For", F4A),)),
    Case("Q3-02", "Q3", BOTH, "X-Forwarded-For chaîne forgée", (("X-Forwarded-For", f"{F4A}, {F4B}"),)),
    Case("Q3-03", "Q3", BOTH, "Forwarded for= IPv4 forgé", (("Forwarded", f"for={F4A}"),)),
    Case("Q3-04", "Q3", BOTH, "Forwarded IPv6 avec port, proto, by",
         (("Forwarded", f'for="[{F6A}]:4711";proto=http;by={F4C}'),)),
    Case("Q3-05", "Q3", BOTH, "valeurs contradictoires X-Real-IP / XFF / Forwarded",
         (("X-Real-IP", F4A), ("X-Forwarded-For", F4B), ("Forwarded", f"for={F4C}"))),
    Case("Q3-06", "Q3", BOTH, "X-Forwarded-Proto / Host / Port forgés",
         (("X-Forwarded-Proto", "http"), ("X-Forwarded-Host", "attacker.example"), ("X-Forwarded-Port", "80"))),
    Case("Q3-07", "Q3", BOTH, "autres en-têtes d'IP client forgés",
         (("True-Client-IP", F4A), ("CF-Connecting-IP", F4A), ("X-Client-IP", F4A),
          ("X-Original-Forwarded-For", F4A), ("X-Envoy-External-Address", F4A), ("X-Cluster-Client-IP", F4A))),
    Case("Q3-08", "Q3", BOTH, "en-têtes Railway forgés",
         (("X-Railway-Edge", "railway/p1q-forged"), ("X-Railway-Request-Id", "p1q-forged-request-id"),
          ("X-Request-Start", "1000000000000"))),
    Case("Q3-09", "Q3", BOTH, "XFF forgé + X-Real-IP absent : dérivation éventuelle",
         (("X-Forwarded-For", f"{F4A}, {F4B}, {F4C}"),)),
    # Q4 — entrées ambiguës
    Case("Q4-01", "Q4", BOTH, "X-Real-IP dupliqué", (("X-Real-IP", F4A), ("X-Real-IP", F4B))),
    Case("Q4-02", "Q4", BOTH, "X-Forwarded-For dupliqué", (("X-Forwarded-For", F4A), ("X-Forwarded-For", F4B))),
    Case("Q4-03", "Q4", BOTH, "Forwarded dupliqué", (("Forwarded", f"for={F4A}"), ("Forwarded", f"for={F4B}"))),
    Case("Q4-04", "Q4", BOTH, "X-Real-IP octet hors bornes", (("X-Real-IP", "203.0.113.999"),)),
    Case("Q4-05", "Q4", BOTH, "X-Real-IP non IP", (("X-Real-IP", "not-an-ip"),)),
    Case("Q4-06", "Q4", BOTH, "X-Real-IP avec port", (("X-Real-IP", f"{F4A}:8080"),)),
    Case("Q4-07", "Q4", BOTH, "X-Real-IP IPv6 entre crochets", (("X-Real-IP", f"[{F6A}]"),)),
    Case("Q4-08", "Q4", BOTH, "X-Real-IP IPv6 avec zone", (("X-Real-IP", "fe80::1%eth0"),)),
    Case("Q4-09", "Q4", BOTH, "X-Real-IP IPv4 mappée IPv6", (("X-Real-IP", MAPPED_F4A),)),
    Case("Q4-10", "Q4", BOTH, "X-Real-IP IPv6 non canonique", (("X-Real-IP", "2001:DB8:0:0:0:0:0:7"),)),
    Case("Q4-11", "Q4", BOTH, "X-Real-IP zéros non significatifs", (("X-Real-IP", "203.000.113.007"),)),
    Case("Q4-12", "Q4", BOTH, "X-Real-IP entier décimal", (("X-Real-IP", "3405803783"),)),
    Case("Q4-13", "Q4", BOTH, "X-Forwarded-For de 100 entrées", (("X-Forwarded-For", _long_xff(100)),)),
    Case("Q4-14", "Q4", BOTH, "X-Real-IP plié (obs-fold, RFC 7230 § 3.2.4)",
         raw_lines=(f"X-Real-IP: {F4A}", f" {F4B}")),
    Case("Q4-15", "Q4", BOTH, "X-Real-IP octet non ASCII", raw_lines=(f"X-Real-IP: {F4A}é",)),
    Case("Q4-16", "Q4", BOTH, "X-Real-IP CGNAT / ULA / lien local",
         (("X-Real-IP", "100.64.0.9"), ("X-Forwarded-For", "fd00::9, fe80::1"))),
    # Q5 — privé nominal
    Case("Q5-01", "Q5", ONLY_PRIVATE, "requête privée nominale, aucun en-tête de proxy", repeat=3),
    # Q6 — privé avec usurpation (test critique)
    Case("Q6-01", "Q6", ONLY_PRIVATE, "imitation complète d'une requête edge (IPv4 forgée)",
         imitate_edge=True, repeat=3),
    Case("Q6-02", "Q6", ONLY_PRIVATE, "imitation complète d'une requête edge (IPv6 forgée)",
         imitate_edge=True, headers=(("X-Real-IP", F6A),)),
    Case("Q6-03", "Q6", ONLY_PRIVATE, "imitation edge + Host public seul",
         imitate_edge=True, host_override="@public"),
    # Q7 — configuration Uvicorn (exécutés dans chaque mode du récepteur)
    Case("Q7-01", "Q7", BOTH, "XFF simple forgé (réécriture de scope[client] ?)", (("X-Forwarded-For", F4A),)),
    Case("Q7-02", "Q7", BOTH, "XFF terminé par 127.0.0.1", (("X-Forwarded-For", f"{F4A}, 127.0.0.1"),)),
    Case("Q7-03", "Q7", BOTH, "XFF non IP", (("X-Forwarded-For", "attacker"),)),
    Case("Q7-04", "Q7", BOTH, "XFF dupliqué (dernier gagnant ?)",
         (("X-Forwarded-For", F4A), ("X-Forwarded-For", F4B))),
    Case("Q7-05", "Q7", BOTH, "X-Forwarded-Proto forgé", (("X-Forwarded-Proto", "http"),)),
)


def cases_for(role, only=None):
    selected = [c for c in CASES if role in c.scopes]
    if only:
        wanted = set(only)
        selected = [c for c in selected if c.id in wanted or c.q in wanted]
    return selected


def _check_header(name, value):
    for text in (name, value):
        if "\r" in text or "\n" in text:
            raise ValueError("CR/LF interdit dans un en-tête de catalogue (utiliser raw_lines)")


def edge_imitation_headers(public_host, forged_ip=F4A):
    """En-têtes qu'ajoute l'edge public Railway (liste documentée), forgés."""
    return (
        ("X-Real-IP", forged_ip),
        ("X-Forwarded-For", forged_ip),
        ("X-Forwarded-Proto", "https"),
        ("X-Forwarded-Host", public_host or "p1q-receiver.up.railway.app"),
        ("X-Railway-Edge", "railway/p1q-forged"),
        ("X-Railway-Request-Id", "p1q-" + secrets.token_hex(8)),
        ("X-Request-Start", str(int(time.time() * 1000))),
    )


def host_header(host, port, tls):
    shown = f"[{host}]" if ":" in host else host
    return shown if port == (443 if tls else 80) else f"{shown}:{port}"


def build_request(case, *, host, public_host=None, self_headers=()):
    """Octets exacts de la requête GET /p1q/observe pour un cas. `host` est la
    valeur de l'en-tête Host (voir host_header)."""
    request_host = host
    if case.host_override == "@public":
        request_host = public_host or host
    headers = [("Host", request_host), ("User-Agent", USER_AGENT), ("Accept", "application/json"),
               ("Connection", "close"), ("X-P1Q-Case", case.id)]
    headers.extend(self_headers)
    extra = list(case.headers)
    if case.imitate_edge:
        forged = dict((k.lower(), v) for k, v in extra).get("x-real-ip", F4A)
        imitation = [h for h in edge_imitation_headers(public_host, forged)]
        extra = imitation + [h for h in extra if h[0].lower() != "x-real-ip"]
    headers.extend(extra)
    lines = [f"GET {OBSERVE_PATH} HTTP/1.1"]
    for name, value in headers:
        _check_header(name, value)
        lines.append(f"{name}: {value}")
    lines.extend(case.raw_lines)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _decode_chunked(body):
    out, rest = b"", body
    while rest:
        size_line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            break
        if size == 0:
            break
        out, rest = out + rest[:size], rest[size + 2:]
    return out


def parse_response(raw):
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return None, {}, b""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    if "chunked" in ",".join(headers.get("transfer-encoding", [])).lower():
        body = _decode_chunked(body)
    return status, headers, body


RESPONSE_HEADERS_REPORTED = ("server", "x-railway-edge", "x-railway-request-id", "x-railway-upstream-zone",
                             "content-type", "cache-control", "via", "alt-svc")


def send_raw(address, port, payload, *, tls_host=None, timeout=10.0):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((address, port))
        conn = sock
        if tls_host is not None:
            context = ssl.create_default_context()
            context.set_alpn_protocols(["http/1.1"])
            conn = context.wrap_socket(sock, server_hostname=tls_host)
        conn.sendall(payload)
        chunks, total = [], 0
        while total < MAX_RESPONSE_BYTES:
            try:
                data = conn.recv(65536)
            except socket.timeout:
                break
            except ssl.SSLError:
                break
            if not data:
                break
            chunks.append(data)
            total += len(data)
        return b"".join(chunks)


@dataclass
class Target:
    label: str
    url: str
    host: str
    port: int
    tls: bool


def parse_target(label, url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("cible : http(s)://hôte[:port] attendu")
    host = check_target_host(parts.hostname)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return Target(label, url, host, port, parts.scheme == "https")


def resolve_family(host, port, family):
    fam = socket.AF_INET if family == 4 else socket.AF_INET6
    try:
        infos = socket.getaddrinfo(host, port, fam, socket.SOCK_STREAM)
    except OSError as exc:
        return None, type(exc).__name__
    addresses = sorted({info[4][0] for info in infos})
    return (addresses[0] if addresses else None), None


class Run:
    def __init__(self, *, role, self_ips=(), public_host=None, out=None, prefix=False):
        self.role = role
        self.run_id = secrets.token_hex(6)
        self.public_host = public_host
        self.out = out or sys.stdout
        self.prefix = prefix
        self.digest = hashlib.sha256()
        self.count = 0
        self.local = Redactor()
        self.self_headers = ()
        if self_ips:
            key = secrets.token_bytes(32)
            macs = ",".join(commitment(key, ip) for ip in self_ips)
            self.self_headers = (("X-P1Q-Self-Key", key.hex()), ("X-P1Q-Self-Mac", macs))

    def emit(self, record):
        line = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        self.digest.update(line.encode("ascii") + b"\n")
        self.count += 1
        self.out.write(("P1Q-RESULT " if self.prefix else "") + line + "\n")
        self.out.flush()

    def finish(self):
        line = json.dumps({"p1q": "prober-digest", "run_id": self.run_id, "results": self.count,
                           "sha256": self.digest.hexdigest()}, sort_keys=True, separators=(",", ":"))
        self.out.write(("P1Q-DIGEST " if self.prefix else "") + line + "\n")
        self.out.flush()

    def execute(self, target, cases, families, *, delay=0.2):
        for family in families:
            address, error = resolve_family(target.host, target.port, family)
            if address is None:
                # Famille indisponible (résolution) : consignée une fois, jamais ignorée.
                self.emit({"p1q": "prober-result", "protocol": PROTOCOL_VERSION, "run_id": self.run_id,
                           "role": self.role, "target": target.label, "tls": target.tls, "family": family,
                           "case": "*", "q": "*", "attempt": 0, "outcome": "family_unavailable", "error": error,
                           "cases_not_run": [c.id for c in cases]})
                continue
            for case in cases:
                for attempt in range(1, case.repeat + 1):
                    record = {"p1q": "prober-result", "protocol": PROTOCOL_VERSION, "run_id": self.run_id,
                              "role": self.role, "target": target.label, "tls": target.tls,
                              "family": family, "case": case.id, "q": case.q, "attempt": attempt}
                    if not target.tls:
                        # Réseau privé : adresse d'infrastructure, rapportée (pseudonymisée si globale).
                        record["connect_address"] = self.local.redact_text(address)
                    payload = build_request(case, host=host_header(target.host, target.port, target.tls),
                                            public_host=self.public_host,
                                            self_headers=self.self_headers)
                    started = time.monotonic()
                    try:
                        raw = send_raw(address, target.port, payload, tls_host=target.host if target.tls else None)
                    except OSError as exc:
                        record.update({"outcome": "connect_error", "error": type(exc).__name__,
                                       "errno": getattr(exc, "errno", None)})
                        self.emit(record)
                        continue
                    record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
                    status, headers, body = parse_response(raw)
                    record["status"] = status
                    record["response_headers"] = {k: [self.local.redact_text(v) for v in headers[k]]
                                                  for k in RESPONSE_HEADERS_REPORTED if k in headers}
                    observation = None
                    try:
                        document = json.loads(body.decode("utf-8")) if body else None
                        if isinstance(document, dict) and document.get("p1q") == "receiver-observation":
                            observation = document
                    except (UnicodeDecodeError, ValueError):
                        pass
                    if observation is not None:
                        record.update({"outcome": "observed", "observation": observation})
                    else:
                        record.update({"outcome": "not_observed" if status is not None else "no_response",
                                       "body_excerpt": self.local.redact_text(
                                           body[:300].decode("utf-8", "replace"))})
                    self.emit(record)
                    if delay:
                        time.sleep(delay)


def _families(text):
    return {"4": [4], "6": [6], "both": [4, 6]}[text]


def main(argv=None, environ=None) -> int:
    environ = dict(os.environ if environ is None else environ)
    parser = argparse.ArgumentParser(description="Sonde P1-Q (Q1-Q7)")
    parser.add_argument("--role", choices=(PUBLIC, PRIVATE), default=environ.get("P1Q_ROLE_PROBE"))
    parser.add_argument("--target", default=environ.get("P1Q_TARGET"), help="http(s)://hôte[:port]")
    parser.add_argument("--public-target", default=environ.get("P1Q_PUBLIC_TARGET"),
                        help="domaine public du récepteur (imitation Q6, et Q6-P depuis B)")
    parser.add_argument("--extra-target", action="append", default=[],
                        help="cible de contournement Q6-B (répétable), http://adresse:port")
    parser.add_argument("--family", choices=("4", "6", "both"), default=environ.get("P1Q_FAMILY", "both"))
    parser.add_argument("--only", action="append", default=[], help="identifiant de cas ou de question")
    parser.add_argument("--self-ip", action="append", default=[],
                        help="adresse publique de l'expérimentateur (jamais transmise en clair)")
    parser.add_argument("--prefix", action="store_true", help="préfixer les lignes (journaux Railway)")
    parser.add_argument("--out", help="fichier JSONL de sortie (défaut : sortie standard)")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--list", action="store_true", help="afficher le catalogue et sortir")
    args = parser.parse_args(argv)

    if args.list:
        for case in CASES:
            print(f"{case.id}\t{case.q}\t{','.join(sorted(case.scopes))}\tx{case.repeat}\t{case.title}")
        return 0
    if any(name.startswith("RAILWAY_") for name in environ) or args.role == PRIVATE:
        banned = forbidden_variables(environ)
        if banned:
            print("p1q-prober: environnement NON isolé, exécution refusée ; variables : " + ", ".join(banned),
                  file=sys.stderr)
            return EXIT_ENVIRONMENT_NOT_ISOLATED
    if args.role is None or args.target is None:
        parser.error("--role et --target sont obligatoires")
    for text in args.self_ip:
        address = parse_plain(text)
        if address is None or category(address) != "global":
            parser.error("--self-ip : adresse publique globale nue attendue")
    extra_targets = list(args.extra_target)
    extra_targets += [t for t in environ.get("P1Q_EXTRA_TARGETS", "").split(",") if t.strip()]
    try:
        target = parse_target("primary", args.target)
        public = parse_target("public", args.public_target) if args.public_target else None
        extras = [parse_target(f"extra:{i}", t.strip()) for i, t in enumerate(extra_targets)]
    except ValueError as exc:
        print(f"p1q-prober: cible refusée : {exc}", file=sys.stderr)
        return EXIT_PRODUCTION_TARGET
    if args.role == PUBLIC and not target.tls:
        parser.error("rôle public : cible https:// (edge Railway) attendue")
    if args.role == PRIVATE and target.tls:
        parser.error("rôle private : cible http:// sur le réseau privé attendue")

    out = open(args.out, "w", encoding="ascii") if args.out else None
    try:
        run = Run(role=args.role, self_ips=args.self_ip, public_host=public.host if public else None,
                  out=out, prefix=args.prefix)
        families = _families(args.family)
        run.execute(target, cases_for(args.role, args.only), families, delay=args.delay)
        if args.role == PRIVATE:
            bypass = [c for c in CASES if c.id in ("Q5-01", "Q6-01")]
            for extra in extras:
                run.execute(extra, bypass, families, delay=args.delay)
            if public is not None:
                # Q6-P : appelant interne passant par l'edge public.
                public_run_cases = [c for c in CASES if c.id in ("Q1-01", "Q2-01", "Q3-05")]
                run.execute(public, public_run_cases, families, delay=args.delay)
        run.finish()
    finally:
        if out is not None:
            out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
