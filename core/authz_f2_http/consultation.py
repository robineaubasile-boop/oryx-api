"""Lectures personnelles F2 des analyses et des thèses (R1 PR 8) —
composition isolée seulement, LECTURE seule.

  GET /api/v2/me/analysis-overview      ListAnalysisOverviewPage
  GET /api/v2/me/theses                 ListThesesPage
  GET /api/v2/me/theses/{thesis_id}     ReadThesis

Le propriétaire est exclusivement AuthorizedPrincipal.user_id : aucun
user_id client, aucun droit tiré d'un curseur. Ordre FERMÉ des contrôles
(le premier échec répond ; aucun refus de provenance, de transport,
d'encodage ou de paramètre n'ouvre de transaction) :

  1. méthode et chemin (routeur : 404 / 405) ;
  2. provenance GET personnelle de PR 5             -> 403 origin_refused ;
  3. en-tête Cookie, parseur strict de PR 3         -> 400 invalid_request ;
  4. X-Oryx-Generation-Id unique, UUID canonique    -> 400 invalid_request ;
  5. query brute de 2048 octets au plus             -> 400 invalid_request ;
  6. listes : query stricte, limit, ticker, curseur (consultation_codec) ;
     détail : query vide, PUIS thesis_id du chemin ([1-9][0-9]{0,9},
     2147483647 au plus, chemin brut sans encodage)  -> 400 invalid_request ;
  7. framing et absence de corps (règles de PR 7) : Transfer-Encoding,
     Content-Length dupliqué ou mal formé -> 400 ; Content-Length valide >
     4096 -> 413 ; Content-Length > 0 -> 400 sans lecture ; flux non vide
     -> 400 (413 au-delà de 4096) ; troncature ou déconnexion -> 400 ;
  8. cookie __Host-oryx-active-<generation_id> EXACT -> 401 session_invalid ;
  9. runner disponible                              -> 503 ;
 10. autorisation transactionnelle (runner, plan READ) -> 401 / 404 / 503 ;
 11. lecture SQL métier (une instruction) ;
 12. vue validée (types exacts, génération, ordre, curseur, filtre) ;
 13. sérialisation complète, puis taille <= 131072 octets -> sinon 503 ;
 14. émission : 200 JSON canonique.

Erreurs {"error": code} : invalid_request, session_invalid, origin_refused,
thesis_not_found (étrangère et inexistante : corps identiques),
request_too_large, temporarily_unavailable. Toute réponse : no-store,
no-cache, nosniff, no-referrer, aucun Set-Cookie, aucun CORS. Aucun jeton
CSRF n'est exigé (GET). Les journaux ne portent que route, statut et code
(ou type d'exception) : ni secret, cookie, curseur, ticker, texte, ni SQL.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from core.auth_f2.crypto import Redacted
from core.auth_f2.service import SessionCredential
from core.auth_f2_http.cookies import CookieHeaderRejected, parse_cookie_headers
from core.auth_f2_http.provenance import ProvenanceRejected
from core.auth_f2_http.routes import NO_STORE_HEADERS, error_response
from core.authz_f2.commands import ReadThesis
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import AUTHORIZED, NOT_FOUND, SESSION_INVALID, AuthorizationOutcome
from core.authz_f2_http import consultation_codec as codec
from core.authz_f2_http.portfolio import _refuse_body
from core.authz_f2_http.provenance import personal_get_origin
from core.authz_f2_http.routes import NAMESPACE, PersonalDependencies, Refusal, _generation_id

logger = logging.getLogger("oryx.authz_f2.http")

ANALYSIS_OVERVIEW = "/me/analysis-overview"
THESES = "/me/theses"


def _raw_query(request: Request) -> bytes:
    """Étape 5 : query brute AVANT tout décodage."""
    raw = request.scope.get("query_string", b"")
    if type(raw) is not bytes or len(raw) > codec.MAX_QUERY_BYTES:
        raise Refusal(400, "invalid_request")
    return raw


def _overview_command(request: Request, raw: bytes):
    try:
        return codec.overview_command(raw)
    except codec.InvalidRequest:
        raise Refusal(400, "invalid_request") from None


def _theses_command(request: Request, raw: bytes):
    try:
        return codec.theses_command(raw)
    except codec.InvalidRequest:
        raise Refusal(400, "invalid_request") from None


def _thesis_command(request: Request, raw: bytes, thesis_id: str):
    """Query refusée AVANT le contrôle de l'identifiant ; chemin reçu
    exactement égal au chemin brut (aucun encodage %)."""
    if raw:
        raise Refusal(400, "invalid_request")
    raw_path = request.scope.get("raw_path")
    if raw_path is not None and raw_path != request.scope.get("path", "").encode("ascii", "replace"):
        raise Refusal(400, "invalid_request")
    try:
        return ReadThesis(codec.parse_thesis_id(thesis_id))
    except (codec.InvalidRequest, ValueError):
        raise Refusal(400, "invalid_request") from None


def _authorized(outcome, generation_id) -> bool:
    return (isinstance(outcome, AuthorizationOutcome) and outcome.status == AUTHORIZED
            and isinstance(outcome.principal, AuthorizedPrincipal)
            and outcome.principal.generation_id == generation_id)


def _response(body: bytes) -> Response:
    return Response(content=body, status_code=200, media_type="application/json", headers=NO_STORE_HEADERS)


def build_consultation_router(deps: PersonalDependencies) -> APIRouter:
    router = APIRouter(prefix=NAMESPACE)

    async def run(label, request: Request, parse):
        """Contrôles 2 à 10 ; (outcome, generation_id, command) ou Response de refus."""
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
            command = parse(request, _raw_query(request))
            await _refuse_body(request)
            secret = cookies.actives.get(generation_id)
            if secret is None:
                raise Refusal(401, "session_invalid")
            if deps.runner is None:
                raise Refusal(503, "temporarily_unavailable")
            credential = SessionCredential(generation_id, Redacted(secret))
            try:
                outcome = await run_in_threadpool(deps.runner.run, credential, command)
            except Exception as exc:  # refus fermé : aucun détail
                logger.warning("authz_f2 http %s runner_error type=%s", label, type(exc).__name__)
                raise Refusal(503, "temporarily_unavailable") from None
        except Refusal as refusal:
            logger.info("authz_f2 http %s status=%s code=%s", label, refusal.http_status, refusal.code)
            return error_response(refusal.http_status, refusal.code)
        return outcome, generation_id, command

    def respond(label, ran, render, *, not_found=None):
        """Contrôles 10 à 14 : résultat fermé du runner, vue validée,
        sérialisation bornée ; tout écart : 503, jamais de correction."""
        if isinstance(ran, Response):
            return ran
        outcome, generation_id, command = ran
        status = getattr(outcome, "status", None)
        if status == SESSION_INVALID:
            logger.info("authz_f2 http %s status=401 code=session_invalid", label)
            return error_response(401, "session_invalid")
        if status == NOT_FOUND and not_found is not None:
            logger.info("authz_f2 http %s status=404 code=%s", label, not_found)
            return error_response(404, not_found)
        body = None
        if _authorized(outcome, generation_id):
            try:
                body = render(outcome.value, command)
            except (AttributeError, TypeError, ValueError):  # codec.Unrestorable compris
                body = None
        if body is None:
            logger.info("authz_f2 http %s status=503 code=temporarily_unavailable", label)
            return error_response(503, "temporarily_unavailable")
        logger.info("authz_f2 http %s status=200", label)
        return _response(body)

    @router.get(ANALYSIS_OVERVIEW, include_in_schema=False)
    async def analysis_overview(request: Request):
        label = "analysis_overview"
        return respond(label, await run(label, request, _overview_command), codec.overview_body)

    @router.get(THESES, include_in_schema=False)
    async def list_theses(request: Request):
        label = "theses.list"
        return respond(label, await run(label, request, _theses_command), codec.theses_body)

    @router.get(THESES + "/{thesis_id}", include_in_schema=False)
    async def read_thesis(request: Request, thesis_id: str):
        label = "theses.read"
        ran = await run(label, request, lambda req, raw: _thesis_command(req, raw, thesis_id))
        return respond(label, ran, lambda view, command: codec.thesis_body(view, command.thesis_id),
                       not_found="thesis_not_found")

    return router
