"""Application personnelle ISOLÉE (R1 PR 5, R1 PR 6) — tests seulement.

build_isolated_personal_app construit une application FastAPI NEUVE qui ne
contient que GET /api/v2/me et PUT /api/v2/me/level. Elle n'est importée
par aucun module applicatif : api.py reste sans ces routes, quelles que
soient les variables d'environnement (tests/test_authz_f2_dormancy.py). La
composition qui la réunit aux routes /api/auth/f2/* de PR 3 / PR 4
n'existe que dans les tests (tests/authz_f2_support.py).

Tout est injecté et validé strictement ; rien n'est lu dans
l'environnement : engine (PostgreSQL), keyring (le trousseau F2 du moteur
qui a émis les sessions), allowed_origins (https exactes), csrf_keyring
(R1 PR 6 : trousseau CSRF de PR 3, dont aucune clé ne peut être une clé
F2), wall_clock (horloge murale du CSRF, distincte de l'horloge
PostgreSQL), clock / fault_hook (tests de frontières et de panne). La
construction n'ouvre aucune connexion et n'écrit rien. Le bootstrap CSRF
(GET /api/auth/f2/csrf/bootstrap) reste dans l'application F2 isolée.
"""
import time

from fastapi import FastAPI

from core.auth_f2.keys import PURPOSES
from core.auth_f2_http.csrf import CsrfKeyring
from core.auth_f2_http.isolated import _NoStore
from core.auth_f2_http.provenance import validate_allowed_origins
from core.authz_f2.runner import AuthorizationConfigError, AuthorizedTransactionRunner
from core.authz_f2_http.routes import PersonalDependencies, build_personal_router


class PersonalHttpConfigError(ValueError):
    """Composition personnelle invalide."""


def build_isolated_personal_app(*, engine, keyring, allowed_origins, csrf_keyring, clock=None, fault_hook=None,
                                wall_clock=time.time) -> FastAPI:
    try:
        origins = validate_allowed_origins(allowed_origins)
    except ValueError as exc:
        raise PersonalHttpConfigError(str(exc)) from None
    if not isinstance(csrf_keyring, CsrfKeyring):
        raise PersonalHttpConfigError("trousseau CSRF attendu")
    if not callable(wall_clock):
        raise PersonalHttpConfigError("horloge murale attendue")
    kwargs = {"fault_hook": fault_hook}
    if clock is not None:
        kwargs["clock"] = clock
    try:
        runner = AuthorizedTransactionRunner(engine, keyring, **kwargs)
    except AuthorizationConfigError as exc:
        raise PersonalHttpConfigError(str(exc)) from None
    # Même règle de composition que PR 3 : clés CSRF indépendantes des clés F2.
    for purpose in PURPOSES:
        for version in keyring.versions(purpose):
            if csrf_keyring.has_key_value(keyring.key(purpose, version)):
                raise PersonalHttpConfigError("clé CSRF identique à une clé F2")
    return make_personal_app(PersonalDependencies(runner, origins, csrf_keyring, wall_clock))


def make_personal_app(deps: PersonalDependencies) -> FastAPI:
    """Application neuve ne contenant que GET /api/v2/me et PUT
    /api/v2/me/level (aucune documentation OpenAPI, aucun CORS, en-têtes
    no-store sur toute réponse y compris 404 / 405)."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(build_personal_router(deps))
    app.add_middleware(_NoStore)
    return app
