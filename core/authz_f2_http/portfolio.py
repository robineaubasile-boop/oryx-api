"""Portefeuille personnel F2 versionné et idempotent (R1 PR 7) — composition
isolée seulement.

  GET    /api/v2/me/portfolio                 ListPositions
  GET    /api/v2/me/portfolio/{position_id}   ReadPosition
  POST   /api/v2/me/portfolio                 CreatePortfolioPosition   201
  PUT    /api/v2/me/portfolio/{position_id}   ReplacePortfolioPosition  200
  DELETE /api/v2/me/portfolio/{position_id}   DeletePortfolioPosition   200

Le propriétaire est exclusivement AuthorizedPrincipal.user_id : aucun
user_id client n'est lu. Ordre des contrôles (le premier échec répond ;
aucun refus de transport n'ouvre de transaction) :

  1. méthode et chemin (routeur : 404 / 405) ;
  2. provenance : GET -> politique personnelle PR 5 ; mutations -> Host
     canonique autorisé, Origin obligatoire et exact, Sec-Fetch-Site
     same-origin s'il est présent, Referer de même origine
                                                    -> 403 origin_refused ;
  3. en-tête Cookie, parseur strict de PR 3         -> 400 invalid_request ;
  4. CSRF oryx-f2-csrf-v1 (mutations)               -> 503 / 403 csrf_invalid ;
  5. X-Oryx-Generation-Id unique, UUID canonique    -> 400 invalid_request ;
  6. aucune query                                   -> 400 invalid_request ;
  7. position_id du chemin : [1-9][0-9]{0,9}, <= 2147483647, chemin brut
     sans encodage                                  -> 400 invalid_request ;
  8. X-Oryx-Operation-Id unique, UUID canonique minuscule (mutations)
                                                    -> 400 invalid_request ;
  9. X-Oryx-Expected-Revision unique, "0" | [1-9][0-9]{0,18},
     <= 2^63 - 1 (PUT, DELETE)                      -> 400 invalid_request ;
 10. Content-Type application/json [; charset=utf-8] (POST, PUT) -> 415 ;
 11. framing : Transfer-Encoding visible, Content-Length dupliqué ou mal
     formé                                          -> 400 invalid_request ;
 12. taille et corps : Content-Length valide > 4096 -> 413 ; flux > 4096
     sans longueur annoncée -> 413 ; discordance, troncature, déconnexion
     -> 400 ; GET / DELETE : tout corps non vide (Content-Length > 0 sans
     lecture, ou flux non vide) -> 400 ;
 13. JSON exact (POST, PUT) : objet de cinq clés, sans doublon, sans
     constante ni nombre, puis ticker, quantity, purchase_price,
     target_percent, envelope (chaînes ou null)     -> 400 invalid_request ;
 14. cookie __Host-oryx-active-<generation_id> EXACT -> 401 session_invalid ;
 15. runner disponible                              -> 503 ;
 16. autorisation transactionnelle (runner)          -> 401 / 404 / 409 / 503 ;
 17. résultat vérifié avant sérialisation           -> sinon 503.

Réponses : GET collection {"positions": [vue...]} (position_id croissant),
GET individuel : la vue ; mutations : corps JSON canonique du résultat
portfolio-write-result-v1, identique octet pour octet au rejeu ; POST 201
avec Location: /api/v2/me/portfolio/{position_id}. Erreurs
{"error": code} : invalid_request, session_invalid, origin_refused,
csrf_invalid, position_not_found, idempotency_conflict,
position_revision_conflict, request_too_large, temporarily_unavailable.
Toute réponse : no-store, nosniff, no-referrer, aucun Set-Cookie, aucun
CORS. Les journaux ne portent que route, statut et code : ni secret,
cookie, HMAC, empreinte, UUID d'opération, valeur financière, corps ni SQL.
"""
import json
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from core.auth_f2.crypto import Redacted
from core.auth_f2.service import SessionCredential
from core.auth_f2_http.cookies import CookieHeaderRejected, canonical_uuid, parse_cookie_headers
from core.auth_f2_http.provenance import ProvenanceRejected
from core.auth_f2_http.routes import NO_STORE_HEADERS, error_response
from core.authz_f2 import portfolio_values as pv
from core.authz_f2.commands import (
    MAX_POSITION_ID,
    MAX_POSITION_REVISION,
    REVISION_TEXT_RE,
    CreatePortfolioPosition,
    DeletePortfolioPosition,
    ListPositions,
    PositionView,
    ReadPosition,
    ReplacePortfolioPosition,
)
from core.authz_f2.portfolio_fingerprint import CREATE, DELETE, UPDATE
from core.authz_f2.portfolio_results import canonical_json, location, same_result, view_payload
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import (
    AUTHORIZED,
    IDEMPOTENCY_CONFLICT,
    NOT_FOUND,
    REVISION_CONFLICT,
    SESSION_INVALID,
    AuthorizationOutcome,
)
from core.authz_f2_http.provenance import personal_get_origin, personal_mutation_origin
from core.authz_f2_http.routes import (
    NAMESPACE,
    PersonalDependencies,
    Refusal,
    _announced_length,
    _check_content_type,
    _check_csrf,
    _generation_id,
    _read_body,
    _reject_constant,
    _reject_duplicates,
)

