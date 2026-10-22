"""Tests de R1 PR 1 : migration 0015_users_level_revision + User.level_revision.

1. Sans base : chaîne Alembic (0015 après 0014, tête unique 0017),
   identifiant <= 32 caractères, 0014 inchangée, SQL offline exact (upgrade
   et downgrade), modèle User.

2. PostgreSQL réel (ORYX_TEST_DATABASE_URL, base jetable protégée par
   tests/pg_guard.py ; sinon SKIPPÉS) : upgrade 0014 -> 0015 sur des données
   historiques représentatives (aucune donnée modifiée, colonne à 0),
   compatibilité de l'ancien backend (INSERT / UPDATE sans la colonne, ORM
   historique), CHECK >= 0, downgrade exact puis ré-upgrade.

3. Garde-fous de verrouillage (PostgreSQL réel, mêmes conditions) : délais
   posés dans la seule transaction de migration, échec complet par
   lock_timeout sans état partiel (0015 à 0017), libération du verrou de
   users quand le client se tait (idle_in_transaction_session_timeout).
"""
import hashlib
import importlib.util
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session, declarative_base

from core.models import User
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
    PR2_0018,
    PR1_0015,
    PR1_0015_FILE,
    PR1_0016,
    PR1_0017,
    R1D1,
    R1D1_FILE,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import _catalog, _global_catalog
from tests.test_migration_0010_r1b_event_idempotence import _insert_event

MIGRATION_PATH = REPO_ROOT / "alembic" / "versions" / PR1_0015_FILE
# sha256 de alembic/versions/0014_evaluation_run_leases.py tel que mergé sur
# main (108e65f, PR #219).
R1D1_SHA256 = "38ec8a38e9720ba84f0c4ff5b1ac055a6f8e7977fb0fe0b20e7cdd7215e4c241"
CHECK_NAME = "ck_users_level_revision_non_negative"
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
LEGACY_USERS = ("legacy-anon-1", "legacy-anon-2", "user_7f3a")
# Garde-fous de verrouillage attendus, dans l'ordre (attente, exécution,
# conservation du verrou).
LOCK_GUARDS = [
    "SET LOCAL lock_timeout = '5s'",
    "SET LOCAL statement_timeout = '15s'",
    "SET LOCAL idle_in_transaction_session_timeout = '15s'",
]
GUARD_SETTINGS = {"lock_timeout": "5s", "statement_timeout": "15s", "idle_in_transaction_session_timeout": "15s"}


