"""Tests de R1 PR 7 : migration 0020_position_revision et modèle correspondant.

1. Sans base : maillon 0019 -> 0020 (tête), identifiant, fichiers 0001 à
   0019 inchangés, SQL offline exact (upgrade additif ; downgrade : délais,
   verrous dans l'ordre positions puis reçus, garde DO évaluée à
   l'exécution, retrait), modèle aligné.
2. PostgreSQL réel (base jetable, tests/pg_guard.py) : montée 0019 -> 0020
   sur données historiques (révision 0, aucune autre valeur modifiée,
   aucune réécriture de la table), colonne / défaut / CHECK exacts,
   catalogue des autres tables intact, insertion historique sans la
   colonne, downgrade exact sur base vierge ou sans données versionnées,
   downgrade REFUSÉ (en ligne et par la garde offline) si un reçu existe
   ou si une révision est non nulle, verrous du downgrade contre les
   écritures concurrentes.
"""
import hashlib
import importlib.util
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from core.db import Base
from tests.pg_guard import assert_disposable_connection
from tests.test_migration_0002_analysis_sessions import (
    REPO_ROOT,
    _reset_schema,
    _run_alembic,
    _script_directory,
    _snapshot,
    _tables,
    _version,
    pg_engine,  # noqa: F401 — fixture
    pg_url,  # noqa: F401 — fixture
)
from tests.test_migration_0004_drop_company_analyses import (
    PR4_0019,
    PR7_0020,
    PR7_0020_FILE,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import _catalog, _global_catalog
from tests.test_migration_0015_users_level_revision import _seed_historical
from tests.test_migration_0016_portfolio_idempotency import _receipt, _rejects
from tests.test_migration_0018_f2_abuse_journal import PR1_SHA256
from tests.test_migration_0019_f2_http_admission import PR2_SHA256

MIGRATION = REPO_ROOT / "alembic" / "versions" / PR7_0020_FILE
# sha256 de 0019 telle que fusionnée sur main (59265c8) : R1 PR 7 ne la modifie pas.
PR4_SHA256 = {"0019_f2_http_admission.py": "a1bf6e21dbdf00cd853e01030bc361ba13305d1a9907231f2cb5b81d6485dcd2"}
OFFLINE = "postgresql://offline@localhost/offline"
GUARDS = ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '15s'",
          "SET LOCAL idle_in_transaction_session_timeout = '15s'"]
CHECK = "ck_portfolio_positions_position_revision_non_negative"


