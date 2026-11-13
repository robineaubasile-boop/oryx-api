"""Point d'entrée ASGI F2 DORMANT, enveloppe d'état et lifespan (ACT-L1 / F04-C V0.3).

Contrat : docs/act_l1_f04c_freeze_v03.md. Ce module n'est importé par aucun
module applicatif et n'est servi par aucune configuration : api:app reste le
seul point d'entrée servi. Une activation exigerait une décision fondateur
distincte (F-03), hors de ce lot.

    uvicorn core.f2_runtime.entrypoint:app      (jamais configuré sur Railway)

Import : aucun secret lu, aucune connexion, aucun fil, aucun moteur, aucun
import d'api ni des compositions F2 ; seuls la porte F-02A (pure) et le
registre sont chargés. `app` est une enveloppe INERTE.

Chaîne lorsque prête : Uvicorn -> F2Envelope -> RoutePolicyGate (F-02A
inchangée) -> Auth F2 (14 couples) / Personnel F2 + C3 (13 couples).

États : INERT -> STARTING -> OPEN -> DRAINING -> CLOSED ; un échec de
démarrage est terminal (CLOSED, issue STARTUP_FAILED), jamais OPEN.

HTTP :
- décision F-02A FORWARD_F2_* (fonction existante classify) : en OPEN,
  admission atomique (avant tout await) dans le WorkRegistry, puis appel de
  la porte avec scope / receive / send inchangés ; l'admission est libérée
  une seule fois dans un finally (y compris sous CancelledError). Hors OPEN :
  503 {"error":"temporarily_unavailable"} no-store, sans lecture du corps,
  sans SQL ni fournisseur ;
- toute autre décision (410 / 404 / 405 / GET /health, websocket 1008) :
  déléguée à la porte, octet pour octet ; avant OPEN, à une porte F-02A
  FERMÉE (aucune application aval), dont les réponses non F2 sont identiques.

Lifespan (une seule portée possédée) :
- startup : config F04-B (copie de os.environ) AVANT tout import pouvant
  charger core.db -> ressources F04-B -> précontrôle PostgreSQL en lecture
  seule -> compositions complètes avec le soumetteur commun -> égalité 14 /
  13, documentation / CORS / lifespan aval actif refusés -> porte -> OPEN ->
  un seul lifespan.startup.complete. Tout échec : ressources possédées
  fermées, un seul lifespan.startup.failed (étape et type d'erreur, aucun
  secret ni URL) ;
- shutdown : DRAINING (admissions fermées) -> attente CONJOINTE admissions ET
  travaux physiques (WorkRegistry, prédicat atomique puis scellement) ->
  F2Resources.close() -> CLOSED -> lifespan.shutdown.complete (issue
  GRACEFUL). Échéance de drainage dépassée (drain_timeout, None par défaut :
  aucune valeur inventée, F04-E) : issue DRAIN_TIMEOUT, ressources NON
  fermées, lifespan.shutdown.failed ; fermeture en échec : issue
  CLOSE_FAILED, lifespan.shutdown.failed. Ni l'une ni l'autre n'est une
  fermeture gracieuse.

Moteur legacy (Q2 / O1) : l'import des compositions charge core.db (via
core.models) et peut construire un moteur historique INERTE ; il n'est jamais
une ressource F04-C ni un moteur de secours. Aucune variable d'environnement
n'est modifiée.

Réserves ouvertes : P1 (résolveur d'IP : pair TCP direct provisoire, NON
validé pour Railway, F04-D) et P2 (délais, signaux, COMMIT incertain, F04-E).
"""
import asyncio
import os
import threading

from core.f2_runtime.work_registry import CURRENT_ADMISSION, RegisteredSubmitter, WorkRegistry
from core.route_policy.gate import (FORWARD_F2_AUTH, FORWARD_F2_PERSONAL, NO_STORE_HEADERS, RoutePolicyGate,
                                    classify)

INERT = "inert"
STARTING = "starting"
OPEN = "open"
DRAINING = "draining"
CLOSED = "closed"
STATES = (INERT, STARTING, OPEN, DRAINING, CLOSED)

STARTUP_FAILED = "startup_failed"
GRACEFUL = "graceful"
DRAIN_TIMEOUT = "drain_timeout"
CLOSE_FAILED = "close_failed"

MAINTENANCE_STATUS = 503
MAINTENANCE_BODY = b'{"error":"temporarily_unavailable"}'
SUBMITTER_STATE_ATTRIBUTE = "oryx_f2_submitter"

