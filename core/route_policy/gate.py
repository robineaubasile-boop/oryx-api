"""Décision de routage pure et porte ASGI fermée F-02A V0.2 — DORMANTE.

Interface prévue pour F-04 (aucun appelant en production) :

    gate = RoutePolicyGate(f2_auth_app=<application F2 d'authentification>,
                           f2_personal_app=<application personnelle F2>)

Les deux arguments sont obligatoires et nommés ; None signifie « non
composé » (les routes F2 concernées répondent alors 404). Chaque
application injectée doit être INSPECTABLE (attribut routes) et n'exposer
qu'un sous-ensemble des routes F2 de SA catégorie, sans documentation
OpenAPI et sans CORSMiddleware : l'application historique api:app, ou toute
application exposant une route retirée, désactivée, candidate ou de
documentation, est refusée à la construction (RouteGateConfigError).

Décision (decide / classify), dans cet ordre, sans aucun décodage :

1. root_path non vide, raw_path absent ou non canonique (canonical.py),
   path incohérent avec raw_path            -> 404 {"detail":"Not Found"}
2. aucun gabarit du registre                -> 404
3. gabarit LEGACY_RETIRED, toute méthode (y compris HEAD, OPTIONS ou une
   méthode inattendue)                      -> 410 {"error":"legacy_personal_api_retired"}
4. gabarit DISABLED, PUBLIC_PENDING_*, FRAMEWORK_DOCS, toute méthode -> 404
5. méthode absente des méthodes déclarées pour ce gabarit
                                            -> 405 {"detail":"Method Not Allowed"}, Allow
6. GET /health                              -> 200 {"status":"ok"} (servi ici)
7. route F2 exacte                          -> transmise telle quelle à
   l'application F2 injectée (scope, receive et send inchangés, hormis le
   retrait défensif des en-têtes Access-Control-* de la réponse).

Toute réponse produite par la porte : JSON, Content-Length, en-têtes
no-store, aucun Set-Cookie, aucune redirection, aucun en-tête CORS, corps
vide pour HEAD. Les refus ne lisent pas le corps de la requête, n'appellent
aucune application, aucune base et aucun fournisseur. Il n'existe AUCUN
repli : rien n'est jamais transmis à api:app.

Portée ASGI : « http » décidé comme ci-dessus ; « websocket » fermé avant
acceptation (1008) ; « lifespan » acquitté par la porte seule (rien n'est
transmis aux applications injectées) ; tout autre type ignoré.
"""
from dataclasses import dataclass

from core.route_policy import registry
from core.route_policy.canonical import canonical_segments, matches, template_segments, templates_overlap

# Actions fermées.
RETIRED = "retired"
NOT_FOUND = "not_found"
METHOD_NOT_ALLOWED = "method_not_allowed"
HEALTH = "health"
FORWARD_F2_AUTH = "forward_f2_auth"
FORWARD_F2_PERSONAL = "forward_f2_personal"
ACTIONS = (RETIRED, NOT_FOUND, METHOD_NOT_ALLOWED, HEALTH, FORWARD_F2_AUTH, FORWARD_F2_PERSONAL)

RETIRED_STATUS = 410
RETIRED_CODE = "legacy_personal_api_retired"
RETIRED_BODY = b'{"error":"legacy_personal_api_retired"}'
NOT_FOUND_BODY = b'{"detail":"Not Found"}'
METHOD_NOT_ALLOWED_BODY = b'{"detail":"Method Not Allowed"}'
HEALTH_BODY = b'{"status":"ok"}'

