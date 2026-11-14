"""P1-Q — Service A : récepteur HTTP de diagnostic (ASGI minimal + Uvicorn).

ACT-L1 / F04-D / P1-Q, protocole docs/act_l1_f04d_p1q_protocol_v01.md.
Code TEMPORAIRE, ISOLÉ du runtime Oryx : bibliothèque standard + uvicorn
(version verrouillée identique à la production : 0.27.0). Aucun import d'un
module Oryx, aucune base, aucun secret, aucun appel sortant.

Routes (toute autre : 404 ; méthode autre que GET / HEAD : 405) :
- GET /p1q/health  -> 200 {"status":"ok"} (sonde de déploiement) ;
- GET /p1q/observe -> 200, observation JSON de la requête reçue.

Observation (jamais journalisée ; rendue uniquement à l'appelant, no-store) :
- pair TCP BRUT vu par Uvicorn (relevé AVANT tout middleware de proxy) ;
- scope["client"] EFFECTIF vu par l'application (après l'éventuel
  ProxyHeadersMiddleware d'Uvicorn, selon le mode) ;
- adresse locale scope["server"], version HTTP, schéma ;
- X-Real-IP, X-Forwarded-For, Forwarded et en-têtes apparentés, dans l'ordre
  et avec leurs doublons ;
- simulation du ProxyHeadersMiddleware RÉEL d'Uvicorn 0.27.0 sur le pair brut
  (confiance 127.0.0.1 = défaut ; confiance « * ») ;
- correspondance éventuelle avec les adresses que le client a ENGAGÉES par
  HMAC (en-têtes x-p1q-self-key / x-p1q-self-mac), sans qu'aucune adresse du
  client ne transite en clair.

Confidentialité : toute adresse globale est pseudonymisée (p1q_redact) ; les
valeurs de Cookie, Authorization et de tout en-tête hors liste ne sont jamais
rendues (nom, nombre, longueur seulement) ; journal d'accès Uvicorn désactivé ;
le processus n'écrit qu'un inventaire de démarrage sans donnée personnelle.

Modes Uvicorn (variable P1Q_UVICORN_MODE, Q7) :
- strict (défaut)          : proxy_headers=False (candidat F04-A § 7.2.3) ;
- uvicorn_defaults         : réglages proxy par défaut d'Uvicorn, comme
                             api.py (proxy_headers=True, FORWARDED_ALLOW_IPS lu
                             dans l'environnement, défaut 127.0.0.1) ;
- trust_all_negative_control : proxy_headers=True, forwarded_allow_ips="*"
                             (témoin négatif : DOIT montrer la falsification).
"""
import json
import os
import re
import secrets
import socket
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from p1q_guard import EXIT_ENVIRONMENT_NOT_ISOLATED, forbidden_variables  # noqa: E402
from p1q_redact import Redactor, parse_plain  # noqa: E402

PROTOCOL_VERSION = "P1Q-V0.1-R1"
RECEIVER_HEADER = b"x-p1q-receiver"
OBSERVE_PATH = "/p1q/observe"
HEALTH_PATH = "/p1q/health"
MODES = ("strict", "uvicorn_defaults", "trust_all_negative_control")
DEFAULT_MAX_REQUESTS = 5000
RAW_CLIENT_KEY = "p1q.raw_client"

# En-têtes dont la valeur (pseudonymisée) est rendue.
ECHOED_HEADERS = (
    "host", "x-real-ip", "x-forwarded-for", "forwarded", "x-forwarded-proto", "x-forwarded-host",
    "x-forwarded-port", "x-forwarded-server", "x-original-forwarded-for", "x-client-ip", "true-client-ip",
    "cf-connecting-ip", "fastly-client-ip", "x-cluster-client-ip", "x-envoy-external-address",
    "x-envoy-internal", "x-envoy-attempt-count", "x-railway-edge", "x-railway-request-id", "x-request-start",
    "x-railway-debug", "via", "x-request-id", "connection", "te", "upgrade", "content-length",
    "transfer-encoding", "x-p1q-case",
)
# En-têtes dont la valeur n'est JAMAIS rendue (nom et nombre seulement).
SENSITIVE_HEADERS = ("cookie", "authorization", "proxy-authorization", "x-api-key", "x-p1q-self-key",
                     "x-p1q-self-mac", "set-cookie")
RAILWAY_ENV_REPORTED = ("RAILWAY_PRIVATE_DOMAIN", "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_ENVIRONMENT_NAME",
                        "RAILWAY_PROJECT_NAME", "RAILWAY_SERVICE_NAME", "RAILWAY_REPLICA_REGION",
                        "RAILWAY_DEPLOYMENT_ID", "RAILWAY_REPLICA_ID")
