"""Tests de R1 PR 1 : migration 0016_portfolio_idempotency + modèle
PortfolioWriteIdempotency.

1. Sans base : maillon 0015 -> 0016, identifiant, SQL offline (une seule
   table, aucun index, aucune purge), modèle.

2. PostgreSQL réel (base jetable protégée ; sinon SKIPPÉS) : upgrade
   0015 -> 0016 sur données historiques, clé (user_id, operation_type,
   operation_id), CHECK, conservation du reçu après suppression de la
   position, transactions concurrentes sur la même clé, downgrade exact.
"""
import json
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from core.models import PortfolioWriteIdempotency
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
    PR1_0015,
    PR1_0016,
    PR1_0016_FILE,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import _catalog, _global_catalog
from tests.test_migration_0015_users_level_revision import _seed_historical

MIGRATION_PATH = REPO_ROOT / "alembic" / "versions" / PR1_0016_FILE
TABLE = "portfolio_write_idempotency"
COLUMNS = [
    "user_id", "operation_type", "operation_id", "fingerprint_version", "fingerprint_key_version",
    "request_fingerprint", "result_version", "result_status", "result_payload", "position_id", "created_at",
]
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
FP_A = "a" * 64
FP_B = "b" * 64


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_links_0016_to_0015():
    script = _script_directory()
    assert script.get_revision(PR1_0016).down_revision == PR1_0015
    assert PR1_0016_FILE == f"{PR1_0016}.py" and len(PR1_0016) <= 32
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert f'revision: str = "{PR1_0016}"' in source
    assert f'down_revision: Union[str, Sequence[str], None] = "{PR1_0015}"' in source


def test_offline_sql_of_0016_creates_only_the_receipt_table():
    sql = _run_alembic("postgresql://offline@localhost/offline", "upgrade", f"{PR1_0015}:{PR1_0016}", "--sql").stdout
    statements = _statements(sql)
    assert len(statements) == 2
    create, version = statements
    assert version == (f"UPDATE alembic_version SET version_num='{PR1_0016}' "
                       f"WHERE alembic_version.version_num = '{PR1_0015}'")
    assert create.startswith(f"CREATE TABLE {TABLE} (")
    for fragment in (
        "user_id VARCHAR NOT NULL", "operation_type VARCHAR NOT NULL", "operation_id UUID NOT NULL",
        "fingerprint_version VARCHAR NOT NULL", "fingerprint_key_version VARCHAR NOT NULL",
        "request_fingerprint VARCHAR NOT NULL", "result_version VARCHAR NOT NULL",
        "result_status SMALLINT NOT NULL", "result_payload JSONB NOT NULL", "position_id INTEGER NOT NULL",
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL",
        "PRIMARY KEY (user_id, operation_type, operation_id)",
        "FOREIGN KEY(user_id) REFERENCES users (id)",
        "CONSTRAINT ck_portfolio_write_idempotency_operation_type CHECK (operation_type IN "
        "('portfolio_position_create', 'portfolio_position_update', 'portfolio_position_delete'))",
        "CONSTRAINT ck_portfolio_write_idempotency_request_fingerprint_format CHECK "
        "(request_fingerprint ~ '^[0-9a-f]{64}$')",
        "CONSTRAINT ck_portfolio_write_idempotency_result_status CHECK (result_status BETWEEN 200 AND 299)",
    ):
        assert fragment in create, fragment
    upper = sql.upper()
    for forbidden in ("EXPIRES", "PURGE", "CREATE INDEX", "UNIQUE", "DEFAULT", "CASCADE", "INSERT INTO",
                      "DELETE FROM", "TRIGGER", "FUNCTION", "PORTFOLIO_POSITIONS"):
        assert forbidden not in upper, forbidden


def test_offline_sql_of_0016_downgrade_drops_only_the_receipt_table():
    sql = _run_alembic("postgresql://offline@localhost/offline", "downgrade", f"{PR1_0016}:{PR1_0015}", "--sql").stdout
    assert _statements(sql) == [
        f"DROP TABLE {TABLE}",
        f"UPDATE alembic_version SET version_num='{PR1_0015}' WHERE alembic_version.version_num = '{PR1_0016}'",
    ]


