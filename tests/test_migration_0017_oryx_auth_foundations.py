"""Tests de R1 PR 1 : migration 0017_oryx_auth_foundations + modèles F2.

1. Sans base : maillon 0016 -> 0017, identifiant, SQL offline (onze
   tables, FK de session initiatrice ajoutée par ALTER après oryx_sessions,
   aucun backfill / trigger / server_default / CASCADE), downgrade dans
   l'ordre inverse, aucune colonne de secret en clair dans les modèles.

2. PostgreSQL réel (base jetable protégée par tests/pg_guard.py ; sinon
   SKIPPÉS) :
   - upgrade 0016 -> 0017 et chaîne complète 0014 -> 0017 sur données
     historiques, downgrade exact, DDL des migrations identique au DDL des
     modèles ;
   - ordre d'insertion des parcours : connexion Supabase, invité sans
     e-mail, email_linking (identité nouvelle, existante, conflit sans
     fusion), reprise de cinq minutes après expiration de la fenêtre
     d'authentification ;
   - transitions d'état (candidat clos / révoqué après liaison, OTP
     incertain, preuve de livraison), CHECK, FK composites ;
   - transactions concurrentes (validations OTP, verrou anti-abus) ;
   - compatibilité de l'ancien backend à la tête.
"""
import hashlib
import json
import re
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
    PR1_0016,
    PR1_0017,
    PR1_0017_FILE,
    PR1_0017_TABLES,
    R1D1,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import _catalog, _global_catalog
from tests.test_migration_0015_users_level_revision import _exercise_legacy_backend, _seed_historical
from tests.test_migration_0016_portfolio_idempotency import _receipt, _rejects, _wait_for_lock_waiter

MIGRATION_PATH = REPO_ROOT / "alembic" / "versions" / PR1_0017_FILE
CREATION_ORDER = [
    "auth_identities", "invitations", "auth_operations", "auth_preauth_candidates", "auth_otp_requests",
    "auth_otp_verification_attempts", "auth_abuse_subjects", "auth_abuse_events", "oryx_sessions",
    "oryx_session_generations", "auth_operation_results",
]
PR1_MODEL_TABLES = set(CREATION_ORDER) | {"portfolio_write_idempotency"}
T0 = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
ISS = "https://oryx-test.supabase.co/auth/v1"
USER = "user_7f3a"
OTHER = "legacy-anon-2"
SECRET_WORDS = ("secret", "token", "password", "hash", "hmac")
FORBIDDEN_COLUMN_NAMES = {"code", "otp", "otp_code", "secret", "token", "password", "access_token",
                          "refresh_token", "provider_token", "session_secret", "candidate_secret"}


