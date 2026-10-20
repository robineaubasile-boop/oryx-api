"""Tests du préflight de production R1 PR 1 (scripts/r1_pr1_preflight.py).

1. Sans base : règles GO / NO-GO, autonomie du script (aucun import de
   core/ ni scripts/), requête unique sans écriture, CLI sans secret.
2. PostgreSQL réel (ORYX_TEST_DATABASE_URL, base jetable protégée par
   tests/pg_guard.py ; sinon SKIPPÉS) : GO en 0014, NO-GO en tête ou sur
   application partielle, avertissement sous activité concurrente, aucune
   écriture possible, base inchangée.
"""
import ast
import re
import subprocess
import sys

import pytest
import sqlalchemy as sa

from scripts import r1_pr1_preflight as preflight
from tests.pg_guard import scrubbed_environment, validate_test_database_url
from tests.test_migration_0002_analysis_sessions import (
    REPO_ROOT,
    _reset_schema,
    _run_alembic,
    _snapshot,
    _tables,
    _version,
    pg_engine,  # noqa: F401 — fixture
    pg_url,  # noqa: F401 — fixture
)
from tests.test_migration_0004_drop_company_analyses import PR1_0015, PR1_0017, R1D1, _data

SCRIPT = REPO_ROOT / "scripts" / "r1_pr1_preflight.py"


def _facts(**overrides) -> dict:
    facts = {
        "server_version": "18.4", "server_version_num": 180004, "in_recovery": False,
        "transaction_read_only": "on", "alembic_revisions": R1D1, "level_revision_present": False,
        "level_revision_check_present": False, "pr1_tables_present": [], "users_estimated_rows": 120,
        "users_total_size": "64 kB", "long_transactions": 0, "oldest_transaction_seconds": 0,
        "users_lock_holders": 0, "role_lock_timeout": "0ms", "role_statement_timeout": "0ms",
        "role_idle_in_transaction_session_timeout": "0ms",
    }
    facts.update(overrides)
    return facts


def _levels(report) -> list:
    return [f.level for f in report.findings]


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_nominal_facts_are_go_without_warning():
    report = preflight.evaluate(_facts())
    assert report.go and "WARN" not in _levels(report) and "FAIL" not in _levels(report)
    text = preflight.format_report(report)
    assert "PostgreSQL 18.4" in text and f"révision Alembic installée : {R1D1}" in text
    assert text.splitlines()[-1].startswith("GO TECHNIQUE R1 PR 1 (0 avertissement(s))")
    assert "sauvegarde restaurable" in text


@pytest.mark.parametrize("overrides, expected", [
    ({"server_version": "10.23", "server_version_num": 100023}, "exige PostgreSQL >= 11"),
    ({"in_recovery": True}, "réplique"),
    ({"transaction_read_only": "off"}, "non read-only"),
    ({"alembic_revisions": "0013_decryptage_conv_affinity"}, "au lieu de 0014_evaluation_run_leases"),
    ({"alembic_revisions": None}, "au lieu de 0014_evaluation_run_leases"),
    ({"alembic_revisions": f"{R1D1},{PR1_0015}"}, "déjà appliquée"),
    ({"alembic_revisions": PR1_0015}, "déjà appliquée"),
    ({"alembic_revisions": PR1_0017}, "déjà appliquée"),
    ({"level_revision_present": True}, "users.level_revision existe déjà"),
    ({"level_revision_check_present": True}, "ck_users_level_revision_non_negative existe déjà"),
    ({"pr1_tables_present": ["oryx_sessions"]}, "tables R1 PR 1 déjà présentes : oryx_sessions"),
])
def test_each_blocking_fact_is_no_go(overrides, expected):
    report = preflight.evaluate(_facts(**overrides))
    assert not report.go
    assert any(f.level == "FAIL" and expected in f.message for f in report.findings)
    assert preflight.format_report(report).splitlines()[-1] == "NO-GO R1 PR 1 — ne pas merger"


@pytest.mark.parametrize("overrides, expected", [
    ({"long_transactions": 1, "oldest_transaction_seconds": 42}, "la plus ancienne : 42 s"),
    ({"users_lock_holders": 2}, "2 session(s) tenant un verrou sur users"),
    ({"users_estimated_rows": 200_000_000}, "~10000 ms sous verrou"),
])
def test_operational_risks_warn_but_do_not_block(overrides, expected):
    report = preflight.evaluate(_facts(**overrides))
    assert report.go
    assert any(f.level == "WARN" and expected in f.message for f in report.findings)


def test_never_analyzed_users_table_is_not_a_negative_estimate():
    report = preflight.evaluate(_facts(users_estimated_rows=-1))
    assert any("~0 lignes" in f.message for f in report.findings)


def test_expected_objects_match_the_pr1_migrations():
    assert preflight.EXPECTED_REVISION == R1D1
    assert preflight.PR1_REVISIONS[0] == PR1_0015 and preflight.PR1_REVISIONS[-1] == PR1_0017
    from core.db import Base
    from core import models  # noqa: F401
    pr1_tables = {"portfolio_write_idempotency"} | {
        t for t in Base.metadata.tables if t.startswith(("auth_", "oryx_session")) or t == "invitations"}
    assert set(preflight.PR1_TABLES) == pr1_tables and len(preflight.PR1_TABLES) == 12


