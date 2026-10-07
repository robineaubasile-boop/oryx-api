"""Tests de T3-A : migration 0006_observation_layer + modèles
ObservationEvaluationRun / PedagogicalObservation.

Même organisation que les tests des migrations 0002 à 0005 (dont on
réutilise les helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, intégrité de 0001
   à 0005, SQL PostgreSQL généré en mode offline, métadonnées des modèles
   (colonnes, types, FK, CHECK, UNIQUE, index partiel, défauts), absence de
   tout branchement applicatif et de service autre que T3-B. La taxonomie
   T4 (0007, FK pedagogical_taxonomy_release_id comprise) est testée dans
   tests/test_migration_0007_pedagogical_taxonomy.py.

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t3a_test \\
           python -m pytest tests/test_migration_0006_observation_layer.py

T3-A = schéma + modèles seulement : aucune route ne lit ni n'écrit ces
tables. Le seul module applicatif autorisé à les écrire est le service T3-B
core/observation_service.py (lui-même non branché, testé dans
tests/test_observation_service.py).
"""
import hashlib
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from core.db import Base
from core.models import CognitiveEvent, ObservationEvaluationRun, PedagogicalObservation, SupportTrace
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
from tests.test_migration_0003_analysis_session_links import T1A_SHA256, T1B1, _statements
from tests.test_migration_0004_drop_company_analyses import (
    _without_r1d1_changes,
    R1B_TABLES,
    R1C1_TABLES,
    R1C2_TABLES,
    REMAINING_TABLES,
    T1B1_SHA256,
    T1C2,
    T3A_INDEXES,
    T3A_TABLES,
    T4A_INDEXES,
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
    _indexes,
    _seed_remaining_tables,
    _without_r1b_changes,
    _without_r1c1_changes,
    _without_r1c2_changes,
)
from tests.test_migration_0005_cognitive_support_traces import (
    T1C2_SHA256,
    T2A,
    T2A_TABLES,
    USER,
    _catalog,
    _global_catalog,
    _upgrade_head_with_users,
)

T3A = "0006_observation_layer"
RUNS = "observation_evaluation_runs"
OBS = "pedagogical_observations"
ACTIVE_INDEX = "uq_observation_evaluation_runs_one_active_event"
assert T3A_TABLES == {RUNS, OBS} and T3A_INDEXES == {ACTIVE_INDEX}
# sha256 de alembic/versions/0005_cognitive_support_traces.py tel que mergé
# sur main (d879311, T2-A) et déployé en production.
T2A_SHA256 = "4782b2ad938a5e79f160fb04ce14b4e6cc7bf57f0fe15924c669d5a36b75aaa7"

RUN_COLUMNS = [
    "id",
    "event_id",
    "execution_status",
    "interpretation_status",
    "trigger",
    "re_evaluates_run_id",
    "evaluation_dedup_key",
    "normalization_version",
    "local_stage_version",
    "pedagogical_taxonomy_release_id",
    "capability_mapping_version",
    "evaluation_schema_version",
    "evaluator_version",
    "model_id",
    "prompt_spec_version",
    "input_fingerprint",
    "output_fingerprint",
    "started_at",
    "completed_at",
    "created_at",
    "failure_code",
]
RUN_NULLABLE = {
    "re_evaluates_run_id",
    "pedagogical_taxonomy_release_id",
    "model_id",
    "prompt_spec_version",
    "output_fingerprint",
    "completed_at",
    "failure_code",
}
OBS_COLUMNS = [
    "id",
    "evaluation_run_id",
    "ordinal",
    "competency_code",
    "observation_role",
    "task_kind",
    "primary_user_action",
    "contributive_user_actions",
    "elicitation_mode",
    "support_level",
    "source_contribution_refs",
    "residual_cognitive_work",
    "polarity",
    "evidence_strength",
    "local_stage",
    "contradiction_scope",
    "error_type",
    "observation_text",
    "capability_localization",
    "integrity_status",
    "invalidated_at",
    "invalidation_reason",
    "created_at",
]
OBS_NULLABLE = {
    "task_kind",
    "local_stage",
    "contradiction_scope",
    "error_type",
    "invalidated_at",
    "invalidation_reason",
}
UUID_COLUMNS = {
    (RUNS, "id"),
    (RUNS, "event_id"),
    (RUNS, "re_evaluates_run_id"),
    (RUNS, "pedagogical_taxonomy_release_id"),
    (OBS, "id"),
    (OBS, "evaluation_run_id"),
}
JSONB_COLUMNS = {
    (OBS, "primary_user_action"),
    (OBS, "contributive_user_actions"),
    (OBS, "source_contribution_refs"),
    (OBS, "residual_cognitive_work"),
}
TIMESTAMPTZ_COLUMNS = {
    (RUNS, "started_at"),
    (RUNS, "completed_at"),
    (RUNS, "created_at"),
    (OBS, "invalidated_at"),
    (OBS, "created_at"),
}
TEXT_COLUMNS = {(OBS, "observation_text"), (OBS, "invalidation_reason")}
# R1-D1 (0014, testée à part) : lease persistée ajoutée au MODÈLE
# ObservationEvaluationRun (la migration 0006 reste inchangée).
R1D1_RUN_COLUMNS = ["lease_token", "lease_expires_at"]
R1D1_UUID_COLUMNS = {(RUNS, "lease_token")}
R1D1_TIMESTAMPTZ_COLUMNS = {(RUNS, "lease_expires_at")}
R1D1_RUN_CHECKS = {
    "ck_observation_evaluation_runs_lease_pair": "(lease_token IS NULL) = (lease_expires_at IS NULL)",
    "ck_observation_evaluation_runs_lease_running": "lease_token IS NULL OR execution_status = 'running'",
}

# Vocabulaires fermés : (table, colonne) -> valeurs autorisées, dans l'ordre
# du CHECK. Toute autre colonne VARCHAR est extensible (trigger, task_kind,
# versions, empreintes...).
RUN_VOCABULARIES = {
    "execution_status": ("running", "completed", "failed"),
    "interpretation_status": ("candidate", "active", "superseded", "obsolete"),
}
OBS_VOCABULARIES = {
    "competency_code": tuple(f"C{i}" for i in range(1, 13)),
    "observation_role": ("primary", "secondary"),
    "elicitation_mode": ("prompted", "spontaneous"),
    "support_level": ("none", "hinted", "guided", "answer_given"),
    "polarity": ("supportive", "contradictory"),
    "evidence_strength": ("weak", "medium", "strong"),
    "local_stage": ("none", "discovery", "comprehension", "application"),
    "contradiction_scope": ("recognition", "comprehension", "application", "undetermined"),
    "error_type": ("conceptual", "procedural", "execution", "factual_premise"),
    "capability_localization": ("localized", "competency_only"),
    "integrity_status": ("valid", "invalidated"),
}
POLARITY_CHECK = "ck_pedagogical_observations_polarity_stage_scope"
INTEGRITY_CHECK = "ck_pedagogical_observations_integrity_invalidation"
POLARITY_CHECK_SQL = (
    "(polarity = 'supportive' AND local_stage IS NOT NULL AND contradiction_scope IS NULL) "
    "OR (polarity = 'contradictory' AND local_stage IS NULL AND contradiction_scope IS NOT NULL)"
)
INTEGRITY_CHECK_SQL = (
    "(integrity_status = 'valid' AND invalidated_at IS NULL AND invalidation_reason IS NULL) "
    "OR (integrity_status = 'invalidated' AND invalidated_at IS NOT NULL "
    "AND invalidation_reason IS NOT NULL)"
)