_FORWARD = frozenset({FORWARD_F2_AUTH, FORWARD_F2_PERSONAL})


class F2StartupError(RuntimeError):
    """Démarrage refusé : étape et type d'erreur seulement (aucun secret)."""

    def __init__(self, stage: str, cause: str):
        super().__init__(f"f2_startup_failed stage={stage} cause={cause}")
        self.stage = stage
        self.cause = cause


class F2CompositionError(ValueError):
    """Composition F2 hors contrat F04-C (message sans donnée sensible)."""


# --------------------------------------------------------------------------
# Contrôles de composition (§ 3.4, § 5 compatibilité, § 6.6)
# --------------------------------------------------------------------------

def exposed_pairs(app) -> frozenset:
    pairs = set()
    for route in getattr(app, "routes", None) or ():
        path, methods = getattr(route, "path", None), getattr(route, "methods", None)
        if type(path) is not str or not methods:
            raise F2CompositionError("route sans méthode")
        pairs |= {(method, path) for method in methods}
    return frozenset(pairs)


def downstream_lifespan_is_active(app) -> bool:
    """Lifespan aval ACTIF : contexte lifespan explicite ou gestionnaire
    startup / shutdown déclaré (FastAPI 0.110.0 recopie dans l'application
    ceux des routeurs inclus). Le _DefaultLifespan implicite de Starlette
    0.36.3, listes vides, n'est PAS actif."""
    from starlette.routing import _DefaultLifespan

    router = getattr(app, "router", None)
    if router is None:
        return True  # non inspectable : refusé
    if not isinstance(getattr(router, "lifespan_context", None), _DefaultLifespan):
        return True
    return bool(getattr(router, "on_startup", None)) or bool(getattr(router, "on_shutdown", None))


def verify_composition(app, category: str, submitter, label: str) -> None:
    from core.route_policy import registry

    if app is None:
        raise F2CompositionError(f"{label} : composition absente")
    if exposed_pairs(app) != registry.pairs_of(category):
        raise F2CompositionError(f"{label} : couples exposés différents du registre F-02A")
    for attribute in ("openapi_url", "docs_url", "redoc_url"):
        if getattr(app, attribute, None) is not None:
            raise F2CompositionError(f"{label} : documentation exposée")
    for middleware in getattr(app, "user_middleware", ()) or ():
        if getattr(getattr(middleware, "cls", None), "__name__", "") == "CORSMiddleware":
            raise F2CompositionError(f"{label} : CORS refusé")
    if getattr(getattr(app, "state", None), SUBMITTER_STATE_ATTRIBUTE, None) is not submitter:
        raise F2CompositionError(f"{label} : soumetteur suivi absent")
    if downstream_lifespan_is_active(app):
        raise F2CompositionError(f"{label} : lifespan aval actif")


def compose_f2(config, resources, submitter):
    """Deux compositions isolées EXISTANTES, même moteur F04-B, mêmes objets
    Keyring Auth et CsrfKeyring, même soumetteur ; C3 obligatoire ; puis la
    porte F-02A inchangée. Rend (porte, auth, personnel)."""
    from core.auth_f2_http.client_ip import PeerAddressResolver
    from core.auth_f2_http.isolated import build_isolated_f2_app
    from core.authz_f2_http.decryptage import DecryptageHttpConfig
    from core.authz_f2_http.isolated import build_isolated_personal_app
    from core.route_policy import registry

    if type(submitter) is not RegisteredSubmitter:
        raise F2CompositionError("soumetteur F04-C attendu")
    auth = build_isolated_f2_app(
        engine=resources.engine, keyring=config.keyring, supabase=resources.supabase, mailer=resources.mailer,
        csrf_keyring=config.csrf_keyring,
        ip_resolver=PeerAddressResolver(),  # provisoire : pair TCP direct, P1 NON validée (F04-D)
        allowed_origins=config.allowed_origins, submitter=submitter)
    personal = build_isolated_personal_app(
        engine=resources.engine, keyring=config.keyring, allowed_origins=config.allowed_origins,
        csrf_keyring=config.csrf_keyring, portfolio_keyring=config.portfolio_keyring,
        decryptage=DecryptageHttpConfig(decryptage_keyring=config.decryptage_keyring, eodhd=resources.eodhd,
                                        fmp=resources.fmp, claude=resources.claude, model=config.decryptage_model),
        submitter=submitter)
    verify_composition(auth, registry.F2_AUTH, submitter, "auth")
    verify_composition(personal, registry.F2_PERSONAL, submitter, "personnel")
    return RoutePolicyGate(f2_auth_app=auth, f2_personal_app=personal), auth, personal