def test_model_key_and_durable_receipt():
    table = PortfolioWriteIdempotency.__table__
    assert [c.name for c in table.columns] == COLUMNS
    assert [c.name for c in table.primary_key.columns] == ["user_id", "operation_type", "operation_id"]
    assert isinstance(table.c.operation_id.type, sa.Uuid)
    assert not table.indexes
    assert "expires_at" not in table.c
    assert not table.c.position_id.foreign_keys
    assert all(not c.nullable for c in table.columns)
    assert all(c.server_default is None for c in table.columns)


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

def _receipt(conn, *, user="legacy-anon-1", operation_type="portfolio_position_create", operation_id=None,
             fingerprint=FP_A, position_id=1, status=201, **overrides):
    values = {
        "user_id": user, "operation_type": operation_type, "operation_id": operation_id or uuid.uuid4(),
        "fingerprint_version": "canon-v1", "fingerprint_key_version": "k1", "request_fingerprint": fingerprint,
        "result_version": "result-v1", "result_status": status,
        "result_payload": json.dumps({"position_id": position_id}), "position_id": position_id, "created_at": NOW,
    }
    values.update(overrides)
    conn.execute(sa.text(
        f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES (:user_id, :operation_type, :operation_id, "
        ":fingerprint_version, :fingerprint_key_version, :request_fingerprint, :result_version, :result_status, "
        "CAST(:result_payload AS JSONB), :position_id, :created_at)"), values)
    return values["operation_id"]


def _rejects(conn, match, statement, **params):
    with pytest.raises(sa.exc.IntegrityError, match=match):
        with conn.begin_nested():
            if callable(statement):
                statement()
            else:
                conn.execute(sa.text(statement), params)


def test_pg_upgrade_0015_to_0016_keeps_data_then_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR1_0015)
    _seed_historical(pg_engine)
    tables = _tables(pg_engine)
    schema_0015 = _snapshot(pg_engine, tables)
    catalog_0015 = _catalog(pg_engine, tables)
    global_0015 = _global_catalog(pg_engine)
    data_0015 = _data(pg_engine, tables - {"alembic_version"})

    _run_alembic(pg_url, "upgrade", PR1_0016)
    assert _version(pg_engine) == PR1_0016 and _tables(pg_engine) == tables | {TABLE}
    assert _snapshot(pg_engine, tables - {"alembic_version"}) == {
        t: schema_0015[t] for t in sorted(tables - {"alembic_version"})}
    assert _catalog(pg_engine, tables) == catalog_0015
    assert _global_catalog(pg_engine) == global_0015
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0015
    assert _data(pg_engine, {TABLE}) == {TABLE: []}
    catalog = _catalog(pg_engine, {TABLE})[TABLE]
    assert [c[0] for c in catalog["columns"]] == COLUMNS
    assert all(c[2] == "NO" and c[3] is None for c in catalog["columns"])
    assert catalog["indexes"] == ["portfolio_write_idempotency_pkey"]
    fks = [c for c in catalog["constraints"] if c[1] == "f"]
    assert fks == [("portfolio_write_idempotency_user_id_fkey", "f", "FOREIGN KEY (user_id) REFERENCES users(id)",
                    "a", "a")]

    _run_alembic(pg_url, "downgrade", PR1_0015)
    assert _tables(pg_engine) == tables
    assert _snapshot(pg_engine, tables) == schema_0015
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0015
    _run_alembic(pg_url, "upgrade", PR1_0016)
    assert _version(pg_engine) == PR1_0016


