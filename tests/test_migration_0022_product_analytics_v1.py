"""Tests de V0-T08 Lot 1 : migration 0022_product_analytics_v1 (socle
PostgreSQL DORMANT de l'analytics produit) et modèles correspondants.

1. Sans base : maillon 0021 -> 0022 (parent de 0023, V0-T08 Lot 2B-1,
   elle-même parent de 0024, V0-T08 Lot 2B-2 bloc A, testées à part), identifiant <= 32 caractères, fichiers 0001 à 0021 inchangés, SQL offline (upgrade : trois
   CREATE TABLE et leurs index, rien d'autre ; downgrade : délais, verrou
   déterministe des trois tables, garde DO évaluée à l'exécution, retrait
   sans CASCADE), modèles alignés sur la migration (colonnes, types,
   nullabilité, CHECK, UNIQUE, FK, index ; la seule UNIQUE du modèle des
   événements absente de 0022 est exactement celle qu'ajoute 0024),
   aucun champ sensible.
2. Dormance : aucun module de production (hors core/models.py) ne mentionne
   les tables ni les modèles ; aucune route d'activité ; le vrai api:app
   n'expose aucune route nouvelle et répond 404 à
   POST /api/v2/me/activity-signals.
3. PostgreSQL réel (base jetable) : montée 0021 -> 0022 sur données
   historiques (pédagogiques, portefeuille, frontière F2) conservées à
   l'identique, aucun backfill, aucune ligne créée, catalogue exact (types,
   contraintes, index DESC et partiels), aucun trigger, fonction, ENUM ni
   extension ; chaque CHECK, PK, FK et UNIQUE réellement exercé ; formes
   légitimes acceptées (enregistrement retardé, plusieurs types dans une
   visite, plusieurs campagnes) ; downgrade exact vers 0021 si les trois
   tables sont vides ; downgrade REFUSÉ, en ligne et par la garde offline
   exécutée, si l'une quelconque des trois tables contient une ligne.
"""
import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from core.db import Base
from tests.pg_guard import assert_disposable_connection
from tests.test_f2_dormancy import MAIN_ROUTES
from tests.test_f2_http_dormancy import _clean_env
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
    PR9B1_0021,
    PR9B1_0021_FILE,
    PR9B1_TABLES,
    PR1_TABLES,
    PR2_TABLES,
    T2A_TABLES,
    T3A_TABLES,
    T4A_TABLES,
    T5A_TABLES,
    T6A_TABLES,
    V0T08_0022,
    V0T08_0023,
    V0T08_0023_TABLES,
    V0T08_0024,
    V0T08_0024_FILE,
    V0T08_0024_TABLES,
    V0T08_0022_FILE,
    V0T08_0022_INDEXES,
    V0T08_0022_TABLES,
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
from tests.test_migration_0021_decryptage_turn_reservations import PR7_SHA256
from tests.test_migration_0021_decryptage_turn_reservations import _insert as _insert_reservation
from tests.test_migration_0021_decryptage_turn_reservations import _seed_conversation

MIGRATION = REPO_ROOT / "alembic" / "versions" / V0T08_0022_FILE
# sha256 de 0021 telle que fusionnée sur main (faeab09, inchangée jusqu'à
# 96f6eab) : V0-T08 Lot 1 ne la modifie pas.
PR9B1_SHA256 = {PR9B1_0021_FILE: "3da753e1698da9d9ef508c9addb1e16d450c6433a0fe8bebe7896c57a030e436"}
OFFLINE = "postgresql://offline@localhost/offline"
GUARDS = ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '15s'",
          "SET LOCAL idle_in_transaction_session_timeout = '15s'"]
CAMPAIGNS, PARTICIPANTS, EVENTS = V0T08_0022_TABLES
ANALYTICS = set(V0T08_0022_TABLES)
# Colonnes exactes, dans l'ordre du freeze : (nom, type PostgreSQL, nullable).
COLUMNS = {
    CAMPAIGNS: [
        ("id", "uuid", False), ("code", "character varying(64)", False),
        ("name", "character varying(160)", False), ("purpose", "text", False),
        ("status", "character varying(16)", False), ("created_at", "timestamp with time zone", False),
        ("activated_at", "timestamp with time zone", True), ("closed_at", "timestamp with time zone", True),
        ("close_by_at", "timestamp with time zone", False), ("retention_days", "smallint", False),
        ("retention_policy_version", "character varying(64)", False),
        ("measurement_policy_version", "character varying(64)", False),
    ],
    PARTICIPANTS: [
        ("campaign_id", "uuid", False), ("user_id", "character varying", False),
        ("window_start_at", "timestamp with time zone", False), ("window_end_at", "timestamp with time zone", False),
        ("participation_status", "character varying(16)", False), ("enrolled_at", "timestamp with time zone", False),
        ("authorization_recorded_at", "timestamp with time zone", True),
        ("notice_version", "character varying(64)", True),
    ],
    EVENTS: [
        ("id", "uuid", False), ("user_id", "character varying", False), ("visit_id", "uuid", False),
        ("idempotency_key", "uuid", False), ("event_type", "character varying(32)", False),
        ("surface", "character varying(32)", False), ("executed_route", "character varying(32)", True),
        ("source", "character varying(24)", False), ("source_operation_kind", "character varying(64)", True),
        ("source_operation_id", "uuid", True), ("outcome", "character varying(24)", False),
        ("delivery_status", "character varying(24)", False), ("occurred_at", "timestamp with time zone", False),
        ("recorded_at", "timestamp with time zone", False), ("updated_at", "timestamp with time zone", False),
        ("resolved_at", "timestamp with time zone", True), ("delivered_at", "timestamp with time zone", True),
        ("retention_scopes", "jsonb", False), ("next_scope_expires_at", "timestamp with time zone", False),
        ("purge_after_at", "timestamp with time zone", False), ("schema_version", "character varying(64)", False),
    ],
}
EVENT_TYPES = ["session_started", "education_used", "coach_used", "decrypter_used", "portfolio_used",
               "profile_viewed", "view_opened"]
SOURCES = ["client_signal", "server_business", "server_visit"]
OUTCOMES = ["in_progress", "succeeded", "failed", "indeterminate"]
DELIVERY_STATUSES = ["not_applicable", "pending", "confirmed", "unconfirmed"]
# Index exacts (pg_indexes.indexdef) : accès par date (DESC), par visite,
# par échéances, participations par utilisateur et fenêtre, unicités.
INDEXDEFS = {
    "ix_product_campaign_participants_user_window":
        "CREATE INDEX ix_product_campaign_participants_user_window ON public.product_campaign_participants "
        "USING btree (user_id, window_start_at, window_end_at)",
    "ix_product_activity_events_user_occurred":
        "CREATE INDEX ix_product_activity_events_user_occurred ON public.product_activity_events "
        "USING btree (user_id, occurred_at DESC, id DESC)",
    "ix_product_activity_events_user_visit":
        "CREATE INDEX ix_product_activity_events_user_visit ON public.product_activity_events "
        "USING btree (user_id, visit_id)",
    "ix_product_activity_events_next_scope_expires_at":
        "CREATE INDEX ix_product_activity_events_next_scope_expires_at ON public.product_activity_events "
        "USING btree (next_scope_expires_at)",
    "ix_product_activity_events_purge_after_at":
        "CREATE INDEX ix_product_activity_events_purge_after_at ON public.product_activity_events "
        "USING btree (purge_after_at)",
    "uq_product_activity_events_visit_session_start":
        "CREATE UNIQUE INDEX uq_product_activity_events_visit_session_start ON public.product_activity_events "
        "USING btree (user_id, visit_id) WHERE ((event_type)::text = 'session_started'::text)",
    "uq_product_activity_events_source_operation":
        "CREATE UNIQUE INDEX uq_product_activity_events_source_operation ON public.product_activity_events "
        "USING btree (user_id, source_operation_kind, source_operation_id) "
        "WHERE ((source_operation_kind IS NOT NULL) AND (source_operation_id IS NOT NULL))",
}
# Index portés par les contraintes PK / UNIQUE.
CONSTRAINT_INDEXES = {"product_campaigns_pkey", "uq_product_campaigns_code", "product_campaign_participants_pkey",
                      "product_activity_events_pkey", "uq_product_activity_events_user_idempotency_key"}
