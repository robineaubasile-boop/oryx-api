"""Garde-fous de propriété transactionnelle des frontières privées du lot 2C
(V0-T08 Lot 2C, PR 2 : extraction MÉCANIQUE depuis la frontière du cycle de
vie du registre des preuves, PR 1) — DORMANTS.

AUCUN appelant de production ; aucune logique métier, aucun SQL, aucune
variable d'activation. Importés seulement par les deux frontières privées du
lot 2C (registre des preuves, PR 1 ; événements, PR 2), sans appelant
elles-mêmes. Le code ci-dessous est celui de la PR 1, déplacé sans aucun
changement sémantique (mêmes noms, mêmes constantes, mêmes exceptions, mêmes
messages) :

- _require_engine : refus STATIQUE, avant toute connexion, de tout ce qui
  n'est pas un `sqlalchemy.Engine` PostgreSQL synchrone transactionnel
  (Connection, Session, URL, None, autre SGBD, moteur asynchrone,
  AUTOCOMMIT configuré, lecture seule) ;
- _require_effective_transaction / _owned : à l'intérieur de CHAQUE
  transaction, avant tout travail, libpq doit rapporter un bloc de
  transaction effectif (PQTRANS_INTRANS) ; sinon _NotOwned, rien n'est lu ni
  écrit. La validité d'une transaction ne garantit jamais celle d'une
  transaction ultérieure ouverte sur le même moteur.

Aucune mutualisation des décisions métier, des compteurs ni de la
classification des COMMIT incertains : chaque frontière garde les siens.
"""
from sqlalchemy.engine import Engine


# Niveau d'isolation et options qui priveraient la frontière d'une transaction
# autonome capable d'écrire.
_AUTOCOMMIT = "AUTOCOMMIT"
_NOT_OWNABLE = "engine : sqlalchemy.Engine PostgreSQL transactionnel requis (ni Connection, ni Session)"


def _isolation(level) -> str | None:
    return None if level is None else str(level).upper().replace("_", " ")


def _require_engine(engine) -> Engine:
    """Refus AVANT toute connexion (examen statique : aucune instruction)."""
    if not isinstance(engine, Engine):  # Connection, Session, URL, None... : la transaction serait celle d'autrui
        raise TypeError(_NOT_OWNABLE)
    dialect, options = engine.dialect, engine.get_execution_options()
    if dialect.name != "postgresql" or getattr(dialect, "is_async", False):
        raise TypeError(_NOT_OWNABLE)
    if _AUTOCOMMIT in (_isolation(options.get("isolation_level")),
                       _isolation(getattr(dialect, "_on_connect_isolation_level", None))):
        raise TypeError(_NOT_OWNABLE)  # aucune transaction : verrous relâchés, écritures hors de tout bloc
    if options.get("postgresql_readonly"):
        raise TypeError(_NOT_OWNABLE)
    return engine


# libpq PQTRANS_INTRANS : bloc de transaction ouvert et sain, rapporté par le
# serveur (ReadyForQuery 'T') ; psycopg2 et psycopg exposent la même valeur.
_INTRANS = 2


class _NotOwned(Exception):
    """La session ne porte pas un bloc de transaction PostgreSQL effectif."""


def _require_effective_transaction(db) -> None:
    status = db.connection().connection.dbapi_connection.info.transaction_status
    if status != _INTRANS:
        raise _NotOwned()


def _owned(work):
    """`work` n'est exécuté que dans un bloc de transaction effectif, ouvert
    par la frontière du bloc C (garde-fous déjà posés) ; sinon exception :
    rien n'est lu ni écrit, la transaction est annulée (_FAILED)."""
    def run(db, **kwargs):
        _require_effective_transaction(db)
        return work(db, **kwargs)
    return run