def h(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_links_0017_to_0016_and_is_the_head():
    script = _script_directory()
    assert script.get_heads() == [PR1_0017]
    assert script.get_revision(PR1_0017).down_revision == PR1_0016
    assert PR1_0017_FILE == f"{PR1_0017}.py" and len(PR1_0017) <= 32
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(files) == 17 and files[-1] == PR1_0017_FILE
    assert tuple(CREATION_ORDER) == PR1_0017_TABLES


def test_offline_sql_of_0017_upgrade_creates_the_eleven_tables_in_order():
    sql = _run_alembic("postgresql://offline@localhost/offline", "upgrade", f"{PR1_0016}:{PR1_0017}", "--sql").stdout
    statements = _statements(sql)
    created = [s.split()[2] for s in statements if s.startswith("CREATE TABLE ")]
    assert created == CREATION_ORDER
    alters = [s for s in statements if s.startswith("ALTER TABLE ")]
    assert alters == [
        "ALTER TABLE auth_operations ADD CONSTRAINT auth_operations_linking_session_fkey FOREIGN KEY"
        "(initiating_session_id, target_user_id, initiating_session_method) REFERENCES oryx_sessions "
        "(id, user_id, auth_method)"]
    position = {s: i for i, s in enumerate(statements)}
    sessions = next(i for s, i in position.items() if s.startswith("CREATE TABLE oryx_sessions "))
    generations = next(i for s, i in position.items() if s.startswith("CREATE TABLE oryx_session_generations "))
    assert sessions < position[alters[0]] < generations
    writes = [s for s in statements if s.split()[0] in ("INSERT", "UPDATE", "DELETE")]
    assert writes == [f"UPDATE alembic_version SET version_num='{PR1_0017}' "
                      f"WHERE alembic_version.version_num = '{PR1_0016}'"]
    upper = sql.upper()
    for forbidden in (" DEFAULT ", "CASCADE", "TRIGGER", "FUNCTION", "CREATE TYPE", "CREATE EXTENSION",
                      "ON DELETE", "ON UPDATE", "DEFERRABLE", "DROP "):
        assert forbidden not in upper, forbidden
    assert statements.count("SET LOCAL lock_timeout = '5s'") == 0


def test_offline_sql_of_0017_downgrade_reverts_in_reverse_order():
    sql = _run_alembic("postgresql://offline@localhost/offline", "downgrade", f"{PR1_0017}:{PR1_0016}", "--sql").stdout
    statements = _statements(sql)
    assert statements == [
        "DROP TABLE auth_operation_results",
        "DROP TABLE oryx_session_generations",
        "ALTER TABLE auth_operations DROP CONSTRAINT auth_operations_linking_session_fkey",
        *[f"DROP TABLE {t}" for t in reversed(CREATION_ORDER[:-2])],
        f"UPDATE alembic_version SET version_num='{PR1_0016}' WHERE alembic_version.version_num = '{PR1_0017}'",
    ]
    assert "CASCADE" not in sql.upper()


def test_models_store_no_cleartext_secret():
    for name in PR1_MODEL_TABLES:
        table = Base.metadata.tables[name]
        hex_checks = {str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
        for column in table.columns:
            assert column.name not in FORBIDDEN_COLUMN_NAMES, (name, column.name)
            assert column.server_default is None, (name, column.name)
            if any(w in column.name for w in SECRET_WORDS) and not column.name.endswith("_version"):
                assert column.name.endswith(("_hash", "_hmac")) or column.name == "request_fingerprint", column.name
            if column.name.endswith(("_hash", "_hmac")) or column.name == "request_fingerprint":
                assert any(f"{column.name} ~ '^[0-9a-f]{{64}}$'" in c for c in hex_checks), (name, column.name)


def test_model_fk_graph_has_only_the_optional_linking_cycle():
    edges = {}
    for name in PR1_MODEL_TABLES:
        for fk in Base.metadata.tables[name].foreign_key_constraints:
            edges.setdefault(name, set()).add((fk.referred_table.name, fk.name, tuple(c.name for c in fk.columns)))
    cyclic = [
        (src, fk_name, columns) for src, targets in edges.items() for dst, fk_name, columns in targets
        if any(back_dst == src for back_dst, _, _ in edges.get(dst, ()))
    ]
    assert sorted(cyclic) == [
        ("auth_operations", "auth_operations_linking_session_fkey",
         ("initiating_session_id", "target_user_id", "initiating_session_method")),
        ("oryx_sessions", "oryx_sessions_operation_holder_fkey", ("created_by_operation_id", "user_id", "auth_method")),
        ("oryx_sessions", "oryx_sessions_operation_identity_fkey", ("created_by_operation_id", "auth_identity_id")),
        ("oryx_sessions", "oryx_sessions_operation_invitation_fkey", ("created_by_operation_id", "invitation_id")),
    ]
    operations = Base.metadata.tables["auth_operations"]
    assert all(operations.c[c].nullable for c in ("initiating_session_id", "target_user_id",
                                                   "initiating_session_method"))
    linking = next(fk for fk in operations.foreign_key_constraints if fk.name == "auth_operations_linking_session_fkey")
    assert linking.use_alter is True


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL — fixtures et constructeurs
# --------------------------------------------------------------------------

def _insert(conn, table, **values):
    columns = list(values)
    params = {}
    placeholders = []
    for column in columns:
        value = values[column]
        if isinstance(value, (dict, list)):
            params[column] = json.dumps(value)
            placeholders.append(f"CAST(:{column} AS JSONB)")
        else:
            params[column] = value
            placeholders.append(f":{column}")
    conn.execute(sa.text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"), params)
    return values.get("id", values.get("operation_id"))


def _update(conn, table, key, **values):
    assignments = ", ".join(f"{c} = :{c}" for c in values)
    key_column = "operation_id" if table == "auth_operation_results" else "id"
    conn.execute(sa.text(f"UPDATE {table} SET {assignments} WHERE {key_column} = :_key"), {**values, "_key": key})


def _identity(conn, user=USER, subject=None, email="alice@example.com"):
    return _insert(conn, "auth_identities", id=uuid.uuid4(), user_id=user, provider="supabase", issuer=ISS,
                   subject=subject or str(uuid.uuid4()), verified_email_normalized=email,
                   email_verified_at=T0 if email else None, created_at=T0)


def _operation(conn, kind, at=T0, **fields):
    values = dict(id=uuid.uuid4(), kind=kind, status="pending", operation_schema_version="ops-v1",
                  created_at=at, updated_at=at, expires_at=at + timedelta(minutes=10))
    values.update(fields)
    return _insert(conn, "auth_operations", **values)


def _candidate(conn, op, kind, at=T0, g=1, **fields):
    values = dict(id=uuid.uuid4(), auth_operation_id=op, operation_kind=kind, generation_no=g,
                  candidate_hash=h(f"candidate-{uuid.uuid4()}"), hash_key_version="k1", status="open",
                  created_at=at, authentication_expires_at=at + timedelta(minutes=10))
    values.update(fields)
    return _insert(conn, "auth_preauth_candidates", **values)


def _otp_request(conn, op, kind, at=T0, gen=1, expected=True, **fields):
    purpose = "sign_in" if kind == "supabase_email_sign_in" else "email_linking"
    values = dict(id=uuid.uuid4(), auth_operation_id=op, operation_kind=kind, purpose=purpose, code_generation=gen,
                  email_normalized="alice@example.com", status="issued" if expected else "issuing",
                  failure_count=0, requested_at=at, expires_at=at + timedelta(seconds=600),
                  provider_sent_at=at if expected else None,
                  expected_code_hmac=h(f"expected-{op}-{gen}") if expected else None,
                  expected_code_hmac_key_version="k1" if expected else None)
    values.update(fields)
    return _insert(conn, "auth_otp_requests", **values)


def _request_row(conn, request_id):
    return conn.execute(sa.text(
        "SELECT code_generation, purpose, expires_at, expected_code_hmac_key_version, expected_code_hmac, "
        "auth_operation_id FROM auth_otp_requests WHERE id = :id"), {"id": request_id}).one()


def _attempt(conn, request_id, n=1, code_hmac=None, at=None, **fields):
    gen, purpose, expires, key_version, expected, _ = _request_row(conn, request_id)
    code = expected if code_hmac is None else code_hmac
    values = dict(id=uuid.uuid4(), otp_request_id=request_id, attempt_no=n, code_generation=gen, purpose=purpose,
                  request_expires_at=expires, code_hmac_key_version=key_version, expected_code_hmac=expected,
                  code_hmac=code, local_match=(code == expected),
                  state="in_flight" if code == expected else "rejected",
                  started_at=at or (expires - timedelta(seconds=540)))
    if values["state"] == "rejected":
        values["resolved_at"] = values["started_at"]
    values.update(fields)
    return _insert(conn, "auth_otp_verification_attempts", **values)


def _verify(conn, request_id, at):
    attempt = _attempt(conn, request_id, at=at)
    _update(conn, "auth_otp_requests", request_id, status="verifying")
    _update(conn, "auth_otp_verification_attempts", attempt, state="accepted", resolved_at=at)
    _update(conn, "auth_otp_requests", request_id, status="verified", verified_at=at)
    return attempt


def _session(conn, op, user, method, at, invitation=None, identity=None, **fields):
    values = dict(id=uuid.uuid4(), user_id=user, created_by_operation_id=op, auth_method=method,
                  invitation_id=invitation, auth_identity_id=identity, status="active", created_at=at,
                  absolute_expires_at=at + timedelta(days=15), session_schema_version="session-v1")
    values.update(fields)
    return _insert(conn, "oryx_sessions", **values)


def _generation(conn, session, op, candidate, at, g=1, **fields):
    values = dict(id=uuid.uuid4(), session_id=session, auth_operation_id=op, candidate_id=candidate,
                  generation_no=g, active_secret_hash=h(f"active-{uuid.uuid4()}"), derivation_version="f2-hkdf-v1",
                  hash_key_version="k1", status="active", issued_at=at, delivery_status="pending")
    values.update(fields)
    return _insert(conn, "oryx_session_generations", **values)


def _result(conn, op, kind, committed_at, **fields):
    values = dict(operation_id=op, operation_kind=kind, operation_status="committed", committed_at=committed_at,
                  resume_until=committed_at + timedelta(minutes=5), result_version="result-v1",
                  result_payload={"operation_id": str(op)})
    values.update(fields)
    return _insert(conn, "auth_operation_results", **values)


def _sign_in_flow(conn, user=USER, at=T0):
    """Parcours A complet, dans l'ordre d'insertion du protocole."""
    identity = _identity(conn, user=user)
    subject = conn.execute(sa.text("SELECT subject FROM auth_identities WHERE id = :i"), {"i": identity}).scalar_one()
    op = _operation(conn, "supabase_email_sign_in", at=at, email_normalized="alice@example.com")
    candidate = _candidate(conn, op, "supabase_email_sign_in", at=at)
    request = _otp_request(conn, op, "supabase_email_sign_in", at=at + timedelta(minutes=1), expected=False)
    _update(conn, "auth_otp_requests", request, status="issued", expected_code_hmac=h(f"expected-{op}-1"),
            expected_code_hmac_key_version="k1", provider_sent_at=at + timedelta(minutes=1))
    _verify(conn, request, at + timedelta(minutes=2))
    commit = at + timedelta(minutes=3)
    _update(conn, "auth_operations", op, status="committed", committed_at=commit, user_id=user,
            auth_identity_id=identity, proven_provider="supabase", proven_issuer=ISS, proven_subject=subject,
            identity_resolution="existing_identity")
    _update(conn, "auth_preauth_candidates", candidate, status="bound", bound_at=commit)
    session = _session(conn, op, user, "supabase_email_sign_in", commit, identity=identity)
    generation = _generation(conn, session, op, candidate, commit)
    request_expires = at + timedelta(minutes=1, seconds=600)
    _result(conn, op, "supabase_email_sign_in", commit, session_id=session, candidate_id=candidate,
            candidate_bound_at=commit, generation_id=generation, auth_identity_id=identity,
            otp_request_id=request, otp_verified_at=at + timedelta(minutes=2), otp_expires_at=request_expires)
    return dict(op=op, candidate=candidate, request=request, session=session, generation=generation,
                identity=identity, commit=commit, subject=subject)


def _guest_flow(conn, at=T0, email=None):
    """Parcours B : invité sans compte Supabase (ni e-mail, ni identité)."""
    invitation = _insert(conn, "invitations", id=uuid.uuid4(), token_hash=h(f"invite-{uuid.uuid4()}"),
                         hash_key_version="k1", recipient_email_normalized=email, cohort="vague_0",
                         status="pending", invited_by_user_id=None, created_at=at - timedelta(days=1),
                         expires_at=at + timedelta(days=6))
    op = _operation(conn, "invitation_activation", at=at, invitation_id=invitation)
    candidate = _candidate(conn, op, "invitation_activation", at=at)
    guest = "oryx_" + uuid.uuid4().hex
    conn.execute(sa.text("INSERT INTO users (id, level, created_at) VALUES (:id, 'debutant', now())"), {"id": guest})
    commit = at + timedelta(minutes=1)
    _update(conn, "auth_operations", op, status="committed", committed_at=commit, user_id=guest)
    _update(conn, "invitations", invitation, status="accepted", accepted_at=commit)
    _update(conn, "auth_preauth_candidates", candidate, status="bound", bound_at=commit)
    session = _session(conn, op, guest, "invitation_activation", commit, invitation=invitation)
    generation = _generation(conn, session, op, candidate, commit)
    _result(conn, op, "invitation_activation", commit, session_id=session, candidate_id=candidate,
            candidate_bound_at=commit, generation_id=generation)
    return dict(invitation=invitation, op=op, candidate=candidate, guest=guest, session=session,
                generation=generation, commit=commit)


def _linking_operation(conn, guest_flow, at, email="guest@example.com"):
    op = _operation(conn, "email_linking", at=at, email_normalized=email, target_user_id=guest_flow["guest"],
                    user_id=guest_flow["guest"], initiating_session_id=guest_flow["session"],
                    initiating_session_method="invitation_activation")
    request = _otp_request(conn, op, "email_linking", at=at, email_normalized=email)
    _verify(conn, request, at + timedelta(minutes=1))
    return op, request


def _commit_linking(conn, op, request, identity, subject, resolution, at, otp_at):
    _update(conn, "auth_operations", op, status="committed", committed_at=at, auth_identity_id=identity,
            proven_provider="supabase", proven_issuer=ISS, proven_subject=subject, identity_resolution=resolution)
    _result(conn, op, "email_linking", at, auth_identity_id=identity, otp_request_id=request,
            otp_verified_at=otp_at, otp_expires_at=otp_at - timedelta(minutes=1) + timedelta(seconds=600))


@pytest.fixture
def conn(pg_url, pg_engine):  # noqa: F811
    """Schéma = head + données historiques ; tout ce que fait le test est annulé."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()


def _count(conn, table):
    return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


# --------------------------------------------------------------------------
# 2a. Migration
# --------------------------------------------------------------------------

def test_pg_upgrade_0016_to_0017_keeps_data_then_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR1_0016)
    _seed_historical(pg_engine)
    with pg_engine.begin() as c:
        _receipt(c, user=USER, position_id=3)
    tables = _tables(pg_engine)
    schema_0016 = _snapshot(pg_engine, tables)
    catalog_0016 = _catalog(pg_engine, tables)
    global_0016 = _global_catalog(pg_engine)
    data_0016 = _data(pg_engine, tables - {"alembic_version"})

    _run_alembic(pg_url, "upgrade", PR1_0017)
    assert _version(pg_engine) == PR1_0017
    assert _tables(pg_engine) == tables | set(CREATION_ORDER)
    others = tables - {"alembic_version"}
    assert _snapshot(pg_engine, others) == {t: schema_0016[t] for t in sorted(others)}
    assert _catalog(pg_engine, tables) == catalog_0016
    assert _global_catalog(pg_engine) == global_0016
    assert _data(pg_engine, others) == data_0016
    assert _data(pg_engine, set(CREATION_ORDER)) == {t: [] for t in sorted(CREATION_ORDER)}
    assert _compare_metadata(pg_engine) == []

    _run_alembic(pg_url, "downgrade", PR1_0016)
    assert _tables(pg_engine) == tables
    assert _snapshot(pg_engine, tables) == schema_0016
    assert _catalog(pg_engine, tables) == catalog_0016
    assert _data(pg_engine, others) == data_0016
    _run_alembic(pg_url, "upgrade", PR1_0017)
    assert _compare_metadata(pg_engine) == []


def test_pg_full_chain_0014_to_head_and_back_preserves_historical_data(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    _seed_historical(pg_engine)
    tables = _tables(pg_engine)
    schema_0014 = _snapshot(pg_engine, tables)
    data_0014 = _data(pg_engine, tables - {"alembic_version"})

    _run_alembic(pg_url, "upgrade", "head")
    data_head = _data(pg_engine, tables - {"alembic_version"})
    assert {t: r for t, r in data_head.items() if t != "users"} == {
        t: r for t, r in data_0014.items() if t != "users"}
    assert data_head["users"] == [row + (0,) for row in data_0014["users"]]
    _exercise_legacy_backend(pg_engine)
    with pg_engine.begin() as c:
        c.execute(sa.text("DELETE FROM users WHERE id = 'legacy-new'"))

    _run_alembic(pg_url, "downgrade", R1D1)
    assert _tables(pg_engine) == tables
    assert _snapshot(pg_engine, tables) == schema_0014
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0014


def _constraint_and_index_ddl(conn, schema, tables):
    defs = {}
    for table in sorted(tables):
        constraints = conn.execute(sa.text(
            "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = CAST(:t AS regclass) ORDER BY conname"), {"t": f"{schema}.{table}"}).all()
        indexes = conn.execute(sa.text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :s AND tablename = :t ORDER BY indexname"
        ), {"s": schema, "t": table}).all()
        columns = conn.execute(sa.text(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"), {"s": schema, "t": table}).all()
        defs[table] = [tuple(str(v).replace(f"{schema}.", "") if v is not None else None for v in row)
                       for row in [*columns, *constraints, *indexes]]
    return defs


def test_pg_migration_ddl_is_identical_to_model_ddl(pg_url, pg_engine):  # noqa: F811
    """compare_metadata ignore les CHECK et les noms : on recrée le schéma
    des modèles dans un schéma model_ref de la même base jetable et on
    compare colonnes, contraintes (CHECK, FK composites, UNIQUE) et index."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    with pg_engine.begin() as c:
        assert_disposable_connection(c)
        c.execute(sa.text("DROP SCHEMA IF EXISTS model_ref CASCADE"))
        c.execute(sa.text("CREATE SCHEMA model_ref"))
    try:
        Base.metadata.create_all(pg_engine.execution_options(schema_translate_map={None: "model_ref"}))
        with pg_engine.connect() as c:
            compared = PR1_MODEL_TABLES | {"users"}
            assert _constraint_and_index_ddl(c, "public", compared) == _constraint_and_index_ddl(c, "model_ref",
                                                                                               compared)
    finally:
        with pg_engine.begin() as c:
            assert_disposable_connection(c)
            c.execute(sa.text("DROP SCHEMA IF EXISTS model_ref CASCADE"))


def test_pg_catalog_has_no_trigger_function_or_cleartext_secret(conn):
    assert conn.execute(sa.text("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal")).scalar_one() == 0
    columns = conn.execute(sa.text(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = ANY(:tables)"), {"tables": sorted(PR1_MODEL_TABLES)}).all()
    hex_columns = {(t, c) for t, c in columns if c.endswith(("_hash", "_hmac")) or c == "request_fingerprint"}
    assert len(hex_columns) == 8
    for table, column in hex_columns:
        defs = conn.execute(sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) "
            "AND contype = 'c'"), {"t": f"public.{table}"}).scalars().all()
        # PostgreSQL réécrit "col ~ '...'" en "((col)::text ~ '...'::text)".
        assert any(re.search(rf"\(?{column}\)?(::text)? ~ '\^\[0-9a-f\]\{{64\}}\$'", d) for d in defs), (table, column)
    assert not [c for _, c in columns if c in FORBIDDEN_COLUMN_NAMES]


# --------------------------------------------------------------------------
# 2b. Ordre d'insertion des parcours
# --------------------------------------------------------------------------

def test_pg_sign_in_flow_inserts_in_protocol_order(conn):
    flow = _sign_in_flow(conn)
    assert conn.execute(sa.text(
        "SELECT o.status, s.auth_method, g.generation_no, g.delivery_status, r.resume_until - r.committed_at "
        "FROM auth_operation_results r JOIN auth_operations o ON o.id = r.operation_id "
        "JOIN oryx_sessions s ON s.id = r.session_id JOIN oryx_session_generations g ON g.id = r.generation_id "
        "WHERE r.operation_id = :op"), {"op": flow["op"]}).one() == (
        "committed", "supabase_email_sign_in", 1, "pending", timedelta(minutes=5))


def test_pg_guest_invitation_flow_needs_no_email_nor_identity(conn):
    flow = _guest_flow(conn)
    assert _count(conn, "auth_identities") == 0
    assert _count(conn, "auth_otp_requests") == 0
    row = conn.execute(sa.text(
        "SELECT s.user_id, s.auth_method, s.invitation_id, s.auth_identity_id, o.email_normalized, "
        "i.recipient_email_normalized, i.status FROM oryx_sessions s "
        "JOIN auth_operations o ON o.id = s.created_by_operation_id JOIN invitations i ON i.id = o.invitation_id"
    )).one()
    assert row == (flow["guest"], "invitation_activation", flow["invitation"], None, None, None, "accepted")
    # Usage unique : une seconde activation validée de la même invitation est refusée.
    other = _operation(conn, "invitation_activation", invitation_id=flow["invitation"])
    _rejects(conn, "uq_auth_operations_one_committed_invitation", lambda: _update(
        conn, "auth_operations", other, status="committed", committed_at=T0 + timedelta(minutes=2),
        user_id=flow["guest"]))


def test_pg_email_linking_new_then_existing_identity(conn):
    guest = _guest_flow(conn)
    # C1 : identité absente -> créée pour le profil cible.
    at = T0 + timedelta(hours=2)
    op, request = _linking_operation(conn, guest, at)
    subject = str(uuid.uuid4())
    identity = _identity(conn, user=guest["guest"], subject=subject, email="guest@example.com")
    _commit_linking(conn, op, request, identity, subject, "new_identity", at + timedelta(minutes=2),
                    at + timedelta(minutes=1))
    # C2 : identité déjà liée au même profil -> existing_identity.
    at2 = T0 + timedelta(hours=3)
    op2, request2 = _linking_operation(conn, guest, at2)
    _commit_linking(conn, op2, request2, identity, subject, "existing_identity", at2 + timedelta(minutes=2),
                    at2 + timedelta(minutes=1))
    assert conn.execute(sa.text(
        "SELECT count(*), count(DISTINCT auth_identity_id) FROM auth_operation_results "
        "WHERE operation_kind = 'email_linking' AND session_id IS NULL")).one() == (2, 1)
    assert _count(conn, "oryx_sessions") == 1  # aucune nouvelle session


def test_pg_email_linking_conflict_never_merges(conn):
    guest = _guest_flow(conn)
    foreign_subject = str(uuid.uuid4())
    foreign = _identity(conn, user=USER, subject=foreign_subject, email="taken@example.com")
    at = T0 + timedelta(hours=2)
    op, request = _linking_operation(conn, guest, at, email="taken@example.com")
    # Rattacher l'identité d'un autre profil : refusé par la FK (identité, user).
    _rejects(conn, "auth_operations_identity_user_fkey", lambda: _update(
        conn, "auth_operations", op, status="committed", committed_at=at + timedelta(minutes=2),
        auth_identity_id=foreign, proven_provider="supabase", proven_issuer=ISS, proven_subject=foreign_subject,
        identity_resolution="existing_identity"))
    # Un conflit ne peut jamais être committed (ck_auth_operations_commit,
    # vérifié avant ck_auth_operations_no_merge par ordre de nom)...
    _rejects(conn, "ck_auth_operations_commit", lambda: _update(
        conn, "auth_operations", op, status="committed", committed_at=at + timedelta(minutes=2),
        proven_provider="supabase", proven_issuer=ISS, proven_subject=foreign_subject,
        identity_resolution="conflict_other_profile"))
    # ... ni garder l'identité de l'autre profil, même en échec.
    _rejects(conn, "ck_auth_operations_no_merge", lambda: _update(
        conn, "auth_operations", op, status="failed", failure_code="identity_owned_by_other_profile",
        auth_identity_id=foreign, proven_provider="supabase", proven_issuer=ISS, proven_subject=foreign_subject,
        identity_resolution="conflict_other_profile"))
    # Issue normale : failed, sans identité, preuve conservée, aucun résultat.
    _update(conn, "auth_operations", op, status="failed", failure_code="identity_owned_by_other_profile",
            proven_provider="supabase", proven_issuer=ISS, proven_subject=foreign_subject,
            identity_resolution="conflict_other_profile")
    _rejects(conn, "auth_operation_results_operation_fkey", lambda: _result(
        conn, op, "email_linking", at + timedelta(minutes=2), auth_identity_id=foreign, otp_request_id=request,
        otp_verified_at=at + timedelta(minutes=1), otp_expires_at=at + timedelta(seconds=600)))
    assert conn.execute(sa.text("SELECT user_id FROM auth_identities WHERE id = :i"),
                        {"i": foreign}).scalar_one() == USER


def test_pg_email_linking_requires_a_guest_session_of_the_target(conn):
    guest = _guest_flow(conn)
    signed = _sign_in_flow(conn, user=OTHER, at=T0 + timedelta(hours=1))
    at = T0 + timedelta(hours=2)
    base = dict(email_normalized="guest@example.com", initiating_session_method="invitation_activation")
    # Session d'un autre profil.
    _rejects(conn, "auth_operations_linking_session_fkey", lambda: _operation(
        conn, "email_linking", at=at, target_user_id=USER, user_id=USER, initiating_session_id=guest["session"],
        **base))
    # Session ouverte par e-mail (non invitée).
    _rejects(conn, "auth_operations_linking_session_fkey", lambda: _operation(
        conn, "email_linking", at=at, target_user_id=OTHER, user_id=OTHER,
        initiating_session_id=signed["session"], **base))
    _rejects(conn, "ck_auth_operations_initiating_session_method", lambda: _operation(
        conn, "email_linking", at=at, target_user_id=OTHER, user_id=OTHER, initiating_session_id=signed["session"],
        email_normalized="x@example.com", initiating_session_method="supabase_email_sign_in"))
    # Sans session / sans cible / profil résolu différent de la cible.
    _rejects(conn, "ck_auth_operations_linking_fields", lambda: _operation(
        conn, "email_linking", at=at, target_user_id=guest["guest"], email_normalized="g@example.com"))
    _rejects(conn, "ck_auth_operations_linking_target", lambda: _operation(
        conn, "email_linking", at=at, target_user_id=guest["guest"], user_id=USER,
        initiating_session_id=guest["session"], **base))
    # Champs de liaison interdits aux autres kinds.
    _rejects(conn, "ck_auth_operations_linking_fields", lambda: _operation(
        conn, "supabase_email_sign_in", at=at, email_normalized="a@example.com", target_user_id=USER))
    # Une opération email_linking n'ouvre jamais de session ni de candidat.
    op, _ = _linking_operation(conn, guest, at)
    _rejects(conn, "auth_preauth_candidates_operation_kind_fkey|ck_auth_preauth_candidates_operation_kind",
             lambda: _candidate(conn, op, "email_linking"))
    _rejects(conn, "ck_oryx_sessions_auth_method", lambda: _session(
        conn, op, guest["guest"], "email_linking", at))


def test_pg_resume_after_authentication_window_creates_nothing(conn):
    """Précision 2 : fenêtre d'authentification (10 min) distincte de la
    reprise (5 min après commit) ; reprise possible après expiration de la
    première ; aucune nouvelle authentification ni prolongation."""
    created = T0
    commit = T0 + timedelta(minutes=9, seconds=30)  # dans la fenêtre d'authentification
    invitation = _insert(conn, "invitations", id=uuid.uuid4(), token_hash=h("late"), hash_key_version="k1",
                         cohort="vague_0", status="pending", created_at=created - timedelta(days=1),
                         expires_at=created + timedelta(days=6))
    op = _operation(conn, "invitation_activation", at=created, invitation_id=invitation)
    candidate = _candidate(conn, op, "invitation_activation", at=created)
    guest = "oryx_" + uuid.uuid4().hex
    conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:id, 'debutant')"), {"id": guest})
    _update(conn, "auth_operations", op, status="committed", committed_at=commit, user_id=guest)
    _update(conn, "invitations", invitation, status="accepted", accepted_at=commit)
    _update(conn, "auth_preauth_candidates", candidate, status="bound", bound_at=commit)
    session = _session(conn, op, guest, "invitation_activation", commit, invitation=invitation)
    generation = _generation(conn, session, op, candidate, commit)
    _result(conn, op, "invitation_activation", commit, session_id=session, candidate_id=candidate,
            candidate_bound_at=commit, generation_id=generation)
    resume_at = T0 + timedelta(minutes=12)  # fenêtre d'authentification expirée, reprise encore ouverte
    row = conn.execute(sa.text(
        "SELECT c.status, c.authentication_expires_at < :now, r.resume_until > :now, s.absolute_expires_at "
        "FROM auth_preauth_candidates c JOIN auth_operation_results r ON r.candidate_id = c.id "
        "JOIN oryx_sessions s ON s.id = r.session_id WHERE c.id = :c"), {"now": resume_at, "c": candidate}).one()
    assert row == ("bound", True, True, commit + timedelta(days=15))
    # La reprise ne crée rien : ni seconde session, ni génération, ni résultat, ni candidat lié.
    _rejects(conn, "uq_oryx_sessions_created_by_operation", lambda: _session(
        conn, op, guest, "invitation_activation", resume_at, invitation=invitation))
    _rejects(conn, "uq_oryx_session_generations_session|uq_oryx_session_generations_candidate",
             lambda: _generation(conn, session, op, candidate, resume_at))
    _rejects(conn, "auth_operation_results_pkey", lambda: _result(
        conn, op, "invitation_activation", commit, session_id=session, candidate_id=candidate,
        candidate_bound_at=commit, generation_id=generation))
    late = _candidate(conn, op, "invitation_activation", at=resume_at, g=2)
    _rejects(conn, "uq_auth_preauth_candidates_one_ever_bound|ck_auth_preauth_candidates_timeline",
             lambda: _update(conn, "auth_preauth_candidates", late, status="bound", bound_at=resume_at))
    # Une liaison après la fenêtre d'authentification est impossible.
    op2 = _operation(conn, "supabase_email_sign_in", at=T0, email_normalized="late@example.com")
    stale = _candidate(conn, op2, "supabase_email_sign_in", at=T0)
    _rejects(conn, "ck_auth_preauth_candidates_timeline", lambda: _update(
        conn, "auth_preauth_candidates", stale, status="bound", bound_at=T0 + timedelta(minutes=10, microseconds=1)))
    # La fenêtre de reprise vaut exactement cinq minutes ; la session reste à quinze jours.
    _rejects(conn, "ck_auth_operation_results_resume_window", lambda: conn.execute(sa.text(
        "UPDATE auth_operation_results SET resume_until = resume_until + interval '1 microsecond' "
        "WHERE operation_id = :op"), {"op": op}))
    _rejects(conn, "ck_oryx_sessions_absolute_ttl", lambda: conn.execute(sa.text(
        "UPDATE oryx_sessions SET absolute_expires_at = absolute_expires_at + interval '5 minutes' WHERE id = :s"),
        {"s": session}))


# --------------------------------------------------------------------------
# 2c. Identités, invitations, opérations
# --------------------------------------------------------------------------

def test_pg_identity_is_provider_issuer_subject_and_belongs_to_one_profile(conn):
    subject = str(uuid.uuid4())
    _identity(conn, user=USER, subject=subject, email="alice@example.com")
    _identity(conn, user=USER, subject=str(uuid.uuid4()), email="alice.pro@example.com")  # 2 identités, 1 profil
    _rejects(conn, "uq_auth_identities_provider_issuer_subject",
             lambda: _identity(conn, user=OTHER, subject=subject, email="other@example.com"))
    # Même subject chez un autre issuer : autre identité. Subject != e-mail.
    _insert(conn, "auth_identities", id=uuid.uuid4(), user_id=OTHER, provider="supabase",
            issuer="https://other.supabase.co/auth/v1", subject=subject, created_at=T0)
    _identity(conn, user=OTHER, subject=str(uuid.uuid4()), email=None)
    _rejects(conn, "ck_auth_identities_provider", lambda: _insert(
        conn, "auth_identities", id=uuid.uuid4(), user_id=USER, provider="google", issuer=ISS, subject="s",
        created_at=T0))
    _rejects(conn, "ck_auth_identities_verified_email_normalized", lambda: _identity(conn, email="Alice@Example.com"))
    _rejects(conn, "ck_auth_identities_email_verified", lambda: _insert(
        conn, "auth_identities", id=uuid.uuid4(), user_id=USER, provider="supabase", issuer=ISS, subject="s2",
        verified_email_normalized="a@example.com", created_at=T0))
    _rejects(conn, "ck_auth_identities_issuer", lambda: _insert(
        conn, "auth_identities", id=uuid.uuid4(), user_id=USER, provider="supabase", issuer="http://x",
        subject="s3", created_at=T0))


def test_pg_identity_cannot_move_once_used(conn):
    flow = _sign_in_flow(conn)
    _rejects(conn, "auth_operations_identity_user_fkey", lambda: conn.execute(sa.text(
        "UPDATE auth_identities SET user_id = :o WHERE id = :i"), {"o": OTHER, "i": flow["identity"]}))
    _rejects(conn, "auth_operations_proven_identity_fkey", lambda: conn.execute(sa.text(
        "UPDATE auth_identities SET subject = 'changed' WHERE id = :i"), {"i": flow["identity"]}))


def test_pg_invitation_constraints(conn):
    def invitation(**fields):
        values = dict(id=uuid.uuid4(), token_hash=h(f"t-{uuid.uuid4()}"), hash_key_version="k1", cohort="vague_0",
                      status="pending", created_at=T0, expires_at=T0 + timedelta(days=7))
        values.update(fields)
        return _insert(conn, "invitations", **values)

    invitation()
    invitation()  # plusieurs invitations sans e-mail
    first = invitation(recipient_email_normalized="bob@example.com")
    _rejects(conn, "uq_invitations_one_pending_email", lambda: invitation(recipient_email_normalized="bob@example.com"))
    _update(conn, "invitations", first, status="accepted", accepted_at=T0 + timedelta(hours=1))
    invitation(recipient_email_normalized="bob@example.com")  # possible après acceptation
    _rejects(conn, "ck_invitations_state", lambda: invitation(status="accepted"))
    _rejects(conn, "ck_invitations_state", lambda: invitation(status="accepted", accepted_at=T0 + timedelta(days=8)))
    _rejects(conn, "ck_invitations_state", lambda: invitation(status="revoked"))
    _rejects(conn, "ck_invitations_token_hash_format", lambda: invitation(token_hash="clear-token"))
    _rejects(conn, "ck_invitations_pii", lambda: invitation(recipient_email_normalized="c@example.com",
                                                            pii_redacted_at=T0))


def test_pg_operation_fields_depend_on_kind(conn):
    invitation = _insert(conn, "invitations", id=uuid.uuid4(), token_hash=h("i"), hash_key_version="k1",
                         cohort="vague_0", status="pending", created_at=T0, expires_at=T0 + timedelta(days=7))
    _operation(conn, "invitation_activation", invitation_id=invitation)  # sans e-mail ni identité
    _rejects(conn, "ck_auth_operations_invitation_fields", lambda: _operation(
        conn, "invitation_activation", invitation_id=invitation, email_normalized="a@example.com"))
    _rejects(conn, "ck_auth_operations_invitation_kind", lambda: _operation(conn, "invitation_activation"))
    _rejects(conn, "ck_auth_operations_invitation_kind", lambda: _operation(
        conn, "supabase_email_sign_in", email_normalized="a@example.com", invitation_id=invitation))
    _rejects(conn, "ck_auth_operations_email_fields", lambda: _operation(conn, "supabase_email_sign_in"))
    op = _operation(conn, "supabase_email_sign_in", email_normalized="a@example.com")
    _rejects(conn, "ck_auth_operations_commit", lambda: _update(
        conn, "auth_operations", op, status="committed", committed_at=T0 + timedelta(minutes=1), user_id=USER))
    _rejects(conn, "ck_auth_operations_commit", lambda: _update(conn, "auth_operations", op, status="committed"))
    late = _operation(conn, "invitation_activation", invitation_id=invitation)
    _rejects(conn, "ck_auth_operations_commit_in_time", lambda: _update(
        conn, "auth_operations", late, status="committed", committed_at=T0 + timedelta(minutes=11), user_id=USER))
    _rejects(conn, "ck_auth_operations_failure", lambda: _update(conn, "auth_operations", op, status="failed"))
    _rejects(conn, "ck_auth_operations_proven_identity", lambda: _update(
        conn, "auth_operations", op, proven_provider="supabase"))
    _rejects(conn, "ck_auth_operations_proven_identity", lambda: _update(
        conn, "auth_operations", op, identity_resolution="new_identity"))
    _rejects(conn, "ck_auth_operations_kind", lambda: _operation(conn, "account_merge"))
    _rejects(conn, "ck_auth_operations_pii", lambda: _operation(
        conn, "supabase_email_sign_in", email_normalized="a@example.com", pii_redacted_at=T0))
    _operation(conn, "supabase_email_sign_in", pii_redacted_at=T0)  # e-mail effacé (rétention future)


COMMIT_REJECTION = "ck_auth_operations_commit|ck_auth_operations_proven_identity"


def _otp_operation_awaiting_commit(conn, kind):
    """Opération OTP pending de chaque kind et les valeurs d'un commit
    complet (identité appartenant au profil, preuve fournisseur complète)."""
    at = T0 + timedelta(hours=3)
    if kind == "email_linking":
        guest = _guest_flow(conn)
        op, _ = _linking_operation(conn, guest, at)
        user = guest["guest"]
    else:
        op = _operation(conn, kind, at=at, email_normalized="alice@example.com")
        user = USER
    subject = str(uuid.uuid4())
    identity = _identity(conn, user=user, subject=subject)
    complete = dict(status="committed", committed_at=at + timedelta(minutes=2), user_id=user,
                    auth_identity_id=identity, proven_provider="supabase", proven_issuer=ISS,
                    proven_subject=subject, identity_resolution="new_identity")
    return op, complete


@pytest.mark.parametrize("kind", ["supabase_email_sign_in", "email_linking"])
def test_pg_committed_otp_operation_requires_complete_identity_proof(conn, kind):
    """Audit PR 1 : un CHECK évalué à NULL n'est pas rejeté par PostgreSQL ;
    une opération OTP committed doit porter explicitement identité,
    résolution et preuve fournisseur complètes (aucun NULL, même partiel)."""
    op, complete = _otp_operation_awaiting_commit(conn, kind)
    proof = ("proven_provider", "proven_issuer", "proven_subject")
    variants = {
        "résolution et preuve absentes": dict(identity_resolution=None, **{c: None for c in proof}),
        "résolution absente, preuve complète": dict(identity_resolution=None),
        "résolution conflit": dict(identity_resolution="conflict_other_profile"),
        "preuve absente": {c: None for c in proof},
        "identité absente": dict(auth_identity_id=None),
        "identité et résolution absentes": dict(auth_identity_id=None, identity_resolution=None),
        "tout absent": dict(auth_identity_id=None, identity_resolution=None, **{c: None for c in proof}),
    }
    for column in proof:
        variants[f"{column} seul absent"] = {column: None}
        variants[f"{column} seul présent"] = {c: None for c in proof if c != column}
    for label, override in variants.items():
        with pytest.raises(sa.exc.IntegrityError, match=COMMIT_REJECTION):
            with conn.begin_nested():
                _update(conn, "auth_operations", op, **{**complete, **override})
        assert conn.execute(sa.text("SELECT status FROM auth_operations WHERE id = :o"),
                            {"o": op}).scalar_one() == "pending", label
    # Même règle à l'insertion directe d'une opération committed sans preuve.
    with pytest.raises(sa.exc.IntegrityError, match=COMMIT_REJECTION):
        with conn.begin_nested():
            fields = {**complete, "identity_resolution": None, **{c: None for c in proof}}
            if kind == "email_linking":
                fields.update(conn.execute(sa.text(
                    "SELECT email_normalized, target_user_id, initiating_session_id, initiating_session_method "
                    "FROM auth_operations WHERE id = :o"), {"o": op}).mappings().one())
            else:
                fields["email_normalized"] = "alice@example.com"
            _operation(conn, kind, at=T0 + timedelta(hours=3), **fields)
    # Une opération valide avec preuve complète reste acceptée.
    _update(conn, "auth_operations", op, **complete)
    assert conn.execute(sa.text(
        "SELECT status, identity_resolution, proven_subject IS NOT NULL FROM auth_operations WHERE id = :o"),
        {"o": op}).one() == ("committed", "new_identity", True)


def test_pg_invitation_commit_still_needs_no_email_nor_external_identity(conn):
    flow = _committed_invitation_op(conn, T0 + timedelta(hours=4))
    assert conn.execute(sa.text(
        "SELECT status, email_normalized, auth_identity_id, identity_resolution, proven_provider, proven_issuer, "
        "proven_subject FROM auth_operations WHERE id = :o"), {"o": flow["op"]}).one() == (
        "committed", None, None, None, None, None, None)


# --------------------------------------------------------------------------
# 2d. Candidats, sessions, générations, résultats
# --------------------------------------------------------------------------

def test_pg_candidate_window_states_and_transitions_after_binding(conn):
    flow = _sign_in_flow(conn)
    # Correction 5 : la liaison historique n'empêche ni clôture ni révocation.
    _update(conn, "auth_preauth_candidates", flow["candidate"], status="closed", closed_at=flow["commit"] +
            timedelta(minutes=5))
    _update(conn, "auth_preauth_candidates", flow["candidate"], status="revoked")
    assert conn.execute(sa.text("SELECT status, bound_at FROM auth_preauth_candidates WHERE id = :c"),
                        {"c": flow["candidate"]}).one() == ("revoked", flow["commit"])
    # bound_at est figé par le résultat canonique (écrit une seule fois).
    _rejects(conn, "auth_operation_results_candidate_fkey", lambda: _update(
        conn, "auth_preauth_candidates", flow["candidate"], bound_at=flow["commit"] - timedelta(seconds=1)))
    op = _operation(conn, "supabase_email_sign_in", email_normalized="c@example.com")
    _rejects(conn, "ck_auth_preauth_candidates_authentication_window", lambda: _candidate(
        conn, op, "supabase_email_sign_in", authentication_expires_at=T0 + timedelta(minutes=10, microseconds=1)))
    first = _candidate(conn, op, "supabase_email_sign_in")
    _rejects(conn, "uq_auth_preauth_candidates_one_open", lambda: _candidate(conn, op, "supabase_email_sign_in", g=2))
    _update(conn, "auth_preauth_candidates", first, status="superseded", closed_at=T0 + timedelta(minutes=1))
    second = _candidate(conn, op, "supabase_email_sign_in", g=2)
    _rejects(conn, "uq_auth_preauth_candidates_operation_generation",
             lambda: _candidate(conn, op, "supabase_email_sign_in", g=2, status="expired", closed_at=T0))
    _rejects(conn, "ck_auth_preauth_candidates_state", lambda: _update(
        conn, "auth_preauth_candidates", second, status="closed", closed_at=T0 + timedelta(minutes=2)))
    _rejects(conn, "ck_auth_preauth_candidates_state", lambda: _update(
        conn, "auth_preauth_candidates", second, status="expired", bound_at=T0 + timedelta(minutes=1),
        closed_at=T0 + timedelta(minutes=2)))
    _rejects(conn, "ck_auth_preauth_candidates_candidate_hash_format", lambda: _candidate(
        conn, op, "supabase_email_sign_in", g=3, status="expired", closed_at=T0, candidate_hash="clear"))
    _rejects(conn, "auth_preauth_candidates_operation_kind_fkey", lambda: _candidate(
        conn, op, "invitation_activation", g=4, status="expired", closed_at=T0))


def _committed_invitation_op(conn, at):
    """Opération invitation committed et candidat lié, SANS session."""
    invitation = _insert(conn, "invitations", id=uuid.uuid4(), token_hash=h(f"inv-{uuid.uuid4()}"),
                         hash_key_version="k1", cohort="vague_0", status="pending",
                         created_at=at - timedelta(days=1), expires_at=at + timedelta(days=6))
    op = _operation(conn, "invitation_activation", at=at, invitation_id=invitation)
    candidate = _candidate(conn, op, "invitation_activation", at=at)
    guest = "oryx_" + uuid.uuid4().hex
    conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:id, 'debutant')"), {"id": guest})
    commit = at + timedelta(minutes=1)
    _update(conn, "auth_operations", op, status="committed", committed_at=commit, user_id=guest)
    _update(conn, "invitations", invitation, status="accepted", accepted_at=commit)
    _update(conn, "auth_preauth_candidates", candidate, status="bound", bound_at=commit)
    return dict(op=op, invitation=invitation, candidate=candidate, guest=guest, commit=commit)


def test_pg_session_and_generation_constraints(conn):
    flow = _sign_in_flow(conn)
    guest = _guest_flow(conn)
    fresh = _committed_invitation_op(conn, T0 + timedelta(hours=1))
    other = _committed_invitation_op(conn, T0 + timedelta(hours=2))
    op = _operation(conn, "supabase_email_sign_in", email_normalized="d@example.com")
    # Session d'une opération non committed (user_id NULL, sans identité) : refusée.
    _rejects(conn, "oryx_sessions_operation_holder_fkey|oryx_sessions_operation_identity_fkey", lambda: _session(
        conn, op, USER, "supabase_email_sign_in", T0, identity=flow["identity"]))
    # Méthode / provenance incohérentes.
    _rejects(conn, "ck_oryx_sessions_identity_provenance", lambda: _session(
        conn, fresh["op"], fresh["guest"], "invitation_activation", fresh["commit"], invitation=fresh["invitation"],
        identity=flow["identity"]))
    _rejects(conn, "ck_oryx_sessions_invitation_provenance", lambda: _session(
        conn, fresh["op"], fresh["guest"], "invitation_activation", fresh["commit"]))
    _rejects(conn, "oryx_sessions_operation_holder_fkey", lambda: _session(
        conn, fresh["op"], USER, "invitation_activation", fresh["commit"], invitation=fresh["invitation"]))
    _rejects(conn, "oryx_sessions_operation_holder_fkey|oryx_sessions_operation_identity_fkey", lambda: _session(
        conn, fresh["op"], fresh["guest"], "supabase_email_sign_in", fresh["commit"], identity=flow["identity"]))
    _rejects(conn, "oryx_sessions_operation_invitation_fkey", lambda: _session(
        conn, fresh["op"], fresh["guest"], "invitation_activation", fresh["commit"], invitation=guest["invitation"]))
    _rejects(conn, "ck_oryx_sessions_absolute_ttl", lambda: _session(
        conn, fresh["op"], fresh["guest"], "invitation_activation", fresh["commit"], invitation=fresh["invitation"],
        absolute_expires_at=fresh["commit"] + timedelta(days=15, microseconds=1)))
    _rejects(conn, "uq_oryx_sessions_created_by_operation", lambda: _session(
        conn, guest["op"], guest["guest"], "invitation_activation", T0, invitation=guest["invitation"]))
    session = _session(conn, fresh["op"], fresh["guest"], "invitation_activation", fresh["commit"],
                       invitation=fresh["invitation"])
    # Générations : une seule par session, candidat et session de la même opération, même g.
    _rejects(conn, "uq_oryx_session_generations_session|uq_oryx_session_generations_candidate", lambda: _generation(
        conn, flow["session"], flow["op"], flow["candidate"], T0))
    _rejects(conn, "oryx_session_generations_session_fkey", lambda: _generation(
        conn, session, other["op"], other["candidate"], fresh["commit"]))
    _rejects(conn, "oryx_session_generations_candidate_fkey", lambda: _generation(
        conn, session, fresh["op"], other["candidate"], fresh["commit"]))
    _rejects(conn, "oryx_session_generations_candidate_fkey", lambda: _generation(
        conn, session, fresh["op"], fresh["candidate"], fresh["commit"], g=2))
    _generation(conn, session, fresh["op"], fresh["candidate"], fresh["commit"])
    _rejects(conn, "oryx_session_generations_candidate_fkey", lambda: conn.execute(sa.text(
        "UPDATE oryx_session_generations SET generation_no = 2 WHERE id = :g"), {"g": flow["generation"]}))
    # Preuve de livraison : première présentation valide, jamais avant l'émission ni après la révocation.
    g = flow["generation"]
    _rejects(conn, "ck_oryx_session_generations_delivery", lambda: _update(
        conn, "oryx_session_generations", g, delivery_status="confirmed"))
    _rejects(conn, "ck_oryx_session_generations_delivery", lambda: _update(
        conn, "oryx_session_generations", g, delivery_status="confirmed", delivery_confirmed_at=flow["commit"]))
    _rejects(conn, "ck_oryx_session_generations_delivery", lambda: _update(
        conn, "oryx_session_generations", g, delivery_status="confirmed",
        delivery_proof_version="first-presentation-v1",
        delivery_confirmed_at=flow["commit"] - timedelta(seconds=1)))
    _update(conn, "oryx_session_generations", g, delivery_status="confirmed",
            delivery_confirmed_at=flow["commit"] + timedelta(seconds=2), delivery_proof_version="first-presentation-v1")
    # Révocation : générations puis session ; cohérence imposée.
    _rejects(conn, "ck_oryx_session_generations_revocation", lambda: _update(
        conn, "oryx_session_generations", g, status="revoked"))
    _rejects(conn, "ck_oryx_session_generations_delivery", lambda: _update(
        conn, "oryx_session_generations", g, status="revoked", revoked_at=flow["commit"] + timedelta(seconds=1),
        revocation_reason="logout"))
    _update(conn, "oryx_session_generations", g, status="revoked", revoked_at=flow["commit"] + timedelta(hours=1),
            revocation_reason="logout")
    _update(conn, "oryx_sessions", flow["session"], status="revoked", revoked_at=flow["commit"] + timedelta(hours=1),
            revocation_reason="logout")
    _rejects(conn, "ck_oryx_sessions_revocation_reason", lambda: _update(
        conn, "oryx_sessions", guest["session"], status="revoked", revoked_at=T0 + timedelta(hours=1),
        revocation_reason="expired"))
    _rejects(conn, "ck_oryx_session_generations_active_secret_hash_format", lambda: _update(
        conn, "oryx_session_generations", guest["generation"], active_secret_hash="cookie-value"))


def test_pg_result_constraints(conn):
    flow = _sign_in_flow(conn)
    linking_guest = _guest_flow(conn, at=T0 + timedelta(hours=1))
    pending_op, pending_request = _linking_operation(conn, linking_guest, T0 + timedelta(hours=2))
    # Résultat d'une opération non committed : refusé.
    _rejects(conn, "auth_operation_results_operation_fkey|auth_operation_results_identity_fkey", lambda: _result(
        conn, pending_op, "email_linking", T0 + timedelta(hours=2, minutes=2), auth_identity_id=flow["identity"],
        otp_request_id=pending_request, otp_verified_at=T0 + timedelta(hours=2, minutes=1),
        otp_expires_at=T0 + timedelta(hours=2, seconds=600)))
    # 'committed' est terminal : figé par le résultat.
    _rejects(conn, "auth_operation_results_operation_fkey", lambda: _update(
        conn, "auth_operations", flow["op"], status="expired", committed_at=None))
    # L'effacement des données personnelles reste possible.
    _update(conn, "auth_operations", flow["op"], email_normalized=None, pii_redacted_at=T0 + timedelta(days=30))
    guest = _guest_flow(conn)
    _rejects(conn, "ck_auth_operation_results_session_parts", lambda: conn.execute(sa.text(
        "UPDATE auth_operation_results SET candidate_bound_at = NULL WHERE operation_id = :o"), {"o": guest["op"]}))
    _rejects(conn, "ck_auth_operation_results_candidate_bound_at", lambda: conn.execute(sa.text(
        "UPDATE auth_operation_results SET candidate_bound_at = candidate_bound_at - interval '1 second' "
        "WHERE operation_id = :o"), {"o": guest["op"]}))
    _rejects(conn, "ck_auth_operation_results_identity", lambda: conn.execute(sa.text(
        "UPDATE auth_operation_results SET auth_identity_id = :i WHERE operation_id = :o"),
        {"i": flow["identity"], "o": guest["op"]}))
    _rejects(conn, "ck_auth_operation_results_otp_recent", lambda: conn.execute(sa.text(
        "UPDATE auth_operation_results SET otp_verified_at = committed_at + interval '1 second' "
        "WHERE operation_id = :o"), {"o": flow["op"]}))


def test_pg_otp_proof_must_be_recent(conn):
    guest = _guest_flow(conn)
    at = T0 + timedelta(hours=2)
    op = _operation(conn, "email_linking", at=at, expires_at=at + timedelta(minutes=30),
                    email_normalized="guest@example.com", target_user_id=guest["guest"], user_id=guest["guest"],
                    initiating_session_id=guest["session"], initiating_session_method="invitation_activation")
    requested = at + timedelta(minutes=1)
    request = _otp_request(conn, op, "email_linking", at=requested, email_normalized="guest@example.com")
    _verify(conn, request, requested + timedelta(minutes=1))
    subject = str(uuid.uuid4())
    identity = _identity(conn, user=guest["guest"], subject=subject, email="guest@example.com")
    request_expires = requested + timedelta(seconds=600)
    commit = request_expires + timedelta(seconds=1)  # après la fin de validité de la demande OTP
    _update(conn, "auth_operations", op, status="committed", committed_at=commit, auth_identity_id=identity,
            proven_provider="supabase", proven_issuer=ISS, proven_subject=subject, identity_resolution="new_identity")
    proof = dict(auth_identity_id=identity, otp_request_id=request, otp_verified_at=requested + timedelta(minutes=1))
    _rejects(conn, "ck_auth_operation_results_otp_recent", lambda: _result(
        conn, op, "email_linking", commit, otp_expires_at=request_expires, **proof))
    # La fin de validité recopiée doit être exactement celle de la demande.
    _rejects(conn, "auth_operation_results_otp_fkey", lambda: _result(
        conn, op, "email_linking", commit, otp_expires_at=commit, **proof))


# --------------------------------------------------------------------------
# 2e. OTP : empreinte attendue, comparaison locale, états, limites
# --------------------------------------------------------------------------

def test_pg_otp_expected_hmac_precedes_any_attempt_and_local_comparison(conn):
    op = _operation(conn, "supabase_email_sign_in", email_normalized="alice@example.com")
    pending = _otp_request(conn, op, "supabase_email_sign_in", expected=False)
    # Aucune tentative avant l'empreinte du code attendu.
    with pytest.raises(sa.exc.IntegrityError):
        with conn.begin_nested():
            _insert(conn, "auth_otp_verification_attempts", id=uuid.uuid4(), otp_request_id=pending, attempt_no=1,
                    code_generation=1, purpose="sign_in", request_expires_at=T0 + timedelta(seconds=600),
                    code_hmac_key_version="k1", expected_code_hmac=h("guess"), code_hmac=h("guess"),
                    local_match=True, state="in_flight", started_at=T0)
    _rejects(conn, "ck_auth_otp_requests_expected_code_required", lambda: _update(
        conn, "auth_otp_requests", pending, status="issued", provider_sent_at=T0))
    _update(conn, "auth_otp_requests", pending, status="superseded", closed_at=T0 + timedelta(seconds=30))
    request = _otp_request(conn, op, "supabase_email_sign_in", gen=2)
    # Code soumis différent : rejeté localement, sans appel externe.
    _attempt(conn, request, n=1, code_hmac=h("wrong"))
    _rejects(conn, "ck_auth_otp_verification_attempts_external_requires_match|"
                   "ck_auth_otp_verification_attempts_local_rejection",
             lambda: _attempt(conn, request, n=2, code_hmac=h("wrong-2"), state="in_flight", resolved_at=None))
    _rejects(conn, "ck_auth_otp_verification_attempts_local_match", lambda: _attempt(
        conn, request, n=2, code_hmac=h("wrong-3"), local_match=True, state="in_flight", resolved_at=None))
    _rejects(conn, "ck_auth_otp_verification_attempts_local_rejection", lambda: _attempt(
        conn, request, n=2, code_hmac=h("wrong-4"), provider_error_code="otp_expired"))
    # Autre clé, autre finalité ou autre génération que l'empreinte attendue : FK.
    for override in (dict(code_hmac_key_version="k2"), dict(purpose="email_linking"), dict(code_generation=1)):
        _rejects(conn, "auth_otp_verification_attempts_request_fkey", lambda: _attempt(conn, request, n=3, **override))
    _rejects(conn, "ck_auth_otp_verification_attempts_started_in_ttl", lambda: _attempt(
        conn, request, n=3, at=T0 + timedelta(seconds=601)))
    # Code correspondant : vérification externe autorisée.
    _attempt(conn, request, n=3)
    # L'empreinte attendue est écrite une seule fois (figée par les tentatives).
    _rejects(conn, "auth_otp_verification_attempts_request_fkey", lambda: _update(
        conn, "auth_otp_requests", request, expected_code_hmac=h("rotated")))


def test_pg_otp_uncertain_external_result_is_tracked(conn):
    op = _operation(conn, "supabase_email_sign_in", email_normalized="alice@example.com")
    request = _otp_request(conn, op, "supabase_email_sign_in")
    first = _attempt(conn, request, n=1, at=T0 + timedelta(seconds=30))
    _update(conn, "auth_otp_requests", request, status="verifying")
    _rejects(conn, "uq_auth_otp_verification_attempts_one_unresolved",
             lambda: _attempt(conn, request, n=2, at=T0 + timedelta(seconds=31)))
    _rejects(conn, "ck_auth_otp_verification_attempts_uncertainty", lambda: _update(
        conn, "auth_otp_verification_attempts", first, state="uncertain"))
    _update(conn, "auth_otp_verification_attempts", first, state="uncertain",
            uncertain_since=T0 + timedelta(seconds=40))
    _update(conn, "auth_otp_requests", request, status="verify_uncertain")
    _rejects(conn, "uq_auth_otp_verification_attempts_one_unresolved",
             lambda: _attempt(conn, request, n=2, at=T0 + timedelta(seconds=50)))
    # Rapprochement : l'incertitude reste tracée après résolution.
    _update(conn, "auth_otp_verification_attempts", first, state="accepted", resolved_at=T0 + timedelta(seconds=60))
    _update(conn, "auth_otp_requests", request, status="verified", verified_at=T0 + timedelta(seconds=60))
    assert conn.execute(sa.text(
        "SELECT state, uncertain_since IS NOT NULL FROM auth_otp_verification_attempts WHERE id = :a"),
        {"a": first}).one() == ("accepted", True)
    _rejects(conn, "uq_auth_otp_verification_attempts_one_accepted|uq_auth_otp_verification_attempts_one_unresolved",
             lambda: _attempt(conn, request, n=2, at=T0 + timedelta(seconds=70), state="accepted",
                              resolved_at=T0 + timedelta(seconds=70)))
    # Une tentative incertaine non résolue est abandonnée quand la demande expire.
    op2 = _operation(conn, "supabase_email_sign_in", email_normalized="bob@example.com")
    request2 = _otp_request(conn, op2, "supabase_email_sign_in")
    stuck = _attempt(conn, request2, n=1, state="uncertain", uncertain_since=T0 + timedelta(seconds=61))
    _update(conn, "auth_otp_verification_attempts", stuck, state="abandoned", resolved_at=T0 + timedelta(seconds=601))
    _update(conn, "auth_otp_requests", request2, status="expired", closed_at=T0 + timedelta(seconds=601))
    _rejects(conn, "ck_auth_otp_requests_verified", lambda: _update(
        conn, "auth_otp_requests", request2, status="verified", closed_at=None,
        verified_at=T0 + timedelta(seconds=601)))


def test_pg_otp_request_states_and_frozen_limits(conn):
    op = _operation(conn, "supabase_email_sign_in", email_normalized="alice@example.com")
    _rejects(conn, "ck_auth_otp_requests_ttl", lambda: _otp_request(
        conn, op, "supabase_email_sign_in", expires_at=T0 + timedelta(seconds=600, microseconds=1)))
    _rejects(conn, "ck_auth_otp_requests_failures", lambda: _otp_request(
        conn, op, "supabase_email_sign_in", failure_count=6))
    _rejects(conn, "ck_auth_otp_requests_exhausted", lambda: _otp_request(
        conn, op, "supabase_email_sign_in", failure_count=4, status="exhausted", closed_at=T0))
    _rejects(conn, "ck_auth_otp_requests_purpose_kind", lambda: _otp_request(
        conn, op, "supabase_email_sign_in", purpose="email_linking"))
    _rejects(conn, "ck_auth_otp_requests_issue", lambda: _otp_request(
        conn, op, "supabase_email_sign_in", expected=False, provider_sent_at=T0))
    _rejects(conn, "ck_auth_otp_requests_expected_code_pair", lambda: _otp_request(
        conn, op, "supabase_email_sign_in", expected_code_hmac_key_version=None))
    _rejects(conn, "ck_auth_otp_requests_expected_code_hmac_format", lambda: _otp_request(
        conn, op, "supabase_email_sign_in", expected_code_hmac="123456"))
    _rejects(conn, "ck_auth_otp_requests_closed", lambda: _otp_request(
        conn, op, "supabase_email_sign_in", status="send_failed", expected=False))
    live = _otp_request(conn, op, "supabase_email_sign_in", expected=False)
    _rejects(conn, "uq_auth_otp_requests_one_live", lambda: _otp_request(conn, op, "supabase_email_sign_in", gen=2))
    _update(conn, "auth_otp_requests", live, status="issue_uncertain")
    _update(conn, "auth_otp_requests", live, status="send_failed", closed_at=T0 + timedelta(seconds=5))
    exhausted = _otp_request(conn, op, "supabase_email_sign_in", gen=2)
    _update(conn, "auth_otp_requests", exhausted, failure_count=5, status="exhausted",
            closed_at=T0 + timedelta(minutes=2))
    invitation = _insert(conn, "invitations", id=uuid.uuid4(), token_hash=h("x"), hash_key_version="k1",
                         cohort="vague_0", status="pending", created_at=T0, expires_at=T0 + timedelta(days=7))
    inv_op = _operation(conn, "invitation_activation", invitation_id=invitation)
    _rejects(conn, "auth_otp_requests_operation_kind_fkey|ck_auth_otp_requests_operation_kind",
             lambda: _otp_request(conn, inv_op, "invitation_activation", purpose="sign_in",
                                  operation_kind="invitation_activation"))
    _rejects(conn, "auth_otp_requests_operation_kind_fkey", lambda: _otp_request(
        conn, inv_op, "supabase_email_sign_in", gen=9))


def test_pg_abuse_events_and_links(conn):
    subject = _insert(conn, "auth_abuse_subjects", id=uuid.uuid4(), scope_kind="email",
                      scope_hmac=h("alice@example.com"), hash_key_version="k1", created_at=T0)
    _rejects(conn, "uq_auth_abuse_subjects_scope", lambda: _insert(
        conn, "auth_abuse_subjects", id=uuid.uuid4(), scope_kind="email", scope_hmac=h("alice@example.com"),
        hash_key_version="k1", created_at=T0))
    _insert(conn, "auth_abuse_subjects", id=uuid.uuid4(), scope_kind="email", scope_hmac=h("alice@example.com"),
            hash_key_version="k2", created_at=T0)
    _rejects(conn, "ck_auth_abuse_subjects_scope_hmac_format", lambda: _insert(
        conn, "auth_abuse_subjects", id=uuid.uuid4(), scope_kind="ip", scope_hmac="192.0.2.1",
        hash_key_version="k1", created_at=T0))
    op = _operation(conn, "supabase_email_sign_in", email_normalized="alice@example.com")
    request = _otp_request(conn, op, "supabase_email_sign_in")
    for minute in (0, 10, 14):
        _insert(conn, "auth_abuse_events", id=uuid.uuid4(), subject_id=subject, event_kind="otp_failure",
                occurred_at=T0 + timedelta(minutes=minute), otp_request_id=request, policy_version="d9-v1")
    window = conn.execute(sa.text(
        "SELECT count(*) FROM auth_abuse_events WHERE subject_id = :s AND event_kind = 'otp_failure' "
        "AND occurred_at > :now - interval '15 minutes'"), {"s": subject, "now": T0 + timedelta(minutes=20)})
    assert window.scalar_one() == 2  # fenêtre glissante
    _rejects(conn, "ck_auth_abuse_events_otp_link", lambda: _insert(
        conn, "auth_abuse_events", id=uuid.uuid4(), subject_id=subject, event_kind="otp_request", occurred_at=T0,
        policy_version="d9-v1"))
    _rejects(conn, "ck_auth_abuse_events_resume_link", lambda: _insert(
        conn, "auth_abuse_events", id=uuid.uuid4(), subject_id=subject, event_kind="resume_attempt", occurred_at=T0,
        policy_version="d9-v1"))


def test_pg_legacy_backend_still_works_at_head(conn):
    _guest_flow(conn)
    conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('legacy-at-head', 'debutant')"))
    conn.execute(sa.text("UPDATE users SET level = 'avance' WHERE id = 'legacy-at-head'"))
    position = conn.execute(sa.text(
        "INSERT INTO portfolio_positions (user_id, ticker, quantity) VALUES ('legacy-at-head', 'OR.PA', 1) "
        "RETURNING id")).scalar_one()
    conn.execute(sa.text("UPDATE portfolio_positions SET quantity = 2 WHERE id = :p"), {"p": position})
    conn.execute(sa.text("DELETE FROM portfolio_positions WHERE id = :p"), {"p": position})
    assert conn.execute(sa.text("SELECT level, level_revision FROM users WHERE id = 'legacy-at-head'")).one() == (
        "avance", 0)
    # Aucune trace des anciens profils dans les nouvelles structures (pas d'import).
    assert conn.execute(sa.text(
        "SELECT count(*) FROM auth_identities WHERE user_id IN ('legacy-anon-1', 'legacy-anon-2', 'user_7f3a')"
    )).scalar_one() == 0


# --------------------------------------------------------------------------
# 2f. Transactions concurrentes
# --------------------------------------------------------------------------

@pytest.fixture
def committed_request(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    with pg_engine.begin() as c:
        op = _operation(c, "supabase_email_sign_in", email_normalized="alice@example.com")
        request = _otp_request(c, op, "supabase_email_sign_in")
        subject = _insert(c, "auth_abuse_subjects", id=uuid.uuid4(), scope_kind="email",
                          scope_hmac=h("alice@example.com"), hash_key_version="k1", created_at=T0)
    return pg_engine, request, subject


def test_pg_concurrent_otp_validations_are_serialized(committed_request):
    engine, request, _ = committed_request
    outcome = {}
    first = engine.connect()
    first_trans = first.begin()
    _attempt(first, request, n=1, at=T0 + timedelta(seconds=10))

    def second():
        try:
            with engine.begin() as other:
                other.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
                _attempt(other, request, n=2, at=T0 + timedelta(seconds=11))
            outcome["result"] = "inserted"
        except sa.exc.IntegrityError as exc:
            outcome["result"] = str(exc.orig)

    thread = threading.Thread(target=second)
    thread.start()
    try:
        _wait_for_lock_waiter(engine)
        first_trans.commit()
    finally:
        first.close()
        thread.join(30)
    assert "uq_auth_otp_verification_attempts_one_unresolved" in outcome["result"]
    with engine.connect() as c:
        assert c.execute(sa.text("SELECT attempt_no, state FROM auth_otp_verification_attempts")).all() == [
            (1, "in_flight")]


def test_pg_abuse_subject_lock_serializes_concurrent_emissions(committed_request):
    engine, request, subject = committed_request
    order = []
    first = engine.connect()
    first_trans = first.begin()
    first.execute(sa.text("SELECT id FROM auth_abuse_subjects WHERE id = :s FOR UPDATE"), {"s": subject})

    def second():
        with engine.begin() as other:
            other.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
            other.execute(sa.text("SELECT id FROM auth_abuse_subjects WHERE id = :s FOR UPDATE"), {"s": subject})
            recent = other.execute(sa.text(
                "SELECT count(*) FROM auth_abuse_events WHERE subject_id = :s AND event_kind = 'otp_request'"),
                {"s": subject}).scalar_one()
            order.append(("second-sees", recent))

    thread = threading.Thread(target=second)
    thread.start()
    try:
        _wait_for_lock_waiter(engine)
        _insert(first, "auth_abuse_events", id=uuid.uuid4(), subject_id=subject, event_kind="otp_request",
                occurred_at=T0, otp_request_id=request, policy_version="d9-v1")
        order.append("first-commits")
        first_trans.commit()
    finally:
        first.close()
        thread.join(30)
    assert order == ["first-commits", ("second-sees", 1)]
