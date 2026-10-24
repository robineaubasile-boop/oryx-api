"""Tests de R1 PR 2 : migration 0018_f2_abuse_journal (variante A de
C.2.1-A) et modèles correspondants.

1. Sans base : maillon 0017 -> 0018 (tête), fichiers 0015-0017 inchangés,
   SQL offline exact (upgrade et downgrade contrôlé), modèles alignés.
2. PostgreSQL réel (base jetable, tests/pg_guard.py) : montée 0017 -> 0018
   avec données conservées, schéma des autres tables intact, downgrade
   exact ou REFUSÉ si des données de 0018 existent (en ligne et SQL
   offline), contraintes et NULL de chaque nouveau type d'événement,
   sur-ensemble du CHECK de liaison de 0017, unicité des marqueurs de
   refus sous concurrence réelle, singleton auth_abuse_key_state, parité
   DDL migration / modèles, ancien backend compatible.
"""
import hashlib
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from core.db import Base
from tests.pg_guard import assert_disposable_connection
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
    PR1_0017,
    PR2_0018,
    PR2_0018_FILE,
    PR2_TABLES,
    PR4_0019,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import _catalog, _global_catalog
from tests.test_migration_0015_users_level_revision import _exercise_legacy_backend, _seed_historical
from tests.test_migration_0016_portfolio_idempotency import _rejects, _wait_for_lock_waiter
from tests.test_migration_0017_oryx_auth_foundations import CREATION_ORDER, _constraint_and_index_ddl

MIGRATION = REPO_ROOT / "alembic" / "versions" / PR2_0018_FILE
# sha256 des migrations de R1 PR 1 telles que fusionnées sur main (a8d5b60) :
# R1 PR 2 ne les modifie pas.
PR1_SHA256 = {
    "0015_users_level_revision.py": "b2685061aa5988521295efe256487e54aae1f8ca6ace49843622697c65fb6cfc",
    "0016_portfolio_idempotency.py": "428e404edf624509bba88f548b4b972c1e8e4c1ba1a82531037e7882c9c5fe5e",
    "0017_oryx_auth_foundations.py": "a9026fb73072afd3355290e5b3e86164877da61bdcdd23e0087bca51ec39b6ba",
}
NEW_KINDS = ("otp_verification_attempt", "otp_email_block", "otp_request_refused", "otp_verification_refused")
T0 = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)  # aligné sur une tranche de 5 minutes
F2_MODEL_TABLES = set(CREATION_ORDER) | PR2_TABLES
OFFLINE = "postgresql://offline@localhost/offline"


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_links_0018_to_0017_and_is_the_parent_of_0019():
    """0018 était la tête de R1 PR 2 ; R1 PR 4 ajoute 0019 au-dessus, sans
    modifier ce fichier (tests/test_migration_0019_f2_http_admission.py)."""
    script = _script_directory()
    assert script.get_heads() == [PR4_0019]
    assert script.get_revision(PR2_0018).down_revision == PR1_0017
    assert script.get_revision(PR4_0019).down_revision == PR2_0018
    assert PR2_0018_FILE == f"{PR2_0018}.py" and len(PR2_0018) <= 32
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(files) == 19 and files[-2] == PR2_0018_FILE


def test_pr1_migration_files_are_unchanged():
    for name, digest in PR1_SHA256.items():
        path = REPO_ROOT / "alembic" / "versions" / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name


def test_offline_sql_of_0018_upgrade_is_exactly_the_extension():
    statements = _statements(_run_alembic(OFFLINE, "upgrade", f"{PR1_0017}:{PR2_0018}", "--sql").stdout)
    assert statements[:3] == ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '15s'",
                              "SET LOCAL idle_in_transaction_session_timeout = '15s'"]
    body = statements[3:]
    assert body[:3] == [
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_event_kind",
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_otp_link",
        "ALTER TABLE auth_abuse_events ADD COLUMN blocked_until TIMESTAMP WITH TIME ZONE",
    ]
    added = [s.split()[5] for s in body if s.startswith("ALTER TABLE auth_abuse_events ADD CONSTRAINT")]
    assert added == ["ck_auth_abuse_events_event_kind", "ck_auth_abuse_events_otp_link", "ck_auth_abuse_events_block",
                     "ck_auth_abuse_events_refusal_bucket"]
    assert ("CREATE UNIQUE INDEX uq_auth_abuse_events_refusal_bucket ON auth_abuse_events "
            "(subject_id, event_kind, occurred_at) WHERE event_kind IN ('otp_request_refused', "
            "'otp_verification_refused')") in body
    assert [s.split()[2] for s in body if s.startswith("CREATE TABLE")] == ["auth_abuse_key_state"]
    writes = [s for s in statements if s.split()[0] in ("INSERT", "UPDATE", "DELETE")]
    assert writes == [f"UPDATE alembic_version SET version_num='{PR2_0018}' "
                      f"WHERE alembic_version.version_num = '{PR1_0017}'"]
    upper = " ".join(statements).upper()
    for forbidden in (" DEFAULT ", "CASCADE", "TRIGGER", "CREATE FUNCTION", "CREATE TYPE", "CREATE EXTENSION",
                      "ON DELETE", "DEFERRABLE", "DROP TABLE", "DROP COLUMN", "SERIAL"):
        assert forbidden not in upper, forbidden
    # Aucune autre table que auth_abuse_events n'est modifiée.
    assert {s.split()[2] for s in body if s.startswith("ALTER TABLE")} == {"auth_abuse_events"}