def _m0020():
    spec = importlib.util.spec_from_file_location("m0020", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_links_0020_to_0019_and_is_the_head():
    script = _script_directory()
    assert script.get_heads() == [PR7_0020]
    assert script.get_revision(PR7_0020).down_revision == PR4_0019
    assert PR7_0020_FILE == f"{PR7_0020}.py" and len(PR7_0020) <= 32
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(files) == 20 and files[-1] == PR7_0020_FILE


def test_migrations_0001_to_0019_are_unchanged():
    for name, digest in {**PR1_SHA256, **PR2_SHA256, **PR4_SHA256}.items():
        path = REPO_ROOT / "alembic" / "versions" / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
    for line in (REPO_ROOT / "ci" / "migration_history.sha256").read_text().splitlines():  # 0001 à 0014
        digest, _, relative = line.partition("  ")
        assert hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest() == digest, relative


def test_offline_upgrade_is_exactly_the_additive_column():
    statements = _statements(_run_alembic(OFFLINE, "upgrade", f"{PR4_0019}:{PR7_0020}", "--sql").stdout)
    assert statements == GUARDS + [
        "ALTER TABLE portfolio_positions ADD COLUMN position_revision BIGINT DEFAULT 0 NOT NULL",
        f"ALTER TABLE portfolio_positions ADD CONSTRAINT {CHECK} CHECK (position_revision >= 0)",
        f"UPDATE alembic_version SET version_num='{PR7_0020}' WHERE alembic_version.version_num = '{PR4_0019}'",
    ]
    upper = " ".join(statements).upper()
    for forbidden in ("TRIGGER", "CREATE FUNCTION", "CREATE TYPE", "CREATE TABLE", "CREATE INDEX", "DROP", "UPDATE "
                      "PORTFOLIO_POSITIONS", "NUMERIC", "PORTFOLIO_WRITE_IDEMPOTENCY"):
        assert forbidden not in upper, forbidden


def test_offline_downgrade_locks_then_guards_at_execution_then_drops():
    statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{PR7_0020}:{PR4_0019}", "--sql").stdout)
    assert statements == GUARDS + [
        "LOCK TABLE portfolio_positions IN ACCESS EXCLUSIVE MODE",
        "LOCK TABLE portfolio_write_idempotency IN SHARE MODE",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM portfolio_write_idempotency UNION ALL SELECT 1 FROM portfolio_positions "
        "WHERE position_revision <> 0) THEN RAISE EXCEPTION 'R1 PR 7 : downgrade de 0020_position_revision refusé, "
        "des données versionnées l''exigent (reçus d''idempotence ou révisions de position non nulles). Aucune "
        "donnée supprimée.'; END IF; END $$",
        f"ALTER TABLE portfolio_positions DROP CONSTRAINT {CHECK}",
        "ALTER TABLE portfolio_positions DROP COLUMN position_revision",
        f"UPDATE alembic_version SET version_num='{PR4_0019}' WHERE alembic_version.version_num = '{PR7_0020}'",
    ]


def test_model_matches_the_migration():
    m = _m0020()
    positions = Base.metadata.tables["portfolio_positions"]
    column = positions.c.position_revision
    assert isinstance(column.type, sa.BigInteger) and not column.nullable
    assert str(column.server_default.arg) == "0"
    checks = {c.name: str(c.sqltext) for c in positions.constraints if isinstance(c, sa.CheckConstraint)}
    assert checks == {m.CHECK_NAME: m.CHECK}
    assert list(positions.c.keys()) == ["id", "user_id", "ticker", "quantity", "purchase_price", "target_percent",
                                        "envelope", "created_at", "updated_at", "position_revision"]
    for name in ("quantity", "purchase_price", "target_percent"):
        assert isinstance(positions.c[name].type, sa.Float)  # aucune conversion NUMERIC


# --------------------------------------------------------------------------
# 2. PostgreSQL réel
# --------------------------------------------------------------------------

def _positions(engine):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT id, user_id, ticker, float8send(quantity), float8send(purchase_price), float8send(target_percent), "
            "envelope, created_at, updated_at FROM portfolio_positions ORDER BY id"))]


def _relfilenode(engine):
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT relfilenode FROM pg_class WHERE oid = 'portfolio_positions'::regclass")) \
            .scalar_one()


