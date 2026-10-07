"""Tests de T5-A : migration 0008_longitudinal_relations + modèles
LongitudinalAssessmentRun / LongitudinalAssessmentInput /
ObservationDependency / DependencyCapability / ObservationTransfer /
TransferCapability / ObservationRevalidation / RevalidationCapability.

Même organisation que les tests des migrations 0002 à 0007 (dont on
réutilise les helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, intégrité de 0001
   à 0007, SQL PostgreSQL généré en mode offline, métadonnées des modèles
   (colonnes, types, PK, FK, CHECK, UNIQUE, index, défauts), anti-dérive
   (aucun seed, route, LLM, score, stade, confiance, profil,
   « independent » ; depuis T5-B, aucun service T5 hors
   core/longitudinal_service.py, testé dans tests/test_longitudinal_service.py).

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t5a_test \\
           python -m pytest tests/test_migration_0008_longitudinal_relations.py

Doctrine : OBSERVATION = unité de preuve ; CAPABILITY = périmètre de la
preuve ; RELATION T5 = structure historique entre preuves, jamais une
preuve supplémentaire. T5-A = schéma + modèles seulement : aucun service,
aucune route, aucune inférence. Les invariants qui traversent plusieurs
tables (snapshot cohérent avec le user / la compétence / la release du run,
polarités, inter-événements, scope ⊆ observations, localized /
competency_only, lifecycle) relèvent de T5-B : des tests ci-dessous
documentent explicitement que la base ne les impose PAS encore.
"""
import ast
import hashlib
import threading
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from core.db import Base
from core.models import (
    DependencyCapability,
    LongitudinalAssessmentInput,
    LongitudinalAssessmentRun,
    ObservationDependency,
    ObservationRevalidation,
    ObservationTransfer,
    RevalidationCapability,
    TransferCapability,
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
    _without_r1d1_changes,
    R1B_TABLES,
    R1C1_TABLES,
    R1C2_TABLES,
    REMAINING_TABLES,
    T1B1_SHA256,
    T1C2,
    T3A_TABLES,
    T4A_TABLES,
    T5A_INDEXES,
    T5A_TABLES,
    T6A_INDEXES,
    T6A_TABLES,
    _catalog_columns,
    _code_tokens,
    _compare_metadata,
    _constraints,
    _data,
    _statements,
    _without_r1b_changes,
    _without_r1c1_changes,
    _without_r1c2_changes,
)
from tests.test_migration_0005_cognitive_support_traces import (
    OTHER_USER,
    T1C2_SHA256,
    T2A,
    T2A_TABLES,
    USER,
    _catalog,
    _global_catalog,
    _trace,
    _upgrade_head_with_users,
)
from tests.test_migration_0006_observation_layer import (
    INSERT_EVENT,
    NOW,
    OBS,
    T2A_SHA256,
    T3A,
    _backend_pid,
    _count,
    _event,
    _in_sql,
    _obs,
    _pg_in_check,
    _run,
    _wait_until_blocked_by,
)
from tests.test_migration_0007_pedagogical_taxonomy import (
    COMPETENCY_CODES,
    T3A_SHA256,
    T4A,
    _assert_absent,
    _definition,
    _indexdefs,
    _map,
    _membership,
    _observation,
    _release,
    _upgrade_to_0006_with_data,
)

T5A = "0008_longitudinal_relations"
# sha256 de alembic/versions/0007_pedagogical_taxonomy.py tel que mergé sur
# main (8466a43, après T4-C ; 0007 introduite par T4-A, PR #181).
T4A_SHA256 = "a58a8dabb29cfba9a157f0ae0138ed631c84d2b2945ecd7e4d159392e9808a38"

ASSESSMENTS = "longitudinal_assessment_runs"
INPUTS = "longitudinal_assessment_inputs"
DEPS = "observation_dependencies"
DEP_CAPS = "dependency_capabilities"
TRANSFERS = "observation_transfers"
TRANSFER_CAPS = "transfer_capabilities"
REVALS = "observation_revalidations"
REVAL_CAPS = "revalidation_capabilities"
TABLES_IN_ORDER = (ASSESSMENTS, INPUTS, DEPS, DEP_CAPS, TRANSFERS, TRANSFER_CAPS, REVALS, REVAL_CAPS)
assert set(TABLES_IN_ORDER) == T5A_TABLES and len(TABLES_IN_ORDER) == 8
T5A_MODELS = (LongitudinalAssessmentRun, LongitudinalAssessmentInput, ObservationDependency, DependencyCapability,
              ObservationTransfer, TransferCapability, ObservationRevalidation, RevalidationCapability)
EDGE_TABLES = (DEPS, TRANSFERS, REVALS)
# Table de périmètre -> (colonne de l'arête, table de l'arête).
SCOPE_TABLES = {DEP_CAPS: ("dependency_id", DEPS), TRANSFER_CAPS: ("transfer_id", TRANSFERS),
                REVAL_CAPS: ("revalidation_id", REVALS)}
# Arête -> (colonne source observation, colonne cible observation).
EDGE_COLUMNS = {DEPS: ("source_observation_id", "target_observation_id"),
                TRANSFERS: ("source_observation_id", "target_observation_id"),
                REVALS: ("source_contradiction_observation_id", "target_supportive_observation_id")}
MEMBERSHIPS = "capability_taxonomy_memberships"
RELEASES = "pedagogical_taxonomy_releases"

ONE_ACTIVE_INDEX = "uq_longitudinal_assessment_runs_one_active_user_competency"
DEDUP_UNIQUE = "uq_longitudinal_assessment_runs_dedup_key"
# Noms explicites : les noms automatiques PostgreSQL (<table>_<colonne>_fkey)
# feraient respectivement 65, 66 et 63 caractères.
RUN_RELEASE_FK = "longitudinal_assessment_runs_taxonomy_release_id_fkey"
REVAL_SOURCE_FK = "observation_revalidations_source_contradiction_fkey"
REVAL_TARGET_FK = "observation_revalidations_target_supportive_fkey"

EXECUTION_STATUSES = ("running", "completed", "failed")
INTERPRETATION_STATUSES = ("candidate", "active", "superseded", "obsolete")
SOURCE_KINDS = ("observation", "support_trace")
# « independent » volontairement ABSENT.
DEPENDENCY_TYPES = ("dependent", "partially_dependent")
SCOPE_MODES = ("whole_observation", "localized", "competency_only")

COLUMNS = {
    ASSESSMENTS: ["id", "user_id", "competency_code", "execution_status", "interpretation_status", "trigger",
                  "pedagogical_taxonomy_release_id", "dependency_version", "transfer_version",
                  "revalidation_version", "relation_schema_version", "input_fingerprint", "assessment_dedup_key",
                  "started_at", "completed_at", "created_at", "failure_code"],
    INPUTS: ["run_id", "observation_id"],
    DEPS: ["id", "run_id", "target_observation_id", "source_kind", "source_observation_id",
           "source_support_trace_id", "dependency_type", "scope_mode", "dependency_basis", "scope_fingerprint",
           "created_at"],
    DEP_CAPS: ["dependency_id", "capability_membership_id"],
    TRANSFERS: ["id", "run_id", "source_observation_id", "target_observation_id", "scope_mode", "transfer_basis",
                "scope_fingerprint", "created_at"],
    TRANSFER_CAPS: ["transfer_id", "capability_membership_id"],
    REVALS: ["id", "run_id", "source_contradiction_observation_id", "target_supportive_observation_id",
             "scope_mode", "revalidation_basis", "scope_fingerprint", "created_at"],
    REVAL_CAPS: ["revalidation_id", "capability_membership_id"],
}
NULLABLE = {
    (ASSESSMENTS, "completed_at"), (ASSESSMENTS, "failure_code"),
    (DEPS, "source_observation_id"), (DEPS, "source_support_trace_id"),
}
UUID_COLUMNS = {
    (ASSESSMENTS, "id"), (ASSESSMENTS, "pedagogical_taxonomy_release_id"),
    (INPUTS, "run_id"), (INPUTS, "observation_id"),
    (DEPS, "id"), (DEPS, "run_id"), (DEPS, "target_observation_id"), (DEPS, "source_observation_id"),
    (DEPS, "source_support_trace_id"),
    (DEP_CAPS, "dependency_id"), (DEP_CAPS, "capability_membership_id"),
    (TRANSFERS, "id"), (TRANSFERS, "run_id"), (TRANSFERS, "source_observation_id"),
    (TRANSFERS, "target_observation_id"),
    (TRANSFER_CAPS, "transfer_id"), (TRANSFER_CAPS, "capability_membership_id"),
    (REVALS, "id"), (REVALS, "run_id"), (REVALS, "source_contradiction_observation_id"),
    (REVALS, "target_supportive_observation_id"),
    (REVAL_CAPS, "revalidation_id"), (REVAL_CAPS, "capability_membership_id"),
}
TIMESTAMPTZ_COLUMNS = {
    (ASSESSMENTS, "started_at"), (ASSESSMENTS, "completed_at"), (ASSESSMENTS, "created_at"),
    (DEPS, "created_at"), (TRANSFERS, "created_at"), (REVALS, "created_at"),
}
JSONB_COLUMNS = {(DEPS, "dependency_basis"), (TRANSFERS, "transfer_basis"), (REVALS, "revalidation_basis")}
PRIMARY_KEYS = {
    ASSESSMENTS: ["id"], INPUTS: ["run_id", "observation_id"],
    DEPS: ["id"], DEP_CAPS: ["dependency_id", "capability_membership_id"],
    TRANSFERS: ["id"], TRANSFER_CAPS: ["transfer_id", "capability_membership_id"],
    REVALS: ["id"], REVAL_CAPS: ["revalidation_id", "capability_membership_id"],
}
# (colonne, cible, nom PostgreSQL) — nom automatique sauf les trois explicites.
FOREIGN_KEYS = {
    ASSESSMENTS: [
        ("pedagogical_taxonomy_release_id", f"{RELEASES}.id", RUN_RELEASE_FK),
        ("user_id", "users.id", "longitudinal_assessment_runs_user_id_fkey"),
    ],
    INPUTS: [
        ("observation_id", f"{OBS}.id", "longitudinal_assessment_inputs_observation_id_fkey"),
        ("run_id", f"{ASSESSMENTS}.id", "longitudinal_assessment_inputs_run_id_fkey"),
    ],
    DEPS: [
        ("run_id", f"{ASSESSMENTS}.id", "observation_dependencies_run_id_fkey"),
        ("source_observation_id", f"{OBS}.id", "observation_dependencies_source_observation_id_fkey"),
        ("source_support_trace_id", "support_traces.id", "observation_dependencies_source_support_trace_id_fkey"),
        ("target_observation_id", f"{OBS}.id", "observation_dependencies_target_observation_id_fkey"),
    ],
    DEP_CAPS: [
        ("capability_membership_id", f"{MEMBERSHIPS}.id", "dependency_capabilities_capability_membership_id_fkey"),
        ("dependency_id", f"{DEPS}.id", "dependency_capabilities_dependency_id_fkey"),
    ],
    TRANSFERS: [
        ("run_id", f"{ASSESSMENTS}.id", "observation_transfers_run_id_fkey"),
        ("source_observation_id", f"{OBS}.id", "observation_transfers_source_observation_id_fkey"),
        ("target_observation_id", f"{OBS}.id", "observation_transfers_target_observation_id_fkey"),
    ],
    TRANSFER_CAPS: [
        ("capability_membership_id", f"{MEMBERSHIPS}.id", "transfer_capabilities_capability_membership_id_fkey"),
        ("transfer_id", f"{TRANSFERS}.id", "transfer_capabilities_transfer_id_fkey"),
    ],
    REVALS: [
        ("run_id", f"{ASSESSMENTS}.id", "observation_revalidations_run_id_fkey"),
        ("source_contradiction_observation_id", f"{OBS}.id", REVAL_SOURCE_FK),
        ("target_supportive_observation_id", f"{OBS}.id", REVAL_TARGET_FK),
    ],
    REVAL_CAPS: [
        ("capability_membership_id", f"{MEMBERSHIPS}.id",
         "revalidation_capabilities_capability_membership_id_fkey"),
        ("revalidation_id", f"{REVALS}.id", "revalidation_capabilities_revalidation_id_fkey"),
    ],
}
EXPLICIT_FK_NAMES = {RUN_RELEASE_FK, REVAL_SOURCE_FK, REVAL_TARGET_FK}

