"""Tests de R1-D1 : migration 0014_evaluation_run_leases + modèle
ObservationEvaluationRun étendu.

Même organisation que les tests des migrations 0002 à 0013 (dont on
réutilise les helpers) :

1. Tests sans base : chaîne Alembic (0014 unique tête après 0013),
   identifiant de révision <= 32 caractères, 0013 inchangée, SQL PostgreSQL
   généré en mode offline (upgrade et downgrade exacts), aucun backfill,
   modèle identique à la migration.

2. Tests contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL vers une base
   DÉDIÉE dont le nom contient "test" ; sinon SKIPPÉS) : upgrade 0013 -> 0014
   sur des runs historiques (NULL / NULL, données intactes, autres tables
   inchangées), CHECK de cohérence, downgrade exact puis ré-upgrade.

Doctrine : 0014 est strictement EXPAND et sans backfill ; la lease ne crée
aucun statut (execution_status / interpretation_status inchangés).
"""
import hashlib
import importlib.util
import uuid

import pytest
import sqlalchemy as sa

from core.models import ObservationEvaluationRun
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
    R1C4,
    R1C4_FILE,
    R1D1,
    R1D1_FILE,
    _code_tokens,
    _compare_metadata,
    _data,
    _statements,
    _without_r1d1_changes,
)
from tests.test_migration_0005_cognitive_support_traces import OTHER_USER, USER, _catalog, _global_catalog
from tests.test_migration_0006_observation_layer import RUN_COLUMNS
from tests.test_migration_0010_r1b_event_idempotence import _insert_event

MIGRATION_PATH = REPO_ROOT / "alembic" / "versions" / R1D1_FILE
# sha256 de alembic/versions/0013_decryptage_conversation_affinity.py tel que
# mergé sur main (52d027c, PR #216).
R1C4_SHA256 = "60f8d5a82e2249bf0c2149530f1314bbd0c26e3c7a60edcc435c08e5b1720055"
RUNS = "observation_evaluation_runs"
PAIR_CHECK = "ck_observation_evaluation_runs_lease_pair"
RUNNING_CHECK = "ck_observation_evaluation_runs_lease_running"
PAIR_SQL = "(lease_token IS NULL) = (lease_expires_at IS NULL)"
RUNNING_SQL = "lease_token IS NULL OR execution_status = 'running'"


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_ends_with_0014_as_the_single_head():
    script = _script_directory()
    assert script.get_heads() == [R1D1]
    assert script.get_revision(R1D1).down_revision == R1C4
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[-2:] == [R1C4_FILE, R1D1_FILE] and len(files) == 14


def test_revision_id_fits_alembic_version_and_names_the_file():
    assert R1D1_FILE == f"{R1D1}.py" and len(R1D1) <= 32
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert f'revision: str = "{R1D1}"' in source
    assert f'down_revision: Union[str, Sequence[str], None] = "{R1C4}"' in source


def test_0013_file_is_unchanged():
    versions = REPO_ROOT / "alembic" / "versions"
    assert hashlib.sha256((versions / R1C4_FILE).read_bytes()).hexdigest() == R1C4_SHA256


def test_offline_sql_of_0014_upgrade_only_adds_the_lease():
    sql = _run_alembic("postgresql://offline@localhost/offline", "upgrade", f"{R1C4}:{R1D1}", "--sql").stdout
    statements = _statements(sql)
    assert statements == [
        f"ALTER TABLE {RUNS} ADD COLUMN lease_token UUID",
        f"ALTER TABLE {RUNS} ADD COLUMN lease_expires_at TIMESTAMP WITH TIME ZONE",
        f"ALTER TABLE {RUNS} ADD CONSTRAINT {PAIR_CHECK} CHECK ({PAIR_SQL})",
        f"ALTER TABLE {RUNS} ADD CONSTRAINT {RUNNING_CHECK} CHECK ({RUNNING_SQL})",
        f"UPDATE alembic_version SET version_num='{R1D1}' WHERE alembic_version.version_num = '{R1C4}'",
    ]
    upper = sql.upper()
    for forbidden in ("INSERT", "DELETE", "DROP", "RENAME", "CREATE TABLE", "CREATE INDEX", "CREATE TYPE",
                      "CREATE TRIGGER", "CREATE FUNCTION", "DEFAULT", "NOT NULL", "NOT VALID", "CASCADE"):
        assert forbidden not in upper, forbidden


