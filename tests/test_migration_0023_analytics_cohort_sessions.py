"""Tests de V0-T08 Lot 2B-1 : migration 0023_analytics_cohort_sessions
(cohorte, places et sessions d'activité DORMANTES) et modèles
correspondants.

1. Sans base : maillon 0022 -> 0023 (tête unique), identifiant <= 32
   caractères, fichiers 0001 à 0022 inchangés, SQL offline (upgrade : quatre
   CREATE TABLE et deux index, rien d'autre ; downgrade : délais, verrou
   déterministe, garde DO évaluée à l'exécution, retrait sans CASCADE),
   modèles alignés sur la migration (colonnes, types, nullabilité, CHECK,
   UNIQUE, FK, index), aucun champ personnel (ni e-mail, ni nom, ni token).
2. PostgreSQL réel (base jetable) : montée 0022 -> 0023 sur données
   historiques et analytiques 0022 conservées à l'identique, aucune ligne
   créée, aucune table existante modifiée (F2 comprise), catalogue exact,
   aucun trigger / fonction / ENUM / extension ; chaque CHECK, FK et UNIQUE
   réellement exercé ; downgrade exact vers 0022 si les quatre tables sont
   vides ; downgrade REFUSÉ, en ligne et par la garde offline exécutée, si
   l'une d'elles contient une ligne.
"""
import hashlib
import importlib.util
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from core.db import Base
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
    V0T08_0022,
    V0T08_0022_FILE,
    V0T08_0023,
    V0T08_0023_FILE,
    V0T08_0023_INDEXES,
    V0T08_0023_TABLES,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import _catalog, _global_catalog
from tests.test_migration_0016_portfolio_idempotency import _rejects
from tests.test_migration_0022_product_analytics_v1 import BASE_TIME, FORBIDDEN_FIELD_MARKERS, GUARDS, OFFLINE
from tests.test_migration_0022_product_analytics_v1 import _seed_0021_history
from tests.test_product_analytics_dormancy import MIGRATION_0022_SHA256

MIGRATION = REPO_ROOT / "alembic" / "versions" / V0T08_0023_FILE
COHORTS, SEATS, SESSIONS, INTERACTIONS = V0T08_0023_TABLES
NEW = set(V0T08_0023_TABLES)
TS = "timestamp with time zone"
COLUMNS = {
    COHORTS: [("campaign_id", "uuid", False), ("cohort_policy_version", "character varying(64)", False),
              ("seat_count", "smallint", False), ("admission_closes_at", TS, False), ("created_at", TS, False)],
    SEATS: [("id", "uuid", False), ("campaign_id", "uuid", False), ("seat_number", "smallint", False),
            ("invitation_id", "uuid", False), ("created_at", TS, False), ("user_id", "character varying", True),
            ("binding_operation_id", "uuid", True), ("binding_operation_kind", "character varying(32)", True),
            ("binding_operation_status", "character varying(16)", True), ("binding_committed_at", TS, True),
            ("bound_at", TS, True), ("authorization_recorded_at", TS, True),
            ("notice_version", "character varying(64)", True), ("admission_failure_first_at", TS, True)],
    SESSIONS: [("id", "uuid", False), ("campaign_id", "uuid", False), ("user_id", "character varying", False),
               ("environment_digest", "character varying(64)", False), ("opened_at", TS, False),
               ("last_activity_at", TS, False), ("idle_expires_at", TS, False), ("finalized_at", TS, True),
               ("end_reason", "character varying(16)", True)],
    INTERACTIONS: [("campaign_id", "uuid", False), ("user_id", "character varying", False),
                   ("interaction_key", "uuid", False), ("interaction_kind", "character varying(16)", False),
                   ("environment_digest", "character varying(64)", False), ("session_id", "uuid", True),
                   ("recorded_at", TS, False)],
}