def test_offline_sql_of_0018_downgrade_is_guarded_then_exact():
    statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{PR2_0018}:{PR1_0017}", "--sql").stdout)
    assert statements[3:5] == ["LOCK TABLE auth_abuse_events IN ACCESS EXCLUSIVE MODE",
                               "LOCK TABLE auth_abuse_key_state IN ACCESS EXCLUSIVE MODE"]
    assert statements[5].startswith("DO $$ BEGIN IF EXISTS (SELECT 1 FROM auth_abuse_events WHERE event_kind NOT IN")
    assert "RAISE EXCEPTION" in statements[5] and "Aucune donnée supprimée" in statements[5]
    assert statements[6:] == [
        "DROP TABLE auth_abuse_key_state",
        "DROP INDEX uq_auth_abuse_events_refusal_bucket",
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_refusal_bucket",
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_block",
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_otp_link",
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_event_kind",
        "ALTER TABLE auth_abuse_events DROP COLUMN blocked_until",
        "ALTER TABLE auth_abuse_events ADD CONSTRAINT ck_auth_abuse_events_event_kind CHECK (event_kind IN "
        "('otp_request', 'otp_failure', 'invitation_attempt', 'resume_attempt'))",
        "ALTER TABLE auth_abuse_events ADD CONSTRAINT ck_auth_abuse_events_otp_link CHECK ((event_kind IN "
        "('otp_request', 'otp_failure')) = (otp_request_id IS NOT NULL))",
        f"UPDATE alembic_version SET version_num='{PR1_0017}' WHERE alembic_version.version_num = '{PR2_0018}'",
    ]


def test_models_match_the_migration_text():
    import importlib.util

    spec = importlib.util.spec_from_file_location("m0018", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    events = Base.metadata.tables["auth_abuse_events"]
    checks = {c.name: str(c.sqltext) for c in events.constraints if isinstance(c, sa.CheckConstraint)}
    # Type et liaison OTP : remplacés par 0019 (tête), qui restaure ces
    # textes exacts au downgrade (tests/test_migration_0019_f2_http_admission.py).
    spec = importlib.util.spec_from_file_location("m0019", REPO_ROOT / "alembic" / "versions" / f"{PR4_0019}.py")
    head = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(head)
    assert (head.OLD_KIND, head.OLD_LINK) == (migration.NEW_KIND, migration.NEW_LINK)
    assert checks["ck_auth_abuse_events_event_kind"] == head.NEW_KIND
    assert checks["ck_auth_abuse_events_otp_link"] == head.NEW_LINK
    assert checks["ck_auth_abuse_events_block"] == migration.BLOCK
    assert checks["ck_auth_abuse_events_refusal_bucket"] == migration.BUCKET
    assert list(events.c.keys())[-2:] == ["blocked_until", "http_action"] and events.c.blocked_until.nullable
    state = Base.metadata.tables["auth_abuse_key_state"]
    assert [c.name for c in state.columns] == ["id", "current_version", "previous_version", "previous_demoted_at",
                                               "updated_at"]
    assert all(c.server_default is None for c in list(events.columns) + list(state.columns))
    assert state.c.id.autoincrement is False


# --------------------------------------------------------------------------
# 2. PostgreSQL réel
# --------------------------------------------------------------------------

def _seed_0017_events(engine):
    """Événements valides sous 0017 (anciens types), à conserver."""
    subject = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO auth_abuse_subjects (id, scope_kind, scope_hmac, hash_key_version, "
                             "created_at) VALUES (:id, 'ip', :h, 'k1', :t)"), {"id": subject, "h": "a" * 64, "t": T0})
        conn.execute(sa.text("INSERT INTO auth_abuse_events (id, subject_id, event_kind, occurred_at, policy_version) "
                             "VALUES (:id, :s, 'invitation_attempt', :t, 'p1')"), {"id": uuid.uuid4(), "s": subject,
                                                                                   "t": T0})
    return subject


