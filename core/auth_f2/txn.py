"""Transactions F2 : PostgreSQL est l'unique coordinateur.

Chaque transaction F2 pose ses garde-fous (SET LOCAL) : une attente de
verrou ou une instruction trop longue (base saturée) lève une erreur et
annule la transaction. L'appelant n'en déduit JAMAIS une admission : il
refuse (refus fermé) et n'appelle aucun fournisseur.

Aucun appel réseau n'est fait à l'intérieur d'un bloc transaction() : les
services ouvrent une transaction, la committent, puis appellent le
fournisseur, puis ouvrent une nouvelle transaction de finalisation.
"""
from contextlib import contextmanager

import sqlalchemy as sa

from core.auth_f2.policy import LOCK_TIMEOUT, STATEMENT_TIMEOUT


@contextmanager
def transaction(engine):
    with engine.begin() as conn:
        conn.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        conn.execute(sa.text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
        yield conn


def pg_clock(conn):
    """Horloge PostgreSQL commune (clock_timestamp(), lue APRÈS la prise
    des verrous : deux transactions sérialisées sur un même sujet lisent
    des instants ordonnés)."""
    return conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one()