logger = logging.getLogger("oryx.authz_f2.http")

PORTFOLIO = "/me/portfolio"
OPERATION_HEADER = "x-oryx-operation-id"
REVISION_HEADER = "x-oryx-expected-revision"
BODY_KEYS = ("ticker", "quantity", "purchase_price", "target_percent", "envelope")
_POSITION_ID_RE = re.compile(r"[1-9][0-9]{0,9}", re.ASCII)


def _canonical_response(http_status: int, body: bytes, extra=None) -> Response:
    headers = dict(NO_STORE_HEADERS)
    headers.update(extra or {})
    return Response(content=body, status_code=http_status, media_type="application/json", headers=headers)


def _position_id(request: Request, raw: str) -> int:
    # Le chemin reçu doit être exactement le chemin brut (aucun encodage %).
    raw_path = request.scope.get("raw_path")
    if raw_path is not None and raw_path != request.scope.get("path", "").encode("ascii", "replace"):
        raise Refusal(400, "invalid_request")
    if not _POSITION_ID_RE.fullmatch(raw) or int(raw) > MAX_POSITION_ID:
        raise Refusal(400, "invalid_request")
    return int(raw)


def _operation_id(request: Request):
    values = request.headers.getlist(OPERATION_HEADER)
    operation_id = canonical_uuid(values[0]) if len(values) == 1 else None
    if operation_id is None:
        raise Refusal(400, "invalid_request")
    return operation_id


def _expected_revision(request: Request) -> int:
    values = request.headers.getlist(REVISION_HEADER)
    if len(values) != 1 or not REVISION_TEXT_RE.fullmatch(values[0]) or int(values[0]) > MAX_POSITION_REVISION:
        raise Refusal(400, "invalid_request")
    return int(values[0])


async def _refuse_body(request: Request) -> None:
    """GET / DELETE : aucun corps. Framing (400) et taille annoncée (413)
    comme PUT ; Content-Length valide > 0 : 400 sans lecture ; sinon flux
    lu sous borne (non vide : 400, > 4096 : 413). Content-Type ignoré."""
    announced = _announced_length(request)
    if announced:
        raise Refusal(400, "invalid_request")
    if await _read_body(request, announced):
        raise Refusal(400, "invalid_request")


def _reject_number(_):
    raise ValueError("nombre JSON refusé")


def _position_request(body: bytes) -> tuple:
    """Cinq valeurs métier validées, dans l'ordre fermé, ou Refusal 400."""
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant,
                          parse_int=_reject_number, parse_float=_reject_number)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refusal(400, "invalid_request") from None
    if type(data) is not dict or set(data) != set(BODY_KEYS):
        raise Refusal(400, "invalid_request")
    values = []
    for key, check in zip(BODY_KEYS, pv.CHECKS):
        try:
            values.append(check(data[key]))
        except pv.ValueRejected:
            raise Refusal(400, "invalid_request") from None
    return tuple(values)


def _authorized(outcome, generation_id) -> bool:
    return (isinstance(outcome, AuthorizationOutcome) and outcome.status == AUTHORIZED
            and isinstance(outcome.principal, AuthorizedPrincipal)
            and outcome.principal.generation_id == generation_id)