def _m0023():
    spec = importlib.util.spec_from_file_location("m0023", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_links_0023_to_0022_and_is_the_single_head():
    script = _script_directory()
    assert script.get_heads() == [V0T08_0023]
    revision = script.get_revision(V0T08_0023)
    assert revision.down_revision == V0T08_0022
    assert not revision.branch_labels and not revision.dependencies
    assert V0T08_0023 == "0023_analytics_cohort_sessions" and len(V0T08_0023) <= 32
    assert V0T08_0023_FILE == f"{V0T08_0023}.py"
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(files) == 23 and files[-1] == V0T08_0023_FILE and files[-2] == V0T08_0022_FILE
    m = _m0023()
    assert (m.revision, m.down_revision, m.branch_labels, m.depends_on) == (V0T08_0023, V0T08_0022, None, None)


def test_migration_0022_is_unchanged():
    """0022 n'est jamais modifiée rétroactivement (sha256 du main de
    référence) ; 0001 à 0021 sont couvertes par la suite 0022."""
    path = REPO_ROOT / "alembic" / "versions" / V0T08_0022_FILE
    assert hashlib.sha256(path.read_bytes()).hexdigest() == MIGRATION_0022_SHA256


def test_offline_upgrade_creates_only_the_four_tables_and_their_indexes():
    statements = _statements(_run_alembic(OFFLINE, "upgrade", f"{V0T08_0022}:{V0T08_0023}", "--sql").stdout)
    assert statements[:3] == GUARDS
    creates = [s for s in statements if s.startswith("CREATE TABLE")]
    assert [s.split(" ")[2] for s in creates] == [COHORTS, SEATS, SESSIONS, INTERACTIONS]
    indexes = [s for s in statements if s.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX"))]
    assert sorted(s.split(" INDEX ")[1].split(" ")[0] for s in indexes) == sorted(V0T08_0023_INDEXES)
    assert statements[-1] == (f"UPDATE alembic_version SET version_num='{V0T08_0023}' "
                              f"WHERE alembic_version.version_num = '{V0T08_0022}'")
    assert len(statements) == 3 + len(creates) + len(indexes) + 1
    m = _m0023()
    by_table = dict(zip((COHORTS, SEATS, SESSIONS, INTERACTIONS), creates))
    for table, checks in ((COHORTS, m.COHORT_CHECKS), (SEATS, m.SEAT_CHECKS), (SESSIONS, m.SESSION_CHECKS),
                          (INTERACTIONS, m.INTERACTION_CHECKS)):
        for name, expression in checks:
            assert f"CONSTRAINT {name} CHECK ({expression})" in by_table[table], name
    for table, fragment in (
            (COHORTS, "CONSTRAINT product_campaign_cohorts_campaign_id_fkey FOREIGN KEY(campaign_id) "
                      "REFERENCES product_campaigns (id)"),
            (SEATS, "CONSTRAINT product_cohort_seats_invitation_id_fkey FOREIGN KEY(invitation_id) "
                    "REFERENCES invitations (id)"),
            (SEATS, "CONSTRAINT product_cohort_seats_binding_commit_fkey FOREIGN KEY(binding_operation_id, "
                    "binding_operation_kind, binding_operation_status, binding_committed_at) REFERENCES "
                    "auth_operations (id, kind, status, committed_at)"),
            (SESSIONS, "CONSTRAINT product_activity_sessions_participant_fkey FOREIGN KEY(campaign_id, user_id) "
                       "REFERENCES product_campaign_participants (campaign_id, user_id)"),
            (INTERACTIONS, "CONSTRAINT product_activity_session_interactions_pkey PRIMARY KEY (campaign_id, "
                           "user_id, interaction_key)"),
            (INTERACTIONS, "CONSTRAINT product_activity_session_interactions_session_fkey FOREIGN KEY(session_id, "
                           "campaign_id, user_id, environment_digest) REFERENCES product_activity_sessions (id, "
                           "campaign_id, user_id, environment_digest)")):
        assert fragment in by_table[table], fragment
    upper = " ".join(statements).upper()
    for forbidden in ("TRIGGER", "FUNCTION", "CREATE TYPE", "ENUM", "EXTENSION", "ALTER TABLE", "DROP", "CASCADE",
                      "DEFERRABLE", "INSERT INTO", "DELETE FROM", "DEFAULT", "SEQUENCE", "SERIAL", "ON DELETE",
                      "ON UPDATE", "COMMENT ON", "ORYX_SESSIONS"):
        assert forbidden not in upper, forbidden
    assert [s for s in statements if s.upper().startswith("UPDATE")] == [statements[-1]]


def test_offline_downgrade_locks_then_guards_each_table_at_execution_then_drops_without_cascade():
    statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{V0T08_0023}:{V0T08_0022}", "--sql").stdout)
    message = ("V0-T08 Lot 2B-1 : downgrade de 0023_analytics_cohort_sessions refusé, la table {} contient des "
               "données. Aucune donnée supprimée.")
    assert statements == GUARDS + [
        f"LOCK TABLE {COHORTS}, {SEATS}, {SESSIONS}, {INTERACTIONS} IN ACCESS EXCLUSIVE MODE",
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {INTERACTIONS}) THEN RAISE EXCEPTION "
        f"'{message.format(INTERACTIONS)}'; "
        f"ELSIF EXISTS (SELECT 1 FROM {SESSIONS}) THEN RAISE EXCEPTION '{message.format(SESSIONS)}'; "
        f"ELSIF EXISTS (SELECT 1 FROM {SEATS}) THEN RAISE EXCEPTION '{message.format(SEATS)}'; "
        f"ELSIF EXISTS (SELECT 1 FROM {COHORTS}) THEN RAISE EXCEPTION '{message.format(COHORTS)}'; "
        "END IF; END $$",
        f"DROP TABLE {INTERACTIONS}",
        f"DROP TABLE {SESSIONS}",
        f"DROP TABLE {SEATS}",
        f"DROP TABLE {COHORTS}",
        f"UPDATE alembic_version SET version_num='{V0T08_0022}' WHERE alembic_version.version_num = '{V0T08_0023}'",
    ]


def test_models_match_the_migration():
    m = _m0023()
    pg_types = {"UUID": "uuid", "SMALLINT": "smallint", "VARCHAR": "character varying", "TIMESTAMP WITH TIME ZONE": TS}
    tables = {name: Base.metadata.tables[name] for name in V0T08_0023_TABLES}
    for table_name, columns in COLUMNS.items():
        described = []
        for column in tables[table_name].c:
            base, _, length = column.type.compile(dialect=postgresql.dialect()).partition("(")
            described.append((column.name, pg_types[base] + (f"({length}" if length else ""), column.nullable))
            assert column.server_default is None and column.default is None, column.name
            assert column.onupdate is None and column.server_onupdate is None, column.name
        assert described == columns, table_name
    for table_name, expected in ((COHORTS, m.COHORT_CHECKS), (SEATS, m.SEAT_CHECKS), (SESSIONS, m.SESSION_CHECKS),
                                 (INTERACTIONS, m.INTERACTION_CHECKS)):
        checks = {c.name: str(c.sqltext) for c in tables[table_name].constraints if isinstance(c, sa.CheckConstraint)}
        assert checks == dict(expected), table_name
    assert [c.name for c in tables[COHORTS].primary_key.columns] == ["campaign_id"]
    assert [c.name for c in tables[SEATS].primary_key.columns] == ["id"]
    assert [c.name for c in tables[SESSIONS].primary_key.columns] == ["id"]
    assert [c.name for c in tables[INTERACTIONS].primary_key.columns] == ["campaign_id", "user_id", "interaction_key"]
    uniques = {c.name: [col.name for col in c.columns] for t in tables.values() for c in t.constraints
               if isinstance(c, sa.UniqueConstraint)}
    assert uniques == {m.SEAT_NUMBER_UNIQUE: ["campaign_id", "seat_number"],
                       m.SEAT_INVITATION_UNIQUE: ["campaign_id", "invitation_id"],
                       m.SEAT_USER_UNIQUE: ["campaign_id", "user_id"],
                       m.SESSION_IDENTITY_UNIQUE: ["id", "campaign_id", "user_id", "environment_digest"]}
    fks = {fk.name: ([e.parent.name for e in fk.elements], [e.target_fullname for e in fk.elements],
                     fk.ondelete, fk.onupdate, fk.deferrable)
           for t in tables.values() for fk in t.foreign_key_constraints}
    assert fks == {
        m.COHORT_CAMPAIGN_FK: (["campaign_id"], ["product_campaigns.id"], None, None, None),
        m.SEAT_COHORT_FK: (["campaign_id"], ["product_campaign_cohorts.campaign_id"], None, None, None),
        m.SEAT_INVITATION_FK: (["invitation_id"], ["invitations.id"], None, None, None),
        m.SEAT_USER_FK: (["user_id"], ["users.id"], None, None, None),
        m.SEAT_BINDING_INVITATION_FK: (["binding_operation_id", "invitation_id"],
                                       ["auth_operations.id", "auth_operations.invitation_id"], None, None, None),
        m.SEAT_BINDING_USER_FK: (["binding_operation_id", "user_id", "binding_operation_kind"],
                                 ["auth_operations.id", "auth_operations.user_id", "auth_operations.kind"],
                                 None, None, None),
        m.SEAT_BINDING_COMMIT_FK: (
            ["binding_operation_id", "binding_operation_kind", "binding_operation_status", "binding_committed_at"],
            ["auth_operations.id", "auth_operations.kind", "auth_operations.status", "auth_operations.committed_at"],
            None, None, None),
        m.SESSION_PARTICIPANT_FK: (["campaign_id", "user_id"], ["product_campaign_participants.campaign_id",
                                                                "product_campaign_participants.user_id"],
                                   None, None, None),
        m.INTERACTION_PARTICIPANT_FK: (["campaign_id", "user_id"], ["product_campaign_participants.campaign_id",
                                                                    "product_campaign_participants.user_id"],
                                       None, None, None),
        m.INTERACTION_SESSION_FK: (["session_id", "campaign_id", "user_id", "environment_digest"],
                                   ["product_activity_sessions.id", "product_activity_sessions.campaign_id",
                                    "product_activity_sessions.user_id",
                                    "product_activity_sessions.environment_digest"],
                                   None, None, None),
    }
    indexes = {index.name: ([e.name for e in index.expressions], index.unique,
                            str(index.dialect_options["postgresql"]["where"]))
               for t in tables.values() for index in t.indexes}
    assert indexes == {m.SESSION_OPEN_UNIQUE: (["campaign_id", "user_id", "environment_digest"], True,
                                               m.SESSION_OPEN_WHERE),
                       m.SESSION_IDLE_INDEX: (["campaign_id", "idle_expires_at"], False, m.SESSION_OPEN_WHERE)}
    assert set(indexes) == V0T08_0023_INDEXES
    # Aucune FK vers les sessions F2 : sessions d'activité distinctes.
    for name in (SESSIONS, INTERACTIONS):
        assert not [fk for fk in tables[name].foreign_key_constraints
                    if any(e.target_fullname.startswith("oryx_session") for e in fk.elements)]
    assert list(m.SESSION_END_REASONS) == ["inactivity", "logout"] and list(m.INTERACTION_KINDS) == ["activity",
                                                                                                     "close"]


def test_no_personal_or_content_field_in_the_new_tables():
    """Ni e-mail, ni nom, ni token, ni hash d'invitation, ni contenu : la
    place ne porte que la référence canonique invitation_id."""
    for table_name, columns in COLUMNS.items():
        for name, _, _ in columns:
            parts = set(name.split("_"))
            for marker in FORBIDDEN_FIELD_MARKERS + ("name", "hash", "cohort_label", "recipient"):
                assert marker not in parts and not name.startswith(marker), (table_name, name)
    assert {t for t in Base.metadata.tables if t.startswith("product_")} >= NEW


# --------------------------------------------------------------------------
# 2. PostgreSQL réel
# --------------------------------------------------------------------------

def _seed_0022(engine) -> dict:
    """Historique 0021 et une campagne / participation 0022 (forgées)."""
    _seed_0021_history(engine)
    campaign = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO product_campaigns (id, code, name, purpose, status, created_at, close_by_at, retention_days, "
            "retention_policy_version, measurement_policy_version) VALUES (:id, 'legacy-2a', 'Lot 2A', 'p', "
            "'planned', :t, :c, 90, 'v0-retention-v1', 'v0-measurement-v1')"),
            {"id": campaign, "t": BASE_TIME, "c": BASE_TIME + timedelta(days=30)})
        user = conn.execute(sa.text("SELECT id FROM users ORDER BY id LIMIT 1")).scalar_one()
        conn.execute(sa.text(
            "INSERT INTO product_campaign_participants VALUES (:c, :u, :s, :e, 'enrolled', :s, :s, 'v0-notice-1')"),
            {"c": campaign, "u": user, "s": BASE_TIME, "e": BASE_TIME + timedelta(days=12)})
    return {"campaign": campaign, "user": user}