SOURCE_KIND_CHECK = "ck_observation_dependencies_source_kind"
SOURCE_XOR_CHECK = "ck_observation_dependencies_source_xor"
NO_SELF_CHECK = "ck_observation_dependencies_no_self_dependency"
DEPENDENCY_TYPE_CHECK = "ck_observation_dependencies_dependency_type"
TRANSFER_DISTINCT_CHECK = "ck_observation_transfers_distinct_observations"
REVAL_DISTINCT_CHECK = "ck_observation_revalidations_distinct_observations"
SOURCE_XOR_SQL = (
    "(source_kind = 'observation' AND source_observation_id IS NOT NULL "
    "AND source_support_trace_id IS NULL) "
    "OR (source_kind = 'support_trace' AND source_observation_id IS NULL "
    "AND source_support_trace_id IS NOT NULL)"
)
NO_SELF_SQL = "source_observation_id IS NULL OR source_observation_id <> target_observation_id"
TRANSFER_DISTINCT_SQL = "source_observation_id <> target_observation_id"
REVAL_DISTINCT_SQL = "source_contradiction_observation_id <> target_supportive_observation_id"
CHECKS = {
    ASSESSMENTS: {
        "ck_longitudinal_assessment_runs_competency_code": _in_sql("competency_code", COMPETENCY_CODES),
        "ck_longitudinal_assessment_runs_execution_status": _in_sql("execution_status", EXECUTION_STATUSES),
        "ck_longitudinal_assessment_runs_interpretation_status":
            _in_sql("interpretation_status", INTERPRETATION_STATUSES),
    },
    INPUTS: {},
    DEPS: {
        SOURCE_KIND_CHECK: _in_sql("source_kind", SOURCE_KINDS),
        SOURCE_XOR_CHECK: SOURCE_XOR_SQL,
        NO_SELF_CHECK: NO_SELF_SQL,
        DEPENDENCY_TYPE_CHECK: _in_sql("dependency_type", DEPENDENCY_TYPES),
        "ck_observation_dependencies_scope_mode": _in_sql("scope_mode", SCOPE_MODES),
    },
    DEP_CAPS: {},
    TRANSFERS: {
        TRANSFER_DISTINCT_CHECK: TRANSFER_DISTINCT_SQL,
        "ck_observation_transfers_scope_mode": _in_sql("scope_mode", SCOPE_MODES),
    },
    TRANSFER_CAPS: {},
    REVALS: {
        REVAL_DISTINCT_CHECK: REVAL_DISTINCT_SQL,
        "ck_observation_revalidations_scope_mode": _in_sql("scope_mode", SCOPE_MODES),
    },
    REVAL_CAPS: {},
}
UNIQUES = {table: {} for table in TABLES_IN_ORDER}
UNIQUES[ASSESSMENTS] = {DEDUP_UNIQUE: ["assessment_dedup_key"]}
INDEXES = {
    ASSESSMENTS: {
        ONE_ACTIVE_INDEX: (["user_id", "competency_code"], True, "interpretation_status = 'active'"),
        "ix_longitudinal_assessment_runs_user_id": (["user_id"], False, None),
        "ix_longitudinal_assessment_runs_taxonomy_release_id": (["pedagogical_taxonomy_release_id"], False, None),
    },
    INPUTS: {"ix_longitudinal_assessment_inputs_observation_id": (["observation_id"], False, None)},
    DEPS: {
        "ix_observation_dependencies_run_id": (["run_id"], False, None),
        "ix_observation_dependencies_target_observation_id": (["target_observation_id"], False, None),
        "ix_observation_dependencies_source_observation_id": (["source_observation_id"], False, None),
        "ix_observation_dependencies_source_support_trace_id": (["source_support_trace_id"], False, None),
    },
    DEP_CAPS: {"ix_dependency_capabilities_capability_membership_id": (["capability_membership_id"], False, None)},
    TRANSFERS: {
        "ix_observation_transfers_run_id": (["run_id"], False, None),
        "ix_observation_transfers_source_observation_id": (["source_observation_id"], False, None),
        "ix_observation_transfers_target_observation_id": (["target_observation_id"], False, None),
    },
    TRANSFER_CAPS: {"ix_transfer_capabilities_capability_membership_id": (["capability_membership_id"], False, None)},
    REVALS: {
        "ix_observation_revalidations_run_id": (["run_id"], False, None),
        "ix_observation_revalidations_source_contradiction": (["source_contradiction_observation_id"], False, None),
        "ix_observation_revalidations_target_supportive": (["target_supportive_observation_id"], False, None),
    },
    REVAL_CAPS: {
        "ix_revalidation_capabilities_capability_membership_id": (["capability_membership_id"], False, None),
    },
}
assert {name for table in INDEXES.values() for name in table} == T5A_INDEXES


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_0001_to_0008():
    """0001 -> ... -> 0007 -> 0008 ; 0008 est la seule migration ajoutée
    par T5-A. La tête de chaîne évolue avec les migrations suivantes (T6-A :
    voir tests/test_migration_0009_competency_inference_state.py) ; on
    vérifie ici les maillons jusqu'à 0008."""
    script = _script_directory()
    assert script.get_bases() == [BASELINE]

    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert {BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A} <= set(revisions)
    assert revisions[T5A].down_revision == T4A
    assert revisions[T4A].down_revision == T3A

    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[:8] == [f"{rev}.py" for rev in (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A)]


def test_revision_id_fits_alembic_version_column():
    """alembic_version.version_num est un VARCHAR(32)."""
    assert len(T5A) == 27
    for rev in _script_directory().walk_revisions():
        assert len(rev.revision) <= 32, rev.revision


def test_0001_to_0007_files_are_unchanged():
    """Migrations historiques immuables : T5-A ne modifie aucune table ni
    migration T0-T4."""
    versions = REPO_ROOT / "alembic" / "versions"
    for rev, expected in (
        (BASELINE, BASELINE_SHA256),
        (T1A, T1A_SHA256),
        (T1B1, T1B1_SHA256),
        (T1C2, T1C2_SHA256),
        (T2A, T2A_SHA256),
        (T3A, T3A_SHA256),
        (T4A, T4A_SHA256),
    ):
        assert hashlib.sha256((versions / f"{rev}.py").read_bytes()).hexdigest() == expected, rev


def _create_table_sql(table, columns, constraints) -> str:
    return f"CREATE TABLE {table} ( " + ", ".join(columns + constraints) + " )"


def _checks_sql(table) -> list:
    return [f"CONSTRAINT {name} CHECK ({sql})" for name, sql in CHECKS[table].items()]


def _scope_table_sql(table) -> list:
    relation_column, relation_table = SCOPE_TABLES[table]
    index = next(iter(INDEXES[table]))
    return [
        _create_table_sql(table, [f"{relation_column} UUID NOT NULL", "capability_membership_id UUID NOT NULL"], [
            f"PRIMARY KEY ({relation_column}, capability_membership_id)",
            f"FOREIGN KEY({relation_column}) REFERENCES {relation_table} (id)",
            "FOREIGN KEY(capability_membership_id) REFERENCES capability_taxonomy_memberships (id)",
        ]),
        f"CREATE INDEX {index} ON {table} (capability_membership_id)",
    ]


def _index_sql(table) -> list:
    return [f"CREATE INDEX {name} ON {table} ({columns[0]})"
            for name, (columns, unique, _) in INDEXES[table].items() if not unique]


def test_offline_sql_of_0008_upgrade_is_exactly_the_t5a_structure():
    """Huit CREATE TABLE et leurs index, dans l'ordre des dépendances ;
    aucun ALTER d'une table existante, aucune donnée, aucun objet hors
    tables / index."""
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "upgrade", f"{T4A}:{T5A}", "--sql",
    ).stdout
    expected = [
        _create_table_sql(ASSESSMENTS, [
            "id UUID NOT NULL", "user_id VARCHAR NOT NULL", "competency_code VARCHAR NOT NULL",
            "execution_status VARCHAR NOT NULL", "interpretation_status VARCHAR NOT NULL",
            "trigger VARCHAR NOT NULL", "pedagogical_taxonomy_release_id UUID NOT NULL",
            "dependency_version VARCHAR NOT NULL", "transfer_version VARCHAR NOT NULL",
            "revalidation_version VARCHAR NOT NULL", "relation_schema_version VARCHAR NOT NULL",
            "input_fingerprint VARCHAR NOT NULL", "assessment_dedup_key VARCHAR NOT NULL",
            "started_at TIMESTAMP WITH TIME ZONE NOT NULL", "completed_at TIMESTAMP WITH TIME ZONE",
            "created_at TIMESTAMP WITH TIME ZONE NOT NULL", "failure_code VARCHAR",
        ], ["PRIMARY KEY (id)", *_checks_sql(ASSESSMENTS),
            "FOREIGN KEY(user_id) REFERENCES users (id)",
            f"CONSTRAINT {RUN_RELEASE_FK} FOREIGN KEY(pedagogical_taxonomy_release_id) "
            "REFERENCES pedagogical_taxonomy_releases (id)",
            f"CONSTRAINT {DEDUP_UNIQUE} UNIQUE (assessment_dedup_key)"]),
        f"CREATE UNIQUE INDEX {ONE_ACTIVE_INDEX} ON {ASSESSMENTS} (user_id, competency_code) "
        "WHERE interpretation_status = 'active'",
        *_index_sql(ASSESSMENTS),
        _create_table_sql(INPUTS, ["run_id UUID NOT NULL", "observation_id UUID NOT NULL"], [
            "PRIMARY KEY (run_id, observation_id)",
            f"FOREIGN KEY(run_id) REFERENCES {ASSESSMENTS} (id)",
            "FOREIGN KEY(observation_id) REFERENCES pedagogical_observations (id)",
        ]),
        *_index_sql(INPUTS),
        _create_table_sql(DEPS, [
            "id UUID NOT NULL", "run_id UUID NOT NULL", "target_observation_id UUID NOT NULL",
            "source_kind VARCHAR NOT NULL", "source_observation_id UUID", "source_support_trace_id UUID",
            "dependency_type VARCHAR NOT NULL", "scope_mode VARCHAR NOT NULL", "dependency_basis JSONB NOT NULL",
            "scope_fingerprint VARCHAR NOT NULL", "created_at TIMESTAMP WITH TIME ZONE NOT NULL",
        ], ["PRIMARY KEY (id)", *_checks_sql(DEPS),
            f"FOREIGN KEY(run_id) REFERENCES {ASSESSMENTS} (id)",
            "FOREIGN KEY(target_observation_id) REFERENCES pedagogical_observations (id)",
            "FOREIGN KEY(source_observation_id) REFERENCES pedagogical_observations (id)",
            "FOREIGN KEY(source_support_trace_id) REFERENCES support_traces (id)"]),
        *_index_sql(DEPS),
        *_scope_table_sql(DEP_CAPS),
        _create_table_sql(TRANSFERS, [
            "id UUID NOT NULL", "run_id UUID NOT NULL", "source_observation_id UUID NOT NULL",
            "target_observation_id UUID NOT NULL", "scope_mode VARCHAR NOT NULL", "transfer_basis JSONB NOT NULL",
            "scope_fingerprint VARCHAR NOT NULL", "created_at TIMESTAMP WITH TIME ZONE NOT NULL",
        ], ["PRIMARY KEY (id)", *_checks_sql(TRANSFERS),
            f"FOREIGN KEY(run_id) REFERENCES {ASSESSMENTS} (id)",
            "FOREIGN KEY(source_observation_id) REFERENCES pedagogical_observations (id)",
            "FOREIGN KEY(target_observation_id) REFERENCES pedagogical_observations (id)"]),
        *_index_sql(TRANSFERS),
        *_scope_table_sql(TRANSFER_CAPS),
        _create_table_sql(REVALS, [
            "id UUID NOT NULL", "run_id UUID NOT NULL", "source_contradiction_observation_id UUID NOT NULL",
            "target_supportive_observation_id UUID NOT NULL", "scope_mode VARCHAR NOT NULL",
            "revalidation_basis JSONB NOT NULL", "scope_fingerprint VARCHAR NOT NULL",
            "created_at TIMESTAMP WITH TIME ZONE NOT NULL",
        ], ["PRIMARY KEY (id)", *_checks_sql(REVALS),
            f"FOREIGN KEY(run_id) REFERENCES {ASSESSMENTS} (id)",
            f"CONSTRAINT {REVAL_SOURCE_FK} FOREIGN KEY(source_contradiction_observation_id) "
            "REFERENCES pedagogical_observations (id)",
            f"CONSTRAINT {REVAL_TARGET_FK} FOREIGN KEY(target_supportive_observation_id) "
            "REFERENCES pedagogical_observations (id)"]),
        *_index_sql(REVALS),
        *_scope_table_sql(REVAL_CAPS),
        # Seule écriture de données : la version Alembic (aucun seed, aucun backfill).
        "UPDATE alembic_version SET version_num='0008_longitudinal_relations' "
        "WHERE alembic_version.version_num = '0007_pedagogical_taxonomy'",
    ]
    assert _statements(sql) == expected
    upper = sql.upper()
    for forbidden in ("DROP ", "INSERT", "DELETE", "ALTER TABLE", "CREATE TYPE", "CREATE EXTENSION",
                      "CREATE TRIGGER", "CREATE FUNCTION", "CREATE PROCEDURE", "CREATE MATERIALIZED",
                      "CREATE VIEW", "DEFAULT", "ON DELETE", "ON UPDATE", "CASCADE", "ENUM", "SEQUENCE",
                      "SERIAL", "'INDEPENDENT'", "SCORE", "CONFIDENCE", "STAGE", "WEIGHT", "MASTERY",
                      "PROGRESS", "PROFILE", "ACTIVE_HISTORY", "CLOSURE", "PREDECESSOR", "EVENT_ID"):
        assert forbidden not in upper, forbidden
    assert [s for s in _statements(sql) if s.upper().startswith("UPDATE")] == [expected[-1]]


