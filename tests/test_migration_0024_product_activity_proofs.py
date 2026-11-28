"""Tests de V0-T08 Lot 2B-2, bloc A : migration 0024_product_activity_proofs
(registre provisoire des preuves et attributions définitives DORMANTS) et
modèles correspondants.

1. Sans base : maillon 0023 -> 0024 (tête unique, 24 fichiers), identifiant
   <= 32 caractères, 0022 et 0023 inchangées, SQL offline exact (upgrade :
   trois ADD CONSTRAINT UNIQUE, deux CREATE TABLE et cinq index, rien
   d'autre ; downgrade : délais, verrou déterministe, garde DO évaluée à
   l'exécution, retrait sans CASCADE), modèles alignés sur la migration
   (colonnes, types, nullabilité, CHECK, UNIQUE, FK, index), vocabulaires
   fermés (natures d'opération = celles de la PR #250), aucun champ
   personnel ni de contenu, dormance (aucun module de production ne
   mentionne les nouvelles tables).
2. PostgreSQL réel (base jetable) : montée 0023 -> 0024 sur données
   historiques, F2 et analytics 0022 / 0023 conservées à l'identique, aucun
   backfill, catalogue exact (seules les trois UNIQUE et leurs index sont
   ajoutés aux tables existantes), aucun trigger / fonction / ENUM /
   extension ; identités croisées, activity / close, visites, opérations
   canoniques, concordance événement / attribution / preuve, formes d'état,
   conservation de 168 heures absolues (changements d'heure compris),
   motifs fermés, ordre de suppression sans cascade — chaque refus étant
   démontré par la contrainte SQL attendue, puis par CHACUNE des
   contraintes concernées seule ; usages légitimes 0022 / 0023 intacts ;
   concurrence ; rollback ; downgrade exact vers 0023 si les deux tables
   sont vides, REFUSÉ en ligne et par la garde offline exécutée sinon.

Les FK prouvent l'identité et le rattachement, jamais la réussite métier
(source, outcome, livraison, instant admissible : services B/C).
"""
import ast
import hashlib
import importlib.util
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from core.authz_f2 import attestations
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
    _extensions,
    _insert_row,
    _participant,
    _scopes,
    _seed_0021_history,
)
from tests.test_product_analytics_dormancy import (
    MIGRATION_0022_SHA256,
    MIGRATION_0023_SHA256,
    PACKAGE,
)

MIGRATION = REPO_ROOT / "alembic" / "versions" / V0T08_0024_FILE
ATTRIBUTIONS, REGISTRY = V0T08_0024_TABLES
NEW = set(V0T08_0024_TABLES)
EVENTS = "product_activity_events"
SESSIONS = "product_activity_sessions"
INTERACTIONS = "product_activity_session_interactions"
PARTICIPANTS = "product_campaign_participants"
ALTERED = {EVENTS, SESSIONS, INTERACTIONS}
TS = "timestamp with time zone"
VC = "character varying"
IDENTITY_COLUMNS = [("campaign_id", "uuid", False), ("user_id", VC, False), ("visit_id", "uuid", False),
                    ("interaction_key", "uuid", False), ("interaction_kind", f"{VC}(16)", False),
                    ("event_type", f"{VC}(32)", False), ("surface", f"{VC}(32)", False),
                    ("source_operation_kind", f"{VC}(64)", False), ("source_operation_id", "uuid", False)]
COLUMNS = {
    ATTRIBUTIONS: [("event_id", "uuid", False), *IDENTITY_COLUMNS, ("certified_at", TS, False)],
    REGISTRY: [("id", "uuid", False), *IDENTITY_COLUMNS, ("status", f"{VC}(24)", False), ("created_at", TS, False),
               ("certified_at", TS, True), ("resolved_at", TS, True), ("recorded_event_id", "uuid", True),
               ("resolution_reason", f"{VC}(64)", True), ("purge_after_at", TS, True)],
}
PORTFOLIO_KINDS = (attestations.PORTFOLIO_CREATE, attestations.PORTFOLIO_UPDATE, attestations.PORTFOLIO_DELETE)
RETENTION = timedelta(hours=168)


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
    assert len(files) == 24 and files[-3:] == [V0T08_0022_FILE, V0T08_0023_FILE, V0T08_0024_FILE]
    m = _m0024()
    assert (m.revision, m.down_revision, m.branch_labels, m.depends_on) == (V0T08_0024, V0T08_0023, None, None)


def test_migrations_0022_and_0023_are_unchanged():
    versions = REPO_ROOT / "alembic" / "versions"
    assert hashlib.sha256((versions / V0T08_0022_FILE).read_bytes()).hexdigest() == MIGRATION_0022_SHA256
    assert hashlib.sha256((versions / V0T08_0023_FILE).read_bytes()).hexdigest() == MIGRATION_0023_SHA256


def test_offline_upgrade_adds_three_uniques_two_tables_and_their_indexes_only():
    statements = _statements(_run_alembic(OFFLINE, "upgrade", f"{V0T08_0023}:{V0T08_0024}", "--sql").stdout)
    m = _m0024()
    assert statements[:3] == GUARDS
    assert statements[3:6] == [
        "ALTER TABLE product_activity_events ADD CONSTRAINT uq_product_activity_events_attribution_identity "
        "UNIQUE (id, user_id, visit_id, event_type, surface, source_operation_kind, source_operation_id)",
        "ALTER TABLE product_activity_sessions ADD CONSTRAINT uq_product_activity_sessions_owner "
        "UNIQUE (id, campaign_id, user_id)",
        "ALTER TABLE product_activity_session_interactions ADD CONSTRAINT "
        "uq_product_activity_session_interactions_origin UNIQUE (campaign_id, user_id, interaction_key, session_id, "
        "interaction_kind)",
    ]
    creates = [s for s in statements if s.startswith("CREATE TABLE")]
    assert [s.split(" ")[2] for s in creates] == [ATTRIBUTIONS, REGISTRY]
    indexes = [s for s in statements if s.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX"))]
    assert sorted(s.split(" INDEX ")[1].split(" ")[0] for s in indexes) == sorted(V0T08_0024_INDEXES)
    assert not [s for s in indexes if s.startswith("CREATE UNIQUE INDEX")]
    assert statements[-1] == (f"UPDATE alembic_version SET version_num='{V0T08_0024}' "
                              f"WHERE alembic_version.version_num = '{V0T08_0023}'")
    assert len(statements) == 3 + 3 + len(creates) + len(indexes) + 1
    by_table = dict(zip((ATTRIBUTIONS, REGISTRY), creates))
    for table, checks in ((ATTRIBUTIONS, m.ATTRIBUTION_CHECKS), (REGISTRY, m.REGISTRY_CHECKS)):
        for name, expression in checks:
            assert f"CONSTRAINT {name} CHECK ({expression})" in by_table[table], name
    for table, fragment in (
            (ATTRIBUTIONS, "CONSTRAINT product_activity_event_attributions_pkey PRIMARY KEY (event_id)"),
            (ATTRIBUTIONS, "CONSTRAINT product_activity_event_attributions_participant_fkey FOREIGN KEY(campaign_id, "
                           "user_id) REFERENCES product_campaign_participants (campaign_id, user_id)"),
            (ATTRIBUTIONS, "CONSTRAINT product_activity_event_attributions_session_fkey FOREIGN KEY(visit_id, "
                           "campaign_id, user_id) REFERENCES product_activity_sessions (id, campaign_id, user_id)"),
            (ATTRIBUTIONS, "CONSTRAINT product_activity_event_attributions_interaction_fkey FOREIGN KEY(campaign_id, "
                           "user_id, interaction_key, visit_id, interaction_kind) REFERENCES "
                           "product_activity_session_interactions (campaign_id, user_id, interaction_key, session_id, "
                           "interaction_kind)"),
            (ATTRIBUTIONS, "CONSTRAINT product_activity_event_attributions_event_fkey FOREIGN KEY(event_id, user_id, "
                           "visit_id, event_type, surface, source_operation_kind, source_operation_id) REFERENCES "
                           "product_activity_events (id, user_id, visit_id, event_type, surface, "
                           "source_operation_kind, source_operation_id)"),
            (ATTRIBUTIONS, "CONSTRAINT uq_product_activity_event_attributions_source_operation UNIQUE (user_id, "
                           "source_operation_kind, source_operation_id)"),
            (REGISTRY, "CONSTRAINT product_activity_proof_registry_pkey PRIMARY KEY (id)"),
            (REGISTRY, "CONSTRAINT product_activity_proof_registry_interaction_fkey FOREIGN KEY(campaign_id, "
                       "user_id, interaction_key, visit_id, interaction_kind) REFERENCES "
                       "product_activity_session_interactions (campaign_id, user_id, interaction_key, session_id, "
                       "interaction_kind)"),
            (REGISTRY, "CONSTRAINT product_activity_proof_registry_attribution_fkey FOREIGN KEY(recorded_event_id, "
                       "campaign_id, user_id, visit_id, interaction_key, event_type, surface, source_operation_kind, "
                       "source_operation_id) REFERENCES product_activity_event_attributions (event_id, campaign_id, "
                       "user_id, visit_id, interaction_key, event_type, surface, source_operation_kind, "
                       "source_operation_id)"),
            (REGISTRY, "CONSTRAINT uq_product_activity_proof_registry_source_operation UNIQUE (user_id, "
                       "source_operation_kind, source_operation_id)")):
        assert fragment in by_table[table], fragment
    # Aucune FK du registre ou des attributions vers une table F2 ; aucune FK
    # des attributions vers le registre (aucune dépendance inverse).
    assert "REFERENCES product_activity_proof_registry" not in " ".join(statements)
    upper = " ".join(statements).upper()
    for forbidden in ("TRIGGER", "FUNCTION", "CREATE TYPE", "ENUM", "EXTENSION", "DROP", "CASCADE", "DEFERRABLE",
                      "INSERT INTO", "DELETE FROM", "DEFAULT", "SEQUENCE", "SERIAL", "ON DELETE", "ON UPDATE",
                      "COMMENT ON", "REFERENCES USERS", "AUTH_", "INVITATIONS", "ORYX_SESSION", "PORTFOLIO_POSITIONS",
                      "ASSISTANT_DELIVERIES", "ALTER COLUMN", "ADD COLUMN", "UPDATE PRODUCT"):
        assert forbidden not in upper, forbidden
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
        f"ALTER TABLE {INTERACTIONS} DROP CONSTRAINT uq_product_activity_session_interactions_origin",
        f"ALTER TABLE {SESSIONS} DROP CONSTRAINT uq_product_activity_sessions_owner",
        f"ALTER TABLE {EVENTS} DROP CONSTRAINT uq_product_activity_events_attribution_identity",
        f"UPDATE alembic_version SET version_num='{V0T08_0023}' WHERE alembic_version.version_num = '{V0T08_0024}'",
    ]


