"""Tests du préflight de schéma (scripts/schema_preflight.py).

Aucun test ici ne se connecte à PostgreSQL ni à Railway : la logique est
volontairement séparée en trois étages purs (collecte via un
faux-inspector, normalisation, comparaison) testables sans base réelle.
"""
from copy import deepcopy

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from scripts.schema_preflight import (
	DATETIME_NAIVE,
	DATETIME_TZ,
	FLOAT,
	INTEGER,
	STRING,
	EXPECTED_MANIFEST,
	PreflightError,
	PreflightResult,
	collect_raw_snapshot,
	column_has_unexpected_server_default,
	column_is_autoincrement,
	compare_snapshot,
	format_report,
	main,
	normalize_database_url,
	normalize_snapshot,
	run_preflight,
	semantic_type,
	assert_transaction_read_only,
)


# --------------------------------------------------------------------------
# Fixtures : schéma "parfait" (conforme à la baseline) au format brut
# renvoyé par un sqlalchemy.engine.reflection.Inspector.
# --------------------------------------------------------------------------

def _col(name, sa_type, nullable, *, autoincrement=False, sequence_default=False, identity=False):
	default = f"nextval('{name}_seq'::regclass)" if sequence_default else None
	return {
		"name": name,
		"type": sa_type,
		"nullable": nullable,
		"default": default,
		"autoincrement": autoincrement,
		"identity": {"always": False} if identity else None,
	}


def _pk(*columns):
	return {"constrained_columns": list(columns), "name": None}


def _fk(columns, ref_table, ref_columns, ref_schema="public"):
	return {
		"constrained_columns": list(columns),
		"referred_schema": ref_schema,
		"referred_table": ref_table,
		"referred_columns": list(ref_columns),
		"name": None,
		"options": {},
	}


def _table(columns, pk, foreign_keys=None, unique_constraints=None, check_constraints=None, indexes=None):
	return {
		"columns": columns,
		"pk": pk,
		"foreign_keys": foreign_keys or [],
		"unique_constraints": unique_constraints or [],
		"check_constraints": check_constraints or [],
		"indexes": indexes or [],
	}


def _perfect_tables():
	return {
		"users": _table(
			[
				_col("id", sa.String(), False),
				_col("level", sa.String(), False),
				_col("created_at", sa.DateTime(), True),
			],
			_pk("id"),
		),
		"portfolio_positions": _table(
			[
				_col("id", sa.Integer(), False, autoincrement=True, sequence_default=True),
				_col("user_id", sa.String(), False),
				_col("ticker", sa.String(), False),
				_col("quantity", sa.Float(), False),
				_col("purchase_price", sa.Float(), True),
				_col("target_percent", sa.Float(), True),
				_col("envelope", sa.String(), True),
				_col("created_at", sa.DateTime(), True),
				_col("updated_at", sa.DateTime(), True),
			],
			_pk("id"),
			foreign_keys=[_fk(["user_id"], "users", ["id"])],
		),
		"company_analyses": _table(
			[
				_col("id", sa.Integer(), False, autoincrement=True, sequence_default=True),
				_col("user_id", sa.String(), False),
				_col("ticker", sa.String(), False),
				_col("current_step", sa.String(), False),
				_col("created_at", sa.DateTime(), True),
				_col("updated_at", sa.DateTime(), True),
			],
			_pk("id"),
			foreign_keys=[_fk(["user_id"], "users", ["id"])],
		),
		"investment_theses": _table(
			[
				_col("id", sa.Integer(), False, autoincrement=True, sequence_default=True),
				_col("user_id", sa.String(), False),
				_col("ticker", sa.String(), False),
				_col("thesis_text", sa.String(), False),
				_col("created_at", sa.DateTime(), True),
			],
			_pk("id"),
			foreign_keys=[_fk(["user_id"], "users", ["id"])],
		),
		"analysis_facts": _table(
			[
				_col("id", sa.Integer(), False, autoincrement=True, sequence_default=True),
				_col("user_id", sa.String(), False),
				_col("ticker", sa.String(), False),
				_col("fact_date", sa.DateTime(), True),
				_col("fact_type", sa.String(), False),
				_col("fact_value", sa.Float(), True),
			],
			_pk("id"),
			foreign_keys=[_fk(["user_id"], "users", ["id"])],
		),
		"user_statements": _table(
			[
				_col("id", sa.Integer(), False, autoincrement=True, sequence_default=True),
				_col("user_id", sa.String(), False),
				_col("ticker", sa.String(), False),
				_col("step", sa.String(), True),
				_col("statement_date", sa.DateTime(), True),
				_col("statement_text", sa.String(), False),
			],
			_pk("id"),
			foreign_keys=[_fk(["user_id"], "users", ["id"])],
		),
	}


