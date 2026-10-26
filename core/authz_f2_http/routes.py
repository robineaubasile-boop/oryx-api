"""GET /api/v2/me (R1 PR 5, C.5.2 §7) et PUT /api/v2/me/level (R1 PR 6) —
composition isolée seulement.

Ordre des contrôles, tous avant l'autorisation :

  1. méthode et chemin (routeur : 404 / 405) ;
  2. provenance (provenance.py)                         -> 403 origin_refused ;
  3. en-tête Cookie, parseur strict de PR 3              -> 400 invalid_request ;
  4. X-Oryx-Generation-Id : un seul, UUID canonique      -> 400 invalid_request ;
  5. aucune query                                        -> 400 invalid_request ;
  6. aucun corps : Content-Length > 4096 ou flux > 4096  -> 413 request_too_large ;
     tout autre corps, Content-Length dupliqué ou illisible -> 400 invalid_request ;
  7. cookie __Host-oryx-active-<generation_id> EXACT (jamais choisi
     implicitement parmi d'autres)                       -> 401 session_invalid ;
  8. AuthorizedTransactionRunner + ReadProfile           -> 200, 401
     session_invalid, 503 temporarily_unavailable.

Réponse 200 (R1 PR 6) : {"level": ..., "level_revision": "<décimal>",
"user_id": ...}, valeurs lues ENSEMBLE dans PostgreSQL (user_id du
principal serveur) ; level_revision, entier interne, est transporté en
chaîne décimale canonique. Aucun user_id client, aucune création de
profil ; un profil incohérent (niveau inconnu, révision hors contrat) :
503, jamais corrigé. Toute réponse porte
Cache-Control: no-store, Pragma: no-cache, X-Content-Type-Options: nosniff,
Referrer-Policy: no-referrer et AUCUN Set-Cookie : le nettoyage ciblé des
cookies reste propre à /api/auth/f2/session/resolve et /logout (PR 3).
Chaque requête réauthentifie le justificatif dans ses propres transactions
: un succès antérieur de /session/resolve ne confère aucun droit
réutilisable.

PUT /api/v2/me/level (R1 PR 6), corps exact
{"level": "debutant"|"avance", "expected_revision": "<décimal>"}. Ordre des
contrôles (le premier échec répond, aucun contrôle suivant ne s'exécute) :

  1. méthode et chemin (routeur : 404 / 405) ;
  2. provenance de mutation (Origin obligatoire)        -> 403 origin_refused ;
  3. en-tête Cookie, parseur strict de PR 3              -> 400 invalid_request ;
  4. CSRF oryx-f2-csrf-v1 de PR 3 (X-Oryx-CSRF unique, cookie
     __Host-oryx-csrf-binding, origine validée, floor(wall_clock())) :
     trousseau ou horloge indisponible               -> 503 temporarily_unavailable ;
     jeton absent, incorrect, mal lié ou expiré      -> 403 csrf_invalid ;
  5. X-Oryx-Generation-Id : un seul, UUID canonique      -> 400 invalid_request ;
  6. aucune query                                        -> 400 invalid_request ;
  7. Content-Type unique application/json, au plus un paramètre
     charset=utf-8                                       -> 415 invalid_request ;
  8. framing : tout Transfer-Encoding, Content-Length dupliqué, vide, signé,
     non ASCII ou mal formé                              -> 400 invalid_request ;
  9. taille : Content-Length valide > 4096               -> 413 request_too_large ;
     flux reçu > Content-Length annoncé, flux tronqué ou déconnexion
     (anomalie de framing, prioritaire)                  -> 400 invalid_request ;
     flux reçu > 4096 (sans Content-Length)              -> 413 request_too_large ;
 10. JSON UTF-8 : objet, clés exactes, sans doublon ni constante, puis level
     puis expected_revision ("0" ou [1-9][0-9]*, 19 caractères ASCII au plus,
     <= 2^63 - 1)                                        -> 400 invalid_request ;
 11. cookie __Host-oryx-active-<generation_id> EXACT     -> 401 session_invalid ;
 12. runner disponible                                   -> 503 temporarily_unavailable ;
 13. AuthorizedTransactionRunner + UpdateProfileLevel    -> 200, 401, 409
     level_revision_conflict, 503 ; résultat vérifié avant sérialisation.
"""
import json
import logging
import math
import re
from dataclasses import dataclass

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from core.auth_f2.crypto import Redacted
from core.auth_f2.service import SessionCredential
from core.auth_f2_http import csrf
from core.auth_f2_http.cookies import CookieHeaderRejected, canonical_uuid, parse_cookie_headers
from core.auth_f2_http.provenance import ProvenanceRejected
from core.auth_f2_http.routes import error_response, json_response
from core.authz_f2.commands import (
    MAX_LEVEL_REVISION,
    ProfileView,
    ReadProfile,
    UpdateProfileLevel,
    valid_level,
    valid_revision,
)
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import AUTHORIZED, REVISION_CONFLICT, SESSION_INVALID, AuthorizationOutcome
from core.authz_f2_http.provenance import personal_get_origin, personal_mutation_origin

