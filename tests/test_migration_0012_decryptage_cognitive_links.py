"""Tests de R1-C2 : migration 0012_decryptage_cognitive_links + modèle
DecryptageCognitiveLink.

Même organisation que les tests des migrations 0002 à 0011 (dont on
réutilise les helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, intégrité de 0001
   à 0011, SQL PostgreSQL généré en mode offline (upgrade et downgrade),
   métadonnées du modèle (colonnes, types, nullabilité, PK, FK, CHECK,
   index unique partiel, aucun server_default, aucune relationship).

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_r1c2_test \\
           python -m pytest tests/test_migration_0012_decryptage_cognitive_links.py

Doctrine : 0012 est strictement EXPAND et sans backfill ; elle crée UNE
table runtime spécifique Décrypter (decryptage_cognitive_links), qui n'est
ni une preuve ni une observation, et ne touche à aucune autre table. Les
AssistantDelivery R1-C1 historiques restent sans lien (legacy).
"""
import hashlib
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from core.db import Base
from core.models import AssistantDelivery, CognitiveEvent, DecryptageCognitiveLink, SupportTrace
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
    R1C2_INDEXES,
    R1C2_TABLES,
    T1B1_SHA256,
    T1C2,
    _code_tokens,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import (
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
    CONVERSATIONS,
    EXISTING,
    R1B,
    R1B_TABLES,
    T6A_SHA256,
    _insert_event,
)
from tests.test_migration_0011_assistant_deliveries import (
    PRE_R1C1_TABLES,
    R1B_SHA256,
    R1C1,
    _insert_delivery,
)

R1C2 = "0012_decryptage_cognitive_links"
# sha256 de alembic/versions/0011_assistant_deliveries.py tel que mergé sur
# main (c96f87d, PR #213 ; 0011 introduite par R1-C1, 688a263).
R1C1_SHA256 = "f1462a5d4456de3758b449fba7b0f5a98b41645bff24d1d0559824589735382b"

LINKS = "decryptage_cognitive_links"
assert R1C2_TABLES == {LINKS}
OPENING_INDEX = "uq_decryptage_cognitive_links_opening_event"
assert R1C2_INDEXES == {OPENING_INDEX}
CHECKS = {
    "ck_decryptage_cognitive_links_capture_state",
    "ck_decryptage_cognitive_links_input_action",
    "ck_decryptage_cognitive_links_response_action",
    "ck_decryptage_cognitive_links_capture_state_fields",
    "ck_decryptage_cognitive_links_input_event",
    "ck_decryptage_cognitive_links_response_refs",
}
COLUMNS = [
    "assistant_delivery_id", "capture_version", "input_action", "input_event_id", "context_exit_event_id",
    "capture_state", "response_action", "response_event_id", "support_trace_id", "created_at", "captured_at",
]
NULLABLE = {"input_event_id", "context_exit_event_id", "response_action", "response_event_id", "support_trace_id",
            "captured_at"}
INPUT_ACTIONS = ("no_open_event", "no_user_contribution", "contribution_appended", "event_closed_context_change",
                 "event_abandoned_context_change")
RESPONSE_ACTIONS = ("opened_event", "continued_event", "continued_without_boundary_signal", "transitioned_event",
                    "closed_terminal", "no_cognitive_action")
HEAD_TABLES = EXISTING | R1B_TABLES | R1C1_TABLES | R1C2_TABLES
PRE_R1C2_TABLES = PRE_R1C1_TABLES | R1C1_TABLES


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_exactly_0001_to_0012():
    """0001 -> ... -> 0011 -> 0012, tête unique = 0012 ; 0012 est la seule
    migration ajoutée par R1-C2."""
    script = _script_directory()
    assert script.get_heads() == [R1C2]
    assert script.get_bases() == [BASELINE]
    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    chain = (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A, T6A, R1B, R1C1, R1C2)
    assert set(revisions) == set(chain)
    assert revisions[R1C2].down_revision == R1C1
    assert revisions[R1C1].down_revision == R1B
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [f"{rev}.py" for rev in chain]
    assert len(files) == 12


def test_revision_id_fits_alembic_version_column():
    assert len(R1C2) == 31
    for rev in _script_directory().walk_revisions():
        assert len(rev.revision) <= 32, rev.revision


