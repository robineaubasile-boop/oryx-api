"""Tests PostgreSQL des invariants de ci/check_migrations.py (jobs
ci / migrations-16 et -18) : ils acceptent le schéma de head et refusent
chaque dérive qu'ils prétendent détecter.

Contre un vrai PostgreSQL uniquement (ORYX_TEST_DATABASE_URL vers une base
DÉDIÉE dont le nom contient "test", schéma public détruit et recréé ; sinon
SKIPPÉS, comme les autres tests PostgreSQL).
"""
import pytest
import sqlalchemy as sa

from ci import check_migrations
from tests.test_migration_0002_analysis_sessions import (
    _reset_schema,
    _run_alembic,
    pg_engine,  # noqa: F401 — fixture
    pg_url,  # noqa: F401 — fixture
)


@pytest.fixture(scope="module")
def head_engine(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    _reset_schema(engine)
    _run_alembic(pg_url, "upgrade", "head")
    yield engine
    _reset_schema(engine)
    engine.dispose()


def _execute(engine, *statements):
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(sa.text(statement))


def test_pg_head_schema_satisfies_the_invariants_and_the_models(head_engine):
    check_migrations.check_invariants(head_engine, check_migrations.snapshot(head_engine))
    check_migrations.check_models(head_engine)


@pytest.mark.parametrize("mutation, undo, message", [
    ("CREATE TYPE ci_probe_mood AS ENUM ('a', 'b')", "DROP TYPE ci_probe_mood", "types"),
    ("CREATE FUNCTION ci_probe_fn() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'",
     "DROP FUNCTION ci_probe_fn()", "fonctions"),
    ("ALTER TABLE users ADD CONSTRAINT ci_probe_level CHECK (level <> '') NOT VALID",
     "ALTER TABLE users DROP CONSTRAINT ci_probe_level", "non validée"),
    ("ALTER TABLE users ADD CONSTRAINT ci_probe_unique UNIQUE (level) DEFERRABLE",
     "ALTER TABLE users DROP CONSTRAINT ci_probe_unique", "différable"),
    ("ALTER TABLE portfolio_positions ADD CONSTRAINT ci_probe_fk FOREIGN KEY (user_id) "
     "REFERENCES users(id) ON DELETE CASCADE",
     "ALTER TABLE portfolio_positions DROP CONSTRAINT ci_probe_fk", "ON DELETE"),
])
def test_pg_invariants_refuse_each_forbidden_object(head_engine, mutation, undo, message):
    _execute(head_engine, mutation)
    try:
        with pytest.raises(check_migrations.CheckFailed, match=message):
            check_migrations.check_invariants(head_engine, check_migrations.snapshot(head_engine))
    finally:
        _execute(head_engine, undo)
    check_migrations.check_invariants(head_engine, check_migrations.snapshot(head_engine))


def test_pg_invariants_refuse_a_trigger(head_engine):
    _execute(head_engine,
             "CREATE FUNCTION ci_probe_fn() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END'",
             "CREATE TRIGGER ci_probe_trigger BEFORE INSERT ON users FOR EACH ROW EXECUTE FUNCTION ci_probe_fn()")
    try:
        with pytest.raises(check_migrations.CheckFailed):
            check_migrations.check_invariants(head_engine, check_migrations.snapshot(head_engine))
        snap = check_migrations.snapshot(head_engine)
        assert snap["triggers"] == [["users", "ci_probe_trigger"]]
    finally:
        _execute(head_engine, "DROP TRIGGER ci_probe_trigger ON users", "DROP FUNCTION ci_probe_fn()")


@pytest.mark.parametrize("mutation, undo", [
    ("ALTER TABLE users ADD COLUMN ci_probe_extra varchar", "ALTER TABLE users DROP COLUMN ci_probe_extra"),
    ("ALTER TABLE users ALTER COLUMN created_at SET DEFAULT now()",
     "ALTER TABLE users ALTER COLUMN created_at DROP DEFAULT"),
    ("ALTER TABLE users ALTER COLUMN created_at SET NOT NULL",
     "ALTER TABLE users ALTER COLUMN created_at DROP NOT NULL"),
    ("CREATE INDEX ci_probe_index ON users (level)", "DROP INDEX ci_probe_index"),
])
def test_pg_models_comparison_refuses_any_drift(head_engine, mutation, undo):
    before = check_migrations.snapshot(head_engine)
    _execute(head_engine, mutation)
    try:
        with pytest.raises(check_migrations.CheckFailed, match="divergent"):
            check_migrations.check_models(head_engine)
        assert check_migrations.schema_difference(before, check_migrations.snapshot(head_engine))
    finally:
        _execute(head_engine, undo)
    assert check_migrations.schema_difference(before, check_migrations.snapshot(head_engine)) == []
    check_migrations.check_models(head_engine)
