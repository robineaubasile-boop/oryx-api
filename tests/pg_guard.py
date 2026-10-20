"""Garde-fous des tests PostgreSQL destructeurs (R1 PR 1).

Les tests de migration détruisent puis recréent le schéma public de la base
visée par ORYX_TEST_DATABASE_URL. Ce module refuse toute cible qui pourrait
être une base Railway, de production ou simplement distante, en trois
niveaux indépendants :

1. Environnement du processus pytest : aucune variable Railway (RAILWAY_*),
   aucune URL applicative (DATABASE_URL, DATABASE_PUBLIC_URL,
   DATABASE_PRIVATE_URL) ni variable libpq (PGHOST, PGDATABASE, ...) ne doit
   être présente (vérifié au démarrage par tests/conftest.py).
2. URL de test : PostgreSQL uniquement, hôte de boucle locale ou socket
   Unix, aucun nom d'hôte Railway, nom de base ^oryx_[a-z0-9_]*_test$.
3. Serveur, dans la connexion même qui exécute le DROP : current_database()
   conforme, adresse serveur NULL (socket Unix) ou de boucle locale, et
   commentaire sentinelle de la base égal à DISPOSABLE_COMMENT, posé à la
   main lors de la création de la base jetable :

       COMMENT ON DATABASE oryx_pr1_test IS 'oryx-disposable-test-db';

Aucun de ces contrôles n'affiche l'URL ni un mot de passe.
"""
import ipaddress
import os
import re

import sqlalchemy as sa

DISPOSABLE_COMMENT = "oryx-disposable-test-db"
TEST_DATABASE_NAME = re.compile(r"^oryx_[a-z0-9_]*_test$")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
FORBIDDEN_HOST_MARKERS = ("rlwy", "railway", "proxy")
FORBIDDEN_ENV_PREFIXES = ("RAILWAY_",)
FORBIDDEN_ENV_NAMES = (
    "DATABASE_URL", "DATABASE_PUBLIC_URL", "DATABASE_PRIVATE_URL",
    "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSERVICE", "PGSERVICEFILE",
)


class UnsafeTestDatabase(RuntimeError):
    """Cible PostgreSQL refusée pour un test destructeur."""


def forbidden_environment(environ=None) -> list:
    """Noms (jamais les valeurs) des variables interdites présentes."""
    environ = os.environ if environ is None else environ
    return sorted(
        name for name in environ
        if name in FORBIDDEN_ENV_NAMES or name.startswith(FORBIDDEN_ENV_PREFIXES)
    )


def scrubbed_environment(environ=None) -> dict:
    """Copie de l'environnement sans aucune variable interdite."""
    environ = os.environ if environ is None else environ
    banned = set(forbidden_environment(environ))
    return {name: value for name, value in environ.items() if name not in banned}


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_test_database_url(raw_url: str) -> sa.engine.URL:
    """Valide l'URL d'une base de test destructrice et la retourne parsée.
    Lève UnsafeTestDatabase (message sans secret) sinon."""
    if not raw_url:
        raise UnsafeTestDatabase("ORYX_TEST_DATABASE_URL est vide")
    try:
        url = sa.engine.make_url(raw_url)
    except Exception as exc:  # noqa: BLE001 — jamais l'URL dans le message
        raise UnsafeTestDatabase("ORYX_TEST_DATABASE_URL illisible") from exc
    if url.get_backend_name() != "postgresql":
        raise UnsafeTestDatabase("la base de test doit être PostgreSQL")
    hosts = []
    if url.host:
        hosts.append(url.host)
    query_host = url.query.get("host")
    if query_host:
        hosts.extend(query_host if isinstance(query_host, tuple) else (query_host,))
    if not hosts:
        raise UnsafeTestDatabase("hôte implicite refusé : préciser localhost ou un socket Unix")
    for host in hosts:
        lowered = host.lower()
        if any(marker in lowered for marker in FORBIDDEN_HOST_MARKERS):
            raise UnsafeTestDatabase("hôte de type Railway / proxy refusé")
        if not (host.startswith("/") or _is_loopback(lowered)):
            raise UnsafeTestDatabase("seuls la boucle locale et les sockets Unix sont autorisés")
    if not TEST_DATABASE_NAME.match(url.database or ""):
        raise UnsafeTestDatabase("le nom de base doit correspondre à ^oryx_[a-z0-9_]*_test$")
    banned = forbidden_environment()
    if banned:
        raise UnsafeTestDatabase("variables interdites présentes : " + ", ".join(banned))
    return url


def assert_disposable_connection(conn) -> None:
    """Contrôle côté serveur, à appeler dans la connexion qui va détruire."""
    database, server_addr, comment = conn.execute(sa.text(
        "SELECT current_database(), host(inet_server_addr()), "
        "shobj_description((SELECT oid FROM pg_database WHERE datname = current_database()), 'pg_database')"
    )).one()
    if not TEST_DATABASE_NAME.match(database or ""):
        raise UnsafeTestDatabase("current_database() n'est pas une base de test Oryx")
    if server_addr is not None and not _is_loopback(server_addr):
        raise UnsafeTestDatabase("le serveur PostgreSQL n'est pas local")
    if comment != DISPOSABLE_COMMENT:
        raise UnsafeTestDatabase("commentaire sentinelle de base jetable absent")