@pytest.fixture
def head(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    subject = _seed_0017_events(pg_engine)
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection, subject
        finally:
            trans.rollback()


def _event(conn, subject, kind, at=T0, **fields):
    values = {"id": uuid.uuid4(), "subject_id": subject, "event_kind": kind, "occurred_at": at,
              "policy_version": "f2-c2-v1", **fields}
    conn.execute(sa.text(f"INSERT INTO auth_abuse_events ({', '.join(values)}) "
                         f"VALUES ({', '.join(':' + k for k in values)})"), values)


def _request(conn):
    """Opération + demande OTP minimales (FK de otp_request_id)."""
    op, request = uuid.uuid4(), uuid.uuid4()
    conn.execute(sa.text(
        "INSERT INTO auth_operations (id, kind, status, email_normalized, operation_schema_version, created_at, "
        "updated_at, expires_at) VALUES (:id, 'supabase_email_sign_in', 'pending', 'a@b.c', 'v', :t, :t, :e)"),
        {"id": op, "t": T0, "e": T0 + timedelta(minutes=10)})
    conn.execute(sa.text(
        "INSERT INTO auth_otp_requests (id, auth_operation_id, operation_kind, purpose, code_generation, "
        "email_normalized, status, failure_count, requested_at, expires_at) VALUES (:id, :op, "
        "'supabase_email_sign_in', 'sign_in', 1, 'a@b.c', 'issuing', 0, :t, :e)"),
        {"id": request, "op": op, "t": T0, "e": T0 + timedelta(minutes=10)})
    return request


def test_pg_upgrade_0017_to_0018_keeps_data_then_exact_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR1_0017)
    _seed_historical(pg_engine)
    _seed_0017_events(pg_engine)
    tables = _tables(pg_engine)
    schema_0017 = _snapshot(pg_engine, tables)
    catalog_0017 = _catalog(pg_engine, tables)
    global_0017 = _global_catalog(pg_engine)
    data_0017 = _data(pg_engine, tables - {"alembic_version"})

    _run_alembic(pg_url, "upgrade", PR2_0018)
    assert _version(pg_engine) == PR2_0018
    assert _tables(pg_engine) == tables | PR2_TABLES
    others = tables - {"alembic_version", "auth_abuse_events"}
    assert _snapshot(pg_engine, others) == {t: schema_0017[t] for t in sorted(others)}
    assert _catalog(pg_engine, others) == {t: catalog_0017[t] for t in sorted(others)}
    assert _global_catalog(pg_engine) == global_0017
    data = _data(pg_engine, tables - {"alembic_version"})
    assert {t: r for t, r in data.items() if t != "auth_abuse_events"} == {
        t: r for t, r in data_0017.items() if t != "auth_abuse_events"}
    assert data["auth_abuse_events"] == [row + (None,) for row in data_0017["auth_abuse_events"]]
    assert _compare_metadata(pg_engine) == []

    _run_alembic(pg_url, "downgrade", PR1_0017)
    assert _tables(pg_engine) == tables
    assert _snapshot(pg_engine, tables) == schema_0017
    assert _catalog(pg_engine, tables) == catalog_0017
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0017
    _run_alembic(pg_url, "upgrade", "head")
    assert _compare_metadata(pg_engine) == []


@pytest.mark.parametrize("payload", ["event", "state"])
def test_pg_downgrade_is_refused_when_0018_data_exists(pg_url, pg_engine, payload):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR2_0018)
    subject = _seed_0017_events(pg_engine)
    with pg_engine.begin() as conn:
        if payload == "event":
            _event(conn, subject, "otp_verification_attempt")
        else:
            conn.execute(sa.text("INSERT INTO auth_abuse_key_state (id, current_version, updated_at) "
                                 "VALUES (1, 'k1', :t)"), {"t": T0})
    before = _data(pg_engine, {"auth_abuse_events", "auth_abuse_key_state"})
    with pytest.raises(Exception) as failure:
        _run_alembic(pg_url, "downgrade", PR1_0017)
    assert "downgrade de 0018_f2_abuse_journal refusé" in str(getattr(failure.value, "stderr", failure.value))
    assert _version(pg_engine) == PR2_0018
    assert _data(pg_engine, {"auth_abuse_events", "auth_abuse_key_state"}) == before
    # Le garde-fou du SQL offline refuse de la même manière.
    guard = next(s for s in _statements(_run_alembic(OFFLINE, "downgrade", f"{PR2_0018}:{PR1_0017}", "--sql").stdout)
                 if s.startswith("DO $$"))
    with pg_engine.connect() as conn:
        with pytest.raises(sa.exc.DBAPIError, match="refusé"):
            conn.exec_driver_sql(guard)