# Familles de tables historiques explicitement surveillées (en plus de
# l'ensemble du schéma 0021) : pédagogiques C1-C12, portefeuille, frontière
# d'autorisation F2.
PEDAGOGICAL_TABLES = T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES | T6A_TABLES
PORTFOLIO_TABLES = {"portfolio_positions", "portfolio_write_idempotency", "investment_theses"}
F2_TABLES = PR1_TABLES | PR2_TABLES | PR9B1_TABLES | {"users", "conversation_identities", "assistant_deliveries"}
# Champs interdits dans les tables analytiques (aucun texte, prompt, réponse,
# donnée financière ou pédagogique, secret).
FORBIDDEN_FIELD_MARKERS = ("question", "prompt", "response", "answer", "text", "content", "message", "ticker",
                           "price", "quantity", "amount", "value", "competency", "capability", "secret", "token",
                           "password", "email", "ip", "user_agent", "payload")
BASE_TIME = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)


def _m0022():
    spec = importlib.util.spec_from_file_location("m0022", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _m0024():
    spec = importlib.util.spec_from_file_location("m0024_from_0022", MIGRATION.parent / V0T08_0024_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_links_0022_to_0021_and_is_the_parent_of_0023():
    """0022 était la tête de V0-T08 Lot 1 ; le lot 2B-1 ajoute 0023 au-dessus,
    puis le lot 2B-2 bloc A ajoute 0024, sans modifier ce fichier
    (tests/test_migration_0023_analytics_cohort_sessions.py,
    tests/test_migration_0024_product_activity_proofs.py)."""
    script = _script_directory()
    assert script.get_heads() == [V0T08_0024]
    assert script.get_revision(V0T08_0024).down_revision == V0T08_0023
    assert script.get_revision(V0T08_0023).down_revision == V0T08_0022
    revision = script.get_revision(V0T08_0022)
    assert revision.down_revision == PR9B1_0021
    assert not revision.branch_labels and not revision.dependencies
    assert V0T08_0022 == "0022_product_analytics_v1" and len(V0T08_0022) <= 32
    assert V0T08_0022_FILE == f"{V0T08_0022}.py"
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(files) == 24 and files[-3] == V0T08_0022_FILE and files[-4] == PR9B1_0021_FILE
    m = _m0022()
    assert (m.revision, m.down_revision, m.branch_labels, m.depends_on) == (V0T08_0022, PR9B1_0021, None, None)


def test_migrations_0001_to_0021_are_unchanged():
    for name, digest in {**PR1_SHA256, **PR2_SHA256, **PR4_SHA256, **PR7_SHA256, **PR9B1_SHA256}.items():
        path = REPO_ROOT / "alembic" / "versions" / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
    for line in (REPO_ROOT / "ci" / "migration_history.sha256").read_text().splitlines():  # 0001 à 0014
        digest, _, relative = line.partition("  ")
        assert hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest() == digest, relative


def test_offline_upgrade_creates_only_the_three_tables_and_their_indexes():
    statements = _statements(_run_alembic(OFFLINE, "upgrade", f"{PR9B1_0021}:{V0T08_0022}", "--sql").stdout)
    assert statements[:3] == GUARDS
    creates = [s for s in statements if s.startswith("CREATE TABLE")]
    assert [s.split(" ")[2] for s in creates] == [CAMPAIGNS, PARTICIPANTS, EVENTS]
    indexes = [s for s in statements if s.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX"))]
    assert sorted(s.split(" INDEX ")[1].split(" ")[0] for s in indexes) == sorted(V0T08_0022_INDEXES)
    assert statements[-1] == (f"UPDATE alembic_version SET version_num='{V0T08_0022}' "
                              f"WHERE alembic_version.version_num = '{PR9B1_0021}'")
    assert len(statements) == 3 + len(creates) + len(indexes) + 1
    m = _m0022()
    by_table = dict(zip((CAMPAIGNS, PARTICIPANTS, EVENTS), creates))
    for table, checks in ((CAMPAIGNS, m.CAMPAIGN_CHECKS), (PARTICIPANTS, m.PARTICIPANT_CHECKS),
                          (EVENTS, m.EVENT_CHECKS)):
        for name, expression in checks:
            assert f"CONSTRAINT {name} CHECK ({expression})" in by_table[table], name
    for table, fragment in (
            (CAMPAIGNS, "CONSTRAINT product_campaigns_pkey PRIMARY KEY (id)"),
            (CAMPAIGNS, "CONSTRAINT uq_product_campaigns_code UNIQUE (code)"),
            (PARTICIPANTS, "CONSTRAINT product_campaign_participants_pkey PRIMARY KEY (campaign_id, user_id)"),
            (PARTICIPANTS, "CONSTRAINT product_campaign_participants_campaign_id_fkey FOREIGN KEY(campaign_id) "
                           "REFERENCES product_campaigns (id)"),
            (PARTICIPANTS, "CONSTRAINT product_campaign_participants_user_id_fkey FOREIGN KEY(user_id) "
                           "REFERENCES users (id)"),
            (EVENTS, "CONSTRAINT product_activity_events_pkey PRIMARY KEY (id)"),
            (EVENTS, "CONSTRAINT product_activity_events_user_id_fkey FOREIGN KEY(user_id) REFERENCES users (id)"),
            (EVENTS, "CONSTRAINT uq_product_activity_events_user_idempotency_key UNIQUE (user_id, idempotency_key)")):
        assert fragment in by_table[table], fragment
    upper = " ".join(statements).upper()
    for forbidden in ("TRIGGER", "FUNCTION", "CREATE TYPE", "ENUM", "EXTENSION", "ALTER TABLE", "DROP", "CASCADE",
                      "DEFERRABLE", "INSERT INTO", "DELETE FROM", "DEFAULT", "SEQUENCE", "SERIAL", "ON DELETE",
                      "ON UPDATE", "COMMENT ON"):
        assert forbidden not in upper, forbidden
    assert [s for s in statements if s.upper().startswith("UPDATE")] == [statements[-1]]


def test_offline_downgrade_locks_then_guards_each_table_at_execution_then_drops_without_cascade():
    statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{V0T08_0022}:{PR9B1_0021}", "--sql").stdout)
    message = ("V0-T08 Lot 1 : downgrade de 0022_product_analytics_v1 refusé, la table {} contient des données. "
               "Aucune donnée supprimée.")
    assert statements == GUARDS + [
        f"LOCK TABLE {CAMPAIGNS}, {PARTICIPANTS}, {EVENTS} IN ACCESS EXCLUSIVE MODE",
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {EVENTS}) THEN RAISE EXCEPTION '{message.format(EVENTS)}'; "
        f"ELSIF EXISTS (SELECT 1 FROM {PARTICIPANTS}) THEN RAISE EXCEPTION '{message.format(PARTICIPANTS)}'; "
        f"ELSIF EXISTS (SELECT 1 FROM {CAMPAIGNS}) THEN RAISE EXCEPTION '{message.format(CAMPAIGNS)}'; "
        "END IF; END $$",
        f"DROP TABLE {EVENTS}",
        f"DROP TABLE {PARTICIPANTS}",
        f"DROP TABLE {CAMPAIGNS}",
        f"UPDATE alembic_version SET version_num='{PR9B1_0021}' WHERE alembic_version.version_num = '{V0T08_0022}'",
    ]
    assert "CASCADE" not in " ".join(statements).upper()


def _sa_type(column) -> str:
    return column.type.compile(dialect=postgresql.dialect())


def test_models_match_the_migration():
    m = _m0022()
    pg_types = {"UUID": "uuid", "TEXT": "text", "SMALLINT": "smallint", "JSONB": "jsonb", "VARCHAR": "character varying",
                "TIMESTAMP WITH TIME ZONE": "timestamp with time zone"}
    for table_name, columns in COLUMNS.items():
        table = Base.metadata.tables[table_name]
        described = []
        for column in table.c:
            compiled = _sa_type(column)
            base, _, length = compiled.partition("(")
            described.append((column.name, pg_types[base] + (f"({length}" if length else ""), column.nullable))
            assert column.server_default is None and column.default is None, column.name
            assert column.onupdate is None and column.server_onupdate is None, column.name
        assert described == columns, table_name
    tables = {name: Base.metadata.tables[name] for name in V0T08_0022_TABLES}
    for table_name, expected in ((CAMPAIGNS, m.CAMPAIGN_CHECKS), (PARTICIPANTS, m.PARTICIPANT_CHECKS),
                                 (EVENTS, m.EVENT_CHECKS)):
        checks = {c.name: str(c.sqltext) for c in tables[table_name].constraints
                  if isinstance(c, sa.CheckConstraint)}
        assert checks == dict(expected), table_name
    assert [c.name for c in tables[CAMPAIGNS].primary_key.columns] == ["id"]
    assert [c.name for c in tables[PARTICIPANTS].primary_key.columns] == ["campaign_id", "user_id"]
    assert [c.name for c in tables[EVENTS].primary_key.columns] == ["id"]
    uniques = {(t.name, c.name): [col.name for col in c.columns] for t in tables.values() for c in t.constraints
               if isinstance(c, sa.UniqueConstraint)}
    # Différence de révision EXPLICITE : le modèle courant des événements porte
    # en plus la seule UNIQUE composite ajoutée par 0024 (cible de la FK des
    # attributions), avec exactement les colonnes de 0024 ; le catalogue de
    # 0022 reste vérifié tel quel (test_pg_catalog_is_exact).
    m24 = _m0024()
    added_by_0024 = {(table, name): list(columns) for name, table, columns in m24.EXISTING_TABLE_UNIQUES
                     if table in V0T08_0022_TABLES}
    assert added_by_0024 == {(EVENTS, "uq_product_activity_events_attribution"): [
        "id", "user_id", "visit_id", "event_type", "surface", "source_operation_kind", "source_operation_id"]}
    assert uniques == {(CAMPAIGNS, m.CAMPAIGN_CODE_UNIQUE): ["code"],
                       (EVENTS, m.EVENT_IDEMPOTENCY_UNIQUE): ["user_id", "idempotency_key"], **added_by_0024}
    fks = {fk.name: ([e.parent.name for e in fk.elements], [e.target_fullname for e in fk.elements],
                     fk.ondelete, fk.onupdate, fk.deferrable)
           for t in tables.values() for fk in t.foreign_key_constraints}
    assert fks == {m.PARTICIPANT_CAMPAIGN_FK: (["campaign_id"], ["product_campaigns.id"], None, None, None),
                   m.PARTICIPANT_USER_FK: (["user_id"], ["users.id"], None, None, None),
                   m.EVENT_USER_FK: (["user_id"], ["users.id"], None, None, None)}
    indexes = {}
    for table in tables.values():
        for index in table.indexes:
            where = index.dialect_options["postgresql"]["where"]
            indexes[index.name] = ([e.name if isinstance(e, sa.Column) else str(e) for e in index.expressions],
                                   index.unique,
                                   None if where is None else str(where))
    assert indexes == {
        m.PARTICIPANT_USER_WINDOW_INDEX: (["user_id", "window_start_at", "window_end_at"], False, None),
        m.EVENT_USER_OCCURRED_INDEX: (["user_id", "occurred_at DESC", "id DESC"], False, None),
        m.EVENT_USER_VISIT_INDEX: (["user_id", "visit_id"], False, None),
        m.EVENT_NEXT_EXPIRY_INDEX: (["next_scope_expires_at"], False, None),
        m.EVENT_PURGE_INDEX: (["purge_after_at"], False, None),
        m.EVENT_SESSION_START_UNIQUE: (["user_id", "visit_id"], True, m.EVENT_SESSION_START_WHERE),
        m.EVENT_SOURCE_OPERATION_UNIQUE: (["user_id", "source_operation_kind", "source_operation_id"], True,
                                          m.EVENT_SOURCE_OPERATION_WHERE),
    }
    assert set(indexes) == V0T08_0022_INDEXES
    # user_id : exactement le type de users.id (String, jamais converti en UUID).
    assert type(Base.metadata.tables["users"].c.id.type) is sa.String
    for name in (PARTICIPANTS, EVENTS):
        assert type(tables[name].c.user_id.type) is sa.String and tables[name].c.user_id.type.length is None
    # Valeurs fermées : exactement celles du freeze (sept types, « unconfirmed », jamais « unknown »).
    assert list(m.EVENT_TYPES) == EVENT_TYPES and list(m.SOURCES) == SOURCES
    assert list(m.OUTCOMES) == OUTCOMES and list(m.DELIVERY_STATUSES) == DELIVERY_STATUSES
    assert list(m.CAMPAIGN_STATUSES) == ["planned", "active", "closed", "cancelled"]
    assert list(m.PARTICIPATION_STATUSES) == ["enrolled", "completed", "withdrawn"]
    checks = dict(m.EVENT_CHECKS)
    for values, name in ((EVENT_TYPES, "event_type"), (SOURCES, "source"), (OUTCOMES, "outcome"),
                         (DELIVERY_STATUSES, "delivery_status")):
        assert checks[f"ck_product_activity_events_{name}"] == f"{name} IN ({', '.join(repr(v) for v in values)})"
    assert "unknown" not in checks["ck_product_activity_events_delivery_status"]


def test_no_forbidden_or_sensitive_field_in_the_analytics_tables():
    for table_name, columns in COLUMNS.items():
        for name, _, _ in columns:
            parts = name.split("_")
            for marker in FORBIDDEN_FIELD_MARKERS:
                assert marker not in parts and not name.startswith(marker), (table_name, name)
    # V0-T08 Lot 2B-1 et 2B-2 bloc A : les tables de 0023 et de 0024 sont
    # vérifiées à part (tests/test_migration_0023_analytics_cohort_sessions.py,
    # tests/test_migration_0024_product_activity_proofs.py).
    assert {t for t in Base.metadata.tables if t.startswith("product_")} == ANALYTICS | set(V0T08_0023_TABLES) | set(
        V0T08_0024_TABLES)


# --------------------------------------------------------------------------
# 2. Dormance : aucune route, aucune collecte, aucun usage applicatif
# --------------------------------------------------------------------------

DORMANCY_MARKERS = ("product_campaigns", "product_campaign_participants", "product_activity_events",
                    "ProductCampaign", "ProductCampaignParticipant", "ProductActivityEvent", "activity-signals",
                    "activity_signals", "retention_scopes", "retention-scopes-v1")


def _code_strings_and_names(path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name)
        elif isinstance(node, ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def test_no_production_module_references_the_analytics_tables():
    """Seuls core/models.py (déclaration), les migrations 0022, 0023 et 0024 et,
    depuis V0-T08 Lot 2A, les services dormants core/product_analytics/ mentionnent
    les tables : aucune route, aucun worker ni rapport ne les lit ou ne les
    écrit. Que rien n'importe ces services est vérifié par
    tests/test_product_analytics_dormancy.py."""
    services = REPO_ROOT / "core" / "product_analytics"
    # V0-T08 Lot 2B-1 : la migration 0023 référence product_campaigns et
    # product_campaign_participants par FK (testée à part).
    migration_0023 = REPO_ROOT / "alembic" / "versions" / "0023_analytics_cohort_sessions.py"
    # V0-T08 Lot 2B-2 bloc A : la migration 0024 référence
    # product_campaign_participants et product_activity_events (FK composites
    # et UNIQUE ajoutée), testée à part.
    migration_0024 = REPO_ROOT / "alembic" / "versions" / V0T08_0024_FILE
    allowed = {REPO_ROOT / "core" / "models.py", MIGRATION, migration_0023, migration_0024, *services.glob("*.py")}
    assert len(allowed) == 4 + 8
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).parts
        if relative[0] in ("tests", ".git") or "node_modules" in relative or path in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        if any(marker in source for marker in DORMANCY_MARKERS):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []
    for directory in ("web", "web-v2"):
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if path.is_file() and path.suffix in (".html", ".js", ".mjs", ".css", ".json"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                assert not any(marker in text for marker in DORMANCY_MARKERS), path
    models_names = _code_strings_and_names(REPO_ROOT / "core" / "models.py")
    assert {"ProductCampaign", "ProductCampaignParticipant", "ProductActivityEvent"} <= models_names


PROBE = r'''
import asyncio, json, socket, sys
attempts = []
def guard(self, address):
    attempts.append(repr(address))
    raise OSError("réseau interdit pendant le test de dormance")
socket.socket.connect = guard
socket.create_connection = lambda *a, **k: guard(None, a[0] if a else None)

import api
import httpx

CASES = [
    ("POST", "/api/v2/me/activity-signals", {"origin": "https://oryx.test", "content-type": "application/json"}),
    ("GET", "/api/v2/me/activity-signals", {}),
    ("POST", "/api/v2/activity-signals", {"content-type": "application/json"}),
    ("POST", "/api/activity", {"content-type": "application/json"}),
    ("GET", "/api/v2/me/activity", {}),
]
BODY = b'{"event_type":"session_started","visit_id":"11111111-2222-4333-8444-555555555555"}'

async def probe():
    transport = httpx.ASGITransport(app=api.app, client=("192.0.2.1", 1))
    out = []
    async with httpx.AsyncClient(transport=transport, base_url="https://oryx.test") as client:
        for method, path, headers in CASES:
            r = await client.request(method, path, headers=headers, content=BODY if method == "POST" else None)
            out.append([method, path, r.status_code])
    return out

responses = asyncio.run(probe())
routes = sorted({(m.lower(), r.path) for r in api.app.routes for m in getattr(r, "methods", ()) if m != "HEAD"})
print(json.dumps({"responses": responses, "routes": routes, "attempts": attempts}))
'''


@pytest.mark.parametrize("environment", [
    {},
    {"ORYX_PRODUCT_ANALYTICS": "1", "ORYX_ACTIVITY_SIGNALS": "true", "ORYX_V0_T08_ENABLED": "1",
     "ORYX_ANALYTICS_ENABLED": "on", "ORYX_F2_ENABLED": "1", "ORYX_AUTHZ_F2": "1"},
])
def test_real_api_exposes_no_activity_route_whatever_the_environment(environment):
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env(environment), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert all(status in (404, 405) for _, _, status in result["responses"]), result["responses"]
    assert [r for r in result["responses"] if r[1] == "/api/v2/me/activity-signals" and r[0] == "POST"] == [
        ["POST", "/api/v2/me/activity-signals", 404]]
    assert [tuple(r) for r in result["routes"] if not r[1].startswith(("/docs", "/openapi", "/redoc"))] == sorted(
        set(MAIN_ROUTES))
    assert result["attempts"] == []


# --------------------------------------------------------------------------
# 3. PostgreSQL réel
# --------------------------------------------------------------------------

def _insert_row(conn, table, values) -> None:
    columns, params, sql_values = [], {}, []
    for key, val in values.items():
        columns.append(key)
        if isinstance(val, sa.sql.elements.TextClause):
            sql_values.append(val.text)
        elif key == "retention_scopes":
            sql_values.append(f"CAST(:{key} AS jsonb)")
            params[key] = val if isinstance(val, str) or val is None else json.dumps(val)
        else:
            sql_values.append(f":{key}")
            params[key] = val
    conn.execute(sa.text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(sql_values)})"), params)


def _campaign(conn, **overrides) -> dict:
    values = {"id": uuid.uuid4(), "code": f"wave-0-{uuid.uuid4().hex[:8]}", "name": "Vague 0",
              "purpose": "Mesurer l'usage des surfaces Oryx pendant la Vague 0.", "status": "planned",
              "created_at": BASE_TIME, "activated_at": None, "closed_at": None,
              "close_by_at": BASE_TIME + timedelta(days=30), "retention_days": 90,
              "retention_policy_version": "v0-retention-v1", "measurement_policy_version": "v0-measurement-v1"}
    values.update(overrides)
    _insert_row(conn, CAMPAIGNS, values)
    return values


def _participant(conn, campaign_id, **overrides) -> dict:
    values = {"campaign_id": campaign_id, "user_id": "legacy-anon-1", "window_start_at": BASE_TIME,
              "window_end_at": BASE_TIME + timedelta(days=14), "participation_status": "enrolled",
              "enrolled_at": BASE_TIME, "authorization_recorded_at": None, "notice_version": None}
    values.update(overrides)
    _insert_row(conn, PARTICIPANTS, values)
    return values


def _scopes(campaign_id=None, expires_at="2027-03-01T09:00:00Z") -> dict:
    return {"version": "retention-scopes-v1",
            "scopes": [{"kind": "campaign", "campaign_id": str(campaign_id or uuid.uuid4()),
                        "policy_version": "v0-retention-v1", "expires_at": expires_at}]}


def _event(conn, **overrides) -> dict:
    values = {"id": uuid.uuid4(), "user_id": "legacy-anon-1", "visit_id": uuid.uuid4(),
              "idempotency_key": uuid.uuid4(), "event_type": "education_used", "surface": "education",
              "executed_route": None, "source": "server_visit", "source_operation_kind": None,
              "source_operation_id": None, "outcome": "succeeded", "delivery_status": "not_applicable",
              "occurred_at": BASE_TIME, "recorded_at": BASE_TIME, "updated_at": BASE_TIME,
              "resolved_at": None, "delivered_at": None, "retention_scopes": _scopes(),
              "next_scope_expires_at": BASE_TIME + timedelta(days=90),
              "purge_after_at": BASE_TIME + timedelta(days=90), "schema_version": "product-activity-event-v1"}
    values.update(overrides)
    _insert_row(conn, EVENTS, values)
    return values


def _seed_0021_history(engine) -> None:
    """Données historiques du schéma 0021 : utilisateurs, portefeuille,
    thèses, faits, session d'analyse, événement cognitif (0015), plus une
    conversation et une réservation Décrypter (frontière F2, 0021)."""
    _seed_historical(engine)
    with engine.begin() as conn:
        _seed_conversation(conn, "conv-hist", "legacy-anon-1")
        _insert_reservation(conn, conversation_key="conv-hist", state="blocked", blocked_reason="legacy_collision",
                            lease_epoch=0, attempts_used=0, lease_token_hash=None, lease_expires_at=None)


def _extensions(engine) -> list:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT extname FROM pg_extension ORDER BY 1")).scalars().all()


def _upgrade_head(pg_url, pg_engine):
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0022)  # V0-T08 Lot 2B-1 : 0023 au-dessus, testée à part
    _seed_historical(pg_engine)


def test_pg_upgrade_0021_to_0022_keeps_every_row_then_exact_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR9B1_0021)
    _seed_0021_history(pg_engine)
    tables = _tables(pg_engine)
    assert not tables & ANALYTICS
    assert PEDAGOGICAL_TABLES | PORTFOLIO_TABLES | F2_TABLES <= tables
    schema_0021, catalog_0021 = _snapshot(pg_engine, tables), _catalog(pg_engine, tables)
    global_0021, extensions_0021 = _global_catalog(pg_engine), _extensions(pg_engine)
    data_0021 = _data(pg_engine, tables - {"alembic_version"})
    assert data_0021["users"] and data_0021["portfolio_positions"] and data_0021["decryptage_turn_reservations"]
    assert data_0021["cognitive_events"]
    assert _compare_metadata(pg_engine) == []  # écarts attendus (trois tables et leurs index) retirés exactement

    _run_alembic(pg_url, "upgrade", V0T08_0022)
    assert _version(pg_engine) == V0T08_0022 and _compare_metadata(pg_engine) == []
    assert _tables(pg_engine) == tables | ANALYTICS
    # Aucun backfill, aucune réparation, aucune ligne créée (ni utilisateur,
    # ni campagne, ni participant, ni événement).
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0021
    assert _data(pg_engine, ANALYTICS) == {t: [] for t in sorted(ANALYTICS)}
    # Aucune table existante modifiée : pédagogiques, portefeuille, F2 et toutes les autres.
    others = tables - {"alembic_version"}
    assert _snapshot(pg_engine, others) == {t: schema_0021[t] for t in sorted(others)}
    assert _catalog(pg_engine, others) == {t: catalog_0021[t] for t in sorted(others)}
    for family in (PEDAGOGICAL_TABLES, PORTFOLIO_TABLES, F2_TABLES):
        assert _catalog(pg_engine, family) == {t: catalog_0021[t] for t in sorted(family)}
    assert _global_catalog(pg_engine) == global_0021  # aucun trigger, fonction, séquence ni ENUM
    assert _extensions(pg_engine) == extensions_0021 == ["plpgsql"]

    _run_alembic(pg_url, "downgrade", PR9B1_0021)
    assert _version(pg_engine) == PR9B1_0021
    assert _tables(pg_engine) == tables
    assert _snapshot(pg_engine, tables) == schema_0021 and _catalog(pg_engine, tables) == catalog_0021
    assert _global_catalog(pg_engine) == global_0021 and _extensions(pg_engine) == extensions_0021
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0021
    _run_alembic(pg_url, "upgrade", V0T08_0022)  # V0-T08 Lot 2B-1 : 0023 au-dessus, testée à part
    assert _version(pg_engine) == V0T08_0022 and _compare_metadata(pg_engine) == []


