"""Composition ISOLÉE de l'application HTTP F2 (R1 PR 3) — tests seulement.

build_isolated_f2_app construit une application FastAPI NEUVE qui ne
contient que les routes /api/auth/f2/*. Elle n'est importée par aucun
module applicatif : api.py (le véritable point d'entrée) reste sans route
F2, quelles que soient les variables d'environnement
(tests/test_f2_http_dormancy.py).

Tout est injecté et validé strictement ; rien n'est lu dans
l'environnement :

- engine     : moteur SQLAlchemy PostgreSQL ;
- keyring    : trousseau F2 à six usages (core.auth_f2.keys.Keyring) ;
- supabase, mailer : fournisseurs (doubles, ou adaptateurs de
  core.auth_f2.http_providers avec leur transport httpx injecté) ;
- csrf_keyring : trousseau CSRF propre ; aucune de ses clés ne peut être
  une clé F2 (indépendance vérifiée ici) ;
- ip_resolver : résolveur d'IP (core.auth_f2_http.client_ip) ;
- allowed_origins : origines https exactes ;
- clock / fault_hook : transmis au moteur et à l'admission HTTP de R1 PR 4
  (tests de frontières et de panne) ; wall_clock / monotonic : horloges de
  la couche HTTP ;
- submitter (ACT-L1 / F04-C) : soumetteur des travaux synchrones, même
  interface que run_in_threadpool ; None (défaut) : run_in_threadpool
  historique, chemin de compatibilité NON supervisé, jamais accepté par la
  composition F04-C (qui vérifie app.state.oryx_f2_submitter).

L'admission HTTP de R1 PR 4 (core.auth_f2.http_admission) est construite
ici avec le même moteur PostgreSQL et le même trousseau que le moteur F2 ;
elle ne s'applique qu'aux sept routes PR 4.

La construction n'ouvre aucune connexion, n'appelle aucun fournisseur,
n'écrit rien et N'INITIALISE PAS auth_abuse_key_state : cette
initialisation reste un acte explicite (abuse.initialize_abuse_key_state)
; sans elle, toute opération est refusée (503).
"""
import time

import sqlalchemy as sa
from fastapi import FastAPI

from core.auth_f2.http_admission import HttpAdmission
from core.auth_f2.keys import PURPOSES, Keyring
from core.auth_f2.service import F2AuthService
from core.auth_f2_http.csrf import CsrfKeyring
from core.auth_f2_http.provenance import validate_allowed_origins
from core.auth_f2_http.routes import NO_STORE_HEADERS, Dependencies, build_router


class F2HttpConfigError(ValueError):
    """Composition HTTP F2 invalide."""


class _NoStore:
    """En-têtes anti-cache sur TOUTE réponse de l'application isolée (y
    compris 404 / 405 produits par le routeur)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_no_store(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.decode("latin-1").lower()
                           not in NO_STORE_HEADERS]
                headers += [(k.encode("latin-1"), v.encode("latin-1")) for k, v in NO_STORE_HEADERS.items()]
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_no_store)


def build_isolated_f2_app(*, engine, keyring, supabase, mailer, csrf_keyring, ip_resolver, allowed_origins,
                          clock=None, fault_hook=None, wall_clock=time.time, monotonic=time.monotonic,
                          submitter=None) -> FastAPI:
    if not isinstance(engine, sa.engine.Engine) or engine.dialect.name != "postgresql":
        raise F2HttpConfigError("moteur PostgreSQL attendu")
    if not isinstance(keyring, Keyring):
        raise F2HttpConfigError("trousseau F2 attendu")
    if not isinstance(csrf_keyring, CsrfKeyring):
        raise F2HttpConfigError("trousseau CSRF attendu")
    for purpose in PURPOSES:
        for version in keyring.versions(purpose):
            if csrf_keyring.has_key_value(keyring.key(purpose, version)):
                raise F2HttpConfigError("clé CSRF identique à une clé F2")
    if not (callable(getattr(supabase, "generate_email_otp", None)) and callable(getattr(supabase, "verify_email_otp",
                                                                                         None))
            and callable(getattr(mailer, "send_otp", None))):
        raise F2HttpConfigError("fournisseurs Supabase et Resend attendus")
    if not callable(ip_resolver):
        raise F2HttpConfigError("résolveur d'IP attendu")
    if not callable(wall_clock) or not callable(monotonic):
        raise F2HttpConfigError("horloges attendues")
    if submitter is not None and not callable(submitter):
        raise F2HttpConfigError("soumetteur appelable attendu")
    try:
        origins = validate_allowed_origins(allowed_origins)
    except ValueError as exc:
        raise F2HttpConfigError(str(exc)) from None
    kwargs = {"fault_hook": fault_hook}
    if clock is not None:
        kwargs["clock"] = clock
    try:
        service = F2AuthService(engine, keyring, supabase, mailer, **kwargs)
        admission = HttpAdmission(engine, keyring, **kwargs)
    except (TypeError, ValueError) as exc:
        raise F2HttpConfigError(str(exc)) from None
    return make_app(Dependencies(service, csrf_keyring, ip_resolver, origins, wall_clock, monotonic, admission,
                                 submitter))


def make_app(deps: Dependencies) -> FastAPI:
    """Application neuve ne contenant que les routes F2 (aucune
    documentation OpenAPI, aucun CORS : même origine uniquement)."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(build_router(deps))
    app.add_middleware(_NoStore)
    app.state.oryx_f2_submitter = deps.submit  # F04-C : soumetteur effectivement injecté
    return app
