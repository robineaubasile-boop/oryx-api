"""Routes HTTP F2 /api/auth/f2/* (R1 PR 3) — composition isolée seulement.

  GET  /csrf/bootstrap   jeton CSRF (aucune opération, aucune session)
  POST /sign-in/start    {"email"}
  POST /sign-in/resend   {"operation_id", "candidate_id"}
  POST /sign-in/verify   {"operation_id", "candidate_id", "code"}
  POST /sign-in/resume   {"operation_id", "candidate_id"}
  POST /session/resolve  {"generation_id"}
  POST /logout           {"generation_id"}

Ordre des contrôles de chaque mutation, TOUS avant le moteur F2 :
provenance (provenance.py) -> en-tête Cookie (cookies.py) -> CSRF
(csrf.py) -> Content-Type et corps JSON (4 Ko, clés exactes, types,
UUID canoniques) -> cookie désigné par l'identifiant exact du corps ->
IP client (client_ip.py, seulement pour start / resend / verify). Le code
OTP n'est contrôlé que comme chaîne JSON : sa forme est jugée par le
moteur, qui compte les échecs (PR 2, code mal formé).

Réponses : JSON, Cache-Control: no-store, aucune donnée secrète ; les
erreurs ne portent qu'un code. Toute exception du moteur donne 503
temporarily_unavailable, sans cookie. Les durées des cookies sont bornées
sans dépendre de l'horloge de l'application quand c'est possible
(horloge monotone depuis l'entrée de la requête) :

- candidat : 15 min - durée écoulée depuis l'entrée de la requête (le
  candidat est créé après cette entrée) ;
- actif (authenticated) : 15 j - durée écoulée (la session est créée
  après cette entrée, absolute_expires_at = création + 15 j) ;
- actif (resumed) : 15 j - 5 min - durée écoulée (la reprise n'est admise
  que moins de 5 min après le commit) ;
- et, dans tous les cas, au plus absolute_expires_at - heure murale.
"""
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from core.auth_f2.crypto import Redacted
from core.auth_f2.policy import RESULT_RESUME_WINDOW, SESSION_ABSOLUTE_TTL
from core.auth_f2.service import CandidateCredential, SessionCredential, SessionPrincipal
from core.auth_f2_http import contract, csrf
from core.auth_f2_http.client_ip import ClientIpUnavailable
from core.auth_f2_http.cookies import (
    CANDIDATE_COOKIE_TTL,
    CSRF_BINDING,
    CSRF_BINDING_TTL,
    MAX_ORYX_COOKIES,
    CookieHeaderRejected,
    active_name,
    candidate_name,
    canonical_uuid,
    clear_cookie,
    parse_cookie_headers,
    set_cookie,
)
from core.auth_f2_http.provenance import ProvenanceRejected, request_origin

logger = logging.getLogger("oryx.auth_f2.http")

NAMESPACE = "/api/auth/f2"
CSRF_HEADER = "x-oryx-csrf"
MAX_JSON_BYTES = 4096
EMAIL_MAX_CHARS = 320
NO_STORE_HEADERS = {
    "cache-control": "no-store",
    "pragma": "no-cache",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


class Refusal(Exception):
    def __init__(self, http_status: int, code: str):
        super().__init__(code)
        self.http_status = http_status
        self.code = code


@dataclass(frozen=True)
class Dependencies:
    service: object
    csrf_keyring: csrf.CsrfKeyring
    ip_resolver: object
    allowed_origins: frozenset
    wall_clock: object
    monotonic: object


def json_response(http_status: int, body: dict, cookies=()) -> Response:
    response = Response(content=json.dumps(body, separators=(",", ":"), sort_keys=True).encode("ascii"),
                        status_code=http_status, media_type="application/json", headers=NO_STORE_HEADERS)
    for cookie in cookies:
        response.headers.append("set-cookie", cookie)
    return response


def error_response(http_status: int, code: str, cookies=()) -> Response:
    return json_response(http_status, {"error": code}, cookies)


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("clé JSON dupliquée")
        out[key] = value
    return out


def _reject_constant(_):
    raise ValueError("constante JSON refusée")


async def read_json(request: Request, keys: frozenset) -> dict:
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1:
        raise Refusal(415, "invalid_request")
    media = [p.strip().lower() for p in content_types[0].split(";")]
    if media[0] != "application/json" or any(p not in ("charset=utf-8",) for p in media[1:]):
        raise Refusal(415, "invalid_request")
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or int(lengths[0]) > MAX_JSON_BYTES)):
        raise Refusal(413, "request_too_large")
    body = bytearray()
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_JSON_BYTES:
                raise Refusal(413, "request_too_large")
    except ClientDisconnect:
        raise Refusal(400, "invalid_request") from None
    try:
        data = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=_reject_duplicates,
                          parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refusal(400, "invalid_request") from None
    if not isinstance(data, dict) or set(data) != keys:
        raise Refusal(400, "invalid_request")
    return data


def _uuid_field(data: dict, key: str):
    value = canonical_uuid(data[key])
    if value is None:
        raise Refusal(400, "invalid_request")
    return value