def _in_sql(column, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _expected_checks(table) -> dict:
    vocabularies = RUN_VOCABULARIES if table == RUNS else OBS_VOCABULARIES
    checks = {f"ck_{table}_{col}": _in_sql(col, values) for col, values in vocabularies.items()}
    if table == OBS:
        checks[POLARITY_CHECK] = POLARITY_CHECK_SQL
        checks[INTEGRITY_CHECK] = INTEGRITY_CHECK_SQL
    return checks


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_0001_to_0006():
    """0001 -> ... -> 0005 -> 0006. La tête de chaîne évolue avec les
    migrations suivantes (T4-A : voir
    tests/test_migration_0007_pedagogical_taxonomy.py) ; on vérifie ici les
    maillons jusqu'à 0006."""
    script = _script_directory()
    assert script.get_bases() == [BASELINE]

    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert {BASELINE, T1A, T1B1, T1C2, T2A, T3A} <= set(revisions)
    assert revisions[T3A].down_revision == T2A
    assert revisions[T2A].down_revision == T1C2

    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[:6] == [f"{rev}.py" for rev in (BASELINE, T1A, T1B1, T1C2, T2A, T3A)]


def test_revision_id_fits_alembic_version_column():
    """alembic_version.version_num est un VARCHAR(32)."""
    assert len(T3A) <= 32
    for rev in _script_directory().walk_revisions():
        assert len(rev.revision) <= 32, rev.revision


def test_0001_to_0005_files_are_unchanged():
    """Migrations historiques immuables (dont 0005 : T2 n'est pas remodelé)."""
    versions = REPO_ROOT / "alembic" / "versions"
    for rev, expected in (
        (BASELINE, BASELINE_SHA256),
        (T1A, T1A_SHA256),
        (T1B1, T1B1_SHA256),
        (T1C2, T1C2_SHA256),
        (T2A, T2A_SHA256),
    ):
        assert hashlib.sha256((versions / f"{rev}.py").read_bytes()).hexdigest() == expected, rev


def _checks_sql(table) -> str:
    return ", ".join(f"CONSTRAINT {name} CHECK ({sql})" for name, sql in _expected_checks(table).items())


def test_offline_sql_of_0006_upgrade_creates_exactly_the_two_tables_and_the_partial_index():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "upgrade", f"{T2A}:{T3A}", "--sql",
    ).stdout
    assert _statements(sql) == [
        "CREATE TABLE observation_evaluation_runs ( id UUID NOT NULL, event_id UUID NOT NULL, "
        "execution_status VARCHAR NOT NULL, interpretation_status VARCHAR NOT NULL, "
        "trigger VARCHAR NOT NULL, re_evaluates_run_id UUID, evaluation_dedup_key VARCHAR NOT NULL, "
        "normalization_version VARCHAR NOT NULL, local_stage_version VARCHAR NOT NULL, "
        "pedagogical_taxonomy_release_id UUID, capability_mapping_version VARCHAR NOT NULL, "
        "evaluation_schema_version VARCHAR NOT NULL, evaluator_version VARCHAR NOT NULL, "
        "model_id VARCHAR, prompt_spec_version VARCHAR, input_fingerprint VARCHAR NOT NULL, "
        "output_fingerprint VARCHAR, started_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "completed_at TIMESTAMP WITH TIME ZONE, created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "failure_code VARCHAR, "
        "PRIMARY KEY (id), "
        f"{_checks_sql(RUNS)}, "
        "FOREIGN KEY(event_id) REFERENCES cognitive_events (id), "
        "FOREIGN KEY(re_evaluates_run_id) REFERENCES observation_evaluation_runs (id), "
        "CONSTRAINT uq_observation_evaluation_runs_dedup_key UNIQUE (evaluation_dedup_key) )",
        f"CREATE UNIQUE INDEX {ACTIVE_INDEX} ON observation_evaluation_runs (event_id) "
        "WHERE interpretation_status = 'active'",
        "CREATE TABLE pedagogical_observations ( id UUID NOT NULL, evaluation_run_id UUID NOT NULL, "
        "ordinal SMALLINT NOT NULL, competency_code VARCHAR NOT NULL, "
        "observation_role VARCHAR NOT NULL, task_kind VARCHAR, primary_user_action JSONB NOT NULL, "
        "contributive_user_actions JSONB NOT NULL, elicitation_mode VARCHAR NOT NULL, "
        "support_level VARCHAR NOT NULL, source_contribution_refs JSONB NOT NULL, "
        "residual_cognitive_work JSONB NOT NULL, polarity VARCHAR NOT NULL, "
        "evidence_strength VARCHAR NOT NULL, local_stage VARCHAR, contradiction_scope VARCHAR, "
        "error_type VARCHAR, observation_text TEXT NOT NULL, capability_localization VARCHAR NOT NULL, "
        "integrity_status VARCHAR NOT NULL, invalidated_at TIMESTAMP WITH TIME ZONE, "
        "invalidation_reason TEXT, created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "PRIMARY KEY (id), "
        f"{_checks_sql(OBS)}, "
        "FOREIGN KEY(evaluation_run_id) REFERENCES observation_evaluation_runs (id), "
        "CONSTRAINT uq_pedagogical_observations_run_ordinal UNIQUE (evaluation_run_id, ordinal) )",
        # Seule écriture de données : la version Alembic (aucun backfill).
        "UPDATE alembic_version SET version_num='0006_observation_layer' "
        "WHERE alembic_version.version_num = '0005_cognitive_support_traces'",
    ]
    for forbidden in ("ALTER TABLE", "DROP ", "INSERT", "CREATE TYPE", "CREATE EXTENSION",
                      "CREATE TRIGGER", "CREATE FUNCTION", "DEFAULT", "ON DELETE", "ON UPDATE",
                      "CASCADE", "ENUM", "MASTERY", "TAXONOMY_RELEASES", "CAPABILITY_DEFINITIONS"):
        assert forbidden not in sql.upper(), forbidden
    assert sql.upper().count("CREATE UNIQUE INDEX") == 1
    assert "CREATE INDEX" not in sql.upper()


def test_offline_sql_of_0006_downgrade_drops_only_the_two_tables_and_the_index():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "downgrade", f"{T3A}:{T2A}", "--sql",
    ).stdout
    assert _statements(sql) == [
        "DROP TABLE pedagogical_observations",
        f"DROP INDEX {ACTIVE_INDEX}",
        "DROP TABLE observation_evaluation_runs",
        "UPDATE alembic_version SET version_num='0005_cognitive_support_traces' "
        "WHERE alembic_version.version_num = '0006_observation_layer'",
    ]
    assert "CASCADE" not in sql.upper()


def test_metadata_declares_the_two_new_tables():
    """Base.metadata = tables de 0005 + observation_evaluation_runs +
    pedagogical_observations (+ les quatre tables T4-A, les huit tables
    T5-A et les six tables T6-A, testées à part) ; aucune autre table de
    taxonomie ou de capacités."""
    assert set(Base.metadata.tables) == (REMAINING_TABLES | T2A_TABLES | {RUNS, OBS} | T4A_TABLES | T5A_TABLES
                                         | T6A_TABLES | R1B_TABLES | R1C1_TABLES | R1C2_TABLES)
    assert ObservationEvaluationRun.__table__ is Base.metadata.tables[RUNS]
    assert PedagogicalObservation.__table__ is Base.metadata.tables[OBS]
    # T6-A : competency_inference_tension_capabilities = périmètre d'une
    # tension (FK vers capability_taxonomy_memberships), testé à part.
    for name in set(Base.metadata.tables) - T4A_TABLES - T5A_TABLES - T6A_TABLES:
        assert "taxonom" not in name and "capabilit" not in name, name