def test_pg_upgrade_0019_to_0020_keeps_every_row_then_exact_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR4_0019)
    _seed_historical(pg_engine)
    tables = _tables(pg_engine)
    schema_0019, catalog_0019 = _snapshot(pg_engine, tables), _catalog(pg_engine, tables)
    global_0019 = _global_catalog(pg_engine)
    data_0019 = _data(pg_engine, tables - {"alembic_version"})
    positions_0019, node = _positions(pg_engine), _relfilenode(pg_engine)
    assert _compare_metadata(pg_engine) == []  # écart attendu (position_revision) retiré exactement

    _run_alembic(pg_url, "upgrade", PR7_0020)
    assert _version(pg_engine) == PR7_0020 and _compare_metadata(pg_engine) == []
    assert _positions(pg_engine) == positions_0019 and _relfilenode(pg_engine) == node  # aucune réécriture
    others = tables - {"alembic_version", "portfolio_positions"}
    assert _snapshot(pg_engine, others) == {t: schema_0019[t] for t in sorted(others)}
    assert _catalog(pg_engine, others) == {t: catalog_0019[t] for t in sorted(others)}
    assert _global_catalog(pg_engine) == global_0019
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT format_type(atttypid, atttypmod), attnotnull, pg_get_expr(adbin, adrelid) FROM pg_attribute "
            "JOIN pg_attrdef ON adrelid = attrelid AND adnum = attnum WHERE attrelid = 'portfolio_positions'::regclass "
            "AND attname = 'position_revision'")).one() == ("bigint", True, "0")
        assert conn.execute(sa.text("SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
                                    "WHERE conname = :c"), {"c": CHECK}).one() == (
            "CHECK ((position_revision >= 0))", True)
        assert set(conn.execute(sa.text("SELECT position_revision FROM portfolio_positions")).scalars()) == {0}
    data = _data(pg_engine, tables - {"alembic_version"})
    assert {t: r for t, r in data.items() if t != "portfolio_positions"} == {
        t: r for t, r in data_0019.items() if t != "portfolio_positions"}

    _run_alembic(pg_url, "downgrade", PR4_0019)
    assert _version(pg_engine) == PR4_0019
    assert _snapshot(pg_engine, tables) == schema_0019 and _catalog(pg_engine, tables) == catalog_0019
    assert _global_catalog(pg_engine) == global_0019
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0019
    _run_alembic(pg_url, "upgrade", "head")
    assert _compare_metadata(pg_engine) == []