def _string_field(data: dict, key: str, max_chars=None) -> str:
    value = data[key]
    if not isinstance(value, str) or (max_chars is not None and len(value) > max_chars):
        raise Refusal(400, "invalid_request")
    return value


def build_router(deps: Dependencies) -> APIRouter:
    router = APIRouter(prefix=NAMESPACE)

    def now_seconds() -> int:
        return math.floor(deps.wall_clock())

    def guard(request: Request):
        """Provenance, cookies, CSRF : (origine, cookies) ou Refusal."""
        try:
            origin = request_origin(request.headers, deps.allowed_origins)
        except ProvenanceRejected:
            raise Refusal(403, "origin_refused") from None
        try:
            cookies = parse_cookie_headers(request.headers.getlist("cookie"))
        except CookieHeaderRejected:
            raise Refusal(400, "invalid_request") from None
        return origin, cookies

    def check_csrf(request: Request, origin: str, cookies) -> None:
        tokens = request.headers.getlist(CSRF_HEADER)
        if (len(tokens) != 1 or cookies.csrf_binding is None
                or not csrf.verify_token(deps.csrf_keyring, tokens[0], cookies.csrf_binding, origin, now_seconds())):
            raise Refusal(403, "csrf_invalid")

    def client_ip(request: Request) -> str:
        try:
            ip = deps.ip_resolver(request)
        except ClientIpUnavailable:
            raise Refusal(503, "temporarily_unavailable") from None
        if not isinstance(ip, str):
            raise Refusal(503, "temporarily_unavailable")
        return ip

    async def mutation(request: Request, keys: frozenset):
        origin, cookies = guard(request)
        check_csrf(request, origin, cookies)
        return cookies, await read_json(request, keys)

    def candidate_from(cookies, data):
        operation_id = _uuid_field(data, "operation_id")
        candidate_id = _uuid_field(data, "candidate_id")
        secret = cookies.candidates.get(candidate_id)
        if secret is None:
            raise Refusal(401, "credential_missing")
        return operation_id, CandidateCredential(candidate_id, Redacted(secret))

    def session_from(cookies, data):
        generation_id = _uuid_field(data, "generation_id")
        secret = cookies.actives.get(generation_id)
        if secret is None:
            raise Refusal(401, "session_invalid")
        return SessionCredential(generation_id, Redacted(secret))

    async def call(fn, *args, **kwargs):
        try:
            return await run_in_threadpool(fn, *args, **kwargs)
        except Exception as exc:  # refus fermé : aucun détail, aucun secret
            logger.warning("f2 http engine_error type=%s", type(exc).__name__)
            raise Refusal(503, "temporarily_unavailable") from None

    def active_max_age(session, started: float) -> float:
        elapsed = deps.monotonic() - started
        bound = SESSION_ABSOLUTE_TTL.total_seconds() - elapsed
        if session.resumed:
            bound -= RESULT_RESUME_WINDOW.total_seconds()
        wall = (session.absolute_expires_at - datetime.fromtimestamp(deps.wall_clock(), timezone.utc)).total_seconds()
        return min(bound, wall)

    def render(endpoint: str, decision: contract.Decision, started: float, *, candidate_id=None) -> Response:
        rule = decision.rule
        if rule.kind == contract.ERROR:
            logger.info("f2 http %s status=%s code=%s", endpoint, rule.http_status, rule.code)
            return error_response(rule.http_status, rule.code)
        if rule.kind == contract.NEW_CANDIDATE:
            cand = decision.candidate
            max_age = CANDIDATE_COOKIE_TTL.total_seconds() - (deps.monotonic() - started)
            if max_age < 1:
                return render(endpoint, contract.Decision(contract.UNAVAILABLE), started)
            cookies = [set_cookie(candidate_name(cand.candidate_id), cand.secret.reveal(), max_age)]
            body = {"status": rule.code, "operation_id": str(decision.operation_id),
                    "candidate_id": str(cand.candidate_id)}
        elif rule.kind == contract.PROGRESS:
            cookies = []
            body = {"status": rule.code, "operation_id": str(decision.operation_id), "candidate_id": str(candidate_id)}
        else:
            session = decision.session
            max_age = math.floor(active_max_age(session, started))
            if max_age < 1:
                return render(endpoint, contract.Decision(contract.UNAVAILABLE), started)
            cookies = [set_cookie(active_name(session.generation_id), session.active_secret.reveal(), max_age),
                       clear_cookie(candidate_name(candidate_id))]
            body = {"status": rule.code, "generation_id": str(session.generation_id), "expires_in": max_age}
        logger.info("f2 http %s status=%s outcome=%s", endpoint, rule.http_status, rule.code)
        return json_response(rule.http_status, body, cookies)

    def refused(endpoint: str, refusal: Refusal, cookies=()) -> Response:
        logger.info("f2 http %s status=%s code=%s", endpoint, refusal.http_status, refusal.code)
        return error_response(refusal.http_status, refusal.code, cookies)

    @router.get("/csrf/bootstrap", include_in_schema=False)
    async def csrf_bootstrap(request: Request):
        try:
            origin, cookies = guard(request)
            binding = cookies.csrf_binding
            if binding is None:
                if cookies.count >= MAX_ORYX_COOKIES:
                    raise Refusal(409, "credential_limit")
                binding = csrf.new_binding()
            token = csrf.issue_token(deps.csrf_keyring, binding, origin, now_seconds())
        except Refusal as refusal:
            return refused("csrf/bootstrap", refusal)
        return json_response(200, {"csrf_token": token, "expires_in": csrf.TOKEN_TTL_SECONDS},
                             [set_cookie(CSRF_BINDING, binding, CSRF_BINDING_TTL.total_seconds())])

    @router.post("/sign-in/start", include_in_schema=False)
    async def sign_in_start(request: Request):
        started = deps.monotonic()
        try:
            cookies, data = await mutation(request, frozenset({"email"}))
            email = _string_field(data, "email", EMAIL_MAX_CHARS)
            if cookies.count >= MAX_ORYX_COOKIES:
                raise Refusal(409, "credential_limit")
            outcome = await call(deps.service.start_sign_in, email=email, client_ip=client_ip(request))
        except Refusal as refusal:
            return refused(contract.START, refusal)
        return render(contract.START, contract.translate(contract.START, outcome), started)

    @router.post("/sign-in/resend", include_in_schema=False)
    async def sign_in_resend(request: Request):
        started = deps.monotonic()
        try:
            cookies, data = await mutation(request, frozenset({"operation_id", "candidate_id"}))
            operation_id, candidate = candidate_from(cookies, data)
            outcome = await call(deps.service.resend_sign_in_otp, operation_id=operation_id, candidate=candidate,
                                 client_ip=client_ip(request))
        except Refusal as refusal:
            return refused(contract.RESEND, refusal)
        decision = contract.translate(contract.RESEND, outcome, operation_id=operation_id)
        return render(contract.RESEND, decision, started, candidate_id=candidate.candidate_id)

    @router.post("/sign-in/verify", include_in_schema=False)
    async def sign_in_verify(request: Request):
        started = deps.monotonic()
        try:
            cookies, data = await mutation(request, frozenset({"operation_id", "candidate_id", "code"}))
            operation_id, candidate = candidate_from(cookies, data)
            code = _string_field(data, "code")
            outcome = await call(deps.service.verify_sign_in, operation_id=operation_id, candidate=candidate,
                                 code=code, client_ip=client_ip(request))
        except Refusal as refusal:
            return refused(contract.VERIFY, refusal)
        decision = contract.translate(contract.VERIFY, outcome, operation_id=operation_id)
        return render(contract.VERIFY, decision, started, candidate_id=candidate.candidate_id)

    @router.post("/sign-in/resume", include_in_schema=False)
    async def sign_in_resume(request: Request):
        started = deps.monotonic()
        try:
            cookies, data = await mutation(request, frozenset({"operation_id", "candidate_id"}))
            operation_id, candidate = candidate_from(cookies, data)
            outcome = await call(deps.service.resume_result, operation_id=operation_id, candidate=candidate)
        except Refusal as refusal:
            return refused(contract.RESUME, refusal)
        decision = contract.translate(contract.RESUME, outcome, operation_id=operation_id)
        return render(contract.RESUME, decision, started, candidate_id=candidate.candidate_id)

    async def authenticated(request: Request, endpoint: str):
        """(cookies, justificatif, principal ou None) ; justificatif désigné
        par son generation_id exact, jugé par authenticate_session."""
        cookies, data = await mutation(request, frozenset({"generation_id"}))
        credential = session_from(cookies, data)
        principal = await call(deps.service.authenticate_session, credential)
        if principal is not None and (not isinstance(principal, SessionPrincipal)
                                      or principal.generation_id != credential.generation_id):
            raise Refusal(503, "temporarily_unavailable")
        return credential, principal

    @router.post("/session/resolve", include_in_schema=False)
    async def session_resolve(request: Request):
        try:
            credential, principal = await authenticated(request, "session/resolve")
        except Refusal as refusal:
            return refused("session/resolve", refusal)
        if principal is None:
            # Ce cookie précis ne vaut plus rien : effacé, et lui seul.
            return refused("session/resolve", Refusal(401, "session_invalid"),
                           [clear_cookie(active_name(credential.generation_id))])
        return json_response(200, {"status": "active", "generation_id": str(principal.generation_id)})

    @router.post("/logout", include_in_schema=False)
    async def logout(request: Request):
        try:
            credential, principal = await authenticated(request, "logout")
            if principal is not None:
                # Révocation de CETTE session seulement (jamais celle d'un autre
                # appareil ni d'un autre cookie du navigateur).
                await call(deps.service.revoke_session, principal.session_id, "logout")
        except Refusal as refusal:
            return refused("logout", refusal)
        cleared = [clear_cookie(active_name(credential.generation_id))]
        if principal is None:
            return refused("logout", Refusal(401, "session_invalid"), cleared)
        logger.info("f2 http logout status=200")
        return json_response(200, {"status": "logged_out"}, cleared)

    return router