def test_offline_sql_of_0014_downgrade_reverts_exactly():
    sql = _run_alembic("postgresql://offline@localhost/offline", "downgrade", f"{R1D1}:{R1C4}", "--sql").stdout
    assert _statements(sql) == [
        f"ALTER TABLE {RUNS} DROP CONSTRAINT {RUNNING_CHECK}",
        f"ALTER TABLE {RUNS} DROP CONSTRAINT {PAIR_CHECK}",
        f"ALTER TABLE {RUNS} DROP COLUMN lease_expires_at",
        f"ALTER TABLE {RUNS} DROP COLUMN lease_token",
        f"UPDATE alembic_version SET version_num='{R1C4}' WHERE alembic_version.version_num = '{R1D1}'",
    ]
    assert "CASCADE" not in sql.upper()


def test_upgrade_writes_no_data_and_creates_no_status_or_worker_column():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade = source[source.index("def upgrade"):source.index("def downgrade")]
    tokens = set(_code_tokens(upgrade).split("\n"))
    for forbidden in ("execute", "bulk_insert", "server_default", "inline_literal", "drop_column", "alter_column",
                      "create_table", "create_index"):
        assert forbidden not in tokens, forbidden
    for word in ("worker_id", "heartbeat", "attempt_count", "leased"):
        assert word not in _code_tokens(source), word


def test_model_matches_the_migration_exactly():
    spec = importlib.util.spec_from_file_location("migration_0014", MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    table = ObservationEvaluationRun.__table__
    assert [c.name for c in table.columns] == RUN_COLUMNS + ["lease_token", "lease_expires_at"]
    token, expires = table.c.lease_token, table.c.lease_expires_at
    assert token.nullable and isinstance(token.type, sa.Uuid) and not token.foreign_keys
    assert expires.nullable and isinstance(expires.type, sa.DateTime) and expires.type.timezone is True
    assert token.server_default is None and expires.server_default is None
    assert token.default is None and expires.default is None
    checks = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert checks[PAIR_CHECK] == migration.LEASE_PAIR == PAIR_SQL
    assert checks[RUNNING_CHECK] == migration.LEASE_RUNNING == RUNNING_SQL
    # Aucun statut « leased » : les vocabulaires de statut sont inchangés.
    assert "leased" not in checks["ck_observation_evaluation_runs_execution_status"]
    assert "leased" not in checks["ck_observation_evaluation_runs_interpretation_status"]


# --------------------------------------------------------------------------
# 2. PostgreSQL
# --------------------------------------------------------------------------

NOW = "2026-10-07T12:00:00+00:00"


def _seed(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:a, 'debutant'), (:b, 'debutant')"),
                     {"a": USER, "b": OTHER_USER})
        event = _insert_event(conn, status="finalized", event_dedup_key="e" * 64)
        rows = []
        for status, interpretation in (("running", "candidate"), ("completed", "active"), ("failed", "obsolete")):
            run_id = uuid.uuid4()
            conn.execute(sa.text(
                f"INSERT INTO {RUNS} (id, event_id, execution_status, interpretation_status, trigger, "
                "evaluation_dedup_key, normalization_version, local_stage_version, capability_mapping_version, "
                "evaluation_schema_version, evaluator_version, input_fingerprint, started_at, created_at) VALUES "
                "(:id, :e, :s, :i, 'initial', :k, 'n', 'l', 'c', 's', 'v', 'f', :now, :now)"),
                {"id": run_id, "e": event, "s": status, "i": interpretation, "k": f"k-{run_id}", "now": NOW})
            rows.append(run_id)
        return rows


