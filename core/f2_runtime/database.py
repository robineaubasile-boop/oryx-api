"""Moteur SQLAlchemy synchrone F2 (ACT-L1 / F04-B V0.3, B03 et B04) — DORMANT.

Fabrique EXPLICITE : aucun moteur n'est construit à l'import, aucune
variable d'environnement n'est lue, aucune connexion n'est ouverte par
create_f2_engine (le pool se remplit au premier emprunt). Ce module
n'importe ni core.db ni aucun module qui l'importe : core.db.engine et
core.db.SessionLocal (moteur legacy construit à l'import de core.db) ne sont
jamais utilisés par F2.

Un seul moteur est construit par la composition F04-B
(core.f2_runtime.resources) et partagé par les futures compositions Auth et
Personnel ; il appartient au gestionnaire de ressources, qui le ferme
explicitement (dispose) une seule fois.

Paramètres CANDIDATS figés pour l'implémentation dormante (freeze F04-B
V0.3), NON validés pour l'exploitation Railway :

    pool_size=5, max_overflow=2 (7 connexions au plus), pool_timeout=5 s
    (attente bornée d'une connexion ; au-delà : sqlalchemy.exc.TimeoutError,
    refus fermé de l'appelant), pool_pre_ping=True, pool_recycle=1800 s,
    isolation_level="READ COMMITTED" (explicite).

Aucun retry transactionnel : une saturation ou une panne remonte telle
quelle à l'appelant. hide_parameters=True : les erreurs SQLAlchemy ne
reproduisent jamais les paramètres liés (empreintes, identifiants).

Normalisation de DATABASE_URL (compatible avec les formats PostgreSQL
acceptés par core.db.normalize_database_url, mais FERMÉE) :

    postgres://...              -> postgresql+psycopg2://...
    postgresql://...            -> postgresql+psycopg2://...
    postgresql+psycopg2://...   inchangé
    tout autre schéma ou pilote -> refus (F2DatabaseUrlError)

L'URL doit désigner un hôte, un utilisateur et une base. Les messages
d'erreur ne reproduisent jamais l'URL ni le mot de passe.
"""
import sqlalchemy as sa
from sqlalchemy.pool import QueuePool

DRIVERNAME = "postgresql+psycopg2"
ISOLATION_LEVEL = "READ COMMITTED"
POOL_SIZE = 5
MAX_OVERFLOW = 2
POOL_TIMEOUT_SECONDS = 5
POOL_PRE_PING = True
POOL_RECYCLE_SECONDS = 1800
POOL_PARAMETERS = {
    "pool_size": POOL_SIZE,
    "max_overflow": MAX_OVERFLOW,
    "pool_timeout": POOL_TIMEOUT_SECONDS,
    "pool_pre_ping": POOL_PRE_PING,
    "pool_recycle": POOL_RECYCLE_SECONDS,
    "isolation_level": ISOLATION_LEVEL,
}
_SCHEMES = (("postgres://", "postgresql://"), ("postgresql://", DRIVERNAME + "://"))


class F2DatabaseUrlError(ValueError):
    """DATABASE_URL hors contrat F2 (message sans URL ni secret)."""


def normalize_f2_database_url(raw) -> sa.engine.URL:
    """URL SQLAlchemy postgresql+psycopg2 de DATABASE_URL, ou refus fermé."""
    if type(raw) is not str or not raw:
        raise F2DatabaseUrlError("DATABASE_URL : chaîne non vide attendue")
    value = raw
    for prefix, replacement in _SCHEMES:
        if value.startswith(prefix):
            value = replacement + value[len(prefix):]
    if not value.startswith(DRIVERNAME + "://"):
        raise F2DatabaseUrlError("DATABASE_URL : schéma PostgreSQL (pilote psycopg2) attendu")
    try:
        url = sa.engine.make_url(value)
    except Exception:  # noqa: BLE001 — le message d'origine peut contenir l'URL
        raise F2DatabaseUrlError("DATABASE_URL illisible") from None
    if url.drivername != DRIVERNAME:
        raise F2DatabaseUrlError("DATABASE_URL : pilote psycopg2 attendu")
    if not url.host or not url.username or not url.database:
        raise F2DatabaseUrlError("DATABASE_URL : hôte, utilisateur et base attendus")
    return url


def create_f2_engine(url) -> sa.engine.Engine:
    """Moteur F2 neuf, pool borné, READ COMMITTED explicite. Aucune
    connexion ouverte ici. url : sa.engine.URL rendue par
    normalize_f2_database_url (toute autre valeur est refusée)."""
    if not isinstance(url, sa.engine.URL) or url.drivername != DRIVERNAME:
        raise F2DatabaseUrlError("URL normalisée postgresql+psycopg2 attendue")
    return sa.create_engine(url, poolclass=QueuePool, hide_parameters=True, **POOL_PARAMETERS)