def _load_0015():
    spec = importlib.util.spec_from_file_location("migration_0015_under_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_puts_0015_after_0014_and_head_is_0017():
    script = _script_directory()
    assert script.get_heads() == [PR2_0018]
    assert script.get_revision(PR1_0015).down_revision == R1D1
    assert script.get_revision(PR1_0016).down_revision == PR1_0015
    assert script.get_revision(PR1_0017).down_revision == PR1_0016


def test_revision_id_fits_alembic_version_and_names_the_file():
    assert PR1_0015_FILE == f"{PR1_0015}.py" and len(PR1_0015) <= 32
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert f'revision: str = "{PR1_0015}"' in source
    assert f'down_revision: Union[str, Sequence[str], None] = "{R1D1}"' in source


def test_0014_file_is_unchanged():
    versions = REPO_ROOT / "alembic" / "versions"
    assert hashlib.sha256((versions / R1D1_FILE).read_bytes()).hexdigest() == R1D1_SHA256


def test_offline_sql_of_0015_upgrade_only_adds_level_revision():
    sql = _run_alembic("postgresql://offline@localhost/offline", "upgrade", f"{R1D1}:{PR1_0015}", "--sql").stdout
    assert _statements(sql) == [
        *LOCK_GUARDS,
        "ALTER TABLE users ADD COLUMN level_revision BIGINT DEFAULT 0 NOT NULL",
        f"ALTER TABLE users ADD CONSTRAINT {CHECK_NAME} CHECK (level_revision >= 0)",
        f"UPDATE alembic_version SET version_num='{PR1_0015}' WHERE alembic_version.version_num = '{R1D1}'",
    ]
    upper = sql.upper()
    for forbidden in ("INSERT", "DELETE", "DROP", "RENAME", "CREATE ", "TRIGGER", "FUNCTION", "CASCADE",
                      "NOT VALID"):
        assert forbidden not in upper, forbidden


def test_offline_sql_of_0015_downgrade_reverts_exactly():
    sql = _run_alembic("postgresql://offline@localhost/offline", "downgrade", f"{PR1_0015}:{R1D1}", "--sql").stdout
    assert _statements(sql) == [
        *LOCK_GUARDS,
        f"ALTER TABLE users DROP CONSTRAINT {CHECK_NAME}",
        "ALTER TABLE users DROP COLUMN level_revision",
        f"UPDATE alembic_version SET version_num='{R1D1}' WHERE alembic_version.version_num = '{PR1_0015}'",
    ]
    assert "CASCADE" not in sql.upper()


def test_lock_guards_are_transaction_local_and_never_persisted():
    """Les garde-fous sont des SET LOCAL : rien n'est écrit dans un rôle,
    une base ou la configuration du serveur (le SQL offline exact de
    l'upgrade et du downgrade ne contient aucune autre instruction)."""
    module = _load_0015()
    assert list(module.LOCK_GUARDS) == LOCK_GUARDS
    assert all(guard.startswith("SET LOCAL ") for guard in module.LOCK_GUARDS)
    sql = _run_alembic("postgresql://offline@localhost/offline", "upgrade", f"{R1D1}:{PR1_0015}", "--sql").stdout
    upper = sql.upper()
    for forbidden in ("ALTER ROLE", "ALTER DATABASE", "ALTER SYSTEM", "SET SESSION", "SET STATEMENT_TIMEOUT"):
        assert forbidden not in upper, forbidden


def test_model_level_revision_is_a_server_default_only():
    column = User.__table__.c.level_revision
    assert type(column.type) is sa.BigInteger
    assert column.nullable is False
    assert column.default is None
    assert str(column.server_default.arg.text) == "0"
    assert [c.name for c in User.__table__.columns] == ["id", "level", "created_at", "level_revision"]
    checks = {c.name: str(c.sqltext) for c in User.__table__.constraints if isinstance(c, sa.CheckConstraint)}
    assert checks == {CHECK_NAME: "level_revision >= 0"}


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

def _seed_historical(engine) -> None:
    """Données historiques représentatives (schéma 0014) : utilisateurs
    anonymes historiques, portefeuilles (valeurs NULL comprises), thèses,
    faits, déclarations, session d'analyse, conversation et événement
    cognitif. Aucune n'a d'équivalent dans les nouvelles tables."""
    session_id = uuid.uuid4()
    with engine.begin() as conn:
        for index, user_id in enumerate(LEGACY_USERS):
            conn.execute(sa.text("INSERT INTO users (id, level, created_at) VALUES (:id, :level, :at)"),
                         {"id": user_id, "level": ("debutant", "intermediaire", "avance")[index],
                          "at": datetime(2026, 1, 1 + index, 8, 30)})
        conn.execute(sa.text(
            "INSERT INTO portfolio_positions (user_id, ticker, quantity, purchase_price, target_percent, envelope, "
            "created_at, updated_at) VALUES "
            "('legacy-anon-1', 'MC.PA', 3.5, 612.4, 20, 'PEA', '2026-02-01 10:00', '2026-02-02 10:00'), "
            "('legacy-anon-1', 'AAPL', 10, NULL, NULL, NULL, '2026-02-03 10:00', NULL), "
            "('user_7f3a', 'TTE.PA', 42, 58.1, 15.5, 'CTO', '2026-03-01 09:00', '2026-03-01 09:00')"
        ))
        conn.execute(sa.text(
            "INSERT INTO analysis_sessions (id, user_id, ticker, status, current_step, started_at, updated_at) "
            "VALUES (:id, 'legacy-anon-1', 'MC.PA', 'in_progress', 'risques', :now, :now)"
        ), {"id": session_id, "now": NOW})
        conn.execute(sa.text(
            "INSERT INTO investment_theses (user_id, ticker, thesis_text, created_at, analysis_session_id) VALUES "
            "('legacy-anon-1', 'MC.PA', 'Pricing power durable', '2026-02-05 11:00', NULL), "
            "('legacy-anon-1', 'MC.PA', 'Thèse révisée', '2026-02-06 11:00', :s)"
        ), {"s": session_id})
        conn.execute(sa.text(
            "INSERT INTO analysis_facts (user_id, ticker, fact_date, fact_type, fact_value) VALUES "
            "('legacy-anon-2', 'AAPL', '2026-02-07 12:00', 'per', 28.4)"
        ))
        conn.execute(sa.text(
            "INSERT INTO user_statements (user_id, ticker, step, statement_date, statement_text) VALUES "
            "('legacy-anon-2', 'AAPL', 'moat', '2026-02-08 12:00', 'Écosystème fermé')"
        ))
        conn.execute(sa.text(
            "INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
            "VALUES ('conv-legacy-1', 'legacy-anon-1', :now)"
        ), {"now": NOW})
        _insert_event(conn, user_id="legacy-anon-1", status="finalized", event_dedup_key="d" * 64)


def _legacy_orm():
    """Modèles de l'ANCIEN backend (avant PR 1) : users sans level_revision."""
    legacy = declarative_base()

    class LegacyUser(legacy):
        __tablename__ = "users"
        id = sa.Column(sa.String, primary_key=True)
        level = sa.Column(sa.String, nullable=False, default="debutant")
        created_at = sa.Column(sa.DateTime, default=datetime.utcnow)

    class LegacyPosition(legacy):
        __tablename__ = "portfolio_positions"
        id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
        user_id = sa.Column(sa.String, sa.ForeignKey("users.id"), nullable=False)
        ticker = sa.Column(sa.String, nullable=False)
        quantity = sa.Column(sa.Float, nullable=False)
        purchase_price = sa.Column(sa.Float, nullable=True)
        target_percent = sa.Column(sa.Float, nullable=True)
        envelope = sa.Column(sa.String, nullable=True)
        created_at = sa.Column(sa.DateTime, default=datetime.utcnow)
        updated_at = sa.Column(sa.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    return LegacyUser, LegacyPosition


def _exercise_legacy_backend(engine) -> None:
    """Rejoue, avec les modèles historiques, ce que font les anciennes
    routes : création implicite d'un utilisateur, changement de niveau,
    ajout / modification / suppression de position."""
    LegacyUser, LegacyPosition = _legacy_orm()
    with Session(engine) as db:
        db.add(LegacyUser(id="legacy-new", level="debutant"))
        db.commit()
        user = db.get(LegacyUser, "legacy-new")
        user.level = "avance"
        position = LegacyPosition(user_id="legacy-new", ticker="AIR.PA", quantity=2, envelope="PEA")
        db.add(position)
        db.commit()
        position.quantity = 5
        db.commit()
        db.delete(position)
        db.commit()


def test_pg_upgrade_0014_to_0015_keeps_data_and_legacy_backend_then_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    _seed_historical(pg_engine)
    tables = _tables(pg_engine)
    others = tables - {"users", "alembic_version"}
    schema_0014 = _snapshot(pg_engine, tables)
    catalog_0014 = _catalog(pg_engine, tables)
    global_0014 = _global_catalog(pg_engine)
    data_0014 = _data(pg_engine, tables - {"alembic_version"})

    # --- upgrade 0014 -> 0015 --------------------------------------------
    _run_alembic(pg_url, "upgrade", PR1_0015)
    assert _version(pg_engine) == PR1_0015 and _tables(pg_engine) == tables
    assert _snapshot(pg_engine, others) == {t: schema_0014[t] for t in sorted(others)}
    assert _catalog(pg_engine, others) == {t: catalog_0014[t] for t in sorted(others)}
    assert _global_catalog(pg_engine) == global_0014
    data_0015 = _data(pg_engine, tables - {"alembic_version"})
    assert {t: r for t, r in data_0015.items() if t != "users"} == {
        t: r for t, r in data_0014.items() if t != "users"}
    assert data_0015["users"] == [row + (0,) for row in data_0014["users"]]
    users = _catalog(pg_engine, {"users"})["users"]
    assert users["columns"][-1] == ("level_revision", "bigint", "NO", "0")
    assert (CHECK_NAME, "c", "CHECK ((level_revision >= 0))", " ", " ") in users["constraints"]
    assert users["indexes"] == catalog_0014["users"]["indexes"]

    # Ancien backend : écritures sans connaître la colonne.
    _exercise_legacy_backend(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('legacy-raw', 'debutant')"))
        conn.execute(sa.text("UPDATE users SET level = 'intermediaire' WHERE id = 'legacy-anon-1'"))
        assert conn.execute(sa.text(
            "SELECT id, level, level_revision FROM users WHERE id IN ('legacy-new', 'legacy-raw', 'legacy-anon-1') "
            "ORDER BY id")).all() == [("legacy-anon-1", "intermediaire", 0), ("legacy-new", "avance", 0),
                                      ("legacy-raw", "debutant", 0)]
        assert conn.execute(sa.text(
            "SELECT count(*) FROM portfolio_positions WHERE user_id = 'legacy-new'")).scalar_one() == 0
    with pytest.raises(sa.exc.IntegrityError, match=CHECK_NAME):
        with pg_engine.begin() as conn:
            conn.execute(sa.text("UPDATE users SET level_revision = -1 WHERE id = 'legacy-raw'"))
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM users WHERE id IN ('legacy-new', 'legacy-raw')"))
        conn.execute(sa.text("UPDATE users SET level = 'debutant' WHERE id = 'legacy-anon-1'"))
    schema_0015 = _snapshot(pg_engine, {"users"})

    # --- downgrade 0015 -> 0014 ------------------------------------------
    _run_alembic(pg_url, "downgrade", R1D1)
    assert _version(pg_engine) == R1D1
    assert _snapshot(pg_engine, tables) == schema_0014
    assert _catalog(pg_engine, tables) == catalog_0014
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0014
    assert _compare_metadata(pg_engine) == []

    # --- ré-upgrade ------------------------------------------------------
    _run_alembic(pg_url, "upgrade", PR1_0015)
    assert _snapshot(pg_engine, {"users"}) == schema_0015


def test_pg_upgrade_from_empty_database_to_head_matches_models(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == PR2_0018
    assert _compare_metadata(pg_engine) == []
    with pg_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('fresh', 'debutant')"))
        assert conn.execute(sa.text("SELECT level_revision FROM users")).scalar_one() == 0


def test_pg_new_model_inserts_rely_on_the_server_default(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR1_0015)
    with Session(pg_engine) as db:
        db.add(User(id="model-user", level="debutant"))
        db.commit()
        assert db.get(User, "model-user").level_revision == 0
    with pg_engine.begin() as conn:
        conn.execute(sa.text(
            "UPDATE users SET level = 'avance', level_revision = level_revision + 1 "
            "WHERE id = 'model-user' AND level_revision = 0"))
        stale = conn.execute(sa.text(
            "UPDATE users SET level = 'debutant', level_revision = level_revision + 1 "
            "WHERE id = 'model-user' AND level_revision = 0"))
        assert stale.rowcount == 0
        assert conn.execute(sa.text("SELECT level, level_revision FROM users")).one() == ("avance", 1)


# --------------------------------------------------------------------------
# 3. Garde-fous de verrouillage (PostgreSQL réel)
# --------------------------------------------------------------------------

def _settings(conn) -> dict:
    return {name: conn.execute(sa.text(f"SHOW {name}")).scalar_one() for name in GUARD_SETTINGS}


def _users_locks(engine, pid) -> list:
    with engine.connect() as conn:
        return conn.execute(sa.text(
            "SELECT l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
            "WHERE c.relname = 'users' AND l.pid = :pid AND l.granted ORDER BY l.mode"), {"pid": pid}).scalars().all()


def test_pg_0015_guards_hold_inside_the_migration_transaction_only(pg_url, pg_engine):  # noqa: F811
    """upgrade() pose les trois délais dans SA transaction, sous le verrou
    ACCESS EXCLUSIVE de users ; ils disparaissent avec elle."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    module = _load_0015()
    with pg_engine.connect() as conn:
        before = _settings(conn)
        conn.rollback()
        assert before != GUARD_SETTINGS
        pid = conn.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        assert _settings(conn) == GUARD_SETTINGS
        assert "AccessExclusiveLock" in _users_locks(pg_engine, pid)
        conn.rollback()
        assert _settings(conn) == before
        assert _users_locks(pg_engine, pid) == []
    assert _version(pg_engine) == R1D1


def test_pg_lock_timeout_aborts_the_whole_upgrade_and_leaves_0014_intact(pg_url, pg_engine):  # noqa: F811
    """Une transaction applicative ouverte sur users : `upgrade head` échoue
    au bout de lock_timeout, sans rien laisser de 0015, 0016 ni 0017 ;
    rejoué après la libération, il réussit."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    _seed_historical(pg_engine)
    tables = _tables(pg_engine)
    schema_0014 = _snapshot(pg_engine, tables)
    data_0014 = _data(pg_engine, tables - {"alembic_version"})
    with pg_engine.connect() as blocker:
        blocker.execute(sa.text("SELECT count(*) FROM users")).scalar_one()  # ACCESS SHARE jusqu'au rollback
        started = time.monotonic()
        with pytest.raises(subprocess.CalledProcessError) as failure:
            _run_alembic(pg_url, "upgrade", "head")
        elapsed = time.monotonic() - started
        blocker.rollback()
    assert "LockNotAvailable" in failure.value.stderr and "lock timeout" in failure.value.stderr
    assert 5 <= elapsed < 15
    assert _version(pg_engine) == R1D1
    assert _tables(pg_engine) == tables
    assert _snapshot(pg_engine, tables) == schema_0014
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0014

    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == PR2_0018
    assert _compare_metadata(pg_engine) == []


def test_pg_idle_in_transaction_guard_releases_the_users_lock(pg_url, pg_engine):  # noqa: F811
    """Un client Alembic qui se tait après l'ALTER (conteneur pre-deploy
    supprimé, réseau coupé) : le serveur termine sa session au bout de
    idle_in_transaction_session_timeout et libère le verrou de users, sans
    rien valider."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    module = _load_0015()
    silent = pg_engine.connect()
    try:
        pid = silent.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
        with Operations.context(MigrationContext.configure(silent)):
            module.upgrade()
        assert "AccessExclusiveLock" in _users_locks(pg_engine, pid)
        deadline = time.monotonic() + 30
        while _users_locks(pg_engine, pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        assert _users_locks(pg_engine, pid) == []
        with pg_engine.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}).scalar_one() == 0
            assert conn.execute(sa.text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'level_revision'")).scalar_one() == 0
            # Une lecture de users n'attend plus.
            conn.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
            conn.execute(sa.text("SELECT count(*) FROM users")).scalar_one()
    finally:
        try:
            silent.close()
        except sa.exc.DBAPIError:
            pass  # session déjà terminée par le serveur
    assert _version(pg_engine) == R1D1
