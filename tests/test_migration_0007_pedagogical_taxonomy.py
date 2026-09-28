"""Tests de T4-A : migration 0007_pedagogical_taxonomy + modèles
PedagogicalTaxonomyRelease / CoreCapabilityDefinition /
CapabilityTaxonomyMembership / ObservationCapability, et FK
observation_evaluation_runs.pedagogical_taxonomy_release_id.

Même organisation que les tests des migrations 0002 à 0006 (dont on
réutilise les helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, intégrité de 0001
   à 0006, vocabulaire des 45 capacités, SQL PostgreSQL généré en mode
   offline, métadonnées des modèles (colonnes, types, PK, FK, CHECK,
   UNIQUE, index, défauts), anti-dérive (aucun seed, route, LLM, état
   utilisateur par capacité, lignée ; depuis T4-B, aucun service hors
   core/taxonomy_service.py).

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t4a_test \\
           python -m pytest tests/test_migration_0007_pedagogical_taxonomy.py

T4-A = schéma + modèles seulement : aucune donnée pédagogique, aucune
release ni capacité créée, aucun service, aucune route. Les invariants qui
traversent plusieurs tables (une seule révision d'un code par release,
observation Cn localisée sur Cn_* uniquement, localized / competency_only)
relèvent de T4-B : des tests ci-dessous documentent explicitement que la
base ne les impose PAS encore.
"""
import hashlib
import subprocess
import threading
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from core.db import Base
from core.models import (
    CapabilityTaxonomyMembership,
    CoreCapabilityDefinition,
    ObservationCapability,
    ObservationEvaluationRun,
    PedagogicalObservation,
    PedagogicalTaxonomyRelease,
)
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
    T4A_INDEXES,
    T4A_TABLES,
    _catalog_columns,
    _code_tokens,
    _compare_metadata,
    _constraints,
    _data,
    _seed_remaining_tables,
    _statements,
)
from tests.test_migration_0005_cognitive_support_traces import (
    T1C2_SHA256,
    T2A,
    T2A_TABLES,
    _catalog,
    _global_catalog,
    _upgrade_head_with_users,
)
from tests.test_migration_0006_observation_layer import (
    NOW,
    OBS,
    RUNS,
    T2A_SHA256,
    T3A,
    _backend_pid,
    _count,
    _event,
    _in_sql,
    _obs,
    _pg_in_check,
    _run,
    _seed_t2,
    _wait_until_blocked_by,
)

T4A = "0007_pedagogical_taxonomy"
RELEASES = "pedagogical_taxonomy_releases"
DEFS = "core_capability_definitions"
MEMBERSHIPS = "capability_taxonomy_memberships"
OBS_CAPS = "observation_capabilities"
assert T4A_TABLES == {RELEASES, DEFS, MEMBERSHIPS, OBS_CAPS}
T4A_MODELS = (PedagogicalTaxonomyRelease, CoreCapabilityDefinition, CapabilityTaxonomyMembership,
              ObservationCapability)
ONE_ACTIVE_INDEX = "uq_pedagogical_taxonomy_releases_one_active"
MEMBERSHIP_DEFINITION_INDEX = "ix_capability_taxonomy_memberships_capability_definition_id"
OBSERVATION_MEMBERSHIP_INDEX = "ix_observation_capabilities_capability_membership_id"
RELEASE_INDEX = "ix_observation_evaluation_runs_pedagogical_taxonomy_release_id"
assert T4A_INDEXES == {ONE_ACTIVE_INDEX, MEMBERSHIP_DEFINITION_INDEX, OBSERVATION_MEMBERSHIP_INDEX, RELEASE_INDEX}
# FK ajoutée à la colonne EXISTANTE de 0006. Nom explicite : le nom
# automatique PostgreSQL (<table>_<colonne>_fkey) ferait 64 caractères.
RELEASE_FK = "observation_evaluation_runs_taxonomy_release_id_fkey"
# sha256 de alembic/versions/0006_observation_layer.py tel que mergé sur
# main (aa91cc3, PR #180 ; 0006 introduite par T3-A) et déployé en production.
T3A_SHA256 = "c9176312008012825fa5c2a3927312e3d98e0cd636a2281480522ede83081e14"

# Les 45 codes conceptuellement figés, recopiés explicitement (jamais
# dérivés du code produit) : 3 capacités pour C1-C3, 4 pour C4-C12.
CAPABILITY_CODES = (
    "C1_A", "C1_B", "C1_C",
    "C2_A", "C2_B", "C2_C",
    "C3_A", "C3_B", "C3_C",
    "C4_A", "C4_B", "C4_C", "C4_D",
    "C5_A", "C5_B", "C5_C", "C5_D",
    "C6_A", "C6_B", "C6_C", "C6_D",
    "C7_A", "C7_B", "C7_C", "C7_D",
    "C8_A", "C8_B", "C8_C", "C8_D",
    "C9_A", "C9_B", "C9_C", "C9_D",
    "C10_A", "C10_B", "C10_C", "C10_D",
    "C11_A", "C11_B", "C11_C", "C11_D",
    "C12_A", "C12_B", "C12_C", "C12_D",
)
COMPETENCY_CODES = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C12")

COLUMNS = {
    RELEASES: ["id", "version_key", "status", "spec_fingerprint", "created_at", "activated_at"],
    DEFS: ["id", "capability_code", "semantic_revision", "competency_code", "label", "definition",
           "mapping_guidance", "created_at"],
    MEMBERSHIPS: ["id", "taxonomy_release_id", "capability_definition_id", "created_at"],
    OBS_CAPS: ["observation_id", "capability_membership_id", "created_at"],
}
NULLABLE = {(RELEASES, "activated_at")}
UUID_COLUMNS = {
    (RELEASES, "id"), (DEFS, "id"), (MEMBERSHIPS, "id"),
    (MEMBERSHIPS, "taxonomy_release_id"), (MEMBERSHIPS, "capability_definition_id"),
    (OBS_CAPS, "observation_id"), (OBS_CAPS, "capability_membership_id"),
}
TIMESTAMPTZ_COLUMNS = {
    (RELEASES, "created_at"), (RELEASES, "activated_at"), (DEFS, "created_at"),
    (MEMBERSHIPS, "created_at"), (OBS_CAPS, "created_at"),
}
JSONB_COLUMNS = {(DEFS, "mapping_guidance")}
TEXT_COLUMNS = {(DEFS, "definition")}
INTEGER_COLUMNS = {(DEFS, "semantic_revision")}
PRIMARY_KEYS = {
    RELEASES: ["id"], DEFS: ["id"], MEMBERSHIPS: ["id"],
    OBS_CAPS: ["observation_id", "capability_membership_id"],
}
FOREIGN_KEYS = {
    RELEASES: [],
    DEFS: [],
    MEMBERSHIPS: [("capability_definition_id", f"{DEFS}.id"), ("taxonomy_release_id", f"{RELEASES}.id")],
    OBS_CAPS: [("capability_membership_id", f"{MEMBERSHIPS}.id"), ("observation_id", f"{OBS}.id")],
}
CODE_CHECK = "ck_core_capability_definitions_capability_code"
COMPETENCY_CHECK = "ck_core_capability_definitions_competency_code"
COHERENCE_CHECK = "ck_core_capability_definitions_code_competency"
REVISION_CHECK = "ck_core_capability_definitions_semantic_revision"
STATUS_CHECK = "ck_pedagogical_taxonomy_releases_status"
RELEASE_STATUSES = ("candidate", "active", "retired")
COHERENCE_SQL = "split_part(capability_code, '_', 1) = competency_code"
CHECKS = {
    RELEASES: {STATUS_CHECK: _in_sql("status", RELEASE_STATUSES)},
    DEFS: {
        CODE_CHECK: _in_sql("capability_code", CAPABILITY_CODES),
        COMPETENCY_CHECK: _in_sql("competency_code", COMPETENCY_CODES),
        COHERENCE_CHECK: COHERENCE_SQL,
        REVISION_CHECK: "semantic_revision >= 1",
    },
    MEMBERSHIPS: {},
    OBS_CAPS: {},
}
UNIQUES = {
    RELEASES: {
        "uq_pedagogical_taxonomy_releases_version_key": ["version_key"],
        "uq_pedagogical_taxonomy_releases_spec_fingerprint": ["spec_fingerprint"],
    },
    DEFS: {"uq_core_capability_definitions_code_revision": ["capability_code", "semantic_revision"]},
    MEMBERSHIPS: {
        "uq_capability_taxonomy_memberships_release_definition": ["taxonomy_release_id", "capability_definition_id"],
    },
    OBS_CAPS: {},
}
INDEXES = {
    RELEASES: {ONE_ACTIVE_INDEX: (["status"], True, "status = 'active'")},
    DEFS: {},
    MEMBERSHIPS: {MEMBERSHIP_DEFINITION_INDEX: (["capability_definition_id"], False, None)},
    OBS_CAPS: {OBSERVATION_MEMBERSHIP_INDEX: (["capability_membership_id"], False, None)},
}


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_exactly_0001_to_0007():
    """0001 -> ... -> 0006 -> 0007, tête unique = 0007 ; 0007 est la seule
    migration ajoutée par T4-A."""
    script = _script_directory()
    assert script.get_heads() == [T4A]
    assert script.get_bases() == [BASELINE]

    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert set(revisions) == {BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A}
    assert revisions[T4A].down_revision == T3A
    assert revisions[T3A].down_revision == T2A

    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [f"{rev}.py" for rev in (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A)]


