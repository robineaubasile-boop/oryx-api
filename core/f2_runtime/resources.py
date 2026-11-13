"""Ressources F2 à propriété explicite (ACT-L1 / F04-B V0.3, B05) — DORMANT.

build_f2_resources(config) construit, dans cet ordre, les ressources de la
future composition F2 et les enregistre au fur et à mesure dans un
ResourceOwner :

    1. engine    moteur PostgreSQL F2 unique (core.f2_runtime.database),
                 partagé par les compositions Auth et Personnel ;
                 fermeture : engine.dispose()
    2. supabase  HttpSupabaseOtpProvider ; fermeture : close() (client httpx)
    3. resend    HttpResendMailer ; fermeture : close() (client httpx)
    4. eodhd     EodhdTransport en mode session_factory (une session Requests
                 par appel, fermée à l'issue de l'appel) : aucune ressource
                 durable à fermer
    5. fmp       FmpTransport, même mode que EODHD
    6. claude    ClaudeTransport (max_retries=0) ; fermeture : close()
                 (client SDK Anthropic)

Règles de propriété :

- seule une ressource EFFECTIVEMENT construite est enregistrée ; une
  construction qui échoue n'enregistre rien, et toutes les ressources déjà
  construites sont fermées en ordre inverse avant que l'échec ne remonte
  (F2ResourceBuildError : étape et type d'erreur, aucun message d'origine) ;
- une ressource INJECTÉE (engine=..., ou un http_client / transport fourni à
  un adaptateur) appartient à celui qui l'a fournie : elle n'est jamais
  fermée ici (engine : enregistré comme emprunté ; transports et clients
  injectés : les close() des adaptateurs ne les ferment pas) ;
- close() ferme chaque ressource possédée UNE seule fois, en ordre inverse
  de construction, y compris si la fermeture d'une autre échoue ; il est
  idempotent (un second appel ne ferme rien et rend le même compte rendu) ;
- aucune fermeture n'utilise un attribut privé d'un adaptateur : seules les
  méthodes publiques close() / dispose() sont appelées.

LIMITE (F04-A § 8.6, § 10.3) : ce module NE DÉCIDE JAMAIS qu'une ressource
est libre d'usage. Appeler close() pendant qu'un travail suivi (requête
d'authentification, fil C3 /turns, /replay, /ack) peut encore l'utiliser
n'est pas une fermeture gracieuse : garantir la fin effective des travaux
avant close() appartient à F04-C (suivi des travaux engagés) et F04-E
(drainage, délai dépassé, arrêt forcé).

Aucun effet à l'import ; build_f2_resources n'ouvre aucune connexion
PostgreSQL et n'appelle aucun fournisseur (les clients se connectent au
premier appel).
"""
import threading
from dataclasses import dataclass

import sqlalchemy as sa

from core.auth_f2.http_providers import HttpResendMailer, HttpSupabaseOtpProvider
from core.decryptage_providers import ClaudeTransport, EodhdTransport, FmpTransport, isolated_session
from core.f2_runtime.config import F2Config
from core.f2_runtime.database import create_f2_engine

STAGES = ("engine", "supabase", "resend", "eodhd", "fmp", "claude")


class F2ResourceError(RuntimeError):
    """Erreur de ressource F2 (message sans secret)."""


class F2ResourceBuildError(F2ResourceError):
    """Construction interrompue à l'étape stage ; les ressources déjà
    construites ont été fermées (cleanup : compte rendu de cette fermeture)."""

    def __init__(self, stage: str, cause_type: str, cleanup):
        super().__init__(f"construction des ressources F2 interrompue à l'étape {stage} ({cause_type})")
        self.stage = stage
        self.cause_type = cause_type
        self.cleanup = cleanup


@dataclass(frozen=True)
class CloseReport:
    """closed : ressources possédées fermées sans erreur (ordre de
    fermeture) ; failed : (nom, type d'erreur) des fermetures en échec ;
    borrowed : ressources empruntées, jamais fermées ici."""
    closed: tuple
    failed: tuple
    borrowed: tuple

    @property
    def ok(self) -> bool:
        return not self.failed