def _assert_columns(table, expected, nullable):
    assert [c.name for c in table.columns] == expected
    assert [c.name for c in table.primary_key.columns] == ["id"]
    assert {c.name: c.nullable for c in table.columns} == {n: n in nullable for n in expected}
    for col in table.columns:
        key = (table.name, col.name)
        if key in UUID_COLUMNS | R1D1_UUID_COLUMNS:
            assert isinstance(col.type, sa.Uuid) and col.type.as_uuid is True, key
        elif key in JSONB_COLUMNS:
            assert type(col.type) is JSONB, key
        elif key in TIMESTAMPTZ_COLUMNS | R1D1_TIMESTAMPTZ_COLUMNS:
            assert isinstance(col.type, sa.DateTime) and col.type.timezone is True, key
        elif key in TEXT_COLUMNS:
            assert type(col.type) is sa.Text, key
        elif key == (OBS, "ordinal"):
            assert type(col.type) is sa.SmallInteger
            assert col.autoincrement in ("auto", False) and not col.primary_key
        else:
            # Tout le reste : VARCHAR sans longueur.
            assert type(col.type) is sa.String and col.type.length is None, key


def test_observation_evaluation_run_columns_types_nullability_and_fks():
    table = ObservationEvaluationRun.__table__
    _assert_columns(table, RUN_COLUMNS + R1D1_RUN_COLUMNS, RUN_NULLABLE | set(R1D1_RUN_COLUMNS))
    fks = sorted((fk.parent.name, fk.target_fullname) for fk in table.foreign_keys)
    # La FK de pedagogical_taxonomy_release_id est ajoutée par T4-A (0007,
    # testée à part) ; la colonne reste nullable.
    assert fks == [
        ("event_id", "cognitive_events.id"),
        ("pedagogical_taxonomy_release_id", "pedagogical_taxonomy_releases.id"),
        ("re_evaluates_run_id", "observation_evaluation_runs.id"),
    ]
    for fk in table.foreign_keys:
        assert type(fk.column.type) is type(fk.parent.type), fk.parent.name
    assert table.c.pedagogical_taxonomy_release_id.nullable is True


def test_pedagogical_observation_columns_types_nullability_and_fks():
    table = PedagogicalObservation.__table__
    _assert_columns(table, OBS_COLUMNS, OBS_NULLABLE)
    fks = [(fk.parent.name, fk.target_fullname) for fk in table.foreign_keys]
    assert fks == [("evaluation_run_id", "observation_evaluation_runs.id")]
    # La provenance vient du run : event_id n'est pas dupliqué.
    assert "event_id" not in table.c
    assert "cognitive_event_id" not in table.c