def test_models_match_the_migration():
    m = _m0024()
    pg_types = {"UUID": "uuid", "VARCHAR": VC, "TIMESTAMP WITH TIME ZONE": TS}
    tables = {name: Base.metadata.tables[name] for name in V0T08_0024_TABLES}
    for table_name, columns in COLUMNS.items():
        described = []
        for column in tables[table_name].c:
            base, _, length = column.type.compile(dialect=postgresql.dialect()).partition("(")
            described.append((column.name, pg_types[base] + (f"({length}" if length else ""), column.nullable))
            assert column.server_default is None and column.default is None, column.name
            assert column.onupdate is None and column.server_onupdate is None, column.name
        assert described == columns, table_name
    for table_name, expected in ((ATTRIBUTIONS, m.ATTRIBUTION_CHECKS), (REGISTRY, m.REGISTRY_CHECKS)):
        checks = {c.name: str(c.sqltext) for c in tables[table_name].constraints if isinstance(c, sa.CheckConstraint)}
        assert checks == dict(expected), table_name
    assert [c.name for c in tables[ATTRIBUTIONS].primary_key.columns] == ["event_id"]
    assert [c.name for c in tables[REGISTRY].primary_key.columns] == ["id"]
    # Les modèles du dépôt ne nomment pas leurs PK : le nom PostgreSQL par
    # défaut <table>_pkey est celui de la migration (vérifié au catalogue).
    assert (m.ATTRIBUTION_PRIMARY_KEY, m.REGISTRY_PRIMARY_KEY) == (f"{ATTRIBUTIONS}_pkey", f"{REGISTRY}_pkey")
    uniques = {c.name: [col.name for col in c.columns] for t in tables.values() for c in t.constraints
               if isinstance(c, sa.UniqueConstraint)}
    assert uniques == {
        m.ATTRIBUTION_SOURCE_OPERATION_UNIQUE: ["user_id", "source_operation_kind", "source_operation_id"],
        m.ATTRIBUTION_PROOF_IDENTITY_UNIQUE: list(m.ATTRIBUTION_PROOF_IDENTITY_COLUMNS),
        m.REGISTRY_SOURCE_OPERATION_UNIQUE: ["user_id", "source_operation_kind", "source_operation_id"],
    }
    fks = {fk.name: ([e.parent.name for e in fk.elements], [e.target_fullname for e in fk.elements],
                     fk.ondelete, fk.onupdate, fk.deferrable, fk.initially)
           for t in tables.values() for fk in t.foreign_key_constraints}
    assert fks == {
        m.ATTRIBUTION_PARTICIPANT_FK: (["campaign_id", "user_id"], m.PARTICIPANT_TARGET, None, None, None, None),
        m.ATTRIBUTION_SESSION_FK: (m.SESSION_SOURCE, m.SESSION_TARGET, None, None, None, None),
        m.ATTRIBUTION_INTERACTION_FK: (m.INTERACTION_SOURCE, m.INTERACTION_TARGET, None, None, None, None),
        m.ATTRIBUTION_EVENT_FK: (m.EVENT_SOURCE, m.EVENT_TARGET, None, None, None, None),
        m.REGISTRY_PARTICIPANT_FK: (["campaign_id", "user_id"], m.PARTICIPANT_TARGET, None, None, None, None),
        m.REGISTRY_SESSION_FK: (m.SESSION_SOURCE, m.SESSION_TARGET, None, None, None, None),
        m.REGISTRY_INTERACTION_FK: (m.INTERACTION_SOURCE, m.INTERACTION_TARGET, None, None, None, None),
        m.REGISTRY_ATTRIBUTION_FK: (list(m.REGISTRY_RECORDED_COLUMNS), m.ATTRIBUTION_TARGET, None, None, None, None),
    }
    # La FK vers l'interaction réfère session_id de l'interaction par visit_id.
    assert dict(zip(m.INTERACTION_SOURCE, m.INTERACTION_TARGET))["visit_id"] == f"{INTERACTIONS}.session_id"
    indexes = {index.name: ([e.name for e in index.expressions], index.unique,
                            None if index.dialect_options["postgresql"]["where"] is None
                            else str(index.dialect_options["postgresql"]["where"]))
               for t in tables.values() for index in t.indexes}
    assert indexes == {
        m.ATTRIBUTION_VISIT_INDEX: (["campaign_id", "user_id", "visit_id"], False, None),
        m.REGISTRY_CAMPAIGN_STATUS_INDEX: (["campaign_id", "status"], False, None),
        m.REGISTRY_VISIT_INDEX: (["campaign_id", "user_id", "visit_id"], False, None),
        m.REGISTRY_RECORDED_EVENT_INDEX: (["recorded_event_id"], False, m.REGISTRY_RECORDED_EVENT_WHERE),
        m.REGISTRY_PURGE_INDEX: (["purge_after_at"], False, m.REGISTRY_PURGE_WHERE),
    }
    assert set(indexes) == V0T08_0024_INDEXES
    # Les trois UNIQUE ajoutées aux modèles existants sont exactement celles de la migration.
    existing = {name: (table, list(columns), created) for table, name, columns in m.EXISTING_UNIQUES
                for created in [{EVENTS: 8, SESSIONS: 9, INTERACTIONS: 9}[table]]}
    assert existing == V0T08_0024_EXISTING_UNIQUES
    for name, (table, columns, _) in existing.items():
        found = [c for c in Base.metadata.tables[table].constraints if isinstance(c, sa.UniqueConstraint)
                 and c.name == name]
        assert [[col.name for col in c.columns] for c in found] == [columns], name
    # user_id : exactement le type de users.id (String, jamais converti en UUID).
    assert type(Base.metadata.tables["users"].c.id.type) is sa.String
    for table in tables.values():
        assert type(table.c.user_id.type) is sa.String and table.c.user_id.type.length is None


def test_closed_vocabularies_match_the_contract_and_the_f2_attestations():
    m = _m0024()
    assert list(m.PROOF_STATUSES) == ["pending", "certified", "recorded", "rejected", "indeterminate"]
    assert list(m.RESOLUTION_REASONS) == ["no_new_effect", "source_unverified", "origin_unverified", "owner_conflict",
                                          "visit_conflict", "operation_conflict", "window_expired",
                                          "finalization_expired", "not_authorized", "technical_uncertainty"]
    assert all(len(r) <= 64 and r.replace("_", "").isalpha() and r.islower() for r in m.RESOLUTION_REASONS)
    # Natures canoniques : celles des attestations F2 (PR #250), jamais regroupées.
    assert m.OPERATION_RULES == (
        ("education_used", "education", ("assistant_delivery",)),
        ("coach_used", "coach", ("assistant_delivery",)),
        ("decrypter_used", "decrypter", (attestations.DECRYPTAGE_DELIVERY_ACK,)),
        ("portfolio_used", "portfolio", PORTFOLIO_KINDS),
    )
    assert "portfolio_write" not in MIGRATION.read_text(encoding="utf-8")
    checks = dict(m.REGISTRY_CHECKS)
    assert checks["ck_product_activity_proof_registry_status"] == (
        f"status IN ({', '.join(repr(s) for s in m.PROOF_STATUSES)})")
    assert checks["ck_product_activity_proof_registry_resolution_reason"] == (
        f"resolution_reason IS NULL OR resolution_reason IN ({', '.join(repr(r) for r in m.RESOLUTION_REASONS)})")
    rules = " OR ".join(
        f"(event_type = '{t}' AND surface = '{s}' AND source_operation_kind "
        + (f"= '{kinds[0]}')" if len(kinds) == 1 else f"IN ({', '.join(repr(k) for k in kinds)}))")
        for t, s, kinds in m.OPERATION_RULES)
    assert m.OPERATION_EXPRESSION == rules
    assert dict(m.ATTRIBUTION_CHECKS)["ck_product_activity_event_attributions_operation"] == rules
    assert m.REGISTRY_RETENTION == "168 hours" and "interval '168 hours'" in checks[
        "ck_product_activity_proof_registry_purge_after"]
    assert "days" not in checks["ck_product_activity_proof_registry_purge_after"]