def _raw_snapshot(tables=None):
	return {"schema": "public", "tables": deepcopy(tables if tables is not None else _perfect_tables())}


def _mismatches_for(tables, dialect="postgresql"):
	normalized = normalize_snapshot(_raw_snapshot(tables))
	return compare_snapshot(normalized, EXPECTED_MANIFEST, dialect=dialect)


# --------------------------------------------------------------------------
# MATCH — schéma parfaitement conforme
# --------------------------------------------------------------------------

def test_perfect_schema_matches():
	assert _mismatches_for(_perfect_tables()) == []


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------

def test_missing_table():
	tables = _perfect_tables()
	del tables["user_statements"]
	assert "missing table: user_statements" in _mismatches_for(tables)


def test_unexpected_table():
	tables = _perfect_tables()
	tables["extra_table"] = deepcopy(tables["users"])
	assert "unexpected table: extra_table" in _mismatches_for(tables)


def test_alembic_version_present_is_mismatch():
	tables = _perfect_tables()
	tables["alembic_version"] = _table(
		[_col("version_num", sa.String(), False)],
		_pk("version_num"),
	)
	mismatches = _mismatches_for(tables)
	assert any(m.startswith("unexpected table: alembic_version") for m in mismatches)
	# un seul message pour alembic_version, pas de doublon avec la règle générique
	assert sum(1 for m in mismatches if "alembic_version" in m) == 1


# --------------------------------------------------------------------------
# Colonnes
# --------------------------------------------------------------------------

def test_missing_column():
	tables = _perfect_tables()
	tables["users"]["columns"] = [c for c in tables["users"]["columns"] if c["name"] != "level"]
	assert "missing column: users.level" in _mismatches_for(tables)


def test_unexpected_column():
	tables = _perfect_tables()
	tables["users"]["columns"].append(_col("nickname", sa.String(), True))
	assert "unexpected column: users.nickname" in _mismatches_for(tables)


def test_wrong_type():
	tables = _perfect_tables()
	for c in tables["users"]["columns"]:
		if c["name"] == "level":
			c["type"] = sa.Integer()
	mismatches = _mismatches_for(tables)
	assert "wrong type: users.level (expected STRING, got INTEGER)" in mismatches


def test_text_instead_of_varchar_is_mismatch():
	tables = _perfect_tables()
	for c in tables["users"]["columns"]:
		if c["name"] == "level":
			c["type"] = pg.TEXT()
	mismatches = _mismatches_for(tables)
	assert "wrong type: users.level (expected STRING, got UNKNOWN:TEXT)" in mismatches


def test_char_instead_of_varchar_is_mismatch():
	tables = _perfect_tables()
	for c in tables["users"]["columns"]:
		if c["name"] == "level":
			c["type"] = pg.CHAR(10)
	mismatches = _mismatches_for(tables)
	assert "wrong type: users.level (expected STRING, got UNKNOWN:CHAR)" in mismatches


def test_bigint_instead_of_integer_is_mismatch():
	tables = _perfect_tables()
	for c in tables["portfolio_positions"]["columns"]:
		if c["name"] == "id":
			c["type"] = pg.BIGINT()
	mismatches = _mismatches_for(tables)
	assert "wrong type: portfolio_positions.id (expected INTEGER, got UNKNOWN:BIGINT)" in mismatches