def test_revision_id_fits_alembic_version_column():
    """alembic_version.version_num est un VARCHAR(32)."""
    assert len(T4A) <= 32
    for rev in _script_directory().walk_revisions():
        assert len(rev.revision) <= 32, rev.revision


def test_0001_to_0006_files_are_unchanged():
    """Migrations historiques immuables (dont 0006 : la colonne
    pedagogical_taxonomy_release_id n'est pas recréée, seule sa FK est
    ajoutée par 0007)."""
    versions = REPO_ROOT / "alembic" / "versions"
    for rev, expected in (
        (BASELINE, BASELINE_SHA256),
        (T1A, T1A_SHA256),
        (T1B1, T1B1_SHA256),
        (T1C2, T1C2_SHA256),
        (T2A, T2A_SHA256),
        (T3A, T3A_SHA256),
    ):
        assert hashlib.sha256((versions / f"{rev}.py").read_bytes()).hexdigest() == expected, rev


def test_capability_vocabulary_is_exactly_45_frozen_codes():
    """45 codes exactement, uniques, préfixés par une compétence C1-C12 :
    3 capacités (A-C) pour C1-C3, 4 (A-D) pour C4-C12."""
    assert len(CAPABILITY_CODES) == 45
    assert len(set(CAPABILITY_CODES)) == 45
    assert len(COMPETENCY_CODES) == 12
    for competency in COMPETENCY_CODES:
        letters = [code.split("_")[1] for code in CAPABILITY_CODES if code.split("_")[0] == competency]
        expected = ["A", "B", "C"] if competency in ("C1", "C2", "C3") else ["A", "B", "C", "D"]
        assert letters == expected, competency
    assert {code.split("_")[0] for code in CAPABILITY_CODES} == set(COMPETENCY_CODES)
    # Le CHECK du modèle et celui de la migration portent exactement ces 45 codes.
    check = str(next(c.sqltext for c in CoreCapabilityDefinition.__table__.constraints
                     if getattr(c, "name", None) == CODE_CHECK))
    assert check.count("'") == 2 * 45
    assert check == _in_sql("capability_code", CAPABILITY_CODES)
    migration = (REPO_ROOT / "alembic" / "versions" / f"{T4A}.py").read_text(encoding="utf-8")
    for code in CAPABILITY_CODES:
        assert migration.count(f"'{code}'") == 1, code


def _checks_sql(table) -> str:
    return ", ".join(f"CONSTRAINT {name} CHECK ({sql})" for name, sql in CHECKS[table].items())


def test_offline_sql_of_0007_upgrade_is_exactly_the_t4a_structure():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "upgrade", f"{T3A}:{T4A}", "--sql",
    ).stdout
    stmts = _statements(sql)
    assert stmts[:6] == [
        "CREATE TABLE pedagogical_taxonomy_releases ( id UUID NOT NULL, version_key VARCHAR NOT NULL, "
        "status VARCHAR NOT NULL, spec_fingerprint VARCHAR NOT NULL, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL, activated_at TIMESTAMP WITH TIME ZONE, "
        "PRIMARY KEY (id), "
        f"{_checks_sql(RELEASES)}, "
        "CONSTRAINT uq_pedagogical_taxonomy_releases_version_key UNIQUE (version_key), "
        "CONSTRAINT uq_pedagogical_taxonomy_releases_spec_fingerprint UNIQUE (spec_fingerprint) )",
        f"CREATE UNIQUE INDEX {ONE_ACTIVE_INDEX} ON pedagogical_taxonomy_releases (status) "
        "WHERE status = 'active'",
        "CREATE TABLE core_capability_definitions ( id UUID NOT NULL, capability_code VARCHAR NOT NULL, "
        "semantic_revision INTEGER NOT NULL, competency_code VARCHAR NOT NULL, label VARCHAR NOT NULL, "
        "definition TEXT NOT NULL, mapping_guidance JSONB NOT NULL, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "PRIMARY KEY (id), "
        f"{_checks_sql(DEFS)}, "
        "CONSTRAINT uq_core_capability_definitions_code_revision UNIQUE (capability_code, semantic_revision) )",
        "CREATE TABLE capability_taxonomy_memberships ( id UUID NOT NULL, taxonomy_release_id UUID NOT NULL, "
        "capability_definition_id UUID NOT NULL, created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "PRIMARY KEY (id), "
        "FOREIGN KEY(taxonomy_release_id) REFERENCES pedagogical_taxonomy_releases (id), "
        "FOREIGN KEY(capability_definition_id) REFERENCES core_capability_definitions (id), "
        "CONSTRAINT uq_capability_taxonomy_memberships_release_definition "
        "UNIQUE (taxonomy_release_id, capability_definition_id) )",
        f"CREATE INDEX {MEMBERSHIP_DEFINITION_INDEX} ON capability_taxonomy_memberships (capability_definition_id)",
        "CREATE TABLE observation_capabilities ( observation_id UUID NOT NULL, "
        "capability_membership_id UUID NOT NULL, created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "PRIMARY KEY (observation_id, capability_membership_id), "
        "FOREIGN KEY(observation_id) REFERENCES pedagogical_observations (id), "
        "FOREIGN KEY(capability_membership_id) REFERENCES capability_taxonomy_memberships (id) )",
    ]
    assert stmts[6] == (
        f"CREATE INDEX {OBSERVATION_MEMBERSHIP_INDEX} ON observation_capabilities (capability_membership_id)"
    )
    # Garde-fou : valeur historique orpheline => exception, avant la FK.
    assert stmts[7].startswith(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM observation_evaluation_runs r "
        "WHERE r.pedagogical_taxonomy_release_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM pedagogical_taxonomy_releases p WHERE p.id = r.pedagogical_taxonomy_release_id)) "
        "THEN RAISE EXCEPTION 'T4-A : "
    )
    assert stmts[7].endswith("avant la migration 0007_pedagogical_taxonomy.'; END IF; END $$")
    assert stmts[8:] == [
        f"CREATE INDEX {RELEASE_INDEX} ON observation_evaluation_runs (pedagogical_taxonomy_release_id)",
        f"ALTER TABLE observation_evaluation_runs ADD CONSTRAINT {RELEASE_FK} "
        "FOREIGN KEY(pedagogical_taxonomy_release_id) REFERENCES pedagogical_taxonomy_releases (id)",
        # Seule écriture de données : la version Alembic (aucun seed).
        "UPDATE alembic_version SET version_num='0007_pedagogical_taxonomy' "
        "WHERE alembic_version.version_num = '0006_observation_layer'",
    ]
    for forbidden in ("DROP ", "INSERT", "DELETE", "CREATE TYPE", "CREATE EXTENSION", "CREATE TRIGGER",
                      "CREATE FUNCTION", "DEFAULT", "ON DELETE", "ON UPDATE", "CASCADE", "ENUM",
                      "ADD COLUMN", "ALTER COLUMN", "SET NOT NULL", "LINEAGE", "USER_CAPABILIT"):
        assert forbidden not in sql.upper(), forbidden
    # Seul ALTER TABLE : la FK sur la colonne existante (jamais recréée).
    assert sql.upper().count("ALTER TABLE") == 1
    # Seul UPDATE : alembic_version (aucun backfill de pedagogical_taxonomy_release_id).
    assert [s for s in stmts if s.upper().startswith("UPDATE")] == [stmts[-1]]


def test_offline_sql_of_0007_downgrade_drops_only_t4a():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "downgrade", f"{T4A}:{T3A}", "--sql",
    ).stdout
    assert _statements(sql) == [
        f"ALTER TABLE observation_evaluation_runs DROP CONSTRAINT {RELEASE_FK}",
        f"DROP INDEX {RELEASE_INDEX}",
        f"DROP INDEX {OBSERVATION_MEMBERSHIP_INDEX}",
        "DROP TABLE observation_capabilities",
        f"DROP INDEX {MEMBERSHIP_DEFINITION_INDEX}",
        "DROP TABLE capability_taxonomy_memberships",
        "DROP TABLE core_capability_definitions",
        f"DROP INDEX {ONE_ACTIVE_INDEX}",
        "DROP TABLE pedagogical_taxonomy_releases",
        "UPDATE alembic_version SET version_num='0006_observation_layer' "
        "WHERE alembic_version.version_num = '0007_pedagogical_taxonomy'",
    ]
    # La colonne pedagogical_taxonomy_release_id (0006) n'est jamais supprimée.
    for forbidden in ("CASCADE", "DROP COLUMN", "DELETE", "INSERT"):
        assert forbidden not in sql.upper(), forbidden


def test_metadata_declares_exactly_the_four_t4a_tables():
    assert set(Base.metadata.tables) == REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES
    for model, name in zip(T4A_MODELS, (RELEASES, DEFS, MEMBERSHIPS, OBS_CAPS)):
        assert model.__tablename__ == name
        assert model.__table__ is Base.metadata.tables[name]


