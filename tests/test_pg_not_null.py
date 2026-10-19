"""Tests de tests/pg_not_null.py : les NOT NULL de PostgreSQL 18 (contype 'n')
sont vérifiés exactement, jamais ignorés.

1. Sans base : nom automatique des contraintes NOT NULL (y compris tronqué à
   63 caractères, comme longitudinal_assessment_runs.pedagogical_taxonomy_release_id),
   extraction des colonnes NOT NULL attendues.
2. Contre un vrai PostgreSQL (ORYX_TEST_DATABASE_URL, base DÉDIÉE dont le nom
   contient "test" ; sinon SKIPPÉS, comme les autres tests PostgreSQL) : sur
   PostgreSQL 16 comme sur 18, une colonne NOT NULL manquante ou en trop dans
   les attentes, une NOT NULL ajoutée en base, une ligne 'n' fabriquée ou
   retirée, une contrainte renommée ou non validée font échouer la
   vérification ; les PK, FK, UNIQUE et CHECK sont rendues intactes.
   Chaque test s'exécute sur les deux versions (aucun saut selon la version).
"""
import pytest
import sqlalchemy as sa

from tests.pg_not_null import (
    expected_not_null_rows,
    not_null_columns,
    not_null_constraint_name,
    without_verified_not_null,
)
from tests.test_migration_0002_analysis_sessions import (
    _reset_schema,
    pg_engine,  # noqa: F401 — fixture
    pg_url,  # noqa: F401 — fixture
)

PARENT = "ci_probe_parents"
TABLE = "ci_probe_items"
LONG_TABLE = "longitudinal_assessment_runs"
LONG_COLUMN = "pedagogical_taxonomy_release_id"


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_not_null_constraint_name_matches_postgresql_naming():
    assert not_null_constraint_name("users", "id") == "users_id_not_null"
    assert not_null_constraint_name("analysis_sessions", "started_at") == "analysis_sessions_started_at_not_null"
    # Observé sur PostgreSQL 18.6 : les deux noms sont raccourcis, le plus long d'abord.
    name = not_null_constraint_name(LONG_TABLE, LONG_COLUMN)
    assert name == "longitudinal_assessment_run_pedagogical_taxonomy_relea_not_null"
    assert len(name) == 63


def test_not_null_columns_reads_the_expected_catalog_rows():
    expected = [("id", "uuid", "NO", None), ("note", "text", "YES", None), ("kind", "character varying", "NO", None)]
    assert not_null_columns(expected) == ["id", "kind"]
    assert expected_not_null_rows("t", ["id"]) == [("t_id_not_null", "n", "NOT NULL id", " ", " ")]


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

@pytest.fixture
def probe(pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(sa.text(f"CREATE TABLE {PARENT} (id integer PRIMARY KEY)"))
        conn.execute(sa.text(
            f"CREATE TABLE {TABLE} (id integer PRIMARY KEY, parent_id integer NOT NULL REFERENCES {PARENT}(id), "
            "kind varchar NOT NULL CONSTRAINT ck_ci_probe_items_kind CHECK (kind <> ''), note varchar, "
            "CONSTRAINT uq_ci_probe_items_parent_kind UNIQUE (parent_id, kind))"
        ))
    yield pg_engine
    _reset_schema(pg_engine)


def _constraints(engine, table=TABLE) -> list:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT conname, contype, pg_get_constraintdef(oid), confdeltype, confupdtype "
            "FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) ORDER BY conname"
        ), {"t": f"public.{table}"}).all()]


def _version(engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.text("SHOW server_version_num")).scalar_one())


EXPECTED_OTHERS = [
    ("ci_probe_items_parent_id_fkey", "f", f"FOREIGN KEY (parent_id) REFERENCES {PARENT}(id)", "a", "a"),
    ("ci_probe_items_pkey", "p", "PRIMARY KEY (id)", " ", " "),
    ("ck_ci_probe_items_kind", "c", "CHECK (((kind)::text <> ''::text))", " ", " "),
    ("uq_ci_probe_items_parent_kind", "u", "UNIQUE (parent_id, kind)", " ", " "),
]
NOT_NULL = ["id", "parent_id", "kind"]


def test_pg_exact_not_null_constraints_are_verified_then_removed(probe):
    raw = _constraints(probe)
    not_null_rows = [row for row in raw if row[1] == "n"]
    if _version(probe) >= 180000:
        assert sorted(not_null_rows) == sorted(expected_not_null_rows(TABLE, NOT_NULL))
    else:
        assert not_null_rows == []
    # PK, FK, UNIQUE et CHECK rendues intactes, dans leur ordre.
    assert without_verified_not_null(probe, TABLE, raw, NOT_NULL) == EXPECTED_OTHERS
    with probe.connect() as conn:  # même résultat avec une connexion existante
        assert without_verified_not_null(conn, TABLE, raw, list(reversed(NOT_NULL))) == EXPECTED_OTHERS