def test_0001_to_0011_files_are_unchanged():
    """Migrations historiques immuables."""
    versions = REPO_ROOT / "alembic" / "versions"
    for rev, expected in (
        (BASELINE, BASELINE_SHA256), (T1A, T1A_SHA256), (T1B1, T1B1_SHA256), (T1C2, T1C2_SHA256),
        (T2A, T2A_SHA256), (T3A, T3A_SHA256), (T4A, T4A_SHA256), (T5A, T5A_SHA256), (T6A, T6A_SHA256),
        (R1B, R1B_SHA256), (R1C1, R1C1_SHA256),
    ):
        assert hashlib.sha256((versions / f"{rev}.py").read_bytes()).hexdigest() == expected, rev


def test_offline_sql_of_0012_upgrade_is_exactly_one_table_and_its_partial_index():
    sql = _run_alembic("postgresql://offline@localhost/offline", "upgrade", f"{R1C1}:{R1C2}", "--sql").stdout
    statements = _statements(sql)
    assert len(statements) == 3
    create, index, version = statements
    assert create.startswith(
        f"CREATE TABLE {LINKS} ( assistant_delivery_id UUID NOT NULL, capture_version VARCHAR NOT NULL, "
        "input_action VARCHAR NOT NULL, input_event_id UUID, context_exit_event_id UUID, "
        "capture_state VARCHAR NOT NULL, response_action VARCHAR, response_event_id UUID, support_trace_id UUID, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL, captured_at TIMESTAMP WITH TIME ZONE, "
        "PRIMARY KEY (assistant_delivery_id), ")
    for name in CHECKS:
        assert f"CONSTRAINT {name} CHECK" in create, name
    assert create.endswith(
        "FOREIGN KEY(assistant_delivery_id) REFERENCES assistant_deliveries (id), "
        "FOREIGN KEY(input_event_id) REFERENCES cognitive_events (id), "
        "FOREIGN KEY(context_exit_event_id) REFERENCES cognitive_events (id), "
        "FOREIGN KEY(response_event_id) REFERENCES cognitive_events (id), "
        "FOREIGN KEY(support_trace_id) REFERENCES support_traces (id) )")
    assert index == (f"CREATE UNIQUE INDEX {OPENING_INDEX} ON {LINKS} (response_event_id) "
                     "WHERE response_action IN ('opened_event', 'transitioned_event')")
    assert version == f"UPDATE alembic_version SET version_num='{R1C2}' WHERE alembic_version.version_num = '{R1C1}'"
    upper = sql.upper()
    for forbidden in ("ALTER TABLE", "DROP ", "INSERT", "DELETE", "CREATE TYPE", "CREATE EXTENSION",
                      "CREATE TRIGGER", "CREATE FUNCTION", "CREATE PROCEDURE", "CREATE VIEW", "CREATE RULE",
                      "DEFAULT", "ON DELETE", "ON UPDATE", "CASCADE", "ENUM", "SEQUENCE", "SERIAL"):
        assert forbidden not in upper, forbidden


def test_offline_sql_of_0012_downgrade_reverts_exactly_r1c2():
    sql = _run_alembic("postgresql://offline@localhost/offline", "downgrade", f"{R1C2}:{R1C1}", "--sql").stdout
    assert _statements(sql) == [
        f"DROP INDEX {OPENING_INDEX}",
        f"DROP TABLE {LINKS}",
        f"UPDATE alembic_version SET version_num='{R1C1}' WHERE alembic_version.version_num = '{R1C2}'",
    ]
    assert "CASCADE" not in sql.upper()


def test_metadata_declares_exactly_the_links_table_in_addition():
    assert set(Base.metadata.tables) == PRE_R1C2_TABLES | R1C2_TABLES
    assert DecryptageCognitiveLink.__table__ is Base.metadata.tables[LINKS]


def test_model_columns_types_and_nullability():
    table = DecryptageCognitiveLink.__table__
    assert [c.name for c in table.columns] == COLUMNS
    assert {c.name: c.nullable for c in table.columns} == {n: n in NULLABLE for n in COLUMNS}
    assert [c.name for c in table.primary_key.columns] == ["assistant_delivery_id"]
    for name in ("assistant_delivery_id", "input_event_id", "context_exit_event_id", "response_event_id",
                 "support_trace_id"):
        assert isinstance(table.c[name].type, sa.Uuid) and table.c[name].type.as_uuid, name
    for name in ("capture_version", "input_action", "capture_state", "response_action"):
        assert type(table.c[name].type) is sa.String and table.c[name].type.length is None, name
    for name in ("created_at", "captured_at"):
        assert isinstance(table.c[name].type, sa.DateTime) and table.c[name].type.timezone, name