def test_smallint_instead_of_integer_is_mismatch():
	tables = _perfect_tables()
	for c in tables["portfolio_positions"]["columns"]:
		if c["name"] == "id":
			c["type"] = pg.SMALLINT()
	mismatches = _mismatches_for(tables)
	assert "wrong type: portfolio_positions.id (expected INTEGER, got UNKNOWN:SMALLINT)" in mismatches


def test_real_instead_of_double_precision_is_mismatch():
	tables = _perfect_tables()
	for c in tables["portfolio_positions"]["columns"]:
		if c["name"] == "quantity":
			c["type"] = pg.REAL()
	mismatches = _mismatches_for(tables)
	assert "wrong type: portfolio_positions.quantity (expected FLOAT, got UNKNOWN:REAL)" in mismatches


def test_double_precision_pg_specific_type_matches_expected_float():
	tables = _perfect_tables()
	for c in tables["portfolio_positions"]["columns"]:
		if c["name"] == "quantity":
			c["type"] = pg.DOUBLE_PRECISION()
	assert _mismatches_for(tables) == []


def test_varchar_pg_specific_type_matches_expected_string():
	tables = _perfect_tables()
	for c in tables["users"]["columns"]:
		if c["name"] == "level":
			c["type"] = pg.VARCHAR()
	assert _mismatches_for(tables) == []


def test_wrong_nullable():
	tables = _perfect_tables()
	for c in tables["users"]["columns"]:
		if c["name"] == "level":
			c["nullable"] = True
	mismatches = _mismatches_for(tables)
	assert "wrong nullable: users.level (expected nullable=False, got True)" in mismatches


def test_timestamptz_instead_of_naive_is_wrong_type():
	tables = _perfect_tables()
	for c in tables["users"]["columns"]:
		if c["name"] == "created_at":
			c["type"] = sa.DateTime(timezone=True)
	mismatches = _mismatches_for(tables)
	assert "wrong type: users.created_at (expected DATETIME_NAIVE, got DATETIME_TZ)" in mismatches


# --------------------------------------------------------------------------
# Clé primaire / clé étrangère
# --------------------------------------------------------------------------

def test_wrong_pk():
	tables = _perfect_tables()
	tables["users"]["pk"] = _pk("level")
	mismatches = _mismatches_for(tables)
	assert any(m.startswith("wrong pk: users") for m in mismatches)


def test_missing_fk():
	tables = _perfect_tables()
	tables["portfolio_positions"]["foreign_keys"] = []
	mismatches = _mismatches_for(tables)
	assert "missing FK: portfolio_positions.user_id -> public.users.id" in mismatches


def test_incorrect_fk_target_is_missing_and_unexpected():
	tables = _perfect_tables()
	tables["portfolio_positions"]["foreign_keys"] = [_fk(["user_id"], "wrong_table", ["id"])]
	mismatches = _mismatches_for(tables)
	assert "missing FK: portfolio_positions.user_id -> public.users.id" in mismatches
	assert "unexpected FK: portfolio_positions.user_id -> public.wrong_table.id" in mismatches


def test_fk_pointing_to_a_different_schema_is_mismatch():
	tables = _perfect_tables()
	tables["portfolio_positions"]["foreign_keys"] = [
		_fk(["user_id"], "users", ["id"], ref_schema="other_schema")
	]
	mismatches = _mismatches_for(tables)
	assert "missing FK: portfolio_positions.user_id -> public.users.id" in mismatches
	assert "unexpected FK: portfolio_positions.user_id -> other_schema.users.id" in mismatches


def test_fk_referred_schema_none_is_equivalent_to_public():
	tables = _perfect_tables()
	# SQLAlchemy renvoie parfois referred_schema=None quand la table
	# référencée est dans le même schéma que la table inspectée.
	tables["portfolio_positions"]["foreign_keys"] = [_fk(["user_id"], "users", ["id"], ref_schema=None)]
	assert _mismatches_for(tables) == []


# --------------------------------------------------------------------------
# Index / UNIQUE / CHECK inattendus
# --------------------------------------------------------------------------