def test_pg_catalog_is_exact(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0022)  # V0-T08 Lot 2B-1 : 0023 au-dessus, testée à part
    m = _m0022()
    with pg_engine.connect() as conn:
        for table, columns in COLUMNS.items():
            rows = conn.execute(sa.text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, "
                "pg_get_expr(d.adbin, d.adrelid), a.attidentity::text, a.attgenerated::text FROM pg_attribute a "
                "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = CAST(:t AS regclass) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"),
                {"t": table}).all()
            assert [tuple(r[:3]) for r in rows] == columns, table
            assert all(r[3] is None and r[4] == "" and r[5] == "" for r in rows), table  # aucun default
        users_id = conn.execute(sa.text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'users'::regclass AND attname = 'id'")).scalar_one()
        assert users_id == "character varying"
        constraints = {}
        for name, table, contype, definition, deferrable, deltype, updtype in conn.execute(sa.text(
                "SELECT conname, conrelid::regclass::text, contype, pg_get_constraintdef(oid), condeferrable, "
                "confdeltype, confupdtype FROM pg_constraint WHERE conrelid = ANY(CAST(:t AS regclass[])) "
                "AND contype <> 'n'"), {"t": sorted(ANALYTICS)}):
            assert not deferrable, name
            if contype == "f":
                assert (deltype, updtype) == ("a", "a"), name  # NO ACTION, jamais CASCADE
            constraints[name] = (table, contype, definition)
        expected_checks = {name: table for table, checks in ((CAMPAIGNS, m.CAMPAIGN_CHECKS),
                                                             (PARTICIPANTS, m.PARTICIPANT_CHECKS),
                                                             (EVENTS, m.EVENT_CHECKS)) for name, _ in checks}
        assert {n: t for n, (t, c, _) in constraints.items() if c == "c"} == expected_checks
        assert {n: (t, d) for n, (t, c, d) in constraints.items() if c != "c"} == {
            "product_campaigns_pkey": (CAMPAIGNS, "PRIMARY KEY (id)"),
            "uq_product_campaigns_code": (CAMPAIGNS, "UNIQUE (code)"),
            "product_campaign_participants_pkey": (PARTICIPANTS, "PRIMARY KEY (campaign_id, user_id)"),
            "product_campaign_participants_campaign_id_fkey": (
                PARTICIPANTS, "FOREIGN KEY (campaign_id) REFERENCES product_campaigns(id)"),
            "product_campaign_participants_user_id_fkey": (PARTICIPANTS, "FOREIGN KEY (user_id) REFERENCES users(id)"),
            "product_activity_events_pkey": (EVENTS, "PRIMARY KEY (id)"),
            "product_activity_events_user_id_fkey": (EVENTS, "FOREIGN KEY (user_id) REFERENCES users(id)"),
            "uq_product_activity_events_user_idempotency_key": (EVENTS, "UNIQUE (user_id, idempotency_key)"),
        }
        assert all(len(name) <= 63 for name in constraints)
        indexdefs = dict(conn.execute(sa.text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND tablename = ANY(:t)"), {"t": sorted(ANALYTICS)}).all())
        assert set(indexdefs) == set(INDEXDEFS) | CONSTRAINT_INDEXES
        assert {name: indexdefs[name] for name in INDEXDEFS} == INDEXDEFS
        triggers = conn.execute(sa.text(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = ANY(CAST(:t AS regclass[]))"),
            {"t": sorted(ANALYTICS)}).scalar_one()
        assert triggers == 0


CHECK_VIOLATIONS = [
    # product_campaigns
    ("campaign", "ck_product_campaigns_status", {"status": "archived"}),
    ("campaign", "ck_product_campaigns_status", {"status": "ACTIVE"}),
    ("campaign", "ck_product_campaigns_retention_days", {"retention_days": 0}),
    ("campaign", "ck_product_campaigns_retention_days", {"retention_days": -90}),
    ("campaign", "ck_product_campaigns_close_by_at", {"close_by_at": BASE_TIME - timedelta(seconds=1)}),
    ("campaign", "ck_product_campaigns_activated_at", {"activated_at": BASE_TIME - timedelta(seconds=1)}),
    ("campaign", "ck_product_campaigns_closed_at", {"status": "active", "activated_at": BASE_TIME + timedelta(days=2),
                                                    "closed_at": BASE_TIME + timedelta(days=1)}),
    ("campaign", "ck_product_campaigns_closed_at", {"status": "closed", "activated_at": BASE_TIME,
                                                    "closed_at": BASE_TIME + timedelta(days=31)}),
    ("campaign", "ck_product_campaigns_closed", {"status": "closed", "activated_at": BASE_TIME}),
    ("campaign", "ck_product_campaigns_closed", {"status": "closed", "closed_at": BASE_TIME + timedelta(days=1)}),
    # product_campaign_participants
    ("participant", "ck_product_campaign_participants_participation_status", {"participation_status": "excluded"}),
    ("participant", "ck_product_campaign_participants_window", {"window_end_at": BASE_TIME}),
    ("participant", "ck_product_campaign_participants_window", {"window_end_at": BASE_TIME - timedelta(days=1)}),
    # product_activity_events
    ("event", "ck_product_activity_events_event_type", {"event_type": "page_viewed"}),
    ("event", "ck_product_activity_events_event_type", {"event_type": "Session_Started"}),
    ("event", "ck_product_activity_events_source", {"source": "client"}),
    ("event", "ck_product_activity_events_outcome", {"outcome": "unknown"}),
    ("event", "ck_product_activity_events_delivery_status", {"delivery_status": "unknown"}),
    ("event", "ck_product_activity_events_delivery_status", {"delivery_status": "delivered"}),
    ("event", "ck_product_activity_events_source_operation", {"source_operation_kind": "decryptage_turn"}),
    ("event", "ck_product_activity_events_source_operation", {"source_operation_id": uuid.uuid4()}),
    ("event", "ck_product_activity_events_timestamps", {"recorded_at": BASE_TIME - timedelta(seconds=1)}),
    ("event", "ck_product_activity_events_timestamps", {"recorded_at": BASE_TIME + timedelta(minutes=1)}),
    ("event", "ck_product_activity_events_timestamps", {"resolved_at": BASE_TIME - timedelta(seconds=1)}),
    ("event", "ck_product_activity_events_timestamps", {"delivered_at": BASE_TIME - timedelta(seconds=1)}),
    ("event", "ck_product_activity_events_retention_window",
     {"next_scope_expires_at": BASE_TIME + timedelta(days=91)}),
    ("event", "ck_product_activity_events_retention_scopes", {"retention_scopes": "[]"}),
    ("event", "ck_product_activity_events_retention_scopes", {"retention_scopes": "\"retention-scopes-v1\""}),
    ("event", "ck_product_activity_events_retention_scopes", {"retention_scopes": "null"}),
    ("event", "ck_product_activity_events_retention_scopes", {"retention_scopes": "{}"}),
    ("event", "ck_product_activity_events_retention_scopes", {"retention_scopes": {"scopes": [{}]}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": "retention-scopes-v2", "scopes": [{}]}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": 1, "scopes": [{}]}}),
    # Égalité JSONB exacte : la version doit être LA chaîne JSON attendue,
    # jamais un tableau, un objet ou un autre type qui la contiendrait.
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": ["retention-scopes-v1"], "scopes": [{}]}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": ["retention-scopes-v1", "retention-scopes-v2"], "scopes": [{}]}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": {"retention-scopes-v1": True}, "scopes": [{}]}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": None, "scopes": [{}]}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": True, "scopes": [{}]}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": " retention-scopes-v1", "scopes": [{}]}}),
    ("event", "ck_product_activity_events_retention_scopes", {"retention_scopes": {"version": "retention-scopes-v1"}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": "retention-scopes-v1", "scopes": []}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": "retention-scopes-v1", "scopes": {"kind": "campaign"}}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": "retention-scopes-v1", "scopes": "campaign"}}),
    ("event", "ck_product_activity_events_retention_scopes",
     {"retention_scopes": {"version": "retention-scopes-v1", "scopes": [{}], "question": "texte interdit"}}),
]