class ResourceOwner:
    """Registre ordonné des ressources construites ; fermeture unique."""

    __slots__ = ("_lock", "_entries", "_report")

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = []  # (nom, ressource, fermeture ou None, possédée)
        self._report = None

    def __repr__(self) -> str:
        return f"ResourceOwner({', '.join(name for name, *_ in self._entries)})"

    def __reduce__(self):
        raise TypeError("ResourceOwner n'est pas sérialisable")

    def _register(self, name, resource, closer, owned):
        if type(name) is not str or not name:
            raise ValueError("nom de ressource attendu")
        if closer is not None and not callable(closer):
            raise TypeError("fermeture appelable attendue")
        with self._lock:
            if self._report is not None:
                raise F2ResourceError("registre de ressources déjà fermé")
            if any(entry[0] == name for entry in self._entries):
                raise F2ResourceError("ressource déjà enregistrée")
            self._entries.append((name, resource, closer, owned))
        return resource

    def own(self, name: str, resource, closer):
        """Ressource construite ici ; closer=None : rien de durable à fermer."""
        return self._register(name, resource, closer, True)

    def borrow(self, name: str, resource):
        """Ressource fournie par un tiers : jamais fermée par ce registre."""
        return self._register(name, resource, None, False)

    def names(self) -> tuple:
        with self._lock:
            return tuple(entry[0] for entry in self._entries)

    def owned(self) -> tuple:
        with self._lock:
            return tuple(entry[0] for entry in self._entries if entry[3])

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._report is not None

    def close(self) -> CloseReport:
        """Ferme les ressources possédées en ordre inverse, une seule fois."""
        with self._lock:
            if self._report is not None:
                return self._report
            closed, failed = [], []
            for name, _resource, closer, owned in reversed(self._entries):
                if not owned:
                    continue
                if closer is not None:
                    try:
                        closer()
                    except Exception as exc:  # noqa: BLE001 — les autres fermetures doivent avoir lieu
                        failed.append((name, type(exc).__name__))
                        continue
                closed.append(name)
            self._report = CloseReport(tuple(closed), tuple(failed),
                                       tuple(entry[0] for entry in self._entries if not entry[3]))
            return self._report


class F2Resources:
    """Ressources construites par build_f2_resources (voir le module)."""

    __slots__ = ("engine", "supabase", "mailer", "eodhd", "fmp", "claude", "_owner")

    def __init__(self, owner: ResourceOwner, **resources):
        self._owner = owner
        for name in ("engine", "supabase", "mailer", "eodhd", "fmp", "claude"):
            setattr(self, name, resources[name])

    def __repr__(self) -> str:
        return "F2Resources(<private>)"

    def __reduce__(self):
        raise TypeError("F2Resources n'est pas sérialisable")

    @property
    def owner(self) -> ResourceOwner:
        return self._owner

    @property
    def closed(self) -> bool:
        return self._owner.closed

    def close(self) -> CloseReport:
        """Voir ResourceOwner.close et la LIMITE du module."""
        return self._owner.close()


def build_f2_resources(config, *, engine=None, supabase_transport=None, resend_transport=None,
                       claude_http_client=None, session_factory=None) -> F2Resources:
    """Construit les six ressources F2 à partir d'une F2Config validée.

    Paramètres facultatifs (tests ou composition qui possède déjà la
    ressource) : engine (emprunté, jamais fermé ici), transports httpx
    Supabase / Resend et http_client Anthropic (restent à leur
    propriétaire), session_factory (EODHD / FMP ; défaut :
    core.decryptage_providers.isolated_session, trust_env=False)."""
    if type(config) is not F2Config:
        raise TypeError("F2Config attendue")
    if engine is not None and (not isinstance(engine, sa.engine.Engine) or engine.dialect.name != "postgresql"):
        raise TypeError("moteur PostgreSQL attendu")
    if session_factory is not None and not callable(session_factory):
        raise TypeError("fabrique de sessions appelable attendue")
    factory = session_factory or isolated_session
    owner = ResourceOwner()
    built = {}
    stage = STAGES[0]
    try:
        if engine is None:
            created = create_f2_engine(config.database_url)
            built["engine"] = owner.own("engine", created, created.dispose)
        else:
            built["engine"] = owner.borrow("engine", engine)
        stage = "supabase"
        supabase = HttpSupabaseOtpProvider(base_url=config.supabase_url, service_key=config.supabase_service_key,
                                           api_key=config.supabase_verify_key, transport=supabase_transport)
        built["supabase"] = owner.own("supabase", supabase, supabase.close)
        stage = "resend"
        mailer = HttpResendMailer(api_key=config.resend_api_key, sender=config.resend_sender,
                                  transport=resend_transport)
        built["mailer"] = owner.own("resend", mailer, mailer.close)
        stage = "eodhd"
        built["eodhd"] = owner.own("eodhd", EodhdTransport(config.eodhd_api_key.reveal(), session_factory=factory),
                                   None)
        stage = "fmp"
        built["fmp"] = owner.own("fmp", FmpTransport(config.fmp_api_key.reveal(), session_factory=factory), None)
        stage = "claude"
        claude = ClaudeTransport(config.anthropic_api_key.reveal(), http_client=claude_http_client)
        built["claude"] = owner.own("claude", claude, claude.close)
    except Exception as exc:  # noqa: BLE001 — nettoyage puis échec fermé, sans message d'origine
        raise F2ResourceBuildError(stage, type(exc).__name__, owner.close()) from None
    return F2Resources(owner, **built)