def test_pg_upgrade_0022_to_0023_keeps_every_row_then_exact_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0022)
    _seed_0022(pg_engine)
    tables = _tables(pg_engine)
    assert not tables & NEW
    schema_0022, catalog_0022 = _snapshot(pg_engine, tables), _catalog(pg_engine, tables)
    global_0022 = _global_catalog(pg_engine)
    data_0022 = _data(pg_engine, tables - {"alembic_version"})
    assert data_0022["product_campaigns"] and data_0022["product_campaign_participants"] and data_0022["users"]
    assert _compare_metadata(pg_engine) == []  # écarts attendus (quatre tables et deux index) retirés exactement

    _run_alembic(pg_url, "upgrade", V0T08_0023)
    assert _version(pg_engine) == V0T08_0023 and _compare_metadata(pg_engine) == []
    assert _tables(pg_engine) == tables | NEW
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0022  # aucun backfill
    assert _data(pg_engine, NEW) == {t: [] for t in sorted(NEW)}
    others = tables - {"alembic_version"}  # aucune table existante modifiée (F2, analytics 0022 comprises)
    assert _snapshot(pg_engine, others) == {t: schema_0022[t] for t in sorted(others)}
    assert _catalog(pg_engine, others) == {t: catalog_0022[t] for t in sorted(others)}
    assert _global_catalog(pg_engine) == global_0022

    _run_alembic(pg_url, "downgrade", V0T08_0022)
    assert _version(pg_engine) == V0T08_0022 and _tables(pg_engine) == tables
    assert _snapshot(pg_engine, tables) == schema_0022 and _catalog(pg_engine, tables) == catalog_0022
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0022
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == V0T08_0023 and _compare_metadata(pg_engine) == []