# Mêmes valeurs que les en-têtes no-store des applications F2 isolées.
NO_STORE_HEADERS = (
    ("cache-control", "no-store"),
    ("pragma", "no-cache"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
)

_CLOSED_CATEGORIES = frozenset({registry.DISABLED, registry.PUBLIC_PENDING_F05, registry.PUBLIC_PENDING_AUDIT,
                                registry.FRAMEWORK_DOCS})
_FORWARD = {registry.F2_AUTH: FORWARD_F2_AUTH, registry.F2_PERSONAL: FORWARD_F2_PERSONAL}


class RouteGateConfigError(ValueError):
    """Composition de la porte refusée."""


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    category: str = None
    template: str = None
    allow: tuple = ()


def _build_index():
    index = {}
    for rule in registry.RULES:
        entry = index.setdefault(rule.template, [template_segments(rule.template), rule.category, set()])
        entry[2].add(rule.method)
    out = tuple((template, parts, category, frozenset(methods)) for template, (parts, category, methods)
                in index.items())
    for i, (template_a, parts_a, _, _) in enumerate(out):
        if canonical_segments(template_a.replace("{", "x").replace("}", "x").encode("ascii")) is None:
            raise AssertionError(f"gabarit non canonique : {template_a}")
        for template_b, parts_b, _, _ in out[i + 1:]:
            if templates_overlap(parts_a, parts_b):
                raise AssertionError(f"gabarits ambigus : {template_a} / {template_b}")
    return out


_INDEX = _build_index()


def decide(method, raw_path) -> Decision:
    """Décision pure pour (méthode, chemin brut)."""
    segments = canonical_segments(raw_path)
    if segments is None:
        return Decision(NOT_FOUND, "non_canonical")
    for template, parts, category, methods in _INDEX:
        if matches(parts, segments):
            break
    else:
        return Decision(NOT_FOUND, "unknown")
    if category == registry.LEGACY_RETIRED:
        return Decision(RETIRED, "retired", category, template)
    if category in _CLOSED_CATEGORIES:
        return Decision(NOT_FOUND, "closed", category, template)
    allow = tuple(sorted(methods))
    if method not in methods:
        return Decision(METHOD_NOT_ALLOWED, "method", category, template, allow)
    if category == registry.PUBLIC_MAINTAINED:
        return Decision(HEALTH, "served", category, template, allow)
    return Decision(_FORWARD[category], "forward", category, template, allow)


def classify(scope) -> Decision:
    """Décision pour une portée ASGI http (root_path, cohérence path / raw_path)."""
    raw_path = scope.get("raw_path")
    if scope.get("root_path", "") != "":
        return Decision(NOT_FOUND, "root_path")
    decision = decide(scope.get("method"), raw_path)
    if decision.reason == "non_canonical":
        return decision
    if scope.get("path") != raw_path.decode("ascii"):
        return Decision(NOT_FOUND, "inconsistent_path")
    return decision


def _validate_downstream(app, category, label):
    if app is None:
        return None
    if not callable(app):
        raise RouteGateConfigError(f"{label} : application ASGI attendue")
    routes = getattr(app, "routes", None)
    if routes is None:
        raise RouteGateConfigError(f"{label} : application inspectable attendue")
    allowed = registry.pairs_of(category)
    exposed = set()
    for route in routes:
        path, methods = getattr(route, "path", None), getattr(route, "methods", None)
        if type(path) is not str or not methods:
            raise RouteGateConfigError(f"{label} : route sans méthode refusée")
        exposed |= {(method, path) for method in methods}
    if not exposed:
        raise RouteGateConfigError(f"{label} : aucune route")
    if not exposed <= allowed:
        raise RouteGateConfigError(f"{label} : route hors politique")
    for attribute in ("openapi_url", "docs_url", "redoc_url"):
        if getattr(app, attribute, None) is not None:
            raise RouteGateConfigError(f"{label} : documentation exposée")
    for middleware in getattr(app, "user_middleware", ()) or ():
        if getattr(getattr(middleware, "cls", None), "__name__", "") == "CORSMiddleware":
            raise RouteGateConfigError(f"{label} : CORS refusé")
    return app


class RoutePolicyGate:
    """Porte ASGI fermée (voir le module)."""

    def __init__(self, *, f2_auth_app, f2_personal_app):
        if f2_auth_app is not None and f2_auth_app is f2_personal_app:
            raise RouteGateConfigError("applications F2 distinctes attendues")
        self._downstream = {
            FORWARD_F2_AUTH: _validate_downstream(f2_auth_app, registry.F2_AUTH, "f2_auth_app"),
            FORWARD_F2_PERSONAL: _validate_downstream(f2_personal_app, registry.F2_PERSONAL, "f2_personal_app"),
        }

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind == "http":
            return await self._http(scope, receive, send)
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return None
        if kind == "lifespan":
            return await _lifespan(receive, send)
        return None

    async def _http(self, scope, receive, send):
        decision = classify(scope)
        method = scope.get("method")
        if decision.action in (FORWARD_F2_AUTH, FORWARD_F2_PERSONAL):
            app = self._downstream[decision.action]
            if app is not None:
                return await app(scope, receive, _without_cors(send))
            return await _respond(send, method, 404, NOT_FOUND_BODY)
        if decision.action == RETIRED:
            return await _respond(send, method, RETIRED_STATUS, RETIRED_BODY)
        if decision.action == METHOD_NOT_ALLOWED:
            return await _respond(send, method, 405, METHOD_NOT_ALLOWED_BODY,
                                  (("allow", ", ".join(decision.allow)),))
        if decision.action == HEALTH:
            return await _respond(send, method, 200, HEALTH_BODY)
        return await _respond(send, method, 404, NOT_FOUND_BODY)


async def _respond(send, method, status, body, extra=()):
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))]
    headers += [(k.encode("ascii"), v.encode("ascii")) for k, v in NO_STORE_HEADERS + tuple(extra)]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})


def _without_cors(send):
    async def filtered(message):
        if message.get("type") == "http.response.start":
            headers = [(k, v) for k, v in message.get("headers", ())
                       if not bytes(k).lower().startswith(b"access-control-")]
            message = {**message, "headers": headers}
        await send(message)
    return filtered


async def _lifespan(receive, send):
    while True:
        message = await receive()
        if message.get("type") == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message.get("type") == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return None
