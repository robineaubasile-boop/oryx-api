"""GET /api/v2/me (R1 PR 5, C.5.2 §7) — composition isolée seulement.

Ordre des contrôles, tous avant l'autorisation :

  1. méthode et chemin (routeur : 404 / 405) ;
  2. provenance (provenance.py)                         -> 403 origin_refused ;
  3. en-tête Cookie, parseur strict de PR 3              -> 400 invalid_request ;
  4. aucune query                                        -> 400 invalid_request ;
  5. aucun corps : Content-Length > 4096 ou flux > 4096  -> 413 request_too_large ;
     tout autre corps, Content-Length dupliqué ou illisible -> 400 invalid_request ;
  6. X-Oryx-Generation-Id : un seul, UUID canonique      -> 400 invalid_request ;
  7. cookie __Host-oryx-active-<generation_id> EXACT (jamais choisi
     implicitement parmi d'autres)                       -> 401 session_invalid ;
  8. AuthorizedTransactionRunner + ReadProfile           -> 200, 401
     session_invalid, 503 temporarily_unavailable.

Réponse 200 : {"level": ..., "user_id": ...}, valeurs lues dans
PostgreSQL (user_id du principal serveur). Aucun level_revision, aucun
user_id client, aucune création de profil. Toute réponse porte
Cache-Control: no-store, Pragma: no-cache, X-Content-Type-Options: nosniff,
Referrer-Policy: no-referrer et AUCUN Set-Cookie : le nettoyage ciblé des
cookies reste propre à /api/auth/f2/session/resolve et /logout (PR 3).
Chaque requête réauthentifie le justificatif dans ses propres transactions
: un succès antérieur de /session/resolve ne confère aucun droit
réutilisable.
"""
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from core.auth_f2.crypto import Redacted
from core.auth_f2.service import SessionCredential
from core.auth_f2_http.cookies import CookieHeaderRejected, canonical_uuid, parse_cookie_headers
from core.auth_f2_http.provenance import ProvenanceRejected
from core.auth_f2_http.routes import error_response, json_response
from core.authz_f2.commands import ProfileView, ReadProfile
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import AUTHORIZED, SESSION_INVALID
from core.authz_f2_http.provenance import personal_get_origin

logger = logging.getLogger("oryx.authz_f2.http")

NAMESPACE = "/api/v2"
GENERATION_HEADER = "x-oryx-generation-id"
MAX_BODY_BYTES = 4096


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
    return (outcome.status == AUTHORIZED and isinstance(outcome.value, ProfileView)
            and isinstance(outcome.principal, AuthorizedPrincipal)
            and outcome.principal.generation_id == generation_id
            and outcome.value.user_id == outcome.principal.user_id and isinstance(outcome.value.level, str))


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
            if request.scope.get("query_string"):
                raise Refusal(400, "invalid_request")
            await _refuse_body(request)
            generation_id = _generation_id(request)
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
        return json_response(200, {"user_id": outcome.value.user_id, "level": outcome.value.level})

    return router