def test_no_personal_or_content_field_in_the_new_tables():
    for table_name, columns in COLUMNS.items():
        for name, _, _ in columns:
            parts = set(name.split("_"))
            for marker in FORBIDDEN_FIELD_MARKERS + ("name", "hash", "note", "comment", "detail", "label", "reason_text",
                                                     "route", "outcome"):
                assert marker not in parts and not name.startswith(marker), (table_name, name)
    assert {t for t in Base.metadata.tables if t.startswith("product_")} >= NEW


DORMANCY_MARKERS = (REGISTRY, ATTRIBUTIONS, "ProductActivityProof", "ProductActivityEventAttribution",
                    "uq_product_activity_sessions_owner", "uq_product_activity_session_interactions_origin",
                    "uq_product_activity_events_attribution_identity", "recorded_event_id")


# V0-T08 Lot 2B-2 bloc B : seul service autorisé à connaître le registre.
PROOF_SERVICE = PACKAGE / "proofs.py"
PROOF_SERVICE_MARKERS = [REGISTRY, "ProductActivityProof", "recorded_event_id"]
# V0-T08 Lot 2B-2 bloc C : seul service autorisé à connaître les attributions
# (finalisation atomique événement / attribution / preuve recorded).
FINALIZATION_SERVICE = PACKAGE / "finalization.py"
FINALIZATION_SERVICE_MARKERS = [REGISTRY, ATTRIBUTIONS, "ProductActivityProof", "ProductActivityEventAttribution",
                                "recorded_event_id"]
# V0-T08 Lot 2C PR 1 : sélection bornée et purge du registre ; ne connaît QUE le
# registre (jamais les attributions, recorded_event_id ni les UNIQUE).
LIFECYCLE_SERVICE = PACKAGE / "proof_lifecycle.py"
LIFECYCLE_SERVICE_MARKERS = [REGISTRY, "ProductActivityProof"]
# V0-T08 Lot 2C PR 2 : suppression de l'événement, de son attribution et de la
# preuve recorded qui la référence ; jamais les UNIQUE cibles des FK.
EVENT_LIFECYCLE_SERVICE = PACKAGE / "event_lifecycle.py"
EVENT_LIFECYCLE_SERVICE_MARKERS = ["ProductActivityProof", "ProductActivityEventAttribution", "recorded_event_id"]
# V0-T08 Lot 2C PR 3 : constate (EXISTS) les preuves et attributions d'une
# participation, sans jamais les verrouiller, les modifier ni les supprimer ;
# jamais recorded_event_id ni les UNIQUE cibles des FK.
PARTICIPATION_LIFECYCLE_SERVICE = PACKAGE / "participation_lifecycle.py"
PARTICIPATION_LIFECYCLE_SERVICE_MARKERS = ["ProductActivityProof", "ProductActivityEventAttribution"]


