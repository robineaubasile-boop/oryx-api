"""Tests de R1-C4 : migration 0013 (fichier
0013_decryptage_conversation_affinity.py, révision
0013_decryptage_conv_affinity) + modèle DecryptageCognitiveLink étendu.

Même organisation que les tests des migrations 0002 à 0012 (dont on
réutilise les helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, identifiant de
   révision court (VARCHAR(32)), intégrité de 0001 à 0012, SQL PostgreSQL
   généré en mode offline (upgrade et downgrade), aucun backfill, modèle
   identique à la migration, vocabulaires du runtime identiques aux CHECK.

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_r1c4_test \\
           python -m pytest tests/test_migration_0013_decryptage_conversation_affinity.py

Doctrine : 0013 est strictement EXPAND et sans backfill. Elle étend
decryptage_cognitive_links (colonnes input_context_event_id et
response_context_event_id + FK, CHECK
étendus et de cohérence par version de runtime) ; aucune colonne supprimée
ni renommée ; les lignes V1 restent valides et gardent leur sémantique.
"""
import hashlib
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from core import decryptage_cognitive_runtime as dcr
from core.models import DecryptageCognitiveLink
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
    R1C2_TABLES,
    R1C4,
    R1C4_FILE,
    _code_tokens,
    _compare_metadata,
    _data,
    _statements,
    _without_r1c4_changes,
)
from tests.test_migration_0005_cognitive_support_traces import (
    OTHER_USER,
    SESSION_ID,
    USER,
    _catalog,
    _global_catalog,
)
from tests.test_migration_0010_r1b_event_idempotence import CONVERSATIONS, EXISTING, R1B_TABLES, _insert_event
from tests.test_migration_0011_assistant_deliveries import _insert_delivery
from tests.test_migration_0012_decryptage_cognitive_links import (
    CHECKS as CHECKS_0012,
    COLUMNS as COLUMNS_0012,
    LINKS,
    OPENING_INDEX,
    R1C1_SHA256,
    R1C1_TABLES,
    R1C2,
    _insert_trace,
)

# Nom de la spec R1-C4 : trop long pour alembic_version.version_num.
SPEC_NAME = "0013_decryptage_conversation_affinity"
MIGRATION_PATH = REPO_ROOT / "alembic" / "versions" / R1C4_FILE
# sha256 de alembic/versions/0012_decryptage_cognitive_links.py tel que
# mergé sur main (b85cb16, PR #215 ; 0012 introduite par R1-C2, PR #214).
R1C2_SHA256 = "13e10ac183e6b9455ac0bceda7f1db41a0ddf15920b118b94b508cc4883f2c28"

COLUMN = "input_context_event_id"
FK = "decryptage_cognitive_links_input_context_event_id_fkey"
RCOLUMN = "response_context_event_id"
RFK = "decryptage_cognitive_links_response_context_event_id_fkey"
NEW_CHECKS = {
    "ck_decryptage_cognitive_links_context_switched",
    "ck_decryptage_cognitive_links_stale_delivery",
    "ck_decryptage_cognitive_links_runtime_version",
    "ck_decryptage_cognitive_links_response_attachment",
}
HEAD_CHECKS = CHECKS_0012 | NEW_CHECKS
HEAD_COLUMNS = COLUMNS_0012 + [COLUMN, RCOLUMN]
V1 = "decryptage-cognitive-runtime-v1"
V2 = "decryptage-cognitive-runtime-v2"
INPUT_ACTIONS = ("no_open_event", "no_user_contribution", "contribution_appended", "event_closed_context_change",
                 "event_abandoned_context_change", "context_switched")
RESPONSE_ACTIONS = ("opened_event", "continued_event", "continued_without_boundary_signal", "transitioned_event",
                    "closed_terminal", "no_cognitive_action", "stale_delivery")
HEAD_TABLES = EXISTING | R1B_TABLES | R1C1_TABLES | R1C2_TABLES


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_ends_with_0013_as_the_single_head():
    script = _script_directory()
    assert script.get_heads() == [R1C4]
    assert script.get_revision(R1C4).down_revision == R1C2
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[-2:] == [f"{R1C2}.py", R1C4_FILE] and len(files) == 13


