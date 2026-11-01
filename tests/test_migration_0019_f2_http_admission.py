"""Tests de R1 PR 4 : migration 0019_f2_http_admission (C.4.5 §2.1) et
modèles correspondants.

1. Sans base : maillon 0018 -> 0019 (tête), fichiers 0015-0018 inchangés,
   SQL offline exact (upgrade, downgrade verrouillé puis contrôlé),
   modèles alignés, CHECK de type et de liaison sur-ensembles stricts de
   ceux de 0018.
2. PostgreSQL réel (base jetable, tests/pg_guard.py) : montée 0018 -> 0019
   avec toutes les lignes historiques conservées et toujours valides,
   schéma des autres tables intact, downgrade exact (catalogue de 0018
   restauré) ou REFUSÉ si des données de 0019 existent (en ligne et SQL
   offline, aucune donnée supprimée), forme exacte d'un probe (liste
   fermée des actions, ni opération, ni demande OTP, ni blocage, version de
   politique f2-pr4-http-v1), http_action interdite aux types historiques,
   parité DDL migration / modèles.
"""
import hashlib
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from core.auth_f2.policy import HTTP_ACTIONS, HTTP_POLICY_VERSION
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
    PR2_0018,
    PR2_0018_FILE,
    PR4_0019,
    PR4_0019_FILE,
    PR7_0020,
    PR9B1_0021,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import _catalog, _global_catalog
from tests.test_migration_0015_users_level_revision import _seed_historical
from tests.test_migration_0016_portfolio_idempotency import _rejects
from tests.test_migration_0017_oryx_auth_foundations import CREATION_ORDER, _constraint_and_index_ddl
from tests.test_migration_0018_f2_abuse_journal import PR1_SHA256

MIGRATION = REPO_ROOT / "alembic" / "versions" / PR4_0019_FILE
# sha256 de 0018 telle que fusionnée sur main (6cc8ca0) : R1 PR 4 ne la
# modifie pas.
PR2_SHA256 = {"0018_f2_abuse_journal.py": "fb38eafcdf82efb347dbdbcf63f2ae360707444f188486dbf87b3f33704a4162"}
T0 = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)  # aligné sur une tranche de 5 minutes
OFFLINE = "postgresql://offline@localhost/offline"
PROBE = "auth_http_probe_attempt"
HISTORICAL_KINDS = ("otp_request", "otp_failure", "invitation_attempt", "resume_attempt", "otp_verification_attempt",
                    "otp_email_block", "otp_request_refused", "otp_verification_refused")


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _m0018():
    return _module(REPO_ROOT / "alembic" / "versions" / PR2_0018_FILE, "m0018")


def _m0019():
    return _module(MIGRATION, "m0019")


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_links_0019_to_0018_and_is_the_parent_of_0020():
    """0019 était la tête de R1 PR 4 ; R1 PR 7 ajoute 0020 au-dessus, sans
    modifier ce fichier (tests/test_migration_0020_position_revision.py)."""
    script = _script_directory()
    assert script.get_heads() == [PR9B1_0021]
    assert script.get_revision(PR4_0019).down_revision == PR2_0018
    assert script.get_revision(PR7_0020).down_revision == PR4_0019
    assert PR4_0019_FILE == f"{PR4_0019}.py" and len(PR4_0019) <= 32
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(files) == 21 and files[-3] == PR4_0019_FILE


def test_previous_f2_migration_files_are_unchanged():
    for name, digest in {**PR1_SHA256, **PR2_SHA256}.items():
        path = REPO_ROOT / "alembic" / "versions" / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name


