"""Tests de T1-A : migration 0002_analysis_sessions + modèle AnalysisSession.

Deux niveaux :

1. Tests sans base (toujours exécutés) : chaîne de révisions Alembic,
   intégrité de 0001, SQL PostgreSQL généré en mode offline, métadonnées
   SQLAlchemy du modèle AnalysisSession.

2. Tests contre un vrai PostgreSQL (upgrade/downgrade réels, types et
   contraintes lus dans le catalogue). Ils ne tournent que si
   ORYX_TEST_DATABASE_URL pointe vers une base DÉDIÉE aux tests, dont le
   nom contient "test" : son schéma public est entièrement détruit et
   recréé. Sans cette variable, ils sont SKIPPÉS (et apparaissent comme
   tels) : aucun comportement PostgreSQL n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t1a_test \\
           python -m pytest tests/test_migration_0002_analysis_sessions.py
"""
import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from core.db import Base, normalize_database_url
from core.models import AnalysisSession

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = "0001_current_oryx_baseline"
T1A = "0002_analysis_sessions"
HISTORICAL_TABLES = {
    "users",
    "portfolio_positions",
    "company_analyses",
    "investment_theses",
    "analysis_facts",
    "user_statements",
}
EXPECTED_COLUMNS = [
    "id",
    "user_id",
    "ticker",
    "status",
    "current_step",
    "started_at",
    "completed_at",
    "updated_at",
]
ALLOWED_STATUSES = {"in_progress", "completed", "abandoned"}
# sha256 de alembic/versions/0001_current_oryx_baseline.py tel que mergé
# sur main (7a11132, PR #171). La baseline ne doit plus jamais changer.
BASELINE_SHA256 = "cfd4f96af8d41a3164cac99903cefb044ac4a1a2e5ed19e7da92d1f5b4c33dc9"


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    """Lance Alembic dans un sous-processus : alembic/env.py lit
    core.db.DATABASE_URL à l'import, donc l'URL doit être dans
    l'environnement du processus qui exécute Alembic."""
    env = dict(os.environ, DATABASE_URL=database_url)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_baseline_then_analysis_sessions():
    script = _script_directory()
    assert script.get_heads() == [T1A]
    assert script.get_bases() == [BASELINE]

    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert set(revisions) == {BASELINE, T1A}
    assert revisions[T1A].down_revision == BASELINE
    assert revisions[BASELINE].down_revision is None


def test_baseline_file_is_unchanged():
    path = REPO_ROOT / "alembic" / "versions" / f"{BASELINE}.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == BASELINE_SHA256


def test_offline_sql_of_0002_only_creates_analysis_sessions():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "upgrade", f"{BASELINE}:{T1A}", "--sql",
    ).stdout
    normalized = " ".join(sql.split())

    assert "CREATE TABLE analysis_sessions" in normalized
    assert normalized.count("CREATE TABLE") == 1
    for forbidden in ("ALTER TABLE", "DROP ", "INSERT INTO", "CREATE INDEX",
                      "UNIQUE", "CREATE TYPE", "CREATE EXTENSION", "ON DELETE"):
        assert forbidden not in normalized, forbidden
    # Seule écriture hors DDL : le changement de version Alembic.
    assert "UPDATE alembic_version SET version_num='0002_analysis_sessions'" in normalized
    assert normalized.count("UPDATE ") == 1

    assert "id UUID NOT NULL" in normalized
    assert "user_id VARCHAR NOT NULL" in normalized
    assert "ticker VARCHAR NOT NULL" in normalized
    assert "status VARCHAR NOT NULL" in normalized
    assert "current_step VARCHAR," in normalized
    assert "started_at TIMESTAMP WITH TIME ZONE NOT NULL" in normalized
    assert "completed_at TIMESTAMP WITH TIME ZONE," in normalized
    assert "updated_at TIMESTAMP WITH TIME ZONE NOT NULL" in normalized
    assert "PRIMARY KEY (id)" in normalized
    assert "FOREIGN KEY(user_id) REFERENCES users (id)" in normalized
    assert (
        "CONSTRAINT ck_analysis_sessions_status CHECK "
        "(status IN ('in_progress', 'completed', 'abandoned'))"
    ) in normalized


def test_offline_sql_of_0002_downgrade_only_drops_analysis_sessions():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "downgrade", f"{T1A}:{BASELINE}", "--sql",
    ).stdout
    normalized = " ".join(sql.split())
    assert "DROP TABLE analysis_sessions" in normalized
    assert normalized.count("DROP ") == 1
    assert "ALTER TABLE" not in normalized