@pytest.mark.parametrize("expected", [
    ["id", "parent_id"],                  # NOT NULL de kind absente des attentes
    ["id", "parent_id", "kind", "note"],  # note attendue NOT NULL mais nullable
    ["id", "parent_id", "kind", "kind"],  # doublon
    [],
])
def test_pg_wrong_expectations_fail(probe, expected):
    with pytest.raises(AssertionError):
        without_verified_not_null(probe, TABLE, _constraints(probe), expected)


def test_pg_an_additional_not_null_in_the_database_fails(probe):
    with probe.begin() as conn:
        conn.execute(sa.text(f"ALTER TABLE {TABLE} ALTER COLUMN note SET NOT NULL"))
    with pytest.raises(AssertionError, match="colonnes NOT NULL"):
        without_verified_not_null(probe, TABLE, _constraints(probe), NOT_NULL)
    without_verified_not_null(probe, TABLE, _constraints(probe), NOT_NULL + ["note"])


def test_pg_a_dropped_not_null_in_the_database_fails(probe):
    with probe.begin() as conn:
        conn.execute(sa.text(f"ALTER TABLE {TABLE} ALTER COLUMN kind DROP NOT NULL"))
    with pytest.raises(AssertionError, match="colonnes NOT NULL"):
        without_verified_not_null(probe, TABLE, _constraints(probe), NOT_NULL)


def test_pg_a_missing_or_fabricated_not_null_row_fails(probe):
    raw = _constraints(probe)
    fabricated = raw + [("ci_probe_items_note_not_null", "n", "NOT NULL note", " ", " ")]
    with pytest.raises(AssertionError):
        without_verified_not_null(probe, TABLE, fabricated, NOT_NULL)
    if _version(probe) >= 180000:
        # Une ligne 'n' retirée des lignes lues (filtrage en amont) est détectée.
        truncated = [row for row in raw if row[0] != "ci_probe_items_kind_not_null"]
        with pytest.raises(AssertionError, match="contraintes NOT NULL"):
            without_verified_not_null(probe, TABLE, truncated, NOT_NULL)
    else:
        # Avant 18, aucune ligne 'n', même portant un nom plausible.
        with pytest.raises(AssertionError, match="avant PostgreSQL 18"):
            without_verified_not_null(probe, TABLE, raw + [expected_not_null_rows(TABLE, ["id"])[0]], NOT_NULL)


def test_pg_a_renamed_or_unvalidated_not_null_constraint_fails(probe):
    with probe.begin() as conn:
        if _version(probe) >= 180000:
            # PostgreSQL 18 : NOT NULL nommée explicitement, puis NOT NULL non validée.
            conn.execute(sa.text(f"ALTER TABLE {TABLE} ALTER COLUMN kind DROP NOT NULL"))
            conn.execute(sa.text(f"ALTER TABLE {TABLE} ADD CONSTRAINT custom_kind_nn NOT NULL kind"))
        else:
            conn.execute(sa.text(f"ALTER TABLE {TABLE} ADD CONSTRAINT custom_kind_nn CHECK (kind IS NOT NULL)"))
    with pytest.raises(AssertionError):
        assert without_verified_not_null(probe, TABLE, _constraints(probe), NOT_NULL) == EXPECTED_OTHERS
    if _version(probe) >= 180000:
        with probe.begin() as conn:
            conn.execute(sa.text(f"ALTER TABLE {TABLE} DROP CONSTRAINT custom_kind_nn"))
            conn.execute(sa.text(f"ALTER TABLE {TABLE} ADD CONSTRAINT ci_probe_items_kind_not_null "
                                 "NOT NULL kind NOT VALID"))
        with pytest.raises(AssertionError):
            without_verified_not_null(probe, TABLE, _constraints(probe), NOT_NULL)


def test_pg_long_names_follow_the_server_truncation(pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(sa.text(f"CREATE TABLE {LONG_TABLE} (id integer PRIMARY KEY, {LONG_COLUMN} uuid NOT NULL)"))
    try:
        rest = without_verified_not_null(pg_engine, LONG_TABLE, _constraints(pg_engine, LONG_TABLE),
                                         ["id", LONG_COLUMN])
        assert rest == [(f"{LONG_TABLE}_pkey", "p", "PRIMARY KEY (id)", " ", " ")]
    finally:
        _reset_schema(pg_engine)