logger = logging.getLogger("oryx.authz_f2.http")

NAMESPACE = "/api/v2"
GENERATION_HEADER = "x-oryx-generation-id"
MAX_BODY_BYTES = 4096
CSRF_HEADER = "x-oryx-csrf"
LEVEL_KEYS = frozenset({"level", "expected_revision"})
# Révision transportée : "0" ou sans zéro initial, chiffres ASCII seulement
# (jamais str.isdigit() ni \d, qui admettent d'autres chiffres Unicode).
_REVISION_RE = re.compile(r"0|[1-9][0-9]{0,18}")
_CONTENT_LENGTH_RE = re.compile(r"[0-9]+")


class Refusal(Exception):
    def __init__(self, http_status: int, code: str):
        super().__init__(code)
        self.http_status = http_status
        self.code = code


@dataclass(frozen=True)
class PersonalDependencies:
    # AuthorizedTransactionRunner ; absent : 503 après les contrôles de transport.
    runner: object
    allowed_origins: frozenset
    # R1 PR 6 : trousseau CSRF (core.auth_f2_http.csrf.CsrfKeyring) et horloge
    # murale ; absents ou défaillants : PUT /me/level refuse (503) au stade CSRF.
    csrf_keyring: object = None
    wall_clock: object = None


async def _refuse_body(request: Request) -> None:
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
        raise Refusal(400, "invalid_request")
    if lengths and int(lengths[0]) > MAX_BODY_BYTES:
        raise Refusal(413, "request_too_large")
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_BODY_BYTES:
                raise Refusal(413, "request_too_large")
    except ClientDisconnect:
        raise Refusal(400, "invalid_request") from None
    if received or (lengths and int(lengths[0]) > 0) or request.headers.getlist("transfer-encoding"):
        raise Refusal(400, "invalid_request")


def _generation_id(request: Request):
    values = request.headers.getlist(GENERATION_HEADER)
    if len(values) != 1:
        raise Refusal(400, "invalid_request")
    generation_id = canonical_uuid(values[0])
    if generation_id is None:
        raise Refusal(400, "invalid_request")
    return generation_id


def _profile_ok(outcome, generation_id) -> bool:
    return (isinstance(outcome, AuthorizationOutcome) and outcome.status == AUTHORIZED
            and isinstance(outcome.value, ProfileView)
            and isinstance(outcome.principal, AuthorizedPrincipal)
            and outcome.principal.generation_id == generation_id
            and outcome.value.user_id == outcome.principal.user_id and valid_level(outcome.value.level)
            and valid_revision(outcome.value.level_revision))


def _profile_body(view: ProfileView) -> dict:
    return {"user_id": view.user_id, "level": view.level, "level_revision": str(view.level_revision)}


def _check_csrf(request: Request, deps: PersonalDependencies, origin: str, cookies) -> None:
    """Protocole oryx-f2-csrf-v1 de PR 3 (csrf.verify_token), lié à
    l'origine validée. Dépendance absente ou horloge défaillante : 503 ;
    jeton absent, incorrect, mal lié ou expiré : 403."""
    if not isinstance(deps.csrf_keyring, csrf.CsrfKeyring) or not callable(deps.wall_clock):
        raise Refusal(503, "temporarily_unavailable")
    try:
        now = math.floor(deps.wall_clock())
    except Exception:  # horloge défaillante (exception, NaN, infini, type) : refus fermé
        raise Refusal(503, "temporarily_unavailable") from None
    tokens = request.headers.getlist(CSRF_HEADER)
    if (len(tokens) != 1 or cookies.csrf_binding is None
            or not csrf.verify_token(deps.csrf_keyring, tokens[0], cookies.csrf_binding, origin, now)):
        raise Refusal(403, "csrf_invalid")


def _check_content_type(request: Request) -> None:
    values = request.headers.getlist("content-type")
    if len(values) != 1:
        raise Refusal(415, "invalid_request")
    media = [part.strip().lower() for part in values[0].split(";")]
    if media[0] != "application/json" or media[1:] not in ([], ["charset=utf-8"]):
        raise Refusal(415, "invalid_request")


def _announced_length(request: Request):
    """Framing (anomalies : 400) puis taille annoncée (413) ; None si
    Content-Length est absent."""
    if request.headers.getlist("transfer-encoding"):
        raise Refusal(400, "invalid_request")
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise Refusal(400, "invalid_request")
    if not lengths:
        return None
    if not _CONTENT_LENGTH_RE.fullmatch(lengths[0]):
        raise Refusal(400, "invalid_request")
    digits = lengths[0].lstrip("0")
    if len(digits) > len(str(MAX_BODY_BYTES)) or int(digits or "0") > MAX_BODY_BYTES:
        raise Refusal(413, "request_too_large")
    return int(digits or "0")


async def _read_body(request: Request, announced) -> bytes:
    body = bytearray()
    try:
        async for chunk in request.stream():
            body += chunk
            if announced is not None and len(body) > announced:
                raise Refusal(400, "invalid_request")  # discordance de longueur : framing, prioritaire
            if len(body) > MAX_BODY_BYTES:
                raise Refusal(413, "request_too_large")
    except ClientDisconnect:
        raise Refusal(400, "invalid_request") from None
    if announced is not None and len(body) != announced:
        raise Refusal(400, "invalid_request")
    return bytes(body)


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("clé JSON dupliquée")
        out[key] = value
    return out