def test_check_expressions_are_strict_supersets_of_0018():
    m18, m19 = _m0018(), _m0019()
    # Le downgrade restaure EXACTEMENT les textes de 0018.
    assert (m19.OLD_KIND, m19.OLD_LINK) == (m18.NEW_KIND, m18.NEW_LINK)
    # Type : seul auth_http_probe_attempt est ajouté.
    assert m19.NEW_KIND == m18.NEW_KIND[:-1] + ", 'auth_http_probe_attempt')"
    # Liaison OTP : seul auth_http_probe_attempt rejoint les types qui
    # INTERDISENT otp_request_id ; la clause des types qui l'exigent est intacte.
    exigent, interdisent = m18.NEW_LINK.split(" AND ")
    assert m19.NEW_LINK == exigent + " AND " + interdisent.replace(
        "'otp_verification_refused')", "'otp_verification_refused', 'auth_http_probe_attempt')")
    assert m19.HTTP_ACTIONS == HTTP_ACTIONS and m19.HTTP_POLICY_VERSION == HTTP_POLICY_VERSION
    for action in HTTP_ACTIONS:
        assert f"'{action}'" in m19.HTTP_PROBE


def test_offline_sql_of_0019_upgrade_is_exactly_the_extension():
    statements = _statements(_run_alembic(OFFLINE, "upgrade", f"{PR2_0018}:{PR4_0019}", "--sql").stdout)
    assert statements[:3] == ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '15s'",
                              "SET LOCAL idle_in_transaction_session_timeout = '15s'"]
    m19 = _m0019()
    assert statements[3:] == [
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_event_kind",
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_otp_link",
        "ALTER TABLE auth_abuse_events ADD COLUMN http_action VARCHAR",
        f"ALTER TABLE auth_abuse_events ADD CONSTRAINT ck_auth_abuse_events_event_kind CHECK ({m19.NEW_KIND})",
        f"ALTER TABLE auth_abuse_events ADD CONSTRAINT ck_auth_abuse_events_otp_link CHECK ({m19.NEW_LINK})",
        f"ALTER TABLE auth_abuse_events ADD CONSTRAINT ck_auth_abuse_events_http_probe CHECK ({m19.HTTP_PROBE})",
        f"UPDATE alembic_version SET version_num='{PR4_0019}' WHERE alembic_version.version_num = '{PR2_0018}'",
    ]
    upper = " ".join(statements).upper()
    for forbidden in (" DEFAULT ", "CASCADE", "TRIGGER", "CREATE FUNCTION", "CREATE TYPE", "CREATE EXTENSION",
                      "CREATE TABLE", "CREATE INDEX", "DROP INDEX", "DROP TABLE", "DROP COLUMN", "SERIAL"):
        assert forbidden not in upper, forbidden


def test_offline_sql_of_0019_downgrade_locks_then_guards_then_restores_0018():
    statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{PR4_0019}:{PR2_0018}", "--sql").stdout)
    m18 = _m0018()
    # Verrou AVANT le contrôle : aucune insertion concurrente entre le
    # contrôle et la suppression de la colonne.
    assert statements[3] == "LOCK TABLE auth_abuse_events IN ACCESS EXCLUSIVE MODE"
    assert statements[4] == (
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM auth_abuse_events WHERE event_kind = 'auth_http_probe_attempt' "
        "OR http_action IS NOT NULL) THEN RAISE EXCEPTION 'R1 PR 4 : downgrade de 0019_f2_http_admission refusé, "
        "des données F2 l''exigent (probes d''admission HTTP ou http_action présents). Aucune donnée supprimée.'; "
        "END IF; END $$")
    assert statements[5:] == [
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_http_probe",
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_otp_link",
        "ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_event_kind",
        "ALTER TABLE auth_abuse_events DROP COLUMN http_action",
        f"ALTER TABLE auth_abuse_events ADD CONSTRAINT ck_auth_abuse_events_event_kind CHECK ({m18.NEW_KIND})",
        f"ALTER TABLE auth_abuse_events ADD CONSTRAINT ck_auth_abuse_events_otp_link CHECK ({m18.NEW_LINK})",
        f"UPDATE alembic_version SET version_num='{PR2_0018}' WHERE alembic_version.version_num = '{PR4_0019}'",
    ]