def test_revision_id_is_short_because_alembic_version_is_varchar_32():
    """Le nom de la spec (37 caractères) ne tient pas dans
    alembic_version.version_num : fichier nommé selon la spec, identifiant
    court documenté dans la migration."""
    assert R1C4_FILE == f"{SPEC_NAME}.py"
    assert len(SPEC_NAME) == 37 > 32
    assert len(R1C4) == 29 <= 32
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert f'revision: str = "{R1C4}"' in source
    assert f'down_revision: Union[str, Sequence[str], None] = "{R1C2}"' in source
    assert "VARCHAR(32)" in source


def test_0011_and_0012_files_are_unchanged():
    """Migrations historiques immuables (0001 à 0010 vérifiées par les tests
    de 0012)."""
    versions = REPO_ROOT / "alembic" / "versions"
    assert hashlib.sha256((versions / "0011_assistant_deliveries.py").read_bytes()).hexdigest() == R1C1_SHA256
    assert hashlib.sha256((versions / f"{R1C2}.py").read_bytes()).hexdigest() == R1C2_SHA256


def test_offline_sql_of_0013_upgrade_only_extends_the_links_table():
    sql = _run_alembic("postgresql://offline@localhost/offline", "upgrade", f"{R1C2}:{R1C4}", "--sql").stdout
    statements = _statements(sql)
    assert statements[:4] == [
        f"ALTER TABLE {LINKS} ADD COLUMN {COLUMN} UUID",
        f"ALTER TABLE {LINKS} ADD COLUMN {RCOLUMN} UUID",
        f"ALTER TABLE {LINKS} ADD CONSTRAINT {FK} FOREIGN KEY({COLUMN}) REFERENCES cognitive_events (id)",
        f"ALTER TABLE {LINKS} ADD CONSTRAINT {RFK} FOREIGN KEY({RCOLUMN}) REFERENCES cognitive_events (id)",
    ]
    assert statements[4] == f"ALTER TABLE {LINKS} DROP CONSTRAINT ck_decryptage_cognitive_links_input_action"
    assert statements[5].startswith(
        f"ALTER TABLE {LINKS} ADD CONSTRAINT ck_decryptage_cognitive_links_input_action CHECK")
    assert "'context_switched'" in statements[5] and "'event_closed_context_change'" in statements[5]
    assert statements[6] == f"ALTER TABLE {LINKS} DROP CONSTRAINT ck_decryptage_cognitive_links_response_action"
    assert statements[7].startswith(
        f"ALTER TABLE {LINKS} ADD CONSTRAINT ck_decryptage_cognitive_links_response_action CHECK")
    assert "'stale_delivery'" in statements[7] and "'no_cognitive_action'" in statements[7]
    added = ("ck_decryptage_cognitive_links_context_switched", "ck_decryptage_cognitive_links_stale_delivery",
             "ck_decryptage_cognitive_links_runtime_version", "ck_decryptage_cognitive_links_response_attachment")
    assert set(added) == NEW_CHECKS
    for statement, name in zip(statements[8:12], added):
        assert statement.startswith(f"ALTER TABLE {LINKS} ADD CONSTRAINT {name} CHECK"), statement
    assert statements[12] == (f"UPDATE alembic_version SET version_num='{R1C4}' "
                              f"WHERE alembic_version.version_num = '{R1C2}'")
    assert len(statements) == 13
    upper = sql.upper()
    # Aucune donnée écrite (aucun backfill), aucune suppression de colonne,
    # aucun objet de schéma hors de la table.
    for forbidden in ("INSERT", "DELETE", "DROP COLUMN", "DROP TABLE", "RENAME", "CREATE TABLE", "CREATE INDEX",
                      "CREATE TYPE", "CREATE TRIGGER", "CREATE FUNCTION", "DEFAULT", "ON DELETE", "ON UPDATE",
                      "CASCADE", "NOT VALID", "SET NOT NULL"):
        assert forbidden not in upper, forbidden
    assert [s for s in statements if s.upper().startswith("UPDATE")] == [statements[12]]


