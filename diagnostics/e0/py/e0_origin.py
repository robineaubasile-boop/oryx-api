"""E0 — application ASGI e0-origin (origine de diagnostic, spécification V0.2-R3 § 2.2).

Code TEMPORAIRE et ISOLÉ du runtime Oryx : bibliothèque standard + modules
e0_* (guard, redact, ipclass, ipcompare, ed25519, attest, keysets, jsonl).
Aucun import d'un module Oryx, aucune base, aucun appel sortant.

Routes :
  GET|HEAD /e0/health    identification (x-e0-origin, boot_id)
  /e0/observe            observation pseudonymisée (toutes méthodes)
  /e0/slow?d=&n=         attente SANS octet anticipé (GET) ; corps lu, attente, longueur et SHA-256 (POST)
  /e0/cookies            trois Set-Cookie __Host-e0-* synthétiques
  /e0/redirect           302 vers https://<hôte public>/e0/observe?redirected=1
  /e0/guarded[/<tid>…]   PRÉFIXE PROTÉGÉ : vérificateur gardé ; 200 si attestation
                         acceptée, sinon 503 {"error":"temporarily_unavailable"}
                         octet pour octet (F04-C § 4), sans lecture du corps
  /e0/page               page statique (CSP default-src 'self'), instrument de mesure
  toute autre cible      observation (catch-all) ; le préfixe protégé en est EXCLU

Le préfixe protégé est reconnu sur la cible BRUTE (`raw_path`) ET sur le chemin
décodé : toute cible dont les deux premiers segments, sous l'une ou l'autre
forme, sont `e0` puis `guarded` est routée vers le vérificateur gardé.

Observations élémentaires d'attestation pour CHAQUE requête : `att_present`,
`att_crypto_valid` (avec kid, aud et origine current/audit),
`att_policy_accepted`, et `t`, `nonce`, `ts`, `x-e0-run`, `x-e0-seq`, `tid`
observés. L'origine ne produit JAMAIS `att_issued_evidence` ; elle n'expose
AUCUN endpoint de comparaison IP. `eng_post` est calculé transitoirement,
l'IP signée étant écartée aussitôt ; seuls son pseudonyme par démarrage,
`eng_post` et `cmp_key_post_id` sont journalisés.

Journal : une ligne `E0-ORIGIN {json}` par requête sur stdout, chaînée
(e0_jsonl), point de contrôle `origin-checkpoint` toutes les 100 lignes de
requête, à l'échéance du TTL et à l'arrêt ; ≤ 4 Kio par ligne. Jamais l'en-tête
d'attestation brut, la valeur de Cookie, une IP globale brute ni une clé.
"""
import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass

import e0_guard
from e0_attest import InMemoryNonceStore, PhaseVerifier, b64url_encode
from e0_ipclass import signable_address
from e0_ipcompare import KEY_ID, PostKey, key_from_hex
from e0_jsonl import ChainWriter, LineTooLong
from e0_keysets import KeySetError, KeySets
from e0_redact import Redactor
from e0_version import ATTESTATION_HEADER, CASE_HEADER, CMP_PRE_KEY_HEADER, CMP_PRE_MAC_HEADER, \
    EDGE_DIAG_HEADER, GUARDED_SEGMENTS, ORIGIN_HEADER, PHASE_AUDIENCE, PHASES, PROTOCOL, REFUSAL_BODY, \
    REFUSAL_NO_STORE_HEADERS, REFUSAL_STATUS, RUN_HEADER, SEQ_HEADER

LOG_PREFIX = "E0-ORIGIN "
CHECKPOINT_EVERY = 100
LINE_MAX_BYTES = 4096
NONCE_STORE_MAX = 100000

# Bornes : miroir de config/phase_params.v1.json (égalité vérifiée par les tests).
PARAM_BOUNDS = {
    "E0_TTL_SECONDS": (14400, 60, 86400),
    "E0_MAX_REQUESTS": (20000, 1, 1000000),
    "E0_W_MS": (30000, 1000, 300000),
    "E0_S_MS": (5000, 0, 60000),
    "E0_SLOW_MAX_S": (300, 1, 300),
    "E0_SLOW_CONCURRENCY": (8, 1, 8),
    "E0_BODY_MAX": (524288, 1, 524288),
}