@pytest.fixture
def conn(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()


def test_pg_idempotency_key_is_user_type_operation(conn):
    op_id = _receipt(conn)
    # Même triplet : refusé (le service rejoue le reçu ou répond 409).
    _rejects(conn, "portfolio_write_idempotency_pkey", lambda: _receipt(conn, operation_id=op_id))
    _rejects(conn, "portfolio_write_idempotency_pkey", lambda: _receipt(conn, operation_id=op_id, fingerprint=FP_B))
    # Même operation_id : autre type ou autre utilisateur = autre clé.
    _receipt(conn, operation_id=op_id, operation_type="portfolio_position_delete")
    _receipt(conn, operation_id=op_id, user="legacy-anon-2")
    assert conn.execute(sa.text(f"SELECT count(*) FROM {TABLE} WHERE operation_id = :o"),
                        {"o": op_id}).scalar_one() == 3
    # Aucune déduplication sur les valeurs : deux achats identiques coexistent.
    _receipt(conn, fingerprint=FP_A)
    _receipt(conn, fingerprint=FP_A)


def test_pg_receipt_checks(conn):
    _rejects(conn, "ck_portfolio_write_idempotency_operation_type",
             lambda: _receipt(conn, operation_type="portfolio_position_merge"))
    _rejects(conn, "ck_portfolio_write_idempotency_request_fingerprint_format",
             lambda: _receipt(conn, fingerprint="A" * 64))
    _rejects(conn, "ck_portfolio_write_idempotency_request_fingerprint_format",
             lambda: _receipt(conn, fingerprint="a" * 63))
    _rejects(conn, "ck_portfolio_write_idempotency_result_status", lambda: _receipt(conn, status=409))
    _rejects(conn, "portfolio_write_idempotency_user_id_fkey", lambda: _receipt(conn, user="unknown-user"))
    _rejects(conn, "not-null|null value", lambda: _receipt(conn, result_payload=None))


def test_pg_receipt_survives_position_deletion(conn):
    position_id = conn.execute(sa.text(
        "INSERT INTO portfolio_positions (user_id, ticker, quantity) VALUES ('user_7f3a', 'AIR.PA', 4) RETURNING id"
    )).scalar_one()
    create_id = _receipt(conn, user="user_7f3a", position_id=position_id)
    delete_id = _receipt(conn, user="user_7f3a", operation_type="portfolio_position_delete", position_id=position_id,
                         status=200)
    conn.execute(sa.text("DELETE FROM portfolio_positions WHERE id = :id"), {"id": position_id})
    rows = conn.execute(sa.text(
        f"SELECT operation_id, position_id, result_payload FROM {TABLE} WHERE user_id = 'user_7f3a' "
        "ORDER BY operation_type")).all()
    assert [(r[0], r[1]) for r in rows] == [(create_id, position_id), (delete_id, position_id)]
    assert rows[0][2] == {"position_id": position_id}


def _wait_for_lock_waiter(engine, timeout=10.0) -> None:
    deadline = time.monotonic() + timeout
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            waiting = probe.execute(sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock'")).scalar_one()
            if waiting:
                return
            time.sleep(0.05)
    raise AssertionError("la seconde transaction n'a jamais attendu le verrou")


def _concurrent_same_key(pg_engine, finish_first):
    op_id = uuid.uuid4()
    outcome = {}
    first = pg_engine.connect()
    first_trans = first.begin()
    _receipt(first, user="user_7f3a", operation_id=op_id)

    def second():
        try:
            with pg_engine.begin() as other:
                other.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
                _receipt(other, user="user_7f3a", operation_id=op_id, fingerprint=FP_B)
            outcome["result"] = "inserted"
        except sa.exc.IntegrityError as exc:
            outcome["result"] = "conflict"
            outcome["error"] = str(exc.orig)

    thread = threading.Thread(target=second)
    thread.start()
    try:
        _wait_for_lock_waiter(pg_engine)
        finish_first(first_trans)
    finally:
        first.close()
        thread.join(30)
    assert not thread.is_alive()
    with pg_engine.connect() as check:
        rows = check.execute(sa.text(f"SELECT request_fingerprint FROM {TABLE} WHERE operation_id = :o"),
                             {"o": op_id}).scalars().all()
    return outcome, rows


@pytest.fixture
def committed_head(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    return pg_engine


def test_pg_concurrent_same_key_waits_then_conflicts(committed_head):
    outcome, rows = _concurrent_same_key(committed_head, lambda trans: trans.commit())
    assert outcome["result"] == "conflict" and "portfolio_write_idempotency_pkey" in outcome["error"]
    assert rows == [FP_A]


def test_pg_concurrent_same_key_after_rollback_inserts_once(committed_head):
    outcome, rows = _concurrent_same_key(committed_head, lambda trans: trans.rollback())
    assert outcome == {"result": "inserted"}
    assert rows == [FP_B]