def test_offline_sql_of_0008_downgrade_drops_only_t5a():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "downgrade", f"{T5A}:{T4A}", "--sql",
    ).stdout
    expected = []
    for table in reversed(TABLES_IN_ORDER):
        expected += [f"DROP INDEX {name}" for name in reversed(list(INDEXES[table]))
                     if name != ONE_ACTIVE_INDEX]
        if table == ASSESSMENTS:
            expected.append(f"DROP INDEX {ONE_ACTIVE_INDEX}")
        expected.append(f"DROP TABLE {table}")
    expected.append("UPDATE alembic_version SET version_num='0007_pedagogical_taxonomy' "
                    "WHERE alembic_version.version_num = '0008_longitudinal_relations'")
    assert _statements(sql) == expected
    # Aucune table T0-T4 touchée.
    for forbidden in ("CASCADE", "DROP COLUMN", "ALTER TABLE", "DELETE", "INSERT"):
        assert forbidden not in sql.upper(), forbidden
    dropped = [s.split()[-1] for s in expected if s.startswith("DROP TABLE")]
    assert set(dropped) == T5A_TABLES and len(dropped) == 8


def test_metadata_declares_exactly_the_eight_t5a_tables():
    """Les huit tables T5-A (+ les six tables T6-A, testées à part)."""
    assert set(Base.metadata.tables) == (REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES
                                         | T6A_TABLES | R1B_TABLES | R1C1_TABLES | R1C2_TABLES)
    for model, name in zip(T5A_MODELS, TABLES_IN_ORDER):
        assert model.__tablename__ == name
        assert model.__table__ is Base.metadata.tables[name]


@pytest.mark.parametrize("model", T5A_MODELS, ids=lambda m: m.__name__)
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
        else:
            # Tout le reste (dont user_id, comme users.id) : VARCHAR sans longueur.
            assert type(col.type) is sa.String and col.type.length is None, key


@pytest.mark.parametrize("model", [LongitudinalAssessmentInput, DependencyCapability, TransferCapability,
                                   RevalidationCapability], ids=lambda m: m.__name__)
def test_snapshot_and_scope_tables_are_pure_composite_keys(model):
    """Aucun id artificiel, aucun poids, score, stade ni horodatage : deux
    colonnes, toutes deux dans la PK composite."""
    table = model.__table__
    assert "id" not in table.c
    assert len(table.columns) == 2
    assert sa.inspect(model).primary_key == tuple(table.primary_key.columns)
    assert list(table.primary_key.columns) == list(table.columns)


@pytest.mark.parametrize("model", T5A_MODELS, ids=lambda m: m.__name__)
def test_foreign_keys_without_cascade_and_with_safe_names(model):
    table = model.__table__
    fks = sorted((fk.parent.name, fk.target_fullname) for fk in table.foreign_keys)
    assert fks == [(col, target) for col, target, _ in FOREIGN_KEYS[table.name]]
    names = {col: name for col, _, name in FOREIGN_KEYS[table.name]}
    for fk in table.foreign_keys:
        assert fk.ondelete is None and fk.onupdate is None, fk.parent.name
        assert type(fk.column.type) is type(fk.parent.type), fk.parent.name
        expected = names[fk.parent.name]
        # Nom explicite uniquement là où le nom automatique serait trop long ;
        # jamais de dépendance à une troncature implicite de PostgreSQL.
        assert fk.constraint.name == (expected if expected in EXPLICIT_FK_NAMES else None), fk.parent.name
        automatic = f"{table.name}_{fk.parent.name}_fkey"
        if expected not in EXPLICIT_FK_NAMES:
            assert automatic == expected and len(automatic) < 63, automatic
        else:
            assert len(automatic) >= 63 and len(expected) < 63, automatic


@pytest.mark.parametrize("model", T5A_MODELS, ids=lambda m: m.__name__)
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
        assert len(name) < 63, name


def test_every_child_foreign_key_is_indexed_without_redundancy():
    """PostgreSQL n'indexe pas le côté enfant d'une FK : chaque colonne FK
    est la PREMIÈRE colonne d'un index (PK composite comprise) ; aucun index
    séparé ne double la première colonne d'une PK composite ; l'index
    unique partiel (runs active seulement) ne remplace pas celui de
    user_id."""
    for model in T5A_MODELS:
        table = model.__table__
        leading = {}
        pk = [c.name for c in table.primary_key.columns]
        leading.setdefault(pk[0], []).append("pk")
        for index in table.indexes:
            if index.dialect_options["postgresql"]["where"] is None:
                leading.setdefault(index.columns[0].name, []).append(index.name)
        for fk in table.foreign_keys:
            assert fk.parent.name in leading, (table.name, fk.parent.name)
        for column, providers in leading.items():
            assert len(providers) == 1, (table.name, column, providers)


def test_vocabularies_and_the_absence_of_independent():
    """dependency_type : dependent / partially_dependent, JAMAIS
    independent (l'absence d'arête ne prouve pas l'indépendance) ; scope_mode
    identique sur les trois tables d'arêtes ; trigger reste ouvert (pas de
    CHECK)."""
    assert CHECKS[DEPS][DEPENDENCY_TYPE_CHECK] == "dependency_type IN ('dependent', 'partially_dependent')"
    migration = (REPO_ROOT / "alembic" / "versions" / f"{T5A}.py").read_text(encoding="utf-8")
    assert "'independent'" not in migration
    for model in T5A_MODELS:
        for constraint in model.__table__.constraints:
            if isinstance(constraint, sa.CheckConstraint):
                assert "'independent'" not in str(constraint.sqltext), constraint.name
    for table in EDGE_TABLES:
        assert CHECKS[table][f"ck_{table}_scope_mode"] == _in_sql("scope_mode", SCOPE_MODES)
    run_checks = " ".join(CHECKS[ASSESSMENTS].values())
    assert "trigger" not in run_checks
    for column in ("dependency_version", "transfer_version", "revalidation_version", "relation_schema_version",
                   "input_fingerprint", "scope_fingerprint"):
        assert column not in " ".join(sql for checks in CHECKS.values() for sql in checks.values()), column


def test_no_server_defaults_only_application_defaults():
    """Aucun server_default ; id (hors PK composites) et horodatages NOT
    NULL générés par l'application (UTC aware) ; aucune autre valeur
    inventée (ni statut, ni basis, ni fingerprint, ni completed_at)."""
    def call(default):
        assert default is not None and default.is_callable
        return default.arg(None)

    for model in T5A_MODELS:
        table = model.__table__
        for col in table.columns:
            assert col.server_default is None and col.server_onupdate is None, (table.name, col.name)
            assert col.onupdate is None, (table.name, col.name)
            if col.name in ("created_at", "started_at"):
                assert call(col.default).utcoffset().total_seconds() == 0
            elif col.name == "id":
                assert isinstance(call(col.default), uuid.UUID)
                assert call(col.default) != call(col.default)
            else:
                assert col.default is None, (table.name, col.name)


def test_jsonb_and_timestamptz_columns():
    jsonb, tz = set(), set()
    for model in T5A_MODELS:
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
    for model in T5A_MODELS:
        assert not sa.inspect(model).relationships, model.__name__


def _models_tree():
    return ast.parse((REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8"))


def test_models_module_contains_no_service_logic():
    """models.py ne déclare que des modèles : aucune fonction hors du helper
    d'horodatage, aucune méthode sur les modèles T5-A."""
    tree = _models_tree()
    assert {n.name for n in tree.body if isinstance(n, ast.FunctionDef)} == {"_utcnow_aware"}
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    for model in T5A_MODELS:
        body = classes[model.__name__].body
        assert not [n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))], model.__name__


def test_model_docstrings_state_the_doctrine():
    """Les docstrings disent ce qu'est (et n'est pas) chaque structure."""
    docs = {model.__name__: " ".join((model.__doc__ or "").split()) for model in T5A_MODELS}
    for name, doc in docs.items():
        assert "T5-A" in doc and "0008_longitudinal_relations" in doc, name
    assert "version précise du dossier" in docs["LongitudinalAssessmentRun"].lower()
    assert "snapshot explicite" in docs["LongitudinalAssessmentInput"].lower()
    assert "JAMAIS une preuve supplémentaire" in docs["ObservationDependency"]
    assert "« independent » n'existe volontairement pas" in docs["ObservationDependency"]
    assert "DIRECTIONNEL" in docs["ObservationTransfer"] and "NON TRANSITIF" in docs["ObservationTransfer"]
    assert "HISTORIQUE" in docs["ObservationRevalidation"] and "T6" in docs["ObservationRevalidation"]
    for name in ("DependencyCapability", "TransferCapability", "RevalidationCapability"):
        assert docs[name].startswith("Périmètre sémantique"), name
    migration = (REPO_ROOT / "alembic" / "versions" / f"{T5A}.py").read_text(encoding="utf-8")
    assert "Invariants volontairement reportés à T5-B" in migration