def _f2_activation(conn, *, user="v0-cat-user") -> dict:
    invitation, operation = uuid.uuid4(), uuid.uuid4()
    conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant') ON CONFLICT DO NOTHING"), {"u": user})
    conn.execute(sa.text(
        "INSERT INTO invitations (id, token_hash, hash_key_version, cohort, status, created_at, expires_at, "
        "accepted_at) VALUES (:i, :h, 'v1', 'wave-0', 'accepted', :t, :e, :a)"),
        {"i": invitation, "h": hashlib.sha256(invitation.bytes).hexdigest(), "t": BASE_TIME - timedelta(days=1),
         "e": BASE_TIME + timedelta(days=30), "a": BASE_TIME})
    conn.execute(sa.text(
        "INSERT INTO auth_operations (id, kind, status, invitation_id, user_id, operation_schema_version, created_at, "
        "updated_at, expires_at, committed_at) VALUES (:o, 'invitation_activation', 'committed', :i, :u, 'v1', :c, "
        ":t, :x, :t)"),
        {"o": operation, "i": invitation, "u": user, "c": BASE_TIME - timedelta(minutes=1), "t": BASE_TIME,
         "x": BASE_TIME + timedelta(minutes=10)})
    return {"invitation": invitation, "operation": operation, "user": user, "committed_at": BASE_TIME}