def test_unexpected_index():
	tables = _perfect_tables()
	tables["users"]["indexes"] = [{"name": "ix_users_level", "column_names": ["level"], "unique": False}]
	mismatches = _mismatches_for(tables)
	assert "unexpected index: users.ix_users_level (level)" in mismatches


def test_unexpected_unique():
	tables = _perfect_tables()
	tables["users"]["unique_constraints"] = [{"column_names": ["level"], "name": "uq_users_level"}]
	mismatches = _mismatches_for(tables)
	assert "unexpected unique: users(level)" in mismatches


def test_unexpected_check():
	tables = _perfect_tables()
	tables["users"]["check_constraints"] = [
		{"name": "ck_level", "sqltext": "level IN ('debutant', 'expert')"}
	]
	mismatches = _mismatches_for(tables)
	assert any(m.startswith("unexpected check: users:") for m in mismatches)


# --------------------------------------------------------------------------
# Server defaults — la baseline n'en déclare aucun hors auto-incrément
# --------------------------------------------------------------------------

def test_unexpected_literal_default_on_business_column_is_mismatch():
	tables = _perfect_tables()
	for c in tables["users"]["columns"]:
		if c["name"] == "level":
			c["default"] = "'debutant'::character varying"
	mismatches = _mismatches_for(tables)
	assert "unexpected server default: users.level" in mismatches


def test_unexpected_now_default_on_business_column_is_mismatch():
	tables = _perfect_tables()
	for c in tables["users"]["columns"]:
		if c["name"] == "created_at":
			c["default"] = "now()"
	mismatches = _mismatches_for(tables)
	assert "unexpected server default: users.created_at" in mismatches


def test_unexpected_default_on_non_id_column_of_another_table_is_mismatch():
	tables = _perfect_tables()
	for c in tables["portfolio_positions"]["columns"]:
		if c["name"] == "ticker":
			c["default"] = "'AAPL'::character varying"
	mismatches = _mismatches_for(tables)
	assert "unexpected server default: portfolio_positions.ticker" in mismatches


def test_no_default_anywhere_matches():
	assert _mismatches_for(_perfect_tables()) == []


def test_nextval_default_on_autoincrement_id_is_not_an_unexpected_default():
	tables = _perfect_tables()
	for c in tables["portfolio_positions"]["columns"]:
		if c["name"] == "id":
			assert c["default"] == "nextval('id_seq'::regclass)"  # posé par la fixture
	assert column_has_unexpected_server_default(
		next(c for c in tables["portfolio_positions"]["columns"] if c["name"] == "id")
	) is False
	assert _mismatches_for(tables) == []


def test_identity_on_autoincrement_id_is_not_an_unexpected_default():
	tables = _perfect_tables()
	for c in tables["portfolio_positions"]["columns"]:
		if c["name"] == "id":
			c["autoincrement"] = False
			c["default"] = None
			c["identity"] = {"always": False}
	assert column_has_unexpected_server_default(
		next(c for c in tables["portfolio_positions"]["columns"] if c["name"] == "id")
	) is False
	assert _mismatches_for(tables) == []


# --------------------------------------------------------------------------
# Auto-incrément
# --------------------------------------------------------------------------

def test_missing_autoincrement():
	tables = _perfect_tables()
	for c in tables["portfolio_positions"]["columns"]:
		if c["name"] == "id":
			c["autoincrement"] = False
			c["default"] = None
			c["identity"] = None
	mismatches = _mismatches_for(tables)
	assert "missing autoincrement: portfolio_positions.id" in mismatches


def test_autoincrement_detected_via_identity_column():
	tables = _perfect_tables()
	for c in tables["portfolio_positions"]["columns"]:
		if c["name"] == "id":
			c["autoincrement"] = False
			c["default"] = None
			c["identity"] = {"always": False}
	# une colonne IDENTITY est une forme valide d'auto-incrément : pas de mismatch
	assert _mismatches_for(tables) == []


def test_autoincrement_detected_via_sequence_default_regardless_of_exact_name():
	tables = _perfect_tables()
	for c in tables["portfolio_positions"]["columns"]:
		if c["name"] == "id":
			c["autoincrement"] = False
			# nom de séquence non standard : toujours accepté, pas de comparaison littérale
			c["default"] = "nextval('some_custom_sequence_name_42'::regclass)"
	assert _mismatches_for(tables) == []