def _insert_kind(conn, kind, overrides):
    if kind == "campaign":
        return _campaign(conn, **overrides)
    if kind == "participant":
        return _participant(conn, _campaign(conn)["id"], **overrides)
    return _event(conn, **overrides)


@pytest.mark.parametrize("kind,constraint,overrides", CHECK_VIOLATIONS)
def test_pg_every_check_is_exercised(pg_url, pg_engine, kind, constraint, overrides):  # noqa: F811
    _upgrade_head(pg_url, pg_engine)
    with pg_engine.begin() as conn:
        assert_disposable_connection(conn)
        _rejects(conn, constraint, lambda: _insert_kind(conn, kind, dict(overrides)))
        assert conn.execute(sa.text(f"SELECT count(*) FROM {EVENTS}")).scalar_one() == 0


def test_every_check_has_a_violation_case():
    m = _m0022()
    names = {name for checks in (m.CAMPAIGN_CHECKS, m.PARTICIPANT_CHECKS, m.EVENT_CHECKS) for name, _ in checks}
    assert {constraint for _, constraint, _ in CHECK_VIOLATIONS} == names


@pytest.mark.parametrize("table,column", [(table, name) for table, columns in COLUMNS.items()
                                          for name, _, nullable in columns if not nullable])
def test_pg_not_null_columns_are_enforced(pg_url, pg_engine, table, column):  # noqa: F811
    _upgrade_head(pg_url, pg_engine)
    with pg_engine.begin() as conn:
        assert_disposable_connection(conn)
        kind = {CAMPAIGNS: "campaign", PARTICIPANTS: "participant", EVENTS: "event"}[table]
        campaign_id = _campaign(conn)["id"] if kind == "participant" else None
        overrides = {column: None}
        with pytest.raises(sa.exc.IntegrityError, match=f'null value in column "{column}"'):
            with conn.begin_nested():
                if kind == "campaign":
                    _campaign(conn, **overrides)
                elif kind == "participant":
                    _participant(conn, **{"campaign_id": campaign_id, **overrides})
                else:
                    _event(conn, **overrides)


