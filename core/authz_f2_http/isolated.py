"""Application personnelle ISOLÉE (R1 PR 5, R1 PR 6, R1 PR 7, R1 PR 8) — tests seulement.

build_isolated_personal_app construit une application FastAPI NEUVE qui ne
contient que GET /api/v2/me, PUT /api/v2/me/level, les cinq routes
/api/v2/me/portfolio de R1 PR 7 et les trois lectures de R1 PR 8 (GET
/api/v2/me/analysis-overview, GET /api/v2/me/theses, GET
/api/v2/me/theses/{thesis_id}). Elle n'est importée
par aucun module applicatif : api.py reste sans ces routes, quelles que
soient les variables d'environnement (tests/test_authz_f2_dormancy.py). La
composition qui la réunit aux routes /api/auth/f2/* de PR 3 / PR 4
n'existe que dans les tests (tests/authz_f2_support.py).

Tout est injecté et validé strictement ; rien n'est lu dans
l'environnement : engine (PostgreSQL), keyring (le trousseau F2 du moteur
qui a émis les sessions), allowed_origins (https exactes), csrf_keyring
(R1 PR 6 : trousseau CSRF de PR 3, dont aucune clé ne peut être une clé
F2), wall_clock (horloge murale du CSRF, distincte de l'horloge
PostgreSQL), clock / fault_hook (tests de frontières et de panne),
portfolio_keyring (R1 PR 7 : trousseau HMAC portefeuille DÉDIÉ, facultatif
; absent, les mutations portefeuille rendent 503 ; aucune de ses clés ne
peut être une clé F2 ou CSRF). Le moteur doit être PostgreSQL ; une
isolation explicitement configurée autre que READ COMMITTED est refusée
(le runner la vérifie en outre dans chaque transaction portefeuille). La
construction n'ouvre aucune connexion et n'écrit rien. Le bootstrap CSRF
(GET /api/auth/f2/csrf/bootstrap) reste dans l'application F2 isolée.
"""
import time

from fastapi import FastAPI

from core.auth_f2.keys import PURPOSES
from core.auth_f2_http.csrf import CsrfKeyring
from core.auth_f2_http.isolated import _NoStore
from core.auth_f2_http.provenance import validate_allowed_origins
from core.authz_f2.portfolio_fingerprint import PortfolioKeyring
from core.authz_f2.runner import AuthorizationConfigError, AuthorizedTransactionRunner
from core.authz_f2_http.consultation import build_consultation_router
from core.authz_f2_http.portfolio import build_portfolio_router
from core.authz_f2_http.routes import PersonalDependencies, build_personal_router


class PersonalHttpConfigError(ValueError):
    """Composition personnelle invalide."""


def build_isolated_personal_app(*, engine, keyring, allowed_origins, csrf_keyring, clock=None, fault_hook=None,
                                wall_clock=time.time, portfolio_keyring=None) -> FastAPI:
    try:
        origins = validate_allowed_origins(allowed_origins)
    except ValueError as exc:
        raise PersonalHttpConfigError(str(exc)) from None
    if not isinstance(csrf_keyring, CsrfKeyring):
        raise PersonalHttpConfigError("trousseau CSRF attendu")
    if not callable(wall_clock):
        raise PersonalHttpConfigError("horloge murale attendue")
    if portfolio_keyring is not None and type(portfolio_keyring) is not PortfolioKeyring:
        raise PersonalHttpConfigError("trousseau portefeuille attendu")
    kwargs = {"fault_hook": fault_hook, "portfolio_keyring": portfolio_keyring}
    if clock is not None:
        kwargs["clock"] = clock
    try:
        runner = AuthorizedTransactionRunner(engine, keyring, **kwargs)
    except AuthorizationConfigError as exc:
        raise PersonalHttpConfigError(str(exc)) from None
    # Isolation explicitement configurée sur le moteur (options d'exécution
    # ou paramètre de create_engine) : READ COMMITTED seulement.
    configured = (engine.get_execution_options().get("isolation_level")
                  or getattr(engine.dialect, "_on_connect_isolation_level", None))
    if configured not in (None, "READ COMMITTED"):
        raise PersonalHttpConfigError("isolation READ COMMITTED attendue")
    # Même règle de composition que PR 3 : clés CSRF indépendantes des clés F2.
    for purpose in PURPOSES:
        for version in keyring.versions(purpose):
            key = keyring.key(purpose, version)
            if csrf_keyring.has_key_value(key):
                raise PersonalHttpConfigError("clé CSRF identique à une clé F2")
            if portfolio_keyring is not None and portfolio_keyring.has_key_value(key):
                raise PersonalHttpConfigError("clé portefeuille identique à une clé F2")
    if portfolio_keyring is not None:
        for version in portfolio_keyring.versions():
            if csrf_keyring.has_key_value(portfolio_keyring.key(version)):
                raise PersonalHttpConfigError("clé portefeuille identique à une clé CSRF")
    return make_personal_app(PersonalDependencies(runner, origins, csrf_keyring, wall_clock))


def make_personal_app(deps: PersonalDependencies) -> FastAPI:
    """Application neuve ne contenant que GET /api/v2/me, PUT
    /api/v2/me/level, les routes /api/v2/me/portfolio et les lectures
    /api/v2/me/analysis-overview et /api/v2/me/theses (aucune documentation
    OpenAPI, aucun CORS, en-têtes no-store sur toute réponse y compris
    404 / 405)."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(build_personal_router(deps))
    app.include_router(build_portfolio_router(deps))
    app.include_router(build_consultation_router(deps))
    app.add_middleware(_NoStore)
    return app