def _reject_constant(_):
    raise ValueError("constante JSON refusée")


def _level_request(body: bytes):
    """(level, expected_revision entier) ou Refusal 400."""
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refusal(400, "invalid_request") from None
    if type(data) is not dict or set(data) != LEVEL_KEYS:
        raise Refusal(400, "invalid_request")
    level = data["level"]
    if not valid_level(level):
        raise Refusal(400, "invalid_request")
    revision = data["expected_revision"]
    if type(revision) is not str or not _REVISION_RE.fullmatch(revision) or int(revision) > MAX_LEVEL_REVISION:
        raise Refusal(400, "invalid_request")
    return level, int(revision)


def build_personal_router(deps: PersonalDependencies) -> APIRouter:
    router = APIRouter(prefix=NAMESPACE)

    @router.get("/me", include_in_schema=False)
    async def me(request: Request):
        try:
            try:
                personal_get_origin(request.headers, deps.allowed_origins)
            except ProvenanceRejected:
                raise Refusal(403, "origin_refused") from None
            try:
                cookies = parse_cookie_headers(request.headers.getlist("cookie"))
            except CookieHeaderRejected:
                raise Refusal(400, "invalid_request") from None
            generation_id = _generation_id(request)
            if request.scope.get("query_string"):
                raise Refusal(400, "invalid_request")
            await _refuse_body(request)
            secret = cookies.actives.get(generation_id)
            if secret is None:
                raise Refusal(401, "session_invalid")
            if deps.runner is None:
                raise Refusal(503, "temporarily_unavailable")
            credential = SessionCredential(generation_id, Redacted(secret))
            try:
                outcome = await run_in_threadpool(deps.runner.run, credential, ReadProfile())
            except Exception as exc:  # refus fermé : aucun détail
                logger.warning("authz_f2 http me runner_error type=%s", type(exc).__name__)
                raise Refusal(503, "temporarily_unavailable") from None
        except Refusal as refusal:
            logger.info("authz_f2 http me status=%s code=%s", refusal.http_status, refusal.code)
            return error_response(refusal.http_status, refusal.code)
        if getattr(outcome, "status", None) == SESSION_INVALID:
            logger.info("authz_f2 http me status=401 code=session_invalid")
            return error_response(401, "session_invalid")
        if not _profile_ok(outcome, generation_id):
            logger.info("authz_f2 http me status=503 code=temporarily_unavailable")
            return error_response(503, "temporarily_unavailable")
        logger.info("authz_f2 http me status=200")
        return json_response(200, _profile_body(outcome.value))

    @router.put("/me/level", include_in_schema=False)
    async def me_level(request: Request):
        try:
            try:
                origin = personal_mutation_origin(request.headers, deps.allowed_origins)
            except ProvenanceRejected:
                raise Refusal(403, "origin_refused") from None
            try:
                cookies = parse_cookie_headers(request.headers.getlist("cookie"))
            except CookieHeaderRejected:
                raise Refusal(400, "invalid_request") from None
            _check_csrf(request, deps, origin, cookies)
            generation_id = _generation_id(request)
            if request.scope.get("query_string"):
                raise Refusal(400, "invalid_request")
            _check_content_type(request)
            announced = _announced_length(request)
            level, expected_revision = _level_request(await _read_body(request, announced))
            secret = cookies.actives.get(generation_id)
            if secret is None:
                raise Refusal(401, "session_invalid")
            if deps.runner is None:
                raise Refusal(503, "temporarily_unavailable")
            credential = SessionCredential(generation_id, Redacted(secret))
            command = UpdateProfileLevel(level, expected_revision)
            try:
                outcome = await run_in_threadpool(deps.runner.run, credential, command)
            except Exception as exc:  # refus fermé : aucun détail
                logger.warning("authz_f2 http me/level runner_error type=%s", type(exc).__name__)
                raise Refusal(503, "temporarily_unavailable") from None
        except Refusal as refusal:
            logger.info("authz_f2 http me/level status=%s code=%s", refusal.http_status, refusal.code)
            return error_response(refusal.http_status, refusal.code)
        status = getattr(outcome, "status", None)
        if status == SESSION_INVALID:
            logger.info("authz_f2 http me/level status=401 code=session_invalid")
            return error_response(401, "session_invalid")
        if status == REVISION_CONFLICT:
            logger.info("authz_f2 http me/level status=409 code=level_revision_conflict")
            return error_response(409, "level_revision_conflict")
        if (not _profile_ok(outcome, generation_id) or outcome.value.level != level
                or outcome.value.level_revision not in (expected_revision, expected_revision + 1)):
            logger.info("authz_f2 http me/level status=503 code=temporarily_unavailable")
            return error_response(503, "temporarily_unavailable")
        logger.info("authz_f2 http me/level status=200")
        return json_response(200, _profile_body(outcome.value))

    return router