def test_pg_lengths_are_enforced(pg_url, pg_engine):  # noqa: F811
    _upgrade_head(pg_url, pg_engine)
    with pg_engine.begin() as conn:
        assert_disposable_connection(conn)
        for insert in (lambda: _campaign(conn, code="c" * 65), lambda: _campaign(conn, name="n" * 161),
                       lambda: _event(conn, surface="s" * 33), lambda: _event(conn, schema_version="v" * 65),
                       lambda: _event(conn, source_operation_kind="k" * 65, source_operation_id=uuid.uuid4())):
            with pytest.raises(sa.exc.DataError, match="value too long"):
                with conn.begin_nested():
                    insert()
        _campaign(conn, code="c" * 64, name="n" * 160)  # bornes exactes admises


def test_pg_campaigns_primary_key_unique_code_and_legitimate_shapes(pg_url, pg_engine):  # noqa: F811
    _upgrade_head(pg_url, pg_engine)
    with pg_engine.begin() as conn:
        assert_disposable_connection(conn)
        planned = _campaign(conn, code="wave-0")
        _rejects(conn, "uq_product_campaigns_code", lambda: _campaign(conn, code="wave-0"))
        _rejects(conn, "product_campaigns_pkey", lambda: _campaign(conn, id=planned["id"]))
        _campaign(conn, status="active", activated_at=BASE_TIME)
        _campaign(conn, status="closed", activated_at=BASE_TIME, closed_at=BASE_TIME + timedelta(days=30))
        _campaign(conn, status="cancelled")
        _campaign(conn, status="cancelled", activated_at=BASE_TIME, closed_at=BASE_TIME + timedelta(days=1))
        _campaign(conn, close_by_at=BASE_TIME, retention_days=1)  # bornes admises
        # Aucune durée de conservation implicite : la colonne n'a pas de défaut.
        _rejects(conn, "retention_days",
                 f"INSERT INTO {CAMPAIGNS} (id, code, name, purpose, status, created_at, close_by_at, "
                 "retention_policy_version, measurement_policy_version) VALUES (:id, 'no-default', 'n', 'p', "
                 "'planned', now(), now(), 'v', 'v')", id=uuid.uuid4())


