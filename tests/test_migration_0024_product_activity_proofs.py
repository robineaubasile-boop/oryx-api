"""Tests de V0-T08 Lot 2B-2, bloc A : migration 0024_product_activity_proofs
(registre provisoire des preuves et attributions définitives, DORMANTS) et
modèles correspondants.

1. Sans base : maillon 0023 -> 0024 (tête unique), identifiant <= 32
   caractères, migrations 0022 et 0023 inchangées (sha256), SQL offline
   (upgrade : trois ADD CONSTRAINT UNIQUE, deux CREATE TABLE et leurs index,
   rien d'autre, aucune table F2 ; downgrade : délais, verrou déterministe,
   garde DO évaluée à l'exécution, retrait sans CASCADE), modèles alignés
   sur la migration (colonnes, types, nullabilité, CHECK, UNIQUE, FK,
   index, aucune valeur par défaut), vocabulaires fermés (natures F2 de la
   PR #250 reprises à l'identique), aucun champ personnel ni de contenu.
2. PostgreSQL réel (base jetable) :
   - identités et visites : chaque rattachement croisé (autre utilisateur,
     autre campagne, autre visite, interaction 'close', inconnue, visite
     inconnue, utilisateur hors participation) refusé PAR POSTGRESQL, sur
     le registre ET sur les attributions ; formes légitimes (deux
     appareils, ancienne interaction rejouée dans SA visite) acceptées ;
   - opérations et événements : unicité canonique, natures Portfolio
     distinctes, concordance exacte événement / attribution (propriétaire,
     visite, type, surface, opération), une attribution par événement, une
     preuve 'recorded' seulement vers SA propre attribution ;
   - états et conservation : formes de lignes, chronologie, motifs fermés,
     purge = résolution + 168 heures absolues (y compris autour d'un
     changement d'heure), suppression de la preuve sans perte de
     l'attribution, attribution protégée tant qu'une dépendance existe ;
   - migration et concurrence : montée 0023 -> 0024 (base vide, puis avec
     données historiques conservées à l'identique, aucun backfill),
     catalogue exact, aucune table F2 ni aucun objet imprévu, downgrade
     exact si les deux tables sont vides, REFUSÉ (en ligne et par la garde
     offline exécutée) sinon, aller-retour offline, insertions concurrentes
     de la même identité canonique, rollback d'une insertion invalide.
"""
import ast
import hashlib
import importlib.util
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

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
    V0T08_0022_FILE,
    V0T08_0023,
    V0T08_0023_FILE,
    V0T08_0024,
    V0T08_0024_EXISTING_UNIQUES,
    V0T08_0024_FILE,
    V0T08_0024_INDEXES,
    V0T08_0024_TABLES,
    V0T08_TABLES,
    _compare_metadata,
    _data,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import _catalog, _global_catalog
from tests.test_migration_0016_portfolio_idempotency import _rejects
from tests.test_migration_0022_product_analytics_v1 import (
    BASE_TIME,
    FORBIDDEN_FIELD_MARKERS,
    GUARDS,
    OFFLINE,
    _campaign,
    _event,
    _participant,
)
from tests.test_migration_0023_analytics_cohort_sessions import _seed_0022
from tests.test_product_analytics_dormancy import MIGRATION_0022_SHA256, MIGRATION_0023_SHA256

MIGRATION = REPO_ROOT / "alembic" / "versions" / V0T08_0024_FILE
ATTRIBUTIONS, REGISTRY = V0T08_0024_TABLES
NEW = set(V0T08_0024_TABLES)
PARTICIPANTS = "product_campaign_participants"
EVENTS = "product_activity_events"
SESSIONS = "product_activity_sessions"
INTERACTIONS = "product_activity_session_interactions"
ALTERED = {EVENTS, SESSIONS, INTERACTIONS}
TS = "timestamp with time zone"
IDENTITY = [("campaign_id", "uuid", False), ("user_id", "character varying", False), ("visit_id", "uuid", False),
            ("interaction_key", "uuid", False), ("interaction_kind", "character varying(16)", False),
            ("event_type", "character varying(32)", False), ("surface", "character varying(32)", False),
            ("source_operation_kind", "character varying(64)", False), ("source_operation_id", "uuid", False)]
COLUMNS = {
    ATTRIBUTIONS: [("event_id", "uuid", False), *IDENTITY, ("certified_at", TS, False)],
    REGISTRY: [("id", "uuid", False), *IDENTITY, ("status", "character varying(24)", False),
               ("created_at", TS, False), ("certified_at", TS, True), ("resolved_at", TS, True),
               ("recorded_event_id", "uuid", True), ("resolution_reason", "character varying(64)", True),
               ("purge_after_at", TS, True)],
}
STATUSES = ["pending", "certified", "recorded", "rejected", "indeterminate"]
REASONS = ["no_new_effect", "source_unverified", "origin_unverified", "owner_conflict", "visit_conflict",
           "operation_conflict", "window_expired", "finalization_expired", "not_authorized", "technical_uncertainty"]
PORTFOLIO_KINDS = ["portfolio_position_create", "portfolio_position_update", "portfolio_position_delete"]
OPERATIONS = [("education_used", "education", "assistant_delivery"), ("coach_used", "coach", "assistant_delivery"),
              ("decrypter_used", "decrypter", "decryptage_delivery_ack")] + [
    ("portfolio_used", "portfolio", kind) for kind in PORTFOLIO_KINDS]
F2_TABLES = ("users", "invitations", "auth_operations", "auth_identities", "auth_preauth_candidates",
             "auth_otp_requests", "auth_otp_verification_attempts", "auth_abuse_subjects", "auth_abuse_events",
             "auth_abuse_key_state", "oryx_sessions", "oryx_session_generations", "auth_operation_results")
F2_FKEYS = {"registry": REGISTRY + "_{}_fkey", "attribution": ATTRIBUTIONS + "_{}_fkey"}