def test_dormancy_no_production_module_knows_the_new_tables():
    """Seuls core/models.py (déclaration) et la migration 0024 mentionnent
    les nouvelles tables : aucun service (core/product_analytics/ compris),
    aucune route, aucun worker, aucun frontend. Bloc B (nominatif) :
    core/product_analytics/proofs.py connaît le registre, jamais les
    attributions. Les nouveaux modèles ne
    réfèrent qu'à des tables analytiques (aucune table F2)."""
    allowed = {REPO_ROOT / "core" / "models.py", MIGRATION}
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).parts
        if relative[0] in ("tests", ".git") or "node_modules" in relative or path in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        if path == PROOF_SERVICE:
            # V0-T08 2B-2 bloc B (nominatif) : le service du registre ne connaît
            # QUE le registre ; ni attribution, ni table d'événements, ni UNIQUE.
            assert [m for m in DORMANCY_MARKERS if m in source] == PROOF_SERVICE_MARKERS
            continue
        if path == FINALIZATION_SERVICE:
            # V0-T08 2B-2 bloc C (nominatif) : registre et attributions ;
            # jamais les UNIQUE cibles des FK (dernière défense, inchangée).
            assert [m for m in DORMANCY_MARKERS if m in source] == FINALIZATION_SERVICE_MARKERS
            continue
        if path == LIFECYCLE_SERVICE:
            # V0-T08 2C PR 1 (nominatif) : le registre seul.
            assert [m for m in DORMANCY_MARKERS if m in source] == LIFECYCLE_SERVICE_MARKERS
            continue
        if path == EVENT_LIFECYCLE_SERVICE:
            # V0-T08 2C PR 2 (nominatif) : modèles du registre et des attributions.
            assert [m for m in DORMANCY_MARKERS if m in source] == EVENT_LIFECYCLE_SERVICE_MARKERS
            continue
        if path == PARTICIPATION_LIFECYCLE_SERVICE:
            # V0-T08 2C PR 3 (nominatif) : modèles du registre et des attributions (EXISTS seulement).
            assert [m for m in DORMANCY_MARKERS if m in source] == PARTICIPATION_LIFECYCLE_SERVICE_MARKERS
            continue
        if any(marker in source for marker in DORMANCY_MARKERS):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []
    for directory in ("web", "web-v2"):
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if path.is_file() and path.suffix in (".html", ".js", ".mjs", ".css", ".json"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                assert not any(marker in text for marker in DORMANCY_MARKERS[:4]), path
    assert not [p for p in PACKAGE.glob("*.py") if p not in (PROOF_SERVICE, FINALIZATION_SERVICE,
                                                             LIFECYCLE_SERVICE, EVENT_LIFECYCLE_SERVICE,
                                                             PARTICIPATION_LIFECYCLE_SERVICE) and any(
        marker in p.read_text(encoding="utf-8") for marker in DORMANCY_MARKERS)]
    for name in V0T08_0024_TABLES:
        targets = {e.column.table.name for fk in Base.metadata.tables[name].foreign_key_constraints
                   for e in fk.elements}
        assert targets <= {PARTICIPANTS, SESSIONS, INTERACTIONS, EVENTS, ATTRIBUTIONS}, (name, targets)
    # La migration n'importe qu'Alembic et SQLAlchemy (aucun module applicatif ni F2).
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {"typing", "alembic", "sqlalchemy"}


# --------------------------------------------------------------------------
# 2. PostgreSQL réel
# --------------------------------------------------------------------------

USER_A, USER_B, USER_C = "v0-proof-a", "v0-proof-b", "v0-proof-c"
T = BASE_TIME
D1, D2 = "1" * 64, "2" * 64  # empreintes de deux appareils (navigateurs) distincts


def _session(conn, campaign, user, digest, *, opened=T) -> uuid.UUID:
    session = uuid.uuid4()
    conn.execute(sa.text(
        "INSERT INTO product_activity_sessions (id, campaign_id, user_id, environment_digest, opened_at, "
        "last_activity_at, idle_expires_at, finalized_at, end_reason) VALUES (:id, :c, :u, :d, :o, :o, :x, NULL, "
        "NULL)"), {"id": session, "c": campaign, "u": user, "d": digest, "o": opened, "x": opened + timedelta(minutes=30)})
    return session


def _interaction(conn, campaign, user, kind, digest, session, *, at=T) -> uuid.UUID:
    key = uuid.uuid4()
    conn.execute(sa.text(
        "INSERT INTO product_activity_session_interactions (campaign_id, user_id, interaction_key, interaction_kind, "
        "environment_digest, session_id, recorded_at) VALUES (:c, :u, :k, :kind, :d, :s, :t)"),
        {"c": campaign, "u": user, "k": key, "kind": kind, "d": digest, "s": session, "t": at})
    return key


def _world(conn) -> SimpleNamespace:
    """Deux campagnes X et Y ; A participe à X et Y, B à X, C à aucune ; A a
    deux appareils dans X ; clés d'interaction 'activity' et 'close'."""
    for user in (USER_A, USER_B, USER_C):
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant')"), {"u": user})
    w = SimpleNamespace()
    w.x = _campaign(conn, created_at=T - timedelta(days=2), close_by_at=T + timedelta(days=30))["id"]
    w.y = _campaign(conn, created_at=T - timedelta(days=2), close_by_at=T + timedelta(days=30))["id"]
    for campaign, user in ((w.x, USER_A), (w.x, USER_B), (w.y, USER_A)):
        _participant(conn, campaign, user_id=user, window_start_at=T - timedelta(days=1),
                     window_end_at=T + timedelta(days=11), enrolled_at=T - timedelta(days=1))
    w.sa1 = _session(conn, w.x, USER_A, D1)
    w.sa2 = _session(conn, w.x, USER_A, D2)
    w.sb1 = _session(conn, w.x, USER_B, D1)
    w.say = _session(conn, w.y, USER_A, D1)
    w.ka1 = _interaction(conn, w.x, USER_A, "activity", D1, w.sa1)
    w.ka1b = _interaction(conn, w.x, USER_A, "activity", D1, w.sa1)
    w.ka2 = _interaction(conn, w.x, USER_A, "activity", D2, w.sa2)
    w.ka1_close = _interaction(conn, w.x, USER_A, "close", D1, w.sa1)
    w.ka_close_alone = _interaction(conn, w.x, USER_A, "close", D1, None)
    w.kb1 = _interaction(conn, w.x, USER_B, "activity", D1, w.sb1)
    w.kay = _interaction(conn, w.y, USER_A, "activity", D1, w.say)
    return w


def _proof(conn, w, **overrides) -> dict:
    values = {"id": uuid.uuid4(), "campaign_id": w.x, "user_id": USER_A, "visit_id": w.sa1,
              "interaction_key": w.ka1, "interaction_kind": "activity", "event_type": "education_used",
              "surface": "education", "source_operation_kind": "assistant_delivery",
              "source_operation_id": uuid.uuid4(), "status": "pending", "created_at": T + timedelta(minutes=1),
              "certified_at": None, "resolved_at": None, "recorded_event_id": None, "resolution_reason": None,
              "purge_after_at": None}
    values.update(overrides)
    _insert_row(conn, REGISTRY, values)
    return values


def _resolved(status, at, **overrides) -> dict:
    values = {"status": status, "resolved_at": at, "purge_after_at": at + RETENTION}
    if status == "recorded":
        values["certified_at"] = at
    else:
        values["resolution_reason"] = "technical_uncertainty" if status == "indeterminate" else "source_unverified"
    values.update(overrides)
    return values


def _event_for(conn, values, **overrides) -> dict:
    """Événement concordant avec une attribution (mêmes propriétaire, visite,
    type, surface et opération) ; overrides le rend discordant."""
    fields = {"id": values["event_id"], "user_id": values["user_id"], "visit_id": values["visit_id"],
              "event_type": values["event_type"], "surface": values["surface"], "source": "server_business",
              "source_operation_kind": values["source_operation_kind"],
              "source_operation_id": values["source_operation_id"], "occurred_at": T, "recorded_at": T,
              "updated_at": T, "resolved_at": T, "retention_scopes": _scopes(values["campaign_id"]),
              "next_scope_expires_at": T + timedelta(days=90), "purge_after_at": T + timedelta(days=90)}
    fields.update(overrides)
    return _event(conn, **fields)


def _attribution(conn, w, *, event=True, event_overrides=None, **overrides) -> dict:
    values = {"event_id": uuid.uuid4(), "campaign_id": w.x, "user_id": USER_A, "visit_id": w.sa1,
              "interaction_key": w.ka1, "interaction_kind": "activity", "event_type": "education_used",
              "surface": "education", "source_operation_kind": "assistant_delivery",
              "source_operation_id": uuid.uuid4(), "certified_at": T + timedelta(minutes=2)}
    values.update(overrides)
    if event:
        _event_for(conn, values, **(event_overrides or {}))
    _insert_row(conn, ATTRIBUTIONS, values)
    return values


def _quoted(name: str) -> str:
    return f'"{name}"'


def _rejects_each(conn, table, names, statement):
    """L'écriture est refusée avec toutes les contraintes en place par l'une
    des contraintes nommées, puis par CHACUNE d'elles seule (les autres
    retirées le temps d'un SAVEPOINT : le DDL PostgreSQL est transactionnel)
    : aucune garantie ne dépend d'une autre."""
    _rejects(conn, "|".join(_quoted(n) for n in names), statement)
    if len(names) == 1:
        return
    for name in names:
        with conn.begin_nested() as savepoint:
            for other in names:
                if other != name:
                    conn.execute(sa.text(f"ALTER TABLE {table} DROP CONSTRAINT {other}"))
            _rejects(conn, _quoted(name), statement)
            savepoint.rollback()


def _head_world(pg_url, pg_engine):
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    conn = pg_engine.connect()
    return conn, _world(conn)


REGISTRY_FK = {kind: f"product_activity_proof_registry_{kind}_fkey" for kind in ("participant", "session",
                                                                                   "interaction", "attribution")}
ATTRIBUTION_FK = {kind: f"product_activity_event_attributions_{kind}_fkey" for kind in ("participant", "session",
                                                                                         "interaction", "event")}


def _writer(conn, w, table):
    if table == REGISTRY:
        return lambda **o: _proof(conn, w, **o)
    return lambda **o: _attribution(conn, w, **o)


@pytest.mark.parametrize("table", [REGISTRY, ATTRIBUTIONS])
def test_pg_identity_and_visit_binding_is_enforced_by_postgresql(pg_url, pg_engine, table):  # noqa: F811
    """Cas 1 à 10 du contrat, pour le registre ET pour les attributions :
    chaque refus est une contrainte SQL (aucune validation Python)."""
    conn, w = _head_world(pg_url, pg_engine)
    write = _writer(conn, w, table)
    fk = REGISTRY_FK if table == REGISTRY else ATTRIBUTION_FK
    try:
        # 1. Preuve de A avec la visite de B.
        _rejects_each(conn, table, [fk["session"], fk["interaction"]], lambda: write(visit_id=w.sb1))
        _rejects_each(conn, table, [fk["session"], fk["interaction"]],
                      lambda: write(visit_id=w.sb1, interaction_key=w.kb1))
        # 2. Campagne X avec une visite de la campagne Y (même utilisateur).
        _rejects_each(conn, table, [fk["session"], fk["interaction"]],
                      lambda: write(visit_id=w.say, interaction_key=w.kay))
        _rejects_each(conn, table, [fk["session"], fk["interaction"]], lambda: write(visit_id=w.say))
        # 3. Interaction 'activity' d'une autre visite (même utilisateur, même campagne).
        _rejects_each(conn, table, [fk["interaction"]], lambda: write(visit_id=w.sa2, interaction_key=w.ka1))
        # 4. Interaction 'close' utilisée comme preuve : refusée par la FK
        #    (type 'activity' exigé de l'interaction), et par le CHECK si la
        #    ligne se déclare 'close' ; clôture sans visite refusée aussi.
        _rejects_each(conn, table, [fk["interaction"]], lambda: write(interaction_key=w.ka1_close))
        check = (f"ck_{table}_interaction_kind")
        _rejects(conn, _quoted(check), lambda: write(interaction_key=w.ka1_close, interaction_kind="close"))
        _rejects_each(conn, table, [fk["interaction"]], lambda: write(interaction_key=w.ka_close_alone))
        # 5. Interaction inconnue.
        _rejects_each(conn, table, [fk["interaction"]], lambda: write(interaction_key=uuid.uuid4()))
        # 6. Visite inconnue.
        _rejects_each(conn, table, [fk["session"], fk["interaction"]], lambda: write(visit_id=uuid.uuid4()))
        # 7. Utilisateur absent de la participation (existant dans users, ou inconnu).
        _rejects_each(conn, table, [fk["participant"], fk["session"], fk["interaction"]],
                      lambda: write(user_id=USER_C))
        _rejects_each(conn, table, [fk["participant"], fk["session"], fk["interaction"]],
                      lambda: write(user_id=USER_B, campaign_id=w.y, visit_id=w.sb1, interaction_key=w.kb1))
        # 8. Interaction 'activity' valide dans la bonne visite : acceptée.
        write()
        # 9. Deux appareils du même utilisateur, deux visites distinctes : chacun
        #    sa preuve ; le mélange appareil 1 / visite 2 est refusé.
        write(visit_id=w.sa2, interaction_key=w.ka2)
        _rejects_each(conn, table, [fk["interaction"]], lambda: write(visit_id=w.sa1, interaction_key=w.ka2))
        # 10. Ancienne interaction rejouée sans changement de visite : la clé
        #     reste attachée à sa visite d'origine, même close ; elle ne peut
        #     pas être rattachée à la nouvelle visite du même appareil.
        conn.execute(sa.text("UPDATE product_activity_sessions SET finalized_at = idle_expires_at, "
                             "end_reason = 'inactivity' WHERE id = :s"), {"s": w.sa1})
        renewed = _session(conn, w.x, USER_A, D1, opened=T + timedelta(hours=2))
        _rejects_each(conn, table, [fk["interaction"]], lambda: write(visit_id=renewed, interaction_key=w.ka1b))
        write(visit_id=w.sa1, interaction_key=w.ka1b)
        # Plusieurs opérations d'une même interaction restent possibles.
        write(visit_id=w.sa1, interaction_key=w.ka1b)
        assert conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one() == 4
    finally:
        conn.rollback()
        conn.close()


def test_pg_canonical_operation_and_event_concordance(pg_url, pg_engine):  # noqa: F811
    """Opérations et événements, cas 1 à 10 du contrat."""
    conn, w = _head_world(pg_url, pg_engine)
    try:
        op = uuid.uuid4()
        # 1. Même opération canonique deux fois (autre tentative, onglet ou appareil).
        _proof(conn, w, event_type="portfolio_used", surface="portfolio",
               source_operation_kind=attestations.PORTFOLIO_CREATE, source_operation_id=op)
        for other in ({"interaction_key": w.ka1b}, {"visit_id": w.sa2, "interaction_key": w.ka2}):
            _rejects(conn, _quoted("uq_product_activity_proof_registry_source_operation"),
                     lambda: _proof(conn, w, event_type="portfolio_used", surface="portfolio",
                                    source_operation_kind=attestations.PORTFOLIO_CREATE, source_operation_id=op,
                                    **other))
        # 2. Même UUID, deux natures Portfolio différentes : deux opérations
        #    canoniques distinctes (jamais regroupées sous portfolio_write).
        for kind in PORTFOLIO_KINDS[1:]:
            _proof(conn, w, event_type="portfolio_used", surface="portfolio", source_operation_kind=kind,
                   source_operation_id=op)
            _attribution(conn, w, event_type="portfolio_used", surface="portfolio", source_operation_kind=kind,
                         source_operation_id=op)
        # Unicité par propriétaire : le même UUID d'un autre utilisateur est indépendant.
        _proof(conn, w, user_id=USER_B, visit_id=w.sb1, interaction_key=w.kb1, event_type="portfolio_used",
               surface="portfolio", source_operation_kind=attestations.PORTFOLIO_CREATE, source_operation_id=op)
        base = {"event_type": "decrypter_used", "surface": "decrypter",
                "source_operation_kind": attestations.DECRYPTAGE_DELIVERY_ACK}
        fk = _quoted(ATTRIBUTION_FK["event"])
        # 3. Attribution à A de l'événement de B.
        _rejects(conn, fk, lambda: _attribution(conn, w, **base, event_overrides={"user_id": USER_B}))
        # 4. Attribution à une autre visite que celle de l'événement.
        _rejects(conn, fk, lambda: _attribution(conn, w, **base, event_overrides={"visit_id": w.sa2}))
        # 5. Autre type métier (l'événement est un coach_used).
        _rejects(conn, fk, lambda: _attribution(conn, w, **base, event_overrides={"event_type": "coach_used",
                                                                                  "surface": "coach"}))
        # 6. Autre surface (même type).
        _rejects(conn, fk, lambda: _attribution(conn, w, **base, event_overrides={"surface": "app"}))
        # 7. Autre identité d'opération (UUID, nature, ou opération absente de l'événement).
        _rejects(conn, fk, lambda: _attribution(conn, w, **base, event_overrides={"source_operation_id": uuid.uuid4()}))
        _rejects(conn, fk, lambda: _attribution(
            conn, w, event_type="portfolio_used", surface="portfolio", source_operation_kind=attestations.PORTFOLIO_UPDATE,
            event_overrides={"source_operation_kind": attestations.PORTFOLIO_CREATE}))
        _rejects(conn, fk, lambda: _attribution(conn, w, **base, event_overrides={
            "source": "client_signal", "source_operation_kind": None, "source_operation_id": None}))
        _rejects(conn, fk, lambda: _attribution(conn, w, **base, event=False))  # événement inexistant
        # Attribution concordante : acceptée.
        good = _attribution(conn, w, **base)
        # 8. Deux attributions du même événement (même opération, ou autre
        #    identité quelconque : la clé primaire event_id refuse seule).
        _rejects(conn, "|".join(_quoted(n) for n in ("product_activity_event_attributions_pkey",
                                                     "uq_product_activity_event_attributions_source_operation",
                                                     "uq_product_activity_event_attributions_proof_identity")),
                 lambda: _attribution(conn, w, **{**good, "interaction_key": w.ka1b}, event=False))
        _rejects(conn, _quoted("product_activity_event_attributions_pkey"),
                 lambda: _attribution(conn, w, **{**good, "interaction_key": w.ka1b,
                                                  "source_operation_id": uuid.uuid4()}, event=False))
        # 9. Deux événements pour une même opération : refusés par l'index
        #    partiel de 0022 ; l'UNIQUE des attributions refuse seule une
        #    seconde attribution de la même opération (index 0022 retiré le
        #    temps d'un SAVEPOINT).
        _rejects(conn, _quoted("uq_product_activity_events_source_operation"),
                 lambda: _attribution(conn, w, **base, source_operation_id=good["source_operation_id"]))
        with conn.begin_nested() as savepoint:
            conn.execute(sa.text("DROP INDEX uq_product_activity_events_source_operation"))
            _rejects(conn, _quoted("uq_product_activity_event_attributions_source_operation"),
                     lambda: _attribution(conn, w, **base, interaction_key=w.ka1b,
                                          source_operation_id=good["source_operation_id"]))
            savepoint.rollback()
        # 10. Preuve 'recorded' associée à une attribution étrangère : refusée
        #     pour tout écart (utilisateur, campagne, visite, interaction,
        #     type, surface, opération) ; acceptée si tout concorde.
        recorded = {**_resolved("recorded", T + timedelta(minutes=3)), "certified_at": T + timedelta(minutes=2),
                    "recorded_event_id": good["event_id"]}
        same = {k: good[k] for k in ("campaign_id", "user_id", "visit_id", "interaction_key", "event_type", "surface",
                                     "source_operation_kind", "source_operation_id")}
        attribution_fk = _quoted(REGISTRY_FK["attribution"])
        for mismatch in ({"user_id": USER_B, "visit_id": w.sb1, "interaction_key": w.kb1},
                         {"campaign_id": w.y, "visit_id": w.say, "interaction_key": w.kay},
                         {"visit_id": w.sa2, "interaction_key": w.ka2},
                         {"interaction_key": w.ka1b},
                         {"source_operation_id": uuid.uuid4()}):
            _rejects(conn, attribution_fk, lambda: _proof(conn, w, **{**same, **recorded, **mismatch}))
        _rejects(conn, attribution_fk, lambda: _proof(conn, w, **{**same, **recorded, "event_type": "coach_used",
                                                                  "surface": "coach",
                                                                  "source_operation_kind": "assistant_delivery"}))
        _rejects(conn, attribution_fk, lambda: _proof(conn, w, **{**same, **recorded,
                                                                  "recorded_event_id": uuid.uuid4()}))
        _proof(conn, w, **same, **recorded)
        # Une preuve de B ne peut viser l'attribution de A, même avec sa propre opération.
        _rejects(conn, attribution_fk, lambda: _proof(conn, w, user_id=USER_B, visit_id=w.sb1, interaction_key=w.kb1,
                                                      **_resolved("recorded", T + timedelta(minutes=3)),
                                                      recorded_event_id=good["event_id"]))
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("table", [REGISTRY, ATTRIBUTIONS])
def test_pg_operation_triples_are_closed(pg_url, pg_engine, table):  # noqa: F811
    conn, w = _head_world(pg_url, pg_engine)
    write = _writer(conn, w, table)
    check = _quoted(f"ck_{table}_operation")
    try:
        for event_type, surface, kinds in _m0024().OPERATION_RULES:
            for kind in kinds:
                write(event_type=event_type, surface=surface, source_operation_kind=kind)
        for event_type, surface, kind in (
                ("decrypter_used", "decrypter", "assistant_delivery"),  # jamais converti par commodité
                ("portfolio_used", "portfolio", "portfolio_write"),  # jamais regroupé
                ("education_used", "coach", "assistant_delivery"),
                ("coach_used", "education", "assistant_delivery"),
                ("education_used", "education", "decryptage_delivery_ack"),
                ("portfolio_used", "decrypter", "portfolio_position_create"),
                ("decrypter_used", "portfolio", "decryptage_delivery_ack"),
                ("session_started", "app", "assistant_delivery"),
                ("profile_viewed", "profile", "assistant_delivery"),
                ("view_opened", "education", "assistant_delivery"),
                ("portfolio_used", "portfolio", "PORTFOLIO_POSITION_CREATE"),
                ("portfolio_used", "portfolio", "portfolio_position_create "),
                ("portfolio_used", "portfolio", "AAPL")):
            _rejects(conn, check, lambda: write(event_type=event_type, surface=surface, source_operation_kind=kind))
    finally:
        conn.rollback()
        conn.close()


STATE = "ck_product_activity_proof_registry_state"
STATUS = "ck_product_activity_proof_registry_status"
CHRONOLOGY = "ck_product_activity_proof_registry_chronology"
PURGE = "ck_product_activity_proof_registry_purge_after"
REASON = "ck_product_activity_proof_registry_resolution_reason"


def test_pg_proof_states_timestamps_and_reasons(pg_url, pg_engine):  # noqa: F811
    conn, w = _head_world(pg_url, pg_engine)
    try:
        created, certified, resolved = T + timedelta(minutes=1), T + timedelta(minutes=2), T + timedelta(minutes=3)
        event, spare = _attribution(conn, w), _attribution(conn, w)
        same = {k: event[k] for k in ("campaign_id", "user_id", "visit_id", "interaction_key", "event_type",
                                      "surface", "source_operation_kind", "source_operation_id")}
        other = {**{k: spare[k] for k in same}, "recorded_event_id": spare["event_id"]}
        # Toutes les formes autorisées.
        _proof(conn, w)
        _proof(conn, w, status="certified", certified_at=certified)
        _proof(conn, w, **same, status="recorded", certified_at=certified, resolved_at=resolved,
               purge_after_at=resolved + RETENTION, recorded_event_id=event["event_id"])
        for status in ("rejected", "indeterminate"):
            _proof(conn, w, **_resolved(status, resolved))  # sans certification
            _proof(conn, w, **_resolved(status, resolved), certified_at=certified)  # certification conservée
        # Égalités limites de la chronologie.
        _proof(conn, w, status="certified", certified_at=created)
        _proof(conn, w, **_resolved("rejected", created), certified_at=created)
        assert conn.execute(sa.text(f"SELECT count(*) FROM {REGISTRY}")).scalar_one() == 9
        # Valeurs d'état inconnues.
        for status in ("succeeded", "PENDING", "", "pending ", "finalized"):
            _rejects_each(conn, REGISTRY, [STATE, STATUS], lambda: _proof(conn, w, status=status))
        # Formes interdites (une seule contrainte fautive à chaque fois).
        forbidden = [
            (STATE, {"certified_at": certified}),  # pending certifiée
            (STATE, {"resolved_at": resolved, "purge_after_at": resolved + RETENTION}),  # pending résolue
            (STATE, {"resolution_reason": "no_new_effect"}),  # pending motivée
            (STATE, {"status": "certified"}),  # certified sans certification
            (STATE, {"status": "certified", "certified_at": certified, "resolved_at": resolved,
                     "purge_after_at": resolved + RETENTION}),
            (STATE, {"status": "recorded", "certified_at": certified, "resolved_at": resolved,
                     "purge_after_at": resolved + RETENTION}),  # recorded sans événement
            (STATE, {**other, "status": "recorded", "resolved_at": resolved,
                     "purge_after_at": resolved + RETENTION}),  # recorded sans certification
            (STATE, {**other, **_resolved("recorded", resolved), "certified_at": certified,
                     "resolution_reason": "no_new_effect"}),
            (STATE, {**_resolved("rejected", resolved), "resolution_reason": None}),  # rejet sans motif
            (STATE, {**_resolved("indeterminate", resolved), "resolution_reason": None}),
            (STATE, {**other, **_resolved("rejected", resolved)}),
            (STATE, {"status": "rejected", "resolution_reason": "owner_conflict"}),  # rejet non résolu
            (STATE, {"status": "indeterminate", "resolution_reason": "technical_uncertainty"}),
            (CHRONOLOGY, {"status": "certified", "certified_at": created - timedelta(microseconds=1)}),
            (CHRONOLOGY, {**_resolved("rejected", created - timedelta(seconds=1))}),
            (CHRONOLOGY, {**_resolved("rejected", certified - timedelta(seconds=1)), "certified_at": certified}),
            (PURGE, {**_resolved("rejected", resolved), "purge_after_at": None}),  # résolue sans échéance
            (PURGE, {"purge_after_at": T + RETENTION}),  # non résolue avec échéance
            (PURGE, {"status": "certified", "certified_at": certified, "purge_after_at": certified + RETENTION}),
            (PURGE, {**_resolved("rejected", resolved), "purge_after_at": resolved + RETENTION
                     - timedelta(microseconds=1)}),
            (PURGE, {**_resolved("rejected", resolved), "purge_after_at": resolved + timedelta(days=90)}),
            (PURGE, {**_resolved("rejected", resolved), "purge_after_at": resolved}),
        ]
        for name, overrides in forbidden:
            _rejects(conn, _quoted(name), lambda: _proof(conn, w, **overrides))
        # Motifs fermés : les dix motifs techniques, rien d'autre.
        for reason in _m0024().RESOLUTION_REASONS:
            _proof(conn, w, **_resolved("rejected", resolved, resolution_reason=reason))
            _proof(conn, w, **_resolved("indeterminate", resolved, resolution_reason=reason))
        for reason in ("NO_NEW_EFFECT", "free text", "AAPL", "Réponse IA", "", "no_new_effect ", "unknown",
                       "succeeded", "recorded"):
            _rejects(conn, _quoted(REASON), lambda: _proof(conn, w, **_resolved("rejected", resolved,
                                                                                resolution_reason=reason)))
        with pytest.raises(sa.exc.DataError):
            with conn.begin_nested():
                _proof(conn, w, **_resolved("rejected", resolved, resolution_reason="x" * 65))
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("timezone_name", ["UTC", "Europe/Paris", "America/New_York"])
def test_pg_retention_is_168_absolute_hours_across_clock_changes(pg_url, pg_engine, timezone_name):  # noqa: F811
    """Conservation du registre : resolved_at + 168 heures EXACTES, quel que
    soit le fuseau de la session et y compris de part et d'autre d'un
    changement d'heure (jamais « 7 jours » civils)."""
    conn, w = _head_world(pg_url, pg_engine)
    try:
        conn.execute(sa.text(f"SET LOCAL TIME ZONE '{timezone_name}'"))
        paris_spring = datetime(2026, 3, 28, 11, 0, tzinfo=timezone.utc)  # Paris : 12:00 CET, veille du passage
        paris_autumn = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)  # Paris : 12:00 CEST, veille du retour
        us_spring = datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc)  # New York : 12:00 EST
        for at in (paris_spring, paris_autumn, us_spring, T):
            created = at - timedelta(hours=1)
            row = _proof(conn, w, created_at=created, **_resolved("rejected", at))
            stored = conn.execute(sa.text(
                f"SELECT purge_after_at - resolved_at, EXTRACT(EPOCH FROM purge_after_at - resolved_at) "
                f"FROM {REGISTRY} WHERE id = :id"), {"id": row["id"]}).one()
            assert stored == (RETENTION, 168 * 3600)
            # « 7 jours » civils dans le fuseau de la session : refusé dès
            # qu'il diffère de 168 heures (changement d'heure).
            civil = conn.execute(sa.text("SELECT CAST(:t AS timestamptz) + interval '7 days'"),
                                 {"t": at}).scalar_one()
            if civil != at + RETENTION:
                assert timezone_name != "UTC"
                _rejects(conn, _quoted(PURGE), lambda: _proof(conn, w, created_at=created,
                                                              **_resolved("rejected", at, purge_after_at=civil)))
        shifted = {"Europe/Paris": 2, "America/New_York": 1, "UTC": 0}[timezone_name]
        civil_differs = conn.execute(sa.text(
            "SELECT count(*) FROM unnest(CAST(:ts AS timestamptz[])) AS t "
            "WHERE t + interval '7 days' <> t + interval '168 hours'"),
            {"ts": [paris_spring, paris_autumn, us_spring, T]}).scalar_one()
        assert civil_differs == shifted
    finally:
        conn.rollback()
        conn.close()