def test_pg_participants_identity_and_foreign_keys(pg_url, pg_engine):  # noqa: F811
    _upgrade_head(pg_url, pg_engine)
    with pg_engine.begin() as conn:
        assert_disposable_connection(conn)
        first, second = _campaign(conn)["id"], _campaign(conn)["id"]
        _participant(conn, first)
        # Une personne, plusieurs campagnes : admis ; deux fois la même campagne : refusé.
        _participant(conn, second, window_start_at=BASE_TIME + timedelta(days=20),
                     window_end_at=BASE_TIME + timedelta(days=34), authorization_recorded_at=BASE_TIME,
                     notice_version="v0-notice-v1")
        _rejects(conn, "product_campaign_participants_pkey", lambda: _participant(conn, first))
        _rejects(conn, "product_campaign_participants_pkey",
                 lambda: _participant(conn, first, participation_status="withdrawn"))
        # Fenêtres semi-ouvertes adjacentes, autres statuts : admis.
        _participant(conn, first, user_id="user_7f3a", window_start_at=BASE_TIME + timedelta(days=14),
                     window_end_at=BASE_TIME + timedelta(days=28), participation_status="completed")
        _participant(conn, first, user_id="legacy-anon-2", participation_status="withdrawn")
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('v0-participant-only', 'debutant')"))
        _participant(conn, second, user_id="v0-participant-only")
        _rejects(conn, "product_campaign_participants_campaign_id_fkey", lambda: _participant(conn, uuid.uuid4()))
        _rejects(conn, "product_campaign_participants_user_id_fkey",
                 lambda: _participant(conn, first, user_id="ghost-user"))
        # FK NO ACTION : ni la campagne ni l'utilisateur ne peuvent disparaître sous un participant.
        _rejects(conn, "product_campaign_participants_campaign_id_fkey",
                 f"DELETE FROM {CAMPAIGNS} WHERE id = :id", id=first)
        _rejects(conn, "product_campaign_participants_user_id_fkey",
                 "DELETE FROM users WHERE id = 'v0-participant-only'")
        assert conn.execute(sa.text(f"SELECT count(*) FROM {PARTICIPANTS}")).scalar_one() == 5


