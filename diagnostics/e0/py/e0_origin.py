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
requête, à l'échéance du TTL et à l'arrêt ; ≤ 4 Kio par ligne, sinon
enregistrement réduit explicitement marqué (`evidence_completeness` ∈
{complete, truncated, unusable}). La réponse n'est envoyée qu'APRÈS
journalisation réussie. `origin_received_ms` (UTC, ms) est capturé à l'entrée
du traitement, indépendamment du `ts` de l'attestation (aucune synchronisation
d'horloges n'est prouvée par cette mesure).

Confidentialité (I6) : jamais l'en-tête d'attestation brut, la valeur de
Cookie, une IP globale brute ni une clé. Toute valeur déclarative, cible,
méthode, champ d'attestation (`t`, `m`, `h`, `hi`) ou en-tête persisté passe
par `Redactor.safe_text` (adresses brutes, à zéros non significatifs ou
pourcent-encodées pseudonymisées) ; `target_b64` encode la cible SÛRE ; les
champs rédigés sont listés dans `redactions`. Les empreintes de contenus courts
porteurs d'une adresse sont des HMAC du démarrage (un SHA-256 nu se renverse).
Limite consignée : un encodage arbitraire choisi par le client (base64, entier,
hexadécimal) n'est pas reconnu ; le banc ne crée lui-même aucun encodage
réversible d'une adresse.
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
from e0_jsonl import ChainWriter, LineTooLong, canonical
from e0_keysets import KeySetError, KeySets
from e0_redact import MAX_VALUE_CHARS, Redactor
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


@dataclass
class Response:
    """Réponse DÉCIDÉE (envoyée seulement après journalisation réussie)."""
    status: int
    headers: list
    body: bytes
    observation: bool = False   # corps = observation, construit après finalisation de l'enregistrement


class CollectionFailed(RuntimeError):
    """La collecte indispensable a échoué : aucune réponse de succès ne doit suivre."""


# Champs conservés par la réduction « truncated » ; tout autre champ est remplacé
# par son empreinte dans `omitted`.
_TRUNCATED_KEEP = ("boot_id", "route", "processing", "http_version", "scheme", "target_len", "target_redacted", "target_hmac_boot",
                   "run", "run_seq", "test_id", "tid", "origin_received_ms", "origin_logged_ms", "plain_count_total",
                   "plain_count_case", "eng_post", "cmp_key_post_id", "cmp_pre_headers_at_origin", "replica",
                   "status", "body", "redactions", "truncated_fields", "extra")
_ATT_CORE = ("att_present", "att_header_count", "att_crypto_valid", "key_origin", "att_policy_accepted", "refusal",
             "aud", "kid", "ts", "nonce", "signed_ip")
_ATT_TEXT = ("m", "h", "hi", "t")
TRUNCATED_TEXT_CHARS = 128
TRUNCATED_TARGET_CHARS = 256
SMALL_BODY_SCAN_BYTES = 4096


class _Notes(set):
    """Champs rédigés (adresse sensible) ; `clipped` : champs coupés à leur borne."""

    def __init__(self):
        super().__init__()
        self.clipped = set()


def _bounded_text(value, limit):
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[:limit] + f"…<+{len(value) - limit}>"


def _small_scalar(value, limit=64):
    """Valeur conservée telle quelle si courte (jeton valide), sinon None."""
    return value if isinstance(value, str) and len(value) <= limit else None