@pytest.mark.parametrize("model", T4A_MODELS, ids=lambda m: m.__name__)
def test_columns_types_nullability_and_primary_keys(model):
    table = model.__table__
    assert [c.name for c in table.columns] == COLUMNS[table.name]
    assert [c.name for c in table.primary_key.columns] == PRIMARY_KEYS[table.name]
    assert {c.name: c.nullable for c in table.columns} == {
        n: (table.name, n) in NULLABLE for n in COLUMNS[table.name]
    }
    for col in table.columns:
        key = (table.name, col.name)
        if key in UUID_COLUMNS:
            assert isinstance(col.type, sa.Uuid) and col.type.as_uuid is True, key
        elif key in JSONB_COLUMNS:
            assert type(col.type) is JSONB, key
        elif key in TIMESTAMPTZ_COLUMNS:
            assert isinstance(col.type, sa.DateTime) and col.type.timezone is True, key
        elif key in TEXT_COLUMNS:
            assert type(col.type) is sa.Text, key
        elif key in INTEGER_COLUMNS:
            assert type(col.type) is sa.Integer, key
        else:
            # Tout le reste : VARCHAR sans longueur.
            assert type(col.type) is sa.String and col.type.length is None, key


def test_observation_capability_has_a_composite_primary_key_and_no_id():
    table = ObservationCapability.__table__
    assert "id" not in table.c
    assert [c.name for c in table.primary_key.columns] == ["observation_id", "capability_membership_id"]
    assert sa.inspect(ObservationCapability).primary_key == (
        table.c.observation_id, table.c.capability_membership_id,
    )


@pytest.mark.parametrize("model", T4A_MODELS, ids=lambda m: m.__name__)
def test_foreign_keys_without_cascade(model):
    table = model.__table__
    fks = sorted((fk.parent.name, fk.target_fullname) for fk in table.foreign_keys)
    assert fks == FOREIGN_KEYS[table.name]
    for fk in table.foreign_keys:
        assert fk.ondelete is None and fk.onupdate is None, fk.parent.name
        assert type(fk.column.type) is type(fk.parent.type), fk.parent.name


def test_run_release_column_gets_a_real_nullable_foreign_key():
    """FK T3 -> T4 sur la colonne EXISTANTE, qui reste UUID nullable, sans
    cascade, avec son index ; aucune autre colonne des runs ne change."""
    col = ObservationEvaluationRun.__table__.c.pedagogical_taxonomy_release_id
    assert isinstance(col.type, sa.Uuid) and col.nullable is True
    (fk,) = col.foreign_keys
    assert (fk.target_fullname, fk.constraint.name) == (f"{RELEASES}.id", RELEASE_FK)
    assert fk.ondelete is None and fk.onupdate is None
    assert col.server_default is None and col.default is None
    (index,) = [i for i in ObservationEvaluationRun.__table__.indexes if i.name == RELEASE_INDEX]
    assert [c.name for c in index.columns] == ["pedagogical_taxonomy_release_id"] and not index.unique
    assert len(RELEASE_FK) <= 63 and len(RELEASE_INDEX) <= 63


@pytest.mark.parametrize("model", T4A_MODELS, ids=lambda m: m.__name__)
def test_check_unique_and_index_definitions(model):
    table = model.__table__
    checks = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert checks == CHECKS[table.name]
    uniques = {c.name: [col.name for col in c.columns]
               for c in table.constraints if isinstance(c, sa.UniqueConstraint)}
    assert uniques == UNIQUES[table.name]
    indexes = {}
    for index in table.indexes:
        where = index.dialect_options["postgresql"]["where"]
        indexes[index.name] = ([c.name for c in index.columns], index.unique, None if where is None else str(where))
    assert indexes == INDEXES[table.name]
    assert not any(c.unique or c.index for c in table.columns), table.name
    for name in list(checks) + list(uniques) + list(indexes):
        assert len(name) <= 63, name


def test_no_server_defaults_only_application_defaults():
    """Aucun server_default ; UUID (sauf PK composite) et created_at générés
    par l'application (UTC aware) ; aucune autre valeur inventée (ni
    status, ni semantic_revision, ni mapping_guidance, ni activated_at)."""
    def call(default):
        assert default is not None and default.is_callable
        return default.arg(None)

    for model in T4A_MODELS:
        table = model.__table__
        for col in table.columns:
            assert col.server_default is None and col.server_onupdate is None, (table.name, col.name)
            assert col.onupdate is None, (table.name, col.name)
            if col.name == "created_at":
                assert call(col.default).utcoffset().total_seconds() == 0
            elif col.name == "id":
                assert isinstance(call(col.default), uuid.UUID)
                assert call(col.default) != call(col.default)
            else:
                assert col.default is None, (table.name, col.name)


def test_jsonb_and_timestamptz_columns():
    jsonb, tz = set(), set()
    for model in T4A_MODELS:
        for col in model.__table__.columns:
            if isinstance(col.type, sa.JSON):
                assert type(col.type) is JSONB
                # Un None Python devient NULL SQL (refusé), jamais le JSON null.
                assert col.type.none_as_null is True, col.name
                jsonb.add((col.table.name, col.name))
            if isinstance(col.type, sa.DateTime):
                assert col.type.timezone is True, col.name
                tz.add((col.table.name, col.name))
    assert jsonb == JSONB_COLUMNS
    assert tz == TIMESTAMPTZ_COLUMNS


def test_no_relationships_foreign_keys_are_the_source_of_truth():
    for model in (*T4A_MODELS, ObservationEvaluationRun, PedagogicalObservation):
        assert not sa.inspect(model).relationships, model.__name__


def test_models_module_contains_no_service_logic():
    """models.py ne déclare que des modèles : aucune fonction hors du helper
    d'horodatage, aucune méthode sur les modèles T4-A."""
    import ast
    tree = ast.parse((REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8"))
    assert {n.name for n in tree.body if isinstance(n, ast.FunctionDef)} == {"_utcnow_aware"}
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    for model in T4A_MODELS:
        body = classes[model.__name__].body
        assert not [n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))], model.__name__


# --- anti-dérive -------------------------------------------------------------

FORBIDDEN_COLUMN_FRAGMENTS = (
    "user", "stage", "score", "weight", "confidence", "evidence", "polarity", "master", "progress",
    "percent", "count", "priority", "target", "next_", "level", "xp", "rank", "order", "ordinal",
    "lineage", "parent", "predecessor", "supersed", "coverage", "acquired", "valid",
)


def test_no_user_state_score_or_progression_in_t4a_tables():
    """Une capacité ne sait jamais où en est l'utilisateur : aucune colonne
    d'état utilisateur, de niveau, de score, de confiance, de progression,
    de poids, d'ordre ni de lignée. Seule la release porte un status (son
    cycle de vie, pas celui d'un utilisateur)."""
    for model in T4A_MODELS:
        for col in model.__table__.columns:
            assert not any(word in col.name for word in FORBIDDEN_COLUMN_FRAGMENTS), (model.__name__, col.name)
            if col.name == "status":
                assert model is PedagogicalTaxonomyRelease
    for table in Base.metadata.tables:
        for fragment in ("user_capabilit", "capability_state", "capability_stage", "capability_score",
                         "capability_master", "capability_progress", "capability_confidence",
                         "lineage", "competenc"):
            assert fragment not in table, table


def test_no_t4b_t4c_service_seed_or_lineage_files():
    """T4-A ne crée ni service de taxonomie / mapping, ni seed / bootstrap
    des 45 capacités, ni release V1. Depuis T4-B, le seul service est
    core/taxonomy_service.py (voir tests/test_taxonomy_service.py) : aucun
    autre module de service, aucun seed."""
    assert (REPO_ROOT / "core" / "taxonomy_service.py").exists()
    forbidden = {
        "capability_service.py", "capability_mapping.py",
        "seed_taxonomy.py", "bootstrap_taxonomy.py", "taxonomy_v1.json", "capabilities_v1.py",
    }
    found = [p for p in REPO_ROOT.rglob("*")
             if p.name in forbidden and ".git" not in p.parts and "node_modules" not in p.parts]
    assert found == []


def _application_sources():
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or rel.startswith(("tests/", ".git/")) or "node_modules" in path.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            source = _code_tokens(source)
        yield rel, source


def test_no_t4b_operation_anywhere():
    """Les opérations de cycle de vie / mapping n'existent que dans le
    service T4-B core/taxonomy_service.py ; aucune opération hors périmètre
    (retrait direct, validation de contenu, fingerprint calculé) n'existe
    nulle part (hors docstrings et commentaires)."""
    t4b_operations = ("create_candidate_release", "activate_release", "map_observation_capability")
    never = ("retire_release", "reuse_capability_definition", "validate_taxonomy",
             "calculate_spec_fingerprint")
    checked = 0
    for rel, source in _application_sources():
        checked += 1
        for name in never:
            assert name not in source, (rel, name)
        if rel != "core/taxonomy_service.py":
            for name in t4b_operations:
                assert name not in source, (rel, name)
    assert checked > 0


