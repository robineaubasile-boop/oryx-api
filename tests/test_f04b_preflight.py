"""F04-B / B06 — précontrôles PostgreSQL en lecture seule.

Base jetable migrée (ORYX_TEST_DATABASE_URL, garde-fous tests/pg_guard.py ;
SKIP sinon). Les altérations d'état (ligne anti-abus absente, tête Alembic
décalée, objet manquant) sont posées et annulées par le TEST, jamais par le
précontrôle : celui-ci ne migre, n'initialise, ne répare et n'écrit rien.
"""
import ast
import json
import subprocess
import sys

import psycopg2
import pytest
import sqlalchemy as sa

from core.auth_f2 import abuse
from core.f2_runtime import preflight as p
from core.f2_runtime.config import load_f2_config
from core.f2_runtime.database import create_f2_engine, normalize_f2_database_url
from tests import f04b_support as s
from tests.f2_support import prepare_schema
from tests.pg_guard import assert_disposable_connection
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture

HEAD = "0021_decryptage_reservations"


@pytest.fixture(scope="module")
def keyring():
    return load_f2_config(s.environ()).keyring


@pytest.fixture(scope="module")
def migrated(pg_url, keyring):  # noqa: F811
    admin = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, admin)
    abuse.initialize_abuse_key_state(admin, keyring, "v1")  # acte explicite du TEST, jamais du précontrôle
    yield admin
    admin.dispose()


@pytest.fixture
def engine(pg_url, migrated):  # noqa: F811
    engine = create_f2_engine(normalize_f2_database_url(pg_url))
    yield engine
    engine.dispose()


def _admin(migrated, *statements):
    with migrated.begin() as conn:
        assert_disposable_connection(conn)
        for statement in statements:
            conn.execute(sa.text(statement))


def _snapshot(migrated) -> dict:
    with migrated.connect() as conn:
        tables = {t: conn.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one() for t in p.F2_REQUIRED_TABLES}
        return {
            "alembic": conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all(),
            "key_state": [tuple(r) for r in conn.execute(sa.text("SELECT * FROM auth_abuse_key_state")).all()],
            "tables": tables,
        }


def _refused(engine, keyring, code, **kwargs):
    with pytest.raises(p.F2PreflightError) as info:
        p.run_f2_preflight(engine, keyring, **kwargs)
    assert info.value.code == code and info.value.__cause__ is None
    return info.value


# --------------------------------------------------------------------------
# Nominal, lecture seule
# --------------------------------------------------------------------------

def test_expected_head_is_the_single_head_of_the_code():
    assert p.expected_alembic_head() == HEAD


def test_nominal_preflight_reports_and_writes_nothing(engine, keyring, migrated):
    before = _snapshot(migrated)
    statements, commits, rollbacks = [], [], []
    sa.event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *a: statements.append(stmt))
    sa.event.listen(engine, "commit", lambda conn: commits.append(1))
    sa.event.listen(engine, "rollback", lambda conn: rollbacks.append(1))
    report = p.run_f2_preflight(engine, keyring)
    assert report.alembic_head == HEAD and report.abuse_versions == ("v1",)
    assert report.server_major in p.QUALIFIED_POSTGRES_MAJORS
    assert commits == [] and rollbacks
    assert statements[0] == "SET TRANSACTION READ ONLY"
    for statement in statements:
        head = statement.lstrip().split(None, 1)[0].upper()
        assert head in ("SELECT", "SET"), statement
        assert not statement.upper().startswith("SET ") or statement.startswith(("SET TRANSACTION READ ONLY",
                                                                                   "SET LOCAL "))
        for verb in ("INSERT", "UPDATE ", "DELETE", "ALTER", "CREATE", "DROP", "TRUNCATE", "GRANT", "COPY",
                     "pg_advisory", "FOR UPDATE", "FOR SHARE", "nextval"):
            assert verb not in statement, statement
    assert _snapshot(migrated) == before
    assert engine.pool.checkedout() == 0


