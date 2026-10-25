"""Le comparateur de métadonnées partagé (tests/test_migration_0004_drop_company_analyses.py,
_compare_metadata / _without_pr1_changes) ne masque aucun écart de schéma
non attendu (R1 PR 1).

Il ne retire que des écarts exactement nommés (users.level_revision, tables
et index de 0016 / 0017), uniquement pour les révisions PR 1 non encore
appliquées, et il échoue si l'un manque ou si un écart PR 1 subsiste après
l'application de sa révision. Tout autre écart (autre colonne, autre table,
autre index, suppression, modification) est rendu tel quel.
"""
import pytest
import sqlalchemy as sa

from core.db import Base
from tests.test_migration_0002_analysis_sessions import (
    _reset_schema,
    _run_alembic,
    pg_engine,  # noqa: F401 — fixture
    pg_url,  # noqa: F401 — fixture
)
from tests.test_migration_0004_drop_company_analyses import (
    PR1_0015,
    PR1_0016,
    PR1_0016_TABLES,
    PR1_0017,
    PR1_0017_INDEXES,
    PR1_0017_TABLES,
    R1D1,
    _compare_metadata,
    PR2_0018,
    PR2_0018_INDEXES,
    PR2_0018_TABLES,
    PR4_0019,
    _raw_compare_metadata,
    _without_pr1_changes,
)

TABLES = Base.metadata.tables
INDEXES = {i.name: i for t in TABLES.values() for i in t.indexes}
# Objets inattendus construits HORS de Base.metadata (un Index lié à une
# colonne de Base.metadata s'attacherait à la vraie table).
_SHADOW = sa.Table("auth_operations", sa.MetaData(), sa.Column("kind", sa.String()))


def _expected(applied):
    diff = []
    if applied < 1:
        diff.append(("add_column", None, "users", TABLES["users"].c.level_revision))
    if applied < 2:
        diff += [("add_table", TABLES[t]) for t in PR1_0016_TABLES]
    if applied < 3:
        diff += [("add_table", TABLES[t]) for t in PR1_0017_TABLES] + [("add_index", INDEXES[i])
                                                                      for i in sorted(PR1_0017_INDEXES)]
    if applied == 3:
        diff.append(("add_column", None, "auth_abuse_events", TABLES["auth_abuse_events"].c.blocked_until))
    if applied < 4:
        diff += [("add_table", TABLES[t]) for t in PR2_0018_TABLES] + [("add_index", INDEXES[i])
                                                                      for i in sorted(PR2_0018_INDEXES)]
    if 3 <= applied < 5:
        diff.append(("add_column", None, "auth_abuse_events", TABLES["auth_abuse_events"].c.http_action))
    return diff


UNEXPECTED = [
    ("add_column", None, "users", sa.Column("level_revision_shadow", sa.BigInteger())),
    ("add_column", None, "portfolio_positions", sa.Column("level_revision", sa.BigInteger())),
    ("remove_column", None, "users", sa.Column("level_revision", sa.BigInteger())),
    ("add_table", TABLES["analysis_sessions"]),
    ("remove_table", TABLES["auth_operations"]),
    ("add_index", sa.Index("ix_auth_operations_shadow", _SHADOW.c.kind)),
    ("remove_index", INDEXES["ix_auth_operations_user_id"]),
    [("modify_nullable", None, "auth_operations", "kind", {}, False, True)],
    ("add_fk", TABLES["oryx_sessions"].foreign_key_constraints.copy().pop()),
]


@pytest.mark.parametrize("applied", [0, 1, 2, 3, 4, 5])
def test_only_exact_pending_pr1_changes_are_removed(applied):
    assert _without_pr1_changes(_expected(applied) + UNEXPECTED, applied) == UNEXPECTED