def test_model_constraints_are_exactly_those_of_the_migration():
    """Aucune duplication de user_id, conversation_key, surface, tour
    source, analysis_session_id, ticker ni marqueur : uniquement des FK vers
    la livraison, les events et l'aide."""
    table = DecryptageCognitiveLink.__table__
    assert sorted((fk.parent.name, fk.target_fullname, fk.ondelete, fk.onupdate) for fk in table.foreign_keys) == [
        ("assistant_delivery_id", "assistant_deliveries.id", None, None),
        ("context_exit_event_id", "cognitive_events.id", None, None),
        ("input_event_id", "cognitive_events.id", None, None),
        ("response_event_id", "cognitive_events.id", None, None),
        ("support_trace_id", "support_traces.id", None, None),
    ]
    assert {c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)} == CHECKS
    assert not [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
    [index] = table.indexes
    assert (index.name, index.unique, [c.name for c in index.columns]) == (OPENING_INDEX, True, ["response_event_id"])
    assert str(index.dialect_options["postgresql"]["where"]) == (
        "response_action IN ('opened_event', 'transitioned_event')")
    for forbidden in ("user_id", "conversation_key", "surface", "source_user_turn_id", "analysis_session_id",
                      "ticker", "marker", "decryptage_step_marker", "task_kind", "competency", "stage", "score"):
        assert forbidden not in table.c, forbidden


def test_no_server_defaults_and_no_relationships():
    for column in DecryptageCognitiveLink.__table__.columns:
        assert column.server_default is None, column.name
        assert column.default is None, column.name
        assert column.onupdate is None, column.name
    assert not sa.inspect(DecryptageCognitiveLink).relationships


def test_other_models_are_untouched_by_r1c2():
    """Aucune colonne ajoutée ailleurs : le lien vit dans sa propre table."""
    assert len(AssistantDelivery.__table__.columns) == 15
    assert len(CognitiveEvent.__table__.columns) == 16
    assert [c.name for c in SupportTrace.__table__.columns] == [
        "id", "cognitive_event_id", "sequence_no", "support_kind", "support_payload", "created_at"]


def test_migration_writes_no_data_and_backfills_nothing():
    source = (REPO_ROOT / "alembic" / "versions" / f"{R1C2}.py").read_text(encoding="utf-8")
    tokens = set(_code_tokens(source).split("\n"))
    for forbidden in ("execute", "bulk_insert", "server_default", "inline_literal", "add_column",
                      "alter_column", "drop_column", "create_foreign_key", "create_unique_constraint"):
        assert forbidden not in tokens, forbidden


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _insert_link(conn, **overrides):
    values = {
        "assistant_delivery_id": None, "capture_version": "decryptage-cognitive-runtime-v1",
        "input_action": "no_open_event", "input_event_id": None, "context_exit_event_id": None,
        "capture_state": "awaiting_delivery", "response_action": None, "response_event_id": None,
        "support_trace_id": None, "created_at": NOW, "captured_at": None,
    }
    values.update(overrides)
    if values["assistant_delivery_id"] is None:
        values["assistant_delivery_id"] = _insert_delivery(conn, source_user_turn_id=uuid.uuid4())
    conn.execute(sa.text(
        f"INSERT INTO {LINKS} ({', '.join(COLUMNS)}) VALUES ({', '.join(':' + c for c in COLUMNS)})"), values)
    return values["assistant_delivery_id"]


def _insert_trace(conn, event_id):
    trace_id = uuid.uuid4()
    conn.execute(sa.text(
        "INSERT INTO support_traces (id, cognitive_event_id, sequence_no, support_kind, support_payload, created_at) "
        "VALUES (:id, :e, 1, 'assistant_response', CAST('{}' AS JSONB), now())"), {"id": trace_id, "e": event_id})
    return trace_id


@pytest.fixture
def conn(pg_url, pg_engine):  # noqa: F811
    """Schéma = head (0012) + deux utilisateurs, une analysis_session, une
    conversation et un event ; tout ce que fait le test est annulé."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            connection.execute(sa.text(f"INSERT INTO {CONVERSATIONS} VALUES ('conv-u', :u, now())"), {"u": USER})
            yield connection
        finally:
            trans.rollback()


def test_pg_upgrade_from_empty_database_to_head(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == R1C2
    assert _tables(pg_engine) == HEAD_TABLES
    assert _compare_metadata(pg_engine) == []
    catalog = _global_catalog(pg_engine)
    assert (catalog["enums"], catalog["triggers"], catalog["functions"]) == (0, 0, 0)
    assert not [s for s in catalog["sequences"] if LINKS in s]
    assert _data(pg_engine, R1C2_TABLES) == {LINKS: []}


def test_pg_catalog_of_decryptage_cognitive_links(pg_url, pg_engine):  # noqa: F811
    _upgrade_head_with_users(pg_url, pg_engine)
    catalog = _catalog(pg_engine, {LINKS})[LINKS]
    assert catalog["columns"] == [
        ("assistant_delivery_id", "uuid", "NO", None),
        ("capture_version", "character varying", "NO", None),
        ("input_action", "character varying", "NO", None),
        ("input_event_id", "uuid", "YES", None),
        ("context_exit_event_id", "uuid", "YES", None),
        ("capture_state", "character varying", "NO", None),
        ("response_action", "character varying", "YES", None),
        ("response_event_id", "uuid", "YES", None),
        ("support_trace_id", "uuid", "YES", None),
        ("created_at", "timestamp with time zone", "NO", None),
        ("captured_at", "timestamp with time zone", "YES", None),
    ]
    assert [c for c in catalog["constraints"] if c[1] != "c"] == [
        ("decryptage_cognitive_links_assistant_delivery_id_fkey", "f",
         "FOREIGN KEY (assistant_delivery_id) REFERENCES assistant_deliveries(id)", "a", "a"),
        ("decryptage_cognitive_links_context_exit_event_id_fkey", "f",
         "FOREIGN KEY (context_exit_event_id) REFERENCES cognitive_events(id)", "a", "a"),
        ("decryptage_cognitive_links_input_event_id_fkey", "f",
         "FOREIGN KEY (input_event_id) REFERENCES cognitive_events(id)", "a", "a"),
        ("decryptage_cognitive_links_pkey", "p", "PRIMARY KEY (assistant_delivery_id)", " ", " "),
        ("decryptage_cognitive_links_response_event_id_fkey", "f",
         "FOREIGN KEY (response_event_id) REFERENCES cognitive_events(id)", "a", "a"),
        ("decryptage_cognitive_links_support_trace_id_fkey", "f",
         "FOREIGN KEY (support_trace_id) REFERENCES support_traces(id)", "a", "a"),
    ]
    assert {c[0] for c in catalog["constraints"] if c[1] == "c"} == CHECKS
    assert catalog["indexes"] == ["decryptage_cognitive_links_pkey", OPENING_INDEX]


def _rejected(conn, constraint, **overrides):
    with pytest.raises(sa.exc.IntegrityError, match=constraint):
        with conn.begin_nested():
            _insert_link(conn, **overrides)


def test_pg_closed_vocabularies(conn):
    event_id = _insert_event(conn)
    for action in INPUT_ACTIONS:
        _insert_link(conn, input_action=action,
                     input_event_id=event_id if action == "contribution_appended" else None)
    for action in ("closed_without_next_task", "CONTRIBUTION_APPENDED", "", "abandoned"):
        _rejected(conn, "ck_decryptage_cognitive_links_input_action", input_action=action)
    for state in ("pending", "delivered", "CAPTURED", ""):
        _rejected(conn, "ck_decryptage_cognitive_links_capture_state", capture_state=state)
    for action in ("closed_without_next_task", "opened", "evaluated"):
        _rejected(conn, "ck_decryptage_cognitive_links_response_action", capture_state="captured",
                  response_action=action, captured_at=NOW)


def test_pg_awaiting_and_captured_field_invariants(conn):
    event_id = _insert_event(conn)
    trace_id = _insert_trace(conn, event_id)
    for overrides in ({"response_action": "no_cognitive_action"}, {"response_event_id": event_id},
                      {"support_trace_id": trace_id}, {"captured_at": NOW}):
        _rejected(conn, "ck_decryptage_cognitive_links_capture_state_fields", **overrides)
    for overrides in ({}, {"response_action": "no_cognitive_action"}, {"captured_at": NOW}):
        _rejected(conn, "ck_decryptage_cognitive_links_capture_state_fields", capture_state="captured",
                  **({"response_action": None, "captured_at": None} | overrides))
    _insert_link(conn, capture_state="captured", response_action="no_cognitive_action", captured_at=NOW)


def test_pg_input_event_matches_contribution_appended(conn):
    event_id = _insert_event(conn)
    _rejected(conn, "ck_decryptage_cognitive_links_input_event", input_action="contribution_appended")
    for action in ("no_open_event", "no_user_contribution", "event_closed_context_change",
                   "event_abandoned_context_change"):
        _rejected(conn, "ck_decryptage_cognitive_links_input_event", input_action=action, input_event_id=event_id)
    # context_exit_event_id (event quitté) est indépendant de input_event_id.
    _insert_link(conn, input_action="event_closed_context_change", context_exit_event_id=event_id)


def test_pg_response_refs_match_response_action(conn):
    captured = {"capture_state": "captured", "captured_at": NOW}
    event_id = _insert_event(conn)
    other_id = _insert_event(conn, event_dedup_key="b" * 64)
    trace_id = _insert_trace(conn, event_id)
    _insert_link(conn, response_action="opened_event", response_event_id=event_id, **captured)
    _insert_link(conn, response_action="transitioned_event", response_event_id=other_id, **captured)
    for action in ("continued_event", "continued_without_boundary_signal"):
        _insert_link(conn, response_action=action, response_event_id=event_id, support_trace_id=trace_id, **captured)
    for action in ("closed_terminal", "no_cognitive_action"):
        _insert_link(conn, response_action=action, **captured)
    bad = [
        {"response_action": "opened_event"},
        {"response_action": "opened_event", "response_event_id": event_id, "support_trace_id": trace_id},
        {"response_action": "continued_event", "response_event_id": event_id},
        {"response_action": "continued_without_boundary_signal", "support_trace_id": trace_id},
        {"response_action": "closed_terminal", "response_event_id": event_id},
        {"response_action": "no_cognitive_action", "support_trace_id": trace_id},
    ]
    for overrides in bad:
        _rejected(conn, "ck_decryptage_cognitive_links_response_refs", **captured, **overrides)


def test_pg_one_link_per_delivery_and_one_opening_per_event(conn):
    delivery_id = _insert_link(conn)
    with pytest.raises(sa.exc.IntegrityError, match="decryptage_cognitive_links_pkey"):
        with conn.begin_nested():
            _insert_link(conn, assistant_delivery_id=delivery_id)
    captured = {"capture_state": "captured", "captured_at": NOW}
    event_id = _insert_event(conn)
    trace_id = _insert_trace(conn, event_id)
    _insert_link(conn, response_action="opened_event", response_event_id=event_id, **captured)
    for action in ("opened_event", "transitioned_event"):
        with pytest.raises(sa.exc.IntegrityError, match=OPENING_INDEX):
            with conn.begin_nested():
                _insert_link(conn, response_action=action, response_event_id=event_id, **captured)
    # Plusieurs continuations du même event : autorisées.
    for _ in range(2):
        _insert_link(conn, response_action="continued_event", response_event_id=event_id, support_trace_id=trace_id,
                     **captured)


def test_pg_foreign_keys_are_no_action(conn):
    for overrides, constraint in (
        ({"assistant_delivery_id": uuid.uuid4()}, "decryptage_cognitive_links_assistant_delivery_id_fkey"),
        ({"input_action": "contribution_appended", "input_event_id": uuid.uuid4()},
         "decryptage_cognitive_links_input_event_id_fkey"),
        ({"context_exit_event_id": uuid.uuid4()}, "decryptage_cognitive_links_context_exit_event_id_fkey"),
        ({"capture_state": "captured", "captured_at": NOW, "response_action": "opened_event",
          "response_event_id": uuid.uuid4()}, "decryptage_cognitive_links_response_event_id_fkey"),
    ):
        _rejected(conn, constraint, **overrides)
    event_id = _insert_event(conn)
    delivery_id = _insert_link(conn, context_exit_event_id=event_id)
    for statement, constraint in (
        ("DELETE FROM cognitive_events WHERE id = :e", "decryptage_cognitive_links_context_exit_event_id_fkey"),
        ("DELETE FROM assistant_deliveries WHERE id = :d", "decryptage_cognitive_links_assistant_delivery_id_fkey"),
    ):
        with pytest.raises(sa.exc.IntegrityError, match=constraint):
            with conn.begin_nested():
                conn.execute(sa.text(statement), {"e": event_id, "d": delivery_id})


def test_pg_upgrade_0011_to_0012_preserves_everything_then_downgrade(pg_url, pg_engine):  # noqa: F811
    """0011 -> 0012 : toutes les tables existantes gardent schéma, catalogue
    et données (aucun backfill : une livraison R1-C1 historique reste sans
    lien) ; seule decryptage_cognitive_links apparaît, vide. Le downgrade
    restaure exactement 0011 ; le ré-upgrade est identique."""
    _upgrade_to_0007_with_data(pg_url, pg_engine)
    _run_alembic(pg_url, "upgrade", T5A)
    _seed_t5(pg_engine)
    _run_alembic(pg_url, "upgrade", R1C1)
    with pg_engine.begin() as connection:
        user_id = connection.execute(sa.text("SELECT user_id FROM cognitive_events LIMIT 1")).scalar_one()
        connection.execute(sa.text(f"INSERT INTO {CONVERSATIONS} VALUES ('conv-u', :u, now())"), {"u": user_id})
        _insert_event(connection, user_id=user_id, event_dedup_key="d" * 64)
        _insert_delivery(connection, user_id=user_id, status="delivered", delivered_at=NOW)
    existing = EXISTING | R1B_TABLES | R1C1_TABLES
    schema_0011 = _snapshot(pg_engine, existing)
    catalog_0011 = _catalog(pg_engine, existing)
    global_0011 = _global_catalog(pg_engine)
    data_0011 = _data(pg_engine, PRE_R1C2_TABLES)
    assert all(data_0011[t] for t in ("cognitive_events", "assistant_deliveries", CONVERSATIONS))

    # --- upgrade 0011 -> 0012 --------------------------------------------
    _run_alembic(pg_url, "upgrade", R1C2)
    assert _version(pg_engine) == R1C2
    assert _tables(pg_engine) == HEAD_TABLES
    assert _snapshot(pg_engine, existing) == schema_0011
    assert _catalog(pg_engine, existing) == catalog_0011
    assert _global_catalog(pg_engine) == global_0011
    assert _data(pg_engine, PRE_R1C2_TABLES) == data_0011
    assert _data(pg_engine, R1C2_TABLES) == {LINKS: []}
    assert _compare_metadata(pg_engine) == []
    schema_r1c2 = _snapshot(pg_engine, R1C2_TABLES)
    catalog_r1c2 = _catalog(pg_engine, R1C2_TABLES)

    # Donnée R1-C2 (supprimée par le downgrade avec sa table).
    with pg_engine.begin() as connection:
        delivery_id = _insert_delivery(connection, user_id=user_id, source_user_turn_id=uuid.uuid4())
        _insert_link(connection, assistant_delivery_id=delivery_id)

    # --- downgrade 0012 -> 0011 ------------------------------------------
    _run_alembic(pg_url, "downgrade", R1C1)
    assert _version(pg_engine) == R1C1
    assert _tables(pg_engine) == existing
    _assert_absent(pg_engine, [LINKS, OPENING_INDEX])
    assert _snapshot(pg_engine, existing) == schema_0011
    assert _catalog(pg_engine, existing) == catalog_0011
    assert _global_catalog(pg_engine) == global_0011
    data_after = _data(pg_engine, PRE_R1C2_TABLES)
    assert len(data_after["assistant_deliveries"]) == len(data_0011["assistant_deliveries"]) + 1
    data_after["assistant_deliveries"] = [r for r in data_after["assistant_deliveries"] if r[0] != delivery_id]
    assert data_after == data_0011

    # --- ré-upgrade 0011 -> 0012 -----------------------------------------
    _run_alembic(pg_url, "upgrade", R1C2)
    assert _snapshot(pg_engine, R1C2_TABLES) == schema_r1c2
    assert _catalog(pg_engine, R1C2_TABLES) == catalog_r1c2
    assert _data(pg_engine, R1C2_TABLES) == {LINKS: []}
    assert _compare_metadata(pg_engine) == []
