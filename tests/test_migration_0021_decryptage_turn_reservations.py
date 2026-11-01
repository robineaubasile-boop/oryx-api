"""Tests de R1 PR 9-B1 : migration 0021_decryptage_reservations (fichier
0021_decryptage_turn_reservations.py) et modèle correspondant.

1. Sans base : maillon 0020 -> 0021 (tête), identifiant <= 32 caractères,
   fichiers 0001 à 0020 inchangés, SQL offline (upgrade additif sans
   trigger, fonction, backfill ni UPDATE ; downgrade : délais, verrou, garde
   DO évaluée à l'exécution, retrait), modèle aligné sur la migration.
2. PostgreSQL réel (base jetable) : montée 0020 -> 0021 sur données
   historiques (aucune ligne modifiée, aucun backfill, PK historique
   conservée), catalogue exact, compare_metadata vide, CHECK réellement
   exercés, FK composite (propriétaire de la conversation), unicité de
   l'identité du tour (jamais de l'empreinte), unicité du lien de
   livraison, aucune réattribution de conversation, downgrade exact,
   downgrade REFUSÉ (en ligne et par la garde offline) si une réservation
   existe.
"""
import hashlib
import importlib.util
import uuid

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
    PR7_0020,
    PR7_0020_FILE,
    PR9B1_0021,
    PR9B1_0021_FILE,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import _catalog, _global_catalog
from tests.test_migration_0015_users_level_revision import _seed_historical
from tests.test_migration_0016_portfolio_idempotency import _rejects
from tests.test_migration_0018_f2_abuse_journal import PR1_SHA256
from tests.test_migration_0019_f2_http_admission import PR2_SHA256
from tests.test_migration_0020_position_revision import PR4_SHA256

MIGRATION = REPO_ROOT / "alembic" / "versions" / PR9B1_0021_FILE
# sha256 de 0020 telle que fusionnée sur main (3767fe8) : R1 PR 9-B1 ne la modifie pas.
PR7_SHA256 = {PR7_0020_FILE: "38fdbbea27a547aeb5aba76643b9e7cf5f8dfbb0e2dd0ac598fd4d4cfb437a2e"}
OFFLINE = "postgresql://offline@localhost/offline"
GUARDS = ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '15s'",
          "SET LOCAL idle_in_transaction_session_timeout = '15s'"]
TABLE = "decryptage_turn_reservations"
COLUMNS = ["id", "user_id", "conversation_key", "surface", "source_user_turn_id", "delivery_ordinal",
           "fingerprint_version", "fingerprint_key_version", "request_fingerprint", "resolved_ticker", "state",
           "blocked_reason", "lease_epoch", "lease_token_hash", "lease_expires_at", "attempts_used",
           "retry_not_before_at", "assistant_delivery_id", "created_at", "updated_at", "finalized_at"]
FP = "a" * 64