SEAT_INSERT = ("INSERT INTO product_cohort_seats (id, campaign_id, seat_number, invitation_id, created_at, user_id, "
               "binding_operation_id, binding_operation_kind, binding_operation_status, binding_committed_at, "
               "bound_at, authorization_recorded_at, notice_version, admission_failure_first_at) VALUES "
               "(gen_random_uuid(), :campaign, :number, :invitation, :created, :user, :operation, :kind, :status, "
               ":committed, :bound, :authorized, :notice, :failure)")


def _seat_params(campaign, proof, **overrides):
    params = {"campaign": campaign, "number": 1, "invitation": proof["invitation"], "created": BASE_TIME,
              "user": proof["user"], "operation": proof["operation"], "kind": "invitation_activation",
              "status": "committed", "committed": proof["committed_at"], "bound": BASE_TIME + timedelta(minutes=1),
              "authorized": BASE_TIME + timedelta(minutes=2), "notice": "v0-notice-1", "failure": None}
    params.update(overrides)
    return params


@pytest.mark.parametrize("constraint,overrides", [
    ("ck_product_cohort_seats_seat_number", {"number": 0}),
    ("ck_product_cohort_seats_seat_number", {"number": 6}),
    ("ck_product_cohort_seats_binding_kind", {"kind": "email_linking"}),
    ("ck_product_cohort_seats_binding_status", {"status": "pending"}),
    ("ck_product_cohort_seats_binding", {"bound": None, "authorized": None, "notice": None}),
    ("ck_product_cohort_seats_bound_at", {"bound": BASE_TIME - timedelta(seconds=1),
                                          "authorized": BASE_TIME + timedelta(minutes=2)}),
    ("ck_product_cohort_seats_authorization", {"notice": None}),
    ("ck_product_cohort_seats_authorization", {"authorized": BASE_TIME}),
    ("ck_product_cohort_seats_admission_failure", {"failure": BASE_TIME}),
    ("product_cohort_seats_binding_user_fkey", {"user": "v0-other-user"}),
    ("product_cohort_seats_binding_commit_fkey", {"committed": BASE_TIME + timedelta(seconds=1)}),
])
def test_pg_every_seat_check_and_binding_fk_is_exercised(pg_url, pg_engine, constraint, overrides):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    with pg_engine.connect() as conn:
        seeded = _seed_campaign_and_cohort(conn)
        proof = _f2_activation(conn)
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('v0-other-user', 'debutant')"))
        conn.execute(sa.text(SEAT_INSERT), _seat_params(seeded, proof, number=2))  # forme légitime
        conn.execute(sa.text("DELETE FROM product_cohort_seats"))
        _rejects(conn, constraint, SEAT_INSERT, **_seat_params(seeded, proof, **overrides))
        conn.rollback()


def _seed_campaign_and_cohort(conn, *, seat_count=5, admission=None) -> uuid.UUID:
    campaign = uuid.uuid4()
    conn.execute(sa.text(
        "INSERT INTO product_campaigns (id, code, name, purpose, status, created_at, close_by_at, retention_days, "
        "retention_policy_version, measurement_policy_version) VALUES (:id, :code, 'Vague 0', 'p', 'planned', :t, :c, "
        "90, 'v0-retention-v1', 'v0-measurement-v1')"),
        {"id": campaign, "code": f"wave-{campaign.hex[:8]}", "t": BASE_TIME - timedelta(days=1),
         "c": BASE_TIME + timedelta(days=30)})
    conn.execute(sa.text(
        "INSERT INTO product_campaign_cohorts VALUES (:c, 'v0-cohort-v1', :n, :a, :t)"),
        {"c": campaign, "n": seat_count, "a": admission or BASE_TIME + timedelta(days=10),
         "t": BASE_TIME - timedelta(days=1)})
    return campaign