def test_pg_deletion_order_without_cascade(pg_url, pg_engine):  # noqa: F811
    """Supprimer une preuve provisoire ne perd pas l'attribution ;
    l'attribution est protégée tant que la preuve 'recorded' existe ;
    l'événement, la visite, l'interaction et la participation sont protégés
    tant que l'attribution existe ; aucune cascade."""
    conn, w = _head_world(pg_url, pg_engine)
    try:
        attribution = _attribution(conn, w)
        same = {k: attribution[k] for k in ("campaign_id", "user_id", "visit_id", "interaction_key", "event_type",
                                            "surface", "source_operation_kind", "source_operation_id")}
        proof = _proof(conn, w, **same, **_resolved("recorded", T + timedelta(minutes=3)),
                       recorded_event_id=attribution["event_id"])
        _rejects(conn, _quoted(REGISTRY_FK["attribution"]),
                 f"DELETE FROM {ATTRIBUTIONS} WHERE event_id = :e", e=attribution["event_id"])
        _rejects(conn, _quoted(REGISTRY_FK["attribution"]),
                 f"UPDATE {ATTRIBUTIONS} SET interaction_key = :k WHERE event_id = :e", e=attribution["event_id"],
                 k=w.ka1b)
        _rejects(conn, _quoted(ATTRIBUTION_FK["event"]),
                 f"DELETE FROM {EVENTS} WHERE id = :e", e=attribution["event_id"])
        _rejects(conn, _quoted(ATTRIBUTION_FK["event"]),
                 f"UPDATE {EVENTS} SET visit_id = :v WHERE id = :e", e=attribution["event_id"], v=w.sa2)
        _rejects(conn, f'{_quoted(ATTRIBUTION_FK["interaction"])}|{_quoted(REGISTRY_FK["interaction"])}',
                 f"DELETE FROM {INTERACTIONS} WHERE interaction_key = :k", k=w.ka1)
        _rejects(conn, "fkey", f"DELETE FROM {SESSIONS} WHERE id = :s", s=w.sa1)
        _rejects(conn, "fkey", f"DELETE FROM {PARTICIPANTS} WHERE campaign_id = :c AND user_id = :u",
                 c=w.x, u=USER_A)
        # Purge future dans l'ordre : preuve, puis attribution, puis événement.
        conn.execute(sa.text(f"DELETE FROM {REGISTRY} WHERE id = :id"), {"id": proof["id"]})
        assert conn.execute(sa.text(f"SELECT count(*) FROM {ATTRIBUTIONS} WHERE event_id = :e"),
                            {"e": attribution["event_id"]}).scalar_one() == 1
        _rejects(conn, _quoted(ATTRIBUTION_FK["event"]),
                 f"DELETE FROM {EVENTS} WHERE id = :e", e=attribution["event_id"])
        conn.execute(sa.text(f"DELETE FROM {ATTRIBUTIONS} WHERE event_id = :e"), {"e": attribution["event_id"]})
        conn.execute(sa.text(f"DELETE FROM {EVENTS} WHERE id = :e"), {"e": attribution["event_id"]})
        rules = conn.execute(sa.text(
            "SELECT DISTINCT confdeltype, confupdtype, condeferrable FROM pg_constraint WHERE contype = 'f' "
            "AND conrelid = ANY(CAST(:t AS regclass[]))"), {"t": sorted(NEW)}).all()
        assert rules == [("a", "a", False)]
    finally:
        conn.rollback()
        conn.close()