def _m0021():
    spec = importlib.util.spec_from_file_location("m0021", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_links_0021_to_0020_and_is_the_head():
    script = _script_directory()
    assert script.get_heads() == [PR9B1_0021]
    assert script.get_revision(PR9B1_0021).down_revision == PR7_0020
    # Identifiant raccourci (alembic_version.version_num VARCHAR(32)), fichier au nom autorisé.
    assert PR9B1_0021 == "0021_decryptage_reservations" and len(PR9B1_0021) <= 32
    assert len("0021_decryptage_turn_reservations") == 33
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(files) == 21 and files[-1] == PR9B1_0021_FILE == "0021_decryptage_turn_reservations.py"


def test_migrations_0001_to_0020_are_unchanged():
    for name, digest in {**PR1_SHA256, **PR2_SHA256, **PR4_SHA256, **PR7_SHA256}.items():
        path = REPO_ROOT / "alembic" / "versions" / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
    for line in (REPO_ROOT / "ci" / "migration_history.sha256").read_text().splitlines():  # 0001 à 0014
        digest, _, relative = line.partition("  ")
        assert hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest() == digest, relative


def test_offline_upgrade_is_additive_without_backfill():
    statements = _statements(_run_alembic(OFFLINE, "upgrade", f"{PR7_0020}:{PR9B1_0021}", "--sql").stdout)
    assert statements[:3] == GUARDS
    assert statements[3] == ("ALTER TABLE conversation_identities ADD CONSTRAINT uq_conversation_identities_key_owner "
                             "UNIQUE (conversation_key, user_id)")
    assert statements[4].startswith(f"CREATE TABLE {TABLE} (")
    assert statements[5:] == [
        f"UPDATE alembic_version SET version_num='{PR9B1_0021}' WHERE alembic_version.version_num = '{PR7_0020}'"]
    create = statements[4]
    for name, expression in _m0021().CHECKS:
        assert f"CONSTRAINT {name} CHECK ({expression})" in create, name
    for fragment in ("CONSTRAINT decryptage_turn_reservations_pkey PRIMARY KEY (id)",
                     "CONSTRAINT decryptage_turn_reservations_user_id_fkey FOREIGN KEY(user_id) REFERENCES users (id)",
                     "CONSTRAINT decryptage_turn_reservations_conversation_fkey FOREIGN KEY(conversation_key, user_id) "
                     "REFERENCES conversation_identities (conversation_key, user_id)",
                     "CONSTRAINT decryptage_turn_reservations_assistant_delivery_id_fkey FOREIGN KEY"
                     "(assistant_delivery_id) REFERENCES assistant_deliveries (id)",
                     "CONSTRAINT uq_decryptage_turn_reservations_turn UNIQUE (user_id, conversation_key, surface, "
                     "source_user_turn_id, delivery_ordinal)",
                     "CONSTRAINT uq_decryptage_turn_reservations_assistant_delivery UNIQUE (assistant_delivery_id)"):
        assert fragment in create, fragment
    upper = " ".join(statements).upper()
    for forbidden in ("TRIGGER", "FUNCTION", "CREATE TYPE", "CREATE INDEX", "DROP", "DEFERRABLE", "INSERT INTO",
                      "UPDATE CONVERSATION", "UPDATE ASSISTANT", "DEFAULT", "QUESTION", "PROMPT", "RESPONSE"):
        assert forbidden not in upper, forbidden


def test_offline_downgrade_locks_then_guards_at_execution_then_drops():
    statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{PR9B1_0021}:{PR7_0020}", "--sql").stdout)
    assert statements == GUARDS + [
        f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE",
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {TABLE}) THEN RAISE EXCEPTION 'R1 PR 9-B1 : downgrade de "
        "0021_decryptage_reservations refusé, des réservations Décrypter existent. Aucune donnée supprimée.'; "
        "END IF; END $$",
        f"DROP TABLE {TABLE}",
        "ALTER TABLE conversation_identities DROP CONSTRAINT uq_conversation_identities_key_owner",
        f"UPDATE alembic_version SET version_num='{PR7_0020}' WHERE alembic_version.version_num = '{PR9B1_0021}'",
    ]


def test_model_matches_the_migration():
    m = _m0021()
    table = Base.metadata.tables[TABLE]
    assert list(table.c.keys()) == COLUMNS
    nullable = {"resolved_ticker", "blocked_reason", "lease_token_hash", "lease_expires_at", "retry_not_before_at",
                "assistant_delivery_id", "finalized_at"}
    for column in table.c:
        assert column.nullable == (column.name in nullable), column.name
        assert column.server_default is None and column.default is None, column.name
    checks = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert checks == dict(m.CHECKS)
    uniques = {c.name: [col.name for col in c.columns] for c in table.constraints
               if isinstance(c, sa.UniqueConstraint)}
    assert uniques == {m.TURN_UNIQUE: ["user_id", "conversation_key", "surface", "source_user_turn_id",
                                       "delivery_ordinal"], m.DELIVERY_UNIQUE: ["assistant_delivery_id"]}
    fks = {fk.name: ([e.parent.name for e in fk.elements], [e.target_fullname for e in fk.elements])
           for fk in table.foreign_key_constraints}
    assert fks == {m.USER_FK: (["user_id"], ["users.id"]),
                   m.CONVERSATION_FK: (["conversation_key", "user_id"], ["conversation_identities.conversation_key",
                                                                         "conversation_identities.user_id"]),
                   m.DELIVERY_FK: (["assistant_delivery_id"], ["assistant_deliveries.id"])}
    conversations = Base.metadata.tables["conversation_identities"]
    assert {c.name: [col.name for col in c.columns] for c in conversations.constraints
            if isinstance(c, sa.UniqueConstraint)} == {m.OWNER_UNIQUE: ["conversation_key", "user_id"]}
    assert [c.name for c in conversations.primary_key.columns] == ["conversation_key"]
    for forbidden in ("question", "context", "prompt", "response", "answer", "token"):
        assert not any(forbidden == name or name.startswith(forbidden + "_") for name in COLUMNS), forbidden