_CASE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_MAC_RE = re.compile(r"[0-9a-f]{64}")
MAX_SELF_MACS = 4


def _header_lists(scope) -> list:
    out = []
    for name, value in scope.get("headers") or ():
        out.append((name.decode("latin-1").lower(), value.decode("latin-1")))
    return out


def _forwarded_for_values(value: str) -> list:
    """Paramètres for= d'un en-tête Forwarded (RFC 7239), sans interprétation."""
    found = []
    for element in value.split(","):
        for pair in element.split(";"):
            name, sep, raw = pair.strip().partition("=")
            if sep and name.strip().lower() == "for":
                raw = raw.strip()
                if len(raw) >= 2 and raw[0] == raw[-1] == '"':
                    raw = raw[1:-1]
                found.append(raw)
    return found


async def _simulate_proxy_headers(raw_scope, trusted):
    """Rejoue le ProxyHeadersMiddleware RÉEL d'Uvicorn sur une copie du scope
    brut ; rend (client, scheme) que l'application aurait vus."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen = {}

    async def capture(scope, receive, send):
        seen["client"] = scope.get("client")
        seen["scheme"] = scope.get("scheme")

    copy = dict(raw_scope)
    copy["client"] = raw_scope.get(RAW_CLIENT_KEY)
    copy["headers"] = list(raw_scope.get("headers") or ())
    await ProxyHeadersMiddleware(capture, trusted_hosts=trusted)(copy, None, None)
    return seen.get("client"), seen.get("scheme")


class RawPeerRecorder:
    """Middleware le plus EXTERNE : copie le pair TCP fourni par le protocole
    HTTP d'Uvicorn avant tout ProxyHeadersMiddleware (qui modifie le scope en
    place)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            client = scope.get("client")
            scope[RAW_CLIENT_KEY] = tuple(client) if client is not None else None
        return await self.app(scope, receive, send)


def _json_response(status, document, *, head=False):
    body = (json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    # x-p1q-receiver : marque TOUTE réponse du récepteur (y compris 404 / 405 / 503), pour qu'une
    # réponse de l'edge (refus) ne soit jamais confondue avec une erreur du récepteur.
    headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
               (b"content-length", str(len(body)).encode("ascii")),
               (RECEIVER_HEADER, PROTOCOL_VERSION.encode("ascii"))]
    return status, headers, b"" if head else body