# --- anti-dérive -------------------------------------------------------------

FORBIDDEN_COLUMN_FRAGMENTS = (
    "score", "weight", "coefficient", "confidence", "stage", "master", "progress", "percent", "coverage",
    "variety", "independen", "consistency", "freshness", "durability", "tension", "required", "need",
    "level", "xp", "rank", "priority", "count", "polarity", "evidence", "predecessor", "reassess",
    "re_evaluat", "lineage", "parent", "model", "prompt", "evaluator", "output_fingerprint", "event_id",
    "active_history", "closure", "transitive", "depth",
)


def test_no_score_stage_confidence_or_profile_in_t5a():
    """Une relation T5 ne vaut jamais +1 / 0.5 preuve, ni poids : aucune
    colonne de score, stade, confiance, maîtrise, progression, profil
    (coverage, variety, independence, consistency, freshness, durability),
    besoin de revalidation / confirmation, prédécesseur, LLM ni event_id
    dupliqué ; aucune table de profil, d'historique actif ni de closure.
    Les stades, tensions, profils de confiance et le cache d'état
    appartiennent au Niveau 6 (six tables T6-A, dérivées, testées dans
    tests/test_migration_0009_competency_inference_state.py), jamais à T5."""
    for model in T5A_MODELS:
        for col in model.__table__.columns:
            assert not any(word in col.name for word in FORBIDDEN_COLUMN_FRAGMENTS), (model.__name__, col.name)
    for table in set(Base.metadata.tables) - T6A_TABLES:
        for fragment in ("profile", "score", "active_history", "closure", "transitive", "stage", "confidence",
                         "mastery", "progress", "independen", "longitudinal_state", "user_competenc"):
            assert fragment not in table, table
    for name in ("confidence_score", "coverage_score", "independence_score", "freshness_score", "durability_score",
                 "variety_score", "progress_percent", "mastery_score", "dependency_weight", "transfer_weight",
                 "current_stage", "previous_stage", "new_stage", "revalidation_required",
                 "confirmation_required", "validation_need", "tension_state"):
        for table in T5A_TABLES:
            assert name not in Base.metadata.tables[table].c, (table, name)


T5B_OPERATIONS = ("build_longitudinal_history", "assess_dependency", "detect_transfer", "detect_revalidation",
                  "activate_longitudinal_run")


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


def test_no_t5_service_or_inference_anywhere():
    """T5-A = persistence only. Depuis T5-B, UN seul module de service
    longitudinal, core/longitudinal_service.py (non branché, voir
    tests/test_longitudinal_service.py) ; aucun autre, ni route, ni script.
    Aucune opération d'inférence / détection (historique, dépendance,
    transfert, revalidation, activation hors service) nulle part, hors
    docstrings et commentaires."""
    modules = [p.relative_to(REPO_ROOT).as_posix()
               for p in [*(REPO_ROOT / "core").rglob("*.py"), *(REPO_ROOT / "scripts").rglob("*.py"),
                         REPO_ROOT / "api.py"]]
    # T5-C : core/longitudinal_view.py, lecteur read-only (non branché,
    # voir tests/test_longitudinal_view.py), n'est pas un service.
    assert sorted(m for m in modules if any(w in m for w in ("longitudinal", "relation", "dependenc", "transfer",
                                                              "revalidation"))) == ["core/longitudinal_service.py",
                                                                                    "core/longitudinal_view.py"]
    checked = 0
    for rel, source in _application_sources():
        checked += 1
        for name in T5B_OPERATIONS:
            assert name not in source, (rel, name)
    assert checked > 0


def test_t5a_tables_are_not_wired_to_the_application():
    """Aucun branchement : hors core/models.py, la migration 0008 et, depuis
    T5-B, le service core/longitudinal_service.py (seul point d'écriture
    applicatif, lui-même non branché : voir
    tests/test_longitudinal_service.py), aucun code applicatif (api.py,
    core/ dont les services T2-B / T3-B / T4-B / T4-C, scripts/, frontend)
    ne mentionne ces modèles ou ces tables."""
    allowed = {"core/models.py", f"alembic/versions/{T5A}.py", "core/longitudinal_service.py",
               # T5-C : reconstruction READ-ONLY du dossier (SELECT
               # uniquement, non branchée : tests/test_longitudinal_view.py).
               "core/longitudinal_view.py",
               # T6-A : FK de provenance des basis refs et du run
               # d'inférence vers T5 (aucun branchement applicatif).
               "alembic/versions/0009_competency_inference_state.py",
               # T6-B : service d'inférence, qui LIT le dossier T5 parent
               # (FOR SHARE), ses inputs et ses relations (jamais d'écriture
               # T5 ; non branché : tests/test_inference_service.py).
               "core/inference_service.py"}
    needles = (*(m.__name__ for m in T5A_MODELS), *T5A_TABLES)
    checked = 0
    for rel, source in _application_sources():
        checked += 1
        if rel not in allowed:
            for needle in needles:
                assert needle not in source, (rel, needle)
    assert checked > 0


def test_no_route_for_t5():
    """api.py inchangé : aucun endpoint longitudinal / history /
    dependencies / transfers / revalidations."""
    source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    tokens = _code_tokens(source).lower()
    for word in ("longitudinal", "revalidation", "dependenc", "transfer"):
        assert word not in tokens, word
    routes = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.args and isinstance(decorator.args[0], ast.Constant)):
                    routes.append(decorator.args[0].value)
    assert routes
    for route in routes:
        for segment in ("/longitudinal", "/history", "/dependencies", "/transfers", "/revalidations"):
            assert segment not in route, route


def test_no_llm_or_runtime_schema_mutation_in_t5a():
    """Ni la migration ni les modèles T5-A n'appellent un LLM ; aucun
    create_all ni mutation de schéma hors Alembic ; aucun champ modèle /
    prompt / évaluateur dans le run longitudinal."""
    migration = REPO_ROOT / "alembic" / "versions" / f"{T5A}.py"
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
    source = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    t5_sources = [ast.get_source_segment(source, node) for node in _models_tree().body
                  if isinstance(node, ast.ClassDef) and node.name in {m.__name__ for m in T5A_MODELS}]
    assert len(t5_sources) == 8
    for label, code in ((migration.name, migration.read_text(encoding="utf-8")), *(("models", s) for s in t5_sources)):
        tokens = _code_tokens(code).lower()
        for word in ("anthropic", "openai", "claude", "llm", "prompt", "evaluator", "classifier", "model_id",
                     "create_all", "requests", "httpx", "bootstrap", "execute", "insert"):
            assert word not in tokens, (label, word)


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

INSERT_ASSESSMENT = sa.text(
    f"INSERT INTO {ASSESSMENTS} ({', '.join(COLUMNS[ASSESSMENTS])}) "
    f"VALUES ({', '.join(':' + c for c in COLUMNS[ASSESSMENTS])})"
)


def _insert(table):
    jsonb = {name for (t, name) in JSONB_COLUMNS if t == table}
    return sa.text(
        f"INSERT INTO {table} ({', '.join(COLUMNS[table])}) VALUES ("
        + ", ".join(f"CAST(:{c} AS JSONB)" if c in jsonb else f":{c}" for c in COLUMNS[table]) + ")"
    )


INSERTS = {table: _insert(table) for table in TABLES_IN_ORDER}


def _assessment(conn, release, /, **overrides) -> uuid.UUID:
    params = {
        "id": uuid.uuid4(),
        "user_id": USER,
        "competency_code": "C7",
        "execution_status": "completed",
        "interpretation_status": "candidate",
        "trigger": "observation_completed",
        "pedagogical_taxonomy_release_id": release,
        "dependency_version": "dependency-1",
        "transfer_version": "transfer-1",
        "revalidation_version": "revalidation-1",
        "relation_schema_version": "relations-1",
        "input_fingerprint": "sha256:input",
        "assessment_dedup_key": f"dedup-{uuid.uuid4()}",
        "started_at": NOW,
        "completed_at": NOW,
        "created_at": NOW,
        "failure_code": None,
    }
    params.update(overrides)
    conn.execute(INSERTS[ASSESSMENTS], params)
    return params["id"]


def _input(conn, run_id, observation_id) -> None:
    conn.execute(INSERTS[INPUTS], {"run_id": run_id, "observation_id": observation_id})


def _dependency(conn, run_id, target, /, *, source_observation=None, source_trace=None, **overrides) -> uuid.UUID:
    params = {
        "id": uuid.uuid4(),
        "run_id": run_id,
        "target_observation_id": target,
        "source_kind": "observation" if source_trace is None else "support_trace",
        "source_observation_id": source_observation,
        "source_support_trace_id": source_trace,
        "dependency_type": "dependent",
        "scope_mode": "whole_observation",
        "dependency_basis": '{"reason": "reprise de la formule montrée à l\'épisode précédent"}',
        "scope_fingerprint": "sha256:scope",
        "created_at": NOW,
    }
    params.update(overrides)
    conn.execute(INSERTS[DEPS], params)
    return params["id"]


def _transfer(conn, run_id, source, target, /, **overrides) -> uuid.UUID:
    params = {
        "id": uuid.uuid4(),
        "run_id": run_id,
        "source_observation_id": source,
        "target_observation_id": target,
        "scope_mode": "whole_observation",
        "transfer_basis": '{"variation": "autre structure de capital, raisonnement adapté"}',
        "scope_fingerprint": "sha256:scope",
        "created_at": NOW,
    }
    params.update(overrides)
    conn.execute(INSERTS[TRANSFERS], params)
    return params["id"]


def _revalidation(conn, run_id, source, target, /, **overrides) -> uuid.UUID:
    params = {
        "id": uuid.uuid4(),
        "run_id": run_id,
        "source_contradiction_observation_id": source,
        "target_supportive_observation_id": target,
        "scope_mode": "whole_observation",
        "revalidation_basis": '{"mechanism": "effet de levier sur le ROE"}',
        "scope_fingerprint": "sha256:scope",
        "created_at": NOW,
    }
    params.update(overrides)
    conn.execute(INSERTS[REVALS], params)
    return params["id"]


def _edge(conn, table, run_id, source, target, /, **overrides) -> uuid.UUID:
    if table == DEPS:
        return _dependency(conn, run_id, target, source_observation=source, **overrides)
    if table == TRANSFERS:
        return _transfer(conn, run_id, source, target, **overrides)
    return _revalidation(conn, run_id, source, target, **overrides)


def _scope(conn, table, relation_id, membership_id) -> None:
    relation_column, _ = SCOPE_TABLES[table]
    conn.execute(INSERTS[table], {relation_column: relation_id, "capability_membership_id": membership_id})


def _contradictory(conn, competency="C7", **overrides) -> uuid.UUID:
    return _observation(conn, competency, polarity="contradictory", local_stage=None,
                        contradiction_scope="application", error_type="conceptual", **overrides)