def test_pg_events_identity_uniqueness_and_legitimate_shapes(pg_url, pg_engine):  # noqa: F811
    _upgrade_head(pg_url, pg_engine)
    with pg_engine.begin() as conn:
        assert_disposable_connection(conn)
        campaign = _campaign(conn, status="active", activated_at=BASE_TIME)
        visit = uuid.uuid4()
        scopes = _scopes(campaign["id"])
        start = _event(conn, visit_id=visit, event_type="session_started", surface="app", retention_scopes=scopes)
        # Plusieurs événements légitimes de types différents dans la même visite.
        for event_type in EVENT_TYPES[1:]:
            _event(conn, visit_id=visit, event_type=event_type, surface=event_type.split("_")[0],
                   retention_scopes=scopes)
        _event(conn, visit_id=visit, event_type="view_opened", surface="portfolio", executed_route="/portfolio")
        # Un deuxième session_started dans la même visite : refusé (index unique partiel).
        _rejects(conn, "uq_product_activity_events_visit_session_start",
                 lambda: _event(conn, visit_id=visit, event_type="session_started", surface="app"))
        _rejects(conn, "uq_product_activity_events_visit_session_start",
                 lambda: _event(conn, visit_id=visit, event_type="session_started", surface="app",
                                source="client_signal", occurred_at=BASE_TIME + timedelta(minutes=5),
                                recorded_at=BASE_TIME + timedelta(minutes=5),
                                updated_at=BASE_TIME + timedelta(minutes=5)))
        # Autre visite du même utilisateur, même visite d'un autre utilisateur : admis.
        _event(conn, event_type="session_started", surface="app")
        _event(conn, user_id="user_7f3a", visit_id=visit, event_type="session_started", surface="app")
        # Clé d'idempotence : unique par utilisateur seulement.
        _rejects(conn, "uq_product_activity_events_user_idempotency_key",
                 lambda: _event(conn, idempotency_key=start["idempotency_key"]))
        _event(conn, user_id="user_7f3a", idempotency_key=start["idempotency_key"])
        _rejects(conn, "product_activity_events_pkey", lambda: _event(conn, id=start["id"]))
        # Identité canonique d'une opération métier, scindée par propriétaire
        # (contrat Portfolio (user_id, operation_type, operation_id)) : une
        # seule ligne par (user_id, source_operation_kind, source_operation_id).
        operation = uuid.uuid4()
        _event(conn, event_type="decrypter_used", surface="decrypter", source="server_business",
               source_operation_kind="decryptage_turn", source_operation_id=operation, outcome="in_progress",
               delivery_status="pending")
        # Même utilisateur + même opération canonique : refus, quels que soient
        # le type d'événement, la visite ou le résultat.
        for overrides in ({}, {"event_type": "portfolio_used", "surface": "portfolio"}, {"visit_id": uuid.uuid4()},
                          {"outcome": "succeeded", "delivery_status": "confirmed"}):
            values = {"event_type": "decrypter_used", "surface": "decrypter", "source": "server_business",
                      "source_operation_kind": "decryptage_turn", "source_operation_id": operation, **overrides}
            _rejects(conn, "uq_product_activity_events_source_operation", lambda v=values: _event(conn, **v))
        # Utilisateurs différents + même type et même identifiant d'opération : admis.
        _event(conn, user_id="user_7f3a", event_type="decrypter_used", surface="decrypter", source="server_business",
               source_operation_kind="decryptage_turn", source_operation_id=operation)
        _event(conn, user_id="legacy-anon-2", event_type="decrypter_used", surface="decrypter",
               source="server_business", source_operation_kind="decryptage_turn", source_operation_id=operation)
        # ... et chacun reste unique pour son propriétaire.
        _rejects(conn, "uq_product_activity_events_source_operation",
                 lambda: _event(conn, user_id="user_7f3a", event_type="decrypter_used", surface="decrypter",
                                source="server_business", source_operation_kind="decryptage_turn",
                                source_operation_id=operation))
        # Même utilisateur + type d'opération différent (même identifiant) : admis.
        _event(conn, event_type="portfolio_used", surface="portfolio", source="server_business",
               source_operation_kind="portfolio_write", source_operation_id=operation)
        _event(conn, event_type="portfolio_used", surface="portfolio", source="server_business",
               source_operation_kind="portfolio_write", source_operation_id=uuid.uuid4())
        assert conn.execute(sa.text(
            f"SELECT count(*) FROM {EVENTS} WHERE source_operation_id = :o"), {"o": operation}).scalar_one() == 4
        # Sans opération : aucune unicité canonique.
        _event(conn, event_type="profile_viewed", surface="profile")
        _event(conn, event_type="profile_viewed", surface="profile")
        # Toutes les valeurs fermées admises.
        for source in SOURCES:
            _event(conn, source=source)
        for outcome in OUTCOMES:
            _event(conn, outcome=outcome)
        for delivery_status in DELIVERY_STATUSES:
            _event(conn, delivery_status=delivery_status)
        # Enregistrement retardé (reprise) : résolu et livré avant l'enregistrement, admis.
        _event(conn, event_type="coach_used", surface="coach", occurred_at=BASE_TIME - timedelta(days=3),
               resolved_at=BASE_TIME - timedelta(days=3) + timedelta(seconds=40),
               delivered_at=BASE_TIME - timedelta(days=2), recorded_at=BASE_TIME,
               updated_at=BASE_TIME + timedelta(hours=1), outcome="succeeded", delivery_status="confirmed")
        _event(conn, outcome="indeterminate", delivery_status="unconfirmed", resolved_at=BASE_TIME,
               updated_at=BASE_TIME + timedelta(minutes=30))
        # Plusieurs finalités, échéances égales ou ordonnées : admis.
        two_scopes = {"version": "retention-scopes-v1", "scopes": [
            _scopes(campaign["id"], "2027-03-01T09:00:00Z")["scopes"][0],
            _scopes(uuid.uuid4(), "2027-06-01T09:00:00Z")["scopes"][0]]}
        _event(conn, retention_scopes=two_scopes, next_scope_expires_at=BASE_TIME + timedelta(days=90),
               purge_after_at=BASE_TIME + timedelta(days=182))
        # Utilisateur inexistant : refusé ; utilisateur référencé : indélébile.
        _rejects(conn, "product_activity_events_user_id_fkey", lambda: _event(conn, user_id="ghost-user"))
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('v0-event-only', 'debutant')"))
        _event(conn, user_id="v0-event-only")
        _rejects(conn, "product_activity_events_user_id_fkey", "DELETE FROM users WHERE id = 'v0-event-only'")
        stored = conn.execute(sa.text(f"SELECT retention_scopes FROM {EVENTS} WHERE id = :id"),
                              {"id": start["id"]}).scalar_one()
        assert stored == scopes