# --------------------------------------------------------------------------
# Dialecte
# --------------------------------------------------------------------------

def test_wrong_dialect_is_mismatch():
	mismatches = _mismatches_for(_perfect_tables(), dialect="sqlite")
	assert "wrong dialect: expected postgresql, got 'sqlite'" in mismatches


# --------------------------------------------------------------------------
# semantic_type()
# --------------------------------------------------------------------------

def test_semantic_type_string_variants():
	assert semantic_type(sa.String()) == STRING
	assert semantic_type(sa.VARCHAR(255)) == STRING
	assert semantic_type(pg.VARCHAR()) == STRING


def test_semantic_type_integer():
	assert semantic_type(sa.Integer()) == INTEGER
	assert semantic_type(pg.INTEGER()) == INTEGER


def test_semantic_type_float_variants():
	assert semantic_type(sa.Float()) == FLOAT
	assert semantic_type(pg.DOUBLE_PRECISION()) == FLOAT


def test_semantic_type_datetime_naive_vs_timezone():
	assert semantic_type(sa.DateTime()) == DATETIME_NAIVE
	assert semantic_type(sa.DateTime(timezone=True)) == DATETIME_TZ
	assert semantic_type(pg.TIMESTAMP(timezone=False)) == DATETIME_NAIVE
	assert semantic_type(pg.TIMESTAMP(timezone=True)) == DATETIME_TZ


def test_semantic_type_unknown_falls_back_without_crashing():
	assert semantic_type(sa.JSON()).startswith("UNKNOWN:")


# --------------------------------------------------------------------------
# column_is_autoincrement()
# --------------------------------------------------------------------------

def test_autoincrement_via_flag():
	assert column_is_autoincrement({"autoincrement": True}) is True


def test_autoincrement_via_identity_dict():
	assert column_is_autoincrement({"autoincrement": False, "identity": {"always": False}}) is True


def test_autoincrement_via_sequence_default():
	assert column_is_autoincrement(
		{"autoincrement": False, "default": "nextval('foo_id_seq'::regclass)"}
	) is True


def test_autoincrement_absent():
	assert column_is_autoincrement({"autoincrement": False, "default": None, "identity": None}) is False


# --------------------------------------------------------------------------
# column_has_unexpected_server_default()
# --------------------------------------------------------------------------

def test_no_default_is_not_unexpected():
	assert column_has_unexpected_server_default({"default": None}) is False


def test_nextval_default_is_not_unexpected():
	assert column_has_unexpected_server_default({"default": "nextval('foo_id_seq'::regclass)"}) is False


def test_literal_default_is_unexpected():
	assert column_has_unexpected_server_default({"default": "'debutant'::character varying"}) is True


def test_now_default_is_unexpected():
	assert column_has_unexpected_server_default({"default": "now()"}) is True


# --------------------------------------------------------------------------
# normalize_database_url() — même logique que core/db.py
# --------------------------------------------------------------------------

def test_normalize_database_url_rewrites_postgres_scheme():
	assert normalize_database_url("postgres://u:p@host/db") == "postgresql://u:p@host/db"


def test_normalize_database_url_leaves_postgresql_scheme_untouched():
	assert normalize_database_url("postgresql://u:p@host/db") == "postgresql://u:p@host/db"


def test_normalize_database_url_leaves_other_schemes_untouched():
	assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"


# --------------------------------------------------------------------------
# assert_transaction_read_only() — sans connexion réelle
# --------------------------------------------------------------------------

class _FakeResult:
	def __init__(self, value):
		self._value = value

	def scalar(self):
		return self._value


class _FakeConnection:
	def __init__(self, read_only_value):
		self._read_only_value = read_only_value
		self.executed = []

	def execute(self, stmt):
		sql = str(stmt)
		self.executed.append(sql)
		if "SHOW transaction_read_only" in sql:
			return _FakeResult(self._read_only_value)
		return _FakeResult(None)