def test_t4a_tables_are_not_wired_to_the_application():
    """Aucun branchement : hors core/models.py et la migration 0007, aucun
    code applicatif (api.py, core/ dont le service T3-B, scripts/,
    frontend) ne mentionne ces modèles ou ces tables. Aucune route T4.
    Depuis T4-B, seul le service core/taxonomy_service.py (lui-même non
    branché ; core/observation_service.py n'appelle que son contrôle
    interne, sans nommer ces modèles) les manipule."""
    allowed = {"core/models.py", f"alembic/versions/{T4A}.py", "core/taxonomy_service.py"}
    needles = (*(m.__name__ for m in T4A_MODELS), RELEASES, "core_capability_definition",
               "capability_taxonomy_membership", "observation_capabilit")
    checked = 0
    for rel, source in _application_sources():
        checked += 1
        if rel not in allowed:
            for needle in needles:
                assert needle not in source, (rel, needle)
    assert checked > 0
    api = _code_tokens((REPO_ROOT / "api.py").read_text(encoding="utf-8")).lower()
    for word in ("taxonom", "capabilit"):
        assert word not in api, word


def test_no_llm_or_runtime_schema_mutation_in_t4a():
    """Ni la migration ni les modèles n'appellent un LLM ; aucun
    create_all ni mutation de schéma hors Alembic."""
    import ast
    migration = REPO_ROOT / "alembic" / "versions" / f"{T4A}.py"
    for path, expected_imports in (
        (migration, {"typing", "alembic", "sqlalchemy", "sqlalchemy.dialects"}),
        (REPO_ROOT / "core" / "models.py",
         {"uuid", "datetime", "sqlalchemy", "sqlalchemy.dialects.postgresql", "core.db"}),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        assert imported == expected_imports, path.name
        tokens = _code_tokens(path.read_text(encoding="utf-8")).lower()
        for word in ("anthropic", "openai", "llm", "create_all", "requests", "httpx"):
            assert word not in tokens, (path.name, word)


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

INSERT_RELEASE = sa.text(
    "INSERT INTO pedagogical_taxonomy_releases (id, version_key, status, spec_fingerprint, created_at, "
    "activated_at) VALUES (:id, :version_key, :status, :spec_fingerprint, :created_at, :activated_at)"
)
INSERT_DEFINITION = sa.text(
    "INSERT INTO core_capability_definitions (id, capability_code, semantic_revision, competency_code, "
    "label, definition, mapping_guidance, created_at) VALUES (:id, :capability_code, :semantic_revision, "
    ":competency_code, :label, :definition, CAST(:mapping_guidance AS JSONB), :created_at)"
)
INSERT_MEMBERSHIP = sa.text(
    "INSERT INTO capability_taxonomy_memberships (id, taxonomy_release_id, capability_definition_id, "
    "created_at) VALUES (:id, :release, :definition, :created_at)"
)
INSERT_MAPPING = sa.text(
    "INSERT INTO observation_capabilities (observation_id, capability_membership_id, created_at) "
    "VALUES (:observation, :membership, :created_at)"
)


def _release(conn, **overrides) -> uuid.UUID:
    release_id = overrides.pop("id", uuid.uuid4())
    params = {
        "id": release_id,
        "version_key": f"v-{release_id}",
        "status": "candidate",
        "spec_fingerprint": f"sha256:{release_id}",
        "created_at": NOW,
        "activated_at": None,
    }
    params.update(overrides)
    conn.execute(INSERT_RELEASE, params)
    return params["id"]


def _definition(conn, code="C7_A", revision=1, **overrides) -> uuid.UUID:
    params = {
        "id": uuid.uuid4(),
        "capability_code": code,
        "semantic_revision": revision,
        "competency_code": code.split("_")[0],
        "label": f"Libellé {code}",
        "definition": f"Définition de test de {code} (révision {revision}).",
        "mapping_guidance": '{"include": ["exemple"], "exclude": []}',
        "created_at": NOW,
    }
    params.update(overrides)
    conn.execute(INSERT_DEFINITION, params)
    return params["id"]


def _membership(conn, release_id, definition_id) -> uuid.UUID:
    membership_id = uuid.uuid4()
    conn.execute(INSERT_MEMBERSHIP, {"id": membership_id, "release": release_id, "definition": definition_id,
                                     "created_at": NOW})
    return membership_id


def _map(conn, observation_id, membership_id) -> None:
    conn.execute(INSERT_MAPPING, {"observation": observation_id, "membership": membership_id, "created_at": NOW})


def _observation(conn, competency="C7", localization="localized", **overrides) -> uuid.UUID:
    run_id = _run(conn, _event(conn))
    return _obs(conn, run_id, 1, competency_code=competency, capability_localization=localization, **overrides)


def _refused(conn, match, action, *, isolate=()):
    """`action` doit être refusée par PostgreSQL avec une erreur contenant
    `match`. `isolate` : autres CHECK de core_capability_definitions
    retirés le temps du test (dans un savepoint annulé) pour vérifier un
    CHECK seul."""
    with pytest.raises(sa.exc.IntegrityError, match=match):
        with conn.begin_nested():
            for name in isolate:
                conn.execute(sa.text(f"ALTER TABLE {DEFS} DROP CONSTRAINT {name}"))
            action()


@pytest.fixture
def conn(pg_url, pg_engine):
    """Schéma = head (0007) + deux utilisateurs et une analysis_session ;
    tout ce que fait le test est annulé."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()


# --- A / B. upgrade et tables -------------------------------------------------

def test_pg_upgrade_from_empty_database_to_head(pg_url, pg_engine):
    """Base vide -> head = 0007 ; Base.metadata == schéma migré ; aucun
    ENUM, trigger ni fonction ; les quatre tables T4 sont VIDES (aucun
    seed, aucune release, aucune capacité)."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    assert _version(pg_engine) == T4A
    assert _tables(pg_engine) == (REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES
                                  | {"alembic_version"})
    assert _compare_metadata(pg_engine) == []
    catalog = _global_catalog(pg_engine)
    assert (catalog["enums"], catalog["triggers"], catalog["functions"]) == (0, 0, 0)
    # Aucune séquence T4 (UUID applicatifs) : seules celles des tables historiques.
    assert not [s for s in catalog["sequences"] if any(t in s for t in T4A_TABLES)]
    assert _data(pg_engine, T4A_TABLES) == {name: [] for name in sorted(T4A_TABLES)}


def _pg_columns(table) -> list:
    types = {}
    for (t, n) in UUID_COLUMNS:
        types[(t, n)] = "uuid"
    for (t, n) in TIMESTAMPTZ_COLUMNS:
        types[(t, n)] = "timestamp with time zone"
    types.update({key: "jsonb" for key in JSONB_COLUMNS})
    types.update({key: "text" for key in TEXT_COLUMNS})
    types.update({key: "integer" for key in INTEGER_COLUMNS})
    return [(n, types.get((table, n), "character varying"), "YES" if (table, n) in NULLABLE else "NO", None)
            for n in COLUMNS[table]]


def _indexdefs(engine, table) -> dict:
    with engine.connect() as conn:
        return dict(conn.execute(sa.text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"
        ), {"t": table}).all())