def _default_resources_factory(config):
    from core.f2_runtime.resources import build_f2_resources

    return build_f2_resources(config)


# --------------------------------------------------------------------------
# Enveloppe
# --------------------------------------------------------------------------

class F2Envelope:
    """Application ASGI F04-C (voir le module).

    environ : mappage lu (COPIÉ) au démarrage seulement ; défaut os.environ.
    drain_timeout : None (défaut) = attente sans échéance ; un nombre de
      secondes (tests, qualification F04-E) borne l'attente : son dépassement
      donne DRAIN_TIMEOUT, jamais une fermeture gracieuse.
    resources_factory : config -> F2Resources (défaut build_f2_resources ;
      les tests injectent des transports simulés, jamais de repli).
    observer(kind, fields) : journal facultatif des transitions (registre et
      enveloppe), rapide et non bloquant."""

    def __init__(self, *, environ=None, drain_timeout=None, resources_factory=None, observer=None):
        if drain_timeout is not None and (isinstance(drain_timeout, bool) or not isinstance(drain_timeout, (int, float))
                                          or drain_timeout < 0):
            raise ValueError("drain_timeout : None ou nombre positif")
        if resources_factory is not None and not callable(resources_factory):
            raise TypeError("fabrique de ressources appelable attendue")
        self._environ = environ
        self._drain_timeout = drain_timeout
        self._resources_factory = resources_factory or _default_resources_factory
        self._observer = observer
        self._lock = threading.Lock()
        self._state = INERT
        self._outcome = None
        self._failure = None
        self._close_report = None
        self._lifespan_claimed = False
        self._registry = WorkRegistry(observer=observer)
        self._submitter = RegisteredSubmitter(self._registry)
        self._closed_gate = RoutePolicyGate(f2_auth_app=None, f2_personal_app=None)
        self._gate = None
        self._resources = None

    def __repr__(self) -> str:
        return f"F2Envelope(state={self._state})"

    def __reduce__(self):
        raise TypeError("F2Envelope n'est pas sérialisable")

    # -- observation ------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def outcome(self):
        return self._outcome

    @property
    def failure(self):
        """(étape, type d'erreur) d'un démarrage refusé, sinon None."""
        return self._failure

    @property
    def close_report(self):
        return self._close_report

    @property
    def registry(self) -> WorkRegistry:
        return self._registry

    @property
    def submitter(self) -> RegisteredSubmitter:
        return self._submitter

    @property
    def resources(self):
        return self._resources

    def _emit(self, kind, **fields):
        if self._observer is not None:
            try:
                self._observer(kind, fields)
            except Exception:  # noqa: BLE001
                pass

    def _set_outcome(self, outcome: str) -> None:
        self._outcome = outcome
        self._emit("OUTCOME", outcome=outcome)

    def _transition(self, expected: tuple, new: str) -> bool:
        with self._lock:
            if self._state not in expected:
                return False
            old, self._state = self._state, new
        self._emit("STATE", old=old, new=new)
        return True

    # -- ASGI -------------------------------------------------------------

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind == "lifespan":
            return await self._lifespan(receive, send)
        if kind == "http":
            return await self._http(scope, receive, send)
        return await (self._gate or self._closed_gate)(scope, receive, send)

    async def _http(self, scope, receive, send):
        if classify(scope).action not in _FORWARD:
            return await (self._gate or self._closed_gate)(scope, receive, send)
        admission = self._registry.admit()  # atomique, avant tout await
        if admission is None:
            return await _maintenance(send, scope.get("method"))
        context = CURRENT_ADMISSION.set(admission)
        try:
            return await self._gate(scope, receive, send)
        finally:
            CURRENT_ADMISSION.reset(context)
            admission.release()

    async def _lifespan(self, receive, send):
        with self._lock:
            claimed, self._lifespan_claimed = self._lifespan_claimed, True
        if claimed:
            # Une seule portée lifespan est possédée : toute autre est refusée
            # sans démarrage ni fermeture.
            while True:
                message = await receive()
                if message.get("type") == "lifespan.startup":
                    await send({"type": "lifespan.startup.failed", "message": "f2_lifespan_already_owned"})
                    return
                if message.get("type") == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.failed", "message": "f2_lifespan_already_owned"})
                    return
        started = False
        while True:
            message = await receive()
            kind = message.get("type")
            if kind == "lifespan.startup" and not started:
                started = True
                error = self._startup()
                if error is None:
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.startup.failed", "message": str(error)})
                    return
            elif kind == "lifespan.shutdown":
                await self._shutdown(send)
                return

    # -- démarrage --------------------------------------------------------

    def _startup(self):
        if not self._transition((INERT,), STARTING):
            return F2StartupError("state", self._state)
        stage, resources = "config", None
        try:
            from core.f2_runtime.config import load_f2_config  # n'importe pas core.db

            source = os.environ if self._environ is None else self._environ
            config = load_f2_config(dict(source))  # COPIE ; aucune mutation
            stage = "resources"
            from core.f2_runtime.resources import F2Resources

            resources = self._resources_factory(config)
            if type(resources) is not F2Resources:
                raise TypeError("F2Resources attendu")
            stage = "preflight"
            from core.f2_runtime.preflight import run_f2_preflight

            run_f2_preflight(resources.engine, config.keyring)
            stage = "composition"  # premier import pouvant charger core.db : après validation
            gate, _auth, _personal = compose_f2(config, resources, self._submitter)
        except Exception as exc:  # noqa: BLE001 — refus fermé, sans message d'origine
            cause = type(exc).__name__
            code = getattr(exc, "code", None)
            if stage == "preflight" and isinstance(code, str):
                cause = f"{cause}:{code}"
            self._fail(stage, cause, resources, getattr(exc, "cleanup", None))
            return F2StartupError(stage, cause)
        except BaseException:
            self._fail(stage, "interrupted", resources, None)
            raise
        self._resources, self._gate = resources, gate
        self._transition((STARTING,), OPEN)
        self._registry.open_admissions()
        return None

    def _fail(self, stage, cause, resources, cleanup):
        report = cleanup
        if resources is not None:
            report = resources.close()
        self._failure = (stage, cause)
        self._close_report = report
        self._emit("STARTUP_FAILED", stage=stage, cause=cause,
                   closed=getattr(report, "closed", None), failed=getattr(report, "failed", None))
        self._set_outcome(STARTUP_FAILED)
        self._transition((STARTING,), CLOSED)

    # -- arrêt ------------------------------------------------------------

    async def _shutdown(self, send):
        if self._state != OPEN:
            # Rien n'a été servi ni possédé (INERT) ou tout est déjà fermé
            # (échec de démarrage) : aucune ressource à fermer.
            if self._state == INERT:
                self._transition((INERT,), CLOSED)
            await send({"type": "lifespan.shutdown.complete"})
            return
        self._registry.close_admissions()  # DRAINING : plus aucune nouvelle admission
        self._transition((OPEN,), DRAINING)
        loop = asyncio.get_running_loop()
        deadline = None if self._drain_timeout is None else loop.time() + self._drain_timeout
        while not self._registry.seal_if_idle():
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            if not await self._registry.wait_idle(remaining) and deadline is not None and loop.time() >= deadline:
                if self._registry.seal_if_idle():
                    break
                snapshot = self._registry.snapshot()
                self._emit("DRAIN_TIMEOUT", admissions=snapshot.admissions_active,
                           jobs=snapshot.jobs_non_terminal)
                self._set_outcome(DRAIN_TIMEOUT)
                await send({"type": "lifespan.shutdown.failed", "message": "f2_drain_timeout"})
                return  # ressources NON fermées : des travaux peuvent encore les utiliser
        self._emit("DRAINED")
        report = self._resources.close()
        self._close_report = report
        self._emit("RESOURCES_CLOSED", closed=report.closed, failed=report.failed)
        self._transition((DRAINING,), CLOSED)
        if not report.ok:
            self._set_outcome(CLOSE_FAILED)
            await send({"type": "lifespan.shutdown.failed", "message": "f2_resource_close_failed"})
            return
        self._set_outcome(GRACEFUL)
        await send({"type": "lifespan.shutdown.complete"})


async def _maintenance(send, method):
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(MAINTENANCE_BODY)).encode("ascii"))]
    headers += [(k.encode("ascii"), v.encode("ascii")) for k, v in NO_STORE_HEADERS]
    await send({"type": "http.response.start", "status": MAINTENANCE_STATUS, "headers": headers})
    await send({"type": "http.response.body", "body": b"" if method == "HEAD" else MAINTENANCE_BODY})


def create_app(**kwargs) -> F2Envelope:
    """Nouvelle enveloppe INERTE (aucun effet externe)."""
    return F2Envelope(**kwargs)


# Instance inerte importable par Uvicorn ; rien n'est lu ni construit avant
# lifespan.startup. Aucune configuration de production ne la sert.
app = F2Envelope()