def _refused(conn, match, action, *, drop=()):
    """`action` doit être refusée par PostgreSQL avec une erreur contenant
    `match`. `drop` : (table, contrainte) retirées le temps du test (dans un
    savepoint annulé) pour vérifier une contrainte seule."""
    with pytest.raises(sa.exc.IntegrityError, match=match):
        with conn.begin_nested():
            for table, name in drop:
                conn.execute(sa.text(f"ALTER TABLE {table} DROP CONSTRAINT {name}"))
            action()


@pytest.fixture
def conn(pg_url, pg_engine):
    """Schéma = head (0009 depuis T6-A) + deux utilisateurs et une
    analysis_session ; tout ce que fait le test est annulé."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()


@pytest.fixture
def release(conn) -> uuid.UUID:
    return _release(conn)


@pytest.fixture
def run(conn, release) -> uuid.UUID:
    return _assessment(conn, release)


# --- upgrade et structure --------------------------------------------------------

def _assert_metadata_matches_0008(engine) -> None:
    """Au schéma 0008, Base.metadata ne diffère que par les six tables de
    T6-A et leurs index, créés seulement en 0009 ; tout le reste correspond
    exactement, hormis les écarts R1-B (0010)."""
    diff = _without_r1b_changes(
        _without_r1c1_changes(_without_r1c2_changes(_without_r1d1_changes(_compare_metadata(engine)))),
                                events_created=True)
    assert sorted((d[0], d[1].name) for d in diff) == sorted(
        [("add_table", t) for t in T6A_TABLES] + [("add_index", i) for i in T6A_INDEXES]
    )


def test_pg_upgrade_from_empty_database_to_0008(pg_url, pg_engine):
    """Base vide -> 0008 ; Base.metadata == schéma migré (hors T6-A) ; aucun
    ENUM, trigger ni fonction ; les huit tables T5 sont VIDES (aucun seed)
    et les tables T4 aussi (aucun bootstrap de taxonomie). La tête est 0009
    depuis T6-A (testée à part)."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T5A)
    assert _version(pg_engine) == T5A
    assert _tables(pg_engine) == (REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES
                                  | {"alembic_version"})
    _assert_metadata_matches_0008(pg_engine)
    catalog = _global_catalog(pg_engine)
    assert (catalog["enums"], catalog["triggers"], catalog["functions"]) == (0, 0, 0)
    # Aucune séquence T5 (UUID applicatifs) : seules celles des tables historiques.
    assert not [s for s in catalog["sequences"] if any(t in s for t in T5A_TABLES)]
    assert _data(pg_engine, T5A_TABLES | T4A_TABLES) == {name: [] for name in sorted(T5A_TABLES | T4A_TABLES)}


def _pg_columns(table) -> list:
    types = {key: "uuid" for key in UUID_COLUMNS}
    types.update({key: "timestamp with time zone" for key in TIMESTAMPTZ_COLUMNS})
    types.update({key: "jsonb" for key in JSONB_COLUMNS})
    return [(n, types.get((table, n), "character varying"), "YES" if (table, n) in NULLABLE else "NO", None)
            for n in COLUMNS[table]]


PG_CHECKS = {
    ASSESSMENTS: {
        "ck_longitudinal_assessment_runs_competency_code": _pg_in_check("competency_code", COMPETENCY_CODES),
        "ck_longitudinal_assessment_runs_execution_status": _pg_in_check("execution_status", EXECUTION_STATUSES),
        "ck_longitudinal_assessment_runs_interpretation_status":
            _pg_in_check("interpretation_status", INTERPRETATION_STATUSES),
    },
    DEPS: {
        SOURCE_KIND_CHECK: _pg_in_check("source_kind", SOURCE_KINDS),
        SOURCE_XOR_CHECK:
            "CHECK (((((source_kind)::text = 'observation'::text) AND (source_observation_id IS NOT NULL) "
            "AND (source_support_trace_id IS NULL)) OR (((source_kind)::text = 'support_trace'::text) "
            "AND (source_observation_id IS NULL) AND (source_support_trace_id IS NOT NULL))))",
        NO_SELF_CHECK:
            "CHECK (((source_observation_id IS NULL) OR (source_observation_id <> target_observation_id)))",
        DEPENDENCY_TYPE_CHECK: _pg_in_check("dependency_type", DEPENDENCY_TYPES),
        "ck_observation_dependencies_scope_mode": _pg_in_check("scope_mode", SCOPE_MODES),
    },
    TRANSFERS: {
        TRANSFER_DISTINCT_CHECK: "CHECK ((source_observation_id <> target_observation_id))",
        "ck_observation_transfers_scope_mode": _pg_in_check("scope_mode", SCOPE_MODES),
    },
    REVALS: {
        REVAL_DISTINCT_CHECK:
            "CHECK ((source_contradiction_observation_id <> target_supportive_observation_id))",
        "ck_observation_revalidations_scope_mode": _pg_in_check("scope_mode", SCOPE_MODES),
    },
}


def _expected_constraints(table) -> list:
    pk_columns = ", ".join(PRIMARY_KEYS[table])
    rows = [(f"{table}_pkey", "p", f"PRIMARY KEY ({pk_columns})", " ", " ")]
    rows += [(name, "c", sql, " ", " ") for name, sql in PG_CHECKS.get(table, {}).items()]
    rows += [(name, "u", f"UNIQUE ({', '.join(cols)})", " ", " ") for name, cols in UNIQUES[table].items()]
    for column, target, name in FOREIGN_KEYS[table]:
        target_table = target.split(".")[0]
        # 'a' = NO ACTION à la suppression comme à la mise à jour.
        rows.append((name, "f", f"FOREIGN KEY ({column}) REFERENCES {target_table}(id)", "a", "a"))
    return sorted(rows)


def _expected_indexdefs(table) -> dict:
    pk_columns = ", ".join(PRIMARY_KEYS[table])
    defs = {f"{table}_pkey": f"CREATE UNIQUE INDEX {table}_pkey ON public.{table} USING btree ({pk_columns})"}
    for name, cols in UNIQUES[table].items():
        defs[name] = f"CREATE UNIQUE INDEX {name} ON public.{table} USING btree ({', '.join(cols)})"
    for name, (cols, unique, where) in INDEXES[table].items():
        prefix = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
        defs[name] = f"{prefix} {name} ON public.{table} USING btree ({', '.join(cols)})"
        if where:
            defs[name] += " WHERE ((interpretation_status)::text = 'active'::text)"
    return defs


def test_pg_exact_structure_of_the_eight_tables(pg_url, pg_engine):
    """Colonnes, types, nullabilité, PK, FK (NO ACTION, noms réels), CHECK,
    UNIQUE et index exacts, lus dans le catalogue PostgreSQL."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T5A)
    for table in TABLES_IN_ORDER:
        assert _catalog_columns(pg_engine, table) == _pg_columns(table), table
        assert _constraints(pg_engine, table) == _expected_constraints(table), table
        assert _indexdefs(pg_engine, table) == _expected_indexdefs(table), table
    # Spot-check explicite de l'index unique partiel et d'une PK composite.
    assert _indexdefs(pg_engine, ASSESSMENTS)[ONE_ACTIVE_INDEX] == (
        f"CREATE UNIQUE INDEX {ONE_ACTIVE_INDEX} ON public.{ASSESSMENTS} USING btree (user_id, competency_code) "
        "WHERE ((interpretation_status)::text = 'active'::text)"
    )
    assert _indexdefs(pg_engine, INPUTS)[f"{INPUTS}_pkey"] == (
        f"CREATE UNIQUE INDEX {INPUTS}_pkey ON public.{INPUTS} USING btree (run_id, observation_id)"
    )
    with pg_engine.connect() as conn:
        # Aucune FK de la base n'est en cascade / set null / set default.
        assert conn.execute(sa.text(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
            "AND (confdeltype <> 'a' OR confupdtype <> 'a')"
        )).scalar_one() == 0


@pytest.mark.parametrize("table", EDGE_TABLES + (ASSESSMENTS, INPUTS) + tuple(SCOPE_TABLES))
def test_pg_required_columns_are_not_null(conn, release, table):
    """Toute colonne hors completed_at, failure_code et des deux sources de
    dépendance est NOT NULL, dont les colonnes *_basis (aucun JSON null ni
    défaut inventé) et scope_fingerprint."""
    run_id = _assessment(conn, release)
    a, b = _observation(conn), _observation(conn)
    membership = _membership(conn, release, _definition(conn, "C7_A"))
    edges = {DEPS: _dependency(conn, run_id, b, source_observation=a), TRANSFERS: _transfer(conn, run_id, a, b),
             REVALS: _revalidation(conn, run_id, a, b)}

    def insert(column):
        if table == ASSESSMENTS:
            return lambda: _assessment(conn, release, **{column: None})
        if table == INPUTS:
            return lambda: conn.execute(INSERTS[INPUTS], {"run_id": run_id, "observation_id": a, column: None})
        if table in SCOPE_TABLES:
            relation_column, relation_table = SCOPE_TABLES[table]
            params = {relation_column: edges[relation_table], "capability_membership_id": membership, column: None}
            return lambda: conn.execute(INSERTS[table], params)
        return lambda: _edge(conn, table, run_id, a, b, **{column: None})

    for column in COLUMNS[table]:
        if (table, column) in NULLABLE:
            continue
        _refused(conn, f'null value in column "{column}"', insert(column))


# --- runs longitudinaux ------------------------------------------------------------

def test_pg_run_competency_code_vocabulary(conn, release):
    """C1..C12 acceptés ; toute autre valeur refusée."""
    for competency in COMPETENCY_CODES:
        _assessment(conn, release, competency_code=competency)
    for value in ("", "C0", "C13", "c7", "C07", "C7 ", "7", "C7_A", "competence"):
        _refused(conn, "ck_longitudinal_assessment_runs_competency_code",
                 lambda: _assessment(conn, release, competency_code=value))
    assert _count(conn, ASSESSMENTS) == 12


@pytest.mark.parametrize("column, values, refused", [
    ("execution_status", EXECUTION_STATUSES, ("", "Running", "pending", "done", "cancelled", "completed ")),
    ("interpretation_status", INTERPRETATION_STATUSES, ("", "Active", "draft", "retired", "invalidated", "stale")),
])
def test_pg_run_status_vocabularies(conn, release, column, values, refused):
    for value in values:
        _assessment(conn, release, **{column: value})
    for value in refused:
        _refused(conn, f"ck_longitudinal_assessment_runs_{column}",
                 lambda: _assessment(conn, release, **{column: value}))
    assert _count(conn, ASSESSMENTS) == len(values)


def test_pg_run_trigger_is_an_open_vocabulary_and_versions_are_free_strings(conn, release):
    for trigger in ("observation_completed", "manual_replay", "taxonomy_release_changed", "un_futur_declencheur"):
        _assessment(conn, release, trigger=trigger, dependency_version=f"d-{trigger}",
                    relation_schema_version="schema-2026-09")
    assert _count(conn, ASSESSMENTS) == 4


def test_pg_run_lifecycle_fields_are_not_cross_checked(conn, release):
    """Comme T3 : aucun CHECK croisé status <-> completed_at / failure_code
    (lifecycle applicatif, T5-B)."""
    _assessment(conn, release, execution_status="running", completed_at=None)
    _assessment(conn, release, execution_status="running", completed_at=NOW)
    _assessment(conn, release, execution_status="completed", completed_at=None)
    _assessment(conn, release, execution_status="failed", failure_code="timeout", interpretation_status="obsolete")
    assert _count(conn, ASSESSMENTS) == 4


def test_pg_run_foreign_keys(conn, release):
    _refused(conn, "longitudinal_assessment_runs_user_id_fkey",
             lambda: _assessment(conn, release, user_id="inconnu"))
    _refused(conn, RUN_RELEASE_FK, lambda: _assessment(conn, uuid.uuid4()))
    _assessment(conn, release, user_id=OTHER_USER)


def test_pg_assessment_dedup_key_is_globally_unique(conn, release):
    """Unique globalement : même clé refusée y compris pour un autre user,
    une autre compétence ou une autre release."""
    _assessment(conn, release, assessment_dedup_key="C7:u:v1")
    other_release = _release(conn)
    for release_id, overrides in ((release, {}), (release, {"user_id": OTHER_USER}),
                                  (release, {"competency_code": "C8"}), (other_release, {}),
                                  (release, {"interpretation_status": "obsolete"})):
        _refused(conn, DEDUP_UNIQUE,
                 lambda: _assessment(conn, release_id, assessment_dedup_key="C7:u:v1", **overrides))
    _assessment(conn, release, assessment_dedup_key="C7:u:v2")
    assert _count(conn, ASSESSMENTS) == 2


def test_pg_at_most_one_active_run_per_user_and_competency(conn, release):
    """U1/C7 active x2 refusé ; U1/C7 + U1/C8 et U1/C7 + U2/C7 autorisés ;
    candidate, superseded et obsolete multiples autorisés (clés de dédup
    distinctes). Également garanti à l'UPDATE."""
    _assessment(conn, release, user_id=USER, competency_code="C7", interpretation_status="active")
    _refused(conn, ONE_ACTIVE_INDEX,
             lambda: _assessment(conn, release, user_id=USER, competency_code="C7", interpretation_status="active"))
    _assessment(conn, release, user_id=USER, competency_code="C8", interpretation_status="active")
    _assessment(conn, release, user_id=OTHER_USER, competency_code="C7", interpretation_status="active")
    for status in ("candidate", "candidate", "superseded", "superseded", "obsolete", "obsolete"):
        _assessment(conn, release, user_id=USER, competency_code="C7", interpretation_status=status)
    candidate = _assessment(conn, release, user_id=USER, competency_code="C7")
    promote = sa.text(f"UPDATE {ASSESSMENTS} SET interpretation_status = 'active' WHERE id = :id")
    _refused(conn, ONE_ACTIVE_INDEX, lambda: conn.execute(promote, {"id": candidate}))
    # Bascule dans une même transaction : l'ancien active est supersédé
    # d'abord (le schéma ne gère pas les transitions : T5-B).
    conn.execute(sa.text(
        f"UPDATE {ASSESSMENTS} SET interpretation_status = 'superseded' "
        "WHERE user_id = :u AND competency_code = 'C7' AND interpretation_status = 'active'"
    ), {"u": USER})
    conn.execute(promote, {"id": candidate})
    assert conn.execute(sa.text(
        f"SELECT id FROM {ASSESSMENTS} WHERE user_id = :u AND competency_code = 'C7' "
        "AND interpretation_status = 'active'"
    ), {"u": USER}).scalars().all() == [candidate]
    assert _count(conn, ASSESSMENTS, "interpretation_status = 'active'") == 3