IP_HEADERS = ("cf-connecting-ip", "cf-connecting-ipv6", "cf-pseudo-ipv4", "x-real-ip", "true-client-ip",
              "x-forwarded-for", "forwarded", "x-client-ip", "x-cluster-client-ip", "fastly-client-ip")
MARKER_HEADERS = ("cf-worker", "cf-ew-via", "cf-ray", "cf-ipcountry", "cdn-loop")
SEC_FETCH = ("sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_TID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_INT = re.compile(r"0|[1-9][0-9]{0,6}")
_DIAG_KEYS = {"phase": str, "class": str, "codes": list, "markers": list, "asn": int, "httpProtocol": str,
              "tlsVersion": str, "signed": bool}

SYNTHETIC_COOKIES = (
    b"__Host-e0-a=e0-synthetic-a; Secure; Path=/; HttpOnly; SameSite=Strict",
    b"__Host-e0-b=e0-synthetic-b; Secure; Path=/; HttpOnly; SameSite=Strict",
    b"__Host-e0-c=e0-synthetic-c; Secure; Path=/; HttpOnly; SameSite=Strict",
)
PAGE_HTML = b"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>E0 - instrument de mesure</title></head>
<body>
<h1>E0 - instrument de mesure (aucune donnee reelle)</h1>
<p>Requetes same-origin vers /e0/observe ; l'observation JSON rendue peut etre enregistree par le navigateur.</p>
<p><a href="/e0/observe?from=page">GET /e0/observe</a></p>
<form method="post" action="/e0/observe?from=page"><input type="hidden" name="e0" value="page">
<button type="submit">POST /e0/observe</button></form>
</body></html>
"""


class OriginConfigError(ValueError):
    """Configuration de l'origine refusée (refus fermé au démarrage)."""


@dataclass(frozen=True)
class OriginConfig:
    phase: str
    public_host: str
    keysets: KeySets
    post_key: object
    ttl_seconds: int
    max_requests: int
    w_ms: int
    s_ms: int
    slow_max_s: int
    slow_concurrency: int
    body_max: int
    replica_id: object
    tls: bool


def _bounded(environ, name) -> int:
    default, low, high = PARAM_BOUNDS[name]
    text = environ.get(name)
    if text is None:
        return default
    if not isinstance(text, str) or not _INT.fullmatch(text):
        raise OriginConfigError(f"{name} : entier décimal attendu")
    value = int(text)
    if not low <= value <= high:
        raise OriginConfigError(f"{name} hors bornes [{low}, {high}]")
    return value


def config_from_environment(environ) -> OriginConfig:
    """Configuration stricte ; OriginConfigError au moindre écart."""
    if environ.get("E0_ROLE") != "origin":
        raise OriginConfigError("E0_ROLE=origin attendu")
    phase = environ.get("E0_PHASE")
    if phase not in PHASES:
        raise OriginConfigError("E0_PHASE inconnue")
    try:
        public_host = e0_guard.check_public_host(environ.get("E0_PUBLIC_HOST"))
    except e0_guard.GuardError as exc:
        raise OriginConfigError(f"E0_PUBLIC_HOST : {exc}") from exc
    try:
        keysets = KeySets.from_environment(phase, environ)
    except KeySetError as exc:
        raise OriginConfigError(f"clés : {exc}") from exc
    key_text, key_id = environ.get("E0_CMP_KEY_POST"), environ.get("E0_CMP_KEY_POST_ID")
    post_key = None
    if key_text is not None or key_id is not None:
        if key_text is None or key_id is None or not KEY_ID.fullmatch(key_id):
            raise OriginConfigError("E0_CMP_KEY_POST et E0_CMP_KEY_POST_ID vont ensemble, identifiant cmp-<16 hex>")
        try:
            post_key = PostKey(key_from_hex(key_text), expected_id=key_id)
        except ValueError as exc:
            raise OriginConfigError(f"clé post-signature : {exc}") from exc
    tls_cert, tls_key = environ.get("E0_TLS_CERT_PEM_B64"), environ.get("E0_TLS_KEY_PEM_B64")
    tls = tls_cert is not None or tls_key is not None
    if tls and (phase != "1c" or tls_cert is None or tls_key is None):
        raise OriginConfigError("mode TLS de l'origine réservé à la phase 1c, certificat et clé ensemble")
    return OriginConfig(
        phase=phase, public_host=public_host, keysets=keysets, post_key=post_key,
        ttl_seconds=_bounded(environ, "E0_TTL_SECONDS"), max_requests=_bounded(environ, "E0_MAX_REQUESTS"),
        w_ms=_bounded(environ, "E0_W_MS"), s_ms=_bounded(environ, "E0_S_MS"),
        slow_max_s=_bounded(environ, "E0_SLOW_MAX_S"), slow_concurrency=_bounded(environ, "E0_SLOW_CONCURRENCY"),
        body_max=_bounded(environ, "E0_BODY_MAX"), replica_id=environ.get("RAILWAY_REPLICA_ID"), tls=tls)


# --------------------------------------------------------------------------
# Routage
# --------------------------------------------------------------------------

def _segments(path: str) -> list:
    return path.split("/")


def is_guarded(raw_path: bytes, path: str) -> bool:
    """Deux premiers segments `e0` puis `guarded`, sur la cible brute OU sur le
    chemin décodé (conservateur : aucune forme ne contourne le vérificateur)."""
    for candidate in (raw_path.decode("latin-1"), path or ""):
        parts = _segments(candidate)
        if len(parts) >= 3 and parts[0] == "" and (parts[1], parts[2]) == GUARDED_SEGMENTS:
            return True
    return False


def guarded_tid(raw_path: bytes):
    parts = _segments(raw_path.decode("latin-1"))
    if len(parts) >= 4 and (parts[1], parts[2]) == GUARDED_SEGMENTS and _TID.fullmatch(parts[3]):
        return parts[3]
    return None


def route_of(method: str, raw_path: bytes, path: str) -> str:
    if is_guarded(raw_path, path):
        return "guarded"
    exact = {b"/e0/observe": "observe", b"/e0/slow": "slow", b"/e0/cookies": "cookies",
             b"/e0/redirect": "redirect", b"/e0/page": "page"}
    if raw_path == b"/e0/health" and method in ("GET", "HEAD"):
        return "health"
    return exact.get(raw_path, "catch_all")


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

def _stdout_emit(line: str) -> None:
    sys.stdout.write(LOG_PREFIX + line + "\n")
    sys.stdout.flush()


async def _read_body(receive, limit):
    """(corps | None, taille, état) ; état ∈ {"ok", "too_large", "disconnected"}."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None, size, "disconnected"
        body = message.get("body", b"")
        size += len(body)
        if size > limit:
            return None, size, "too_large"
        chunks.append(body)
        if not message.get("more_body", False):
            return b"".join(chunks), size, "ok"


async def _wait_or_disconnect(receive, seconds) -> bool:
    """Attend `seconds` ; rend True si le client s'est déconnecté entre-temps."""

    async def disconnect():
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return

    sleeper = asyncio.ensure_future(asyncio.sleep(seconds))
    listener = asyncio.ensure_future(disconnect())
    try:
        done, _ = await asyncio.wait({sleeper, listener}, return_when=asyncio.FIRST_COMPLETED)
        return listener in done
    finally:
        sleeper.cancel()
        listener.cancel()


def deterministic_body(size: int) -> bytes:
    pattern = b"oryx-e0-slow-body/v1:0123456789abcdef\n"
    return (pattern * (size // len(pattern) + 1))[:size]


class OriginApp:
    """Application ASGI 3 de l'origine E0 (un démarrage = un `boot_id`)."""

    def __init__(self, config: OriginConfig, *, bench: str, emit=None, clock_ms=None):
        self.config = config
        self.boot_id = secrets.token_hex(16)
        self.redactor = Redactor()
        self.emit = emit or _stdout_emit
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.writer = ChainWriter(bench, max_line_bytes=LINE_MAX_BYTES)
        self.nonce_store = InMemoryNonceStore(NONCE_STORE_MAX)
        self.verifier = PhaseVerifier(phase=config.phase, keysets=config.keysets, public_host=config.public_host,
                                      w_ms=config.w_ms, s_ms=config.s_ms, nonce_store=self.nonce_store,
                                      clock_ms=self.clock_ms)
        self.request_count = 0
        self.request_records = 0
        self.accepted = 0
        self.refused = 0
        self.plain_total = 0
        self.plain_by_case = {}
        self.slow_active = 0
        self.on_exhausted = None
        self._log_lock = threading.Lock()

    # ----- journal -----

    def _write(self, record_type: str, body: dict) -> str:
        # Le point de contrôle du TTL est émis depuis un autre fil : chaînage sérialisé.
        with self._log_lock:
            line = self.writer.append(record_type, body)
            self.emit(line)
            return line

    def startup(self, extra: dict) -> str:
        body = {"boot_id": self.boot_id, "phase": self.config.phase,
                "audience": PHASE_AUDIENCE[self.config.phase], "public_host": self.config.public_host,
                "keysets": self.config.keysets.summary(),
                "cmp_key_post_id": None if self.config.post_key is None else self.config.post_key.key_id,
                "params": {"ttl_seconds": self.config.ttl_seconds, "max_requests": self.config.max_requests,
                           "w_ms": self.config.w_ms, "s_ms": self.config.s_ms,
                           "slow_max_s": self.config.slow_max_s, "slow_concurrency": self.config.slow_concurrency,
                           "body_max": self.config.body_max},
                "tls": self.config.tls, "replica": self.config.replica_id}
        body.update(extra)
        return self._write("origin-startup", body)

    def checkpoint(self, reason: str) -> str:
        return self._write("origin-checkpoint", {
            "boot_id": self.boot_id, "reason": reason, "head": self.writer.head,
            "request_records": self.request_records, "requests": self.request_count, "accepted": self.accepted,
            "refused": self.refused, "plain_count_total": self.plain_total,
            "plain_count_by_case": dict(sorted(self.plain_by_case.items()))})

    def _log_request(self, body: dict) -> None:
        try:
            self._write("origin-record", body)
        except LineTooLong:
            reduced = dict(body)
            for name in ("ip_headers", "markers", "host", "origin", "referer", "sec_fetch", "edge_diag", "xfp"):
                if name in reduced:
                    digest = hashlib.sha256(json.dumps(reduced[name], sort_keys=True).encode("ascii")).hexdigest()
                    reduced[name] = {"omitted": True, "sha256": digest}
            reduced["oversize"] = True
            self._write("origin-record", reduced)
        self.request_records += 1
        if self.request_records % CHECKPOINT_EVERY == 0:
            self.checkpoint("periodic")

    # ----- observation -----

    def _values(self, headers, name):
        return [value for key, value in headers if key == name]

    def _token(self, headers, name):
        values = self._values(headers, name)
        if not values:
            return None
        if len(values) == 1 and _TOKEN.fullmatch(values[0]):
            return values[0]
        return {"invalid": True, "count": len(values)}

    def _describe_headers(self, headers, names, *, as_ip):
        out = {}
        for name in names:
            values = self._values(headers, name)
            if values:
                out[name] = [self.redactor.describe(v) if as_ip else self.redactor.redact_text(v) for v in values]
        return out

    def _edge_diag(self, headers):
        values = self._values(headers, EDGE_DIAG_HEADER)
        if not values:
            return None
        if len(values) != 1:
            return {"authenticated": False, "parsed": False, "count": len(values)}
        raw = values[0]
        try:
            padded = raw + "=" * (-len(raw) % 4)
            document = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        except (ValueError, binascii.Error, UnicodeError):
            return {"authenticated": False, "parsed": False,
                    "sha256": hashlib.sha256(raw.encode("latin-1")).hexdigest()}
        if not isinstance(document, dict):
            return {"authenticated": False, "parsed": False}
        fields = {}
        for key, kind in _DIAG_KEYS.items():
            value = document.get(key)
            if value is None or type(value) is not kind:
                continue
            if kind is str:
                fields[key] = self.redactor.redact_text(value)[:64]
            elif kind is list:
                fields[key] = [self.redactor.redact_text(str(v))[:64] for v in value[:16]]
            else:
                fields[key] = value
        return {"authenticated": False, "parsed": True, "fields": fields}

    def _observation(self, scope, headers, route, raw_target, evaluation, signed_ip_record, eng_post, test_id,
                     test_key):
        peer = scope.get("client")
        cookies = self._values(headers, "cookie")
        xfp = self._values(headers, "x-forwarded-proto")
        return {
            "boot_id": self.boot_id, "route": route, "method": scope.get("method"),
            "http_version": scope.get("http_version"), "scheme": scope.get("scheme"),
            "target_b64": b64url_encode(raw_target.encode("latin-1")), "target_len": len(raw_target),
            "run": self._token(headers, RUN_HEADER), "run_seq": self._token(headers, SEQ_HEADER),
            "test_id": test_id, "tid": guarded_tid(scope.get("raw_path") or b"") if route == "guarded" else None,
            "host": [self.redactor.redact_text(v) for v in self._values(headers, "host")],
            "origin": [self.redactor.redact_text(v) for v in self._values(headers, "origin")],
            "referer": [self.redactor.redact_text(v) for v in self._values(headers, "referer")],
            "sec_fetch": self._describe_headers(headers, SEC_FETCH, as_ip=False),
            "cookie": {"count": len(cookies),
                       "sha256": hashlib.sha256("\n".join(cookies).encode("latin-1")).hexdigest() if cookies
                       else None},
            "xfp": [self.redactor.redact_text(v) for v in xfp],
            "plain_count_total": self.plain_total, "plain_count_case": self.plain_by_case.get(test_key, 0),
            "ip_headers": self._describe_headers(headers, IP_HEADERS, as_ip=True),
            "markers": self._describe_headers(headers, MARKER_HEADERS, as_ip=False),
            "attestation": dict(evaluation.record(), signed_ip=signed_ip_record),
            "eng_post": eng_post,
            "cmp_key_post_id": None if eng_post is None else self.config.post_key.key_id,
            "cmp_pre_headers_at_origin": len(self._values(headers, CMP_PRE_KEY_HEADER))
            + len(self._values(headers, CMP_PRE_MAC_HEADER)),
            "edge_diag": self._edge_diag(headers),
            "peer": None if not peer else {"addr": self.redactor.describe(str(peer[0])),
                                            "port": peer[1] if len(peer) > 1 else None},
            "replica": self.config.replica_id,
        }

    # ----- réponses -----

    async def _send(self, send, status, headers, body, method):
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})

    def _headers(self, content_type, body, extra=()):
        headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode("ascii")),
                   (b"cache-control", b"no-store"), (ORIGIN_HEADER.encode("ascii"), PROTOCOL.encode("ascii"))]
        return headers + list(extra)

    async def _json(self, send, status, document, method, extra=()):
        body = (json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        await self._send(send, status, self._headers(b"application/json", body, extra), body, method)

    async def _refusal(self, send, method):
        """503 de maintenance F04-C § 4, octets constants (aucun en-tête ajouté)."""
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(REFUSAL_BODY)).encode("ascii"))]
        headers += [(k.encode("ascii"), v.encode("ascii")) for k, v in REFUSAL_NO_STORE_HEADERS]
        await self._send(send, REFUSAL_STATUS, headers, REFUSAL_BODY, method)

    # ----- ASGI -----

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        self.request_count += 1
        method = scope.get("method", "")
        headers = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope.get("headers") or ()]
        raw_path = scope.get("raw_path") or scope.get("path", "").encode("latin-1")
        query = scope.get("query_string") or b""
        raw_target = raw_path.decode("latin-1") + ("?" + query.decode("latin-1") if query else "")
        route = route_of(method, raw_path, scope.get("path", ""))
        test_id = self._token(headers, CASE_HEADER)
        test_key = test_id if isinstance(test_id, str) else "_none"
        if any(v.strip().lower() == "http" for v in self._values(headers, "x-forwarded-proto")):
            self.plain_total += 1
            self.plain_by_case[test_key] = self.plain_by_case.get(test_key, 0) + 1

        evaluation = self.verifier.evaluate(self._values(headers, ATTESTATION_HEADER), method=method,
                                            host_values=self._values(headers, "host"), raw_target=raw_target)
        signed_ip_record, eng_post = None, None
        signed_ip = evaluation.take_signed_ip()
        if signed_ip is not None:
            try:
                address = ipaddress.ip_address(signed_ip)
                signed_ip_record = {"c": "V", "reclass": "V" if signable_address(signed_ip) else "not_V",
                                    "text": self.redactor.address_text(address)}
                if self.config.post_key is not None:
                    eng_post = self.config.post_key.eng_post(signed_ip)
            finally:
                signed_ip = None
                address = None
        if evaluation.att_policy_accepted:
            self.accepted += 1
        elif evaluation.att_present:
            self.refused += 1

        record = self._observation(scope, headers, route, raw_target, evaluation, signed_ip_record, eng_post,
                                   test_id, test_key)
        extra, status = {}, None
        try:
            status = await self._dispatch(route, method, scope, receive, send, record, evaluation, extra)
        except Exception as exc:
            extra["error"] = type(exc).__name__
            raise
        finally:
            record["status"] = status
            record["body"] = extra.pop("body", {"read": False})
            if extra:
                record["extra"] = extra
            self._log_request(record)

    async def _dispatch(self, route, method, scope, receive, send, record, evaluation, extra):
        if self.request_count > self.config.max_requests:
            await self._json(send, 503, {"error": "e0_budget_exhausted"}, method)
            if self.on_exhausted is not None:
                self.on_exhausted()
            return 503
        if route == "health":
            await self._json(send, 200, {"status": "ok", "protocol": PROTOCOL, "boot_id": self.boot_id}, method)
            return 200
        if route == "guarded":
            if not evaluation.att_policy_accepted:
                await self._refusal(send, method)
                return REFUSAL_STATUS
            body, size, state = await _read_body(receive, self.config.body_max)
            extra["body"] = self._body_record(body, size, state)
            if state != "ok":
                return await self._body_problem(send, state, method)
            await self._json(send, 200, {"guarded": "accepted", "boot_id": self.boot_id, "tid": record["tid"]},
                             method)
            return 200
        if route == "slow":
            return await self._slow(scope, receive, send, method, extra)
        if route == "cookies":
            body = b'{"cookies":3}\n'
            await self._send(send, 200, self._headers(b"application/json", body,
                                                      [(b"set-cookie", c) for c in SYNTHETIC_COOKIES]), body, method)
            return 200
        if route == "redirect":
            location = ("https://" + self.config.public_host + "/e0/observe?redirected=1").encode("ascii")
            await self._send(send, 302, self._headers(b"text/plain", b"", [(b"location", location)]), b"", method)
            return 302
        if route == "page":
            await self._send(send, 200, self._headers(b"text/html; charset=utf-8", PAGE_HTML,
                                                      [(b"content-security-policy", b"default-src 'self'")]),
                             PAGE_HTML, method)
            return 200
        body, size, state = await _read_body(receive, self.config.body_max)
        extra["body"] = self._body_record(body, size, state)
        if state != "ok":
            return await self._body_problem(send, state, method)
        visible = {k: v for k, v in record.items() if k not in ("eng_post", "cmp_key_post_id")}
        visible["body"] = extra["body"]
        await self._json(send, 200, {"observation": visible}, method)
        return 200

    def _body_record(self, body, size, state):
        return {"read": True, "state": state, "length": size,
                "sha256": hashlib.sha256(body).hexdigest() if body is not None else None}

    async def _body_problem(self, send, state, method):
        if state == "too_large":
            await self._json(send, 413, {"error": "e0_body_too_large"}, method)
            return 413
        return None  # client déconnecté : aucune réponse possible

    async def _slow(self, scope, receive, send, method, extra):
        params = self._slow_params(scope.get("query_string") or b"")
        if params is None:
            await self._json(send, 400, {"error": "e0_slow_parameters"}, method)
            return 400
        delay, size = params
        if self.slow_active >= self.config.slow_concurrency:
            await self._json(send, 503, {"error": "e0_slow_busy"}, method)
            return 503
        self.slow_active += 1
        started = time.monotonic()
        try:
            body, length, state = await _read_body(receive, self.config.body_max)
            extra["body"] = self._body_record(body, length, state)
            if state != "ok":
                return await self._body_problem(send, state, method)
            disconnected = await _wait_or_disconnect(receive, delay)
            extra["slow"] = {"d": delay, "n": size, "client_disconnected": disconnected,
                             "waited_ms": int((time.monotonic() - started) * 1000)}
            if disconnected:
                return None
            if method == "POST":
                await self._json(send, 200, {"length": length, "sha256": hashlib.sha256(body).hexdigest(),
                                             "d": delay}, method)
                return 200
            payload = deterministic_body(size)
            await self._send(send, 200, self._headers(b"application/octet-stream", payload), payload, method)
            return 200
        finally:
            self.slow_active -= 1

    def _slow_params(self, query: bytes):
        seen = {}
        if query:
            for part in query.decode("latin-1").split("&"):
                name, sep, value = part.partition("=")
                if not sep or name not in ("d", "n") or name in seen or not _INT.fullmatch(value):
                    return None
                seen[name] = int(value)
        if "d" not in seen:
            return None
        delay, size = seen["d"], seen.get("n", 0)
        if delay > self.config.slow_max_s or size > self.config.body_max:
            return None
        return delay, size