def test_pg_downgrade_of_a_blank_database(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _run_alembic(pg_url, "downgrade", PR4_0019)
    assert _version(pg_engine) == PR4_0019 and _compare_metadata(pg_engine) == []


def test_pg_check_default_and_legacy_insert(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    with pg_engine.begin() as conn:
        assert_disposable_connection(conn)
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('legacy-anon-1', 'debutant')"))
        # Insertion historique (POST /api/user/{id}/portfolio) : sans la colonne.
        legacy = conn.execute(sa.text("INSERT INTO portfolio_positions (user_id, ticker, quantity) "
                                      "VALUES ('legacy-anon-1', 'MC.PA', 1) RETURNING position_revision")).scalar_one()
        assert legacy == 0
        top = conn.execute(sa.text("INSERT INTO portfolio_positions (user_id, ticker, quantity, position_revision) "
                                   "VALUES ('legacy-anon-1', 'MC.PA', 1, 9223372036854775807) "
                                   "RETURNING position_revision")).scalar_one()
        assert top == 2**63 - 1
        _rejects(conn, CHECK, "INSERT INTO portfolio_positions (user_id, ticker, quantity, position_revision) "
                              "VALUES ('legacy-anon-1', 'MC.PA', 1, -1)")
        with pytest.raises(sa.exc.DataError):
            with conn.begin_nested():
                conn.execute(sa.text("UPDATE portfolio_positions SET position_revision = position_revision + 1 "
                                     "WHERE position_revision = 9223372036854775807"))
        _rejects(conn, "null value", "INSERT INTO portfolio_positions (user_id, ticker, quantity, position_revision) "
                                     "VALUES ('legacy-anon-1', 'MC.PA', 1, NULL)")


@pytest.mark.parametrize("blocking", ["receipt", "revision"])
def test_pg_downgrade_is_refused_when_versioned_data_exists(pg_url, pg_engine, blocking):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    with pg_engine.begin() as conn:
        if blocking == "receipt":
            _receipt(conn, fingerprint="a" * 64, created_at=datetime(2026, 10, 26, tzinfo=timezone.utc))
        else:
            conn.execute(sa.text("UPDATE portfolio_positions SET position_revision = 1 WHERE ticker = 'AAPL'"))
    tables = _tables(pg_engine)
    before = (_snapshot(pg_engine, tables), _data(pg_engine, tables - {"alembic_version"}))
    with pytest.raises(Exception) as failure:
        _run_alembic(pg_url, "downgrade", PR4_0019)
    assert "downgrade de 0020_position_revision refusé" in str(getattr(failure.value, "stderr", failure.value))
    assert _version(pg_engine) == PR7_0020
    assert (_snapshot(pg_engine, tables), _data(pg_engine, tables - {"alembic_version"})) == before
    # La garde offline, évaluée à l'exécution, refuse de même.
    guard = next(s for s in _statements(_run_alembic(OFFLINE, "downgrade", f"{PR7_0020}:{PR4_0019}", "--sql").stdout)
                 if s.startswith("DO $$"))
    with pg_engine.connect() as conn:
        with pytest.raises(sa.exc.DBAPIError, match="refusé"):
            conn.exec_driver_sql(guard)


def test_pg_offline_guard_passes_on_unversioned_data(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    guard = next(s for s in _statements(_run_alembic(OFFLINE, "downgrade", f"{PR7_0020}:{PR4_0019}", "--sql").stdout)
                 if s.startswith("DO $$"))
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(guard)  # aucune exception


def test_pg_downgrade_waits_for_a_writer_and_fails_closed(pg_url, pg_engine):  # noqa: F811
    """Un writer qui détient un verrou sur les positions bloque le
    downgrade (ACCESS EXCLUSIVE) : lock_timeout de 5 s, échec, rien n'est
    modifié."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    with pg_engine.connect() as writer:
        tx = writer.begin()
        writer.execute(sa.text("SELECT id FROM portfolio_positions LIMIT 1 FOR UPDATE"))
        started = time.monotonic()
        with pytest.raises(Exception) as failure:
            _run_alembic(pg_url, "downgrade", PR4_0019)
        elapsed = time.monotonic() - started
        tx.rollback()
    assert "lock timeout" in str(getattr(failure.value, "stderr", failure.value)).lower()
    assert 4.5 <= elapsed < 14
    assert _version(pg_engine) == PR7_0020


def test_pg_downgrade_locks_exclude_concurrent_receipts_and_position_writes(pg_url, pg_engine):  # noqa: F811
    """Pendant le contrôle du downgrade (verrous de la migration détenus),
    aucune insertion de reçu ni écriture de position n'est admise."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    m = _m0020()
    with pg_engine.connect() as maintenance:
        tx = maintenance.begin()
        for lock in m.DOWNGRADE_LOCKS:
            maintenance.execute(sa.text(lock))
        assert maintenance.execute(sa.text(f"SELECT EXISTS ({m.BLOCKING})")).scalar_one() is False
        results = {}

        def attempt(name, sql):
            with pg_engine.connect() as conn:
                try:
                    with conn.begin():
                        conn.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
                        conn.execute(sa.text(sql))
                    results[name] = "written"
                except sa.exc.OperationalError as exc:
                    results[name] = "lock timeout" if "lock timeout" in str(exc) else str(exc)

        receipt_sql = ("INSERT INTO portfolio_write_idempotency (user_id, operation_type, operation_id, "
                       "fingerprint_version, fingerprint_key_version, request_fingerprint, result_version, "
                       "result_status, result_payload, position_id, created_at) VALUES ('legacy-anon-1', "
                       f"'portfolio_position_create', '{uuid.uuid4()}', 'v', 'k1', '{'b' * 64}', 'r', 201, "
                       "'{}'::jsonb, 1, now())")
        threads = [threading.Thread(target=attempt, args=("receipt", receipt_sql)),
                   threading.Thread(target=attempt, args=("position", "UPDATE portfolio_positions "
                                                                       "SET position_revision = 1"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        tx.rollback()
    assert results == {"receipt": "lock timeout", "position": "lock timeout"}
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM portfolio_write_idempotency")).scalar_one() == 0
        assert set(conn.execute(sa.text("SELECT position_revision FROM portfolio_positions")).scalars()) == {0}