def test_pg_exact_structure_of_the_four_tables(pg_url, pg_engine):
    """C. Colonnes, types, nullabilité, PK, FK (NO ACTION), CHECK, UNIQUE et
    index exacts, lus dans le catalogue PostgreSQL."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T4A)
    for table in T4A_TABLES:
        assert _catalog_columns(pg_engine, table) == _pg_columns(table), table
    assert _constraints(pg_engine, RELEASES) == sorted([
        (STATUS_CHECK, "c", _pg_in_check("status", RELEASE_STATUSES), " ", " "),
        ("pedagogical_taxonomy_releases_pkey", "p", "PRIMARY KEY (id)", " ", " "),
        ("uq_pedagogical_taxonomy_releases_spec_fingerprint", "u", "UNIQUE (spec_fingerprint)", " ", " "),
        ("uq_pedagogical_taxonomy_releases_version_key", "u", "UNIQUE (version_key)", " ", " "),
    ])
    assert _constraints(pg_engine, DEFS) == sorted([
        (CODE_CHECK, "c", _pg_in_check("capability_code", CAPABILITY_CODES), " ", " "),
        (COHERENCE_CHECK, "c",
         "CHECK ((split_part((capability_code)::text, '_'::text, 1) = (competency_code)::text))", " ", " "),
        (COMPETENCY_CHECK, "c", _pg_in_check("competency_code", COMPETENCY_CODES), " ", " "),
        (REVISION_CHECK, "c", "CHECK ((semantic_revision >= 1))", " ", " "),
        ("core_capability_definitions_pkey", "p", "PRIMARY KEY (id)", " ", " "),
        ("uq_core_capability_definitions_code_revision", "u", "UNIQUE (capability_code, semantic_revision)",
         " ", " "),
    ])
    assert _constraints(pg_engine, MEMBERSHIPS) == sorted([
        ("capability_taxonomy_memberships_capability_definition_id_fkey", "f",
         "FOREIGN KEY (capability_definition_id) REFERENCES core_capability_definitions(id)", "a", "a"),
        ("capability_taxonomy_memberships_pkey", "p", "PRIMARY KEY (id)", " ", " "),
        ("capability_taxonomy_memberships_taxonomy_release_id_fkey", "f",
         "FOREIGN KEY (taxonomy_release_id) REFERENCES pedagogical_taxonomy_releases(id)", "a", "a"),
        ("uq_capability_taxonomy_memberships_release_definition", "u",
         "UNIQUE (taxonomy_release_id, capability_definition_id)", " ", " "),
    ])
    assert _constraints(pg_engine, OBS_CAPS) == sorted([
        ("observation_capabilities_capability_membership_id_fkey", "f",
         "FOREIGN KEY (capability_membership_id) REFERENCES capability_taxonomy_memberships(id)", "a", "a"),
        ("observation_capabilities_observation_id_fkey", "f",
         "FOREIGN KEY (observation_id) REFERENCES pedagogical_observations(id)", "a", "a"),
        ("observation_capabilities_pkey", "p", "PRIMARY KEY (observation_id, capability_membership_id)", " ", " "),
    ])
    assert _indexdefs(pg_engine, RELEASES) == {
        "pedagogical_taxonomy_releases_pkey":
            "CREATE UNIQUE INDEX pedagogical_taxonomy_releases_pkey ON public.pedagogical_taxonomy_releases "
            "USING btree (id)",
        "uq_pedagogical_taxonomy_releases_spec_fingerprint":
            "CREATE UNIQUE INDEX uq_pedagogical_taxonomy_releases_spec_fingerprint "
            "ON public.pedagogical_taxonomy_releases USING btree (spec_fingerprint)",
        "uq_pedagogical_taxonomy_releases_version_key":
            "CREATE UNIQUE INDEX uq_pedagogical_taxonomy_releases_version_key "
            "ON public.pedagogical_taxonomy_releases USING btree (version_key)",
        ONE_ACTIVE_INDEX:
            f"CREATE UNIQUE INDEX {ONE_ACTIVE_INDEX} ON public.pedagogical_taxonomy_releases "
            "USING btree (status) WHERE ((status)::text = 'active'::text)",
    }
    assert _indexdefs(pg_engine, DEFS) == {
        "core_capability_definitions_pkey":
            "CREATE UNIQUE INDEX core_capability_definitions_pkey ON public.core_capability_definitions "
            "USING btree (id)",
        "uq_core_capability_definitions_code_revision":
            "CREATE UNIQUE INDEX uq_core_capability_definitions_code_revision "
            "ON public.core_capability_definitions USING btree (capability_code, semantic_revision)",
    }
    # taxonomy_release_id : couvert par l'UNIQUE (première colonne) ;
    # capability_definition_id : index dédié.
    assert _indexdefs(pg_engine, MEMBERSHIPS) == {
        "capability_taxonomy_memberships_pkey":
            "CREATE UNIQUE INDEX capability_taxonomy_memberships_pkey "
            "ON public.capability_taxonomy_memberships USING btree (id)",
        "uq_capability_taxonomy_memberships_release_definition":
            "CREATE UNIQUE INDEX uq_capability_taxonomy_memberships_release_definition "
            "ON public.capability_taxonomy_memberships USING btree (taxonomy_release_id, capability_definition_id)",
        MEMBERSHIP_DEFINITION_INDEX:
            f"CREATE INDEX {MEMBERSHIP_DEFINITION_INDEX} ON public.capability_taxonomy_memberships "
            "USING btree (capability_definition_id)",
    }
    # observation_id : couvert par la PK composite (première colonne) ;
    # capability_membership_id : index dédié.
    assert _indexdefs(pg_engine, OBS_CAPS) == {
        "observation_capabilities_pkey":
            "CREATE UNIQUE INDEX observation_capabilities_pkey ON public.observation_capabilities "
            "USING btree (observation_id, capability_membership_id)",
        OBSERVATION_MEMBERSHIP_INDEX:
            f"CREATE INDEX {OBSERVATION_MEMBERSHIP_INDEX} ON public.observation_capabilities "
            "USING btree (capability_membership_id)",
    }
    with pg_engine.connect() as conn:
        # Aucune FK de la base n'est en cascade / set null / set default.
        assert conn.execute(sa.text(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
            "AND (confdeltype <> 'a' OR confupdtype <> 'a')"
        )).scalar_one() == 0


def test_pg_run_release_column_foreign_key_and_index(pg_url, pg_engine):
    """La colonne existante reste UUID nullable ; seules la FK T4 et son
    index sont ajoutés à observation_evaluation_runs."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T3A)
    columns_0006 = _catalog_columns(pg_engine, RUNS)
    constraints_0006 = _constraints(pg_engine, RUNS)
    indexes_0006 = _indexdefs(pg_engine, RUNS)
    _run_alembic(pg_url, "upgrade", T4A)
    assert _catalog_columns(pg_engine, RUNS) == columns_0006
    assert ("pedagogical_taxonomy_release_id", "uuid", "YES", None) in columns_0006
    assert _constraints(pg_engine, RUNS) == sorted(constraints_0006 + [
        (RELEASE_FK, "f",
         "FOREIGN KEY (pedagogical_taxonomy_release_id) REFERENCES pedagogical_taxonomy_releases(id)", "a", "a"),
    ])
    assert _indexdefs(pg_engine, RUNS) == {
        **indexes_0006,
        RELEASE_INDEX: f"CREATE INDEX {RELEASE_INDEX} ON public.observation_evaluation_runs "
                       "USING btree (pedagogical_taxonomy_release_id)",
    }


@pytest.mark.parametrize("table", sorted(T4A_TABLES))
def test_pg_required_columns_are_not_null(conn, table):
    """Toute colonne (sauf activated_at) est NOT NULL, dont
    mapping_guidance (aucun JSON null ni défaut inventé)."""
    release, definition = _release(conn), _definition(conn)
    membership = _membership(conn, release, definition)
    observation = _observation(conn)

    def insert(column):
        if table == RELEASES:
            return lambda: _release(conn, **{column: None})
        if table == DEFS:
            return lambda: _definition(conn, "C7_B", **{column: None})
        if table == MEMBERSHIPS:
            params = {"id": uuid.uuid4(), "release": _release(conn), "definition": definition, "created_at": NOW}
            key = {"taxonomy_release_id": "release", "capability_definition_id": "definition"}.get(column, column)
            return lambda: conn.execute(INSERT_MEMBERSHIP, {**params, key: None})
        params = {"observation": observation, "membership": membership, "created_at": NOW}
        key = {"observation_id": "observation", "capability_membership_id": "membership"}.get(column, column)
        return lambda: conn.execute(INSERT_MAPPING, {**params, key: None})

    for column in COLUMNS[table]:
        if (table, column) in NULLABLE:
            continue
        _refused(conn, f'null value in column "{column}"', insert(column))


# --- D / E / F / G. releases ----------------------------------------------------

def test_pg_release_status_vocabulary(conn):
    """D. candidate, active, retired acceptés ; toute autre valeur refusée."""
    for status in RELEASE_STATUSES:
        _release(conn, status=status, activated_at=NOW if status != "candidate" else None)
    for status in ("", "Active", "ACTIVE", "draft", "published", "archived", "superseded", "active "):
        _refused(conn, STATUS_CHECK, lambda: _release(conn, status=status))
    assert _count(conn, RELEASES) == 3


def test_pg_version_key_and_spec_fingerprint_are_unique(conn):
    """E / F."""
    _release(conn, version_key="v1", spec_fingerprint="sha256:a")
    _refused(conn, "uq_pedagogical_taxonomy_releases_version_key",
             lambda: _release(conn, version_key="v1", spec_fingerprint="sha256:b"))
    _refused(conn, "uq_pedagogical_taxonomy_releases_spec_fingerprint",
             lambda: _release(conn, version_key="v2", spec_fingerprint="sha256:a"))
    _release(conn, version_key="v2", spec_fingerprint="sha256:b")
    assert _count(conn, RELEASES) == 2


def test_pg_at_most_one_active_release(conn):
    """G. Plusieurs candidate et retired autorisées ; une seule active,
    garantie par l'index unique partiel (INSERT et UPDATE)."""
    for status in ("candidate", "candidate", "retired", "retired"):
        _release(conn, status=status)
    active = _release(conn, status="active", activated_at=NOW)
    _refused(conn, ONE_ACTIVE_INDEX, lambda: _release(conn, status="active", activated_at=NOW))
    candidate = _release(conn)
    promote = sa.text("UPDATE pedagogical_taxonomy_releases SET status = 'active', activated_at = now() "
                      "WHERE id = :id")
    _refused(conn, ONE_ACTIVE_INDEX, lambda: conn.execute(promote, {"id": candidate}))
    # Bascule dans une même transaction : l'ancienne active est retirée
    # d'abord (le schéma ne gère pas les transitions : T4-B).
    conn.execute(sa.text("UPDATE pedagogical_taxonomy_releases SET status = 'retired' WHERE id = :id"),
                 {"id": active})
    conn.execute(promote, {"id": candidate})
    assert conn.execute(sa.text(
        "SELECT id FROM pedagogical_taxonomy_releases WHERE status = 'active'"
    )).scalars().all() == [candidate]
    assert _count(conn, RELEASES, "status = 'retired'") == 3