class Receiver:
    """Application ASGI 3 de diagnostic. Aucune écriture, aucun journal."""

    def __init__(self, *, mode, settings, redactor=None, max_requests=DEFAULT_MAX_REQUESTS, inventory=None):
        if mode not in MODES:
            raise ValueError("mode inconnu")
        self.mode = mode
        self.settings = dict(settings)
        self.redactor = redactor or Redactor()
        self.max_requests = max_requests
        self.boot_id = secrets.token_hex(8)
        self.started = time.monotonic()
        self.inventory = inventory or {}
        self._count = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":  # lifespan="off" en service ; défensif
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        status, headers, body = await self._handle(scope)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _handle(self, scope):
        method, path = scope.get("method"), scope.get("path")
        head = method == "HEAD"
        if path == HEALTH_PATH and method in ("GET", "HEAD"):
            return _json_response(200, {"status": "ok"}, head=head)
        if path != OBSERVE_PATH:
            return _json_response(404, {"error": "not_found"}, head=head)
        if method not in ("GET", "HEAD"):
            status, headers, body = _json_response(405, {"error": "method_not_allowed"})
            return status, headers + [(b"allow", b"GET, HEAD")], body
        self._count += 1
        if self._count > self.max_requests:
            return _json_response(503, {"error": "request_budget_exhausted"}, head=head)
        document = await self.observe(scope)
        return _json_response(200, document, head=head)

    async def observe(self, scope) -> dict:
        r = self.redactor
        headers = _header_lists(scope)
        names = [name for name, _ in headers]
        echoed, sensitive, others = {}, {}, {}
        for name, value in headers:
            if name in SENSITIVE_HEADERS:
                sensitive[name] = sensitive.get(name, 0) + 1
            elif name in ECHOED_HEADERS:
                echoed.setdefault(name, []).append({"value": r.redact_text(value), "length": len(value)})
            else:
                entry = others.setdefault(name, {"count": 0, "max_length": 0})
                entry["count"] += 1
                entry["max_length"] = max(entry["max_length"], len(value))

        def values(name):
            return [v for n, v in headers if n == name]

        x_real_ip = values("x-real-ip")
        xff = values("x-forwarded-for")
        forwarded = values("forwarded")
        raw_client = scope.get(RAW_CLIENT_KEY)
        effective_client = scope.get("client")

        sim_default = await _simulate_proxy_headers(scope, "127.0.0.1")
        sim_all = await _simulate_proxy_headers(scope, "*")

        case = values("x-p1q-case")
        case_id = case[0] if len(case) == 1 and _CASE_RE.fullmatch(case[0]) else ("invalid" if case else None)

        document = {
            "p1q": "receiver-observation",
            "protocol": PROTOCOL_VERSION,
            "boot_id": self.boot_id,
            "seq": self._count,
            "uptime_ms": int((time.monotonic() - self.started) * 1000),
            "mode": self.mode,
            "uvicorn": self.settings,
            "case": case_id,
            "method": scope.get("method"),
            "http_version": scope.get("http_version"),
            "scheme": scope.get("scheme"),
            "raw_client": r.describe_tuple(raw_client),
            "effective_client": r.describe_tuple(effective_client),
            "effective_equals_raw": (tuple(effective_client) if effective_client else None) == raw_client,
            "server": r.describe_tuple(scope.get("server")),
            "header_names": names,
            "headers": echoed,
            "sensitive_headers_present": sensitive,
            "other_headers": others,
            "x_real_ip": {"count": len(x_real_ip), "values": [r.describe(v) for v in x_real_ip]},
            "x_forwarded_for": {"count": len(xff),
                                "entries": [[r.describe(e.strip(" \t")) for e in v.split(",")] for v in xff[:4]]},
            "forwarded": {"count": len(forwarded),
                          "for": [[r.describe(f) for f in _forwarded_for_values(v)] for v in forwarded[:4]]},
            "simulation": {
                "uvicorn_trust_127_0_0_1": {"client": r.describe_tuple(sim_default[0]), "scheme": sim_default[1]},
                "uvicorn_trust_all": {"client": r.describe_tuple(sim_all[0]), "scheme": sim_all[1]},
            },
            "self_match": self._self_match(values, raw_client, x_real_ip, xff),
            "inventory": self.inventory,
        }
        return document

    def _self_match(self, values, raw_client, x_real_ip, xff):
        keys, macs_lines = values("x-p1q-self-key"), values("x-p1q-self-mac")
        if not keys and not macs_lines:
            return None
        if len(keys) != 1 or len(macs_lines) != 1:
            return {"valid": False}
        macs = [m.strip() for m in macs_lines[0].split(",")]
        if not macs or len(macs) > MAX_SELF_MACS or not all(_MAC_RE.fullmatch(m) for m in macs):
            return {"valid": False}
        key = keys[0].strip()

        def match(text):
            address = parse_plain(text) if isinstance(text, str) else None
            return None if address is None else self.redactor.matches_commitment(address, key, macs)

        return {
            "valid": True,
            "raw_client": match(raw_client[0]) if raw_client else None,
            "x_real_ip": [match(v) for v in x_real_ip],
            "x_forwarded_for": [[match(e.strip(" \t")) for e in v.split(",")] for v in xff[:4]],
        }


# --------------------------------------------------------------------------
# Inventaire de démarrage (aucune donnée personnelle)
# --------------------------------------------------------------------------