def test_pg_cohort_checks_and_foreign_keys(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    with pg_engine.connect() as conn:
        _rejects(conn, "ck_product_campaign_cohorts_seat_count", lambda: _seed_campaign_and_cohort(conn, seat_count=4))
        _rejects(conn, "ck_product_campaign_cohorts_seat_count", lambda: _seed_campaign_and_cohort(conn, seat_count=6))
        _rejects(conn, "ck_product_campaign_cohorts_admission",
                 lambda: _seed_campaign_and_cohort(conn, admission=BASE_TIME - timedelta(days=1)))
        _rejects(conn, "product_campaign_cohorts_campaign_id_fkey",
                 "INSERT INTO product_campaign_cohorts VALUES (gen_random_uuid(), 'v0-cohort-v1', 5, :a, :t)",
                 a=BASE_TIME + timedelta(days=1), t=BASE_TIME)
        campaign = _seed_campaign_and_cohort(conn)
        _rejects(conn, "product_campaign_cohorts_pkey",
                 "INSERT INTO product_campaign_cohorts VALUES (:c, 'v0-cohort-v1', 5, :a, :t)",
                 c=campaign, a=BASE_TIME + timedelta(days=1), t=BASE_TIME)
        unbound = {"user": None, "operation": None, "kind": None, "status": None, "committed": None, "bound": None,
                   "authorized": None, "notice": None}
        proof = _f2_activation(conn)
        _rejects(conn, "product_cohort_seats_invitation_id_fkey", SEAT_INSERT,
                 **_seat_params(campaign, proof, invitation=uuid.uuid4(), **unbound))
        _rejects(conn, "product_cohort_seats_campaign_id_fkey", SEAT_INSERT,
                 **_seat_params(uuid.uuid4(), proof, **unbound))
        conn.execute(sa.text(SEAT_INSERT), _seat_params(campaign, proof, **unbound))  # place pré-J0 non rattachée
        other = _f2_activation(conn, user="v0-cat-user-2")
        _rejects(conn, "product_cohort_seats_binding_invitation_fkey", SEAT_INSERT,
                 **_seat_params(campaign, proof, number=2, invitation=other["invitation"]))
        _rejects(conn, "uq_product_cohort_seats_campaign_invitation", SEAT_INSERT,
                 **_seat_params(campaign, proof, number=2, **unbound))
        conn.execute(sa.text(SEAT_INSERT), _seat_params(campaign, other, number=2))
        _rejects(conn, "uq_product_cohort_seats_campaign_user",
                 "UPDATE product_cohort_seats SET user_id = :u, binding_operation_id = :o, "
                 "binding_operation_kind = 'invitation_activation', binding_operation_status = 'committed', "
                 "binding_committed_at = :c, bound_at = :b WHERE seat_number = 1",
                 u=other["user"], o=other["operation"], c=other["committed_at"], b=BASE_TIME + timedelta(hours=1))
        # Les tables F2 référencées ne peuvent perdre une ligne référencée (aucune cascade).
        pending = uuid.uuid4()
        conn.execute(sa.text(
            "INSERT INTO invitations (id, token_hash, hash_key_version, cohort, status, created_at, expires_at) "
            "VALUES (:i, :h, 'v1', 'wave-0', 'pending', :t, :e)"),
            {"i": pending, "h": hashlib.sha256(pending.bytes).hexdigest(), "t": BASE_TIME,
             "e": BASE_TIME + timedelta(days=30)})
        conn.execute(sa.text(SEAT_INSERT), _seat_params(campaign, proof, number=3, invitation=pending, **unbound))
        _rejects(conn, "product_cohort_seats_invitation_id_fkey", "DELETE FROM invitations WHERE id = :i", i=pending)
        conn.rollback()


SESSION_ID = uuid.UUID("5e55105e-0000-4000-8000-000000000023")


def test_pg_downgrade_is_refused_when_any_new_table_has_data(pg_url, pg_engine):  # noqa: F811
    for table in (INTERACTIONS, SESSIONS, SEATS, COHORTS):
        for offline in (False, True):
            _reset_schema(pg_engine)
            _run_alembic(pg_url, "upgrade", "head")
            with pg_engine.begin() as conn:
                campaign = _seed_campaign_and_cohort(conn)
                if table in (SEATS, SESSIONS, INTERACTIONS):
                    conn.execute(sa.text(SEAT_INSERT), _seat_params(campaign, _f2_activation(conn)))
                if table in (SESSIONS, INTERACTIONS):
                    conn.execute(sa.text("DELETE FROM product_cohort_seats"))
                    conn.execute(sa.text("DELETE FROM product_campaign_cohorts"))
                    conn.execute(sa.text(
                        "INSERT INTO product_campaign_participants VALUES (:c, 'v0-cat-user', :s, :e, 'enrolled', "
                        ":s, :s, 'v0-notice-1')"),
                        {"c": campaign, "s": BASE_TIME, "e": BASE_TIME + timedelta(days=12)})
                    conn.execute(sa.text(
                        "INSERT INTO product_activity_sessions VALUES (:s, :c, 'v0-cat-user', :d, :t, :t, :x, NULL, "
                        "NULL)"),
                        {"s": SESSION_ID, "c": campaign, "d": "0" * 64, "t": BASE_TIME,
                         "x": BASE_TIME + timedelta(minutes=30)})
                if table == INTERACTIONS:
                    conn.execute(sa.text(
                        "INSERT INTO product_activity_session_interactions VALUES (:c, 'v0-cat-user', "
                        "gen_random_uuid(), 'activity', :d, :s, :t)"),
                        {"c": campaign, "d": "0" * 64, "s": SESSION_ID, "t": BASE_TIME})
            tables = _tables(pg_engine)
            before = _data(pg_engine, tables - {"alembic_version"})
            expected = f"downgrade de 0023_analytics_cohort_sessions refusé, la table {table} contient des données"
            if offline:
                statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{V0T08_0023}:{V0T08_0022}",
                                                      "--sql").stdout)
                lock = next(s for s in statements if s.startswith("LOCK TABLE"))
                guard = next(s for s in statements if s.startswith("DO $$"))
                with pg_engine.connect() as conn:
                    with pytest.raises(sa.exc.DBAPIError, match=expected):
                        with conn.begin():
                            conn.exec_driver_sql(lock)
                            conn.exec_driver_sql(guard)
            else:
                with pytest.raises(Exception) as failure:
                    _run_alembic(pg_url, "downgrade", V0T08_0022)
                assert expected in str(getattr(failure.value, "stderr", failure.value))
            assert _version(pg_engine) == V0T08_0023
            assert _data(pg_engine, tables - {"alembic_version"}) == before