def test_pg_concurrent_active_releases_are_serialized(pg_url, pg_engine):
    """L'invariant tient sous concurrence : la seconde transaction qui
    insère une release active attend la première (index unique) puis
    échoue quand celle-ci commite."""
    _upgrade_head_with_users(pg_url, pg_engine)
    try:
        with pg_engine.connect() as holder, pg_engine.connect() as waiter:
            holder_pid, waiter_pid = _backend_pid(holder), _backend_pid(waiter)
            holder.commit()
            waiter.commit()
            holder.begin()
            _release(holder, version_key="first", status="active", activated_at=NOW)
            outcome = {}

            def work():
                try:
                    with waiter.begin():
                        _release(waiter, version_key="second", status="active", activated_at=NOW)
                except Exception as exc:  # noqa: BLE001 — restitué au test
                    outcome["error"] = exc

            thread = threading.Thread(target=work)
            thread.start()
            try:
                _wait_until_blocked_by(pg_engine, waiter_pid, holder_pid)
                assert not outcome
                holder.commit()
            finally:
                if holder.in_transaction():
                    holder.rollback()
                thread.join(timeout=10)
            assert not thread.is_alive()
            assert isinstance(outcome.get("error"), sa.exc.IntegrityError)
            assert ONE_ACTIVE_INDEX in str(outcome["error"])
        with pg_engine.connect() as check:
            assert check.execute(sa.text(
                "SELECT version_key FROM pedagogical_taxonomy_releases"
            )).scalars().all() == ["first"]
    finally:
        with pg_engine.begin() as cleanup:
            cleanup.execute(sa.text("DELETE FROM pedagogical_taxonomy_releases"))


# --- H / I / J / K / L. définitions -----------------------------------------------

def test_pg_all_45_capability_codes_are_accepted(conn):
    """H. Les 45 codes, chacun avec sa compétence, sont acceptés."""
    for code in CAPABILITY_CODES:
        _definition(conn, code)
    assert _count(conn, DEFS) == 45
    assert conn.execute(sa.text(
        "SELECT count(DISTINCT competency_code) FROM core_capability_definitions"
    )).scalar_one() == 12


@pytest.mark.parametrize("code", [
    "", "C1_D", "C2_D", "C3_D", "C4_E", "C12_E", "C13_A", "C0_A", "c7_a", "C7_a", "C7A", "C7-A", "C7_",
    "C7_A ", " C7_A", "C7_AA", "C07_A", "C7", "C7_A_1", "C10A", "_A",
])
def test_pg_unknown_capability_code_is_refused(conn, code):
    """H. Tout code hors des 45 est refusé par le CHECK du vocabulaire
    (vérifié seul, les autres CHECK retirés dans un savepoint annulé)."""
    _refused(conn, CODE_CHECK, lambda: _definition(conn, code, competency_code="C1"),
             isolate=(COHERENCE_CHECK,))


def test_pg_competency_code_vocabulary(conn):
    """I. C1-C12 acceptés ; C13, C0 et valeurs arbitraires refusés (CHECK
    vérifié seul)."""
    for competency in COMPETENCY_CODES:
        _definition(conn, f"{competency}_A", competency_code=competency)
    for value in ("C13", "C0", "c7", "", "C07", "7", "Cx", "C7 ", "competence", "C1_A"):
        _refused(conn, COMPETENCY_CHECK, lambda: _definition(conn, "C7_B", competency_code=value),
                 isolate=(COHERENCE_CHECK,))
    assert _count(conn, DEFS) == 12


@pytest.mark.parametrize("code, competency", [
    ("C7_A", "C7"), ("C10_D", "C10"), ("C1_A", "C1"), ("C11_B", "C11"), ("C12_D", "C12"), ("C3_C", "C3"),
])
def test_pg_capability_competency_coherence_accepted(conn, code, competency):
    """J."""
    _definition(conn, code, competency_code=competency)
    assert _count(conn, DEFS) == 1


@pytest.mark.parametrize("code, competency", [
    ("C7_A", "C8"), ("C10_D", "C1"), ("C1_A", "C10"), ("C1_B", "C11"), ("C12_B", "C1"), ("C11_A", "C1"),
    ("C2_A", "C12"),
])
def test_pg_capability_competency_incoherence_refused(conn, code, competency):
    """J. La compétence doit être exactement le préfixe du code (C1 n'est
    pas un préfixe valide de C10_*)."""
    _refused(conn, COHERENCE_CHECK, lambda: _definition(conn, code, competency_code=competency))
    # Même refus par UPDATE après coup.
    definition = _definition(conn, code)
    _refused(conn, COHERENCE_CHECK, lambda: conn.execute(sa.text(
        "UPDATE core_capability_definitions SET competency_code = :c WHERE id = :id"
    ), {"c": competency, "id": definition}))


def test_pg_semantic_revision_must_be_at_least_one(conn):
    """K."""
    _definition(conn, "C7_A", 1)
    _definition(conn, "C7_A", 2)
    for revision in (0, -1, -100):
        _refused(conn, REVISION_CHECK, lambda: _definition(conn, "C7_B", revision))
    assert _count(conn, DEFS) == 2


def test_pg_capability_code_and_revision_are_unique(conn):
    """L. Même code + même révision refusé ; nouvelle révision = nouvelle
    ligne (nouvel UUID), l'ancienne reste intacte."""
    first = _definition(conn, "C7_A", 1, definition="Sens initial.")
    _refused(conn, "uq_core_capability_definitions_code_revision",
             lambda: _definition(conn, "C7_A", 1, definition="Autre sens."))
    second = _definition(conn, "C7_A", 2, definition="Nouveau sens.")
    assert first != second
    assert conn.execute(sa.text(
        "SELECT semantic_revision, definition FROM core_capability_definitions "
        "WHERE capability_code = 'C7_A' ORDER BY semantic_revision"
    )).all() == [(1, "Sens initial."), (2, "Nouveau sens.")]


def test_pg_mapping_guidance_is_free_jsonb(conn):
    """Aucun mini-schéma imposé en base : tout JSON (objet, liste) est
    conservé tel quel ; texte long en TEXT jamais tronqué."""
    long_definition = "Définition détaillée. " * 2000
    guidance = ('{"include": ["relier le ROE aux capitaux propres"], "exclude": ["calcul du ROA"], '
                '"boundary_notes": "voir C7_B", "neighbours": {"C7_B": "application"}}')
    definition = _definition(conn, "C7_A", mapping_guidance=guidance, definition=long_definition)
    _definition(conn, "C7_B", mapping_guidance="[]")
    _definition(conn, "C7_C", mapping_guidance="{}")
    assert conn.execute(sa.text(
        "SELECT mapping_guidance, definition FROM core_capability_definitions WHERE id = :id"
    ), {"id": definition}).one() == (
        {"include": ["relier le ROE aux capitaux propres"], "exclude": ["calcul du ROA"],
         "boundary_notes": "voir C7_B", "neighbours": {"C7_B": "application"}},
        long_definition,
    )


# --- M. memberships --------------------------------------------------------------

def test_pg_memberships(conn):
    """M. FK release et définition valides ; doublon exact refusé ; une
    définition inchangée réutilisée par deux releases."""
    v1, v2 = _release(conn, version_key="V1"), _release(conn, version_key="V2")
    c10_a = _definition(conn, "C10_A", 1)
    c10_b_r1, c10_b_r2 = _definition(conn, "C10_B", 1), _definition(conn, "C10_B", 2)
    _membership(conn, v1, c10_a)
    _membership(conn, v1, c10_b_r1)
    _membership(conn, v2, c10_a)          # même definition_id réutilisée
    _membership(conn, v2, c10_b_r2)       # sens changé : nouvelle révision
    _refused(conn, "uq_capability_taxonomy_memberships_release_definition", lambda: _membership(conn, v1, c10_a))
    _refused(conn, "capability_taxonomy_memberships_taxonomy_release_id_fkey",
             lambda: _membership(conn, uuid.uuid4(), c10_a))
    _refused(conn, "capability_taxonomy_memberships_capability_definition_id_fkey",
             lambda: _membership(conn, v1, uuid.uuid4()))
    assert conn.execute(sa.text(
        "SELECT r.version_key, d.capability_code, d.semantic_revision FROM capability_taxonomy_memberships m "
        "JOIN pedagogical_taxonomy_releases r ON r.id = m.taxonomy_release_id "
        "JOIN core_capability_definitions d ON d.id = m.capability_definition_id ORDER BY 1, 2, 3"
    )).all() == [("V1", "C10_A", 1), ("V1", "C10_B", 1), ("V2", "C10_A", 1), ("V2", "C10_B", 2)]