def test_offline_sql_of_0013_downgrade_reverts_exactly_r1c4():
    sql = _run_alembic("postgresql://offline@localhost/offline", "downgrade", f"{R1C4}:{R1C2}", "--sql").stdout
    statements = _statements(sql)
    assert statements[:6] == [
        f"ALTER TABLE {LINKS} DROP CONSTRAINT ck_decryptage_cognitive_links_response_attachment",
        f"ALTER TABLE {LINKS} DROP CONSTRAINT ck_decryptage_cognitive_links_runtime_version",
        f"ALTER TABLE {LINKS} DROP CONSTRAINT ck_decryptage_cognitive_links_stale_delivery",
        f"ALTER TABLE {LINKS} DROP CONSTRAINT ck_decryptage_cognitive_links_context_switched",
        f"ALTER TABLE {LINKS} DROP CONSTRAINT ck_decryptage_cognitive_links_response_action",
        f"ALTER TABLE {LINKS} ADD CONSTRAINT ck_decryptage_cognitive_links_response_action CHECK (response_action "
        "IS NULL OR response_action IN ('opened_event', 'continued_event', 'continued_without_boundary_signal', "
        "'transitioned_event', 'closed_terminal', 'no_cognitive_action'))",
    ]
    assert statements[6:12] == [
        f"ALTER TABLE {LINKS} DROP CONSTRAINT ck_decryptage_cognitive_links_input_action",
        f"ALTER TABLE {LINKS} ADD CONSTRAINT ck_decryptage_cognitive_links_input_action CHECK (input_action IN "
        "('no_open_event', 'no_user_contribution', 'contribution_appended', 'event_closed_context_change', "
        "'event_abandoned_context_change'))",
        f"ALTER TABLE {LINKS} DROP CONSTRAINT {RFK}",
        f"ALTER TABLE {LINKS} DROP CONSTRAINT {FK}",
        f"ALTER TABLE {LINKS} DROP COLUMN {RCOLUMN}",
        f"ALTER TABLE {LINKS} DROP COLUMN {COLUMN}",
    ]
    assert statements[12] == (f"UPDATE alembic_version SET version_num='{R1C2}' "
                              f"WHERE alembic_version.version_num = '{R1C4}'")
    assert len(statements) == 13 and "CASCADE" not in sql.upper()


def test_upgrade_writes_no_data_and_backfills_nothing():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade = source[source.index("def upgrade"):source.index("def downgrade")]
    tokens = set(_code_tokens(upgrade).split("\n"))
    for forbidden in ("execute", "bulk_insert", "server_default", "inline_literal", "drop_column", "alter_column",
                      "drop_table", "rename_table", "create_table", "create_index"):
        assert forbidden not in tokens, forbidden


def test_model_matches_the_migration_exactly():
    """Le modèle porte les deux colonnes (fin de table, nullables, FK sans
    cascade) et les CHECK avec le MÊME texte SQL que la migration."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("migration_0013", MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    table = DecryptageCognitiveLink.__table__
    assert [c.name for c in table.columns] == HEAD_COLUMNS
    for name in (COLUMN, RCOLUMN):
        column = table.c[name]
        assert column.nullable and isinstance(column.type, sa.Uuid) and column.server_default is None
        [fk] = column.foreign_keys
        assert (fk.target_fullname, fk.ondelete, fk.onupdate) == ("cognitive_events.id", None, None)
    checks = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert set(checks) == HEAD_CHECKS
    assert checks["ck_decryptage_cognitive_links_input_action"] == migration.INPUT_ACTIONS_0013
    assert checks["ck_decryptage_cognitive_links_response_action"] == migration.RESPONSE_ACTIONS_0013
    assert checks["ck_decryptage_cognitive_links_context_switched"] == migration.CONTEXT_SWITCHED
    assert checks["ck_decryptage_cognitive_links_stale_delivery"] == migration.STALE_DELIVERY
    assert checks["ck_decryptage_cognitive_links_runtime_version"] == migration.RUNTIME_VERSION
    assert checks["ck_decryptage_cognitive_links_response_attachment"] == migration.RESPONSE_ATTACHMENT


def test_runtime_vocabularies_are_exactly_the_checks():
    assert dcr.INPUT_ACTIONS == INPUT_ACTIONS
    assert dcr.RESPONSE_ACTIONS == RESPONSE_ACTIONS
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    for value in (*INPUT_ACTIONS, *RESPONSE_ACTIONS, V1, V2):
        assert f"'{value}'" in source, value
    assert dcr.SUPPORTED_RUNTIME_VERSIONS == (V1, V2) and dcr.CAPTURE_VERSION == V2


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _insert_link(conn, **overrides):
    values = {
        "assistant_delivery_id": None, "capture_version": V1, "input_action": "no_open_event",
        "input_event_id": None, "context_exit_event_id": None, "capture_state": "awaiting_delivery",
        "response_action": None, "response_event_id": None, "support_trace_id": None, "created_at": NOW,
        "captured_at": None, COLUMN: None, RCOLUMN: None,
    }
    values.update(overrides)
    if values["assistant_delivery_id"] is None:
        values["assistant_delivery_id"] = _insert_delivery(conn, source_user_turn_id=uuid.uuid4())
    columns = [c for c in HEAD_COLUMNS if c in values]
    conn.execute(sa.text(
        f"INSERT INTO {LINKS} ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)})"), values)
    return values["assistant_delivery_id"]


def _rejected(conn, constraint, **overrides):
    with pytest.raises(sa.exc.IntegrityError, match=constraint):
        with conn.begin_nested():
            _insert_link(conn, **overrides)


def _seed_users(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:a, 'debutant'), (:b, 'debutant')"),
                     {"a": USER, "b": OTHER_USER})
        conn.execute(sa.text("INSERT INTO analysis_sessions (id, user_id, ticker, status, started_at, updated_at) "
                             "VALUES (:id, :u, 'MC.PA', 'in_progress', now(), now())"), {"id": SESSION_ID, "u": USER})
        conn.execute(sa.text(f"INSERT INTO {CONVERSATIONS} VALUES ('conv-u', :u, now())"), {"u": USER})


@pytest.fixture
def conn(pg_url, pg_engine):  # noqa: F811
    """Schéma = head (0013) + utilisateurs, conversation ; tout est annulé."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_users(pg_engine)
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()