def test_pg_concurrent_active_runs_for_same_user_competency_are_serialized(pg_url, pg_engine):
    """L'invariant tient sous concurrence : deux transactions insèrent
    chacune un run active pour le même (user_id, competency_code) ; la
    seconde attend la première (index unique, pg_blocking_pids) puis échoue
    quand celle-ci commite."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with pg_engine.begin() as setup:
        release_id = _release(setup)
    try:
        with pg_engine.connect() as holder, pg_engine.connect() as waiter:
            holder_pid, waiter_pid = _backend_pid(holder), _backend_pid(waiter)
            holder.commit()
            waiter.commit()
            holder.begin()
            _assessment(holder, release_id, interpretation_status="active", assessment_dedup_key="first")
            outcome = {}

            def work():
                try:
                    with waiter.begin():
                        _assessment(waiter, release_id, interpretation_status="active",
                                    assessment_dedup_key="second")
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
                f"SELECT assessment_dedup_key FROM {ASSESSMENTS}"
            )).scalars().all() == ["first"]
    finally:
        with pg_engine.begin() as cleanup:
            cleanup.execute(sa.text(f"DELETE FROM {ASSESSMENTS}"))
            cleanup.execute(sa.text(f"DELETE FROM {RELEASES}"))


# --- snapshot d'entrée ------------------------------------------------------------------

def test_pg_input_snapshot(conn, release):
    """Même observation une seule fois par run, mais dans plusieurs runs ;
    FK run et observation ; un run peut n'avoir AUCUNE entrée (dossier vide
    représentable, distinct de « aucun run »)."""
    first, second = _assessment(conn, release), _assessment(conn, release)
    empty = _assessment(conn, release)
    observation, other = _observation(conn), _observation(conn)
    _input(conn, first, observation)
    _input(conn, first, other)
    _refused(conn, f"{INPUTS}_pkey", lambda: _input(conn, first, observation))
    _input(conn, second, observation)
    _refused(conn, "longitudinal_assessment_inputs_observation_id_fkey", lambda: _input(conn, first, uuid.uuid4()))
    _refused(conn, "longitudinal_assessment_inputs_run_id_fkey", lambda: _input(conn, uuid.uuid4(), observation))
    assert _count(conn, INPUTS, "run_id = :r", {"r": first}) == 2
    assert _count(conn, INPUTS, "observation_id = :o", {"o": observation}) == 2
    assert _count(conn, INPUTS, "run_id = :r", {"r": empty}) == 0
    # Toujours deux preuves : le snapshot ne crée aucune observation.
    assert _count(conn, OBS) == 2


def test_pg_input_snapshot_deletions_are_refused(conn, release):
    run_id, observation = _assessment(conn, release), _observation(conn)
    _input(conn, run_id, observation)
    _refused(conn, "longitudinal_assessment_inputs_observation_id_fkey",
             lambda: conn.execute(sa.text(f"DELETE FROM {OBS} WHERE id = :id"), {"id": observation}))
    _refused(conn, "longitudinal_assessment_inputs_run_id_fkey",
             lambda: conn.execute(sa.text(f"DELETE FROM {ASSESSMENTS} WHERE id = :id"), {"id": run_id}))
    assert (_count(conn, INPUTS), _count(conn, ASSESSMENTS), _count(conn, OBS)) == (1, 1, 1)


# --- dépendances ------------------------------------------------------------------------

@pytest.mark.parametrize("kind, with_observation, with_trace, accepted", [
    ("observation", True, False, True),
    ("support_trace", False, True, True),
    ("observation", True, True, False),
    ("observation", False, False, False),
    ("observation", False, True, False),
    ("support_trace", True, False, False),
    ("support_trace", True, True, False),
    ("support_trace", False, False, False),
])
def test_pg_dependency_source_xor(conn, run, kind, with_observation, with_trace, accepted):
    """Exactement une source, cohérente avec source_kind."""
    target = _observation(conn)
    event_id = _event(conn)
    trace = _trace(conn, event_id, 1)
    source = _observation(conn)

    def insert():
        return _dependency(conn, run, target, source_kind=kind,
                           source_observation=source if with_observation else None,
                           source_trace=trace if with_trace else None)
    if accepted:
        insert()
        assert _count(conn, DEPS) == 1
    else:
        _refused(conn, SOURCE_XOR_CHECK, insert)
        assert _count(conn, DEPS) == 0


@pytest.mark.parametrize("kind", ["", "Observation", "support", "trace", "observation ", "both", "none"])
def test_pg_dependency_unknown_source_kind_is_refused(conn, run, kind):
    """Refusé par le vocabulaire ET par le XOR, chacun vérifié seul."""
    target, source = _observation(conn), _observation(conn)

    def insert():
        return _dependency(conn, run, target, source_observation=source, source_kind=kind)
    _refused(conn, SOURCE_KIND_CHECK, insert, drop=((DEPS, SOURCE_XOR_CHECK),))
    _refused(conn, SOURCE_XOR_CHECK, insert, drop=((DEPS, SOURCE_KIND_CHECK),))


def test_pg_dependency_type_vocabulary_has_no_independent(conn, run):
    target, source = _observation(conn), _observation(conn)
    for dependency_type in DEPENDENCY_TYPES:
        _dependency(conn, run, target, source_observation=source, dependency_type=dependency_type)
    for value in ("independent", "Independent", "not_dependent", "none", "", "partial", "Dependent"):
        _refused(conn, DEPENDENCY_TYPE_CHECK,
                 lambda: _dependency(conn, run, target, source_observation=source, dependency_type=value))
    assert _count(conn, DEPS) == 2


def test_pg_self_dependency_is_refused(conn, run):
    """Une observation ne peut pas dépendre d'elle-même (INSERT comme
    UPDATE) ; une source support_trace n'est pas concernée."""
    observation, other = _observation(conn), _observation(conn)
    _refused(conn, NO_SELF_CHECK, lambda: _dependency(conn, run, observation, source_observation=observation))
    edge = _dependency(conn, run, observation, source_observation=other)
    _refused(conn, NO_SELF_CHECK, lambda: conn.execute(sa.text(
        f"UPDATE {DEPS} SET source_observation_id = target_observation_id WHERE id = :id"), {"id": edge}))
    _dependency(conn, run, observation, source_trace=_trace(conn, _event(conn), 1))
    assert _count(conn, DEPS) == 2


def test_pg_dependency_foreign_keys(conn, run):
    target, source = _observation(conn), _observation(conn)
    _refused(conn, "observation_dependencies_run_id_fkey",
             lambda: _dependency(conn, uuid.uuid4(), target, source_observation=source))
    _refused(conn, "observation_dependencies_target_observation_id_fkey",
             lambda: _dependency(conn, run, uuid.uuid4(), source_observation=source))
    _refused(conn, "observation_dependencies_source_observation_id_fkey",
             lambda: _dependency(conn, run, target, source_observation=uuid.uuid4()))
    _refused(conn, "observation_dependencies_source_support_trace_id_fkey",
             lambda: _dependency(conn, run, target, source_trace=uuid.uuid4()))
    assert _count(conn, DEPS) == 0


# --- scope_mode, basis, fingerprint (trois tables d'arêtes) ---------------------------------

@pytest.mark.parametrize("table", EDGE_TABLES)
def test_pg_scope_mode_vocabulary(conn, run, table):
    """whole_observation / localized / competency_only acceptés, autres
    refusés. La cohérence avec les lignes *_capabilities est T5-B."""
    a, b = _observation(conn), _observation(conn)
    for mode in SCOPE_MODES:
        _edge(conn, table, run, a, b, scope_mode=mode)
    for value in ("", "whole", "Localized", "capability", "partial", "competency", "global", "localized "):
        _refused(conn, f"ck_{table}_scope_mode", lambda: _edge(conn, table, run, a, b, scope_mode=value))
    assert _count(conn, table) == 3


@pytest.mark.parametrize("table, basis_column", [(DEPS, "dependency_basis"), (TRANSFERS, "transfer_basis"),
                                                 (REVALS, "revalidation_basis")])
def test_pg_basis_is_free_jsonb_and_fingerprint_is_stored_as_is(conn, run, table, basis_column):
    """Aucun mini-schéma en base : tout JSON (objet, liste, vide) est conservé
    tel quel ; scope_fingerprint est stocké sans calcul ni normalisation."""
    a, b = _observation(conn), _observation(conn)
    payload = ('{"why": "reprend l\'exemple travaillé", "refs": [{"work_index": 2}], '
               '"notes": null, "nested": {"é": ["ü", 1, true]}}')
    edge = _edge(conn, table, run, a, b, **{basis_column: payload, "scope_fingerprint": "  Opaque Fingerprint  "})
    _edge(conn, table, run, a, b, **{basis_column: "[]"})
    _edge(conn, table, run, a, b, **{basis_column: "{}"})
    assert conn.execute(sa.text(f"SELECT {basis_column}, scope_fingerprint FROM {table} WHERE id = :id"),
                        {"id": edge}).one() == (
        {"why": "reprend l'exemple travaillé", "refs": [{"work_index": 2}], "notes": None,
         "nested": {"é": ["ü", 1, True]}},
        "  Opaque Fingerprint  ",
    )