class OriginApp:
    """Application ASGI 3 de l'origine E0 (un démarrage = un `boot_id`).

    Ordre de traitement d'une requête HTTP :
      1. `origin_received_ms` (UTC, ms) capturé à l'ENTRÉE, avant toute
         vérification, consommation de nonce, lecture de corps ou attente ;
      2. vérification de l'attestation (observations élémentaires) ;
      3. réponse DÉCIDÉE (corps lu, attente /e0/slow effectuée) ;
      4. enregistrement `origin-record` journalisé (réduit si nécessaire,
         jamais au-delà de 4 Kio) ;
      5. PUIS SEULEMENT envoi de la réponse. Si la collecte échoue, aucune
         réponse de succès n'est envoyée : 503 `e0_collection_failed` et arrêt.

    Annulation de la tâche (`asyncio.CancelledError`, distincte d'un
    `http.disconnect`) pendant la lecture du corps ou l'attente /e0/slow :
    l'enregistrement est journalisé (`processing = "cancelled"`, jamais
    `complete`), puis l'annulation est propagée, sans aucune réponse. Champ
    `processing` ∈ {completed, client_disconnected, cancelled, error} ; le
    statut journalisé est le statut DÉCIDÉ avant l'envoi (un échec ou une
    annulation pendant l'envoi est compté dans `send_failures`).

    Limites non garantissables (consignées) : perte brutale du processus
    (SIGKILL, OOM) entre l'écriture et le vidage, ou perte de la sortie
    standard en aval de l'appel système d'écriture (journal de plateforme) ;
    une écriture PARTIELLEMENT réussie suivie d'une exception laisse un
    fragment avant la ligne réémise : aucune unicité n'est revendiquée (au
    moins une émission complète par ligne engagée, fragment ou doublon
    possibles, signalés par la vérification de chaîne et `emit_retries`) ;
    une requête refusée par le serveur HTTP avant l'application (en-têtes
    au-delà des limites de h11) n'atteint pas l'origine et n'est pas observée.
    L'absence de trace n'est JAMAIS une preuve de non-invocation.
    """

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
        self.reduced_records = {"truncated": 0, "unusable": 0}
        self.emit_retries = 0
        self.collection_failures = 0
        self.send_failures = 0
        self.cancelled_requests = 0
        self.collection_broken = False
        self.on_exhausted = None
        self._log_lock = threading.Lock()

    # ----- journal -----

    def _write(self, record_type: str, body: dict) -> str:
        """Construit, émet (une nouvelle tentative au plus), PUIS engage la
        ligne : une ligne non émise n'avance jamais la tête de chaîne. Le
        point de contrôle du TTL vient d'un autre fil : chaînage sérialisé."""
        with self._log_lock:
            line = self.writer.build(record_type, body)   # LineTooLong possible
            for attempt in (1, 2):
                try:
                    self.emit(line)
                    break
                except Exception as exc:  # sortie indisponible
                    if attempt == 2:
                        self.collection_failures += 1
                        self.collection_broken = True
                        raise CollectionFailed(type(exc).__name__) from exc
                    self.emit_retries += 1
            self.writer.commit(line)
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
                "tls": self.config.tls, "replica": self.config.replica_id,
                "origin_time_ms": self.clock_ms()}
        body.update(extra)
        return self._write("origin-startup", body)

    def checkpoint(self, reason: str) -> str:
        return self._write("origin-checkpoint", {
            "boot_id": self.boot_id, "reason": reason, "head": self.writer.head,
            "request_records": self.request_records, "requests": self.request_count, "accepted": self.accepted,
            "refused": self.refused, "plain_count_total": self.plain_total,
            "plain_count_by_case": dict(sorted(self.plain_by_case.items())),
            "reduced_records": dict(self.reduced_records), "emit_retries": self.emit_retries,
            "collection_failures": self.collection_failures, "send_failures": self.send_failures,
            "cancelled_requests": self.cancelled_requests,
            "origin_time_ms": self.clock_ms()})

    @staticmethod
    def _digest(value) -> str:
        return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()

    def _truncated(self, record: dict) -> dict:
        reduced = {k: record[k] for k in _TRUNCATED_KEEP if k in record}
        for name in ("run", "run_seq", "test_id"):
            if isinstance(reduced.get(name), dict):
                reduced[name] = {"invalid": True, "hmac_boot": reduced[name].get("hmac_boot")}
        if isinstance(reduced.get("extra"), dict):
            reduced["extra"] = {"omitted": True, "sha256": self._digest(reduced["extra"])}
        reduced["method"] = _bounded_text(record.get("method"), 32)
        target = record.get("target_b64") or ""
        reduced["target_b64"] = target[:TRUNCATED_TARGET_CHARS]
        reduced["target_b64_truncated"] = len(target) > TRUNCATED_TARGET_CHARS
        attestation = record.get("attestation") or {}
        reduced["attestation"] = {k: attestation.get(k) for k in _ATT_CORE}
        for name in _ATT_TEXT:
            reduced["attestation"][name] = _bounded_text(attestation.get(name), TRUNCATED_TEXT_CHARS)
        reduced["omitted"] = {k: self._digest(v) for k, v in sorted(record.items())
                              if k not in reduced and k != "attestation"}
        reduced["evidence_completeness"] = "truncated"
        return reduced

    def _unusable(self, record: dict) -> dict:
        """Forme minimale BORNÉE PAR CONSTRUCTION (champs de taille fixe)."""
        attestation = record.get("attestation") or {}
        signed = attestation.get("signed_ip")
        return {"boot_id": record.get("boot_id"), "route": _small_scalar(record.get("route"), 16),
                "processing": _small_scalar(record.get("processing"), 24),
                "status": record.get("status") if isinstance(record.get("status"), int) else None,
                "origin_received_ms": record.get("origin_received_ms"),
                "origin_logged_ms": record.get("origin_logged_ms"),
                "run": _small_scalar(record.get("run")), "run_seq": _small_scalar(record.get("run_seq")),
                "test_id": _small_scalar(record.get("test_id")), "tid": _small_scalar(record.get("tid")),
                "attestation": {"att_present": bool(attestation.get("att_present")),
                                "att_crypto_valid": bool(attestation.get("att_crypto_valid")),
                                "att_policy_accepted": bool(attestation.get("att_policy_accepted")),
                                "key_origin": _small_scalar(attestation.get("key_origin"), 8),
                                "refusal": _small_scalar(attestation.get("refusal"), 32),
                                "kid": _small_scalar(attestation.get("kid"), 24),
                                "nonce": _small_scalar(attestation.get("nonce"), 22),
                                "signed_ip": signed if isinstance(signed, dict) and len(canonical(signed)) < 128
                                else None},
                "eng_post": _small_scalar(record.get("eng_post")),
                "cmp_key_post_id": _small_scalar(record.get("cmp_key_post_id"), 20),
                "record_sha256": self._digest(record), "evidence_completeness": "unusable"}

    def _log_request(self, record: dict) -> None:
        """Toujours une ligne ≤ 4 Kio : complète, sinon réduite (`truncated`),
        sinon minimale (`unusable`). CollectionFailed si la sortie est perdue."""
        # Une valeur coupée à sa borne, ou un traitement annulé ou en erreur, n'est
        # jamais présenté comme complet.
        incomplete = record.get("truncated_fields") or record.get("processing") in ("cancelled", "error")
        record = dict(record, evidence_completeness="truncated" if incomplete else "complete")
        for stage in ("complete", "truncated", "unusable"):
            candidate = record if stage == "complete" else (
                self._truncated(record) if stage == "truncated" else self._unusable(record))
            try:
                self._write("origin-record", candidate)
            except LineTooLong:
                if stage == "unusable":  # impossible par construction ; jamais silencieux
                    self.collection_failures += 1
                    self.collection_broken = True
                    raise CollectionFailed("LineTooLong")
                continue
            if candidate["evidence_completeness"] != "complete":
                self.reduced_records[candidate["evidence_completeness"]] += 1
            break
        self.request_records += 1
        if self.request_records % CHECKPOINT_EVERY == 0:
            self.checkpoint("periodic")

    # ----- valeurs sûres -----

    def _values(self, headers, name):
        return [value for key, value in headers if key == name]

    def _safe(self, field, value, redactions):
        """Texte publiable ; le champ est consigné dans `redactions` s'il
        contenait une adresse sensible (brute ou encodée)."""
        text, sensitive = self.redactor.safe_text(value)
        if sensitive:
            redactions.add(field)
        if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
            redactions.clipped.add(field)
        return text

    def _token(self, headers, name, redactions):
        """Identifiant de corrélation : exploitable seulement s'il est unique,
        conforme à la grammaire ET sans adresse sensible ; sinon représentation
        bornée et sûre, avec empreinte HMAC du démarrage."""
        values = self._values(headers, name)
        if not values:
            return None
        if len(values) == 1 and _TOKEN.fullmatch(values[0]) and not self.redactor.contains_sensitive(values[0]):
            return values[0]
        return {"invalid": True, "count": len(values),
                "redacted": [self._safe(name, v, redactions)[:64] for v in values[:4]],
                "hmac_boot": self.redactor.keyed_digest("\n".join(values))}

    def _describe_headers(self, headers, names, redactions, *, as_ip):
        out = {}
        for name in names:
            values = self._values(headers, name)
            if values:
                if as_ip:
                    described = [self.redactor.describe(v) for v in values]
                    if any(d.get("redacted") or (d.get("parsed") and d["text"] != v)
                           for d, v in zip(described, values)):
                        redactions.add(name)
                    out[name] = described
                else:
                    out[name] = [self._safe(name, v, redactions) for v in values]
        return out

    def _content_digest(self, field, data: bytes, redactions):
        """SHA-256 de contenu (RT-04, RT-10) ; HMAC du démarrage si un contenu
        COURT contient une adresse sensible (un SHA-256 nu se renverserait)."""
        if len(data) <= SMALL_BODY_SCAN_BYTES and self.redactor.contains_sensitive(data.decode("latin-1")):
            redactions.add(field)
            return {"hmac_boot": self.redactor.keyed_digest(data.decode("latin-1"))}
        return {"sha256": hashlib.sha256(data).hexdigest()}

    def _edge_diag(self, headers, redactions):
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
            return {"authenticated": False, "parsed": False, "hmac_boot": self.redactor.keyed_digest(raw)}
        if not isinstance(document, dict):
            return {"authenticated": False, "parsed": False, "hmac_boot": self.redactor.keyed_digest(raw)}
        fields = {}
        for key, kind in _DIAG_KEYS.items():
            value = document.get(key)
            if value is None or type(value) is not kind:
                continue
            if kind is str:
                fields[key] = self._safe("edge_diag", value, redactions)[:64]
                if len(value) > 64:
                    redactions.clipped.add("edge_diag")
            elif kind is list:
                fields[key] = [self._safe("edge_diag", str(v), redactions)[:64] for v in value[:16]]
                if len(value) > 16 or any(len(str(v)) > 64 for v in value):
                    redactions.clipped.add("edge_diag")
            else:
                fields[key] = value
        return {"authenticated": False, "parsed": True, "fields": fields}

    def _attestation_record(self, evaluation, signed_ip_record, redactions):
        record = evaluation.record()
        for name in _ATT_TEXT:
            if isinstance(record.get(name), str):
                record[name] = self._safe("attestation." + name, record[name], redactions)
        record["signed_ip"] = signed_ip_record
        return record

    def _observation(self, scope, headers, route, raw_target, evaluation, signed_ip_record, eng_post, test_id,
                     test_key, received_ms, redactions):
        peer = scope.get("client")
        cookies = self._values(headers, "cookie")
        safe_target, target_sensitive = self.redactor.safe_text(raw_target)
        if target_sensitive:
            redactions.add("target")
        if len(raw_target) > MAX_VALUE_CHARS:
            redactions.clipped.add("target")
        record = {
            "boot_id": self.boot_id, "route": route, "origin_received_ms": received_ms,
            "method": self._safe("method", scope.get("method", ""), redactions),
            "http_version": scope.get("http_version"), "scheme": scope.get("scheme"),
            # base64url d'une cible SÛRE (jamais un encodage réversible d'une adresse brute)
            "target_b64": b64url_encode(safe_target.encode("ascii", "backslashreplace")),
            "target_len": len(raw_target), "target_redacted": target_sensitive,
            "target_hmac_boot": self.redactor.keyed_digest(raw_target) if target_sensitive else None,
            "run": self._token(headers, RUN_HEADER, redactions), "run_seq": self._token(headers, SEQ_HEADER,
                                                                                         redactions),
            "test_id": test_id, "tid": guarded_tid(scope.get("raw_path") or b"") if route == "guarded" else None,
            "host": [self._safe("host", v, redactions) for v in self._values(headers, "host")],
            "origin": [self._safe("origin", v, redactions) for v in self._values(headers, "origin")],
            "referer": [self._safe("referer", v, redactions) for v in self._values(headers, "referer")],
            "sec_fetch": self._describe_headers(headers, SEC_FETCH, redactions, as_ip=False),
            "cookie": dict({"count": len(cookies)},
                           **(self._content_digest("cookie", "\n".join(cookies).encode("latin-1"), redactions)
                              if cookies else {"sha256": None})),
            "xfp": [self._safe("xfp", v, redactions) for v in self._values(headers, "x-forwarded-proto")],
            "plain_count_total": self.plain_total, "plain_count_case": self.plain_by_case.get(test_key, 0),
            "ip_headers": self._describe_headers(headers, IP_HEADERS, redactions, as_ip=True),
            "markers": self._describe_headers(headers, MARKER_HEADERS, redactions, as_ip=False),
            "attestation": self._attestation_record(evaluation, signed_ip_record, redactions),
            "eng_post": eng_post,
            "cmp_key_post_id": None if eng_post is None else self.config.post_key.key_id,
            "cmp_pre_headers_at_origin": len(self._values(headers, CMP_PRE_KEY_HEADER))
            + len(self._values(headers, CMP_PRE_MAC_HEADER)),
            "edge_diag": self._edge_diag(headers, redactions),
            "peer": None if not peer else {"addr": self.redactor.describe(str(peer[0])),
                                            "port": peer[1] if len(peer) > 1 else None},
            "replica": self.config.replica_id,
        }
        return record

    # ----- réponses -----

    async def _send(self, send, status, headers, body, method):
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})

    def _headers(self, content_type, body, extra=()):
        headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode("ascii")),
                   (b"cache-control", b"no-store"), (ORIGIN_HEADER.encode("ascii"), PROTOCOL.encode("ascii"))]
        return headers + list(extra)

    def _json(self, status, document, extra=()) -> Response:
        body = (json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        return Response(status, self._headers(b"application/json", body, extra), body)

    @staticmethod
    def _refusal() -> Response:
        """503 de maintenance F04-C § 4, octets constants (aucun en-tête ajouté)."""
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(REFUSAL_BODY)).encode("ascii"))]
        headers += [(k.encode("ascii"), v.encode("ascii")) for k, v in REFUSAL_NO_STORE_HEADERS]
        return Response(REFUSAL_STATUS, headers, REFUSAL_BODY)

    # ----- ASGI -----

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        received_ms = self.clock_ms()  # AVANT toute vérification, lecture, attente ou réponse
        self.request_count += 1
        redactions = _Notes()
        method = scope.get("method", "")
        headers = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope.get("headers") or ()]
        raw_path = scope.get("raw_path") or scope.get("path", "").encode("latin-1")
        query = scope.get("query_string") or b""
        raw_target = raw_path.decode("latin-1") + ("?" + query.decode("latin-1") if query else "")
        route = route_of(method, raw_path, scope.get("path", ""))
        test_id = self._token(headers, CASE_HEADER, redactions)
        test_key = test_id if isinstance(test_id, str) else "_none"
        if any(v.strip().lower() == "http" for v in self._values(headers, "x-forwarded-proto")):
            self.plain_total += 1
            self.plain_by_case[test_key] = self.plain_by_case.get(test_key, 0) + 1

        # La vérification opère sur les valeurs BRUTES en mémoire ; seule leur
        # forme sûre est persistée.
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
                                   test_id, test_key, received_ms, redactions)
        extra, response, error, cancelled = {}, None, None, None
        try:
            response = await self._dispatch(route, method, scope, receive, record, evaluation, extra, redactions)
            processing = "completed" if response is not None else "client_disconnected"
        except asyncio.CancelledError as exc:
            # Annulation de la tâche ASGI (arrêt du serveur, délai externe…) : distincte
            # d'un http.disconnect. L'observation est journalisée, PUIS l'annulation propagée.
            cancelled, processing = exc, "cancelled"
            extra.setdefault("body", {"read": False, "state": "interrupted"})
        except Exception as exc:
            error, processing = exc, "error"
            extra["error"] = type(exc).__name__
        record["processing"] = processing
        record["status"] = None if response is None else response.status
        record["body"] = extra.pop("body", {"read": False})
        if extra:
            record["extra"] = extra
        record["redactions"] = sorted(redactions)
        record["truncated_fields"] = sorted(redactions.clipped)
        if response is not None and response.observation:
            visible = {k: v for k, v in record.items() if k not in ("eng_post", "cmp_key_post_id")}
            body = (json.dumps({"observation": visible}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                    + "\n").encode("ascii")
            response.headers = self._headers(b"application/json", body)
            response.body = body
        record["origin_logged_ms"] = self.clock_ms()
        if cancelled is not None:
            self.cancelled_requests += 1
            try:
                self._log_request(record)  # synchrone : ne peut pas être interrompu par l'annulation
            except CollectionFailed:
                if self.on_exhausted is not None:
                    self.on_exhausted()
            raise cancelled  # jamais de réponse, jamais un succès
        try:
            self._log_request(record)
        except CollectionFailed:
            # Jamais de réponse de succès après un échec de collecte : refus et arrêt.
            if self.on_exhausted is not None:
                self.on_exhausted()
            try:
                await self._send(send, 503, *self._failure_parts(), method)
            except asyncio.CancelledError:
                self.send_failures += 1
                raise
            except Exception:
                self.send_failures += 1
            return
        if error is not None:
            raise error
        if response is not None:
            try:
                await self._send(send, response.status, response.headers, response.body, method)
            except (Exception, asyncio.CancelledError):
                # L'enregistrement déjà journalisé porte le statut DÉCIDÉ ; l'échec d'envoi
                # (ou l'annulation pendant l'envoi) est compté au point de contrôle.
                self.send_failures += 1
                raise

    def _failure_parts(self):
        response = self._json(503, {"error": "e0_collection_failed"})
        return response.headers, response.body

    async def _dispatch(self, route, method, scope, receive, record, evaluation, extra, notes):
        if self.collection_broken:
            return self._json(503, {"error": "e0_collection_failed"})
        if self.request_count > self.config.max_requests:
            if self.on_exhausted is not None:
                self.on_exhausted()
            return self._json(503, {"error": "e0_budget_exhausted"})
        if route == "health":
            return self._json(200, {"status": "ok", "protocol": PROTOCOL, "boot_id": self.boot_id})
        if route == "guarded":
            if not evaluation.att_policy_accepted:
                return self._refusal()
            body, size, state = await _read_body(receive, self.config.body_max)
            extra["body"] = self._body_record(body, size, state, notes)
            if state != "ok":
                return self._body_problem(state)
            return self._json(200, {"guarded": "accepted", "boot_id": self.boot_id, "tid": record["tid"]})
        if route == "slow":
            return await self._slow(scope, receive, method, extra, notes)
        if route == "cookies":
            body = b'{"cookies":3}\n'
            return Response(200, self._headers(b"application/json", body,
                                               [(b"set-cookie", c) for c in SYNTHETIC_COOKIES]), body)
        if route == "redirect":
            location = ("https://" + self.config.public_host + "/e0/observe?redirected=1").encode("ascii")
            return Response(302, self._headers(b"text/plain", b"", [(b"location", location)]), b"")
        if route == "page":
            return Response(200, self._headers(b"text/html; charset=utf-8", PAGE_HTML,
                                               [(b"content-security-policy", b"default-src 'self'")]), PAGE_HTML)
        body, size, state = await _read_body(receive, self.config.body_max)
        extra["body"] = self._body_record(body, size, state, notes)
        if state != "ok":
            return self._body_problem(state)
        return Response(200, [], b"", observation=True)

    def _body_record(self, body, size, state, notes):
        """Le champ `body` rédigé est reporté dans les métadonnées de la requête
        (`redactions`), comme tout autre champ."""
        out = {"read": True, "state": state, "length": size}
        if body is None:
            out["sha256"] = None
        else:
            local = set()
            out.update(self._content_digest("body", body, local))
            if local:
                out["redacted"] = True
                notes.add("body")
        return out

    def _body_problem(self, state):
        if state == "too_large":
            return self._json(413, {"error": "e0_body_too_large"})
        return None  # client déconnecté : aucune réponse possible

    async def _slow(self, scope, receive, method, extra, notes):
        params = self._slow_params(scope.get("query_string") or b"")
        if params is None:
            return self._json(400, {"error": "e0_slow_parameters"})
        delay, size = params
        if self.slow_active >= self.config.slow_concurrency:
            return self._json(503, {"error": "e0_slow_busy"})
        self.slow_active += 1
        started = time.monotonic()
        try:
            body, length, state = await _read_body(receive, self.config.body_max)
            extra["body"] = self._body_record(body, length, state, notes)
            if state != "ok":
                return self._body_problem(state)
            disconnected = await _wait_or_disconnect(receive, delay)
            extra["slow"] = {"d": delay, "n": size, "client_disconnected": disconnected,
                             "waited_ms": int((time.monotonic() - started) * 1000)}
            if disconnected:
                return None
            if method == "POST":
                return self._json(200, {"length": length, "sha256": hashlib.sha256(body).hexdigest(), "d": delay})
            payload = deterministic_body(size)
            return Response(200, self._headers(b"application/octet-stream", payload), payload)
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