def test_models_match_the_migration_text():
    m18, m19 = _m0018(), _m0019()
    events = Base.metadata.tables["auth_abuse_events"]
    checks = {c.name: str(c.sqltext) for c in events.constraints if isinstance(c, sa.CheckConstraint)}
    assert checks["ck_auth_abuse_events_event_kind"] == m19.NEW_KIND
    assert checks["ck_auth_abuse_events_otp_link"] == m19.NEW_LINK
    assert checks["ck_auth_abuse_events_http_probe"] == m19.HTTP_PROBE
    assert checks["ck_auth_abuse_events_block"] == m18.BLOCK
    assert checks["ck_auth_abuse_events_refusal_bucket"] == m18.BUCKET
    assert checks["ck_auth_abuse_events_resume_link"] == "event_kind <> 'resume_attempt' OR auth_operation_id IS NOT NULL"
    assert set(checks) == {"ck_auth_abuse_events_event_kind", "ck_auth_abuse_events_otp_link",
                           "ck_auth_abuse_events_resume_link", "ck_auth_abuse_events_block",
                           "ck_auth_abuse_events_refusal_bucket", "ck_auth_abuse_events_http_probe"}
    assert list(events.c.keys())[-1] == "http_action" and events.c.http_action.nullable
    assert events.c.http_action.server_default is None
    assert set(Base.metadata.tables) >= set(CREATION_ORDER) | {"auth_abuse_key_state"}


# --------------------------------------------------------------------------
# 2. PostgreSQL réel
# --------------------------------------------------------------------------

def _subject(conn, kind="ip", fill="a"):
    subject = uuid.uuid4()
    conn.execute(sa.text("INSERT INTO auth_abuse_subjects (id, scope_kind, scope_hmac, hash_key_version, created_at) "
                         "VALUES (:id, :k, :h, 'k1', :t)"), {"id": subject, "k": kind, "h": fill * 64, "t": T0})
    return subject


def _operation_and_request(conn):
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
    return op, request


def _event(conn, subject, kind, at=T0, **fields):
    values = {"id": uuid.uuid4(), "subject_id": subject, "event_kind": kind, "occurred_at": at,
              "policy_version": "f2-c2-v1", **fields}
    conn.execute(sa.text(f"INSERT INTO auth_abuse_events ({', '.join(values)}) "
                         f"VALUES ({', '.join(':' + k for k in values)})"), values)


def _probe(conn, subject, action="invitation_begin", **fields):
    _event(conn, subject, PROBE, **{"policy_version": HTTP_POLICY_VERSION, "http_action": action, **fields})


def _seed_every_0018_kind(engine):
    """Une ligne valide de CHAQUE type historique (0017 et 0018)."""
    with engine.begin() as conn:
        subject = _subject(conn)
        op, request = _operation_and_request(conn)
        _event(conn, subject, "otp_request", otp_request_id=request, auth_operation_id=op)
        _event(conn, subject, "otp_failure", otp_request_id=request, auth_operation_id=op)
        _event(conn, subject, "invitation_attempt")
        _event(conn, subject, "resume_attempt", auth_operation_id=op)
        _event(conn, subject, "otp_verification_attempt")
        _event(conn, subject, "otp_verification_attempt", otp_request_id=request)
        _event(conn, subject, "otp_email_block", otp_request_id=request, blocked_until=T0 + timedelta(minutes=15))
        _event(conn, subject, "otp_request_refused")
        _event(conn, subject, "otp_verification_refused")
    return subject