def test_pg_known_limit_two_revisions_of_a_code_in_one_release_is_not_refused_by_the_schema(conn):
    """LIMITE DOCUMENTÉE : « une release ne contient jamais deux révisions
    du même capability_code » traverse memberships + definitions. T4-A ne
    l'impose volontairement ni par trigger ni par dénormalisation : T4-B le
    garantira transactionnellement. Ce test fige ce comportement pour que
    tout changement soit délibéré."""
    release = _release(conn)
    _membership(conn, release, _definition(conn, "C10_B", 1))
    _membership(conn, release, _definition(conn, "C10_B", 2))
    assert _count(conn, MEMBERSHIPS) == 2


# --- N. observation_capabilities ---------------------------------------------------

def test_pg_observation_capabilities(conn):
    """N. FK observation et membership valides ; doublon exact refusé ; une
    observation localisée sur plusieurs capacités reste UNE observation."""
    release = _release(conn)
    c7_a = _membership(conn, release, _definition(conn, "C7_A"))
    c7_b = _membership(conn, release, _definition(conn, "C7_B"))
    observation = _observation(conn, "C7")
    _map(conn, observation, c7_a)
    _map(conn, observation, c7_b)
    _refused(conn, "observation_capabilities_pkey", lambda: _map(conn, observation, c7_a))
    _refused(conn, "observation_capabilities_observation_id_fkey", lambda: _map(conn, uuid.uuid4(), c7_a))
    _refused(conn, "observation_capabilities_capability_membership_id_fkey",
             lambda: _map(conn, observation, uuid.uuid4()))
    assert _count(conn, OBS_CAPS, "observation_id = :o", {"o": observation}) == 2
    # Toujours une seule preuve.
    assert _count(conn, OBS) == 1


def test_pg_known_limits_of_observation_localization_are_left_to_t4b(conn):
    """LIMITES DOCUMENTÉES (T4-B, transactionnel) : la base n'empêche pas
    encore une observation C7 d'être liée à une capacité C8, ni une
    observation competency_only d'avoir des localisations, ni une
    observation localized d'en avoir zéro. Une FK simple ne peut pas le
    garantir sans dénormalisation ni trigger, volontairement absents."""
    release = _release(conn)
    c8_a = _membership(conn, release, _definition(conn, "C8_A"))
    _map(conn, _observation(conn, "C7", "localized"), c8_a)
    _map(conn, _observation(conn, "C8", "competency_only"), c8_a)
    _observation(conn, "C7", "localized")
    assert _count(conn, OBS_CAPS) == 2 and _count(conn, OBS) == 3


# --- O. FK T3 -> T4 ------------------------------------------------------------------

def test_pg_run_taxonomy_release_foreign_key(conn):
    """O. NULL accepté ; UUID d'une vraie release accepté ; UUID inexistant
    refusé (à l'INSERT comme à l'UPDATE)."""
    event_id = _event(conn)
    release = _release(conn)
    without = _run(conn, event_id, pedagogical_taxonomy_release_id=None)
    _run(conn, event_id, pedagogical_taxonomy_release_id=release)
    _refused(conn, RELEASE_FK, lambda: _run(conn, event_id, pedagogical_taxonomy_release_id=uuid.uuid4()))
    _refused(conn, RELEASE_FK, lambda: conn.execute(sa.text(
        "UPDATE observation_evaluation_runs SET pedagogical_taxonomy_release_id = :r WHERE id = :id"
    ), {"r": uuid.uuid4(), "id": without}))
    assert _count(conn, RUNS, "pedagogical_taxonomy_release_id IS NULL") == 1
    assert _count(conn, RUNS, "pedagogical_taxonomy_release_id = :r", {"r": release}) == 1


# --- P. aucune cascade destructive ------------------------------------------------------

def test_pg_no_destructive_cascade(conn):
    """P. Supprimer une release, une définition, un membership ou une
    observation encore référencés est refusé ; rien n'est supprimé en
    cascade."""
    release, spare_release = _release(conn), _release(conn)
    definition = _definition(conn, "C7_A")
    membership = _membership(conn, release, definition)
    observation = _observation(conn, "C7")
    _map(conn, observation, membership)
    _run(conn, _event(conn), pedagogical_taxonomy_release_id=spare_release)

    def delete(table, where, params):
        return lambda: conn.execute(sa.text(f"DELETE FROM {table} WHERE {where}"), params)

    _refused(conn, "capability_taxonomy_memberships_taxonomy_release_id_fkey",
             delete(RELEASES, "id = :id", {"id": release}))
    _refused(conn, RELEASE_FK, delete(RELEASES, "id = :id", {"id": spare_release}))
    _refused(conn, "capability_taxonomy_memberships_capability_definition_id_fkey",
             delete(DEFS, "id = :id", {"id": definition}))
    _refused(conn, "observation_capabilities_capability_membership_id_fkey",
             delete(MEMBERSHIPS, "id = :id", {"id": membership}))
    _refused(conn, "observation_capabilities_observation_id_fkey",
             delete(OBS, "id = :id", {"id": observation}))
    # Pas de cascade à la modification des clés non plus.
    _refused(conn, "capability_taxonomy_memberships_taxonomy_release_id_fkey", lambda: conn.execute(sa.text(
        "UPDATE pedagogical_taxonomy_releases SET id = :new WHERE id = :id"), {"new": uuid.uuid4(), "id": release}))
    assert [_count(conn, t) for t in (RELEASES, DEFS, MEMBERSHIPS, OBS_CAPS, OBS)] == [2, 1, 1, 1, 1]
    # Une fois la localisation retirée explicitement, l'observation n'est
    # plus bloquée par T4 (seule sa propre suppression reste une décision
    # du service T3-B).
    conn.execute(sa.text("DELETE FROM observation_capabilities"))
    conn.execute(sa.text("DELETE FROM capability_taxonomy_memberships"))
    conn.execute(sa.text("DELETE FROM core_capability_definitions"))
    conn.execute(sa.text("DELETE FROM pedagogical_taxonomy_releases WHERE id = :id"), {"id": release})


# --- ORM -----------------------------------------------------------------------------------

