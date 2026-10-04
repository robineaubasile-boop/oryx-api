"""Tests de R1-B : migration 0010_r1b_event_idempotence + modèles
ConversationIdentity / CognitiveEvent (identité idempotente).

Même organisation que les tests des migrations 0002 à 0009 (dont on
réutilise les helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, intégrité de 0001
   à 0009, SQL PostgreSQL généré en mode offline (upgrade et downgrade),
   métadonnées des modèles (colonnes, nullabilité, PK, FK, UNIQUE, aucun
   server_default, aucune relationship).

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_r1b_test \\
           python -m pytest tests/test_migration_0010_r1b_event_idempotence.py

Doctrine : 0010 est strictement EXPAND et sans backfill. Les lignes
cognitive_events pré-R1-B gardent NULL dans les quatre colonnes nouvelles
(legacy, jamais « v1 » artificiel) ; conversation_identities n'est qu'un
registre d'appartenance conversation_key -> user_id, jamais un historique de
conversation.
"""
import hashlib
import subprocess
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from core.db import Base
from core.models import CognitiveEvent, ConversationIdentity, User
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
    REMAINING_TABLES,
    T1B1_SHA256,
    T1C2,
    T3A_TABLES,
    T4A_TABLES,
    T5A_TABLES,
    T6A_TABLES,
    _code_tokens,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import (
    OTHER_USER,
    T1C2_SHA256,
    T2A,
    T2A_TABLES,
    USER,
    _catalog,
    _global_catalog,
    _upgrade_head_with_users,
)
from tests.test_migration_0006_observation_layer import T2A_SHA256, T3A
from tests.test_migration_0007_pedagogical_taxonomy import T3A_SHA256, T4A, _assert_absent
from tests.test_migration_0008_longitudinal_relations import T4A_SHA256, T5A, _upgrade_to_0007_with_data
from tests.test_migration_0009_competency_inference_state import T5A_SHA256, T6A, _seed_t5

R1B = "0010_r1b_event_idempotence"
# sha256 de alembic/versions/0009_competency_inference_state.py tel que mergé
# sur main (4982474, après Step 6.4D ; 0009 introduite par T6-A, bad616b).
T6A_SHA256 = "199d04973d4cbc0d62fffe87a25366fca585f390980b8f57235d63db063086d2"

EVENTS = "cognitive_events"
CONVERSATIONS = "conversation_identities"
R1B_TABLES = {CONVERSATIONS}
NEW_EVENT_COLUMNS = ["event_dedup_key", "event_builder_version", "admission_version", "event_schema_version"]
DEDUP_UNIQUE = "uq_cognitive_events_event_dedup_key"
EVENT_COLUMNS = ["id", "user_id", "analysis_session_id", "event_origin", "task_kind", "status",
                 "conversation_key", "stimulus_snapshot", "user_work_snapshot", "started_at", "updated_at",
                 "closed_at", *NEW_EVENT_COLUMNS]
EVENT_NULLABLE = {"analysis_session_id", "task_kind", "conversation_key", "closed_at", *NEW_EVENT_COLUMNS}

ALL_TABLES = (REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES | T6A_TABLES)
EXISTING = ALL_TABLES | {"alembic_version"}


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_exactly_0001_to_0010():
    """0001 -> ... -> 0009 -> 0010, tête unique = 0010 ; 0010 est la seule
    migration ajoutée par R1-B."""
    script = _script_directory()
    assert script.get_heads() == [R1B]
    assert script.get_bases() == [BASELINE]
    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert set(revisions) == {BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A, T6A, R1B}
    assert revisions[R1B].down_revision == T6A
    assert revisions[T6A].down_revision == T5A
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [f"{rev}.py" for rev in (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A, T6A, R1B)]


def test_revision_id_fits_alembic_version_column():
    """alembic_version.version_num est un VARCHAR(32)."""
    assert len(R1B) == 26
    for rev in _script_directory().walk_revisions():
        assert len(rev.revision) <= 32, rev.revision


def test_0001_to_0009_files_are_unchanged():
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
    ):
        assert hashlib.sha256((versions / f"{rev}.py").read_bytes()).hexdigest() == expected, rev