@pytest.fixture
def head(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection, _subject(connection)
        finally:
            trans.rollback()


def test_pg_upgrade_0018_to_0019_keeps_every_historical_row_then_exact_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR2_0018)
    _seed_historical(pg_engine)
    _seed_every_0018_kind(pg_engine)
    tables = _tables(pg_engine)
    schema_0018 = _snapshot(pg_engine, tables)
    catalog_0018 = _catalog(pg_engine, tables)
    global_0018 = _global_catalog(pg_engine)
    data_0018 = _data(pg_engine, tables - {"alembic_version"})
    assert _compare_metadata(pg_engine) == []  # écart attendu (http_action) retiré exactement

    _run_alembic(pg_url, "upgrade", PR4_0019)
    assert _version(pg_engine) == PR4_0019
    assert _tables(pg_engine) == tables
    others = tables - {"alembic_version", "auth_abuse_events"}
    assert _snapshot(pg_engine, others) == {t: schema_0018[t] for t in sorted(others)}
    assert _catalog(pg_engine, others) == {t: catalog_0018[t] for t in sorted(others)}
    assert _global_catalog(pg_engine) == global_0018
    data = _data(pg_engine, tables - {"alembic_version"})
    assert {t: r for t, r in data.items() if t != "auth_abuse_events"} == {
        t: r for t, r in data_0018.items() if t != "auth_abuse_events"}
    assert data["auth_abuse_events"] == [row + (None,) for row in data_0018["auth_abuse_events"]]
    with pg_engine.connect() as conn:
        kinds = set(conn.execute(sa.text("SELECT event_kind FROM auth_abuse_events")).scalars())
        # Toutes les lignes historiques satisfont les CHECK revalidés.
        assert conn.execute(sa.text(
            "SELECT bool_and(convalidated) FROM pg_constraint WHERE conrelid = 'auth_abuse_events'::regclass "
            "AND contype = 'c'")).scalar_one() is True
    assert kinds == set(HISTORICAL_KINDS)
    assert _compare_metadata(pg_engine) == []

    _run_alembic(pg_url, "downgrade", PR2_0018)
    assert _version(pg_engine) == PR2_0018
    assert _snapshot(pg_engine, tables) == schema_0018
    assert _catalog(pg_engine, tables) == catalog_0018
    assert _global_catalog(pg_engine) == global_0018
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0018
    _run_alembic(pg_url, "upgrade", "head")
    assert _compare_metadata(pg_engine) == []


@pytest.mark.parametrize("action", HTTP_ACTIONS)
def test_pg_downgrade_is_refused_when_a_probe_exists(pg_url, pg_engine, action):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR4_0019)  # R1 PR 7 : 0020 au-dessus, testée à part
    subject = _seed_every_0018_kind(pg_engine)
    with pg_engine.begin() as conn:
        _probe(conn, subject, action)
    before = _data(pg_engine, {"auth_abuse_events", "auth_abuse_subjects"})
    with pytest.raises(Exception) as failure:
        _run_alembic(pg_url, "downgrade", PR2_0018)
    assert "downgrade de 0019_f2_http_admission refusé" in str(getattr(failure.value, "stderr", failure.value))
    assert _version(pg_engine) == PR4_0019
    assert _data(pg_engine, {"auth_abuse_events", "auth_abuse_subjects"}) == before
    assert _compare_metadata(pg_engine) == []
    guard = next(s for s in _statements(_run_alembic(OFFLINE, "downgrade", f"{PR4_0019}:{PR2_0018}", "--sql").stdout)
                 if s.startswith("DO $$"))
    with pg_engine.connect() as conn:
        with pytest.raises(sa.exc.DBAPIError, match="refusé"):
            conn.exec_driver_sql(guard)