BLOCKING_CASES = ["events", "participants", "campaigns"]


def _seed_blocking(conn, case) -> str:
    if case == "events":
        _event(conn)  # sans campagne ni participant
        return EVENTS
    campaign = _campaign(conn)
    if case == "participants":
        _participant(conn, campaign["id"])
        return PARTICIPANTS
    return CAMPAIGNS


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("case", BLOCKING_CASES)
def test_pg_downgrade_is_refused_when_any_analytics_table_has_data(pg_url, pg_engine, case, offline):  # noqa: F811
    _upgrade_head(pg_url, pg_engine)
    with pg_engine.begin() as conn:
        table = _seed_blocking(conn, case)
    tables = _tables(pg_engine)
    before = (_snapshot(pg_engine, tables), _catalog(pg_engine, tables), _data(pg_engine, tables - {"alembic_version"}))
    expected = f"downgrade de 0022_product_analytics_v1 refusé, la table {table} contient des données"
    if offline:
        statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{V0T08_0022}:{PR9B1_0021}", "--sql").stdout)
        lock = next(s for s in statements if s.startswith("LOCK TABLE"))
        guard = next(s for s in statements if s.startswith("DO $$"))
        with pg_engine.connect() as conn:
            with pytest.raises(sa.exc.DBAPIError, match=expected):
                with conn.begin():
                    conn.exec_driver_sql(lock)
                    conn.exec_driver_sql(guard)
    else:
        with pytest.raises(Exception) as failure:
            _run_alembic(pg_url, "downgrade", PR9B1_0021)
        assert expected in str(getattr(failure.value, "stderr", failure.value))
    assert _version(pg_engine) == V0T08_0022
    assert (_snapshot(pg_engine, tables), _catalog(pg_engine, tables),
            _data(pg_engine, tables - {"alembic_version"})) == before


def test_pg_offline_guard_passes_when_the_three_tables_are_empty(pg_url, pg_engine):  # noqa: F811
    _upgrade_head(pg_url, pg_engine)
    statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{V0T08_0022}:{PR9B1_0021}", "--sql").stdout)
    with pg_engine.begin() as conn:
        for statement in statements[:statements.index(next(s for s in statements if s.startswith("DO $$"))) + 1]:
            conn.exec_driver_sql(statement)  # délais, verrou et garde : aucune exception


def test_pg_offline_upgrade_then_offline_downgrade_round_trip(pg_url, pg_engine):  # noqa: F811
    """Le SQL offline de 0022, exécuté tel quel sur une base en 0021, produit
    le même schéma que la montée en ligne ; le SQL offline du downgrade le
    retire exactement."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", PR9B1_0021)
    _seed_0021_history(pg_engine)
    tables = _tables(pg_engine)
    schema_0021, data_0021 = _catalog(pg_engine, tables), _data(pg_engine, tables - {"alembic_version"})
    up = _run_alembic(OFFLINE, "upgrade", f"{PR9B1_0021}:{V0T08_0022}", "--sql").stdout
    down = _run_alembic(OFFLINE, "downgrade", f"{V0T08_0022}:{PR9B1_0021}", "--sql").stdout
    with pg_engine.begin() as conn:
        for statement in _statements(up):
            conn.exec_driver_sql(statement)
    assert _version(pg_engine) == V0T08_0022 and _compare_metadata(pg_engine) == []
    offline_catalog = _catalog(pg_engine, tables | ANALYTICS)
    with pg_engine.begin() as conn:
        for statement in _statements(down):
            conn.exec_driver_sql(statement)
    assert _version(pg_engine) == PR9B1_0021
    assert _catalog(pg_engine, tables) == schema_0021 and _tables(pg_engine) == tables
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0021
    _run_alembic(pg_url, "upgrade", V0T08_0022)
    assert _catalog(pg_engine, tables | ANALYTICS) == offline_catalog