def test_offline_sql_of_0010_upgrade_is_exactly_the_r1b_change():
    """Une table de registre (trois colonnes), quatre colonnes nullable,
    task_kind nullable, une UNIQUE nommée ; aucune donnée, aucun backfill,
    aucun index supplémentaire, ni trigger, ni fonction, ni ENUM, ni
    server_default."""
    sql = _run_alembic("postgresql://offline@localhost/offline", "upgrade", f"{T6A}:{R1B}", "--sql").stdout
    assert _statements(sql) == [
        f"CREATE TABLE {CONVERSATIONS} ( conversation_key VARCHAR NOT NULL, user_id VARCHAR NOT NULL, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (conversation_key), "
        "FOREIGN KEY(user_id) REFERENCES users (id) )",
        *[f"ALTER TABLE {EVENTS} ADD COLUMN {column} VARCHAR" for column in NEW_EVENT_COLUMNS],
        f"ALTER TABLE {EVENTS} ALTER COLUMN task_kind DROP NOT NULL",
        f"ALTER TABLE {EVENTS} ADD CONSTRAINT {DEDUP_UNIQUE} UNIQUE (event_dedup_key)",
        # Seule écriture de données : la version Alembic.
        f"UPDATE alembic_version SET version_num='{R1B}' WHERE alembic_version.version_num = '{T6A}'",
    ]
    upper = sql.upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "DROP CONSTRAINT", "DROP INDEX", "INSERT", "DELETE", "CREATE INDEX", "CREATE TYPE", "CREATE EXTENSION",
                      "CREATE TRIGGER", "CREATE FUNCTION", "CREATE PROCEDURE", "CREATE VIEW", "CREATE RULE",
                      "DEFAULT", "ON DELETE", "ON UPDATE", "CASCADE", "ENUM", "SEQUENCE", "SERIAL",
                      "SET EVENT_SCHEMA_VERSION", "CHECK"):
        assert forbidden not in upper, forbidden
    updates = [s for s in _statements(sql) if s.upper().startswith("UPDATE")]
    assert updates == [f"UPDATE alembic_version SET version_num='{R1B}' WHERE alembic_version.version_num = '{T6A}'"]


def test_offline_sql_of_0010_downgrade_reverts_exactly_r1b():
    sql = _run_alembic("postgresql://offline@localhost/offline", "downgrade", f"{R1B}:{T6A}", "--sql").stdout
    assert _statements(sql) == [
        f"ALTER TABLE {EVENTS} DROP CONSTRAINT {DEDUP_UNIQUE}",
        f"ALTER TABLE {EVENTS} ALTER COLUMN task_kind SET NOT NULL",
        *[f"ALTER TABLE {EVENTS} DROP COLUMN {column}" for column in reversed(NEW_EVENT_COLUMNS)],
        f"DROP TABLE {CONVERSATIONS}",
        f"UPDATE alembic_version SET version_num='{T6A}' WHERE alembic_version.version_num = '{R1B}'",
    ]
    # Aucun backfill artificiel pour rendre le downgrade permissif.
    for forbidden in ("CASCADE", "DELETE", "INSERT", "UPDATE COGNITIVE_EVENTS", "COALESCE"):
        assert forbidden not in sql.upper(), forbidden


def test_metadata_declares_exactly_the_r1b_registry_in_addition():
    assert set(Base.metadata.tables) == ALL_TABLES | R1B_TABLES
    assert ConversationIdentity.__table__ is Base.metadata.tables[CONVERSATIONS]


def test_conversation_identity_is_a_minimal_ownership_registry():
    """Trois colonnes et rien d'autre : ni messages, surface, titre, route,
    niveau ni lifecycle."""
    table = ConversationIdentity.__table__
    assert [c.name for c in table.columns] == ["conversation_key", "user_id", "created_at"]
    assert [c.name for c in table.primary_key.columns] == ["conversation_key"]
    assert {c.name: c.nullable for c in table.columns} == {
        "conversation_key": False, "user_id": False, "created_at": False}
    assert isinstance(table.c.conversation_key.type, sa.String)
    assert isinstance(table.c.user_id.type, sa.String)
    assert isinstance(table.c.created_at.type, sa.DateTime) and table.c.created_at.type.timezone
    assert [(fk.parent.name, fk.target_fullname, fk.ondelete, fk.onupdate, fk.name)
            for fk in table.foreign_keys] == [("user_id", "users.id", None, None, None)]
    assert not table.indexes
    assert not [c for c in table.constraints if isinstance(c, (sa.UniqueConstraint, sa.CheckConstraint))]