# --------------------------------------------------------------------------
# 2. PostgreSQL réel
# --------------------------------------------------------------------------

def _seed_conversation(conn, key="conv-1", user_id="legacy-anon-1"):
    conn.execute(sa.text("INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
                         "VALUES (:k, :u, now()) ON CONFLICT DO NOTHING"), {"k": key, "u": user_id})


def _insert(conn, **overrides):
    values = {"id": uuid.uuid4(), "user_id": "legacy-anon-1", "conversation_key": "conv-1", "surface": "decryptage",
              "source_user_turn_id": uuid.uuid4(), "delivery_ordinal": 1,
              "fingerprint_version": "oryx-decryptage-reservation-v1", "fingerprint_key_version": "d1",
              "request_fingerprint": FP, "state": "reserved", "blocked_reason": None, "lease_epoch": 1,
              "lease_token_hash": "b" * 64, "lease_expires_at": sa.text("now() + interval '360 seconds'"),
              "attempts_used": 1, "retry_not_before_at": None, "assistant_delivery_id": None,
              "created_at": sa.text("now()"), "updated_at": sa.text("now()"), "finalized_at": None}
    values.update(overrides)
    columns, params, sql_values = [], {}, []
    for key, val in values.items():
        columns.append(key)
        if isinstance(val, sa.sql.elements.TextClause):
            sql_values.append(val.text)
        else:
            sql_values.append(f":{key}")
            params[key] = val
    conn.execute(sa.text(f"INSERT INTO {TABLE} ({', '.join(columns)}) VALUES ({', '.join(sql_values)})"), params)
    return values


def _delivery(conn, key="conv-1", user_id="legacy-anon-1", turn=None):
    delivery_id = uuid.uuid4()
    conn.execute(sa.text(
        "INSERT INTO assistant_deliveries (id, user_id, conversation_key, surface, source_user_turn_id, "
        "delivery_ordinal, request_fingerprint, visible_content_fingerprint, response_payload, private_metadata, "
        "status, delivery_schema_version, generated_at) VALUES (:id, :u, :k, 'decryptage', :t, 1, :f, :f, "
        "'{}'::jsonb, '{}'::jsonb, 'pending', 'assistant-delivery-v1', now())"),
        {"id": delivery_id, "u": user_id, "k": key, "t": turn or uuid.uuid4(), "f": "c" * 64})
    return delivery_id