def test_model_columns_types_and_nullability():
    table = AnalysisSession.__table__
    assert table.name == "analysis_sessions"
    assert [c.name for c in table.columns] == EXPECTED_COLUMNS

    cols = table.columns
    assert isinstance(cols.id.type, sa.Uuid)
    assert cols.id.type.as_uuid is True
    assert cols.id.primary_key is True
    assert [c.name for c in table.primary_key.columns] == ["id"]
    assert cols.id.server_default is None
    assert cols.id.default is not None and cols.id.default.is_callable

    for name in ("user_id", "ticker", "status", "current_step"):
        assert type(cols[name].type) is sa.String, name
        assert cols[name].type.length is None, name

    for name in ("started_at", "completed_at", "updated_at"):
        assert isinstance(cols[name].type, sa.DateTime), name
        assert cols[name].type.timezone is True, name
        assert cols[name].server_default is None, name

    nullable = {c.name: c.nullable for c in table.columns}
    assert nullable == {
        "id": False,
        "user_id": False,
        "ticker": False,
        "status": False,
        "current_step": True,
        "started_at": False,
        "completed_at": True,
        "updated_at": False,
    }


def test_model_constraints():
    table = AnalysisSession.__table__

    fks = list(table.foreign_keys)
    assert len(fks) == 1
    assert fks[0].parent.name == "user_id"
    assert fks[0].target_fullname == "users.id"
    assert fks[0].ondelete is None
    assert fks[0].onupdate is None
    # Même type que users.id (VARCHAR historique, inchangé).
    assert type(Base.metadata.tables["users"].c.id.type) is sa.String

    checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]
    assert len(checks) == 1
    assert checks[0].name == "ck_analysis_sessions_status"
    assert str(checks[0].sqltext) == "status IN ('in_progress', 'completed', 'abandoned')"

    assert not [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
    assert not table.indexes
    assert not any(c.unique for c in table.columns)


def test_model_does_not_touch_historical_tables():
    assert set(Base.metadata.tables) == HISTORICAL_TABLES | {"analysis_sessions"}
    for name in HISTORICAL_TABLES:
        assert "analysis_session_id" not in Base.metadata.tables[name].c, name


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

_PG_URL = normalize_database_url(os.getenv("ORYX_TEST_DATABASE_URL", ""))


@pytest.fixture(scope="module")
def pg_url():
    if not _PG_URL:
        pytest.skip("ORYX_TEST_DATABASE_URL non défini : tests PostgreSQL réels non exécutés")
    url = sa.engine.make_url(_PG_URL)
    if url.get_backend_name() != "postgresql" or "test" not in (url.database or ""):
        pytest.fail("ORYX_TEST_DATABASE_URL doit viser une base PostgreSQL dédiée dont le nom contient 'test'")
    return _PG_URL


@pytest.fixture
def pg_engine(pg_url):
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    yield engine
    engine.dispose()


def _reset_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))


def _snapshot(engine, tables) -> dict:
    """Description complète (colonnes, PK, FK, UNIQUE, CHECK, index) des
    tables données, pour vérifier qu'une migration ne les modifie pas."""
    insp = sa.inspect(engine)
    snap = {}
    for t in sorted(tables):
        snap[t] = {
            "columns": [
                (c["name"], str(c["type"]), c["nullable"], c["default"])
                for c in insp.get_columns(t)
            ],
            "pk": insp.get_pk_constraint(t),
            "fks": insp.get_foreign_keys(t),
            "uniques": insp.get_unique_constraints(t),
            "checks": insp.get_check_constraints(t),
            "indexes": insp.get_indexes(t),
        }
    return snap


def _version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()


def _tables(engine) -> set:
    return set(sa.inspect(engine).get_table_names())


def test_pg_upgrade_from_empty_database_to_head(pg_url, pg_engine):
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == T1A
    assert _tables(pg_engine) == HISTORICAL_TABLES | {"analysis_sessions", "alembic_version"}