def _m0024():
    spec = importlib.util.spec_from_file_location("m0024", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_links_0024_to_0023_and_is_the_single_head():
    script = _script_directory()
    assert script.get_heads() == [V0T08_0024]
    revision = script.get_revision(V0T08_0024)
    assert revision.down_revision == V0T08_0023
    assert not revision.branch_labels and not revision.dependencies
    assert V0T08_0024 == "0024_product_activity_proofs" and len(V0T08_0024) <= 32
    assert V0T08_0024_FILE == f"{V0T08_0024}.py"
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(files) == 24 and files[-1] == V0T08_0024_FILE and files[-2] == V0T08_0023_FILE
    assert files[-3] == V0T08_0022_FILE
    m = _m0024()
    assert (m.revision, m.down_revision, m.branch_labels, m.depends_on) == (V0T08_0024, V0T08_0023, None, None)


def test_migrations_0022_and_0023_are_unchanged():
    """Empreintes des migrations 0022 et 0023 telles que fusionnées sur main
    (PR #247 et PR #249) ; 0001 à 0021 sont couvertes par la suite 0022."""
    versions = REPO_ROOT / "alembic" / "versions"
    assert hashlib.sha256((versions / V0T08_0022_FILE).read_bytes()).hexdigest() == MIGRATION_0022_SHA256
    assert hashlib.sha256((versions / V0T08_0023_FILE).read_bytes()).hexdigest() == MIGRATION_0023_SHA256


def test_offline_upgrade_adds_only_three_uniques_two_tables_and_their_indexes():
    statements = _statements(_run_alembic(OFFLINE, "upgrade", f"{V0T08_0023}:{V0T08_0024}", "--sql").stdout)
    assert statements[:3] == GUARDS
    m = _m0024()
    assert statements[3:6] == [
        f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE ({', '.join(columns)})"
        for name, table, columns in m.EXISTING_TABLE_UNIQUES]
    creates = [s for s in statements if s.startswith("CREATE TABLE")]
    assert [s.split(" ")[2] for s in creates] == [ATTRIBUTIONS, REGISTRY]
    indexes = [s for s in statements if s.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX"))]
    assert sorted(s.split(" INDEX ")[1].split(" ")[0] for s in indexes) == sorted(V0T08_0024_INDEXES)
    assert all(" UNIQUE INDEX " not in s for s in indexes)
    assert statements[-1] == (f"UPDATE alembic_version SET version_num='{V0T08_0024}' "
                              f"WHERE alembic_version.version_num = '{V0T08_0023}'")
    assert len(statements) == 3 + 3 + len(creates) + len(indexes) + 1
    by_table = dict(zip((ATTRIBUTIONS, REGISTRY), creates))
    for table, checks in ((ATTRIBUTIONS, m.ATTRIBUTION_CHECKS), (REGISTRY, m.REGISTRY_CHECKS)):
        for name, expression in checks:
            assert f"CONSTRAINT {name} CHECK ({expression})" in by_table[table], name
    origin = ("participant_fkey FOREIGN KEY(campaign_id, user_id) REFERENCES product_campaign_participants "
              "(campaign_id, user_id)",
              "visit_fkey FOREIGN KEY(visit_id, campaign_id, user_id) REFERENCES product_activity_sessions "
              "(id, campaign_id, user_id)",
              "interaction_fkey FOREIGN KEY(campaign_id, user_id, interaction_key, visit_id, interaction_kind) "
              "REFERENCES product_activity_session_interactions (campaign_id, user_id, interaction_key, session_id, "
              "interaction_kind)")
    for table in (ATTRIBUTIONS, REGISTRY):
        for fragment in origin:
            assert f"CONSTRAINT {table}_{fragment}" in by_table[table], (table, fragment)
    assert ("CONSTRAINT product_activity_event_attributions_event_fkey FOREIGN KEY(event_id, user_id, visit_id, "
            "event_type, surface, source_operation_kind, source_operation_id) REFERENCES product_activity_events "
            "(id, user_id, visit_id, event_type, surface, source_operation_kind, source_operation_id)"
            ) in by_table[ATTRIBUTIONS]
    assert ("CONSTRAINT product_activity_proof_registry_recorded_event_fkey FOREIGN KEY(recorded_event_id, "
            "campaign_id, user_id, visit_id, interaction_key, interaction_kind, event_type, surface, "
            "source_operation_kind, source_operation_id) REFERENCES product_activity_event_attributions (event_id, "
            "campaign_id, user_id, visit_id, interaction_key, interaction_kind, event_type, surface, "
            "source_operation_kind, source_operation_id)") in by_table[REGISTRY]
    upper = " ".join(statements).upper()
    for forbidden in ("TRIGGER", "FUNCTION", "CREATE TYPE", "ENUM", "EXTENSION", "DROP", "CASCADE", "DEFERRABLE",
                      "INSERT INTO", "DELETE FROM", "DEFAULT", "SEQUENCE", "SERIAL", "ON DELETE", "ON UPDATE",
                      "COMMENT ON", "ALTER COLUMN", "RENAME", "USING", "NOT VALID"):
        assert forbidden not in upper, forbidden
    joined = " ".join(statements)
    for f2_table in F2_TABLES:  # aucune table F2 créée, modifiée ni même référencée
        assert f" {f2_table} " not in joined and f" {f2_table}(" not in joined, f2_table
    assert [s for s in statements if s.upper().startswith("UPDATE")] == [statements[-1]]
    assert [s for s in statements if s.startswith("ALTER TABLE")] == statements[3:6]


def test_offline_downgrade_locks_then_guards_each_table_at_execution_then_drops_without_cascade():
    statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{V0T08_0024}:{V0T08_0023}", "--sql").stdout)
    message = ("V0-T08 Lot 2B-2 : downgrade de 0024_product_activity_proofs refusé, la table {} contient des "
               "données. Aucune donnée supprimée.")
    assert statements == GUARDS + [
        f"LOCK TABLE {PARTICIPANTS}, {EVENTS}, {SESSIONS}, {INTERACTIONS}, {ATTRIBUTIONS}, {REGISTRY} "
        "IN ACCESS EXCLUSIVE MODE",
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {REGISTRY}) THEN RAISE EXCEPTION '{message.format(REGISTRY)}'; "
        f"ELSIF EXISTS (SELECT 1 FROM {ATTRIBUTIONS}) THEN RAISE EXCEPTION '{message.format(ATTRIBUTIONS)}'; "
        "END IF; END $$",
        f"DROP TABLE {REGISTRY}",
        f"DROP TABLE {ATTRIBUTIONS}",
        f"ALTER TABLE {EVENTS} DROP CONSTRAINT uq_product_activity_events_attribution",
        f"ALTER TABLE {INTERACTIONS} DROP CONSTRAINT uq_product_activity_session_interactions_origin",
        f"ALTER TABLE {SESSIONS} DROP CONSTRAINT uq_product_activity_sessions_owner",
        f"UPDATE alembic_version SET version_num='{V0T08_0023}' WHERE alembic_version.version_num = '{V0T08_0024}'",
    ]


def test_models_match_the_migration():
    m = _m0024()
    pg_types = {"UUID": "uuid", "VARCHAR": "character varying", "TIMESTAMP WITH TIME ZONE": TS}
    tables = {name: Base.metadata.tables[name] for name in V0T08_0024_TABLES}
    for table_name, columns in COLUMNS.items():
        described = []
        for column in tables[table_name].c:
            base, _, length = column.type.compile(dialect=postgresql.dialect()).partition("(")
            described.append((column.name, pg_types[base] + (f"({length}" if length else ""), column.nullable))
            assert column.server_default is None and column.default is None, column.name
            assert column.onupdate is None and column.server_onupdate is None, column.name
        assert described == columns, table_name
    # user_id : exactement le type historique de users.id (String, jamais UUID).
    assert type(Base.metadata.tables["users"].c.id.type) is sa.String
    for table in tables.values():
        assert type(table.c.user_id.type) is sa.String and table.c.user_id.type.length is None
    for table_name, expected in ((ATTRIBUTIONS, m.ATTRIBUTION_CHECKS), (REGISTRY, m.REGISTRY_CHECKS)):
        checks = {c.name: str(c.sqltext) for c in tables[table_name].constraints if isinstance(c, sa.CheckConstraint)}
        assert checks == dict(expected), table_name
    # PK : mêmes colonnes ; nom PostgreSQL par défaut <table>_pkey, identique
    # à celui de la migration (convention des modèles, vérifiée au catalogue).
    assert [c.name for c in tables[ATTRIBUTIONS].primary_key.columns] == ["event_id"]
    assert [c.name for c in tables[REGISTRY].primary_key.columns] == ["id"]
    assert (m.ATTRIBUTION_PRIMARY_KEY, m.REGISTRY_PRIMARY_KEY) == (f"{ATTRIBUTIONS}_pkey", f"{REGISTRY}_pkey")
    uniques = {c.name: [col.name for col in c.columns] for t in tables.values() for c in t.constraints
               if isinstance(c, sa.UniqueConstraint)}
    assert uniques == {
        m.ATTRIBUTION_OPERATION_UNIQUE: list(m.OPERATION_COLUMNS),
        m.ATTRIBUTION_PROOF_UNIQUE: ["event_id", *m.PROOF_IDENTITY],
        m.REGISTRY_OPERATION_UNIQUE: list(m.OPERATION_COLUMNS),
        m.REGISTRY_RECORDED_EVENT_UNIQUE: ["recorded_event_id"],
    }
    assert list(m.OPERATION_COLUMNS) == ["user_id", "source_operation_kind", "source_operation_id"]
    fks = {fk.name: ([e.parent.name for e in fk.elements], [e.target_fullname for e in fk.elements],
                     fk.ondelete, fk.onupdate, fk.deferrable)
           for t in tables.values() for fk in t.foreign_key_constraints}
    origin = {
        "participant_fkey": (["campaign_id", "user_id"], [f"{PARTICIPANTS}.campaign_id", f"{PARTICIPANTS}.user_id"]),
        "visit_fkey": (["visit_id", "campaign_id", "user_id"],
                       [f"{SESSIONS}.id", f"{SESSIONS}.campaign_id", f"{SESSIONS}.user_id"]),
        "interaction_fkey": (["campaign_id", "user_id", "interaction_key", "visit_id", "interaction_kind"],
                             [f"{INTERACTIONS}.{c}" for c in ("campaign_id", "user_id", "interaction_key",
                                                               "session_id", "interaction_kind")]),
    }
    expected = {f"{table}_{suffix}": (*columns, None, None, None)
                for table in (ATTRIBUTIONS, REGISTRY) for suffix, columns in origin.items()}
    expected[m.ATTRIBUTION_EVENT_FK] = (
        ["event_id", "user_id", "visit_id", "event_type", "surface", "source_operation_kind", "source_operation_id"],
        [f"{EVENTS}.{c}" for c in ("id", "user_id", "visit_id", "event_type", "surface", "source_operation_kind",
                                    "source_operation_id")], None, None, None)
    expected[m.REGISTRY_RECORDED_EVENT_FK] = (["recorded_event_id", *m.PROOF_IDENTITY],
                                              [f"{ATTRIBUTIONS}.{c}" for c in ("event_id", *m.PROOF_IDENTITY)],
                                              None, None, None)
    assert fks == expected
    # Aucune FK vers une table F2 (users compris : la participation suffit).
    assert not [fk for t in tables.values() for fk in t.foreign_key_constraints
                if any(e.column.table.name in F2_TABLES for e in fk.elements)]
    indexes = {index.name: ([e.name for e in index.expressions], index.unique,
                            None if index.dialect_options["postgresql"]["where"] is None
                            else str(index.dialect_options["postgresql"]["where"]))
               for t in tables.values() for index in t.indexes}
    assert indexes == {
        m.ATTRIBUTION_INTERACTION_INDEX: (["campaign_id", "user_id", "interaction_key"], False, None),
        m.ATTRIBUTION_VISIT_INDEX: (["visit_id"], False, None),
        m.REGISTRY_CAMPAIGN_STATUS_INDEX: (["campaign_id", "status"], False, None),
        m.REGISTRY_INTERACTION_INDEX: (["campaign_id", "user_id", "interaction_key"], False, None),
        m.REGISTRY_VISIT_INDEX: (["visit_id"], False, None),
        m.REGISTRY_PURGE_INDEX: (["purge_after_at"], False, m.REGISTRY_PURGE_WHERE),
    }
    assert set(indexes) == V0T08_0024_INDEXES
    # Les trois UNIQUE ajoutées aux tables existantes : mêmes noms et colonnes
    # dans les modèles, dans la migration et dans l'inventaire partagé.
    existing = {c.name: (t.name, [col.name for col in c.columns]) for t in Base.metadata.tables.values()
                for c in t.constraints if isinstance(c, sa.UniqueConstraint) and c.name in V0T08_0024_EXISTING_UNIQUES}
    assert existing == {name: (table, list(columns)) for name, table, columns in m.EXISTING_TABLE_UNIQUES}
    assert existing == {name: (table, columns) for name, (table, columns, _) in V0T08_0024_EXISTING_UNIQUES.items()}


def test_closed_vocabularies_and_canonical_operations():
    """États, motifs et couples (event_type, surface, nature) fermés ; les
    natures F2 sont exactement celles de core/authz_f2/attestations.py (PR
    #250), lues sans importer la frontière F2 ; Portfolio n'est jamais
    regroupé sous un identifiant unique."""
    m = _m0024()
    assert list(m.PROOF_STATUSES) == STATUSES and list(m.RESOLUTION_REASONS) == REASONS
    assert m.PROOF_INTERACTION_KIND == "activity"
    assert [(t, s, k) for t, s, kinds in m.OPERATION_RULES for k in kinds] == OPERATIONS
    checks = dict(m.REGISTRY_CHECKS)
    assert checks["ck_product_activity_proof_registry_status"] == f"status IN ({', '.join(map(repr, STATUSES))})"
    assert checks["ck_product_activity_proof_registry_resolution_reason"] == (
        f"resolution_reason IS NULL OR resolution_reason IN ({', '.join(map(repr, REASONS))})")
    assert checks["ck_product_activity_proof_registry_operation"] == m.OPERATION_CHECK_SQL
    assert dict(m.ATTRIBUTION_CHECKS)["ck_product_activity_event_attributions_operation"] == m.OPERATION_CHECK_SQL
    for event_type, surface, kind in OPERATIONS:
        assert f"event_type = '{event_type}' AND surface = '{surface}'" in m.OPERATION_CHECK_SQL
        assert f"'{kind}'" in m.OPERATION_CHECK_SQL
    assert "portfolio_write" not in MIGRATION.read_text(encoding="utf-8")
    assert m.RETENTION_AFTER_RESOLUTION == "168 hours" and "interval '168 hours'" in checks[
        "ck_product_activity_proof_registry_purge"]
    tree = ast.parse((REPO_ROOT / "core" / "authz_f2" / "attestations.py").read_text(encoding="utf-8"))
    constants = {node.targets[0].id: node.value.value for node in tree.body
                 if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                 and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}
    assert [constants[name] for name in ("DECRYPTAGE_DELIVERY_ACK", "PORTFOLIO_CREATE", "PORTFOLIO_UPDATE",
                                         "PORTFOLIO_DELETE")] == ["decryptage_delivery_ack", *PORTFOLIO_KINDS]


def test_no_personal_content_or_free_text_field_in_the_new_tables():
    for table_name, columns in COLUMNS.items():
        for name, sql_type, _ in columns:
            parts = set(name.split("_"))
            for marker in FORBIDDEN_FIELD_MARKERS + ("name", "email", "hash", "token", "label", "note", "detail"):
                assert marker not in parts and not name.startswith(marker), (table_name, name)
            assert sql_type != "text" and "json" not in sql_type, (table_name, name)
    assert {t for t in Base.metadata.tables if t.startswith("product_")} == V0T08_TABLES


def test_the_migration_and_models_import_nothing_from_f2():
    for path in (MIGRATION,):
        imported = {node.module if isinstance(node, ast.ImportFrom) else alias.name
                    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in (node.names if isinstance(node, ast.Import) else [None])}
        assert imported == {"typing", "alembic", "sqlalchemy"}, imported
    models = ast.parse((REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8"))
    assert {node.module for node in models.body if isinstance(node, ast.ImportFrom)} == {
        "datetime", "sqlalchemy", "sqlalchemy.dialects.postgresql", "core.db"}


# --------------------------------------------------------------------------
# 2. PostgreSQL réel : monde de référence
# --------------------------------------------------------------------------

USER_A, USER_B, USER_C = "v0-proof-user-a", "v0-proof-user-b", "v0-proof-user-c"
DIGEST_1, DIGEST_2 = "1" * 64, "2" * 64


class World:
    """Deux campagnes ; A inscrit aux deux, B à la première, C à aucune.
    Visites : A1 et A2 (deux appareils de A, campagne 1), B1, AC2 (A,
    campagne 2). Interactions acceptées : k_a1 (activity, A1), k_a1_close
    (close, A1), k_a2 (activity, A2), k_b1 (activity, B1), k_ac2 (activity,
    AC2)."""


def _session(conn, campaign, user, digest, *, at=BASE_TIME) -> uuid.UUID:
    session = uuid.uuid4()
    conn.execute(sa.text(
        "INSERT INTO product_activity_sessions (id, campaign_id, user_id, environment_digest, opened_at, "
        "last_activity_at, idle_expires_at, finalized_at, end_reason) VALUES (:id, :c, :u, :d, :t, :t, :x, NULL, "
        "NULL)"), {"id": session, "c": campaign, "u": user, "d": digest, "t": at, "x": at + timedelta(minutes=30)})
    return session


def _interaction(conn, campaign, user, digest, session, kind="activity", *, at=BASE_TIME) -> uuid.UUID:
    key = uuid.uuid4()
    conn.execute(sa.text(
        "INSERT INTO product_activity_session_interactions (campaign_id, user_id, interaction_key, interaction_kind, "
        "environment_digest, session_id, recorded_at) VALUES (:c, :u, :k, :kind, :d, :s, :t)"),
        {"c": campaign, "u": user, "k": key, "kind": kind, "d": digest, "s": session, "t": at})
    return key


def _world(conn) -> World:
    w = World()
    for user in (USER_A, USER_B, USER_C):
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant')"), {"u": user})
    w.c1, w.c2 = _campaign(conn)["id"], _campaign(conn)["id"]
    for campaign, user in ((w.c1, USER_A), (w.c1, USER_B), (w.c2, USER_A)):
        _participant(conn, campaign, user_id=user)
    w.a1 = _session(conn, w.c1, USER_A, DIGEST_1)
    w.a2 = _session(conn, w.c1, USER_A, DIGEST_2)
    w.b1 = _session(conn, w.c1, USER_B, DIGEST_1)
    w.ac2 = _session(conn, w.c2, USER_A, DIGEST_1)
    w.k_a1 = _interaction(conn, w.c1, USER_A, DIGEST_1, w.a1)
    w.k_a1_close = _interaction(conn, w.c1, USER_A, DIGEST_1, w.a1, "close")
    w.k_a2 = _interaction(conn, w.c1, USER_A, DIGEST_2, w.a2)
    w.k_b1 = _interaction(conn, w.c1, USER_B, DIGEST_1, w.b1)
    w.k_ac2 = _interaction(conn, w.c2, USER_A, DIGEST_1, w.ac2)
    return w


def _origin(w, **overrides) -> dict:
    values = {"campaign_id": w.c1, "user_id": USER_A, "visit_id": w.a1, "interaction_key": w.k_a1,
              "interaction_kind": "activity", "event_type": "decrypter_used", "surface": "decrypter",
              "source_operation_kind": "decryptage_delivery_ack", "source_operation_id": uuid.uuid4()}
    values.update(overrides)
    return values


REGISTRY_INSERT = (
    "INSERT INTO product_activity_proof_registry (id, campaign_id, user_id, visit_id, interaction_key, "
    "interaction_kind, event_type, surface, source_operation_kind, source_operation_id, status, created_at, "
    "certified_at, resolved_at, recorded_event_id, resolution_reason, purge_after_at) VALUES (:id, :campaign_id, "
    ":user_id, :visit_id, :interaction_key, :interaction_kind, :event_type, :surface, :source_operation_kind, "
    ":source_operation_id, :status, :created_at, :certified_at, :resolved_at, :recorded_event_id, "
    ":resolution_reason, :purge_after_at)")
ATTRIBUTION_INSERT = (
    "INSERT INTO product_activity_event_attributions (event_id, campaign_id, user_id, visit_id, interaction_key, "
    "interaction_kind, event_type, surface, source_operation_kind, source_operation_id, certified_at) VALUES "
    "(:event_id, :campaign_id, :user_id, :visit_id, :interaction_key, :interaction_kind, :event_type, :surface, "
    ":source_operation_kind, :source_operation_id, :certified_at)")


def _proof(origin, **overrides) -> dict:
    values = {"id": uuid.uuid4(), **origin, "status": "pending", "created_at": BASE_TIME, "certified_at": None,
              "resolved_at": None, "recorded_event_id": None, "resolution_reason": None, "purge_after_at": None}
    values.update(overrides)
    return values


def _resolved(origin, status, *, reason=None, event_id=None, certified=True, resolved_at=None, **overrides) -> dict:
    resolved_at = resolved_at or BASE_TIME + timedelta(minutes=5)
    overrides.setdefault("purge_after_at", resolved_at + timedelta(hours=168))
    return _proof(origin, status=status, certified_at=BASE_TIME + timedelta(minutes=1) if certified else None,
                  resolved_at=resolved_at, recorded_event_id=event_id, resolution_reason=reason, **overrides)


def _matching_event(conn, origin, **overrides) -> uuid.UUID:
    """Événement définitif dont l'identité concorde avec `origin`."""
    values = {"user_id": origin["user_id"], "visit_id": origin["visit_id"], "event_type": origin["event_type"],
              "surface": origin["surface"], "source": "server_business",
              "source_operation_kind": origin["source_operation_kind"],
              "source_operation_id": origin["source_operation_id"]}
    values.update(overrides)
    return _event(conn, **values)["id"]


def _attribution(origin, event_id, **overrides) -> dict:
    values = {"event_id": event_id, **origin, "certified_at": BASE_TIME + timedelta(minutes=1)}
    values.update(overrides)
    return values


def _record(conn, origin) -> tuple:
    """Séquence future du bloc C : événement, attribution, preuve recorded."""
    event_id = _matching_event(conn, origin)
    conn.execute(sa.text(ATTRIBUTION_INSERT), _attribution(origin, event_id))
    proof = _resolved(origin, "recorded", event_id=event_id)
    conn.execute(sa.text(REGISTRY_INSERT), proof)
    return event_id, proof


@pytest.fixture
def world(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    with pg_engine.connect() as conn:
        w = _world(conn)
        w.conn = conn
        yield w
        conn.rollback()


def _insert_origin(conn, target, origin) -> None:
    """Insère une ligne d'origine `origin` dans le registre ou dans les
    attributions ; pour une attribution, l'événement concordant est créé
    d'abord (product_activity_events ne contraint ni la visite ni la
    campagne), de sorte que seule l'origine puisse être refusée."""
    if target == "registry":
        conn.execute(sa.text(REGISTRY_INSERT), _proof(origin))
    else:
        conn.execute(sa.text(ATTRIBUTION_INSERT), _attribution(origin, _matching_event(conn, origin)))


def _fkey(target, *names) -> set:
    return {F2_FKEYS[target].format(name) for name in names}


def _refused(conn, constraints, statement, **params) -> None:
    """La base refuse l'écriture (SAVEPOINT annulé) et la contrainte qui
    refuse est EXACTEMENT l'une de `constraints` (diag PostgreSQL, pas une
    recherche dans le message). Plusieurs noms seulement quand la forme
    viole nécessairement plusieurs contraintes à la fois."""
    allowed = {constraints} if isinstance(constraints, str) else set(constraints)
    with pytest.raises(sa.exc.IntegrityError) as failure:
        with conn.begin_nested():
            if callable(statement):
                statement()
            else:
                conn.execute(sa.text(statement), params)
    assert failure.value.orig.diag.constraint_name in allowed, (failure.value.orig.diag.constraint_name, allowed)


# --------------------------------------------------------------------------
# 2a. Identités et visites (registre ET attributions)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["registry", "attribution"])
def test_pg_cross_identity_and_visit_attachments_are_refused_by_postgresql(world, target):
    w, conn = world, world.conn
    cases = [
        # 1. preuve de A sur la visite de B (avec la clé d'activité de B ou de A)
        ("A + visite de B", _origin(w, visit_id=w.b1, interaction_key=w.k_b1), ("visit", "interaction")),
        ("A + visite de B, clé de A", _origin(w, visit_id=w.b1), ("visit", "interaction")),
        # 2. campagne 1, visite de la campagne 2 (même utilisateur)
        ("campagne 1 + visite campagne 2", _origin(w, visit_id=w.ac2, interaction_key=w.k_ac2),
         ("visit", "interaction")),
        # 3. interaction d'activité appartenant à une autre visite du même propriétaire
        ("interaction d'une autre visite", _origin(w, visit_id=w.a2, interaction_key=w.k_a1), ("interaction",)),
        # 4. interaction 'close' utilisée comme origine (déclarée activity, puis close)
        ("clôture déclarée activity", _origin(w, interaction_key=w.k_a1_close), ("interaction",)),
        # 5. interaction inconnue
        ("interaction inconnue", _origin(w, interaction_key=uuid.uuid4()), ("interaction",)),
        # 6. visite inconnue
        ("visite inconnue", _origin(w, visit_id=uuid.uuid4()), ("visit", "interaction")),
        # 7. utilisateur absent de la participation (existant, puis inexistant)
        ("utilisateur non inscrit", _origin(w, user_id=USER_C), ("participant", "visit", "interaction")),
        ("utilisateur inexistant", _origin(w, user_id="v0-proof-ghost"), ("participant", "visit", "interaction")),
        ("B inscrit, campagne 2", _origin(w, campaign_id=w.c2, user_id=USER_B, visit_id=w.b1,
                                          interaction_key=w.k_b1), ("participant", "visit", "interaction")),
    ]
    for label, origin, constraints in cases:
        if target == "attribution" and origin["user_id"] == "v0-proof-ghost":
            # Un utilisateur inexistant ne peut posséder aucun événement (FK de
            # 0022) : l'attribution vise alors un événement quelconque.
            _refused(conn, _fkey(target, *constraints, "event"), ATTRIBUTION_INSERT,
                     **_attribution(origin, uuid.uuid4()))
            continue
        _refused(conn, _fkey(target, *constraints), lambda o=origin: _insert_origin(conn, target, o))
    kind_check = (f"ck_{REGISTRY}_interaction_kind" if target == "registry"
                  else f"ck_{ATTRIBUTIONS}_interaction_kind")
    _refused(conn, kind_check, lambda: _insert_origin(conn, target, _origin(w, interaction_key=w.k_a1_close,
                                                                            interaction_kind="close")))
    assert conn.execute(sa.text(f"SELECT count(*) FROM {REGISTRY if target == 'registry' else ATTRIBUTIONS}")
                        ).scalar_one() == 0
    # 8. interaction activity valide dans la bonne visite : acceptée.
    _insert_origin(conn, target, _origin(w))


@pytest.mark.parametrize("target", ["registry", "attribution"])
def test_pg_each_origin_foreign_key_alone_is_enforced(world, target):
    """Cas où UNE seule FK d'origine est violée : la contrainte nommée exacte
    est celle qui refuse (aucune vérification applicative en jeu)."""
    w, conn = world, world.conn
    _refused(conn, _fkey(target, "interaction"),
             lambda: _insert_origin(conn, target, _origin(w, visit_id=w.a2, interaction_key=w.k_a1)))
    _refused(conn, _fkey(target, "interaction"),
             lambda: _insert_origin(conn, target, _origin(w, interaction_key=w.k_a1_close)))
    # Une participation absente entraîne nécessairement l'absence de visite et
    # d'interaction (FK de 0023) : ces trois FK sont toutes présentes en base.
    names = conn.execute(sa.text(
        "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND contype = 'f'"),
        {"t": REGISTRY if target == "registry" else ATTRIBUTIONS}).scalars().all()
    assert set(names) == _fkey(target, "participant", "visit", "interaction",
                               "recorded_event" if target == "registry" else "event")


@pytest.mark.parametrize("target", ["registry", "attribution"])
def test_pg_two_devices_and_an_old_replayed_interaction_keep_their_own_visit(world, target):
    w, conn = world, world.conn
    # 9. deux appareils du même utilisateur : visites distinctes, chacune sa preuve.
    _insert_origin(conn, target, _origin(w))
    _insert_origin(conn, target, _origin(w, visit_id=w.a2, interaction_key=w.k_a2))
    # ... mais jamais la clé d'un appareil sur la visite de l'autre.
    _refused(conn, _fkey(target, "interaction"),
             lambda: _insert_origin(conn, target, _origin(w, visit_id=w.a1, interaction_key=w.k_a2)))
    # 10. ancienne interaction rejouée : la visite A1 est close, une nouvelle
    # visite A3 s'ouvre sur le même appareil ; l'ancienne clé reste attachée
    # à SA visite d'origine (acceptée par le schéma, la fenêtre temporelle
    # relève des blocs B/C) et ne peut jamais être déplacée vers A3.
    conn.execute(sa.text("UPDATE product_activity_sessions SET finalized_at = :t, end_reason = 'logout' "
                         "WHERE id = :s"), {"t": BASE_TIME + timedelta(minutes=1), "s": w.a1})
    a3 = _session(conn, w.c1, USER_A, DIGEST_1, at=BASE_TIME + timedelta(hours=2))
    _refused(conn, _fkey(target, "interaction"),
             lambda: _insert_origin(conn, target, _origin(w, visit_id=a3, interaction_key=w.k_a1)))
    _refused(conn, "product_activity_session_interactions_pkey",
             lambda: _interaction_with_key(conn, w, w.k_a1, a3))
    _insert_origin(conn, target, _origin(w, source_operation_id=uuid.uuid4()))  # rejeu dans SA visite


def _interaction_with_key(conn, w, key, session) -> None:
    conn.execute(sa.text(
        "INSERT INTO product_activity_session_interactions VALUES (:c, :u, :k, 'activity', :d, :s, :t)"),
        {"c": w.c1, "u": USER_A, "k": key, "d": DIGEST_1, "s": session, "t": BASE_TIME + timedelta(hours=2)})


# --------------------------------------------------------------------------
# 2b. Opérations canoniques et événements
# --------------------------------------------------------------------------

def test_pg_operation_coherence_is_closed(world):
    w, conn = world, world.conn
    for event_type, surface, kind in OPERATIONS:
        origin = _origin(w, event_type=event_type, surface=surface, source_operation_kind=kind)
        conn.execute(sa.text(REGISTRY_INSERT), _proof(origin))
        conn.execute(sa.text(ATTRIBUTION_INSERT), _attribution(origin, _matching_event(conn, origin)))
    forbidden = [("decrypter_used", "decrypter", "assistant_delivery"),  # jamais converti par commodité
                 ("portfolio_used", "portfolio", "portfolio_write"),  # jamais regroupé
                 ("education_used", "education", "decryptage_delivery_ack"),
                 ("coach_used", "education", "assistant_delivery"),
                 ("portfolio_used", "decrypter", "portfolio_position_create"),
                 ("session_started", "app", "assistant_delivery"),
                 ("profile_viewed", "profile", "assistant_delivery"),
                 ("view_opened", "portfolio", "portfolio_position_update"),
                 ("decrypter_used", "decrypter", "DECRYPTAGE_DELIVERY_ACK"),
                 ("portfolio_used", "portfolio", "AIR.PA")]
    for event_type, surface, kind in forbidden:
        origin = _origin(w, event_type=event_type, surface=surface, source_operation_kind=kind)
        _refused(conn, f"ck_{REGISTRY}_operation", REGISTRY_INSERT, **_proof(origin))
        _refused(conn, f"ck_{ATTRIBUTIONS}_operation",
                 lambda o=origin: conn.execute(sa.text(ATTRIBUTION_INSERT),
                                               _attribution(o, _matching_event(conn, o))))


def test_pg_canonical_operation_is_unique_per_owner(world):
    w, conn = world, world.conn
    operation = uuid.uuid4()
    first = _origin(w, source_operation_id=operation)
    conn.execute(sa.text(REGISTRY_INSERT), _proof(first))
    # 1. même opération canonique, autre tentative / autre appareil / autre visite : refusée.
    for retry in (first, _origin(w, visit_id=w.a2, interaction_key=w.k_a2, source_operation_id=operation)):
        _refused(conn, f"uq_{REGISTRY}_operation", REGISTRY_INSERT, **_proof(retry))
    # 2. même UUID, natures Portfolio différentes : opérations distinctes.
    shared = uuid.uuid4()
    for kind in PORTFOLIO_KINDS:
        conn.execute(sa.text(REGISTRY_INSERT), _proof(_origin(
            w, event_type="portfolio_used", surface="portfolio", source_operation_kind=kind,
            source_operation_id=shared)))
    _refused(conn, f"uq_{REGISTRY}_operation", REGISTRY_INSERT, **_proof(_origin(
        w, event_type="portfolio_used", surface="portfolio", source_operation_kind="portfolio_position_update",
        source_operation_id=shared, visit_id=w.a2, interaction_key=w.k_a2)))
    # Même UUID d'opération pour un AUTRE propriétaire : identité distincte.
    conn.execute(sa.text(REGISTRY_INSERT), _proof(_origin(w, user_id=USER_B, visit_id=w.b1, interaction_key=w.k_b1,
                                                          source_operation_id=operation)))
    # Attributions : une seule par opération canonique, en plus de l'index
    # partiel des événements (9. deux événements pour une même opération).
    event_id = _matching_event(conn, first)
    conn.execute(sa.text(ATTRIBUTION_INSERT), _attribution(first, event_id))
    _refused(conn, "uq_product_activity_events_source_operation", lambda: _matching_event(conn, first))
    # 8. deux attributions du même événement (même ligne : PK ou UNIQUE, selon
    # l'ordre des index ; autre origine : PK seule).
    _refused(conn, {f"{ATTRIBUTIONS}_pkey", f"uq_{ATTRIBUTIONS}_operation", f"uq_{ATTRIBUTIONS}_proof"},
             ATTRIBUTION_INSERT, **_attribution(first, event_id))
    _refused(conn, f"{ATTRIBUTIONS}_pkey", ATTRIBUTION_INSERT,
             **_attribution(_origin(w, visit_id=w.a2, interaction_key=w.k_a2, source_operation_id=uuid.uuid4()),
                            event_id))
    for kind in ("portfolio_position_create", "portfolio_position_delete"):
        origin = _origin(w, event_type="portfolio_used", surface="portfolio", source_operation_kind=kind,
                         source_operation_id=shared)
        conn.execute(sa.text(ATTRIBUTION_INSERT), _attribution(origin, _matching_event(conn, origin)))


def test_pg_attribution_must_match_its_event_exactly(world):
    w, conn = world, world.conn
    origin_a = _origin(w)
    event_a = _matching_event(conn, origin_a)
    origin_b = _origin(w, user_id=USER_B, visit_id=w.b1, interaction_key=w.k_b1)
    event_b = _matching_event(conn, origin_b)
    portfolio = _origin(w, event_type="portfolio_used", surface="portfolio",
                        source_operation_kind="portfolio_position_create")
    event_p = _matching_event(conn, portfolio)
    off_surface = _origin(w)
    event_off_surface = _matching_event(conn, off_surface, surface="app")  # 0022 ne fixe pas la surface
    event_fk = f"{ATTRIBUTIONS}_event_fkey"
    cases = [
        # 3. événement de B attribué à A (origine A valide)
        _attribution(_origin(w, source_operation_id=origin_b["source_operation_id"]), event_b),
        # 4. attribution à une autre visite (origine valide sur A2)
        _attribution({**origin_a, "visit_id": w.a2, "interaction_key": w.k_a2}, event_a),
        # 5. autre type métier (couple cohérent, mais pas celui de l'événement)
        _attribution({**origin_a, "event_type": "portfolio_used", "surface": "portfolio",
                      "source_operation_kind": "portfolio_position_create"}, event_a),
        # 6. autre surface que celle de l'événement
        _attribution(off_surface, event_off_surface),
        # 7. autre identité d'opération : autre UUID, puis autre nature Portfolio
        _attribution({**origin_a, "source_operation_id": uuid.uuid4()}, event_a),
        _attribution({**portfolio, "source_operation_kind": "portfolio_position_update"}, event_p),
        # événement inexistant
        _attribution(_origin(w), uuid.uuid4()),
    ]
    for row in cases:
        _refused(conn, event_fk, ATTRIBUTION_INSERT, **row)
    # Un événement sans opération canonique (signal client) n'est jamais attribuable.
    signal = _event(conn, user_id=USER_A, visit_id=w.a1, event_type="decrypter_used", surface="decrypter",
                    source="client_signal")["id"]
    _refused(conn, event_fk, ATTRIBUTION_INSERT, **_attribution(_origin(w), signal))
    assert conn.execute(sa.text(f"SELECT count(*) FROM {ATTRIBUTIONS}")).scalar_one() == 0
    for origin, event_id in ((origin_a, event_a), (origin_b, event_b), (portfolio, event_p)):
        conn.execute(sa.text(ATTRIBUTION_INSERT), _attribution(origin, event_id))


def test_pg_recorded_proof_only_points_to_its_own_attribution(world):
    w, conn = world, world.conn
    event_a, _ = _record(conn, _origin(w))
    origin_b = _origin(w, user_id=USER_B, visit_id=w.b1, interaction_key=w.k_b1)
    event_b = _matching_event(conn, origin_b)
    conn.execute(sa.text(ATTRIBUTION_INSERT), _attribution(origin_b, event_b))
    other_a = _origin(w, visit_id=w.a2, interaction_key=w.k_a2)
    event_other_a = _matching_event(conn, other_a)
    conn.execute(sa.text(ATTRIBUTION_INSERT), _attribution(other_a, event_other_a))
    recorded_fk = f"{REGISTRY}_recorded_event_fkey"
    pending = _origin(w)
    cases = [
        # 10. preuve de A rattachée à l'attribution de B (même UUID d'opération)
        _resolved({**pending, "source_operation_id": origin_b["source_operation_id"]}, "recorded",
                  event_id=event_b),
        # attribution de A, mais autre visite / autre interaction / autre opération
        _resolved(pending, "recorded", event_id=event_other_a),
        _resolved({**pending, "source_operation_id": other_a["source_operation_id"]}, "recorded",
                  event_id=event_other_a),
        # attribution inexistante
        _resolved(pending, "recorded", event_id=uuid.uuid4()),
    ]
    for row in cases:
        _refused(conn, recorded_fk, REGISTRY_INSERT, **row)
    # Une attribution ne porte qu'une preuve (unicité opération puis recorded_event_id).
    _refused(conn, {f"uq_{REGISTRY}_operation", f"uq_{REGISTRY}_recorded_event"}, REGISTRY_INSERT,
             **_resolved({**_origin(w), "source_operation_id": conn.execute(sa.text(
                 f"SELECT source_operation_id FROM {ATTRIBUTIONS} WHERE event_id = :e"), {"e": event_a}
             ).scalar_one()}, "recorded", event_id=event_a))
    # La preuve de B vers SA propre attribution : acceptée.
    conn.execute(sa.text(REGISTRY_INSERT), _resolved(origin_b, "recorded", event_id=event_b))


# --------------------------------------------------------------------------
# 2c. États et conservation
# --------------------------------------------------------------------------

def test_pg_every_allowed_state_shape_is_accepted(world):
    w, conn = world, world.conn
    rows = [
        _proof(_origin(w)),
        _proof(_origin(w), status="certified", certified_at=BASE_TIME),
        _resolved(_origin(w), "rejected", reason="no_new_effect"),
        _resolved(_origin(w), "rejected", reason="source_unverified", certified=False),  # jamais certifiée
        _resolved(_origin(w), "indeterminate", reason="technical_uncertainty"),
        _resolved(_origin(w), "indeterminate", reason="finalization_expired", certified=False),
        # instants égaux : chronologie large
        _proof(_origin(w), status="rejected", certified_at=BASE_TIME, resolved_at=BASE_TIME,
               resolution_reason="window_expired", purge_after_at=BASE_TIME + timedelta(hours=168)),
    ]
    for row in rows:
        conn.execute(sa.text(REGISTRY_INSERT), row)
    for reason in REASONS:
        conn.execute(sa.text(REGISTRY_INSERT), _resolved(_origin(w), "rejected", reason=reason))
    _record(conn, _origin(w))
    statuses = conn.execute(sa.text(f"SELECT DISTINCT status FROM {REGISTRY}")).scalars().all()
    assert sorted(statuses) == sorted(STATUSES)


def test_pg_forbidden_state_shapes_are_refused(world):
    w, conn = world, world.conn
    event_id, recorded = _record(conn, _origin(w))
    later = BASE_TIME + timedelta(minutes=5)
    purge = later + timedelta(hours=168)
    state, chronology = f"ck_{REGISTRY}_state", f"ck_{REGISTRY}_chronology"
    cases = [
        # statut inconnu : aucune forme ne correspond ET le statut est hors
        # vocabulaire ; PostgreSQL évalue les CHECK par ordre alphabétique.
        ({state, f"ck_{REGISTRY}_status"}, _proof(_origin(w), status="succeeded")),
        ({state, f"ck_{REGISTRY}_status"}, _proof(_origin(w), status="PENDING")),
        ({state, f"ck_{REGISTRY}_status"}, _proof(_origin(w), status="purged")),
        # pending avec certification, résolution, motif ou échéance
        (state, _proof(_origin(w), certified_at=BASE_TIME)),
        (state, _proof(_origin(w), resolved_at=later, purge_after_at=purge)),
        (state, _proof(_origin(w), resolution_reason="no_new_effect")),
        (f"ck_{REGISTRY}_purge", _proof(_origin(w), purge_after_at=purge)),
        # certified sans certification, ou déjà résolue
        (state, _proof(_origin(w), status="certified")),
        (state, _proof(_origin(w), status="certified", certified_at=BASE_TIME, resolved_at=later,
                       purge_after_at=purge)),
        # recorded sans événement, sans certification, avec motif
        (state, _resolved(_origin(w), "recorded")),
        (state, _resolved(_origin(w), "recorded", event_id=event_id, certified=False)),
        (state, _resolved(_origin(w), "recorded", event_id=event_id, reason="no_new_effect")),
        # rejected / indeterminate sans motif, sans résolution, ou avec événement
        (state, _resolved(_origin(w), "rejected")),
        (state, _resolved(_origin(w), "indeterminate")),
        (state, _proof(_origin(w), status="rejected", resolution_reason="no_new_effect")),
        (state, _resolved(_origin(w), "rejected", reason="no_new_effect", event_id=event_id)),
        # chronologie
        (chronology, _proof(_origin(w), status="certified", certified_at=BASE_TIME - timedelta(seconds=1))),
        (chronology, _resolved(_origin(w), "rejected", reason="no_new_effect", certified=False,
                               resolved_at=BASE_TIME - timedelta(seconds=1))),
        (chronology, _proof(_origin(w), status="rejected", certified_at=later, resolved_at=later - timedelta(
            microseconds=1), resolution_reason="no_new_effect", purge_after_at=later - timedelta(microseconds=1)
            + timedelta(hours=168))),
        # motifs fermés : aucune valeur libre
        (f"ck_{REGISTRY}_resolution_reason", _resolved(_origin(w), "rejected", reason="Ticker AIR.PA refusé")),
        (f"ck_{REGISTRY}_resolution_reason", _resolved(_origin(w), "rejected", reason="NO_NEW_EFFECT")),
        (f"ck_{REGISTRY}_resolution_reason", _resolved(_origin(w), "indeterminate", reason="unknown")),
        (f"ck_{REGISTRY}_resolution_reason", _resolved(_origin(w), "rejected", reason="")),
    ]
    for constraint, row in cases:
        _refused(conn, constraint, REGISTRY_INSERT, **row)
    # Une preuve recorded ne redevient pas pending en gardant son événement.
    _refused(conn, state, "UPDATE product_activity_proof_registry SET status = 'pending' WHERE id = :i",
             i=recorded["id"])


def test_pg_retention_is_exactly_168_absolute_hours_after_resolution(world):
    w, conn = world, world.conn
    purge = f"ck_{REGISTRY}_purge"
    resolved_at = BASE_TIME + timedelta(minutes=5)
    conn.execute(sa.text(REGISTRY_INSERT), _resolved(_origin(w), "rejected", reason="no_new_effect"))
    for delta in (timedelta(hours=168) - timedelta(microseconds=1), timedelta(hours=168) + timedelta(microseconds=1),
                  timedelta(hours=167), timedelta(days=90)):
        _refused(conn, purge, REGISTRY_INSERT, **_resolved(_origin(w), "rejected", reason="no_new_effect",
                                                           purge_after_at=resolved_at + delta))
    # Une preuve résolue sans échéance : refusée (le CHECK n'est jamais inconnu).
    _refused(conn, purge, REGISTRY_INSERT, **_resolved(_origin(w), "rejected", reason="no_new_effect",
                                                       purge_after_at=None))
    _refused(conn, purge, REGISTRY_INSERT, **_proof(_origin(w), status="certified", certified_at=BASE_TIME,
                                                    purge_after_at=BASE_TIME + timedelta(hours=168)))


@pytest.mark.parametrize("zone", ["Europe/Paris", "America/New_York", "UTC"])
def test_pg_retention_ignores_the_session_time_zone_and_daylight_saving_changes(world, zone):
    """Fin de l'heure d'été européenne le 25/10/2026 et américaine le
    1/11/2026 : 168 heures absolues, jamais « 7 jours » d'horloge locale."""
    w, conn = world, world.conn
    conn.execute(sa.text(f"SET LOCAL TIME ZONE '{zone}'"))
    purge = f"ck_{REGISTRY}_purge"
    for resolved_at in (datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc),
                        datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc),
                        datetime(2026, 3, 27, 10, 0, tzinfo=timezone.utc)):
        origin = _origin(w)
        created = resolved_at - timedelta(minutes=5)
        row = _proof(origin, status="rejected", created_at=created, resolved_at=resolved_at,
                     resolution_reason="window_expired", purge_after_at=resolved_at + timedelta(hours=168))
        conn.execute(sa.text(REGISTRY_INSERT), row)
        local_seven_days = conn.execute(sa.text("SELECT CAST(:r AS timestamptz) + interval '7 days'"),
                                        {"r": resolved_at}).scalar_one()
        if local_seven_days != resolved_at + timedelta(hours=168):  # passage d'heure dans le fuseau de session
            _refused(conn, purge, REGISTRY_INSERT,
                     **{**row, "id": uuid.uuid4(), "source_operation_id": uuid.uuid4(),
                        "purge_after_at": local_seven_days})
        stored = conn.execute(sa.text(f"SELECT purge_after_at - resolved_at FROM {REGISTRY} WHERE id = :i"),
                              {"i": row["id"]}).scalar_one()
        assert stored == timedelta(hours=168)
    crossings = conn.execute(sa.text(
        "SELECT count(*) FROM (VALUES (CAST('2026-10-24 10:00+00' AS timestamptz)), "
        "(CAST('2026-10-30 12:00+00' AS timestamptz)), (CAST('2026-03-27 10:00+00' AS timestamptz))) v(r) "
        "WHERE r + interval '7 days' <> r + interval '168 hours'")).scalar_one()
    assert crossings == {"Europe/Paris": 2, "America/New_York": 1, "UTC": 0}[zone]


def test_pg_deleting_a_proof_keeps_the_definitive_attribution_and_dependencies_are_protected(world):
    w, conn = world, world.conn
    event_id, proof = _record(conn, _origin(w))
    attribution = conn.execute(sa.text(f"SELECT * FROM {ATTRIBUTIONS} WHERE event_id = :e"),
                               {"e": event_id}).one()
    # Attribution protégée tant que la preuve qui la référence existe ; événement
    # protégé tant que l'attribution existe ; interaction, visite et
    # participation protégées (aucune cascade).
    _refused(conn, f"{REGISTRY}_recorded_event_fkey", f"DELETE FROM {ATTRIBUTIONS} WHERE event_id = :e", e=event_id)
    _refused(conn, f"{ATTRIBUTIONS}_event_fkey", f"DELETE FROM {EVENTS} WHERE id = :e", e=event_id)
    _refused(conn, f"{ATTRIBUTIONS}_event_fkey", f"UPDATE {EVENTS} SET surface = 'app' WHERE id = :e", e=event_id)
    _refused(conn, {f"{REGISTRY}_interaction_fkey", f"{ATTRIBUTIONS}_interaction_fkey"},
             f"DELETE FROM {INTERACTIONS} WHERE interaction_key = :k", k=w.k_a1)
    _refused(conn, {f"{REGISTRY}_visit_fkey", f"{ATTRIBUTIONS}_visit_fkey", f"{INTERACTIONS}_session_fkey"},
             f"DELETE FROM {SESSIONS} WHERE id = :s", s=w.a1)
    # La purge du registre (lot 2C) ne touche pas l'attribution définitive.
    conn.execute(sa.text(f"DELETE FROM {REGISTRY} WHERE id = :i"), {"i": proof["id"]})
    assert conn.execute(sa.text(f"SELECT * FROM {ATTRIBUTIONS} WHERE event_id = :e"),
                        {"e": event_id}).one() == attribution
    # Ordre de suppression future : preuve, attribution, puis événement.
    _refused(conn, f"{ATTRIBUTIONS}_event_fkey", f"DELETE FROM {EVENTS} WHERE id = :e", e=event_id)
    conn.execute(sa.text(f"DELETE FROM {ATTRIBUTIONS} WHERE event_id = :e"), {"e": event_id})
    conn.execute(sa.text(f"DELETE FROM {EVENTS} WHERE id = :e"), {"e": event_id})


def test_pg_invalid_insert_is_rolled_back_and_leaves_nothing(world):
    w, conn = world, world.conn
    valid = _proof(_origin(w))
    with pytest.raises(sa.exc.IntegrityError):
        with conn.begin_nested():
            conn.execute(sa.text(REGISTRY_INSERT), valid)
            conn.execute(sa.text(REGISTRY_INSERT), _proof(_origin(w, interaction_key=w.k_a1_close)))
    assert conn.execute(sa.text(f"SELECT count(*) FROM {REGISTRY}")).scalar_one() == 0
    conn.execute(sa.text(REGISTRY_INSERT), valid)  # la transaction reste utilisable
    assert conn.execute(sa.text(f"SELECT count(*) FROM {REGISTRY}")).scalar_one() == 1


@pytest.mark.parametrize("target", ["registry", "attribution"])
def test_pg_not_null_columns_are_enforced(world, target):
    w, conn = world, world.conn
    table = REGISTRY if target == "registry" else ATTRIBUTIONS
    for name, _, nullable in COLUMNS[table]:
        if nullable:
            continue
        origin = _origin(w)
        row = _proof(origin) if target == "registry" else _attribution(origin, _matching_event(conn, origin))
        statement = REGISTRY_INSERT if target == "registry" else ATTRIBUTION_INSERT
        _rejects(conn, f'null value in column "{name}"', statement, **{**row, name: None})


# --------------------------------------------------------------------------
# 2d. Migration, catalogue et concurrence
# --------------------------------------------------------------------------

def _seed_0023(engine) -> dict:
    """Historique 0021, campagne / participation 0022, et lignes 0023
    (visite, interactions activity et close) plus un événement 0022."""
    seeded = _seed_0022(engine)
    with engine.begin() as conn:
        session = _session(conn, seeded["campaign"], seeded["user"], DIGEST_1)
        _interaction(conn, seeded["campaign"], seeded["user"], DIGEST_1, session)
        _interaction(conn, seeded["campaign"], seeded["user"], DIGEST_1, session, "close")
        _event(conn, user_id=seeded["user"], visit_id=session, event_type="session_started", surface="app",
               source="server_visit")
        _event(conn, user_id=seeded["user"], visit_id=session, event_type="decrypter_used", surface="decrypter",
               source="server_business", source_operation_kind="assistant_delivery",
               source_operation_id=uuid.uuid4())  # forme lot 2A conservée telle quelle
    return seeded


def _constraint_defs(engine, tables) -> dict:
    with engine.connect() as conn:
        return {name: (table, contype, definition) for name, table, contype, definition in conn.execute(sa.text(
            "SELECT conname, conrelid::regclass::text, contype, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = ANY(CAST(:t AS regclass[])) AND contype <> 'n'"), {"t": sorted(tables)})}


def test_pg_upgrade_0023_to_0024_on_an_empty_database(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0023)
    tables = _tables(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    assert _version(pg_engine) == V0T08_0024 and _compare_metadata(pg_engine) == []
    assert _tables(pg_engine) == tables | NEW
    assert _data(pg_engine, NEW) == {t: [] for t in sorted(NEW)}


def test_pg_upgrade_0023_to_0024_keeps_every_row_then_exact_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0023)
    _seed_0023(pg_engine)
    tables = _tables(pg_engine)
    assert not tables & NEW
    schema_0023, catalog_0023 = _snapshot(pg_engine, tables), _catalog(pg_engine, tables)
    global_0023 = _global_catalog(pg_engine)
    data_0023 = _data(pg_engine, tables - {"alembic_version"})
    assert all(data_0023[t] for t in (EVENTS, SESSIONS, INTERACTIONS, PARTICIPANTS, "users"))
    assert _compare_metadata(pg_engine) == []  # écarts attendus de 0024 retirés exactement

    _run_alembic(pg_url, "upgrade", V0T08_0024)
    assert _version(pg_engine) == V0T08_0024 and _compare_metadata(pg_engine) == []
    assert _tables(pg_engine) == tables | NEW
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0023  # aucun backfill, aucune ligne modifiée
    assert _data(pg_engine, NEW) == {t: [] for t in sorted(NEW)}
    untouched = tables - {"alembic_version"} - ALTERED  # F2, pédagogie, analytics 0022/0023 non modifiées
    assert {t for t in F2_TABLES if t in tables} <= untouched
    assert _snapshot(pg_engine, untouched) == {t: schema_0023[t] for t in sorted(untouched)}
    assert _catalog(pg_engine, untouched) == {t: catalog_0023[t] for t in sorted(untouched)}
    assert _global_catalog(pg_engine) == global_0023
    # Tables existantes modifiées : colonnes identiques, exactement une UNIQUE
    # (et son index) en plus, rien de retiré.
    after = _catalog(pg_engine, ALTERED)
    for name, (table, columns, _) in V0T08_0024_EXISTING_UNIQUES.items():
        assert after[table]["columns"] == catalog_0023[table]["columns"]
        added = set(after[table]["constraints"]) - set(catalog_0023[table]["constraints"])
        assert set(catalog_0023[table]["constraints"]) <= set(after[table]["constraints"])
        assert added == {(name, "u", f"UNIQUE ({', '.join(columns)})", " ", " ")}
        assert sorted(after[table]["indexes"]) == sorted(catalog_0023[table]["indexes"] + [name])

    _run_alembic(pg_url, "downgrade", V0T08_0023)
    assert _version(pg_engine) == V0T08_0023 and _tables(pg_engine) == tables
    assert _snapshot(pg_engine, tables) == schema_0023 and _catalog(pg_engine, tables) == catalog_0023
    assert _global_catalog(pg_engine) == global_0023
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0023
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == V0T08_0024 and _compare_metadata(pg_engine) == []


def test_pg_catalog_is_exact(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    m = _m0024()
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
        fk_rules = conn.execute(sa.text(
            "SELECT conname, condeferrable, confdeltype, confupdtype, confmatchtype FROM pg_constraint "
            "WHERE conrelid = ANY(CAST(:t AS regclass[])) AND contype = 'f'"), {"t": sorted(NEW)}).all()
        assert len(fk_rules) == 8  # quatre par table
        for name, deferrable, deltype, updtype, matchtype in fk_rules:
            assert (deferrable, deltype, updtype, matchtype) == (False, "a", "a", "s"), name  # NO ACTION, SIMPLE
        triggers = conn.execute(sa.text(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = ANY(CAST(:t AS regclass[]))"),
            {"t": sorted(NEW | ALTERED)}).scalar_one()
        assert triggers == 0
        indexdefs = dict(conn.execute(sa.text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = ANY(:t)"),
            {"t": sorted(NEW)}).all())
    constraints = _constraint_defs(pg_engine, NEW)
    expected_checks = {name: table for table, checks in ((ATTRIBUTIONS, m.ATTRIBUTION_CHECKS),
                                                         (REGISTRY, m.REGISTRY_CHECKS)) for name, _ in checks}
    assert {n: t for n, (t, c, _) in constraints.items() if c == "c"} == expected_checks
    origin = {
        "participant_fkey": "FOREIGN KEY (campaign_id, user_id) REFERENCES "
                            "product_campaign_participants(campaign_id, user_id)",
        "visit_fkey": "FOREIGN KEY (visit_id, campaign_id, user_id) REFERENCES "
                      "product_activity_sessions(id, campaign_id, user_id)",
        "interaction_fkey": "FOREIGN KEY (campaign_id, user_id, interaction_key, visit_id, interaction_kind) "
                            "REFERENCES product_activity_session_interactions(campaign_id, user_id, interaction_key, "
                            "session_id, interaction_kind)",
    }
    identity = ("campaign_id, user_id, visit_id, interaction_key, interaction_kind, event_type, surface, "
                "source_operation_kind, source_operation_id")
    expected = {f"{table}_{suffix}": (table, definition) for table in (ATTRIBUTIONS, REGISTRY)
                for suffix, definition in origin.items()}
    expected.update({
        f"{ATTRIBUTIONS}_pkey": (ATTRIBUTIONS, "PRIMARY KEY (event_id)"),
        f"{ATTRIBUTIONS}_event_fkey": (
            ATTRIBUTIONS, "FOREIGN KEY (event_id, user_id, visit_id, event_type, surface, source_operation_kind, "
                          "source_operation_id) REFERENCES product_activity_events(id, user_id, visit_id, event_type, "
                          "surface, source_operation_kind, source_operation_id)"),
        f"uq_{ATTRIBUTIONS}_operation": (ATTRIBUTIONS, "UNIQUE (user_id, source_operation_kind, source_operation_id)"),
        f"uq_{ATTRIBUTIONS}_proof": (ATTRIBUTIONS, f"UNIQUE (event_id, {identity})"),
        f"{REGISTRY}_pkey": (REGISTRY, "PRIMARY KEY (id)"),
        f"{REGISTRY}_recorded_event_fkey": (
            REGISTRY, f"FOREIGN KEY (recorded_event_id, {identity}) REFERENCES "
                      f"product_activity_event_attributions(event_id, {identity})"),
        f"uq_{REGISTRY}_operation": (REGISTRY, "UNIQUE (user_id, source_operation_kind, source_operation_id)"),
        f"uq_{REGISTRY}_recorded_event": (REGISTRY, "UNIQUE (recorded_event_id)"),
    })
    assert {n: (t, d) for n, (t, c, d) in constraints.items() if c != "c"} == expected
    assert all(len(name) <= 63 for name in constraints)
    assert indexdefs == {
        f"{ATTRIBUTIONS}_pkey": f"CREATE UNIQUE INDEX {ATTRIBUTIONS}_pkey ON public.{ATTRIBUTIONS} USING btree "
                                "(event_id)",
        f"uq_{ATTRIBUTIONS}_operation": f"CREATE UNIQUE INDEX uq_{ATTRIBUTIONS}_operation ON public.{ATTRIBUTIONS} "
                                        "USING btree (user_id, source_operation_kind, source_operation_id)",
        f"uq_{ATTRIBUTIONS}_proof": f"CREATE UNIQUE INDEX uq_{ATTRIBUTIONS}_proof ON public.{ATTRIBUTIONS} USING "
                                    f"btree (event_id, {identity})",
        f"ix_{ATTRIBUTIONS}_interaction": f"CREATE INDEX ix_{ATTRIBUTIONS}_interaction ON public.{ATTRIBUTIONS} "
                                          "USING btree (campaign_id, user_id, interaction_key)",
        f"ix_{ATTRIBUTIONS}_visit": f"CREATE INDEX ix_{ATTRIBUTIONS}_visit ON public.{ATTRIBUTIONS} USING btree "
                                    "(visit_id)",
        f"{REGISTRY}_pkey": f"CREATE UNIQUE INDEX {REGISTRY}_pkey ON public.{REGISTRY} USING btree (id)",
        f"uq_{REGISTRY}_operation": f"CREATE UNIQUE INDEX uq_{REGISTRY}_operation ON public.{REGISTRY} USING btree "
                                    "(user_id, source_operation_kind, source_operation_id)",
        f"uq_{REGISTRY}_recorded_event": f"CREATE UNIQUE INDEX uq_{REGISTRY}_recorded_event ON public.{REGISTRY} "
                                         "USING btree (recorded_event_id)",
        f"ix_{REGISTRY}_campaign_status": f"CREATE INDEX ix_{REGISTRY}_campaign_status ON public.{REGISTRY} USING "
                                          "btree (campaign_id, status)",
        f"ix_{REGISTRY}_interaction": f"CREATE INDEX ix_{REGISTRY}_interaction ON public.{REGISTRY} USING btree "
                                      "(campaign_id, user_id, interaction_key)",
        f"ix_{REGISTRY}_visit": f"CREATE INDEX ix_{REGISTRY}_visit ON public.{REGISTRY} USING btree (visit_id)",
        f"ix_{REGISTRY}_purge_after_at": f"CREATE INDEX ix_{REGISTRY}_purge_after_at ON public.{REGISTRY} USING "
                                         "btree (purge_after_at) WHERE (purge_after_at IS NOT NULL)",
    }


def _seed_blocking(engine, table) -> None:
    with engine.begin() as conn:
        w = _world(conn)
        origin = _origin(w)
        if table == REGISTRY:
            conn.execute(sa.text(REGISTRY_INSERT), _proof(origin))
        else:
            conn.execute(sa.text(ATTRIBUTION_INSERT), _attribution(origin, _matching_event(conn, origin)))


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("table", [REGISTRY, ATTRIBUTIONS])
def test_pg_downgrade_is_refused_when_a_new_table_has_data(pg_url, pg_engine, table, offline):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    _seed_blocking(pg_engine, table)
    tables = _tables(pg_engine)
    before, catalog = _data(pg_engine, tables - {"alembic_version"}), _catalog(pg_engine, tables)
    assert before[table] and (table == REGISTRY or not before[REGISTRY])
    expected = f"downgrade de 0024_product_activity_proofs refusé, la table {table} contient des données"
    if offline:
        statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{V0T08_0024}:{V0T08_0023}", "--sql").stdout)
        with pg_engine.connect() as conn:
            with pytest.raises(sa.exc.DBAPIError, match=expected):
                with conn.begin():
                    for statement in statements:  # script complet exécuté : la garde arrête tout
                        conn.exec_driver_sql(statement)
    else:
        with pytest.raises(Exception) as failure:
            _run_alembic(pg_url, "downgrade", V0T08_0023)
        assert expected in str(getattr(failure.value, "stderr", failure.value))
    assert _version(pg_engine) == V0T08_0024
    assert _data(pg_engine, tables - {"alembic_version"}) == before
    assert _catalog(pg_engine, tables) == catalog


def test_pg_offline_guard_passes_when_both_tables_are_empty_even_with_0023_data(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0023)
    _seed_0023(pg_engine)
    tables = _tables(pg_engine)
    data, catalog = _data(pg_engine, tables - {"alembic_version"}), _catalog(pg_engine, tables)
    up = _run_alembic(OFFLINE, "upgrade", f"{V0T08_0023}:{V0T08_0024}", "--sql").stdout
    down = _run_alembic(OFFLINE, "downgrade", f"{V0T08_0024}:{V0T08_0023}", "--sql").stdout
    with pg_engine.begin() as conn:
        for statement in _statements(up):
            conn.exec_driver_sql(statement)
    assert _version(pg_engine) == V0T08_0024 and _compare_metadata(pg_engine) == []
    offline_catalog = _catalog(pg_engine, tables | NEW)
    with pg_engine.begin() as conn:
        for statement in _statements(down):
            conn.exec_driver_sql(statement)
    assert _version(pg_engine) == V0T08_0023 and _catalog(pg_engine, tables) == catalog
    assert _data(pg_engine, tables - {"alembic_version"}) == data
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    assert _catalog(pg_engine, tables | NEW) == offline_catalog  # en ligne et offline : même catalogue


def test_pg_concurrent_inserts_of_the_same_canonical_identity(pg_url, pg_engine):  # noqa: F811
    """Deux transactions concurrentes (deux onglets / appareils) déclarent la
    même opération canonique : la seconde attend la première puis est
    refusée par l'UNIQUE ; une seule preuve subsiste."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    with pg_engine.begin() as conn:
        w = _world(conn)
    operation = uuid.uuid4()
    rows = [_proof(_origin(w, source_operation_id=operation)),
            _proof(_origin(w, visit_id=w.a2, interaction_key=w.k_a2, source_operation_id=operation))]
    first = pg_engine.connect()
    first_tx = first.begin()
    first.execute(sa.text(REGISTRY_INSERT), rows[0])
    outcome, started = {}, threading.Event()

    def second():
        with pg_engine.connect() as conn:
            try:
                with conn.begin():
                    conn.execute(sa.text("SET LOCAL statement_timeout = '20s'"))
                    started.set()
                    conn.execute(sa.text(REGISTRY_INSERT), rows[1])
                outcome["result"] = "inserted"
            except sa.exc.IntegrityError as exc:
                outcome["result"] = str(exc.orig)

    thread = threading.Thread(target=second)
    thread.start()
    started.wait(10)
    deadline, waiting = time.monotonic() + 10, 0
    with pg_engine.connect() as probe:  # la seconde insertion attend le verrou de la première
        while time.monotonic() < deadline and not waiting:
            waiting = probe.execute(sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND query LIKE 'INSERT INTO product_activity_proof_registry%'")).scalar_one()
            probe.rollback()
            time.sleep(0.05)
    assert waiting == 1 and thread.is_alive()
    first_tx.commit()
    first.close()
    thread.join(30)
    assert not thread.is_alive()
    assert f"uq_{REGISTRY}_operation" in outcome["result"]
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text(f"SELECT id FROM {REGISTRY}")).scalars().all() == [rows[0]["id"]]
