"""Tests de R1-C1 : migration 0011_assistant_deliveries + modèle
AssistantDelivery.

Même organisation que les tests des migrations 0002 à 0010 (dont on
réutilise les helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, intégrité de 0001
   à 0010, SQL PostgreSQL généré en mode offline (upgrade et downgrade),
   métadonnées du modèle (colonnes, types, nullabilité, PK, FK, CHECK,
   UNIQUE, aucun index supplémentaire, aucun server_default, aucune
   relationship).

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_r1c1_test \\
           python -m pytest tests/test_migration_0011_assistant_deliveries.py

Doctrine : 0011 est strictement EXPAND et sans backfill ; elle crée UNE
table runtime (assistant_deliveries), qui n'est ni un SupportTrace ni un
CognitiveEvent, et ne touche à aucune autre table.
"""
import hashlib
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from core.db import Base
from core.models import AssistantDelivery, User
from tests.test_migration_0002_analysis_sessions import (
    BASELINE,
    BASELINE_SHA256,
    REPO_ROOT,
    T1A,
    _reset_schema,
    _run_alembic,
    _script_directory,
    _snapshot,
    _tables,
    _version,
    pg_engine,  # noqa: F401 — fixture
    pg_url,  # noqa: F401 — fixture
)
from tests.test_migration_0003_analysis_session_links import T1A_SHA256, T1B1
from tests.test_migration_0004_drop_company_analyses import (
    R1C1_TABLES,
    SESSION_ID,
    T1B1_SHA256,
    T1C2,
    _code_tokens,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import (
    OTHER_USER,
    T1C2_SHA256,
    T2A,
    USER,
    _catalog,
    _global_catalog,
    _upgrade_head_with_users,
)
from tests.test_migration_0006_observation_layer import T2A_SHA256, T3A
from tests.test_migration_0007_pedagogical_taxonomy import T3A_SHA256, T4A, _assert_absent
from tests.test_migration_0008_longitudinal_relations import T4A_SHA256, T5A, _upgrade_to_0007_with_data
from tests.test_migration_0009_competency_inference_state import T5A_SHA256, T6A, _seed_t5
from tests.test_migration_0010_r1b_event_idempotence import (
    ALL_TABLES,
    CONVERSATIONS,
    EXISTING,
    R1B,
    R1B_TABLES,
    T6A_SHA256,
    _insert_event,
)

R1C1 = "0011_assistant_deliveries"
# sha256 de alembic/versions/0010_r1b_event_idempotence.py tel que mergé sur
# main (e6d6300, PR #212 ; 0010 introduite par R1-B, 3eb10dd).
R1B_SHA256 = "1166f006421ce19cf66b2401383f57cda001edb68b203ace77dd11045f22f9d7"

DELIVERIES = "assistant_deliveries"
assert R1C1_TABLES == {DELIVERIES}
TURN_UNIQUE = "uq_assistant_deliveries_turn_ordinal"
STATUS_CHECK = "ck_assistant_deliveries_status"
ORDINAL_CHECK = "ck_assistant_deliveries_delivery_ordinal"
COLUMNS = [
    "id", "user_id", "conversation_key", "surface", "source_user_turn_id", "delivery_ordinal",
    "analysis_session_id", "request_fingerprint", "visible_content_fingerprint", "response_payload",
    "private_metadata", "status", "delivery_schema_version", "generated_at", "delivered_at",
]
NULLABLE = {"analysis_session_id", "delivered_at"}
UNIQUE_COLUMNS = ["user_id", "conversation_key", "surface", "source_user_turn_id", "delivery_ordinal"]
HEAD_TABLES = EXISTING | R1B_TABLES | R1C1_TABLES
PRE_R1C1_TABLES = ALL_TABLES | R1B_TABLES


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_exactly_0001_to_0011():
    """0001 -> ... -> 0010 -> 0011, tête unique = 0011 ; 0011 est la seule
    migration ajoutée par R1-C1."""
    script = _script_directory()
    assert script.get_heads() == [R1C1]
    assert script.get_bases() == [BASELINE]
    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    chain = (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A, T6A, R1B, R1C1)
    assert set(revisions) == set(chain)
    assert revisions[R1C1].down_revision == R1B
    assert revisions[R1B].down_revision == T6A
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [f"{rev}.py" for rev in chain]
    assert len(files) == 11


def test_revision_id_fits_alembic_version_column():
    """alembic_version.version_num est un VARCHAR(32)."""
    assert len(R1C1) == 25
    for rev in _script_directory().walk_revisions():
        assert len(rev.revision) <= 32, rev.revision


def test_0001_to_0010_files_are_unchanged():
    """Migrations historiques immuables."""
    versions = REPO_ROOT / "alembic" / "versions"
    for rev, expected in (
        (BASELINE, BASELINE_SHA256),
        (T1A, T1A_SHA256),
        (T1B1, T1B1_SHA256),
        (T1C2, T1C2_SHA256),
        (T2A, T2A_SHA256),
        (T3A, T3A_SHA256),
        (T4A, T4A_SHA256),
        (T5A, T5A_SHA256),
        (T6A, T6A_SHA256),
        (R1B, R1B_SHA256),
    ):
        assert hashlib.sha256((versions / f"{rev}.py").read_bytes()).hexdigest() == expected, rev


def test_offline_sql_of_0011_upgrade_is_exactly_one_table():
    """Une table, ses deux CHECK nommés, trois FK NO ACTION et une UNIQUE
    nommée ; aucune donnée, aucun index supplémentaire, ni trigger, ni
    fonction, ni ENUM, ni server_default."""
    sql = _run_alembic("postgresql://offline@localhost/offline", "upgrade", f"{R1B}:{R1C1}", "--sql").stdout
    assert _statements(sql) == [
        f"CREATE TABLE {DELIVERIES} ( id UUID NOT NULL, user_id VARCHAR NOT NULL, conversation_key VARCHAR NOT NULL, "
        "surface VARCHAR NOT NULL, source_user_turn_id UUID NOT NULL, delivery_ordinal INTEGER NOT NULL, "
        "analysis_session_id UUID, request_fingerprint VARCHAR NOT NULL, visible_content_fingerprint VARCHAR NOT NULL, "
        "response_payload JSONB NOT NULL, private_metadata JSONB NOT NULL, status VARCHAR NOT NULL, "
        "delivery_schema_version VARCHAR NOT NULL, generated_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "delivered_at TIMESTAMP WITH TIME ZONE, PRIMARY KEY (id), "
        f"CONSTRAINT {STATUS_CHECK} CHECK (status IN ('pending', 'delivered')), "
        f"CONSTRAINT {ORDINAL_CHECK} CHECK (delivery_ordinal >= 1), "
        "FOREIGN KEY(user_id) REFERENCES users (id), "
        "FOREIGN KEY(conversation_key) REFERENCES conversation_identities (conversation_key), "
        "FOREIGN KEY(analysis_session_id) REFERENCES analysis_sessions (id), "
        f"CONSTRAINT {TURN_UNIQUE} UNIQUE (user_id, conversation_key, surface, source_user_turn_id, delivery_ordinal) )",
        # Seule écriture de données : la version Alembic.
        f"UPDATE alembic_version SET version_num='{R1C1}' WHERE alembic_version.version_num = '{R1B}'",
    ]
    upper = sql.upper()
    for forbidden in ("ALTER TABLE", "DROP ", "INSERT", "DELETE", "CREATE INDEX", "CREATE TYPE", "CREATE EXTENSION",
                      "CREATE TRIGGER", "CREATE FUNCTION", "CREATE PROCEDURE", "CREATE VIEW", "CREATE RULE",
                      "DEFAULT", "ON DELETE", "ON UPDATE", "CASCADE", "ENUM", "SEQUENCE", "SERIAL"):
        assert forbidden not in upper, forbidden


def test_offline_sql_of_0011_downgrade_reverts_exactly_r1c1():
    sql = _run_alembic("postgresql://offline@localhost/offline", "downgrade", f"{R1C1}:{R1B}", "--sql").stdout
    assert _statements(sql) == [
        f"DROP TABLE {DELIVERIES}",
        f"UPDATE alembic_version SET version_num='{R1B}' WHERE alembic_version.version_num = '{R1C1}'",
    ]
    assert "CASCADE" not in sql.upper()


def test_metadata_declares_exactly_the_assistant_deliveries_table_in_addition():
    assert set(Base.metadata.tables) == PRE_R1C1_TABLES | R1C1_TABLES
    assert AssistantDelivery.__table__ is Base.metadata.tables[DELIVERIES]


def test_model_columns_types_and_nullability():
    table = AssistantDelivery.__table__
    assert [c.name for c in table.columns] == COLUMNS
    assert {c.name: c.nullable for c in table.columns} == {n: n in NULLABLE for n in COLUMNS}
    assert [c.name for c in table.primary_key.columns] == ["id"]
    for name in ("id", "source_user_turn_id", "analysis_session_id"):
        assert isinstance(table.c[name].type, sa.Uuid) and table.c[name].type.as_uuid, name
    for name in ("user_id", "conversation_key", "surface", "request_fingerprint", "visible_content_fingerprint",
                 "status", "delivery_schema_version"):
        assert type(table.c[name].type) is sa.String and table.c[name].type.length is None, name
    assert type(table.c.delivery_ordinal.type) is sa.Integer
    for name in ("response_payload", "private_metadata"):
        assert isinstance(table.c[name].type, postgresql.JSONB), name
        assert table.c[name].type.none_as_null is True, name
    for name in ("generated_at", "delivered_at"):
        assert isinstance(table.c[name].type, sa.DateTime) and table.c[name].type.timezone, name


def test_model_constraints_are_exactly_those_of_the_migration():
    table = AssistantDelivery.__table__
    assert sorted((fk.parent.name, fk.target_fullname, fk.ondelete, fk.onupdate) for fk in table.foreign_keys) == [
        ("analysis_session_id", "analysis_sessions.id", None, None),
        ("conversation_key", "conversation_identities.conversation_key", None, None),
        ("user_id", "users.id", None, None),
    ]
    checks = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert checks == {STATUS_CHECK: "status IN ('pending', 'delivered')", ORDINAL_CHECK: "delivery_ordinal >= 1"}
    uniques = [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
    assert [(u.name, [c.name for c in u.columns]) for u in uniques] == [(TURN_UNIQUE, UNIQUE_COLUMNS)]
    assert not table.indexes


def test_no_server_defaults_and_no_relationships():
    for column in AssistantDelivery.__table__.columns:
        assert column.server_default is None, column.name
        assert column.server_onupdate is None, column.name
        assert column.onupdate is None, column.name
    # Seul id a un défaut applicatif (UUID) ; tout le reste est renseigné
    # explicitement par core/assistant_delivery.py.
    assert {c.name for c in AssistantDelivery.__table__.columns if c.default is not None} == {"id"}
    assert not sa.inspect(AssistantDelivery).relationships


def test_users_and_registry_tables_are_untouched():
    assert [c.name for c in User.__table__.columns] == ["id", "level", "created_at"]
    registry = Base.metadata.tables[CONVERSATIONS]
    assert [c.name for c in registry.columns] == ["conversation_key", "user_id", "created_at"]


def test_migration_writes_no_data_and_backfills_nothing():
    source = (REPO_ROOT / "alembic" / "versions" / f"{R1C1}.py").read_text(encoding="utf-8")
    tokens = set(_code_tokens(source).split("\n"))
    for forbidden in ("execute", "bulk_insert", "server_default", "create_index", "inline_literal", "add_column",
                      "alter_column", "drop_column", "create_foreign_key", "create_unique_constraint"):
        assert forbidden not in tokens, forbidden


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
FP = "a" * 64


def _insert_delivery(conn, **overrides):
    values = {
        "id": uuid.uuid4(), "user_id": USER, "conversation_key": "conv-u", "surface": "decryptage",
        "source_user_turn_id": uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), "delivery_ordinal": 1,
        "analysis_session_id": None, "request_fingerprint": FP, "visible_content_fingerprint": FP,
        "response_payload": '{"success": true}', "private_metadata": '{"decryptage_step_marker": null}',
        "status": "pending", "delivery_schema_version": "assistant-delivery-v1", "generated_at": NOW,
        "delivered_at": None,
    }
    values.update(overrides)
    conn.execute(sa.text(
        f"INSERT INTO {DELIVERIES} (id, user_id, conversation_key, surface, source_user_turn_id, delivery_ordinal, "
        "analysis_session_id, request_fingerprint, visible_content_fingerprint, response_payload, private_metadata, "
        "status, delivery_schema_version, generated_at, delivered_at) VALUES (:id, :user_id, :conversation_key, "
        ":surface, :source_user_turn_id, :delivery_ordinal, :analysis_session_id, :request_fingerprint, "
        ":visible_content_fingerprint, CAST(:response_payload AS JSONB), CAST(:private_metadata AS JSONB), :status, "
        ":delivery_schema_version, :generated_at, :delivered_at)"
    ), values)
    return values["id"]


@pytest.fixture
def conn(pg_url, pg_engine):  # noqa: F811
    """Schéma = head (0011) + deux utilisateurs, une analysis_session et deux
    conversations enregistrées ; tout ce que fait le test est annulé."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            connection.execute(sa.text(f"INSERT INTO {CONVERSATIONS} VALUES ('conv-u', :u, now()), "
                                       "('conv-u2', :u2, now())"), {"u": USER, "u2": OTHER_USER})
            yield connection
        finally:
            trans.rollback()


def test_pg_upgrade_from_empty_database_to_head(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == R1C1
    assert _tables(pg_engine) == HEAD_TABLES
    assert _compare_metadata(pg_engine) == []
    catalog = _global_catalog(pg_engine)
    assert (catalog["enums"], catalog["triggers"], catalog["functions"]) == (0, 0, 0)
    assert not [s for s in catalog["sequences"] if DELIVERIES in s]
    assert _data(pg_engine, R1C1_TABLES) == {DELIVERIES: []}


def test_pg_catalog_of_assistant_deliveries(pg_url, pg_engine):  # noqa: F811
    _upgrade_head_with_users(pg_url, pg_engine)
    assert _catalog(pg_engine, {DELIVERIES})[DELIVERIES] == {
        "columns": [
            ("id", "uuid", "NO", None),
            ("user_id", "character varying", "NO", None),
            ("conversation_key", "character varying", "NO", None),
            ("surface", "character varying", "NO", None),
            ("source_user_turn_id", "uuid", "NO", None),
            ("delivery_ordinal", "integer", "NO", None),
            ("analysis_session_id", "uuid", "YES", None),
            ("request_fingerprint", "character varying", "NO", None),
            ("visible_content_fingerprint", "character varying", "NO", None),
            ("response_payload", "jsonb", "NO", None),
            ("private_metadata", "jsonb", "NO", None),
            ("status", "character varying", "NO", None),
            ("delivery_schema_version", "character varying", "NO", None),
            ("generated_at", "timestamp with time zone", "NO", None),
            ("delivered_at", "timestamp with time zone", "YES", None),
        ],
        "constraints": [
            ("assistant_deliveries_analysis_session_id_fkey", "f",
             "FOREIGN KEY (analysis_session_id) REFERENCES analysis_sessions(id)", "a", "a"),
            ("assistant_deliveries_conversation_key_fkey", "f",
             "FOREIGN KEY (conversation_key) REFERENCES conversation_identities(conversation_key)", "a", "a"),
            ("assistant_deliveries_pkey", "p", "PRIMARY KEY (id)", " ", " "),
            ("assistant_deliveries_user_id_fkey", "f", "FOREIGN KEY (user_id) REFERENCES users(id)", "a", "a"),
            (ORDINAL_CHECK, "c", "CHECK ((delivery_ordinal >= 1))", " ", " "),
            (STATUS_CHECK, "c",
             "CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'delivered'::character varying])::text[])))",
             " ", " "),
            (TURN_UNIQUE, "u",
             "UNIQUE (user_id, conversation_key, surface, source_user_turn_id, delivery_ordinal)", " ", " "),
        ],
        "indexes": ["assistant_deliveries_pkey", TURN_UNIQUE],
    }


def test_pg_status_check(conn):
    _insert_delivery(conn, status="delivered", delivered_at=NOW)
    for status in ("open", "read", "PENDING", ""):
        with pytest.raises(sa.exc.IntegrityError, match=STATUS_CHECK):
            with conn.begin_nested():
                _insert_delivery(conn, status=status, source_user_turn_id=uuid.uuid4())


def test_pg_ordinal_check(conn):
    _insert_delivery(conn, delivery_ordinal=2)
    for ordinal in (0, -1):
        with pytest.raises(sa.exc.IntegrityError, match=ORDINAL_CHECK):
            with conn.begin_nested():
                _insert_delivery(conn, delivery_ordinal=ordinal, source_user_turn_id=uuid.uuid4())


def test_pg_turn_identity_is_unique(conn):
    """Même (user, conversation, surface, tour, ordinal) : refusé, quel que
    soit le contenu ; un autre ordinal, tour ou une autre surface passe."""
    _insert_delivery(conn)
    with pytest.raises(sa.exc.IntegrityError, match=TURN_UNIQUE):
        with conn.begin_nested():
            _insert_delivery(conn, request_fingerprint="b" * 64, response_payload='{"other": 1}')
    _insert_delivery(conn, delivery_ordinal=2)
    _insert_delivery(conn, source_user_turn_id=uuid.uuid4())
    _insert_delivery(conn, surface="other-surface")
    # Même UUID de tour dans la conversation d'un autre utilisateur : distinct.
    _insert_delivery(conn, user_id=OTHER_USER, conversation_key="conv-u2")


def test_pg_foreign_keys_are_no_action(conn):
    for overrides, constraint in (
        ({"user_id": "ghost"}, "assistant_deliveries_user_id_fkey"),
        ({"conversation_key": "unregistered"}, "assistant_deliveries_conversation_key_fkey"),
        ({"analysis_session_id": uuid.uuid4()}, "assistant_deliveries_analysis_session_id_fkey"),
    ):
        with pytest.raises(sa.exc.IntegrityError, match=constraint):
            with conn.begin_nested():
                _insert_delivery(conn, source_user_turn_id=uuid.uuid4(), **overrides)
    _insert_delivery(conn, analysis_session_id=SESSION_ID)
    for statement, constraint in (
        ("DELETE FROM analysis_sessions WHERE id = :s", "assistant_deliveries_analysis_session_id_fkey"),
        (f"DELETE FROM {CONVERSATIONS} WHERE conversation_key = 'conv-u'", "assistant_deliveries_conversation_key_fkey"),
    ):
        with pytest.raises(sa.exc.IntegrityError, match=constraint):
            with conn.begin_nested():
                conn.execute(sa.text(statement), {"s": SESSION_ID})


def test_pg_upgrade_0010_to_0011_preserves_everything_then_downgrade(pg_url, pg_engine):  # noqa: F811
    """0010 -> 0011 : toutes les tables existantes gardent schéma, catalogue
    et données (aucun backfill) ; seule assistant_deliveries apparaît, vide.
    Le downgrade restaure exactement 0010 ; le ré-upgrade est identique."""
    _upgrade_to_0007_with_data(pg_url, pg_engine)
    _run_alembic(pg_url, "upgrade", T5A)
    _seed_t5(pg_engine)
    _run_alembic(pg_url, "upgrade", R1B)
    with pg_engine.begin() as connection:
        user_id = connection.execute(sa.text("SELECT user_id FROM cognitive_events LIMIT 1")).scalar_one()
        connection.execute(sa.text(f"INSERT INTO {CONVERSATIONS} VALUES ('c-r1b', :u, now())"), {"u": user_id})
        _insert_event(connection, user_id=user_id, event_dedup_key="d" * 64)
    existing = EXISTING | R1B_TABLES
    schema_0010 = _snapshot(pg_engine, existing)
    catalog_0010 = _catalog(pg_engine, existing)
    global_0010 = _global_catalog(pg_engine)
    data_0010 = _data(pg_engine, PRE_R1C1_TABLES)
    assert all(data_0010[t] for t in ("cognitive_events", "support_traces", CONVERSATIONS, "analysis_sessions"))

    # --- upgrade 0010 -> 0011 --------------------------------------------
    _run_alembic(pg_url, "upgrade", R1C1)
    assert _version(pg_engine) == R1C1
    assert _tables(pg_engine) == HEAD_TABLES
    assert _snapshot(pg_engine, existing) == schema_0010
    assert _catalog(pg_engine, existing) == catalog_0010
    assert _global_catalog(pg_engine) == global_0010
    assert _data(pg_engine, PRE_R1C1_TABLES) == data_0010
    assert _data(pg_engine, R1C1_TABLES) == {DELIVERIES: []}
    assert _compare_metadata(pg_engine) == []
    schema_r1c1 = _snapshot(pg_engine, R1C1_TABLES)
    catalog_r1c1 = _catalog(pg_engine, R1C1_TABLES)

    # Donnée R1-C1 (supprimée par le downgrade avec sa table).
    with pg_engine.begin() as connection:
        _insert_delivery(connection, user_id=user_id, conversation_key="c-r1b")

    # --- downgrade 0011 -> 0010 ------------------------------------------
    _run_alembic(pg_url, "downgrade", R1B)
    assert _version(pg_engine) == R1B
    assert _tables(pg_engine) == existing
    _assert_absent(pg_engine, [DELIVERIES, TURN_UNIQUE])
    assert _snapshot(pg_engine, existing) == schema_0010
    assert _catalog(pg_engine, existing) == catalog_0010
    assert _global_catalog(pg_engine) == global_0010
    assert _data(pg_engine, PRE_R1C1_TABLES) == data_0010

    # --- ré-upgrade 0010 -> 0011 -----------------------------------------
    _run_alembic(pg_url, "upgrade", R1C1)
    assert _snapshot(pg_engine, R1C1_TABLES) == schema_r1c1
    assert _catalog(pg_engine, R1C1_TABLES) == catalog_r1c1
    assert _data(pg_engine, PRE_R1C1_TABLES | R1C1_TABLES) == {**data_0010, DELIVERIES: []}
    assert _compare_metadata(pg_engine) == []