# --- transferts et revalidations --------------------------------------------------------------

@pytest.mark.parametrize("table, check, source_fk, target_fk", [
    (TRANSFERS, TRANSFER_DISTINCT_CHECK, "observation_transfers_source_observation_id_fkey",
     "observation_transfers_target_observation_id_fkey"),
    (REVALS, REVAL_DISTINCT_CHECK, REVAL_SOURCE_FK, REVAL_TARGET_FK),
])
def test_pg_directional_edges(conn, run, table, check, source_fk, target_fk):
    """FK source et cible valides, source <> cible ; directionnel : A -> B
    et B -> A sont deux faits distincts ; plusieurs arêtes identiques ne
    sont pas dédupliquées par la base (T5-B)."""
    a, b = _observation(conn), _observation(conn)
    _refused(conn, check, lambda: _edge(conn, table, run, a, a))
    _refused(conn, source_fk, lambda: _edge(conn, table, run, uuid.uuid4(), b))
    _refused(conn, target_fk, lambda: _edge(conn, table, run, a, uuid.uuid4()))
    _refused(conn, f"{table}_run_id_fkey", lambda: _edge(conn, table, uuid.uuid4(), a, b))
    forward = _edge(conn, table, run, a, b)
    _edge(conn, table, run, b, a)
    source_column, target_column = EDGE_COLUMNS[table]
    _refused(conn, check, lambda: conn.execute(sa.text(
        f"UPDATE {table} SET {target_column} = {source_column} WHERE id = :id"), {"id": forward}))
    assert _count(conn, table) == 2


def test_pg_known_limit_transfer_semantics_are_left_to_t5b(conn, run):
    """LIMITE DOCUMENTÉE : la base n'impose pas encore la sémantique du
    transfert (lectures cross-table, T5-B) : source contradictory, cible
    non Application, même CognitiveEvent, simple changement de ticker,
    autre compétence ou autre utilisateur sont tous acceptés. Ce test fige
    ce comportement pour que tout changement soit délibéré."""
    contradictory, supportive = _contradictory(conn), _observation(conn, local_stage="discovery")
    _transfer(conn, run, contradictory, supportive)
    event_run = _run(conn, _event(conn))
    same_event_a, same_event_b = _obs(conn, event_run, 1), _obs(conn, event_run, 2)
    _transfer(conn, run, same_event_a, same_event_b)
    _transfer(conn, run, _observation(conn, "C7"), _observation(conn, "C8"))
    _transfer(conn, run, _observation(conn), _observation(conn),
              transfer_basis='{"variation": "même exercice, autre ticker"}')
    assert _count(conn, TRANSFERS) == 4


def test_pg_known_limit_revalidation_semantics_are_left_to_t5b(conn, run):
    """LIMITE DOCUMENTÉE : la base n'impose pas encore source contradictory
    / cible supportive, ni des événements distincts : T5-B."""
    supportive_a, supportive_b = _observation(conn), _observation(conn)
    _revalidation(conn, run, supportive_a, supportive_b)
    _revalidation(conn, run, _observation(conn), _contradictory(conn))
    event_run = _run(conn, _event(conn))
    contradiction = _obs(conn, event_run, 1, polarity="contradictory", local_stage=None,
                         contradiction_scope="application")
    _revalidation(conn, run, contradiction, _obs(conn, event_run, 2))
    assert _count(conn, REVALS) == 3


def test_pg_relations_create_no_evidence_and_are_not_transitive(conn, run):
    """A -> B et B -> C n'ont aucun effet de bord : ni A -> C généré, ni
    observation créée ou modifiée (aucun trigger, aucune closure)."""
    a, b, c = _observation(conn), _observation(conn), _observation(conn)
    observations_before = conn.execute(sa.text(f"SELECT * FROM {OBS} ORDER BY id")).all()
    for table in EDGE_TABLES:
        _edge(conn, table, run, a, b)
        _edge(conn, table, run, b, c)
        source_column, target_column = EDGE_COLUMNS[table]
        assert _count(conn, table) == 2, table
        assert _count(conn, table, f"{source_column} = :a AND {target_column} = :c", {"a": a, "c": c}) == 0, table
    assert conn.execute(sa.text(f"SELECT * FROM {OBS} ORDER BY id")).all() == observations_before


# --- périmètres capability -----------------------------------------------------------------------

@pytest.mark.parametrize("table", sorted(SCOPE_TABLES))
def test_pg_capability_scope_tables(conn, release, run, table):
    """Même paire refusée (PK) ; même membership sur deux arêtes autorisé ;
    FK arête et membership ; plusieurs lignes restent UNE arête et
    n'ajoutent aucune preuve."""
    relation_column, relation_table = SCOPE_TABLES[table]
    c7_a = _membership(conn, release, _definition(conn, "C7_A"))
    c7_b = _membership(conn, release, _definition(conn, "C7_B"))
    a, b = _observation(conn), _observation(conn)
    first, second = _edge(conn, relation_table, run, a, b), _edge(conn, relation_table, run, a, b)
    _scope(conn, table, first, c7_a)
    _scope(conn, table, first, c7_b)
    _refused(conn, f"{table}_pkey", lambda: _scope(conn, table, first, c7_a))
    _scope(conn, table, second, c7_a)
    _refused(conn, f"{table}_{relation_column}_fkey", lambda: _scope(conn, table, uuid.uuid4(), c7_a))
    _refused(conn, f"{table}_capability_membership_id_fkey", lambda: _scope(conn, table, first, uuid.uuid4()))
    assert _count(conn, table, f"{relation_column} = :r", {"r": first}) == 2
    assert _count(conn, table, "capability_membership_id = :m", {"m": c7_a}) == 2
    assert (_count(conn, relation_table), _count(conn, OBS)) == (2, 2)


@pytest.mark.parametrize("table", sorted(SCOPE_TABLES))
def test_pg_capability_scope_deletions_are_refused(conn, release, run, table):
    relation_column, relation_table = SCOPE_TABLES[table]
    membership = _membership(conn, release, _definition(conn, "C7_A"))
    edge = _edge(conn, relation_table, run, _observation(conn), _observation(conn))
    _scope(conn, table, edge, membership)
    _refused(conn, f"{table}_{relation_column}_fkey",
             lambda: conn.execute(sa.text(f"DELETE FROM {relation_table} WHERE id = :id"), {"id": edge}))
    _refused(conn, f"{table}_capability_membership_id_fkey",
             lambda: conn.execute(sa.text(f"DELETE FROM {MEMBERSHIPS} WHERE id = :id"), {"id": membership}))
    assert (_count(conn, table), _count(conn, relation_table), _count(conn, MEMBERSHIPS)) == (1, 1, 1)


def test_pg_known_limit_scope_coherence_is_left_to_t5b(conn, release, run):
    """LIMITE DOCUMENTÉE (T5-B) : la base accepte un scope localized sans
    ligne, un scope competency_only avec lignes, un membership d'une autre
    release que celle du run, une capacité d'une autre compétence ou hors
    du périmètre des observations reliées. Aucun trigger ne simule ces
    invariants cross-table ; observation_capabilities n'est pas touchée."""
    other_release = _release(conn)
    foreign = _membership(conn, other_release, _definition(conn, "C8_A"))
    local = _membership(conn, release, _definition(conn, "C7_A"))
    a, b = _observation(conn), _observation(conn)
    _map(conn, a, local)
    mappings_before = conn.execute(sa.text("SELECT * FROM observation_capabilities")).all()
    _edge(conn, DEPS, run, a, b, scope_mode="localized")
    only = _edge(conn, TRANSFERS, run, a, b, scope_mode="competency_only")
    _scope(conn, TRANSFER_CAPS, only, local)
    reval = _edge(conn, REVALS, run, a, b, scope_mode="localized")
    _scope(conn, REVAL_CAPS, reval, foreign)
    assert conn.execute(sa.text("SELECT * FROM observation_capabilities")).all() == mappings_before


def test_pg_known_limit_snapshot_and_edge_membership_are_left_to_t5b(conn, release):
    """LIMITE DOCUMENTÉE (T5-B) : la base accepte en entrée une observation
    d'un autre utilisateur, d'une autre compétence, invalidée, ou issue
    d'un run T3 non active ; et des arêtes entre observations absentes du
    snapshot, ou une dépendance envers une aide du même événement. Une
    dépendance et un transfert peuvent coexister sur le même scope."""
    run_id = _assessment(conn, release, user_id=USER, competency_code="C7")
    other_event = uuid.uuid4()
    conn.execute(INSERT_EVENT, {"id": other_event, "u": OTHER_USER})
    other_user_obs = _obs(conn, _run(conn, other_event), 1)
    invalidated = _observation(conn, integrity_status="invalidated", invalidated_at=NOW,
                               invalidation_reason="erreur d'extraction")
    superseded_run_obs = _obs(conn, _run(conn, _event(conn), interpretation_status="superseded"), 1)
    for observation in (other_user_obs, _observation(conn, "C8"), invalidated, superseded_run_obs):
        _input(conn, run_id, observation)
    outside_a, outside_b = _observation(conn), _observation(conn)
    _dependency(conn, run_id, outside_b, source_observation=outside_a)
    _transfer(conn, run_id, outside_a, outside_b)
    event_id = _event(conn)
    same_event_target = _obs(conn, _run(conn, event_id), 1)
    _dependency(conn, run_id, same_event_target, source_trace=_trace(conn, event_id, 1))
    assert (_count(conn, INPUTS), _count(conn, DEPS), _count(conn, TRANSFERS)) == (4, 2, 1)


# --- aucune cascade destructive ------------------------------------------------------------------