def test_the_preflight_transaction_is_really_read_only(engine, keyring, monkeypatch, migrated):
    """Une écriture tentée DANS la transaction du précontrôle (à l'étape
    anti-abus) est refusée par PostgreSQL : échec fermé, état inchangé."""
    before = _snapshot(migrated)
    seen = {}

    def writing_probe(conn, ring):
        seen["read_only"] = conn.execute(sa.text("SHOW transaction_read_only")).scalar_one()
        conn.execute(sa.text("UPDATE auth_abuse_key_state SET current_version = 'v9' WHERE id = 1"))

    monkeypatch.setattr(p, "load_key_state", writing_probe)
    _refused(engine, keyring, p.DATABASE_ERROR)
    assert seen == {"read_only": "on"}
    assert _snapshot(migrated) == before


def test_the_preflight_never_migrates_initializes_or_repairs():
    tree = ast.parse((REPO_ROOT / "core" / "f2_runtime" / "preflight.py").read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    for forbidden in ("upgrade", "downgrade", "stamp", "initialize_abuse_key_state", "rotate_abuse_key",
                      "retire_previous_abuse_key", "purge_abuse_history", "ensure_subjects", "lock_key_state",
                      "insert_event", "commit", "begin_nested", "run_env"):
        assert forbidden not in names, forbidden
    assert not any(m.startswith("alembic.command") or m == "alembic.op" for m in imported)
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    sql = [x for x in strings if x.lstrip().upper().startswith(("SELECT", "SET", "INSERT", "UPDATE", "DELETE"))]
    assert all(x.lstrip().upper().startswith(("SELECT", "SET TRANSACTION READ ONLY", "SET LOCAL ")) for x in sql), sql


def test_computing_the_head_loads_no_legacy_module():
    code = ("import sys, json\nfrom core.f2_runtime.preflight import expected_alembic_head\n"
            "head = expected_alembic_head()\n"
            # alembic/env.py importe core.db : son absence prouve que env.py n'a pas été exécuté.
            "print(json.dumps([head, sorted(m for m in ('core.db', 'core.models', 'api') if m in sys.modules)]))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env={**_clean_env({}), "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/oryx_never_test"},
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [HEAD, []]


# --------------------------------------------------------------------------
# État anti-abus absent ou incompatible
# --------------------------------------------------------------------------

def test_missing_abuse_key_state_fails_closed_and_is_not_initialized(engine, keyring, migrated):
    _admin(migrated, "DELETE FROM auth_abuse_key_state")
    try:
        _refused(engine, keyring, p.ABUSE_KEY_STATE_UNAVAILABLE)
        assert _snapshot(migrated)["key_state"] == []  # toujours absent : aucune initialisation
    finally:
        abuse.initialize_abuse_key_state(migrated, keyring, "v1")


def test_keyring_incompatible_with_the_recorded_state_fails_closed(engine, migrated):
    document = s.auth_document()
    s.set_purpose(document, "abuse", {"v2": s.key(900)}, "v2")
    other = load_f2_config(s.environ(ORYX_F2_AUTH_KEYS_JSON=json.dumps(document))).keyring
    before = _snapshot(migrated)
    _refused(engine, other, p.ABUSE_KEY_STATE_UNAVAILABLE)
    assert _snapshot(migrated) == before  # aucune rotation, aucune réparation


def test_recorded_previous_version_must_also_be_known(engine, keyring, migrated):
    _admin(migrated, "UPDATE auth_abuse_key_state SET previous_version = 'v0', "
                     "previous_demoted_at = clock_timestamp() WHERE id = 1")
    try:
        _refused(engine, keyring, p.ABUSE_KEY_STATE_UNAVAILABLE)
    finally:
        _admin(migrated, "UPDATE auth_abuse_key_state SET previous_version = NULL, previous_demoted_at = NULL "
                         "WHERE id = 1")


# --------------------------------------------------------------------------
# Version Alembic et objets F2
# --------------------------------------------------------------------------

def test_an_older_alembic_version_fails_closed_without_upgrade(engine, keyring, migrated):
    _admin(migrated, "UPDATE alembic_version SET version_num = '0020_position_revision'")
    try:
        _refused(engine, keyring, p.ALEMBIC_HEAD_MISMATCH)
        assert _snapshot(migrated)["alembic"] == ["0020_position_revision"]  # jamais migrée
    finally:
        _admin(migrated, f"UPDATE alembic_version SET version_num = '{HEAD}'")


def test_an_unexpected_head_or_several_rows_fail_closed(engine, keyring, migrated):
    _refused(engine, keyring, p.ALEMBIC_HEAD_MISMATCH, expected_head="0022_future")
    _admin(migrated, "INSERT INTO alembic_version (version_num) VALUES ('0099_branch')")
    try:
        _refused(engine, keyring, p.ALEMBIC_HEAD_MISMATCH)
    finally:
        _admin(migrated, "DELETE FROM alembic_version WHERE version_num = '0099_branch'")


def test_missing_or_empty_alembic_version_fails_closed(engine, keyring, migrated):
    _admin(migrated, "ALTER TABLE alembic_version RENAME TO alembic_version_f04b")
    try:
        _refused(engine, keyring, p.ALEMBIC_VERSION_MISSING)
    finally:
        _admin(migrated, "ALTER TABLE alembic_version_f04b RENAME TO alembic_version")
    _admin(migrated, "DELETE FROM alembic_version")
    try:
        _refused(engine, keyring, p.ALEMBIC_VERSION_MISSING)
    finally:
        _admin(migrated, f"INSERT INTO alembic_version (version_num) VALUES ('{HEAD}')")


@pytest.mark.parametrize("table", ["decryptage_turn_reservations", "auth_abuse_key_state", "oryx_sessions",
                                   "portfolio_write_idempotency"])
def test_a_missing_f2_table_fails_closed(engine, keyring, migrated, table):
    _admin(migrated, f"ALTER TABLE {table} RENAME TO {table}_f04b")
    try:
        _refused(engine, keyring, p.F2_OBJECTS_MISSING)
    finally:
        _admin(migrated, f"ALTER TABLE {table}_f04b RENAME TO {table}")


@pytest.mark.parametrize("table, column", p.F2_REQUIRED_COLUMNS)
def test_a_missing_f2_column_fails_closed(engine, keyring, migrated, table, column):
    _admin(migrated, f"ALTER TABLE {table} RENAME COLUMN {column} TO {column}_f04b")
    try:
        _refused(engine, keyring, p.F2_OBJECTS_MISSING)
    finally:
        _admin(migrated, f"ALTER TABLE {table} RENAME COLUMN {column}_f04b TO {column}")


# --------------------------------------------------------------------------
# Serveur, isolation, connexion
# --------------------------------------------------------------------------

def test_a_server_that_is_not_the_configured_database_fails_closed(pg_url, keyring, migrated):  # noqa: F811
    real = normalize_f2_database_url(pg_url)
    lying = real.set(database="oryx_other_f04b_test")
    dsn = real.render_as_string(hide_password=False).replace("postgresql+psycopg2://", "postgresql://")
    engine = sa.create_engine(lying, creator=lambda: psycopg2.connect(dsn), isolation_level="READ COMMITTED")
    try:
        _refused(engine, keyring, p.UNEXPECTED_SERVER)
    finally:
        engine.dispose()


def test_an_unqualified_server_version_fails_closed(engine, keyring, monkeypatch):
    monkeypatch.setattr(p, "QUALIFIED_POSTGRES_MAJORS", (99,))
    _refused(engine, keyring, p.UNSUPPORTED_SERVER_VERSION)


def test_an_engine_without_read_committed_fails_closed(pg_url, keyring, migrated):  # noqa: F811
    engine = sa.create_engine(normalize_f2_database_url(pg_url), isolation_level="SERIALIZABLE")
    try:
        _refused(engine, keyring, p.ISOLATION_NOT_READ_COMMITTED)
    finally:
        engine.dispose()


def test_an_unreachable_server_fails_closed(keyring):
    engine = create_f2_engine(normalize_f2_database_url("postgresql://u:synthetic-password@127.0.0.1:1/oryx_x_test"))
    try:
        error = _refused(engine, keyring, p.CONNECTION_FAILED)
        assert "synthetic-password" not in str(error)
    finally:
        engine.dispose()


def test_wrong_inputs_are_refused_before_any_connection(keyring):
    with pytest.raises(TypeError):
        p.run_f2_preflight(sa.create_engine("sqlite://"), keyring)
    engine = create_f2_engine(normalize_f2_database_url("postgresql://u:p@127.0.0.1:1/oryx_x_test"))
    with pytest.raises(TypeError):
        p.run_f2_preflight(engine, object())
    with pytest.raises(TypeError):
        p.run_f2_preflight(engine, keyring, expected_head="")
    engine.dispose()