def test_pg_offline_upgrade_then_offline_downgrade_round_trip(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0022)
    tables = _tables(pg_engine)
    catalog_0022 = _catalog(pg_engine, tables)
    up = _run_alembic(OFFLINE, "upgrade", f"{V0T08_0022}:{V0T08_0023}", "--sql").stdout
    down = _run_alembic(OFFLINE, "downgrade", f"{V0T08_0023}:{V0T08_0022}", "--sql").stdout
    with pg_engine.begin() as conn:
        for statement in _statements(up):
            conn.exec_driver_sql(statement)
    assert _version(pg_engine) == V0T08_0023 and _compare_metadata(pg_engine) == []
    offline_catalog = _catalog(pg_engine, tables | NEW)
    with pg_engine.begin() as conn:
        for statement in _statements(down):
            conn.exec_driver_sql(statement)
    assert _version(pg_engine) == V0T08_0022 and _catalog(pg_engine, tables) == catalog_0022
    _run_alembic(pg_url, "upgrade", V0T08_0023)
    assert _catalog(pg_engine, tables | NEW) == offline_catalog


SESSION_INSERT = ("INSERT INTO product_activity_sessions VALUES (:id, :campaign, 'v0-cat-user', :digest, :opened, "
                  ":last, :idle, :finalized, :reason)")
INTERACTION_INSERT = ("INSERT INTO product_activity_session_interactions VALUES (:campaign, :user, :key, :kind, "
                      ":digest, :session, :at)")