def test_pg_no_destructive_cascade(conn, release):
    """Supprimer un run, une release, un utilisateur, une observation ou une
    aide encore référencés par T5 est refusé ; rien n'est supprimé en
    cascade (ni à la suppression, ni à la modification d'une clé)."""
    run_id = _assessment(conn, release)
    # OTHER_USER n'est référencé que par ce run (aucun événement ni session).
    _assessment(conn, release, user_id=OTHER_USER)
    a, b = _observation(conn), _observation(conn)
    trace = _trace(conn, _event(conn), 1)
    _dependency(conn, run_id, b, source_trace=trace)
    _transfer(conn, run_id, a, b)
    _revalidation(conn, run_id, a, b)

    def delete(table, value):
        return lambda: conn.execute(sa.text(f"DELETE FROM {table} WHERE id = :id"), {"id": value})

    _refused(conn, "observation_dependencies_run_id_fkey", delete(ASSESSMENTS, run_id),
             drop=((TRANSFERS, "observation_transfers_run_id_fkey"),
                   (REVALS, "observation_revalidations_run_id_fkey")))
    _refused(conn, "observation_transfers_run_id_fkey", delete(ASSESSMENTS, run_id),
             drop=((DEPS, "observation_dependencies_run_id_fkey"),
                   (REVALS, "observation_revalidations_run_id_fkey")))
    _refused(conn, "observation_revalidations_run_id_fkey", delete(ASSESSMENTS, run_id),
             drop=((DEPS, "observation_dependencies_run_id_fkey"),
                   (TRANSFERS, "observation_transfers_run_id_fkey")))
    _refused(conn, RUN_RELEASE_FK, delete(RELEASES, release))
    _refused(conn, "longitudinal_assessment_runs_user_id_fkey", lambda: conn.execute(
        sa.text("DELETE FROM users WHERE id = :id"), {"id": OTHER_USER}))
    _refused(conn, "observation_dependencies_source_support_trace_id_fkey", delete("support_traces", trace))
    _refused(conn, REVAL_SOURCE_FK, delete(OBS, a),
             drop=((TRANSFERS, "observation_transfers_source_observation_id_fkey"),))
    _refused(conn, REVAL_TARGET_FK, delete(OBS, b),
             drop=((DEPS, "observation_dependencies_target_observation_id_fkey"),
                   (TRANSFERS, "observation_transfers_target_observation_id_fkey")))
    _refused(conn, "observation_transfers_source_observation_id_fkey", delete(OBS, a),
             drop=((REVALS, REVAL_SOURCE_FK),))
    _refused(conn, "observation_dependencies_run_id_fkey", lambda: conn.execute(sa.text(
        f"UPDATE {ASSESSMENTS} SET id = :new WHERE id = :id"), {"new": uuid.uuid4(), "id": run_id}),
        drop=((TRANSFERS, "observation_transfers_run_id_fkey"), (REVALS, "observation_revalidations_run_id_fkey")))
    assert [_count(conn, t) for t in (ASSESSMENTS, DEPS, TRANSFERS, REVALS, OBS, "support_traces")] == [
        2, 1, 1, 1, 2, 1]
    assert _count(conn, "users", "id = :u", {"u": OTHER_USER}) == 1


# --- ORM -----------------------------------------------------------------------------------------------

def test_pg_orm_insertion_uses_application_defaults(pg_url, pg_engine):
    """Insertion via l'ORM des huit modèles : UUID, started_at et created_at
    UTC aware générés côté application ; *_basis dict -> JSONB ; PK
    composites ; un None Python sur *_basis devient NULL SQL (refusé)."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with Session(pg_engine) as session:
        with session.begin():
            connection = session.connection()
            release_id = _release(connection)
            membership = _membership(connection, release_id, _definition(connection, "C7_A"))
            a, b = _observation(connection), _observation(connection)
            contradiction = _contradictory(connection)
            trace = _trace(connection, _event(connection), 1)
            run = LongitudinalAssessmentRun(
                user_id=USER, competency_code="C7", execution_status="running", interpretation_status="candidate",
                trigger="manual_replay", pedagogical_taxonomy_release_id=release_id, dependency_version="d1",
                transfer_version="t1", revalidation_version="r1", relation_schema_version="s1",
                input_fingerprint="sha256:in", assessment_dedup_key="orm",
            )
            session.add(run)
            session.flush()
            dependency = ObservationDependency(
                run_id=run.id, target_observation_id=b, source_kind="support_trace", source_support_trace_id=trace,
                dependency_type="partially_dependent", scope_mode="localized",
                dependency_basis={"why": "indice visible", "refs": [1, None]}, scope_fingerprint="fp",
            )
            transfer = ObservationTransfer(run_id=run.id, source_observation_id=a, target_observation_id=b,
                                           scope_mode="localized", transfer_basis={"variation": "x"},
                                           scope_fingerprint="fp")
            revalidation = ObservationRevalidation(run_id=run.id, source_contradiction_observation_id=contradiction,
                                                   target_supportive_observation_id=b, scope_mode="localized",
                                                   revalidation_basis=[], scope_fingerprint="fp")
            session.add_all([LongitudinalAssessmentInput(run_id=run.id, observation_id=a),
                             LongitudinalAssessmentInput(run_id=run.id, observation_id=b),
                             dependency, transfer, revalidation])
            session.flush()
            session.add_all([DependencyCapability(dependency_id=dependency.id, capability_membership_id=membership),
                             TransferCapability(transfer_id=transfer.id, capability_membership_id=membership),
                             RevalidationCapability(revalidation_id=revalidation.id,
                                                    capability_membership_id=membership)])
            session.flush()
            for obj in (run, dependency, transfer, revalidation):
                session.refresh(obj)
                assert isinstance(obj.id, uuid.UUID)
                assert obj.created_at.tzinfo is not None and obj.created_at.utcoffset().total_seconds() == 0
            assert run.started_at.utcoffset().total_seconds() == 0
            assert (run.completed_at, run.failure_code) == (None, None)
            assert dependency.dependency_basis == {"why": "indice visible", "refs": [1, None]}
            assert revalidation.revalidation_basis == []
            assert session.get(LongitudinalAssessmentInput, (run.id, a)) is not None
            assert session.get(TransferCapability, (transfer.id, membership)) is not None

            with pytest.raises(sa.exc.IntegrityError, match='null value in column "transfer_basis"'):
                with session.begin_nested():
                    session.add(ObservationTransfer(run_id=run.id, source_observation_id=a,
                                                    target_observation_id=b, scope_mode="localized",
                                                    transfer_basis=None, scope_fingerprint="fp"))
            session.rollback()
    with pg_engine.connect() as c:
        assert [_count(c, t) for t in TABLES_IN_ORDER] == [0] * 8


def test_pg_models_timestamps_match_utc(pg_url, pg_engine):
    _upgrade_head_with_users(pg_url, pg_engine)
    with Session(pg_engine) as session:
        with session.begin():
            release_id = _release(session.connection())
            completed = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
            run = LongitudinalAssessmentRun(
                user_id=USER, competency_code="C12", execution_status="completed", interpretation_status="active",
                trigger="t", pedagogical_taxonomy_release_id=release_id, dependency_version="d",
                transfer_version="t", revalidation_version="r", relation_schema_version="s",
                input_fingerprint="f", assessment_dedup_key="k", completed_at=completed,
            )
            session.add(run)
            session.flush()
            session.refresh(run)
            assert run.completed_at == completed
            assert run.created_at.utcoffset().total_seconds() == 0
            session.rollback()


# --- upgrade / downgrade ------------------------------------------------------------------------------

EXISTING = REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES | {"alembic_version"}
DATA_TABLES = REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES


def _seed_t4(engine) -> None:
    """Une release active, une définition, un membership, la localisation de
    l'observation 5555, une observation contradictory 8888 et un run T3
    évalué sous cette release (schéma 0007)."""
    with engine.begin() as conn:
        release_id = _release(conn, id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
                              status="active", activated_at=NOW)
        definition = _definition(conn, "C7_A", id=uuid.UUID("77777777-7777-7777-7777-777777777777"))
        membership = _membership(conn, release_id, definition)
        _map(conn, uuid.UUID("55555555-5555-5555-5555-555555555555"), membership)
        _obs(conn, uuid.UUID("44444444-4444-4444-4444-444444444444"), 2,
             id=uuid.UUID("88888888-8888-8888-8888-888888888888"), polarity="contradictory", local_stage=None,
             contradiction_scope="comprehension", error_type="conceptual")
        _run(conn, uuid.UUID("22222222-2222-2222-2222-222222222222"), pedagogical_taxonomy_release_id=release_id)


def _upgrade_to_0007_with_data(pg_url, pg_engine) -> None:
    _upgrade_to_0006_with_data(pg_url, pg_engine)
    _run_alembic(pg_url, "upgrade", T4A)
    _seed_t4(pg_engine)


def test_pg_upgrade_0007_to_0008_preserves_everything_then_downgrade(pg_url, pg_engine):
    """0007 -> 0008 conserve schéma, catalogue et données de TOUTES les
    tables T0-T4 (taxonomie comprise) ; les huit tables T5 sont vides ;
    aucun ENUM, trigger, fonction ni séquence ajouté. Le downgrade supprime
    exactement T5 et restaure exactement 0007 (schéma, catalogue,
    données) ; le ré-upgrade produit la même structure T5."""
    _upgrade_to_0007_with_data(pg_url, pg_engine)
    schema_0007 = _snapshot(pg_engine, EXISTING)
    catalog_0007 = _catalog(pg_engine, EXISTING)
    global_0007 = _global_catalog(pg_engine)
    data_0007 = _data(pg_engine, DATA_TABLES)
    assert all(data_0007[t] for t in DATA_TABLES)

    # --- upgrade 0007 -> 0008 --------------------------------------------
    _run_alembic(pg_url, "upgrade", T5A)
    assert _version(pg_engine) == T5A
    assert _tables(pg_engine) == EXISTING | T5A_TABLES
    assert _snapshot(pg_engine, EXISTING) == schema_0007
    assert _catalog(pg_engine, EXISTING) == catalog_0007
    assert _data(pg_engine, DATA_TABLES) == data_0007
    assert _data(pg_engine, T5A_TABLES) == {name: [] for name in sorted(T5A_TABLES)}
    assert _global_catalog(pg_engine) == global_0007
    assert (global_0007["enums"], global_0007["triggers"], global_0007["functions"]) == (0, 0, 0)
    _assert_metadata_matches_0008(pg_engine)
    schema_t5 = _snapshot(pg_engine, T5A_TABLES)
    catalog_t5 = _catalog(pg_engine, T5A_TABLES)

    # Données T5 (supprimées par le downgrade, comme leurs tables).
    with pg_engine.begin() as conn:
        release_id = uuid.UUID("66666666-6666-6666-6666-666666666666")
        run_id = _assessment(conn, release_id, interpretation_status="active")
        supportive = uuid.UUID("55555555-5555-5555-5555-555555555555")
        contradiction = uuid.UUID("88888888-8888-8888-8888-888888888888")
        _input(conn, run_id, supportive)
        _input(conn, run_id, contradiction)
        edge = _dependency(conn, run_id, supportive, source_trace=uuid.UUID("33333333-3333-3333-3333-333333333333"))
        membership = conn.execute(sa.text(f"SELECT id FROM {MEMBERSHIPS}")).scalar_one()
        _scope(conn, DEP_CAPS, edge, membership)
        _scope(conn, TRANSFER_CAPS, _transfer(conn, run_id, contradiction, supportive), membership)
        _scope(conn, REVAL_CAPS, _revalidation(conn, run_id, contradiction, supportive), membership)
    assert all(_data(pg_engine, T5A_TABLES)[t] for t in T5A_TABLES)

    # --- downgrade 0008 -> 0007 ------------------------------------------
    _run_alembic(pg_url, "downgrade", T4A)
    assert _version(pg_engine) == T4A
    assert _tables(pg_engine) == EXISTING
    _assert_absent(pg_engine, [*T5A_TABLES, *T5A_INDEXES])
    assert _snapshot(pg_engine, EXISTING) == schema_0007
    assert _catalog(pg_engine, EXISTING) == catalog_0007
    assert _global_catalog(pg_engine) == global_0007
    # Toutes les données T0-T4 (dont release, définition, membership,
    # localisation, runs, observations, événements, aides) : intactes.
    assert _data(pg_engine, DATA_TABLES) == data_0007

    # --- ré-upgrade 0007 -> 0008 -----------------------------------------
    _run_alembic(pg_url, "upgrade", T5A)
    assert _version(pg_engine) == T5A
    assert _snapshot(pg_engine, EXISTING) == schema_0007
    assert _snapshot(pg_engine, T5A_TABLES) == schema_t5
    assert _catalog(pg_engine, T5A_TABLES) == catalog_t5
    assert _data(pg_engine, DATA_TABLES) == data_0007
    assert _data(pg_engine, T5A_TABLES) == {name: [] for name in sorted(T5A_TABLES)}
    _assert_metadata_matches_0008(pg_engine)