def test_pg_downgrade_with_only_historical_rows_is_allowed(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_every_0018_kind(pg_engine)
    before = _data(pg_engine, {"auth_abuse_events"})["auth_abuse_events"]
    _run_alembic(pg_url, "downgrade", PR2_0018)
    assert _version(pg_engine) == PR2_0018
    assert _data(pg_engine, {"auth_abuse_events"})["auth_abuse_events"] == [row[:-1] for row in before]


@pytest.mark.parametrize("action", HTTP_ACTIONS)
def test_pg_probe_of_each_action_is_accepted(head, action):
    conn, subject = head
    _probe(conn, subject, action)
    _probe(conn, subject, action)  # aucune déduplication par tranche
    assert conn.execute(sa.text("SELECT count(*) FROM auth_abuse_events WHERE http_action = :a"),
                        {"a": action}).scalar_one() == 2


def test_pg_probe_shape_is_closed(head):
    conn, subject = head
    op, request = _operation_and_request(conn)
    _rejects(conn, "ck_auth_abuse_events_http_probe", lambda: _probe(conn, subject, None))
    for action in ("invitation_revoke", "", "INVITATION_BEGIN", "sign_in_start"):
        _rejects(conn, "ck_auth_abuse_events_http_probe", lambda: _probe(conn, subject, action))
    _rejects(conn, "ck_auth_abuse_events_http_probe", lambda: _probe(conn, subject, auth_operation_id=op))
    # Un probe lié à une demande OTP est refusé par la liaison OTP ET par le
    # CHECK du probe (l'ajout du type ne rouvre pas la liaison).
    _rejects(conn, "ck_auth_abuse_events_otp_link|ck_auth_abuse_events_http_probe",
             lambda: _probe(conn, subject, otp_request_id=request))
    _rejects(conn, "ck_auth_abuse_events_block|ck_auth_abuse_events_http_probe",
             lambda: _probe(conn, subject, blocked_until=T0 + timedelta(minutes=15)))
    for version in ("f2-c2-v1", "f2-pr4-http-v2", ""):
        _rejects(conn, "ck_auth_abuse_events_http_probe", lambda: _probe(conn, subject, policy_version=version))


def test_pg_probe_link_check_alone_refuses_an_otp_request(head):
    """La liaison OTP seule (sans le CHECK du probe) refuse un probe portant
    otp_request_id : vérifié en désactivant temporairement l'autre CHECK
    dans la transaction du test (annulée)."""
    conn, subject = head
    _, request = _operation_and_request(conn)
    conn.execute(sa.text("ALTER TABLE auth_abuse_events DROP CONSTRAINT ck_auth_abuse_events_http_probe"))
    _rejects(conn, "ck_auth_abuse_events_otp_link", lambda: _probe(conn, subject, otp_request_id=request))


@pytest.mark.parametrize("kind", HISTORICAL_KINDS)
def test_pg_historical_kinds_never_carry_an_http_action(head, kind):
    conn, subject = head
    op, request = _operation_and_request(conn)
    fields = {"invitation_attempt": {}, "otp_request_refused": {}, "otp_verification_refused": {},
              "otp_verification_attempt": {}, "resume_attempt": {"auth_operation_id": op},
              "otp_request": {"otp_request_id": request}, "otp_failure": {"otp_request_id": request},
              "otp_email_block": {"otp_request_id": request, "blocked_until": T0 + timedelta(minutes=15)}}[kind]
    _event(conn, subject, kind, **fields)  # valide, http_action NULL
    later = T0 + timedelta(minutes=5)  # autre tranche : seul le CHECK du probe peut refuser
    if "blocked_until" in fields:
        fields = {**fields, "blocked_until": later + timedelta(minutes=15)}
    _rejects(conn, "ck_auth_abuse_events_http_probe",
             lambda: _event(conn, subject, kind, at=later, http_action="invitation_begin", **fields))


def test_pg_0018_rules_are_preserved_at_head(head):
    conn, subject = head
    op, request = _operation_and_request(conn)
    for kind in ("otp_request", "otp_failure", "otp_email_block"):
        _rejects(conn, "ck_auth_abuse_events_otp_link|ck_auth_abuse_events_block", lambda: _event(conn, subject, kind))
    for kind in ("invitation_attempt", "otp_request_refused", "otp_verification_refused"):
        _rejects(conn, "ck_auth_abuse_events_otp_link", lambda: _event(conn, subject, kind, otp_request_id=request))
    _rejects(conn, "ck_auth_abuse_events_resume_link", lambda: _event(conn, subject, "resume_attempt"))
    _rejects(conn, "ck_auth_abuse_events_refusal_bucket",
             lambda: _event(conn, subject, "otp_request_refused", at=T0 + timedelta(seconds=1)))
    _event(conn, subject, "otp_request_refused")
    _rejects(conn, "uq_auth_abuse_events_refusal_bucket", lambda: _event(conn, subject, "otp_request_refused"))
    _rejects(conn, "ck_auth_abuse_events_event_kind", lambda: _event(conn, subject, "http_probe"))
    _rejects(conn, "auth_abuse_events_subject_id_fkey", lambda: _probe(conn, uuid.uuid4()))


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
            compared = set(CREATION_ORDER) | {"auth_abuse_key_state", "users"}
            assert _constraint_and_index_ddl(c, "public", compared) == _constraint_and_index_ddl(c, "model_ref",
                                                                                               compared)
    finally:
        with pg_engine.begin() as c:
            assert_disposable_connection(c)
            c.execute(sa.text("DROP SCHEMA IF EXISTS model_ref CASCADE"))