@pytest.mark.parametrize("applied", [0, 1, 2, 3, 4])
def test_a_missing_expected_pr1_change_fails(applied):
    expected = _expected(applied)
    for index in range(len(expected)):
        with pytest.raises(AssertionError):
            _without_pr1_changes(expected[:index] + expected[index + 1:], applied)


@pytest.mark.parametrize("applied", [1, 2, 3, 4, 5])
def test_a_pr1_change_after_its_revision_fails(applied):
    for leftover in _expected(applied - 1)[: len(_expected(applied - 1)) - len(_expected(applied))]:
        with pytest.raises(AssertionError):
            _without_pr1_changes(_expected(applied) + [leftover], applied)


def test_at_head_nothing_is_removed():
    assert _without_pr1_changes(UNEXPECTED, 5) == UNEXPECTED
    with pytest.raises(AssertionError):
        _without_pr1_changes([("add_table", TABLES["oryx_sessions"])], 5)
    with pytest.raises(AssertionError):
        _without_pr1_changes([("add_table", TABLES["auth_abuse_key_state"])], 5)
    with pytest.raises(AssertionError):
        _without_pr1_changes([("add_column", None, "auth_abuse_events", TABLES["auth_abuse_events"].c.http_action)], 5)


def test_blocked_until_is_removed_only_as_a_pending_0018_column():
    column = ("add_column", None, "auth_abuse_events", TABLES["auth_abuse_events"].c.blocked_until)
    with pytest.raises(AssertionError):
        _without_pr1_changes(_expected(2) + [column], 2)
    with pytest.raises(AssertionError):
        _without_pr1_changes(_expected(4) + [column], 4)


def test_http_action_is_removed_only_as_a_pending_0019_column():
    column = ("add_column", None, "auth_abuse_events", TABLES["auth_abuse_events"].c.http_action)
    with pytest.raises(AssertionError):
        _without_pr1_changes(_expected(2) + [column], 2)  # avant 0017 : arrive avec la table
    with pytest.raises(AssertionError):
        _without_pr1_changes(_expected(5) + [column], 5)
    assert _without_pr1_changes(_expected(4), 4) == []


# --------------------------------------------------------------------------
# Contre un vrai PostgreSQL : de vraies dérives de schéma restent visibles.
# --------------------------------------------------------------------------

def test_pg_unexpected_drift_before_pr1_is_still_reported(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    assert _compare_metadata(pg_engine) == []
    with pg_engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE portfolio_positions ADD COLUMN drift_column INTEGER"))
        conn.execute(sa.text("CREATE INDEX ix_drift ON users (level)"))
    diff = _compare_metadata(pg_engine)
    assert sorted((d[0], getattr(d[-1], "name", None) or d[1].name) for d in diff) == [
        ("remove_column", "drift_column"), ("remove_index", "ix_drift")]


def test_pg_premature_pr1_object_is_never_hidden(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    with pg_engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE users ADD COLUMN level_revision BIGINT NOT NULL DEFAULT 0"))
    with pytest.raises(AssertionError):
        _compare_metadata(pg_engine)


@pytest.mark.parametrize("revision", [PR1_0015, PR1_0016, PR1_0017, PR2_0018, PR4_0019])
def test_pg_missing_pr1_object_after_its_revision_is_reported(pg_url, pg_engine, revision):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", revision)
    assert _compare_metadata(pg_engine) == []
    statement = {
        PR1_0015: "ALTER TABLE users DROP COLUMN level_revision",
        PR1_0016: "DROP TABLE portfolio_write_idempotency",
        PR1_0017: "DROP INDEX ix_auth_operations_user_id",
        PR2_0018: "DROP INDEX uq_auth_abuse_events_refusal_bucket",
        PR4_0019: "ALTER TABLE auth_abuse_events DROP COLUMN http_action",
    }[revision]
    with pg_engine.begin() as conn:
        conn.execute(sa.text(statement))
    assert _raw_compare_metadata(pg_engine) != []
    with pytest.raises(AssertionError):
        _compare_metadata(pg_engine)