def test_script_is_autonomous():
    """Exécutable par l'entrée standard dans le conteneur déployé : seuls la
    bibliothèque standard et SQLAlchemy sont importés."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert roots <= {"__future__", "os", "sys", "dataclasses", "typing", "sqlalchemy"}


def test_preflight_sql_is_a_single_select_without_writes():
    sql = preflight.PREFLIGHT_SQL
    assert sql.startswith("SELECT") and ";" not in sql
    words = set(re.findall(r"[a-z_.]+", sql.lower()))
    for forbidden in ("insert", "update", "delete", "alter", "create", "drop", "truncate", "grant", "copy",
                      "set_config", "pg_terminate_backend", "pg_cancel_backend", "nextval", "lock"):
        assert forbidden not in words, forbidden
    # users n'est jamais lue : seuls ses identifiants de catalogue servent.
    assert not re.search(r"\bfrom\s+(public\.)?users\b", sql, re.IGNORECASE)


def test_cli_print_sql_needs_no_database(capsys):
    assert preflight.main(["--print-sql"]) == 0
    assert capsys.readouterr().out.strip() == preflight.PREFLIGHT_SQL


def test_cli_without_database_url_is_an_operational_error(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert preflight.main([]) == 2
    assert "DATABASE_URL n'est pas défini" in capsys.readouterr().err
    assert preflight.main(["--unknown"]) == 2


def test_connection_error_never_prints_the_url():
    secret = "postgresql://oryx:s3cr3t-password@127.0.0.1:1/oryx_nowhere_test"
    result = subprocess.run([sys.executable, str(SCRIPT)], env=dict(scrubbed_environment(), DATABASE_URL=secret),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert "s3cr3t" not in result.stdout + result.stderr and "oryx_nowhere" not in result.stderr
    assert result.stderr.startswith("PREFLIGHT ERROR: erreur PostgreSQL (OperationalError)")


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

def _run_preflight(url: str) -> subprocess.CompletedProcess:
    """Comme en production : le script seul, transmis par l'entrée standard
    (python -), hors du dépôt."""
    validate_test_database_url(url)
    return subprocess.run([sys.executable, "-I", "-"], input=SCRIPT.read_text(encoding="utf-8"),
                          env=dict(scrubbed_environment(), DATABASE_URL=url), capture_output=True, text=True,
                          timeout=120, cwd="/")


def test_pg_go_at_0014_and_database_unchanged(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    with pg_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('u1', 'debutant'), ('u2', 'avance')"))
        conn.execute(sa.text("ANALYZE users"))
    tables = _tables(pg_engine)
    before = (_snapshot(pg_engine, tables), _data(pg_engine, tables))
    result = _run_preflight(pg_url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"INFO révision Alembic installée : {R1D1}" in result.stdout
    assert "INFO users : ~2 lignes" in result.stdout
    assert "aucune transaction longue ni verrou concurrent sur users" in result.stdout
    assert result.stdout.splitlines()[-1].startswith("GO TECHNIQUE R1 PR 1 (0 avertissement(s))")
    assert (_snapshot(pg_engine, tables), _data(pg_engine, tables)) == before


def test_pg_no_go_once_pr1_is_applied(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    result = _run_preflight(pg_url)
    assert result.returncode == 1
    assert f"révision Alembic {PR1_0017} : une migration de R1 PR 1 est déjà appliquée" in result.stdout
    assert "FAIL users.level_revision existe déjà" in result.stdout
    assert "FAIL tables R1 PR 1 déjà présentes : auth_abuse_events" in result.stdout
    assert result.stdout.splitlines()[-1] == "NO-GO R1 PR 1 — ne pas merger"


def test_pg_no_go_on_a_partial_manual_application(pg_url, pg_engine):  # noqa: F811
    """Révision 0014 mais objet déjà créé à la main : NO-GO."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    with pg_engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE users ADD COLUMN level_revision BIGINT DEFAULT 0 NOT NULL"))
    result = _run_preflight(pg_url)
    assert result.returncode == 1
    assert "FAIL users.level_revision existe déjà" in result.stdout
    assert _version(pg_engine) == R1D1


def test_pg_warns_under_concurrent_activity_without_waiting(pg_url, pg_engine):  # noqa: F811
    """Une transaction applicative ouverte sur users : avertissement, et le
    préflight lui-même ne reste pas bloqué."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    with pg_engine.connect() as busy:
        busy.execute(sa.text("SELECT count(*) FROM users")).scalar_one()
        busy.execute(sa.text("SELECT pg_sleep(6)"))
        result = _run_preflight(pg_url)
        busy.rollback()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN activité concurrente : 1 transaction(s) ouverte(s) depuis plus de 5 s" in result.stdout
    assert "1 session(s) tenant un verrou sur users" in result.stdout


def test_pg_preflight_connection_refuses_any_write(pg_url, pg_engine):  # noqa: F811
    """La connexion du préflight est read-only côté serveur : une écriture
    y échoue (preuve que PREFLIGHT_SQL ne pourrait rien modifier)."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, connect_args=preflight.READONLY_CONNECT_ARGS)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SHOW transaction_read_only")).scalar_one() == "on"
            with pytest.raises(sa.exc.InternalError, match="read-only transaction"):
                conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('x', 'debutant')"))
    finally:
        engine.dispose()
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users")).scalar_one() == 0