def _seed_v1_history(connection):
    """Historique V1 réaliste (toutes les formes R1-C2), à 0012."""
    e1 = _insert_event(connection, event_dedup_key="1" * 64)
    e2 = _insert_event(connection, event_dedup_key="2" * 64)
    trace = _insert_trace(connection, e1)
    captured = {"capture_state": "captured", "captured_at": NOW}
    rows = [
        {"input_action": "no_open_event", "response_action": "opened_event", "response_event_id": e1, **captured},
        {"input_action": "contribution_appended", "input_event_id": e1, "response_action": "continued_event",
         "response_event_id": e1, "support_trace_id": trace, **captured},
        {"input_action": "event_closed_context_change", "context_exit_event_id": e1,
         "response_action": "transitioned_event", "response_event_id": e2, **captured},
        {"input_action": "event_abandoned_context_change", "context_exit_event_id": e2},
        {"input_action": "no_user_contribution", "response_action": "closed_terminal", **captured},
    ]
    for row in rows:
        values = {"capture_version": V1, "capture_state": "awaiting_delivery", "input_event_id": None,
                  "context_exit_event_id": None, "response_action": None, "response_event_id": None,
                  "support_trace_id": None, "captured_at": None, "created_at": NOW, **row}
        values["assistant_delivery_id"] = _insert_delivery(connection, source_user_turn_id=uuid.uuid4())
        connection.execute(sa.text(
            f"INSERT INTO {LINKS} ({', '.join(COLUMNS_0012)}) VALUES ({', '.join(':' + c for c in COLUMNS_0012)})"),
            values)