def test_pg_orm_insertion_uses_application_defaults(pg_url, pg_engine):
    """Insertion via l'ORM : UUID et created_at UTC aware générés côté
    application ; mapping_guidance dict -> JSONB ; PK composite ; un None
    Python sur mapping_guidance devient NULL SQL (refusé)."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with Session(pg_engine) as session:
        with session.begin():
            release = PedagogicalTaxonomyRelease(version_key="V-test", status="candidate",
                                                 spec_fingerprint="sha256:spec")
            definition = CoreCapabilityDefinition(
                capability_code="C10_B", semantic_revision=1, competency_code="C10", label="Libellé",
                definition="Définition.", mapping_guidance={"include": ["a"], "exclude": [], "notes": None},
            )
            session.add_all([release, definition])
            session.flush()
            membership = CapabilityTaxonomyMembership(taxonomy_release_id=release.id,
                                                      capability_definition_id=definition.id)
            session.add(membership)
            session.flush()
            observation_id = _observation(session.connection(), "C10")
            mapping = ObservationCapability(observation_id=observation_id, capability_membership_id=membership.id)
            session.add(mapping)
            session.flush()
            for obj in (release, definition, membership, mapping):
                session.refresh(obj)

            for obj in (release, definition, membership):
                assert isinstance(obj.id, uuid.UUID)
            for value in (release.created_at, definition.created_at, membership.created_at, mapping.created_at):
                assert value.tzinfo is not None and value.utcoffset().total_seconds() == 0
            assert release.activated_at is None
            assert definition.mapping_guidance == {"include": ["a"], "exclude": [], "notes": None}
            assert session.get(ObservationCapability, (observation_id, membership.id)) is mapping

            with pytest.raises(sa.exc.IntegrityError, match='null value in column "mapping_guidance"'):
                with session.begin_nested():
                    session.add(CoreCapabilityDefinition(
                        capability_code="C10_B", semantic_revision=2, competency_code="C10", label="l",
                        definition="d", mapping_guidance=None,
                    ))
            session.rollback()
    with pg_engine.connect() as c:
        assert [_count(c, t) for t in sorted(T4A_TABLES)] == [0, 0, 0, 0]


# --- Q. upgrade / downgrade -----------------------------------------------------------------

EXISTING = REMAINING_TABLES | T2A_TABLES | T3A_TABLES | {"alembic_version"}
DATA_TABLES = REMAINING_TABLES | T2A_TABLES | T3A_TABLES


def _seed_t3(engine) -> uuid.UUID:
    """Un run completed (sans release) et une observation (schéma 0006)."""
    with engine.begin() as conn:
        run_id = _run(conn, uuid.UUID("22222222-2222-2222-2222-222222222222"),
                      id=uuid.UUID("44444444-4444-4444-4444-444444444444"), interpretation_status="active")
        _obs(conn, run_id, 1, id=uuid.UUID("55555555-5555-5555-5555-555555555555"))
    return run_id


def _upgrade_to_0006_with_data(pg_url, pg_engine) -> None:
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T1C2)
    _seed_remaining_tables(pg_engine)
    _run_alembic(pg_url, "upgrade", T2A)
    _seed_t2(pg_engine)
    _run_alembic(pg_url, "upgrade", T3A)
    _seed_t3(pg_engine)


def _assert_absent(engine, names) -> None:
    with engine.connect() as conn:
        for name in names:
            assert conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": f"public.{name}"}).scalar_one() is None, name


def test_pg_upgrade_0006_to_0007_preserves_everything_then_downgrade(pg_url, pg_engine):
    """A. 0006 -> 0007 conserve schéma et données de toutes les tables
    existantes (runs : seule la FK et son index sont ajoutés) ; aucune
    donnée T4 créée. Q. Le downgrade supprime les quatre tables et les
    objets ajoutés aux runs, garde la colonne pedagogical_taxonomy_release_id
    et toutes les données T1/T2/T3, et restaure exactement 0006 ; le
    ré-upgrade est identique."""
    _upgrade_to_0006_with_data(pg_url, pg_engine)
    schema_0006 = _snapshot(pg_engine, EXISTING)
    catalog_0006 = _catalog(pg_engine, EXISTING)
    global_0006 = _global_catalog(pg_engine)
    data_0006 = _data(pg_engine, DATA_TABLES)
    assert all(data_0006[t] for t in DATA_TABLES)

    # --- upgrade 0006 -> 0007 --------------------------------------------
    _run_alembic(pg_url, "upgrade", T4A)
    assert _version(pg_engine) == T4A
    assert _tables(pg_engine) == EXISTING | T4A_TABLES
    unchanged = EXISTING - {RUNS}
    assert _snapshot(pg_engine, unchanged) == {t: schema_0006[t] for t in sorted(unchanged)}
    assert _catalog(pg_engine, unchanged) == {t: catalog_0006[t] for t in sorted(unchanged)}
    runs_0007 = _catalog(pg_engine, {RUNS})[RUNS]
    assert runs_0007["columns"] == catalog_0006[RUNS]["columns"]
    assert sorted(set(runs_0007["constraints"]) - set(catalog_0006[RUNS]["constraints"])) == [
        (RELEASE_FK, "f",
         "FOREIGN KEY (pedagogical_taxonomy_release_id) REFERENCES pedagogical_taxonomy_releases(id)", "a", "a"),
    ]
    assert set(runs_0007["indexes"]) - set(catalog_0006[RUNS]["indexes"]) == {RELEASE_INDEX}
    assert _data(pg_engine, DATA_TABLES) == data_0006
    # Aucun seed : ni release, ni capacité, ni membership, ni localisation.
    assert _data(pg_engine, T4A_TABLES) == {name: [] for name in sorted(T4A_TABLES)}
    assert _global_catalog(pg_engine) == global_0006
    assert _compare_metadata(pg_engine) == []
    schema_t4 = _snapshot(pg_engine, T4A_TABLES | {RUNS})
    catalog_t4 = _catalog(pg_engine, T4A_TABLES | {RUNS})

    # Données T4 (supprimées par le downgrade, comme leurs tables).
    with pg_engine.begin() as conn:
        release = _release(conn)
        membership = _membership(conn, release, _definition(conn, "C7_A"))
        _map(conn, uuid.UUID("55555555-5555-5555-5555-555555555555"), membership)

    # --- downgrade 0007 -> 0006 ------------------------------------------
    _run_alembic(pg_url, "downgrade", T3A)
    assert _version(pg_engine) == T3A
    assert _tables(pg_engine) == EXISTING
    _assert_absent(pg_engine, [*T4A_TABLES, *T4A_INDEXES])
    assert _snapshot(pg_engine, EXISTING) == schema_0006
    assert _catalog(pg_engine, EXISTING) == catalog_0006
    assert _global_catalog(pg_engine) == global_0006
    # Runs, observations, événements, aides : intacts.
    assert _data(pg_engine, DATA_TABLES) == data_0006
    assert ("pedagogical_taxonomy_release_id", "uuid", "YES", None) in _catalog_columns(pg_engine, RUNS)
    assert RELEASE_FK not in [c[0] for c in _constraints(pg_engine, RUNS)]

    # --- ré-upgrade 0006 -> 0007 -----------------------------------------
    _run_alembic(pg_url, "upgrade", T4A)
    assert _version(pg_engine) == T4A
    assert _snapshot(pg_engine, unchanged) == {t: schema_0006[t] for t in sorted(unchanged)}
    assert _snapshot(pg_engine, T4A_TABLES | {RUNS}) == schema_t4
    assert _catalog(pg_engine, T4A_TABLES | {RUNS}) == catalog_t4
    assert _data(pg_engine, DATA_TABLES) == data_0006
    assert _compare_metadata(pg_engine) == []


def _failed_upgrade(pg_url) -> subprocess.CalledProcessError:
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _run_alembic(pg_url, "upgrade", T4A)
    return exc.value


def test_pg_upgrade_refuses_orphan_release_ids_without_rewriting_them(pg_url, pg_engine):
    """§21. En 0006, pedagogical_taxonomy_release_id accepte n'importe quel
    UUID. Une valeur préexistante est forcément orpheline : l'upgrade
    échoue AVANT la FK, sans effacer ni réécrire la valeur, sans faux
    backfill ; la transaction Alembic est annulée et la base reste en 0006."""
    _upgrade_to_0006_with_data(pg_url, pg_engine)
    orphan = uuid.uuid4()
    with pg_engine.begin() as conn:
        _run(conn, uuid.UUID("22222222-2222-2222-2222-222222222222"), pedagogical_taxonomy_release_id=orphan)
    schema_before = _snapshot(pg_engine, EXISTING)
    data_before = _data(pg_engine, DATA_TABLES)

    err = _failed_upgrade(pg_url)
    assert "RuntimeError" in err.stderr
    assert "pedagogical_taxonomy_release_id sans release correspondante, FK non ajoutée" in err.stderr
    assert "(1 run(s) concerné(s))" in err.stderr

    assert _version(pg_engine) == T3A
    assert _tables(pg_engine) == EXISTING
    _assert_absent(pg_engine, [*T4A_TABLES, *T4A_INDEXES])
    assert _snapshot(pg_engine, EXISTING) == schema_before
    assert _data(pg_engine, DATA_TABLES) == data_before
    with pg_engine.connect() as conn:
        assert _count(conn, RUNS, "pedagogical_taxonomy_release_id = :r", {"r": orphan}) == 1


def test_pg_offline_guard_refuses_orphan_release_ids(pg_url, pg_engine):
    """Le script généré en mode --sql porte le même garde-fou ; sans valeur
    orpheline, il s'exécute entièrement."""
    _upgrade_to_0006_with_data(pg_url, pg_engine)
    body = _statements(_run_alembic(pg_url, "upgrade", f"{T3A}:{T4A}", "--sql").stdout)
    with pg_engine.connect() as conn:
        trans = conn.begin()
        try:
            for stmt in body:
                conn.exec_driver_sql(stmt)
            assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == T4A
        finally:
            trans.rollback()
    with pg_engine.begin() as conn:
        _run(conn, uuid.UUID("22222222-2222-2222-2222-222222222222"), pedagogical_taxonomy_release_id=uuid.uuid4())
    with pg_engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(sa.exc.DBAPIError, match="FK non ajoutée"):
                for stmt in body:
                    conn.exec_driver_sql(stmt)
        finally:
            trans.rollback()
    assert _version(pg_engine) == T3A
    _assert_absent(pg_engine, T4A_TABLES)


def test_pg_downgrade_keeps_release_values_and_reupgrade_refuses_them(pg_url, pg_engine):
    """Q. Un run évalué sous une release garde sa valeur
    pedagogical_taxonomy_release_id après le downgrade (la colonne
    appartient à 0006) ; la release ayant disparu avec sa table, le
    ré-upgrade est refusé par le garde-fou plutôt que d'effacer la valeur."""
    _upgrade_to_0006_with_data(pg_url, pg_engine)
    _run_alembic(pg_url, "upgrade", T4A)
    with pg_engine.begin() as conn:
        release = _release(conn, status="active", activated_at=NOW)
        run_id = _run(conn, uuid.UUID("22222222-2222-2222-2222-222222222222"),
                      pedagogical_taxonomy_release_id=release)
    runs_before = _data(pg_engine, {RUNS})

    _run_alembic(pg_url, "downgrade", T3A)
    assert _version(pg_engine) == T3A
    assert _data(pg_engine, {RUNS}) == runs_before
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT pedagogical_taxonomy_release_id FROM observation_evaluation_runs WHERE id = :id"
        ), {"id": run_id}).scalar_one() == release

    err = _failed_upgrade(pg_url)
    assert "FK non ajoutée" in err.stderr
    assert _version(pg_engine) == T3A
    assert _data(pg_engine, {RUNS}) == runs_before


def test_pg_models_timestamps_match_utc(pg_url, pg_engine):
    """Les created_at écrits par l'application relisent en UTC aware."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with Session(pg_engine) as session:
        with session.begin():
            release = PedagogicalTaxonomyRelease(version_key="V", status="active", spec_fingerprint="f",
                                                 activated_at=datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc))
            session.add(release)
            session.flush()
            session.refresh(release)
            assert release.activated_at == datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
            assert release.created_at.utcoffset().total_seconds() == 0
            session.rollback()