def test_assert_transaction_read_only_confirmed_does_not_raise():
	conn = _FakeConnection("on")
	assert_transaction_read_only(conn)
	assert any("SET TRANSACTION READ ONLY" in s for s in conn.executed)


def test_assert_transaction_read_only_rejected_raises_preflight_error():
	conn = _FakeConnection("off")
	with pytest.raises(PreflightError):
		assert_transaction_read_only(conn)


# --------------------------------------------------------------------------
# collect_raw_snapshot() — avec un faux inspector (pas de base réelle)
# --------------------------------------------------------------------------

class _FakeInspector:
	"""Reproduit la surface d'API de sqlalchemy.engine.reflection.Inspector
	utilisée par collect_raw_snapshot, sans connexion réelle."""

	def __init__(self, tables):
		self._tables = tables

	def get_table_names(self, schema=None):
		return list(self._tables.keys())

	def get_columns(self, table_name, schema=None):
		return self._tables[table_name]["columns"]

	def get_pk_constraint(self, table_name, schema=None):
		return self._tables[table_name]["pk"]

	def get_foreign_keys(self, table_name, schema=None):
		return self._tables[table_name]["foreign_keys"]

	def get_unique_constraints(self, table_name, schema=None):
		return self._tables[table_name]["unique_constraints"]

	def get_check_constraints(self, table_name, schema=None):
		return self._tables[table_name]["check_constraints"]

	def get_indexes(self, table_name, schema=None):
		return self._tables[table_name]["indexes"]


def test_collect_raw_snapshot_builds_expected_structure():
	users_table = _perfect_tables()["users"]
	inspector = _FakeInspector({"users": users_table})
	raw = collect_raw_snapshot(inspector, schema="public")
	assert raw["schema"] == "public"
	assert set(raw["tables"].keys()) == {"users"}
	assert raw["tables"]["users"]["columns"] == users_table["columns"]


def test_full_pipeline_via_fake_inspector_matches():
	inspector = _FakeInspector(_perfect_tables())
	raw = collect_raw_snapshot(inspector, schema="public")
	normalized = normalize_snapshot(raw)
	mismatches = compare_snapshot(normalized, EXPECTED_MANIFEST, dialect="postgresql")
	assert mismatches == []


def test_full_pipeline_via_fake_inspector_detects_missing_table():
	tables = _perfect_tables()
	del tables["analysis_facts"]
	inspector = _FakeInspector(tables)
	raw = collect_raw_snapshot(inspector, schema="public")
	normalized = normalize_snapshot(raw)
	mismatches = compare_snapshot(normalized, EXPECTED_MANIFEST, dialect="postgresql")
	assert "missing table: analysis_facts" in mismatches


# --------------------------------------------------------------------------
# format_report()
# --------------------------------------------------------------------------

def test_format_report_match():
	result = PreflightResult(match=True, mismatches=[], dialect="postgresql")
	assert format_report(result) == "SCHEMA MATCH — production compatible avec 0001_current_oryx_baseline"


def test_format_report_mismatch_lists_every_issue():
	result = PreflightResult(
		match=False,
		mismatches=["missing table: users", "unexpected column: portfolio_positions.foo"],
		dialect="postgresql",
	)
	report = format_report(result)
	assert report.startswith("SCHEMA MISMATCH")
	assert "missing table: users" in report
	assert "unexpected column: portfolio_positions.foo" in report


# --------------------------------------------------------------------------
# Comportement opérationnel — sans DATABASE_URL, sans connexion réelle
# --------------------------------------------------------------------------

def test_run_preflight_raises_without_database_url():
	with pytest.raises(PreflightError):
		run_preflight(database_url="")


def test_main_returns_exit_code_2_without_database_url(monkeypatch, capsys):
	monkeypatch.delenv("DATABASE_URL", raising=False)
	exit_code = main()
	assert exit_code == 2
	captured = capsys.readouterr()
	assert "PREFLIGHT ERROR" in captured.err
	# jamais de fuite de secret même dans le cas d'erreur
	assert "DATABASE_URL=" not in captured.err