def _local_ipv4_addresses() -> list:
    found = []
    try:
        with open("/proc/net/fib_trie", encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return found
    for index, line in enumerate(lines[:-1]):
        candidate = line.strip().lstrip("|-+` ").strip()
        if parse_plain(candidate) is not None and "/32 host LOCAL" in lines[index + 1]:
            if candidate not in found:
                found.append(candidate)
    return found


def _local_ipv6_addresses() -> list:
    found = []
    try:
        with open("/proc/net/if_inet6", encoding="ascii") as handle:
            for line in handle:
                raw = line.split()[0]
                text = ":".join(raw[i:i + 4] for i in range(0, 32, 4))
                address = parse_plain(text)
                if address is not None and address.compressed not in found:
                    found.append(address.compressed)
    except (OSError, IndexError):
        pass
    return found


def _resolve(host):
    if not host:
        return None
    out = {}
    for family, label in ((socket.AF_INET, "ipv4"), (socket.AF_INET6, "ipv6")):
        try:
            infos = socket.getaddrinfo(host, None, family, socket.SOCK_STREAM)
            out[label] = sorted({info[4][0] for info in infos})
        except OSError as exc:
            out[label] = {"error": type(exc).__name__}
    return out


def build_inventory(redactor, environ, listeners) -> dict:
    def describe_all(texts):
        return [redactor.describe(t) for t in texts]

    resolved = _resolve(environ.get("RAILWAY_PRIVATE_DOMAIN"))
    if resolved:
        resolved = {k: (describe_all(v) if isinstance(v, list) else v) for k, v in resolved.items()}
    return {
        "local_ipv4": describe_all(_local_ipv4_addresses()),
        "local_ipv6": describe_all(_local_ipv6_addresses()),
        "private_domain_resolution": resolved,
        "listeners": listeners,
        "railway": {name: environ.get(name) for name in RAILWAY_ENV_REPORTED if environ.get(name)},
        "python": sys.version.split()[0],
    }


# --------------------------------------------------------------------------
# Lancement
# --------------------------------------------------------------------------

def uvicorn_settings(mode) -> dict:
    """Paramètres proxy transmis à uvicorn.Config pour le mode demandé.
    `None` = paramètre NON transmis (défaut Uvicorn)."""
    if mode == "strict":
        return {"proxy_headers": False}
    if mode == "uvicorn_defaults":
        return {}
    if mode == "trust_all_negative_control":
        return {"proxy_headers": True, "forwarded_allow_ips": "*"}
    raise ValueError("mode inconnu")


def open_listeners(port, listen):
    """Sockets d'écoute : IPv4 0.0.0.0 (comme api.py) et IPv6 [::] en
    IPV6_V6ONLY (réseau privé Railway). Un échec IPv6 est rapporté, jamais
    masqué."""
    sockets, report = [], []
    if listen in ("dual", "v4"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sockets.append(sock)
        report.append({"family": "ipv4", "bind": "0.0.0.0", "port": sock.getsockname()[1]})
    if listen in ("dual", "v6"):
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(("::", port if listen == "v6" or not sockets else sockets[0].getsockname()[1]))
            sockets.append(sock)
            report.append({"family": "ipv6", "bind": "::", "v6only": True, "port": sock.getsockname()[1]})
        except OSError as exc:
            report.append({"family": "ipv6", "error": f"{type(exc).__name__}:{exc.errno}"})
    if not sockets:
        raise OSError("aucune socket d'écoute")
    return sockets, report


def build_server(environ):
    import uvicorn

    mode = environ.get("P1Q_UVICORN_MODE", "strict")
    if mode not in MODES:
        raise SystemExit(f"P1Q_UVICORN_MODE inconnu (attendu : {', '.join(MODES)})")
    port = int(environ.get("PORT", "8080"))
    listen = environ.get("P1Q_LISTEN", "dual")
    if listen not in ("dual", "v4", "v6"):
        raise SystemExit("P1Q_LISTEN inconnu (dual, v4, v6)")
    max_requests = int(environ.get("P1Q_MAX_REQUESTS", str(DEFAULT_MAX_REQUESTS)))
    proxy = uvicorn_settings(mode)
    redactor = Redactor()
    sockets, listeners = open_listeners(port, listen)
    receiver = Receiver(mode=mode, settings={}, redactor=redactor, max_requests=max_requests)
    config = uvicorn.Config(
        receiver, http="h11", loop="asyncio", ws="none", lifespan="off", interface="asgi3",
        access_log=False, server_header=False, log_level="warning", **proxy)
    config.load()
    config.loaded_app = RawPeerRecorder(config.loaded_app)
    env_value = environ.get("FORWARDED_ALLOW_IPS")
    receiver.settings = {
        "version": uvicorn.__version__,
        "proxy_headers": config.proxy_headers,
        "forwarded_allow_ips": redactor.redact_text(str(config.forwarded_allow_ips)),
        "forwarded_allow_ips_env_present": env_value is not None,
        "forwarded_allow_ips_env": None if env_value is None else redactor.redact_text(env_value),
        "proxy_middleware_installed": config.proxy_headers,
        "http": "h11",
    }
    receiver.inventory = build_inventory(redactor, environ, listeners)
    return uvicorn.Server(config), sockets, receiver


def main(environ=None) -> int:
    environ = dict(os.environ if environ is None else environ)
    banned = forbidden_variables(environ)
    if banned:
        print("p1q-receiver: environnement NON isolé, démarrage refusé ; variables : " + ", ".join(banned),
              file=sys.stderr, flush=True)
        return EXIT_ENVIRONMENT_NOT_ISOLATED
    server, sockets, receiver = build_server(environ)
    startup = {"p1q": "receiver-startup", "protocol": PROTOCOL_VERSION, "boot_id": receiver.boot_id,
               "mode": receiver.mode, "uvicorn": receiver.settings, "inventory": receiver.inventory}
    print("P1Q-STARTUP " + json.dumps(startup, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
          flush=True)
    server.run(sockets=sockets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