def test_pg_rollback_of_an_invalid_insertion(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    with pg_engine.begin() as conn:
        w = _world(conn)
    with pg_engine.connect() as conn:
        valid = _proof(conn, w)
        _rejects(conn, _quoted(REGISTRY_FK["interaction"]), lambda: _proof(conn, w, interaction_key=w.ka1_close))
        conn.commit()  # le SAVEPOINT annulé laisse la transaction utilisable
    with pg_engine.connect() as conn:
        with pytest.raises(sa.exc.IntegrityError, match=_quoted(REGISTRY_FK["interaction"])):
            with conn.begin():
                _proof(conn, w)  # valide, puis annulée avec la transaction
                _proof(conn, w, interaction_key=uuid.uuid4())
        assert conn.execute(sa.text(f"SELECT id FROM {REGISTRY}")).scalars().all() == [valid["id"]]
        assert conn.execute(sa.text(f"SELECT count(*) FROM {ATTRIBUTIONS}")).scalar_one() == 0


def _wait_for_lock_wait(engine, timeout=15.0) -> None:
    deadline = time.monotonic() + timeout
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            waiting = probe.execute(sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock'")).scalar_one()
            probe.rollback()
            if waiting:
                return
            time.sleep(0.05)
    raise AssertionError("la seconde insertion n'attend pas la première")


@pytest.mark.parametrize("first_outcome", ["commit", "rollback"])
@pytest.mark.parametrize("table", [REGISTRY, ATTRIBUTIONS])
def test_pg_concurrent_insertions_of_one_canonical_identity(pg_url, pg_engine, table, first_outcome):  # noqa: F811
    """Deux insertions concurrentes de la même opération canonique (deux
    onglets ou appareils) : la seconde attend la première ; si la première
    valide, la seconde est refusée par l'unicité ; si elle annule, la seconde
    passe. Jamais deux lignes."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    with pg_engine.begin() as conn:
        w = _world(conn)
    op = uuid.uuid4()
    first = pg_engine.connect()
    first.begin()
    _writer(first, w, table)(source_operation_id=op)
    result = {}

    def second():
        try:
            with pg_engine.connect() as conn:
                conn.execute(sa.text("SET lock_timeout = '20s'"))
                with conn.begin_nested():
                    _writer(conn, w, table)(source_operation_id=op, visit_id=w.sa2, interaction_key=w.ka2)
                conn.commit()
                result["outcome"] = "inserted"
        except sa.exc.IntegrityError as exc:
            result["outcome"] = str(exc.orig)

    thread = threading.Thread(target=second)
    thread.start()
    try:
        _wait_for_lock_wait(pg_engine)
    finally:
        getattr(first, first_outcome)()
        first.close()
        thread.join(30)
    assert not thread.is_alive()
    expected = {REGISTRY: ("uq_product_activity_proof_registry_source_operation",),
                ATTRIBUTIONS: ("uq_product_activity_events_source_operation",
                               "uq_product_activity_event_attributions_source_operation")}[table]
    if first_outcome == "commit":
        assert any(name in result["outcome"] for name in expected), result
    else:
        assert result["outcome"] == "inserted", result
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE source_operation_id = :o"),
                            {"o": op}).scalar_one() == 1


def test_pg_legitimate_0022_and_0023_usage_is_unchanged_at_0024(pg_url, pg_engine):  # noqa: F811
    """Les UNIQUE ajoutées contiennent la clé primaire de leur table : aucun
    usage légitime 0022 / 0023 n'est interdit (événements sans opération,
    plusieurs types dans une visite, clôture sans visite, rejeux, plusieurs
    sessions par environnement dans le temps)."""
    conn, w = _head_world(pg_url, pg_engine)
    try:
        visit = uuid.uuid4()  # visit_id des événements : libre (aucune FK en 0022)
        for event_type, surface in (("session_started", "app"), ("view_opened", "education"),
                                    ("profile_viewed", "profile"), ("education_used", "education")):
            _event(conn, user_id=USER_A, visit_id=visit, event_type=event_type, surface=surface,
                   source="server_visit")
        _event(conn, user_id=USER_A, visit_id=visit, event_type="decrypter_used", surface="decrypter",
               source="server_business", source_operation_kind="assistant_delivery",
               source_operation_id=uuid.uuid4(), outcome="in_progress", delivery_status="pending")
        _event(conn, user_id=USER_B, event_type="portfolio_used", surface="portfolio", source="client_signal")
        _interaction(conn, w.x, USER_B, "close", D1, None)
        _interaction(conn, w.x, USER_B, "close", D1, w.sb1)
        conn.execute(sa.text("UPDATE product_activity_sessions SET finalized_at = last_activity_at, "
                             "end_reason = 'logout' WHERE id = :s"), {"s": w.sb1})
        again = _session(conn, w.x, USER_B, D1, opened=T + timedelta(hours=1))
        _interaction(conn, w.x, USER_B, "activity", D1, again)
        _rejects(conn, _quoted("product_activity_session_interactions_pkey"),
                 "INSERT INTO product_activity_session_interactions VALUES (:c, :u, :k, 'activity', :d, :s, :t)",
                 c=w.x, u=USER_A, k=w.ka1, d=D1, s=w.sa1, t=T)
        _rejects(conn, _quoted("uq_product_activity_events_visit_session_start"),
                 lambda: _event(conn, user_id=USER_A, visit_id=visit, event_type="session_started", surface="app",
                                source="server_visit"))
    finally:
        conn.rollback()
        conn.close()


# --------------------------------------------------------------------------
# Migration : montée, catalogue, downgrade
# --------------------------------------------------------------------------

def _seed_0023(engine) -> SimpleNamespace:
    """Historique 0021 (pédagogie, portefeuille, frontière F2) et lignes
    analytiques 0022 / 0023 (campagne, participation, événements, visite,
    interactions)."""
    _seed_0021_history(engine)
    with engine.begin() as conn:
        w = _world(conn)
        _event(conn, user_id=USER_A, visit_id=w.sa1, event_type="session_started", surface="app",
               source="server_visit")
        _event(conn, user_id=USER_A, visit_id=w.sa1, event_type="decrypter_used", surface="decrypter",
               source="server_business", source_operation_kind="assistant_delivery",
               source_operation_id=uuid.uuid4())
    return w


def _constraint_rows(engine, table) -> list:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT conname, contype, pg_get_constraintdef(oid), condeferrable, confdeltype, confupdtype "
            "FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND contype <> 'n' ORDER BY conname"),
            {"t": table}).all()]


ADDED_DEFINITIONS = {
    EVENTS: ("uq_product_activity_events_attribution_identity",
             "UNIQUE (id, user_id, visit_id, event_type, surface, source_operation_kind, source_operation_id)"),
    SESSIONS: ("uq_product_activity_sessions_owner", "UNIQUE (id, campaign_id, user_id)"),
    INTERACTIONS: ("uq_product_activity_session_interactions_origin",
                   "UNIQUE (campaign_id, user_id, interaction_key, session_id, interaction_kind)"),
}


def test_pg_upgrade_0023_to_0024_keeps_every_row_then_exact_downgrade(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0023)
    _seed_0023(pg_engine)
    tables = _tables(pg_engine)
    assert not tables & NEW
    schema_0023, catalog_0023 = _snapshot(pg_engine, tables), _catalog(pg_engine, tables)
    constraints_0023 = {t: _constraint_rows(pg_engine, t) for t in ALTERED}
    global_0023, extensions_0023 = _global_catalog(pg_engine), _extensions(pg_engine)
    data_0023 = _data(pg_engine, tables - {"alembic_version"})
    for table in (EVENTS, SESSIONS, INTERACTIONS, PARTICIPANTS, "users", "portfolio_positions",
                  "decryptage_turn_reservations"):
        assert data_0023[table], table
    assert _compare_metadata(pg_engine) == []  # écarts exacts de 0024 retirés (tables, index, trois UNIQUE)

    _run_alembic(pg_url, "upgrade", V0T08_0024)
    assert _version(pg_engine) == V0T08_0024 and _compare_metadata(pg_engine) == []
    assert _tables(pg_engine) == tables | NEW
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0023  # aucun backfill, aucune ligne modifiée
    assert _data(pg_engine, NEW) == {t: [] for t in sorted(NEW)}
    untouched = tables - {"alembic_version"} - ALTERED  # F2, pédagogie, portefeuille, cohortes : intacts
    assert _snapshot(pg_engine, untouched) == {t: schema_0023[t] for t in sorted(untouched)}
    assert _catalog(pg_engine, untouched) == {t: catalog_0023[t] for t in sorted(untouched)}
    for table in ALTERED:  # seule différence : la UNIQUE ajoutée et son index
        name, definition = ADDED_DEFINITIONS[table]
        assert _constraint_rows(pg_engine, table) == sorted(constraints_0023[table] + [
            (name, "u", definition, False, " ", " ")])
        catalog = _catalog(pg_engine, {table})[table]
        assert catalog["columns"] == catalog_0023[table]["columns"]
        assert catalog["indexes"] == sorted(catalog_0023[table]["indexes"] + [name])
    assert _global_catalog(pg_engine) == global_0023 and _extensions(pg_engine) == extensions_0023

    _run_alembic(pg_url, "downgrade", V0T08_0023)
    assert _version(pg_engine) == V0T08_0023 and _tables(pg_engine) == tables
    assert _snapshot(pg_engine, tables) == schema_0023 and _catalog(pg_engine, tables) == catalog_0023
    assert {t: _constraint_rows(pg_engine, t) for t in ALTERED} == constraints_0023
    assert _global_catalog(pg_engine) == global_0023
    assert _data(pg_engine, tables - {"alembic_version"}) == data_0023
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == V0T08_0024 and _compare_metadata(pg_engine) == []


INDEXDEFS = {
    "ix_product_activity_event_attributions_visit":
        "CREATE INDEX ix_product_activity_event_attributions_visit ON public.product_activity_event_attributions "
        "USING btree (campaign_id, user_id, visit_id)",
    "ix_product_activity_proof_registry_campaign_status":
        "CREATE INDEX ix_product_activity_proof_registry_campaign_status ON public.product_activity_proof_registry "
        "USING btree (campaign_id, status)",
    "ix_product_activity_proof_registry_visit":
        "CREATE INDEX ix_product_activity_proof_registry_visit ON public.product_activity_proof_registry "
        "USING btree (campaign_id, user_id, visit_id)",
    "ix_product_activity_proof_registry_recorded_event":
        "CREATE INDEX ix_product_activity_proof_registry_recorded_event ON public.product_activity_proof_registry "
        "USING btree (recorded_event_id) WHERE (recorded_event_id IS NOT NULL)",
    "ix_product_activity_proof_registry_purge_after_at":
        "CREATE INDEX ix_product_activity_proof_registry_purge_after_at ON public.product_activity_proof_registry "
        "USING btree (purge_after_at) WHERE (purge_after_at IS NOT NULL)",
}
CONSTRAINT_INDEXES = {"product_activity_event_attributions_pkey", "product_activity_proof_registry_pkey",
                      "uq_product_activity_event_attributions_source_operation",
                      "uq_product_activity_event_attributions_proof_identity",
                      "uq_product_activity_proof_registry_source_operation"}
NON_CHECK_DEFINITIONS = {
    "product_activity_event_attributions_pkey": (ATTRIBUTIONS, "PRIMARY KEY (event_id)"),
    "uq_product_activity_event_attributions_source_operation": (
        ATTRIBUTIONS, "UNIQUE (user_id, source_operation_kind, source_operation_id)"),
    "uq_product_activity_event_attributions_proof_identity": (
        ATTRIBUTIONS, "UNIQUE (event_id, campaign_id, user_id, visit_id, interaction_key, event_type, surface, "
                      "source_operation_kind, source_operation_id)"),
    "product_activity_event_attributions_participant_fkey": (
        ATTRIBUTIONS, "FOREIGN KEY (campaign_id, user_id) REFERENCES "
                      "product_campaign_participants(campaign_id, user_id)"),
    "product_activity_event_attributions_session_fkey": (
        ATTRIBUTIONS, "FOREIGN KEY (visit_id, campaign_id, user_id) REFERENCES "
                      "product_activity_sessions(id, campaign_id, user_id)"),
    "product_activity_event_attributions_interaction_fkey": (
        ATTRIBUTIONS, "FOREIGN KEY (campaign_id, user_id, interaction_key, visit_id, interaction_kind) REFERENCES "
                      "product_activity_session_interactions(campaign_id, user_id, interaction_key, session_id, "
                      "interaction_kind)"),
    "product_activity_event_attributions_event_fkey": (
        ATTRIBUTIONS, "FOREIGN KEY (event_id, user_id, visit_id, event_type, surface, source_operation_kind, "
                      "source_operation_id) REFERENCES product_activity_events(id, user_id, visit_id, event_type, "
                      "surface, source_operation_kind, source_operation_id)"),
    "product_activity_proof_registry_pkey": (REGISTRY, "PRIMARY KEY (id)"),
    "uq_product_activity_proof_registry_source_operation": (
        REGISTRY, "UNIQUE (user_id, source_operation_kind, source_operation_id)"),
    "product_activity_proof_registry_participant_fkey": (
        REGISTRY, "FOREIGN KEY (campaign_id, user_id) REFERENCES product_campaign_participants(campaign_id, user_id)"),
    "product_activity_proof_registry_session_fkey": (
        REGISTRY, "FOREIGN KEY (visit_id, campaign_id, user_id) REFERENCES "
                  "product_activity_sessions(id, campaign_id, user_id)"),
    "product_activity_proof_registry_interaction_fkey": (
        REGISTRY, "FOREIGN KEY (campaign_id, user_id, interaction_key, visit_id, interaction_kind) REFERENCES "
                  "product_activity_session_interactions(campaign_id, user_id, interaction_key, session_id, "
                  "interaction_kind)"),
    "product_activity_proof_registry_attribution_fkey": (
        REGISTRY, "FOREIGN KEY (recorded_event_id, campaign_id, user_id, visit_id, interaction_key, event_type, "
                  "surface, source_operation_kind, source_operation_id) REFERENCES "
                  "product_activity_event_attributions(event_id, campaign_id, user_id, visit_id, interaction_key, "
                  "event_type, surface, source_operation_kind, source_operation_id)"),
}


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
        users_id = conn.execute(sa.text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'users'::regclass AND attname = 'id'")).scalar_one()
        assert users_id == VC
        constraints = {}
        for name, table, contype, definition, deferrable, validated, deltype, updtype in conn.execute(sa.text(
                "SELECT conname, conrelid::regclass::text, contype, pg_get_constraintdef(oid), condeferrable, "
                "convalidated, confdeltype, confupdtype FROM pg_constraint "
                "WHERE conrelid = ANY(CAST(:t AS regclass[])) AND contype <> 'n'"), {"t": sorted(NEW)}):
            assert not deferrable and validated, name
            if contype == "f":
                assert (deltype, updtype) == ("a", "a"), name  # NO ACTION, jamais CASCADE
            constraints[name] = (table, contype, definition)
        expected_checks = {name: table for table, checks in ((ATTRIBUTIONS, m.ATTRIBUTION_CHECKS),
                                                             (REGISTRY, m.REGISTRY_CHECKS)) for name, _ in checks}
        assert {n: t for n, (t, c, _) in constraints.items() if c == "c"} == expected_checks
        assert {n: (t, d) for n, (t, c, d) in constraints.items() if c != "c"} == NON_CHECK_DEFINITIONS
        assert "'168:00:00'::interval" in constraints[PURGE][2]
        assert all(len(name) <= 63 for name in constraints)
        indexdefs = dict(conn.execute(sa.text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND tablename = ANY(:t)"), {"t": sorted(NEW)}).all())
        assert set(indexdefs) == set(INDEXDEFS) | CONSTRAINT_INDEXES
        assert {name: indexdefs[name] for name in INDEXDEFS} == INDEXDEFS
        triggers = conn.execute(sa.text(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgrelid = ANY(CAST(:t AS regclass[]))"),
            {"t": sorted(NEW | ALTERED)}).scalar_one()
        assert triggers == 0
        # Aucune FK ne vise le registre provisoire (aucune dépendance inverse).
        assert conn.execute(sa.text(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
            "AND confrelid = CAST('product_activity_proof_registry' AS regclass)")).scalar_one() == 0


def _seed_one(pg_engine, table) -> None:
    with pg_engine.begin() as conn:
        w = _world(conn)
        if table == REGISTRY:
            _proof(conn, w)
        else:
            _attribution(conn, w)


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("table", [REGISTRY, ATTRIBUTIONS])
def test_pg_downgrade_is_refused_when_a_new_table_has_data(pg_url, pg_engine, table, offline):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    _seed_one(pg_engine, table)
    tables = _tables(pg_engine)
    before = _data(pg_engine, tables - {"alembic_version"})
    catalog = _catalog(pg_engine, tables)
    assert before[table] and not before[ATTRIBUTIONS if table == REGISTRY else REGISTRY]
    expected = f"downgrade de 0024_product_activity_proofs refusé, la table {table} contient des données"
    if offline:
        statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{V0T08_0024}:{V0T08_0023}", "--sql").stdout)
        executed = statements[:statements.index(next(s for s in statements if s.startswith("DO $$"))) + 1]
        with pg_engine.connect() as conn:
            with pytest.raises(sa.exc.DBAPIError, match=expected):
                with conn.begin():
                    for statement in executed:
                        conn.exec_driver_sql(statement)
    else:
        with pytest.raises(Exception) as failure:
            _run_alembic(pg_url, "downgrade", V0T08_0023)
        assert expected in str(getattr(failure.value, "stderr", failure.value))
    assert _version(pg_engine) == V0T08_0024
    assert _data(pg_engine, tables - {"alembic_version"}) == before
    assert _catalog(pg_engine, tables) == catalog


def test_pg_offline_guard_passes_when_both_tables_are_empty(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    with pg_engine.begin() as conn:
        _world(conn)
    statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{V0T08_0024}:{V0T08_0023}", "--sql").stdout)
    with pg_engine.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)
    assert _version(pg_engine) == V0T08_0023 and not _tables(pg_engine) & NEW


def test_pg_downgrade_lock_set_is_deterministic_and_sufficient(pg_url, pg_engine):  # noqa: F811
    """Le LOCK du downgrade couvre toutes les tables que les DROP du downgrade
    verrouillent : exécutées après lui, ces instructions n'ajoutent aucun
    verrou de table (aucun ordre d'acquisition caché)."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    statements = _statements(_run_alembic(OFFLINE, "downgrade", f"{V0T08_0024}:{V0T08_0023}", "--sql").stdout)
    lock = next(s for s in statements if s.startswith("LOCK TABLE"))
    drops = [s for s in statements if s.startswith(("DROP", "ALTER TABLE"))]
    query = ("SELECT c.relname, l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
             "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' AND c.relkind = 'r' "
             "AND c.relnamespace = 'public'::regnamespace ORDER BY 1, 2")
    with pg_engine.connect() as conn:
        conn.exec_driver_sql(lock)
        locked = conn.execute(sa.text(query)).all()
        assert [r[0] for r in locked] == sorted([PARTICIPANTS, EVENTS, SESSIONS, INTERACTIONS, ATTRIBUTIONS,
                                                 REGISTRY])
        assert {r[1] for r in locked} == {"AccessExclusiveLock"}
        for statement in drops:
            conn.exec_driver_sql(statement)
        after = {name for name, _ in conn.execute(sa.text(query)).all()}
        assert after <= {r[0] for r in locked}
        conn.rollback()


def test_pg_offline_upgrade_then_offline_downgrade_round_trip(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", V0T08_0023)
    tables = _tables(pg_engine)
    catalog_0023 = _catalog(pg_engine, tables)
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
    assert _version(pg_engine) == V0T08_0023 and _catalog(pg_engine, tables) == catalog_0023
    _run_alembic(pg_url, "upgrade", V0T08_0024)
    assert _catalog(pg_engine, tables | NEW) == offline_catalog


def test_pg_upgrade_from_an_empty_database_to_0024(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == V0T08_0024 and _compare_metadata(pg_engine) == []
    assert _data(pg_engine, NEW) == {t: [] for t in sorted(NEW)}