def test_check_constraints_are_exactly_the_closed_vocabularies_and_cross_checks():
    """CHECK nommés : un par vocabulaire fermé + polarity/stage/scope +
    intégrité. Aucun CHECK sur trigger ni task_kind (extensibles)."""
    for model in (ObservationEvaluationRun, PedagogicalObservation):
        table = model.__table__
        checks = {c.name: str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
        expected = _expected_checks(table.name) | (R1D1_RUN_CHECKS if table.name == RUNS else {})
        assert checks == expected, table.name
        for sqltext in checks.values():
            assert "trigger" not in sqltext and "task_kind" not in sqltext


def test_mastery_and_forbidden_values_are_absent_from_vocabularies():
    """mastery n'est jamais un stade local ; pas de polarité mixed ; la
    supersession appartient au run, pas à l'intégrité de l'observation."""
    assert "mastery" not in OBS_VOCABULARIES["local_stage"]
    assert "mixed" not in OBS_VOCABULARIES["polarity"]
    assert set(OBS_VOCABULARIES["integrity_status"]) == {"valid", "invalidated"}
    for table in (RUNS, OBS):
        for sqltext in _expected_checks(table).values():
            assert "mastery" not in sqltext.lower()
            assert "mixed" not in sqltext.lower()


def test_unique_constraints_and_partial_index():
    runs, obs = ObservationEvaluationRun.__table__, PedagogicalObservation.__table__
    uniques = {c.name: [col.name for col in c.columns]
               for t in (runs, obs) for c in t.constraints if isinstance(c, sa.UniqueConstraint)}
    # Aucune unicité sur competency_code : plusieurs observations d'une même
    # compétence sont possibles dans un même run.
    assert uniques == {
        "uq_observation_evaluation_runs_dedup_key": ["evaluation_dedup_key"],
        "uq_pedagogical_observations_run_ordinal": ["evaluation_run_id", "ordinal"],
    }
    assert not obs.indexes
    # Seul autre index de runs : celui de la FK T4-A (0007, testé à part).
    assert {i.name for i in runs.indexes} == {
        ACTIVE_INDEX, "ix_observation_evaluation_runs_pedagogical_taxonomy_release_id",
    }
    (index,) = [i for i in runs.indexes if i.name == ACTIVE_INDEX]
    assert index.unique is True
    assert [c.name for c in index.columns] == ["event_id"]
    assert str(index.dialect_options["postgresql"]["where"]) == "interpretation_status = 'active'"
    for table in (runs, obs):
        assert not any(c.unique or c.index for c in table.columns), table.name


def test_no_foreign_key_cascades():
    for table in (ObservationEvaluationRun.__table__, PedagogicalObservation.__table__):
        for fk in table.foreign_keys:
            assert fk.ondelete is None and fk.onupdate is None, (table.name, fk.parent.name)


def test_no_server_defaults_only_application_defaults():
    """Aucun server_default ; UUID et started_at / created_at générés par
    l'application (UTC aware) ; aucune autre valeur inventée."""
    runs, obs = ObservationEvaluationRun.__table__.c, PedagogicalObservation.__table__.c
    for table in (runs, obs):
        for col in table:
            assert col.server_default is None and col.server_onupdate is None, col.name
            assert col.onupdate is None, col.name

    def call(default):
        assert default is not None and default.is_callable
        return default.arg(None)

    for col in (runs.id, obs.id):
        assert isinstance(call(col.default), uuid.UUID)
        assert call(col.default) != call(col.default)
    with_default = {(RUNS, "id"), (RUNS, "started_at"), (RUNS, "created_at"), (OBS, "id"), (OBS, "created_at")}
    for col in (runs.started_at, runs.created_at, obs.created_at):
        assert call(col.default).utcoffset().total_seconds() == 0, col.name
    for table in (runs, obs):
        for col in table:
            if (col.table.name, col.name) not in with_default:
                assert col.default is None, (col.table.name, col.name)


def test_jsonb_and_timestamptz_columns():
    jsonb, tz = set(), set()
    for table in (ObservationEvaluationRun.__table__, PedagogicalObservation.__table__):
        for col in table.columns:
            if isinstance(col.type, sa.JSON):
                assert type(col.type) is JSONB
                # Un None Python devient NULL SQL (refusé), jamais le JSON null.
                assert col.type.none_as_null is True, col.name
                jsonb.add((table.name, col.name))
            if isinstance(col.type, sa.DateTime):
                assert col.type.timezone is True, col.name
                tz.add((table.name, col.name))
    assert jsonb == JSONB_COLUMNS
    assert tz == TIMESTAMPTZ_COLUMNS | R1D1_TIMESTAMPTZ_COLUMNS


def test_no_relationships_and_t2_models_untouched():
    """Les FK suffisent : aucune relationship ORM ; aucun modèle T2 ne gagne
    de colonne vers T3 (T3 dépend de T2 sans le remodeler)."""
    for model in (ObservationEvaluationRun, PedagogicalObservation, CognitiveEvent, SupportTrace):
        assert not sa.inspect(model).relationships, model.__name__
    for name in REMAINING_TABLES | T2A_TABLES:
        for col in Base.metadata.tables[name].columns:
            assert all(fk.column.table.name not in T3A_TABLES for fk in col.foreign_keys), name


def test_no_score_progress_or_global_state_columns():
    """Une observation n'est ni un score, ni un XP, ni une confiance, ni un
    état global de compétence, ni une progression."""
    forbidden = ("score", "xp", "confidence", "progress", "mastery", "level_", "posture",
                 "inference", "user_level", "competency_state")
    for table in (ObservationEvaluationRun.__table__, PedagogicalObservation.__table__):
        for col in table.columns:
            # lease_expires_at (R1-D1) : « xp » de « expires », pas un XP.
            name = col.name.replace("expires", "")
            assert not any(word in name for word in forbidden), (table.name, col.name)
        assert "user_id" not in table.c, table.name


def test_t3a_tables_are_not_wired_to_the_application():
    """Aucun branchement : hors core/models.py, la migration 0006 et le
    service T3-B core/observation_service.py (lui-même non branché : voir
    tests/test_observation_service.py), aucun code applicatif (api.py,
    core/, scripts/, frontend) ne mentionne ces modèles ou ces tables."""
    allowed = {"core/models.py", f"alembic/versions/{T3A}.py", "core/observation_service.py",
               # T4-A : FK vers pedagogical_observations et FK ajoutée à
               # observation_evaluation_runs (aucun branchement applicatif).
               "alembic/versions/0007_pedagogical_taxonomy.py",
               # T4-B : service de taxonomie (mapping des observations),
               # lui-même non branché (tests/test_taxonomy_service.py).
               "core/taxonomy_service.py",
               # T5-A : FK des relations longitudinales vers
               # pedagogical_observations (aucun branchement applicatif).
               "alembic/versions/0008_longitudinal_relations.py",
               # T5-B : service longitudinal, qui LIT les observations et
               # leurs runs pour construire le snapshot (jamais d'écriture ;
               # lui-même non branché : tests/test_longitudinal_service.py).
               "core/longitudinal_service.py",
               # T5-C : reconstruction READ-ONLY du dossier (SELECT
               # uniquement ; non branchée : tests/test_longitudinal_view.py).
               "core/longitudinal_view.py",
               # T6-A : FK de provenance des basis refs vers
               # pedagogical_observations (aucun branchement applicatif).
               "alembic/versions/0009_competency_inference_state.py",
               # T6-B : service d'inférence, qui LIT et verrouille en FOR
               # SHARE les observations et runs T3 du dossier (jamais
               # d'écriture T3 ; non branché : tests/test_inference_service.py).
               "core/inference_service.py",
               # R1-D1 : migration 0014 (lease des runs), orchestration du
               # worker interne d'évaluation (écritures exclusivement via
               # T3-B / T4-B) et vérification de schéma au démarrage du
               # worker. Aucune route : api.py ne les nomme pas.
               "alembic/versions/0014_evaluation_run_leases.py",
               "core/evaluation_runtime.py",
               "core/evaluation_worker.py"}
    needles = ("ObservationEvaluationRun", "PedagogicalObservation",
               "observation_evaluation_run", "pedagogical_observation")
    checked = 0
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or rel.startswith(("tests/", ".git/")) or "node_modules" in path.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            source = _code_tokens(source)
        checked += 1
        if rel not in allowed:
            for needle in needles:
                assert needle not in source, (rel, needle)
    assert checked > 0


def test_only_the_t3b_service():
    """Le lifecycle vit uniquement dans le service T3-B
    core/observation_service.py : aucun autre module de service, aucune
    fonction activate/supersede séparée. (Les tables de taxonomie existent
    depuis T4-A, sans service : voir
    tests/test_migration_0007_pedagogical_taxonomy.py.)"""
    assert (REPO_ROOT / "core" / "observation_service.py").exists()
    for name in ("observation_evaluation.py", "observation_capture.py"):
        assert not (REPO_ROOT / "core" / name).exists(), name
    lifecycle = ("start_run", "complete_run", "fail_run", "activate_run", "supersede_run")
    for path in list((REPO_ROOT / "core").glob("*.py")) + [REPO_ROOT / "api.py"]:
        tokens = _code_tokens(path.read_text(encoding="utf-8")).split("\n")
        for name in lifecycle:
            assert name not in tokens, (path.name, name)
        if path.name != "observation_service.py":
            assert "invalidate_observation" not in tokens, path.name


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

INSERT_EVENT = sa.text(
    "INSERT INTO cognitive_events (id, user_id, event_origin, task_kind, status, stimulus_snapshot, "
    "user_work_snapshot, started_at, updated_at, closed_at) VALUES (:id, :u, 'education', "
    "'interpret_metric', 'finalized', CAST('{\"question\": \"Que mesure le ROE ?\"}' AS JSONB), "
    "CAST('[{\"text\": \"la rentabilité des capitaux propres\"}]' AS JSONB), now(), now(), now())"
)
INSERT_RUN = sa.text(
    f"INSERT INTO observation_evaluation_runs ({', '.join(RUN_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in RUN_COLUMNS)})"
)
OBS_JSONB = {name for (_, name) in JSONB_COLUMNS}
INSERT_OBS = sa.text(
    f"INSERT INTO pedagogical_observations ({', '.join(OBS_COLUMNS)}) VALUES ("
    + ", ".join(f"CAST(:{c} AS JSONB)" if c in OBS_JSONB else f":{c}" for c in OBS_COLUMNS)
    + ")"
)
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _event(conn) -> uuid.UUID:
    event_id = uuid.uuid4()
    conn.execute(INSERT_EVENT, {"id": event_id, "u": USER})
    return event_id


def _run(conn, event_id, **overrides) -> uuid.UUID:
    params = {
        "id": uuid.uuid4(),
        "event_id": event_id,
        "execution_status": "completed",
        "interpretation_status": "candidate",
        "trigger": "event_finalized",
        "re_evaluates_run_id": None,
        "evaluation_dedup_key": f"dedup-{uuid.uuid4()}",
        "normalization_version": "norm-1",
        "local_stage_version": "stage-1",
        "pedagogical_taxonomy_release_id": None,
        "capability_mapping_version": "map-1",
        "evaluation_schema_version": "schema-1",
        "evaluator_version": "evaluator-1",
        "model_id": None,
        "prompt_spec_version": None,
        "input_fingerprint": "sha256:in",
        "output_fingerprint": None,
        "started_at": NOW,
        "completed_at": NOW,
        "created_at": NOW,
        "failure_code": None,
    }
    params.update(overrides)
    conn.execute(INSERT_RUN, params)
    return params["id"]


def _obs(conn, run_id, ordinal, **overrides) -> uuid.UUID:
    params = {
        "id": uuid.uuid4(),
        "evaluation_run_id": run_id,
        "ordinal": ordinal,
        "competency_code": "C7",
        "observation_role": "primary",
        "task_kind": "interpret_metric",
        "primary_user_action": '{"work_index": 0, "kind": "explanation"}',
        "contributive_user_actions": "[]",
        "elicitation_mode": "prompted",
        "support_level": "none",
        "source_contribution_refs": '[{"work_index": 0}]',
        "residual_cognitive_work": '{"description": "relier le ROE aux capitaux propres"}',
        "polarity": "supportive",
        "evidence_strength": "medium",
        "local_stage": "comprehension",
        "contradiction_scope": None,
        "error_type": None,
        "observation_text": "Explique correctement ce que mesure le ROE.",
        "capability_localization": "competency_only",
        "integrity_status": "valid",
        "invalidated_at": None,
        "invalidation_reason": None,
        "created_at": NOW,
    }
    params.update(overrides)
    conn.execute(INSERT_OBS, params)
    return params["id"]


def _refused(conn, match, action, *, isolate=()):
    """`action` doit être refusée par PostgreSQL avec une erreur contenant
    `match`. `isolate` : CHECK croisés de pedagogical_observations retirés
    le temps du test (dans un savepoint annulé) pour vérifier un CHECK de
    vocabulaire seul."""
    with pytest.raises(sa.exc.IntegrityError, match=match):
        with conn.begin_nested():
            for name in isolate:
                conn.execute(sa.text(f"ALTER TABLE {OBS} DROP CONSTRAINT {name}"))
            action()


def _count(conn, table, where="TRUE", params=None) -> int:
    return conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {where}"), params or {}).scalar_one()


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


def _pg_in_check(column, values) -> str:
    array = ", ".join(f"'{v}'::character varying" for v in values)
    return f"CHECK ((({column})::text = ANY ((ARRAY[{array}])::text[])))"


T4A_RELEASE_FK = "observation_evaluation_runs_taxonomy_release_id_fkey"


def _assert_metadata_matches_0006(engine) -> None:
    """Au schéma 0006, Base.metadata ne diffère que par T4-A (0007) : les
    quatre tables de taxonomie, leurs index, et l'index + la FK ajoutés à
    observation_evaluation_runs.pedagogical_taxonomy_release_id ; et par
    T5-A (0008) : les huit tables de relations longitudinales et leurs
    index ; et par T6-A (0009) : les six tables d'inférence de l'état et
    leurs index ; et par les écarts R1-B (0010) ; tout le reste correspond
    exactement."""
    diff = _without_r1b_changes(
        _without_r1c1_changes(_without_r1c2_changes(_without_r1d1_changes(_compare_metadata(engine)))),
                                events_created=True)
    assert sorted((d[0], d[1].name) for d in diff) == sorted(
        [("add_table", t) for t in T4A_TABLES | T5A_TABLES | T6A_TABLES]
        + [("add_index", i) for i in T4A_INDEXES | T5A_INDEXES | T6A_INDEXES]
        + [("add_fk", T4A_RELEASE_FK)]
    )


def test_pg_upgrade_from_empty_database_to_0006(pg_url, pg_engine):
    """Base vide -> 0006 ; Base.metadata == schéma migré (hors T4-A, T5-A et
    T6-A) ; aucun ENUM, trigger ni fonction. La tête est 0009 depuis T6-A
    (testée à part)."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T3A)
    assert _version(pg_engine) == T3A
    assert _tables(pg_engine) == REMAINING_TABLES | T2A_TABLES | T3A_TABLES | {"alembic_version"}
    _assert_metadata_matches_0006(pg_engine)
    catalog = _global_catalog(pg_engine)
    assert (catalog["enums"], catalog["triggers"], catalog["functions"]) == (0, 0, 0)


def _pg_columns(names, nullable, types) -> list:
    return [(n, types.get(n, "character varying"), "YES" if n in nullable else "NO", None) for n in names]


def test_pg_observation_evaluation_runs_exact_structure(pg_url, pg_engine):
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T3A)
    types = {n: "uuid" for (t, n) in UUID_COLUMNS if t == RUNS}
    types.update({n: "timestamp with time zone" for (t, n) in TIMESTAMPTZ_COLUMNS if t == RUNS})
    assert _catalog_columns(pg_engine, RUNS) == _pg_columns(RUN_COLUMNS, RUN_NULLABLE, types)
    assert _constraints(pg_engine, RUNS) == sorted([
        *[(f"ck_{RUNS}_{col}", "c", _pg_in_check(col, values), " ", " ")
          for col, values in RUN_VOCABULARIES.items()],
        ("observation_evaluation_runs_event_id_fkey", "f",
         "FOREIGN KEY (event_id) REFERENCES cognitive_events(id)", "a", "a"),
        ("observation_evaluation_runs_pkey", "p", "PRIMARY KEY (id)", " ", " "),
        ("observation_evaluation_runs_re_evaluates_run_id_fkey", "f",
         "FOREIGN KEY (re_evaluates_run_id) REFERENCES observation_evaluation_runs(id)", "a", "a"),
        ("uq_observation_evaluation_runs_dedup_key", "u", "UNIQUE (evaluation_dedup_key)", " ", " "),
    ])
    # pedagogical_taxonomy_release_id : aucune FK (référence logique T4).
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM pg_constraint c JOIN pg_attribute a "
            "ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.conrelid = CAST(:t AS regclass) AND a.attname = 'pedagogical_taxonomy_release_id'"
        ), {"t": f"public.{RUNS}"}).scalar_one() == 0
        indexdefs = dict(conn.execute(sa.text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"
        ), {"t": RUNS}).all())
    assert indexdefs == {
        "observation_evaluation_runs_pkey":
            "CREATE UNIQUE INDEX observation_evaluation_runs_pkey ON public.observation_evaluation_runs "
            "USING btree (id)",
        "uq_observation_evaluation_runs_dedup_key":
            "CREATE UNIQUE INDEX uq_observation_evaluation_runs_dedup_key ON public.observation_evaluation_runs "
            "USING btree (evaluation_dedup_key)",
        ACTIVE_INDEX:
            f"CREATE UNIQUE INDEX {ACTIVE_INDEX} ON public.observation_evaluation_runs USING btree (event_id) "
            "WHERE ((interpretation_status)::text = 'active'::text)",
    }


def test_pg_pedagogical_observations_exact_structure(pg_url, pg_engine):
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T3A)
    types = {n: "uuid" for (t, n) in UUID_COLUMNS if t == OBS}
    types.update({n: "timestamp with time zone" for (t, n) in TIMESTAMPTZ_COLUMNS if t == OBS})
    types.update({n: "jsonb" for (_, n) in JSONB_COLUMNS})
    types.update({n: "text" for (_, n) in TEXT_COLUMNS})
    types["ordinal"] = "smallint"
    assert _catalog_columns(pg_engine, OBS) == _pg_columns(OBS_COLUMNS, OBS_NULLABLE, types)
    assert _constraints(pg_engine, OBS) == sorted([
        *[(f"ck_{OBS}_{col}", "c", _pg_in_check(col, values), " ", " ")
          for col, values in OBS_VOCABULARIES.items()],
        (POLARITY_CHECK, "c",
         "CHECK (((((polarity)::text = 'supportive'::text) AND (local_stage IS NOT NULL) "
         "AND (contradiction_scope IS NULL)) OR (((polarity)::text = 'contradictory'::text) "
         "AND (local_stage IS NULL) AND (contradiction_scope IS NOT NULL))))", " ", " "),
        (INTEGRITY_CHECK, "c",
         "CHECK (((((integrity_status)::text = 'valid'::text) AND (invalidated_at IS NULL) "
         "AND (invalidation_reason IS NULL)) OR (((integrity_status)::text = 'invalidated'::text) "
         "AND (invalidated_at IS NOT NULL) AND (invalidation_reason IS NOT NULL))))", " ", " "),
        ("pedagogical_observations_evaluation_run_id_fkey", "f",
         "FOREIGN KEY (evaluation_run_id) REFERENCES observation_evaluation_runs(id)", "a", "a"),
        ("pedagogical_observations_pkey", "p", "PRIMARY KEY (id)", " ", " "),
        ("uq_pedagogical_observations_run_ordinal", "u", "UNIQUE (evaluation_run_id, ordinal)", " ", " "),
    ])
    assert sorted(_indexes(pg_engine, OBS)) == [
        "pedagogical_observations_pkey", "uq_pedagogical_observations_run_ordinal",
    ]
    with pg_engine.connect() as conn:
        # Aucune FK de la base n'est en cascade / set null / set default.
        assert conn.execute(sa.text(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
            "AND (confdeltype <> 'a' OR confupdtype <> 'a')"
        )).scalar_one() == 0


# --- G. sémantique des runs -------------------------------------------------

def test_pg_run_states_and_zero_observation_completed_run(conn):
    """Jamais évalué = aucun run ; running ; completed sans observation
    (valide : aucune fausse observation) ; completed avec observations ;
    failed avec failure_code."""
    never, running, empty, full, failed = (_event(conn) for _ in range(5))
    _run(conn, running, execution_status="running", completed_at=None)
    empty_run = _run(conn, empty, execution_status="completed", interpretation_status="active",
                     output_fingerprint="sha256:out")
    full_run = _run(conn, full, execution_status="completed", interpretation_status="active")
    _obs(conn, full_run, 1)
    _obs(conn, full_run, 2, competency_code="C3", polarity="contradictory", local_stage=None,
         contradiction_scope="application", error_type="procedural", evidence_strength="strong")
    _run(conn, failed, execution_status="failed", completed_at=None, failure_code="evaluator_timeout")

    assert _count(conn, RUNS, "event_id = :e", {"e": never}) == 0
    assert _count(conn, OBS, "evaluation_run_id = :r", {"r": empty_run}) == 0
    assert _count(conn, OBS, "evaluation_run_id = :r", {"r": full_run}) == 2
    assert conn.execute(sa.text(
        "SELECT execution_status, failure_code FROM observation_evaluation_runs WHERE event_id = :e"
    ), {"e": failed}).one() == ("failed", "evaluator_timeout")


def test_pg_extensible_and_optional_run_columns(conn):
    """trigger est extensible ; model_id, prompt_spec_version,
    output_fingerprint, re_evaluates_run_id et
    pedagogical_taxonomy_release_id sont facultatifs. Depuis T4-A (0007),
    ce dernier doit désigner une release existante (FK testée dans
    tests/test_migration_0007_pedagogical_taxonomy.py)."""
    event_id = _event(conn)
    release_id = uuid.uuid4()
    conn.execute(sa.text(
        "INSERT INTO pedagogical_taxonomy_releases (id, version_key, status, spec_fingerprint, created_at) "
        "VALUES (:id, 'v-test', 'candidate', 'sha256:spec', now())"
    ), {"id": release_id})
    _run(conn, event_id, trigger="un_declencheur_futur_quelconque", model_id="claude-x",
         prompt_spec_version="prompt-3", output_fingerprint="sha256:out",
         pedagogical_taxonomy_release_id=release_id)
    _run(conn, event_id)
    assert _count(conn, RUNS) == 2


@pytest.mark.parametrize("column", [c for c in RUN_COLUMNS if c not in RUN_NULLABLE and c != "id"])
def test_pg_run_required_columns_are_not_null(conn, column):
    params = {"event_id": _event(conn), column: None}
    _refused(conn, f'null value in column "{column}"', lambda: _run(conn, **params))


@pytest.mark.parametrize("column", sorted(RUN_VOCABULARIES))
def test_pg_run_vocabularies(conn, column):
    """B. Toutes les valeurs autorisées passent ; toute autre est refusée
    par le CHECK nommé."""
    event_id = _event(conn)
    for value in RUN_VOCABULARIES[column]:
        _run(conn, event_id, **{column: value})
    for value in ("", "ACTIVE", "Completed", "pending", "done", "mixed"):
        if value in RUN_VOCABULARIES[column]:
            continue
        _refused(conn, f"ck_{RUNS}_{column}", lambda: _run(conn, event_id, **{column: value}))


# --- H. un seul run active par événement ------------------------------------

def test_pg_only_one_active_run_per_event(conn):
    first_event, second_event = _event(conn), _event(conn)
    active = _run(conn, first_event, interpretation_status="active")
    _refused(conn, ACTIVE_INDEX, lambda: _run(conn, first_event, interpretation_status="active"))
    # Un active par événement : un autre événement peut avoir le sien.
    _run(conn, second_event, interpretation_status="active")
    # candidate / superseded / obsolete : multiples pour un même événement.
    for status in ("candidate", "candidate", "superseded", "superseded", "obsolete", "obsolete"):
        _run(conn, first_event, interpretation_status=status)
    assert _count(conn, RUNS, "event_id = :e", {"e": first_event}) == 7
    # Réactiver par UPDATE est aussi refusé.
    candidate = conn.execute(sa.text(
        "SELECT id FROM observation_evaluation_runs WHERE event_id = :e AND interpretation_status = 'candidate' "
        "LIMIT 1"
    ), {"e": first_event}).scalar_one()
    promote = sa.text("UPDATE observation_evaluation_runs SET interpretation_status = 'active' WHERE id = :id")
    _refused(conn, ACTIVE_INDEX, lambda: conn.execute(promote, {"id": candidate}))
    # Supersession : l'ancien active devient superseded, puis le nouveau
    # devient active, dans la même transaction.
    conn.execute(sa.text(
        "UPDATE observation_evaluation_runs SET interpretation_status = 'superseded' WHERE id = :id"
    ), {"id": active})
    conn.execute(promote, {"id": candidate})
    assert conn.execute(sa.text(
        "SELECT id FROM observation_evaluation_runs WHERE event_id = :e AND interpretation_status = 'active'"
    ), {"e": first_event}).scalars().all() == [candidate]


def _backend_pid(connection) -> int:
    return connection.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()


def _wait_until_blocked_by(engine, waiting_pid, holder_pid, timeout=10.0) -> None:
    deadline = time.monotonic() + timeout
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as watcher:
        while time.monotonic() < deadline:
            if watcher.execute(sa.text("SELECT :h = ANY(pg_blocking_pids(:w))"),
                               {"h": holder_pid, "w": waiting_pid}).scalar_one():
                return
            time.sleep(0.005)
    pytest.fail(f"la connexion {waiting_pid} n'a jamais attendu la connexion {holder_pid}")


def test_pg_concurrent_active_runs_for_same_event_are_serialized(pg_url, pg_engine):
    """L'invariant tient sous concurrence : deux transactions qui insèrent
    chacune un run active pour le même événement ; la seconde attend la
    première (index unique) puis échoue quand celle-ci commite."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with pg_engine.begin() as setup:
        event_id = _event(setup)
    try:
        with pg_engine.connect() as holder, pg_engine.connect() as waiter:
            holder_pid, waiter_pid = _backend_pid(holder), _backend_pid(waiter)
            holder.commit()
            waiter.commit()
            holder.begin()
            _run(holder, event_id, interpretation_status="active", evaluation_dedup_key="first")
            outcome = {}

            def work():
                try:
                    with waiter.begin():
                        _run(waiter, event_id, interpretation_status="active", evaluation_dedup_key="second")
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
            assert ACTIVE_INDEX in str(outcome["error"])
        with pg_engine.connect() as check:
            assert check.execute(sa.text(
                "SELECT evaluation_dedup_key FROM observation_evaluation_runs WHERE event_id = :e"
            ), {"e": event_id}).scalars().all() == ["first"]
    finally:
        with pg_engine.begin() as cleanup:
            cleanup.execute(sa.text("DELETE FROM observation_evaluation_runs"))
            cleanup.execute(sa.text("DELETE FROM cognitive_events"))


# --- I. déduplication --------------------------------------------------------

def test_pg_evaluation_dedup_key_is_unique(conn):
    first, second = _event(conn), _event(conn)
    _run(conn, first, evaluation_dedup_key="event:v1")
    _refused(conn, "uq_observation_evaluation_runs_dedup_key",
             lambda: _run(conn, first, evaluation_dedup_key="event:v1"))
    _refused(conn, "uq_observation_evaluation_runs_dedup_key",
             lambda: _run(conn, second, evaluation_dedup_key="event:v1"))
    _run(conn, first, evaluation_dedup_key="event:v2")


# --- FK -----------------------------------------------------------------------

def test_pg_run_foreign_keys_and_no_cascade(conn):
    event_id = _event(conn)
    _refused(conn, "observation_evaluation_runs_event_id_fkey", lambda: _run(conn, uuid.uuid4()))
    first = _run(conn, event_id)
    # Un premier run n'en réévalue aucun ; une réévaluation pointe vers un run existant.
    second = _run(conn, event_id, re_evaluates_run_id=first)
    _refused(conn, "observation_evaluation_runs_re_evaluates_run_id_fkey",
             lambda: _run(conn, event_id, re_evaluates_run_id=uuid.uuid4()))
    _obs(conn, second, 1)
    _refused(conn, "observation_evaluation_runs_event_id_fkey",
             lambda: conn.execute(sa.text("DELETE FROM cognitive_events WHERE id = :id"), {"id": event_id}))
    _refused(conn, "observation_evaluation_runs_re_evaluates_run_id_fkey",
             lambda: conn.execute(sa.text("DELETE FROM observation_evaluation_runs WHERE id = :id"), {"id": first}))
    _refused(conn, "pedagogical_observations_evaluation_run_id_fkey",
             lambda: conn.execute(sa.text("DELETE FROM observation_evaluation_runs WHERE id = :id"), {"id": second}))
    _refused(conn, "pedagogical_observations_evaluation_run_id_fkey", lambda: _obs(conn, uuid.uuid4(), 1))
    assert (_count(conn, RUNS), _count(conn, OBS)) == (2, 1)


# --- B. vocabulaires des observations ----------------------------------------

def _valid_for(column, value) -> dict:
    """Surcharges rendant la ligne valide pour `column = value` vis-à-vis
    des contraintes croisées."""
    if column == "polarity" and value == "contradictory":
        return {"local_stage": None, "contradiction_scope": "undetermined"}
    if column == "contradiction_scope":
        return {"polarity": "contradictory", "local_stage": None}
    if column == "integrity_status" and value == "invalidated":
        return {"invalidated_at": NOW, "invalidation_reason": "source_data_error"}
    return {}


@pytest.mark.parametrize("column", sorted(OBS_VOCABULARIES))
def test_pg_observation_vocabularies(conn, column):
    """B. Toutes les valeurs autorisées passent ; toute autre est refusée
    par le CHECK nommé de la colonne (vérifié seul, contraintes croisées
    retirées dans un savepoint annulé)."""
    run_id = _run(conn, _event(conn))
    ordinals = iter(range(1, 100))
    for value in OBS_VOCABULARIES[column]:
        _obs(conn, run_id, next(ordinals), **{column: value}, **_valid_for(column, value))
    for value in ("", "c7", "C0", "C13", "mastery", "mixed", "superseded", "obsolete", "strong ",
                  "NONE", "Application", "other"):
        if value in OBS_VOCABULARIES[column]:
            continue
        _refused(conn, f'"ck_{OBS}_{column}"',
                 lambda: _obs(conn, run_id, next(ordinals), **{column: value}),
                 isolate=(POLARITY_CHECK, INTEGRITY_CHECK))


@pytest.mark.parametrize("column", [c for c in OBS_COLUMNS if c not in OBS_NULLABLE and c != "id"])
def test_pg_observation_required_columns_are_not_null(conn, column):
    params = {"run_id": _run(conn, _event(conn)), "ordinal": 1}
    params[{"evaluation_run_id": "run_id"}.get(column, column)] = None
    _refused(conn, f'null value in column "{column}"', lambda: _obs(conn, **params))


def test_pg_extensible_task_kind_and_free_text(conn):
    """task_kind extensible et facultatif ; observation_text jamais tronqué ;
    JSONB conservés tels quels."""
    run_id = _run(conn, _event(conn))
    long_text = "Observation détaillée. " * 2000
    _obs(conn, run_id, 1, task_kind="une_tache_future", observation_text=long_text,
         contributive_user_actions='[{"work_index": 1}, {"work_index": 2}]')
    _obs(conn, run_id, 2, task_kind=None)
    row = conn.execute(sa.text(
        "SELECT observation_text, primary_user_action, contributive_user_actions, "
        "source_contribution_refs, residual_cognitive_work FROM pedagogical_observations WHERE ordinal = 1"
    )).one()
    assert row == (
        long_text,
        {"work_index": 0, "kind": "explanation"},
        [{"work_index": 1}, {"work_index": 2}],
        [{"work_index": 0}],
        {"description": "relier le ROE aux capitaux propres"},
    )


# --- C / D. polarity, local_stage, contradiction_scope, mastery --------------

def test_pg_polarity_stage_scope_valid_combinations(conn):
    run_id = _run(conn, _event(conn))
    _obs(conn, run_id, 1, polarity="supportive", local_stage="discovery", contradiction_scope=None)
    # supportive + none est valide (ne signifie jamais Discovery à lui seul).
    _obs(conn, run_id, 2, polarity="supportive", local_stage="none", contradiction_scope=None)
    _obs(conn, run_id, 3, polarity="contradictory", local_stage=None, contradiction_scope="comprehension")
    # Force de preuve != état : une contradiction peut être strong.
    _obs(conn, run_id, 4, polarity="contradictory", local_stage=None, contradiction_scope="application",
         evidence_strength="strong", error_type="conceptual")
    for ordinal, stage in enumerate(OBS_VOCABULARIES["local_stage"], start=10):
        _obs(conn, run_id, ordinal, polarity="supportive", local_stage=stage)
    assert _count(conn, OBS) == 8


@pytest.mark.parametrize("overrides", [
    {"polarity": "supportive", "local_stage": None, "contradiction_scope": None},
    {"polarity": "supportive", "local_stage": "application", "contradiction_scope": "application"},
    {"polarity": "supportive", "local_stage": None, "contradiction_scope": "recognition"},
    {"polarity": "contradictory", "local_stage": "application", "contradiction_scope": "application"},
    {"polarity": "contradictory", "local_stage": None, "contradiction_scope": None},
    {"polarity": "contradictory", "local_stage": "none", "contradiction_scope": None},
])
def test_pg_polarity_stage_scope_invalid_combinations(conn, overrides):
    run_id = _run(conn, _event(conn))
    _refused(conn, POLARITY_CHECK, lambda: _obs(conn, run_id, 1, **overrides))


def test_pg_mastery_is_never_a_local_stage(conn):
    """D. mastery refusé par PostgreSQL, quelle que soit la casse."""
    run_id = _run(conn, _event(conn))
    for value in ("mastery", "Mastery", "MASTERY"):
        _refused(conn, "ck_pedagogical_observations_local_stage",
                 lambda: _obs(conn, run_id, 1, polarity="supportive", local_stage=value))
    assert _count(conn, OBS) == 0


# --- E / F. atomicité et ordinal ---------------------------------------------

def test_pg_several_observations_on_same_competency_in_one_run(conn):
    """E. Plusieurs observations C7 (de forces et polarités différentes)
    dans un même run, dès lors que l'ordinal diffère."""
    run_id = _run(conn, _event(conn))
    _obs(conn, run_id, 1, competency_code="C7", evidence_strength="strong")
    _obs(conn, run_id, 2, competency_code="C7", evidence_strength="weak", observation_role="secondary")
    _obs(conn, run_id, 3, competency_code="C7", polarity="contradictory", local_stage=None,
         contradiction_scope="recognition", error_type="factual_premise")
    _obs(conn, run_id, 4, competency_code="C10")
    assert conn.execute(sa.text(
        "SELECT ordinal, competency_code FROM pedagogical_observations "
        "WHERE evaluation_run_id = :r ORDER BY ordinal"
    ), {"r": run_id}).all() == [(1, "C7"), (2, "C7"), (3, "C7"), (4, "C10")]


def test_pg_ordinal_is_unique_per_run_only(conn):
    """F. Même run + même ordinal refusé ; même ordinal sur deux runs
    autorisé ; aucune séquence sans trou imposée en base."""
    event_id = _event(conn)
    first, second = _run(conn, event_id), _run(conn, event_id)
    _obs(conn, first, 1)
    _refused(conn, "uq_pedagogical_observations_run_ordinal",
             lambda: _obs(conn, first, 1, competency_code="C2"))
    _obs(conn, second, 1)
    _obs(conn, first, 5)
    assert _count(conn, OBS) == 3


# --- J. intégrité --------------------------------------------------------------

def test_pg_integrity_invalidation_consistency(conn):
    run_id = _run(conn, _event(conn))
    _obs(conn, run_id, 1, integrity_status="valid")
    _obs(conn, run_id, 2, integrity_status="invalidated", invalidated_at=NOW,
         invalidation_reason="stimulus_snapshot_corrompu")
    for ordinal, overrides in enumerate([
        {"integrity_status": "valid", "invalidated_at": NOW},
        {"integrity_status": "valid", "invalidation_reason": "raison"},
        {"integrity_status": "valid", "invalidated_at": NOW, "invalidation_reason": "raison"},
        {"integrity_status": "invalidated"},
        {"integrity_status": "invalidated", "invalidated_at": NOW},
        {"integrity_status": "invalidated", "invalidation_reason": "raison"},
    ], start=10):
        _refused(conn, INTEGRITY_CHECK, lambda: _obs(conn, run_id, ordinal, **overrides))
    # Transition valid -> invalidated en une seule mise à jour cohérente.
    conn.execute(sa.text(
        "UPDATE pedagogical_observations SET integrity_status = 'invalidated', invalidated_at = now(), "
        "invalidation_reason = 'erreur de donnée source' WHERE ordinal = 1"
    ))
    # Effacer la raison d'une observation invalidée est refusé.
    _refused(conn, INTEGRITY_CHECK, lambda: conn.execute(sa.text(
        "UPDATE pedagogical_observations SET invalidation_reason = NULL WHERE ordinal = 2"
    )))
    assert _count(conn, OBS, "integrity_status = 'invalidated'") == 2


def test_pg_observation_of_superseded_run_stays_valid(conn):
    """La supersession appartient au run : une observation d'un run
    superseded reste intrinsèquement valid."""
    run_id = _run(conn, _event(conn), interpretation_status="superseded")
    _obs(conn, run_id, 1, integrity_status="valid")
    assert _count(conn, OBS, "integrity_status = 'valid'") == 1


# --- ORM ------------------------------------------------------------------------

def test_pg_orm_insertion_uses_application_defaults(pg_url, pg_engine):
    """Insertion via l'ORM : UUID et timestamps UTC aware générés côté
    application ; un None Python sur un JSONB devient NULL SQL (refusé)."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with Session(pg_engine) as session:
        with session.begin():
            event = CognitiveEvent(user_id=USER, event_origin="coach", task_kind="interpret_metric",
                                   status="finalized", closed_at=NOW)
            session.add(event)
            session.flush()
            run = ObservationEvaluationRun(
                event_id=event.id, execution_status="completed", interpretation_status="active",
                trigger="event_finalized", evaluation_dedup_key="k", normalization_version="n",
                local_stage_version="s", capability_mapping_version="m", evaluation_schema_version="e",
                evaluator_version="v", input_fingerprint="in", completed_at=NOW,
            )
            session.add(run)
            session.flush()
            observation = PedagogicalObservation(
                evaluation_run_id=run.id, ordinal=1, competency_code="C7", observation_role="primary",
                primary_user_action={"work_index": 0}, contributive_user_actions=[],
                elicitation_mode="spontaneous", support_level="hinted", source_contribution_refs=[],
                residual_cognitive_work={}, polarity="supportive", evidence_strength="weak",
                local_stage="none", observation_text="t", capability_localization="localized",
                integrity_status="valid",
            )
            session.add(observation)
            session.flush()
            session.refresh(run)
            session.refresh(observation)

            assert isinstance(run.id, uuid.UUID) and isinstance(observation.id, uuid.UUID)
            assert run.re_evaluates_run_id is None and run.pedagogical_taxonomy_release_id is None
            for value in (run.started_at, run.created_at, observation.created_at):
                assert value.tzinfo is not None and value.utcoffset().total_seconds() == 0
            assert observation.contributive_user_actions == [] and observation.residual_cognitive_work == {}

            with pytest.raises(sa.exc.IntegrityError, match='null value in column "residual_cognitive_work"'):
                with session.begin_nested():
                    session.add(PedagogicalObservation(
                        evaluation_run_id=run.id, ordinal=2, competency_code="C7",
                        observation_role="primary", primary_user_action={},
                        contributive_user_actions=[], elicitation_mode="prompted", support_level="none",
                        source_contribution_refs=[], residual_cognitive_work=None, polarity="supportive",
                        evidence_strength="weak", local_stage="none", observation_text="t",
                        capability_localization="localized", integrity_status="valid",
                    ))
            session.rollback()
    with pg_engine.connect() as c:
        assert _count(c, RUNS) == 0 and _count(c, OBS) == 0


# --- migration ---------------------------------------------------------------------

def _seed_t2(engine) -> None:
    """Un événement finalized et une aide réellement visible (schéma 0005)."""
    with engine.begin() as conn:
        event_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        conn.execute(sa.text(
            "INSERT INTO cognitive_events (id, user_id, event_origin, task_kind, status, "
            "stimulus_snapshot, user_work_snapshot, started_at, updated_at, closed_at) VALUES "
            "(:id, 'u', 'education', 'explain_concept', 'finalized', CAST('{\"q\": 1}' AS JSONB), "
            "CAST('[{\"text\": \"r\"}]' AS JSONB), '2026-09-01+00', '2026-09-01+00', '2026-09-01+00')"
        ), {"id": event_id})
        conn.execute(sa.text(
            "INSERT INTO support_traces (id, cognitive_event_id, sequence_no, support_kind, "
            "support_payload, created_at) VALUES (:id, :e, 1, 'hint', CAST('{\"t\": 1}' AS JSONB), "
            "'2026-09-01+00')"
        ), {"id": uuid.UUID("33333333-3333-3333-3333-333333333333"), "e": event_id})


def test_pg_upgrade_0005_to_0006_preserves_everything_then_downgrade(pg_url, pg_engine):
    """0005 -> 0006 conserve schéma et données de toutes les tables
    existantes (dont T2) ; le downgrade retire uniquement les deux tables et
    l'index et restaure exactement 0005 ; le ré-upgrade est identique."""
    existing = REMAINING_TABLES | T2A_TABLES | {"alembic_version"}
    data_tables = REMAINING_TABLES | T2A_TABLES
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T1C2)
    _seed_remaining_tables(pg_engine)
    _run_alembic(pg_url, "upgrade", T2A)
    _seed_t2(pg_engine)
    schema_0005 = _snapshot(pg_engine, existing)
    catalog_0005 = _catalog(pg_engine, existing)
    global_0005 = _global_catalog(pg_engine)
    data_0005 = _data(pg_engine, data_tables)
    assert all(data_0005[t] for t in data_tables)

    # --- upgrade 0005 -> 0006 --------------------------------------------
    _run_alembic(pg_url, "upgrade", T3A)
    assert _version(pg_engine) == T3A
    assert _tables(pg_engine) == existing | T3A_TABLES
    assert _snapshot(pg_engine, existing) == schema_0005
    assert _catalog(pg_engine, existing) == catalog_0005
    assert _data(pg_engine, data_tables) == data_0005
    # Aucun backfill ni seed.
    assert _data(pg_engine, T3A_TABLES) == {OBS: [], RUNS: []}
    # Aucun ENUM, séquence, trigger ni fonction ajoutés.
    assert _global_catalog(pg_engine) == global_0005
    schema_0006 = _snapshot(pg_engine, T3A_TABLES)
    catalog_0006 = _catalog(pg_engine, T3A_TABLES)
    _assert_metadata_matches_0006(pg_engine)

    # --- downgrade 0006 -> 0005 ------------------------------------------
    _run_alembic(pg_url, "downgrade", T2A)
    assert _version(pg_engine) == T2A
    assert _tables(pg_engine) == existing
    with pg_engine.connect() as conn:
        for name in (RUNS, OBS, ACTIVE_INDEX):
            assert conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": f"public.{name}"}).scalar_one() is None
    assert _snapshot(pg_engine, existing) == schema_0005
    assert _catalog(pg_engine, existing) == catalog_0005
    assert _global_catalog(pg_engine) == global_0005
    assert _data(pg_engine, data_tables) == data_0005

    # --- ré-upgrade 0005 -> 0006 -----------------------------------------
    _run_alembic(pg_url, "upgrade", T3A)
    assert _version(pg_engine) == T3A
    assert _snapshot(pg_engine, existing) == schema_0005
    assert _snapshot(pg_engine, T3A_TABLES) == schema_0006
    assert _catalog(pg_engine, T3A_TABLES) == catalog_0006
    assert _data(pg_engine, data_tables) == data_0005
    _assert_metadata_matches_0006(pg_engine)