def test_pg_upgrade_0001_to_0002_then_downgrade(pg_url, pg_engine):
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", BASELINE)
    assert _version(pg_engine) == BASELINE
    assert "analysis_sessions" not in _tables(pg_engine)
    historical_before = _snapshot(pg_engine, HISTORICAL_TABLES)

    # --- upgrade 0001 -> 0002 -------------------------------------------
    _run_alembic(pg_url, "upgrade", T1A)
    assert _version(pg_engine) == T1A
    assert _tables(pg_engine) == HISTORICAL_TABLES | {"analysis_sessions", "alembic_version"}
    assert _snapshot(pg_engine, HISTORICAL_TABLES) == historical_before

    with pg_engine.connect() as conn:
        # Types réels du catalogue PostgreSQL.
        rows = conn.execute(sa.text(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t "
            "ORDER BY ordinal_position"
        ), {"t": "analysis_sessions"}).all()
        assert [tuple(r) for r in rows] == [
            ("id", "uuid", "NO", None),
            ("user_id", "character varying", "NO", None),
            ("ticker", "character varying", "NO", None),
            ("status", "character varying", "NO", None),
            ("current_step", "character varying", "YES", None),
            ("started_at", "timestamp with time zone", "NO", None),
            ("completed_at", "timestamp with time zone", "YES", None),
            ("updated_at", "timestamp with time zone", "NO", None),
        ]
        users_id_type = conn.execute(sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'id'"
        )).scalar_one()
        assert users_id_type == "character varying"

        # Contraintes : PK(id), FK(user_id) -> users(id) NO ACTION, 1 CHECK, rien d'autre.
        cons = conn.execute(sa.text(
            "SELECT conname, contype, pg_get_constraintdef(oid), confdeltype, confupdtype "
            "FROM pg_constraint WHERE conrelid = 'public.analysis_sessions'::regclass "
            "ORDER BY contype"
        )).all()
        by_type = {}
        for name, contype, definition, deltype, updtype in cons:
            by_type.setdefault(contype, []).append((name, definition, deltype, updtype))
        assert set(by_type) == {"c", "f", "p"}
        assert by_type["p"] == [("analysis_sessions_pkey", "PRIMARY KEY (id)", " ", " ")]
        assert by_type["f"] == [(
            "analysis_sessions_user_id_fkey",
            "FOREIGN KEY (user_id) REFERENCES users(id)",
            "a",  # ON DELETE NO ACTION : pas de cascade
            "a",  # ON UPDATE NO ACTION
        )]
        assert len(by_type["c"]) == 1
        check_name, check_def, _, _ = by_type["c"][0]
        assert check_name == "ck_analysis_sessions_status"
        for status in ALLOWED_STATUSES:
            assert f"'{status}'" in check_def

        # Seul index : celui de la PK. Aucun type ENUM créé.
        indexes = conn.execute(sa.text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'analysis_sessions'"
        )).scalars().all()
        assert indexes == ["analysis_sessions_pkey"]
        enums = conn.execute(sa.text("SELECT count(*) FROM pg_type WHERE typtype = 'e'")).scalar_one()
        assert enums == 0

    # Le modèle SQLAlchemy correspond à la base migrée (autogenerate vide).
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    with pg_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []

    # --- comportement des contraintes (transaction annulée) --------------
    with pg_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('u1', 'debutant')"))
            insert = sa.text(
                "INSERT INTO analysis_sessions "
                "(id, user_id, ticker, status, current_step, started_at, completed_at, updated_at) "
                "VALUES (:id, :user_id, 'AAPL', :status, NULL, now(), NULL, now())"
            )
            # Deux sessions pour le même (user_id, ticker) : autorisé.
            for status in sorted(ALLOWED_STATUSES):
                conn.execute(insert, {"id": uuid.uuid4(), "user_id": "u1", "status": status})
            count = conn.execute(sa.text(
                "SELECT count(*) FROM analysis_sessions WHERE user_id = 'u1' AND ticker = 'AAPL'"
            )).scalar_one()
            assert count == 3

            with pytest.raises(sa.exc.IntegrityError, match="ck_analysis_sessions_status"):
                with conn.begin_nested():
                    conn.execute(insert, {"id": uuid.uuid4(), "user_id": "u1", "status": "paused"})
            with pytest.raises(sa.exc.IntegrityError, match="analysis_sessions_user_id_fkey"):
                with conn.begin_nested():
                    conn.execute(insert, {"id": uuid.uuid4(), "user_id": "ghost", "status": "in_progress"})
            # Pas de cascade : supprimer un utilisateur ayant des sessions est refusé.
            with pytest.raises(sa.exc.IntegrityError, match="analysis_sessions_user_id_fkey"):
                with conn.begin_nested():
                    conn.execute(sa.text("DELETE FROM users WHERE id = 'u1'"))
        finally:
            trans.rollback()

    # Insertion via l'ORM : UUID et timestamps aware générés côté application.
    from sqlalchemy.orm import Session
    with Session(pg_engine) as session:
        with session.begin():
            session.execute(sa.text("INSERT INTO users (id, level) VALUES ('u2', 'debutant')"))
            obj = AnalysisSession(user_id="u2", ticker="MSFT", status="in_progress")
            session.add(obj)
            session.flush()
            session.refresh(obj)
            assert isinstance(obj.id, uuid.UUID)
            assert obj.started_at.tzinfo is not None
            assert obj.updated_at.tzinfo is not None
            assert obj.completed_at is None
            session.rollback()

    # --- downgrade 0002 -> 0001 ------------------------------------------
    _run_alembic(pg_url, "downgrade", BASELINE)
    assert _version(pg_engine) == BASELINE
    assert _tables(pg_engine) == HISTORICAL_TABLES | {"alembic_version"}
    assert _snapshot(pg_engine, HISTORICAL_TABLES) == historical_before

    # --- ré-upgrade : la migration est rejouable --------------------------
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == T1A
    assert _snapshot(pg_engine, HISTORICAL_TABLES) == historical_before