def test_pg_upgrade_0013_to_0014_keeps_historical_runs_then_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1C4)
    running, completed, failed = _seed(pg_engine)
    tables = _tables(pg_engine)
    others = tables - {RUNS, "alembic_version"}
    schema_0013 = _snapshot(pg_engine, tables)
    catalog_0013 = _catalog(pg_engine, tables)
    global_0013 = _global_catalog(pg_engine)
    data_0013 = _data(pg_engine, tables - {"alembic_version"})

    # --- upgrade 0013 -> 0014 --------------------------------------------
    _run_alembic(pg_url, "upgrade", R1D1)
    assert _version(pg_engine) == R1D1 and _tables(pg_engine) == tables
    assert _snapshot(pg_engine, others) == {t: schema_0013[t] for t in sorted(others)}
    assert _catalog(pg_engine, others) == {t: catalog_0013[t] for t in sorted(others)}
    assert _global_catalog(pg_engine) == global_0013
    data_0014 = _data(pg_engine, tables - {"alembic_version"})
    assert {t: r for t, r in data_0014.items() if t != RUNS} == {t: r for t, r in data_0013.items() if t != RUNS}
    # Runs historiques : mêmes valeurs, lease NULL / NULL (aucun backfill).
    assert data_0014[RUNS] == [row + (None, None) for row in data_0013[RUNS]]
    assert _compare_metadata(pg_engine) == []
    catalog = _catalog(pg_engine, {RUNS})[RUNS]
    assert catalog["columns"][-2:] == [("lease_token", "uuid", "YES", None),
                                       ("lease_expires_at", "timestamp with time zone", "YES", None)]
    assert [c[0] for c in catalog["columns"][:-2]] == RUN_COLUMNS
    assert catalog["indexes"] == catalog_0013[RUNS]["indexes"]
    schema_0014 = _snapshot(pg_engine, {RUNS})

    # --- downgrade 0014 -> 0013 ------------------------------------------
    _run_alembic(pg_url, "downgrade", R1C4)
    assert _version(pg_engine) == R1C4
    assert _snapshot(pg_engine, tables) == schema_0013
    assert _catalog(pg_engine, tables) == catalog_0013
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0013
    assert _without_r1d1_changes(_compare_metadata(pg_engine)) == []

    # --- ré-upgrade ------------------------------------------------------
    _run_alembic(pg_url, "upgrade", R1D1)
    assert _snapshot(pg_engine, {RUNS}) == schema_0014


@pytest.fixture
def runs(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    return _seed(pg_engine)


def _try(pg_engine, statement, **params):
    with pg_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(sa.text(statement), params)
        finally:
            trans.rollback()


def test_pg_lease_pair_and_running_checks(pg_engine, runs):  # noqa: F811
    running, completed, failed = runs
    token = uuid.uuid4()
    # Valide : lease complète sur un run running.
    _try(pg_engine, f"UPDATE {RUNS} SET lease_token = :t, lease_expires_at = now() WHERE id = :id", t=token,
         id=running)
    for statement, run_id in (
        (f"UPDATE {RUNS} SET lease_token = :t WHERE id = :id", running),
        (f"UPDATE {RUNS} SET lease_expires_at = now() WHERE id = :id", running),
        (f"UPDATE {RUNS} SET lease_token = :t, lease_expires_at = now() WHERE id = :id", completed),
        (f"UPDATE {RUNS} SET lease_token = :t, lease_expires_at = now() WHERE id = :id", failed),
    ):
        with pytest.raises(sa.exc.IntegrityError, match="ck_observation_evaluation_runs_lease"):
            _try(pg_engine, statement, t=token, id=run_id)


def test_pg_completing_a_leased_run_without_clearing_it_is_refused(pg_engine, runs):  # noqa: F811
    running = runs[0]
    with pytest.raises(sa.exc.IntegrityError, match=RUNNING_CHECK):
        _try(pg_engine, f"UPDATE {RUNS} SET lease_token = :t, lease_expires_at = now(), "
             "execution_status = 'completed', interpretation_status = 'active' WHERE id = :id",
             t=uuid.uuid4(), id=running)
