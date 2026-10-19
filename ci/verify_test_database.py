"""Vérifie qu'ORYX_TEST_DATABASE_URL désigne bien la base jetable du job CI.

    python ci/verify_test_database.py --expected-version 16.15 --report reports/postgres.json

Refuse (exit 1, message sans URL ni mot de passe) si :

1. l'environnement du processus contient une variable RAILWAY_*, DATABASE_*
   ou PG* (le processus est lancé sous `env -i`, comme pytest) ;
2. l'URL n'est pas PostgreSQL, sur 127.0.0.1 / ::1 / localhost, ou si le nom
   de base ne correspond pas à ^oryx_[a-z0-9_]*_test$ ;
3. côté serveur, dans la même connexion : current_database() non conforme,
   inet_server_addr() NULL (socket Unix : la CI exige TCP en boucle locale)
   ou non local, commentaire sentinelle absent, serveur en recovery, version
   différente de la version épinglée, listen_addresses autre que 127.0.0.1.

Mêmes règles que les garde-fous destructeurs de la PR #220 (tests/pg_guard.py
sur sa branche) ; ce script les applique AVANT pytest, indépendamment.
"""
import argparse
import ipaddress
import json
import os
import re
import sys

import sqlalchemy as sa

DISPOSABLE_COMMENT = "oryx-disposable-test-db"
TEST_DATABASE_NAME = re.compile(r"^oryx_[a-z0-9_]*_test$")
FORBIDDEN_ENV = re.compile(r"^(RAILWAY_|DATABASE_|PG)")


class Refused(RuntimeError):
    pass


def forbidden_environment(environ) -> list:
    return sorted(name for name in environ if FORBIDDEN_ENV.match(name))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(raw_url: str) -> sa.engine.URL:
    if not raw_url:
        raise Refused("ORYX_TEST_DATABASE_URL est vide")
    try:
        url = sa.engine.make_url(raw_url)
    except Exception as exc:  # noqa: BLE001 — jamais l'URL dans le message
        raise Refused("ORYX_TEST_DATABASE_URL illisible") from exc
    if url.get_backend_name() != "postgresql":
        raise Refused("la base de test doit être PostgreSQL")
    if url.query:
        raise Refused("aucun paramètre de requête accepté dans l'URL de test")
    if not url.host or not _is_loopback(url.host):
        raise Refused("seule la boucle locale (TCP) est autorisée")
    if not TEST_DATABASE_NAME.match(url.database or ""):
        raise Refused("le nom de base doit correspondre à ^oryx_[a-z0-9_]*_test$")
    return url


def check_server(row: dict, expected_version: str) -> None:
    if not TEST_DATABASE_NAME.match(row["database"] or ""):
        raise Refused("current_database() n'est pas une base de test Oryx")
    if row["server_addr"] is None:
        raise Refused("inet_server_addr() est NULL : connexion TCP en boucle locale exigée")
    if not _is_loopback(row["server_addr"]):
        raise Refused("inet_server_addr() n'est pas une adresse de boucle locale")
    if row["comment"] != DISPOSABLE_COMMENT:
        raise Refused("commentaire sentinelle de base jetable absent")
    if row["in_recovery"]:
        raise Refused("le serveur est en recovery")
    if row["listen_addresses"] != "127.0.0.1":
        raise Refused("listen_addresses doit valoir 127.0.0.1")
    if row["server_version"] != expected_version:
        raise Refused(f"version serveur {row['server_version']} != version épinglée {expected_version}")


def inspect_server(url: sa.engine.URL) -> dict:
    url = url.set(drivername="postgresql+psycopg2")
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT current_database() AS database, host(inet_server_addr()) AS server_addr, "
                "inet_server_port() AS server_port, "
                "shobj_description((SELECT oid FROM pg_database WHERE datname = current_database()), "
                "'pg_database') AS comment, pg_is_in_recovery() AS in_recovery, "
                "current_setting('server_version') AS server_version, "
                "current_setting('server_version_num') AS server_version_num, "
                "current_setting('listen_addresses') AS listen_addresses, version() AS version"
            )).mappings().one()
            return dict(row)
    finally:
        engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    try:
        banned = forbidden_environment(os.environ)
        if banned:
            raise Refused("variables interdites présentes : " + ", ".join(banned))
        url = check_url(os.environ.get("ORYX_TEST_DATABASE_URL", ""))
        row = inspect_server(url)
        check_server(row, args.expected_version)
    except Refused as exc:
        print(f"BASE DE TEST REFUSÉE : {exc}", file=sys.stderr)
        return 1
    report = {key: row[key] for key in (
        "database", "server_addr", "server_port", "server_version", "server_version_num",
        "listen_addresses", "version", "comment", "in_recovery")}
    report["python_environment_names"] = sorted(os.environ)
    os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