@pytest.mark.parametrize("constraint,overrides", [
    ("ck_product_activity_sessions_end", {"finalized": BASE_TIME + timedelta(minutes=40)}),
    ("ck_product_activity_sessions_end", {"reason": "inactivity"}),
    ("ck_product_activity_sessions_end_reason", {"finalized": BASE_TIME + timedelta(minutes=40), "reason": "crash"}),
    # inactivité : jamais avant l'expiration
    ("ck_product_activity_sessions_finalized",
     {"finalized": BASE_TIME + timedelta(minutes=29), "reason": "inactivity"}),
    # déconnexion : jamais avant la dernière activité, toujours avant l'expiration
    ("ck_product_activity_sessions_finalized", {"finalized": BASE_TIME - timedelta(seconds=1), "reason": "logout"}),
    ("ck_product_activity_sessions_finalized", {"finalized": BASE_TIME + timedelta(minutes=30), "reason": "logout"}),
    ("ck_product_activity_sessions_idle_expiry", {"idle": BASE_TIME + timedelta(days=15)}),
    ("ck_product_activity_sessions_activity", {"opened": BASE_TIME + timedelta(seconds=1)}),
    ("ck_product_activity_sessions_environment_digest", {"digest": "not-a-digest"}),
])
def test_pg_every_session_check_is_exercised(pg_url, pg_engine, constraint, overrides):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    base = {"id": uuid.uuid4(), "digest": "0" * 64, "opened": BASE_TIME, "last": BASE_TIME,
            "idle": BASE_TIME + timedelta(minutes=30), "finalized": None, "reason": None}
    with pg_engine.connect() as conn:
        campaign = _seed_campaign_and_cohort(conn)
        _f2_activation(conn)
        conn.execute(sa.text(
            "INSERT INTO product_campaign_participants VALUES (:c, 'v0-cat-user', :s, :e, 'enrolled', :s, :s, 'n')"),
            {"c": campaign, "s": BASE_TIME, "e": BASE_TIME + timedelta(days=12)})
        for legit in ({}, {"finalized": BASE_TIME, "reason": "logout"},
                      {"finalized": BASE_TIME + timedelta(minutes=29), "reason": "logout"},
                      {"finalized": BASE_TIME + timedelta(minutes=30), "reason": "inactivity"}):
            conn.execute(sa.text(SESSION_INSERT), {**base, "campaign": campaign, **legit, "id": uuid.uuid4(),
                                                   "digest": uuid.uuid4().hex * 2})
        _rejects(conn, constraint, SESSION_INSERT, **{**base, "campaign": campaign, **overrides})
        conn.rollback()


def test_pg_interaction_registry_constraints(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    with pg_engine.connect() as conn:
        campaign = _seed_campaign_and_cohort(conn)
        _f2_activation(conn)
        _f2_activation(conn, user="v0-cat-user-2")
        for user in ("v0-cat-user", "v0-cat-user-2"):
            conn.execute(sa.text(
                "INSERT INTO product_campaign_participants VALUES (:c, :u, :s, :e, 'enrolled', :s, :s, 'n')"),
                {"c": campaign, "u": user, "s": BASE_TIME, "e": BASE_TIME + timedelta(days=12)})
        session, digest, key = uuid.uuid4(), "a" * 64, uuid.uuid4()
        conn.execute(sa.text(SESSION_INSERT), {"id": session, "campaign": campaign, "digest": digest,
                                               "opened": BASE_TIME, "last": BASE_TIME,
                                               "idle": BASE_TIME + timedelta(minutes=30), "finalized": None,
                                               "reason": None})
        row = {"campaign": campaign, "user": "v0-cat-user", "key": key, "kind": "activity", "digest": digest,
               "session": session, "at": BASE_TIME}
        conn.execute(sa.text(INTERACTION_INSERT), row)
        # Une clé n'est acceptée qu'une fois par participation (rejeu ancien compris).
        _rejects(conn, "product_activity_session_interactions_pkey", INTERACTION_INSERT, **row)
        # Même clé pour un autre utilisateur : indépendante (sans session : clôture seulement).
        conn.execute(sa.text(INTERACTION_INSERT), {**row, "user": "v0-cat-user-2", "kind": "close", "session": None})
        _rejects(conn, "ck_product_activity_session_interactions_session", INTERACTION_INSERT,
                 **{**row, "key": uuid.uuid4(), "session": None})
        _rejects(conn, "ck_product_activity_session_interactions_kind", INTERACTION_INSERT,
                 **{**row, "key": uuid.uuid4(), "kind": "refresh"})
        _rejects(conn, "ck_product_activity_session_interactions_environment_digest", INTERACTION_INSERT,
                 **{**row, "key": uuid.uuid4(), "digest": "raw-key"})
        # La session doit appartenir à la même participation ET au même environnement.
        _rejects(conn, "product_activity_session_interactions_session_fkey", INTERACTION_INSERT,
                 **{**row, "key": uuid.uuid4(), "digest": "b" * 64})
        _rejects(conn, "product_activity_session_interactions_session_fkey", INTERACTION_INSERT,
                 **{**row, "key": uuid.uuid4(), "user": "v0-cat-user-2"})
        _rejects(conn, "product_activity_session_interactions_participant_fkey", INTERACTION_INSERT,
                 **{**row, "key": uuid.uuid4(), "user": "v0-unknown", "kind": "close", "session": None})
        conn.rollback()