def test_pg_upgrade_0020_to_0021_keeps_every_row_then_exact_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR7_0020)
    _seed_historical(pg_engine)
    with pg_engine.begin() as conn:
        _seed_conversation(conn, "conv-hist", "legacy-anon-1")
        _delivery(conn, "conv-hist")
    tables = _tables(pg_engine)
    schema_0020, catalog_0020 = _snapshot(pg_engine, tables), _catalog(pg_engine, tables)
    global_0020 = _global_catalog(pg_engine)
    data_0020 = _data(pg_engine, tables - {"alembic_version"})
    assert _compare_metadata(pg_engine) == []  # écarts attendus (table, UNIQUE composite) retirés exactement

    _run_alembic(pg_url, "upgrade", PR9B1_0021)
    assert _version(pg_engine) == PR9B1_0021 and _compare_metadata(pg_engine) == []
    assert _tables(pg_engine) == tables | {TABLE}
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0020  # aucun backfill, aucune réparation
    assert _data(pg_engine, {TABLE}) == {TABLE: []}
    others = tables - {"alembic_version", "conversation_identities"}
    assert _snapshot(pg_engine, others) == {t: schema_0020[t] for t in sorted(others)}
    assert _catalog(pg_engine, others) == {t: catalog_0020[t] for t in sorted(others)}
    assert _global_catalog(pg_engine) == global_0020  # aucun trigger, fonction ni type
    with pg_engine.connect() as conn:
        rows = {r[0]: r[1:] for r in conn.execute(sa.text(
            "SELECT conname, contype, pg_get_constraintdef(oid), condeferrable FROM pg_constraint "
            "WHERE conrelid = 'conversation_identities'::regclass ORDER BY conname"))}
        assert rows["conversation_identities_pkey"] == ("p", "PRIMARY KEY (conversation_key)", False)
        assert rows["uq_conversation_identities_key_owner"] == ("u", "UNIQUE (conversation_key, user_id)", False)
        defs = {r[0]: r[1] for r in conn.execute(sa.text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)"),
            {"t": TABLE})}
        assert defs["decryptage_turn_reservations_conversation_fkey"] == (
            "FOREIGN KEY (conversation_key, user_id) REFERENCES conversation_identities(conversation_key, user_id)")
        assert defs["uq_decryptage_turn_reservations_turn"] == (
            "UNIQUE (user_id, conversation_key, surface, source_user_turn_id, delivery_ordinal)")
        assert defs["uq_decryptage_turn_reservations_assistant_delivery"] == "UNIQUE (assistant_delivery_id)"
        assert len([n for n in defs if n.startswith("ck_")]) == len(_m0021().CHECKS)
        types = dict(conn.execute(sa.text(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = :t"), {"t": TABLE}).all())
        assert types["user_id"] == types["conversation_key"] == types["request_fingerprint"] == "text"
        assert types["lease_epoch"] == "bigint" and types["attempts_used"] == "integer"
        assert types["lease_expires_at"] == "timestamp with time zone" and types["id"] == "uuid"

    _run_alembic(pg_url, "downgrade", PR7_0020)
    assert _version(pg_engine) == PR7_0020
    assert _snapshot(pg_engine, tables) == schema_0020 and _catalog(pg_engine, tables) == catalog_0020
    assert _global_catalog(pg_engine) == global_0020
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0020
    _run_alembic(pg_url, "upgrade", "head")
    assert _compare_metadata(pg_engine) == []


CHECK_VIOLATIONS = [
    ("ck_decryptage_turn_reservations_surface", {"surface": "coach"}),
    ("ck_decryptage_turn_reservations_delivery_ordinal", {"delivery_ordinal": 2}),
    ("ck_decryptage_turn_reservations_fingerprint_version", {"fingerprint_version": "oryx-decryptage-reservation-v2"}),
    ("ck_decryptage_turn_reservations_fingerprint_key_version", {"fingerprint_key_version": "D1"}),
    ("ck_decryptage_turn_reservations_request_fingerprint", {"request_fingerprint": "A" * 64}),
    ("ck_decryptage_turn_reservations_request_fingerprint", {"request_fingerprint": "a" * 63}),
    ("ck_decryptage_turn_reservations_lease_token_hash", {"lease_token_hash": "secret-en-clair"}),
    ("ck_decryptage_turn_reservations_state", {"state": "leased"}),
    ("ck_decryptage_turn_reservations_blocked_reason", {"blocked_reason": "manual"}),
    ("ck_decryptage_turn_reservations_counters", {"lease_epoch": -1}),
    ("ck_decryptage_turn_reservations_counters", {"attempts_used": -1}),
    # reserved : jeton XOR backoff, jamais de livraison, de finalisation ni de motif.
    ("ck_decryptage_turn_reservations_reserved", {"retry_not_before_at": sa.text("now()")}),
    ("ck_decryptage_turn_reservations_reserved", {"lease_token_hash": None, "lease_expires_at": None}),
    ("ck_decryptage_turn_reservations_reserved", {"lease_expires_at": None}),
    ("ck_decryptage_turn_reservations_reserved", {"lease_token_hash": None}),
    ("ck_decryptage_turn_reservations_reserved", {"finalized_at": sa.text("now()")}),
    ("ck_decryptage_turn_reservations_reserved", {"blocked_reason": "legacy_collision"}),
    # blocked : motif obligatoire, aucun bail, backoff, livraison ni finalisation.
    ("ck_decryptage_turn_reservations_blocked", {"state": "blocked", "lease_token_hash": None,
                                                 "lease_expires_at": None}),
    ("ck_decryptage_turn_reservations_blocked", {"state": "blocked", "blocked_reason": "legacy_collision"}),
    ("ck_decryptage_turn_reservations_blocked", {"state": "blocked", "blocked_reason": "attempts_exhausted",
                                                 "lease_token_hash": None, "lease_expires_at": None,
                                                 "retry_not_before_at": sa.text("now()")}),
    # finalized : livraison et finalisation présentes, aucun bail ni motif.
    ("ck_decryptage_turn_reservations_finalized", {"state": "finalized", "lease_token_hash": None,
                                                   "lease_expires_at": None, "finalized_at": sa.text("now()")}),
    ("ck_decryptage_turn_reservations_timestamps", {"updated_at": sa.text("now() - interval '1 second'")}),
]


@pytest.mark.parametrize("constraint,overrides", CHECK_VIOLATIONS)
def test_pg_every_check_is_exercised(pg_url, pg_engine, constraint, overrides):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    with pg_engine.begin() as conn:
        assert_disposable_connection(conn)
        _seed_conversation(conn)
        _rejects(conn, constraint, lambda: _insert(conn, **overrides))


def test_pg_valid_shapes_identity_and_foreign_keys(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    with pg_engine.begin() as conn:
        assert_disposable_connection(conn)
        _seed_conversation(conn)
        _seed_conversation(conn, "conv-other", "user_7f3a")
        leased = _insert(conn)
        _insert(conn, lease_token_hash=None, lease_expires_at=None, retry_not_before_at=sa.text("now()"))
        _insert(conn, state="blocked", blocked_reason="legacy_collision", lease_epoch=0, attempts_used=0,
                lease_token_hash=None, lease_expires_at=None)
        delivery = _delivery(conn)
        _insert(conn, state="finalized", lease_token_hash=None, lease_expires_at=None,
                assistant_delivery_id=delivery, finalized_at=sa.text("now()"))
        # Même empreinte, autre tour : admis (l'empreinte n'est jamais une clé).
        _insert(conn, request_fingerprint=FP)
        # Même identité de tour : refusée.
        _rejects(conn, "uq_decryptage_turn_reservations_turn",
                 lambda: _insert(conn, source_user_turn_id=leased["source_user_turn_id"]))
        # Une livraison ne peut être liée qu'à une réservation.
        _rejects(conn, "uq_decryptage_turn_reservations_assistant_delivery",
                 lambda: _insert(conn, state="finalized", lease_token_hash=None, lease_expires_at=None,
                                 assistant_delivery_id=delivery, finalized_at=sa.text("now()")))
        _rejects(conn, "decryptage_turn_reservations_assistant_delivery_id_fkey",
                 lambda: _insert(conn, state="finalized", lease_token_hash=None, lease_expires_at=None,
                                 assistant_delivery_id=uuid.uuid4(), finalized_at=sa.text("now()")))
        # FK composite : conversation d'un autre propriétaire ou inexistante.
        _rejects(conn, "decryptage_turn_reservations_conversation_fkey",
                 lambda: _insert(conn, conversation_key="conv-other"))
        _rejects(conn, "decryptage_turn_reservations_conversation_fkey",
                 lambda: _insert(conn, conversation_key="conv-absent"))
        _rejects(conn, "decryptage_turn_reservations_user_id_fkey|decryptage_turn_reservations_conversation_fkey",
                 lambda: _insert(conn, user_id="ghost"))
        # La clé de conversation reste unique à elle seule : aucune réattribution.
        _rejects(conn, "conversation_identities_pkey|uq_conversation_identities_key_owner",
                 "INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
                 "VALUES ('conv-1', 'user_7f3a', now())")
        _rejects(conn, "decryptage_turn_reservations_conversation_fkey",
                 "UPDATE conversation_identities SET user_id = 'user_7f3a' WHERE conversation_key = 'conv-1'")


@pytest.mark.parametrize("offline", [False, True])
def test_pg_downgrade_is_refused_when_a_reservation_exists(pg_url, pg_engine, offline):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    with pg_engine.begin() as conn:
        _seed_conversation(conn)
        _insert(conn, state="blocked", blocked_reason="attempts_exhausted", lease_epoch=3, attempts_used=3,
                lease_token_hash=None, lease_expires_at=None)
    tables = _tables(pg_engine)
    before = (_snapshot(pg_engine, tables), _data(pg_engine, tables - {"alembic_version"}))
    if offline:
        guard = next(s for s in _statements(_run_alembic(OFFLINE, "downgrade", f"{PR9B1_0021}:{PR7_0020}",
                                                         "--sql").stdout) if s.startswith("DO $$"))
        with pg_engine.connect() as conn:
            with pytest.raises(sa.exc.DBAPIError, match="refusé"):
                conn.exec_driver_sql(guard)
    else:
        with pytest.raises(Exception) as failure:
            _run_alembic(pg_url, "downgrade", PR7_0020)
        assert "downgrade de 0021_decryptage_reservations refusé" in str(getattr(failure.value, "stderr",
                                                                                 failure.value))
    assert _version(pg_engine) == PR9B1_0021
    assert (_snapshot(pg_engine, tables), _data(pg_engine, tables - {"alembic_version"})) == before


def test_pg_offline_guard_passes_without_reservation(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    _seed_historical(pg_engine)
    guard = next(s for s in _statements(_run_alembic(OFFLINE, "downgrade", f"{PR9B1_0021}:{PR7_0020}",
                                                     "--sql").stdout) if s.startswith("DO $$"))
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(guard)  # aucune exception
