"""Processus concurrents de R1 PR 7 (multiprocessing, contexte spawn).

Chaque processus construit SON PROPRE runner sur SON PROPRE moteur
SQLAlchemy (NullPool) : ses connexions PostgreSQL sont indépendantes de
celles des autres processus et du processus de test (pg_backend_pid()
relevés et rapportés). Tous exécutent la MÊME intention (même triplet)
après une barrière. Le secret de session transite en chaîne et n'est
jamais rapporté ; les clés portefeuille sont les clés fictives des tests.
"""
import uuid

import sqlalchemy as sa

from core.auth_f2.crypto import Redacted
from core.auth_f2.service import SessionCredential
from core.authz_f2 import commands as c
from core.authz_f2.runner import AuthorizedTransactionRunner
from tests.authz_f2_support import portfolio_keyring
from tests.f2_support import make_keyring


def worker(url, args, barrier, queue):
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    pids = set()

    @sa.event.listens_for(engine, "connect")
    def record_pid(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("SELECT pg_backend_pid()")
        pids.add(cursor.fetchone()[0])
        cursor.close()

    try:
        runner = AuthorizedTransactionRunner(engine, make_keyring(), portfolio_keyring=portfolio_keyring())
        credential = SessionCredential(uuid.UUID(args["generation_id"]), Redacted(args["secret"]))
        operation_id = uuid.UUID(args["operation_id"])
        if args["kind"] == "create":
            command = c.CreatePortfolioPosition(operation_id, "AIR.PA", "4", None, None, "PEA")
        else:
            command = c.ReplacePortfolioPosition(operation_id, args["position_id"], 0, "AIR.PA", "5", None, None,
                                                 "PEA")
        barrier.wait(timeout=120)
        outcome = runner.run(credential, command)
        body = outcome.value.body.decode() if outcome.value is not None else None
        queue.put(("ok", outcome.status, body, sorted(pids)))
    except BaseException as exc:  # noqa: BLE001 — rapporté au processus de test
        queue.put(("error", f"{type(exc).__name__}: {str(exc)[:300]}", None, sorted(pids)))
    finally:
        engine.dispose()