def test_pg_new_event_kinds_and_their_shapes(head):
    conn, subject = head
    request = _request(conn)
    _event(conn, subject, "otp_verification_attempt")
    _event(conn, subject, "otp_verification_attempt", otp_request_id=request)
    _event(conn, subject, "otp_email_block", otp_request_id=request, blocked_until=T0 + timedelta(minutes=15))
    _event(conn, subject, "otp_request_refused")
    _event(conn, subject, "otp_verification_refused")
    _rejects(conn, "ck_auth_abuse_events_otp_link", lambda: _event(conn, subject, "otp_email_block",
                                                                     blocked_until=T0 + timedelta(minutes=15)))
    for until in (T0 + timedelta(minutes=14, seconds=59), T0 + timedelta(minutes=15, microseconds=1), None):
        _rejects(conn, "ck_auth_abuse_events_block", lambda: _event(conn, subject, "otp_email_block",
                                                                      otp_request_id=request, blocked_until=until))
    _rejects(conn, "ck_auth_abuse_events_block", lambda: _event(conn, subject, "otp_failure", otp_request_id=request,
                                                                  blocked_until=T0 + timedelta(minutes=15)))
    for kind in ("otp_request_refused", "otp_verification_refused"):
        _rejects(conn, "ck_auth_abuse_events_otp_link", lambda: _event(conn, subject, kind,
                                                                         at=T0 + timedelta(minutes=5),
                                                                         otp_request_id=request))
        _rejects(conn, "ck_auth_abuse_events_refusal_bucket", lambda: _event(conn, subject, kind,
                                                                               at=T0 + timedelta(seconds=1)))
    _rejects(conn, "ck_auth_abuse_events_event_kind", lambda: _event(conn, subject, "otp_unknown"))
    _rejects(conn, "null value|not-null", lambda: _event(conn, subject, "otp_verification_attempt",
                                                         policy_version=None))


def test_pg_0017_link_rules_are_preserved(head):
    conn, subject = head
    request = _request(conn)
    for kind in ("otp_request", "otp_failure"):
        _event(conn, subject, kind, otp_request_id=request)
        _rejects(conn, "ck_auth_abuse_events_otp_link", lambda: _event(conn, subject, kind))
    _event(conn, subject, "invitation_attempt")
    _rejects(conn, "ck_auth_abuse_events_otp_link", lambda: _event(conn, subject, "invitation_attempt",
                                                                     otp_request_id=request))
    _rejects(conn, "ck_auth_abuse_events_otp_link|ck_auth_abuse_events_resume_link",
             lambda: _event(conn, subject, "resume_attempt", otp_request_id=request))
    _rejects(conn, "ck_auth_abuse_events_resume_link", lambda: _event(conn, subject, "resume_attempt"))


def test_pg_one_refusal_marker_per_subject_kind_and_bucket(head):
    conn, subject = head
    other = uuid.uuid4()
    conn.execute(sa.text("INSERT INTO auth_abuse_subjects (id, scope_kind, scope_hmac, hash_key_version, created_at) "
                         "VALUES (:id, 'email', :h, 'k1', :t)"), {"id": other, "h": "b" * 64, "t": T0})
    _event(conn, subject, "otp_request_refused")
    _rejects(conn, "uq_auth_abuse_events_refusal_bucket", lambda: _event(conn, subject, "otp_request_refused"))
    _event(conn, subject, "otp_verification_refused")
    _event(conn, other, "otp_request_refused")
    _event(conn, subject, "otp_request_refused", at=T0 + timedelta(minutes=5))
    # Les admissions ne sont pas dédupliquées par tranche.
    _event(conn, subject, "otp_verification_attempt")
    _event(conn, subject, "otp_verification_attempt")
    insert = sa.text(
        "INSERT INTO auth_abuse_events (id, subject_id, event_kind, occurred_at, policy_version) VALUES (:id, :s, "
        "'otp_request_refused', date_bin(interval '5 minutes', CAST(:t AS timestamptz), "
        "timestamptz '2000-01-01 00:00:00+00'), 'p') ON CONFLICT (subject_id, event_kind, occurred_at) "
        "WHERE event_kind IN ('otp_request_refused', 'otp_verification_refused') DO NOTHING")
    assert conn.execute(insert, {"id": uuid.uuid4(), "s": subject, "t": T0 + timedelta(minutes=4, seconds=59)}
                        ).rowcount == 0