def build_portfolio_router(deps: PersonalDependencies) -> APIRouter:
    router = APIRouter(prefix=NAMESPACE)

    async def run(label, request: Request, *, mutation: bool, position=None, operation=False, revision=False,
                  body=False, build=None):
        """Contrôles 2 à 15 puis runner ; (outcome, generation_id, operation_id)
        ou Response de refus."""
        try:
            try:
                if mutation:
                    origin = personal_mutation_origin(request.headers, deps.allowed_origins)
                else:
                    personal_get_origin(request.headers, deps.allowed_origins)
            except ProvenanceRejected:
                raise Refusal(403, "origin_refused") from None
            try:
                cookies = parse_cookie_headers(request.headers.getlist("cookie"))
            except CookieHeaderRejected:
                raise Refusal(400, "invalid_request") from None
            if mutation:
                _check_csrf(request, deps, origin, cookies)
            generation_id = _generation_id(request)
            if request.scope.get("query_string"):
                raise Refusal(400, "invalid_request")
            position_id = _position_id(request, position) if position is not None else None
            operation_id = _operation_id(request) if operation else None
            expected = _expected_revision(request) if revision else None
            if body:
                _check_content_type(request)
                values = _position_request(await _read_body(request, _announced_length(request)))
            else:
                await _refuse_body(request)
                values = None
            secret = cookies.actives.get(generation_id)
            if secret is None:
                raise Refusal(401, "session_invalid")
            if deps.runner is None:
                raise Refusal(503, "temporarily_unavailable")
            try:
                command = build(position_id, operation_id, expected, values)
            except (TypeError, ValueError):  # valeurs déjà validées : incohérence interne
                raise Refusal(503, "temporarily_unavailable") from None
            credential = SessionCredential(generation_id, Redacted(secret))
            try:
                outcome = await run_in_threadpool(deps.runner.run, credential, command)
            except Exception as exc:  # refus fermé : aucun détail
                logger.warning("authz_f2 http %s runner_error type=%s", label, type(exc).__name__)
                raise Refusal(503, "temporarily_unavailable") from None
        except Refusal as refusal:
            logger.info("authz_f2 http %s status=%s code=%s", label, refusal.http_status, refusal.code)
            return error_response(refusal.http_status, refusal.code)
        return outcome, generation_id, operation_id

    def refusal(label, outcome):
        """Résultat fermé non autorisé -> réponse, ou None."""
        status = getattr(outcome, "status", None)
        mapping = {SESSION_INVALID: (401, "session_invalid"), NOT_FOUND: (404, "position_not_found"),
                   REVISION_CONFLICT: (409, "position_revision_conflict"),
                   IDEMPOTENCY_CONFLICT: (409, "idempotency_conflict")}
        if status in mapping:
            http_status, code = mapping[status]
            logger.info("authz_f2 http %s status=%s code=%s", label, http_status, code)
            return error_response(http_status, code)
        return None

    def unavailable(label):
        logger.info("authz_f2 http %s status=503 code=temporarily_unavailable", label)
        return error_response(503, "temporarily_unavailable")

    @router.get(PORTFOLIO, include_in_schema=False)
    async def list_positions(request: Request):
        label = "portfolio.list"
        ran = await run(label, request, mutation=False, build=lambda *_: ListPositions())
        if isinstance(ran, Response):
            return ran
        outcome, generation_id, _ = ran
        if getattr(outcome, "status", None) == SESSION_INVALID:
            return refusal(label, outcome)
        views = getattr(outcome, "value", None)
        if (not _authorized(outcome, generation_id) or type(views) is not tuple
                or any(type(view) is not PositionView for view in views)
                or [view.position_id for view in views] != sorted({view.position_id for view in views})):
            return unavailable(label)
        logger.info("authz_f2 http %s status=200", label)
        return _canonical_response(200, canonical_json({"positions": [view_payload(view) for view in views]}))

    @router.get(PORTFOLIO + "/{position_id}", include_in_schema=False)
    async def read_position(request: Request, position_id: str):
        label = "portfolio.read"
        ran = await run(label, request, mutation=False, position=position_id,
                        build=lambda pid, *_: ReadPosition(pid))
        if isinstance(ran, Response):
            return ran
        outcome, generation_id, _ = ran
        if getattr(outcome, "status", None) in (SESSION_INVALID, NOT_FOUND):
            return refusal(label, outcome)
        view = getattr(outcome, "value", None)
        if not _authorized(outcome, generation_id) or type(view) is not PositionView \
                or str(view.position_id) != position_id:
            return unavailable(label)
        logger.info("authz_f2 http %s status=200", label)
        return _canonical_response(200, canonical_json(view_payload(view)))

    async def mutate(label, request: Request, operation_type: str, **kwargs):
        ran = await run(label, request, mutation=True, operation=True, **kwargs)
        if isinstance(ran, Response):
            return ran
        outcome, generation_id, operation_id = ran
        refused = refusal(label, outcome)
        if refused is not None:
            return refused
        result = getattr(outcome, "value", None)
        if not _authorized(outcome, generation_id) or not same_result(result, operation_type, operation_id):
            return unavailable(label)
        position = kwargs.get("position")
        if position is not None and str(result.position_id) != position:
            return unavailable(label)
        where = location(result)
        logger.info("authz_f2 http %s status=%s", label, result.status)
        return _canonical_response(result.status, result.body, {"location": where} if where else None)

    @router.post(PORTFOLIO, include_in_schema=False)
    async def create_position(request: Request):
        return await mutate("portfolio.create", request, CREATE, body=True,
                            build=lambda _, op, __, values: CreatePortfolioPosition(op, *values))

    @router.put(PORTFOLIO + "/{position_id}", include_in_schema=False)
    async def replace_position(request: Request, position_id: str):
        return await mutate("portfolio.replace", request, UPDATE, position=position_id, revision=True, body=True,
                            build=lambda pid, op, rev, values: ReplacePortfolioPosition(op, pid, rev, *values))

    @router.delete(PORTFOLIO + "/{position_id}", include_in_schema=False)
    async def delete_position(request: Request, position_id: str):
        return await mutate("portfolio.delete", request, DELETE, position=position_id, revision=True,
                            build=lambda pid, op, rev, _: DeletePortfolioPosition(op, pid, rev))

    return router