def test_pg_upgrade_0012_to_0013_keeps_v1_rows_untouched_then_downgrade(pg_url, pg_engine):  # noqa: F811
    """0012 -> 0013 : données V1 (toutes les actions historiques) intactes,
    input_context_event_id NULL partout (AUCUN backfill), autres tables
    inchangées (schéma, catalogue, données). Le downgrade restaure exactement
    0012 ; le ré-upgrade est identique."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1C2)
    _seed_users(pg_engine)
    with pg_engine.begin() as connection:
        _seed_v1_history(connection)
    others = HEAD_TABLES - {LINKS}
    schema_0012 = _snapshot(pg_engine, HEAD_TABLES)
    catalog_0012 = _catalog(pg_engine, HEAD_TABLES)
    global_0012 = _global_catalog(pg_engine)
    data_0012 = _data(pg_engine, HEAD_TABLES - {"alembic_version"})
    assert len(data_0012[LINKS]) == 5

    # --- upgrade 0012 -> 0013 --------------------------------------------
    _run_alembic(pg_url, "upgrade", R1C4)
    assert _version(pg_engine) == R1C4
    assert _tables(pg_engine) == HEAD_TABLES
    assert _snapshot(pg_engine, others) == {t: schema_0012[t] for t in sorted(others)}
    assert _catalog(pg_engine, others) == {t: catalog_0012[t] for t in sorted(others)}
    assert _global_catalog(pg_engine) == global_0012
    data_0013 = _data(pg_engine, HEAD_TABLES - {"alembic_version"})
    assert {t: rows for t, rows in data_0013.items() if t != LINKS} == {
        t: rows for t, rows in data_0012.items() if t != LINKS}
    # Lignes V1 : mêmes valeurs, nouvelles colonnes NULL (aucun backfill).
    assert data_0013[LINKS] == [row + (None, None) for row in data_0012[LINKS]]
    assert _compare_metadata(pg_engine) == []
    catalog = _catalog(pg_engine, {LINKS})[LINKS]
    assert catalog["columns"][-2:] == [(COLUMN, "uuid", "YES", None), (RCOLUMN, "uuid", "YES", None)]
    assert [c[0] for c in catalog["columns"][:-2]] == COLUMNS_0012  # aucune colonne supprimée / renommée
    assert {c[0] for c in catalog["constraints"] if c[1] == "c"} == HEAD_CHECKS
    for column, fk in ((COLUMN, FK), (RCOLUMN, RFK)):
        assert (fk, "f", f"FOREIGN KEY ({column}) REFERENCES cognitive_events(id)", "a", "a") in catalog["constraints"]
    assert catalog["indexes"] == catalog_0012[LINKS]["indexes"]
    schema_0013 = _snapshot(pg_engine, {LINKS})
    catalog_0013 = _catalog(pg_engine, {LINKS})

    # --- downgrade 0013 -> 0012 (données V1 seulement) --------------------
    _run_alembic(pg_url, "downgrade", R1C2)
    assert _version(pg_engine) == R1C2
    assert _snapshot(pg_engine, HEAD_TABLES) == schema_0012
    assert _catalog(pg_engine, HEAD_TABLES) == catalog_0012
    assert _global_catalog(pg_engine) == global_0012
    assert _data(pg_engine, HEAD_TABLES - {"alembic_version"}) == data_0012
    assert _without_r1c4_changes(_compare_metadata(pg_engine)) == []

    # --- ré-upgrade --------------------------------------------------------
    _run_alembic(pg_url, "upgrade", R1C4)
    assert _snapshot(pg_engine, {LINKS}) == schema_0013
    assert _catalog(pg_engine, {LINKS}) == catalog_0013
    assert _compare_metadata(pg_engine) == []


def test_pg_downgrade_refuses_to_lose_v2_semantics(pg_url, pg_engine):  # noqa: F811
    """Une ligne V2 context_switched existe : le downgrade échoue (le CHECK
    d'origine la refuse) sans rien perdre ; la base reste à 0013."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_users(pg_engine)
    with pg_engine.begin() as connection:
        anchor = _insert_event(connection, event_dedup_key="a" * 64)
        _insert_link(connection, capture_version=V2, input_action="context_switched", **{COLUMN: anchor})
    before = _data(pg_engine, {LINKS})
    with pytest.raises(Exception):
        _run_alembic(pg_url, "downgrade", R1C2)
    assert _version(pg_engine) == R1C4
    assert _data(pg_engine, {LINKS}) == before


def test_pg_v1_rows_remain_valid_at_head(conn):
    _seed_v1_history(conn)
    assert conn.execute(sa.text(f"SELECT count(*) FROM {LINKS} WHERE {COLUMN} IS NULL")).scalar_one() == 5


def test_pg_v2_vocabulary_and_context_switched_check(conn):
    anchor = _insert_event(conn, event_dedup_key="a" * 64)
    other = _insert_event(conn, event_dedup_key="b" * 64)
    v2 = {"capture_version": V2}
    _insert_link(conn, **v2, input_action="context_switched", **{COLUMN: anchor})
    _insert_link(conn, **v2, input_action="context_switched", **{COLUMN: anchor, RCOLUMN: other})
    _insert_link(conn, **v2, input_action="no_user_contribution", **{COLUMN: anchor, RCOLUMN: anchor})
    _insert_link(conn, **v2, input_action="contribution_appended", input_event_id=anchor,
                 **{COLUMN: anchor, RCOLUMN: anchor})
    _insert_link(conn, **v2, input_action="no_open_event")
    _insert_link(conn, **v2, input_action="no_open_event", **{RCOLUMN: other})
    check = "ck_decryptage_cognitive_links_context_switched"
    _rejected(conn, check, **v2, input_action="context_switched")  # ancre obligatoire
    _rejected(conn, check, **v2, input_action="context_switched", context_exit_event_id=other, **{COLUMN: anchor})
    _rejected(conn, "ck_decryptage_cognitive_links_(input_event|context_switched)", **v2,
              input_action="context_switched", input_event_id=anchor, **{COLUMN: anchor})
    for action in ("CONTEXT_SWITCHED", "context_switch", "detached"):
        _rejected(conn, "ck_decryptage_cognitive_links_input_action", **v2, input_action=action, **{COLUMN: anchor})


def test_pg_stale_delivery_check(conn):
    event_id = _insert_event(conn, event_dedup_key="a" * 64)
    trace_id = _insert_trace(conn, event_id)
    captured = {"capture_version": V2, "input_action": "no_user_contribution", COLUMN: event_id,
                RCOLUMN: event_id, "capture_state": "captured", "captured_at": NOW}
    _insert_link(conn, response_action="stale_delivery", **captured)
    for overrides in ({"response_event_id": event_id}, {"support_trace_id": trace_id},
                      {"response_event_id": event_id, "support_trace_id": trace_id}):
        with pytest.raises(sa.exc.IntegrityError, match="ck_decryptage_cognitive_links_(stale_delivery|response_refs)"):
            with conn.begin_nested():
                _insert_link(conn, response_action="stale_delivery", **captured, **overrides)
    _rejected(conn, "ck_decryptage_cognitive_links_capture_state_fields", capture_version=V2,
              input_action="no_user_contribution", response_action="stale_delivery",
              **{COLUMN: event_id, RCOLUMN: event_id})
    _rejected(conn, "ck_decryptage_cognitive_links_response_action", response_action="stale", **captured)


def test_pg_runtime_version_check(conn):
    anchor = _insert_event(conn, event_dedup_key="a" * 64)
    other = _insert_event(conn, event_dedup_key="b" * 64)
    check = "ck_decryptage_cognitive_links_runtime_version"
    captured = {"capture_state": "captured", "captured_at": NOW}
    # V1 : jamais d'ancre, de context_switched ni de stale_delivery.
    _rejected(conn, check, capture_version=V1, input_action="no_user_contribution", **{COLUMN: anchor})
    _rejected(conn, check, capture_version=V1, input_action="context_switched", **{COLUMN: anchor})
    _rejected(conn, check, capture_version=V1, response_action="stale_delivery", **captured)
    # V2 : jamais de fermeture sur navigation, ancre <=> pas no_open_event,
    # contribution = event d'ancrage exact.
    for action in ("event_closed_context_change", "event_abandoned_context_change"):
        _rejected(conn, check, capture_version=V2, input_action=action, context_exit_event_id=anchor,
                  **{COLUMN: anchor})
    _rejected(conn, check, capture_version=V2, input_action="no_user_contribution", context_exit_event_id=other,
              **{COLUMN: anchor, RCOLUMN: anchor})
    _rejected(conn, check, capture_version=V2, input_action="no_open_event", **{COLUMN: anchor})
    _rejected(conn, check, capture_version=V2, input_action="no_user_contribution")
    _rejected(conn, check, capture_version=V2, input_action="contribution_appended", input_event_id=other,
              **{COLUMN: anchor, RCOLUMN: anchor})
    # R1-C4 (blocker 1) : V1 jamais de cible de réponse figée ; un tour ancré
    # destine sa réponse exactement à son ancre ; un context switch jamais à
    # l'event quitté.
    _rejected(conn, check, capture_version=V1, **{RCOLUMN: anchor})
    for action, extra in (("no_user_contribution", {}), ("contribution_appended", {"input_event_id": anchor})):
        _rejected(conn, check, capture_version=V2, input_action=action, **extra, **{COLUMN: anchor})
        _rejected(conn, check, capture_version=V2, input_action=action, **extra, **{COLUMN: anchor, RCOLUMN: other})
    _rejected(conn, check, capture_version=V2, input_action="context_switched", **{COLUMN: anchor, RCOLUMN: anchor})
    for version in ("decryptage-cognitive-runtime-v3", "v2", ""):
        _rejected(conn, check, capture_version=version)


def test_pg_input_context_event_fk_is_no_action(conn):
    _rejected(conn, FK, capture_version=V2, input_action="context_switched", **{COLUMN: uuid.uuid4()})
    anchor = _insert_event(conn, event_dedup_key="a" * 64)
    _insert_link(conn, capture_version=V2, input_action="context_switched", **{COLUMN: anchor})
    with pytest.raises(sa.exc.IntegrityError, match=FK):
        with conn.begin_nested():
            conn.execute(sa.text("DELETE FROM cognitive_events WHERE id = :e"), {"e": anchor})


def test_pg_response_context_event_fk_is_no_action(conn):
    _rejected(conn, RFK, capture_version=V2, input_action="no_open_event", **{RCOLUMN: uuid.uuid4()})
    target = _insert_event(conn, event_dedup_key="a" * 64)
    _insert_link(conn, capture_version=V2, input_action="no_open_event", **{RCOLUMN: target})
    with pytest.raises(sa.exc.IntegrityError, match=RFK):
        with conn.begin_nested():
            conn.execute(sa.text("DELETE FROM cognitive_events WHERE id = :e"), {"e": target})


def test_pg_response_context_check(conn):
    """R1-C4 (blocker 1) : la réponse ne se rattache qu'à sa cible figée."""
    target = _insert_event(conn, event_dedup_key="a" * 64)
    other = _insert_event(conn, event_dedup_key="b" * 64)
    trace = _insert_trace(conn, target)
    other_trace = _insert_trace(conn, other)
    check = "ck_decryptage_cognitive_links_response_attachment"
    v2 = {"capture_version": V2, "input_action": "no_open_event", "capture_state": "captured", "captured_at": NOW}
    # Valides.
    for action in ("continued_event", "continued_without_boundary_signal"):
        _insert_link(conn, **v2, response_action=action, response_event_id=target, support_trace_id=trace,
                     **{RCOLUMN: target})
    _insert_link(conn, **v2, response_action="transitioned_event", response_event_id=other, **{RCOLUMN: target})
    _insert_link(conn, **v2, response_action="closed_terminal", **{RCOLUMN: target})
    _insert_link(conn, **v2, response_action="no_cognitive_action")
    _insert_link(conn, **v2, response_action="stale_delivery", **{RCOLUMN: target})
    _insert_link(conn, **v2, response_action="stale_delivery")
    # Refusés : continuation d'un autre event que la cible figée (ou sans
    # cible), transition / fermeture sans cible, ouverture alors qu'une cible
    # existait.
    for action in ("continued_event", "continued_without_boundary_signal"):
        _rejected(conn, check, **v2, response_action=action, response_event_id=other, support_trace_id=other_trace,
                  **{RCOLUMN: target})
        _rejected(conn, check, **v2, response_action=action, response_event_id=other, support_trace_id=other_trace)
    _rejected(conn, check, **v2, response_action="transitioned_event", response_event_id=other)
    _rejected(conn, check, **v2, response_action="closed_terminal")
    _rejected(conn, check, **v2, response_action="no_cognitive_action", **{RCOLUMN: target})
    # (opened_event avec cible : transition/ouverture déguisée)
    fresh = _insert_event(conn, event_dedup_key="c" * 64)
    _rejected(conn, check, **v2, response_action="opened_event", response_event_id=fresh, **{RCOLUMN: target})


def test_pg_opening_index_is_unchanged(conn):
    event_id = _insert_event(conn, event_dedup_key="a" * 64)
    previous = _insert_event(conn, event_dedup_key="b" * 64)
    captured = {"capture_version": V2, "capture_state": "captured", "captured_at": NOW}
    _insert_link(conn, response_action="opened_event", response_event_id=event_id, **captured)
    with pytest.raises(sa.exc.IntegrityError, match=OPENING_INDEX):
        with conn.begin_nested():
            _insert_link(conn, response_action="transitioned_event", response_event_id=event_id,
                         **captured, **{RCOLUMN: previous})