def test_cognitive_event_r1b_columns_and_unique():
    table = CognitiveEvent.__table__
    assert [c.name for c in table.columns] == EVENT_COLUMNS
    assert {c.name: c.nullable for c in table.columns} == {n: n in EVENT_NULLABLE for n in EVENT_COLUMNS}
    for column in NEW_EVENT_COLUMNS:
        assert type(table.c[column].type) is sa.String and table.c[column].type.length is None, column
    uniques = [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
    assert [(u.name, [c.name for c in u.columns]) for u in uniques] == [(DEDUP_UNIQUE, ["event_dedup_key"])]
    checks = {c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert checks == {"ck_cognitive_events_status"}
    assert not table.indexes


@pytest.mark.parametrize("model", [ConversationIdentity, CognitiveEvent], ids=lambda m: m.__name__)
def test_no_server_defaults_and_no_relationships(model):
    for column in model.__table__.columns:
        assert column.server_default is None, column.name
        assert column.server_onupdate is None, column.name
    assert not sa.inspect(model).relationships
    # Les colonnes R1-B n'ont pas de défaut applicatif non plus : NULL =
    # legacy, renseignées explicitement par le service.
    for column in NEW_EVENT_COLUMNS if model is CognitiveEvent else ():
        assert model.__table__.c[column].default is None, column


def test_users_table_is_untouched():
    assert [c.name for c in User.__table__.columns] == ["id", "level", "created_at"]
    assert not User.__table__.foreign_keys


def test_migration_writes_no_data_and_backfills_nothing():
    """Le module de migration n'émet aucune écriture de données (ni
    op.execute, ni bulk_insert, ni UPDATE)."""
    source = (REPO_ROOT / "alembic" / "versions" / f"{R1B}.py").read_text(encoding="utf-8")
    tokens = set(_code_tokens(source).split("\n"))
    for forbidden in ("execute", "bulk_insert", "server_default", "create_index", "inline_literal"):
        assert forbidden not in tokens, forbidden


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _insert_event(conn, **overrides):
    values = {
        "id": uuid.uuid4(), "user_id": USER, "event_origin": "education", "task_kind": "interpret_metric",
        "status": "open", "stimulus_snapshot": "{}", "user_work_snapshot": "[]", "started_at": NOW,
        "updated_at": NOW, "event_dedup_key": None,
    }
    values.update(overrides)
    conn.execute(sa.text(
        "INSERT INTO cognitive_events (id, user_id, event_origin, task_kind, status, stimulus_snapshot, "
        "user_work_snapshot, started_at, updated_at, event_dedup_key) VALUES (:id, :user_id, :event_origin, "
        ":task_kind, :status, CAST(:stimulus_snapshot AS JSONB), CAST(:user_work_snapshot AS JSONB), "
        ":started_at, :updated_at, :event_dedup_key)"
    ), values)
    return values["id"]


@pytest.fixture
def conn(pg_url, pg_engine):  # noqa: F811
    """Schéma = head (0010) + deux utilisateurs et une analysis_session ;
    tout ce que fait le test est annulé."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()


def test_pg_upgrade_from_empty_database_to_head(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == R1B
    assert _tables(pg_engine) == EXISTING | R1B_TABLES
    assert _compare_metadata(pg_engine) == []
    catalog = _global_catalog(pg_engine)
    assert (catalog["enums"], catalog["triggers"], catalog["functions"]) == (0, 0, 0)
    assert not [s for s in catalog["sequences"] if CONVERSATIONS in s]
    assert _data(pg_engine, R1B_TABLES) == {CONVERSATIONS: []}


def test_pg_catalog_of_the_r1b_objects(pg_url, pg_engine):  # noqa: F811
    _upgrade_head_with_users(pg_url, pg_engine)
    catalog = _catalog(pg_engine, {CONVERSATIONS, EVENTS})
    assert catalog[CONVERSATIONS] == {
        "columns": [("conversation_key", "character varying", "NO", None),
                    ("user_id", "character varying", "NO", None),
                    ("created_at", "timestamp with time zone", "NO", None)],
        "constraints": [
            ("conversation_identities_pkey", "p", "PRIMARY KEY (conversation_key)", " ", " "),
            ("conversation_identities_user_id_fkey", "f", "FOREIGN KEY (user_id) REFERENCES users(id)", "a", "a"),
        ],
        "indexes": ["conversation_identities_pkey"],
    }
    columns = {name: (data_type, nullable, default) for name, data_type, nullable, default
               in catalog[EVENTS]["columns"]}
    assert [name for name, *_ in catalog[EVENTS]["columns"]] == EVENT_COLUMNS
    for column in NEW_EVENT_COLUMNS:
        assert columns[column] == ("character varying", "YES", None), column
    assert columns["task_kind"] == ("character varying", "YES", None)
    assert (DEDUP_UNIQUE, "u", "UNIQUE (event_dedup_key)", " ", " ") in catalog[EVENTS]["constraints"]
    assert sorted(catalog[EVENTS]["indexes"]) == ["cognitive_events_pkey", DEDUP_UNIQUE]


def test_pg_registry_primary_key_and_user_foreign_key(conn):
    conn.execute(sa.text(f"INSERT INTO {CONVERSATIONS} VALUES ('c1', :u, now())"), {"u": OTHER_USER})
    with pytest.raises(sa.exc.IntegrityError, match="conversation_identities_pkey"):
        with conn.begin_nested():
            conn.execute(sa.text(f"INSERT INTO {CONVERSATIONS} VALUES ('c1', :u, now())"), {"u": USER})
    with pytest.raises(sa.exc.IntegrityError, match="conversation_identities_user_id_fkey"):
        with conn.begin_nested():
            conn.execute(sa.text(f"INSERT INTO {CONVERSATIONS} VALUES ('c2', 'ghost', now())"))
    # FK NO ACTION : un utilisateur propriétaire ne peut pas être supprimé.
    with pytest.raises(sa.exc.IntegrityError, match="conversation_identities_user_id_fkey"):
        with conn.begin_nested():
            conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": OTHER_USER})


def test_pg_dedup_key_is_unique_but_null_is_repeatable(conn):
    """Plusieurs lignes legacy (event_dedup_key NULL) coexistent ; une clé
    non NULL est globalement unique."""
    _insert_event(conn)
    _insert_event(conn)
    _insert_event(conn, event_dedup_key="a" * 64)
    with pytest.raises(sa.exc.IntegrityError, match=DEDUP_UNIQUE):
        with conn.begin_nested():
            _insert_event(conn, event_dedup_key="a" * 64, user_id="u2")


def test_pg_task_kind_accepts_null(conn):
    event_id = _insert_event(conn, task_kind=None)
    assert conn.execute(sa.text("SELECT task_kind FROM cognitive_events WHERE id = :id"),
                        {"id": event_id}).scalar_one() is None


def test_pg_upgrade_0009_to_0010_preserves_everything_then_downgrade(pg_url, pg_engine):  # noqa: F811
    """0009 -> 0010 : toutes les tables autres que cognitive_events gardent
    schéma, catalogue et données ; cognitive_events garde ses lignes legacy
    à l'identique, les quatre colonnes nouvelles à NULL (aucun backfill).
    Le downgrade restaure exactement 0009 ; le ré-upgrade est identique."""
    _upgrade_to_0007_with_data(pg_url, pg_engine)
    _run_alembic(pg_url, "upgrade", T5A)
    _seed_t5(pg_engine)
    _run_alembic(pg_url, "upgrade", T6A)
    untouched = EXISTING - {EVENTS, "alembic_version"}
    schema_0009 = _snapshot(pg_engine, EXISTING)
    catalog_0009 = _catalog(pg_engine, EXISTING)
    global_0009 = _global_catalog(pg_engine)
    data_0009 = _data(pg_engine, ALL_TABLES)
    legacy_events = data_0009[EVENTS]
    assert legacy_events and all(data_0009[t] for t in T2A_TABLES)

    # --- upgrade 0009 -> 0010 --------------------------------------------
    _run_alembic(pg_url, "upgrade", R1B)
    assert _version(pg_engine) == R1B
    assert _tables(pg_engine) == EXISTING | R1B_TABLES
    assert _snapshot(pg_engine, untouched) == {t: schema_0009[t] for t in sorted(untouched)}
    assert _catalog(pg_engine, untouched) == {t: catalog_0009[t] for t in sorted(untouched)}
    assert _global_catalog(pg_engine) == global_0009
    data_0010 = _data(pg_engine, ALL_TABLES | R1B_TABLES)
    assert {t: data_0010[t] for t in ALL_TABLES - {EVENTS}} == {t: data_0009[t] for t in ALL_TABLES - {EVENTS}}
    # Lignes legacy inchangées, colonnes R1-B à NULL ; aucun registre créé.
    assert data_0010[EVENTS] == [row + (None, None, None, None) for row in legacy_events]
    assert data_0010[CONVERSATIONS] == []
    assert _compare_metadata(pg_engine) == []
    schema_r1b = _snapshot(pg_engine, {EVENTS, CONVERSATIONS})
    catalog_r1b = _catalog(pg_engine, {EVENTS, CONVERSATIONS})

    # Donnée R1-B (supprimée par le downgrade avec sa table / ses colonnes).
    with pg_engine.begin() as connection:
        user_id = connection.execute(sa.text("SELECT user_id FROM cognitive_events LIMIT 1")).scalar_one()
        connection.execute(sa.text(f"INSERT INTO {CONVERSATIONS} VALUES ('c-r1b', :u, now())"), {"u": user_id})
        _insert_event(connection, user_id=user_id, event_dedup_key="b" * 64)
        r1b_event = connection.execute(sa.text(
            "SELECT id FROM cognitive_events WHERE event_dedup_key IS NOT NULL")).scalar_one()

    # --- downgrade 0010 -> 0009 ------------------------------------------
    _run_alembic(pg_url, "downgrade", T6A)
    assert _version(pg_engine) == T6A
    assert _tables(pg_engine) == EXISTING
    _assert_absent(pg_engine, [CONVERSATIONS, DEDUP_UNIQUE])
    assert _snapshot(pg_engine, EXISTING) == schema_0009
    assert _catalog(pg_engine, EXISTING) == catalog_0009
    assert _global_catalog(pg_engine) == global_0009
    with pg_engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM cognitive_events WHERE id = :id"), {"id": r1b_event})
    assert _data(pg_engine, ALL_TABLES) == data_0009

    # --- ré-upgrade 0009 -> 0010 -----------------------------------------
    _run_alembic(pg_url, "upgrade", R1B)
    assert _snapshot(pg_engine, {EVENTS, CONVERSATIONS}) == schema_r1b
    assert _catalog(pg_engine, {EVENTS, CONVERSATIONS}) == catalog_r1b
    assert _data(pg_engine, {EVENTS})[EVENTS] == [row + (None, None, None, None) for row in legacy_events]
    assert _compare_metadata(pg_engine) == []


def test_pg_known_limit_downgrade_refuses_events_without_task_kind(pg_url, pg_engine):  # noqa: F811
    """Limite documentée : aucun backfill artificiel ; un événement R1-B à
    task_kind NULL bloque le retour à NOT NULL (et rien n'est modifié)."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with pg_engine.begin() as connection:
        _insert_event(connection, task_kind=None, event_dedup_key="c" * 64)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        _run_alembic(pg_url, "downgrade", T6A)
    assert 'column "task_kind" of relation "cognitive_events" contains null values' in failure.value.stderr
    assert _version(pg_engine) == R1B
    assert _data(pg_engine, {EVENTS})[EVENTS][0][4] is None