def test_pg_concurrent_markers_in_one_bucket_keep_a_single_row(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    subject = _seed_0017_events(pg_engine)
    insert = sa.text(
        "INSERT INTO auth_abuse_events (id, subject_id, event_kind, occurred_at, policy_version) VALUES (:id, :s, "
        "'otp_verification_refused', date_bin(interval '5 minutes', CAST(:t AS timestamptz), "
        "timestamptz '2000-01-01 00:00:00+00'), 'p') ON CONFLICT (subject_id, event_kind, occurred_at) "
        "WHERE event_kind IN ('otp_request_refused', 'otp_verification_refused') DO NOTHING")
    first = pg_engine.connect()
    trans = first.begin()
    assert first.execute(insert, {"id": uuid.uuid4(), "s": subject, "t": T0 + timedelta(seconds=10)}).rowcount == 1
    outcome = {}

    def second():
        with pg_engine.begin() as conn:
            outcome["rows"] = conn.execute(insert, {"id": uuid.uuid4(), "s": subject,
                                                    "t": T0 + timedelta(minutes=4)}).rowcount

    thread = threading.Thread(target=second)
    thread.start()
    _wait_for_lock_waiter(pg_engine)
    trans.commit()
    first.close()
    thread.join(timeout=30)
    assert outcome["rows"] == 0
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM auth_abuse_events WHERE event_kind = "
                                    "'otp_verification_refused'")).scalar_one() == 1


def test_pg_key_state_is_a_constrained_singleton(head):
    conn, _ = head
    insert = ("INSERT INTO auth_abuse_key_state (id, current_version, previous_version, previous_demoted_at, "
              "updated_at) VALUES (:id, :cur, :prev, :demoted, :t)")
    ok = {"id": 1, "cur": "v2", "prev": "v1", "demoted": T0, "t": T0}
    _rejects(conn, "ck_auth_abuse_key_state_singleton", insert, **dict(ok, id=2))
    _rejects(conn, "ck_auth_abuse_key_state_previous", insert, **dict(ok, demoted=None))
    _rejects(conn, "ck_auth_abuse_key_state_previous", insert, **dict(ok, prev=None))
    _rejects(conn, "ck_auth_abuse_key_state_previous", insert, **dict(ok, prev="v2"))
    _rejects(conn, "ck_auth_abuse_key_state_current_version", insert, **dict(ok, cur="v 2"))
    _rejects(conn, "ck_auth_abuse_key_state_previous_version", insert, **dict(ok, prev="v/1"))
    _rejects(conn, "null value|not-null", insert, **dict(ok, cur=None))
    _rejects(conn, "null value|not-null", insert, **dict(ok, t=None))
    conn.execute(sa.text(insert), ok)
    _rejects(conn, "auth_abuse_key_state_pkey", insert, **ok)


def test_pg_migration_ddl_is_identical_to_model_ddl(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    with pg_engine.begin() as c:
        assert_disposable_connection(c)
        c.execute(sa.text("DROP SCHEMA IF EXISTS model_ref CASCADE"))
        c.execute(sa.text("CREATE SCHEMA model_ref"))
    try:
        Base.metadata.create_all(pg_engine.execution_options(schema_translate_map={None: "model_ref"}))
        with pg_engine.connect() as c:
            compared = F2_MODEL_TABLES | {"users"}
            assert _constraint_and_index_ddl(c, "public", compared) == _constraint_and_index_ddl(c, "model_ref",
                                                                                               compared)
    finally:
        with pg_engine.begin() as c:
            assert_disposable_connection(c)
            c.execute(sa.text("DROP SCHEMA IF EXISTS model_ref CASCADE"))


def test_pg_head_keeps_legacy_backend_working_and_has_no_trigger_or_function(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    _exercise_legacy_backend(pg_engine)
    catalog = _global_catalog(pg_engine)
    assert (catalog["enums"], catalog["triggers"], catalog["functions"]) == (0, 0, 0)
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM auth_abuse_key_state")).scalar_one() == 0
