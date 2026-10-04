"""Tests de T6-A : migration 0009_competency_inference_state + modèles
CompetencyInferenceRun / CompetencyStageClaim / CompetencyInferenceTension /
CompetencyInferenceTensionCapability / CompetencyInferenceBasisRef /
UserCompetencyState.

Même organisation que les tests des migrations 0002 à 0008 (dont on
réutilise les helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, intégrité de 0001
   à 0008, SQL PostgreSQL généré en mode offline, métadonnées des modèles
   (colonnes, types, PK, FK, CHECK, UNIQUE, index, défauts), anti-dérive
   (aucun score, confiance numérique, stade numérique, niveau global,
   active_validation_plan, fraîcheur, seed, route, service T6, LLM).

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t6a_test \\
           python -m pytest tests/test_migration_0009_competency_inference_state.py

Doctrine : le Niveau 6 est une INTERPRÉTATION VERSIONNÉE dérivée du dossier
longitudinal T5 ; il ne crée aucune preuve. Run, stage claim, tension,
basis ref et cache user_competency_states ne deviennent jamais une preuve
utilisateur. T6-A = schéma + modèles seulement : aucun service, aucune
route, aucun moteur, aucun LLM. Les invariants qui traversent plusieurs
tables (run T5 parent, predecessor, complétude des quatre claims,
transitions, appartenance des sources au dossier, périmètres, écriture du
cache) relèvent de T6-B / T6-C : des tests ci-dessous documentent
explicitement que la base ne les impose PAS encore.
"""
import ast
import hashlib
import re
import threading
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from core.db import Base
from core.models import (
    CompetencyInferenceBasisRef,
    CompetencyInferenceRun,
    CompetencyInferenceTension,
    CompetencyInferenceTensionCapability,
    CompetencyStageClaim,
    User,
    UserCompetencyState,
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
    R1B_TABLES,
    R1C1_TABLES,
    REMAINING_TABLES,
    T1B1_SHA256,
    T1C2,
    T3A_TABLES,
    T4A_TABLES,
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
)
from tests.test_migration_0005_cognitive_support_traces import (
    OTHER_USER,
    T1C2_SHA256,
    T2A,
    T2A_TABLES,
    USER,
    _catalog,
    _global_catalog,
    _upgrade_head_with_users,
)
from tests.test_migration_0006_observation_layer import (
    NOW,
    OBS,
    T2A_SHA256,
    T3A,
    _backend_pid,
    _count,
    _in_sql,
    _pg_in_check,
    _wait_until_blocked_by,
)
from tests.test_migration_0007_pedagogical_taxonomy import (
    COMPETENCY_CODES,
    T3A_SHA256,
    T4A,
    _assert_absent,
    _definition,
    _indexdefs,
    _membership,
    _observation,
    _release,
)
from tests.test_migration_0008_longitudinal_relations import (
    T4A_SHA256,
    T5A,
    _assessment,
    _contradictory,
    _dependency,
    _input,
    _revalidation,
    _scope,
    _transfer,
    _upgrade_to_0007_with_data,
)

T6A = "0009_competency_inference_state"
R1B = "0010_r1b_event_idempotence"
R1C1 = "0011_assistant_deliveries"
# sha256 de alembic/versions/0008_longitudinal_relations.py tel que mergé sur
# main (ec33c34, après T5-C ; 0008 introduite par T5-A, PR #184).
T5A_SHA256 = "98e0a1a546cdc46d070971594aa826737c23c0cbafbff23860336bfa9d6cd5cd"

RUNS = "competency_inference_runs"
CLAIMS = "competency_stage_claims"
TENSIONS = "competency_inference_tensions"
TENSION_CAPS = "competency_inference_tension_capabilities"
REFS = "competency_inference_basis_refs"
STATES = "user_competency_states"
TABLES_IN_ORDER = (RUNS, CLAIMS, TENSIONS, TENSION_CAPS, REFS, STATES)
assert set(TABLES_IN_ORDER) == T6A_TABLES and len(TABLES_IN_ORDER) == 6
T6A_MODELS = (CompetencyInferenceRun, CompetencyStageClaim, CompetencyInferenceTension,
              CompetencyInferenceTensionCapability, CompetencyInferenceBasisRef, UserCompetencyState)
ASSESSMENTS = "longitudinal_assessment_runs"
DEPS = "observation_dependencies"
TRANSFERS = "observation_transfers"
REVALS = "observation_revalidations"
MEMBERSHIPS = "capability_taxonomy_memberships"

ONE_ACTIVE_INDEX = "uq_competency_inference_runs_one_active_user_competency"
DEDUP_UNIQUE = "uq_competency_inference_runs_dedup_key"
CLAIM_UNIQUE = "uq_competency_stage_claims_run_stage"
STATE_ACTIVE_UNIQUE = "uq_user_competency_states_active_inference_run_id"
# Nom explicite : le nom automatique PostgreSQL (<table>_<colonne>_fkey)
# ferait 71 caractères.
TENSION_CAP_MEMBERSHIP_FK = "competency_inference_tension_caps_membership_id_fkey"
EXPLICIT_FK_NAMES = {TENSION_CAP_MEMBERSHIP_FK}

EXECUTION_STATUSES = ("running", "completed", "failed")
INTERPRETATION_STATUSES = ("candidate", "active", "superseded", "obsolete")
# Ordre conceptuel (utilisé en logique métier T6-C, jamais stocké en nombre).
CURRENT_STAGES = ("non_etabli", "discovery", "comprehension", "application", "mastery")
# non_etabli n'est PAS une stage claim.
CLAIM_STAGES = ("discovery", "comprehension", "application", "mastery")
TRANSITIONS = ("maintained", "upgraded", "revised_down")
TRANSITION_CAUSES = ("new_user_evidence", "evidence_integrity_change", "pedagogical_reinterpretation")
TENSION_STATES = ("none", "open")
BASIS_STATUSES = ("established", "not_established")
BASIS_MODES = ("direct", "implied_by_higher_claim", "none")
TENSION_SCOPE_MODES = ("whole_competency", "localized", "competency_only")
REVISION_STATUSES = ("unresolved", "revalidation_needed")
REF_ROLES = ("positive_basis", "confidence", "tension", "transition", "validation", "mastery")
CONFIDENCE_DIMENSIONS = ("diagnosticity", "coverage", "independence", "consistency", "temporal_validation")
SOURCE_KINDS = ("observation", "dependency", "transfer", "revalidation")
# source_kind -> colonne source.
SOURCE_COLUMNS = {"observation": "source_observation_id", "dependency": "source_dependency_id",
                  "transfer": "source_transfer_id", "revalidation": "source_revalidation_id"}

COLUMNS = {
    RUNS: ["id", "user_id", "competency_code", "longitudinal_assessment_run_id", "predecessor_inference_run_id",
           "execution_status", "interpretation_status", "trigger", "previous_stage", "current_stage", "transition",
           "transition_cause", "tension_state", "unresolved_revision_context", "validation_needs",
           "state_decision_summary", "positive_basis_version", "confidence_profile_version",
           "state_decision_version", "validation_version", "inference_schema_version", "evaluator_version",
           "model_id", "prompt_spec_version", "input_fingerprint", "output_fingerprint", "inference_dedup_key",
           "started_at", "completed_at", "created_at", "failure_code"],
    CLAIMS: ["id", "inference_run_id", "stage", "positive_basis_status", "basis_mode", "basis_summary",
             "scope_summary", "confidence_profile", "mastery_assessment", "created_at"],
    TENSIONS: ["id", "inference_run_id", "fragilized_stage", "scope_mode", "summary", "revision_status",
               "created_at"],
    TENSION_CAPS: ["tension_id", "capability_membership_id"],
    REFS: ["id", "inference_run_id", "stage_claim_id", "tension_id", "ref_role", "confidence_dimension",
           "source_kind", "source_observation_id", "source_dependency_id", "source_transfer_id",
           "source_revalidation_id", "created_at"],
    STATES: ["user_id", "competency_code", "active_inference_run_id", "current_stage", "tension_state",
             "state_generation", "updated_at"],
}
NULLABLE = {
    (RUNS, "predecessor_inference_run_id"), (RUNS, "previous_stage"), (RUNS, "current_stage"),
    (RUNS, "transition"), (RUNS, "transition_cause"), (RUNS, "tension_state"),
    (RUNS, "unresolved_revision_context"), (RUNS, "validation_needs"), (RUNS, "state_decision_summary"),
    (RUNS, "model_id"), (RUNS, "prompt_spec_version"), (RUNS, "output_fingerprint"), (RUNS, "completed_at"),
    (RUNS, "failure_code"),
    (CLAIMS, "basis_summary"), (CLAIMS, "scope_summary"), (CLAIMS, "confidence_profile"),
    (CLAIMS, "mastery_assessment"),
    (REFS, "stage_claim_id"), (REFS, "tension_id"), (REFS, "confidence_dimension"),
    (REFS, "source_observation_id"), (REFS, "source_dependency_id"), (REFS, "source_transfer_id"),
    (REFS, "source_revalidation_id"),
}
# Sorties d'un run qui peuvent rester NULL pendant running (T6-B imposera
# la complétion) : non_etabli n'est jamais un placeholder.
RUN_OUTPUTS = ("previous_stage", "current_stage", "transition", "transition_cause", "tension_state",
               "unresolved_revision_context", "validation_needs", "state_decision_summary", "output_fingerprint")
UUID_COLUMNS = {
    (RUNS, "id"), (RUNS, "longitudinal_assessment_run_id"), (RUNS, "predecessor_inference_run_id"),
    (CLAIMS, "id"), (CLAIMS, "inference_run_id"),
    (TENSIONS, "id"), (TENSIONS, "inference_run_id"),
    (TENSION_CAPS, "tension_id"), (TENSION_CAPS, "capability_membership_id"),
    (REFS, "id"), (REFS, "inference_run_id"), (REFS, "stage_claim_id"), (REFS, "tension_id"),
    (REFS, "source_observation_id"), (REFS, "source_dependency_id"), (REFS, "source_transfer_id"),
    (REFS, "source_revalidation_id"),
    (STATES, "active_inference_run_id"),
}
TIMESTAMPTZ_COLUMNS = {
    (RUNS, "started_at"), (RUNS, "completed_at"), (RUNS, "created_at"), (CLAIMS, "created_at"),
    (TENSIONS, "created_at"), (REFS, "created_at"), (STATES, "updated_at"),
}
JSONB_COLUMNS = {(RUNS, "unresolved_revision_context"), (RUNS, "validation_needs"),
                 (CLAIMS, "confidence_profile"), (CLAIMS, "mastery_assessment")}
TEXT_COLUMNS = {(RUNS, "state_decision_summary"), (CLAIMS, "basis_summary"), (CLAIMS, "scope_summary"),
                (TENSIONS, "summary")}
BIGINT_COLUMNS = {(STATES, "state_generation")}
PRIMARY_KEYS = {
    RUNS: ["id"], CLAIMS: ["id"], TENSIONS: ["id"], TENSION_CAPS: ["tension_id", "capability_membership_id"],
    REFS: ["id"], STATES: ["user_id", "competency_code"],
}
# (colonne, cible, nom PostgreSQL) — nom automatique sauf l'explicite.
FOREIGN_KEYS = {
    RUNS: [
        ("longitudinal_assessment_run_id", f"{ASSESSMENTS}.id",
         "competency_inference_runs_longitudinal_assessment_run_id_fkey"),
        ("predecessor_inference_run_id", f"{RUNS}.id", "competency_inference_runs_predecessor_inference_run_id_fkey"),
        ("user_id", "users.id", "competency_inference_runs_user_id_fkey"),
    ],
    CLAIMS: [("inference_run_id", f"{RUNS}.id", "competency_stage_claims_inference_run_id_fkey")],
    TENSIONS: [("inference_run_id", f"{RUNS}.id", "competency_inference_tensions_inference_run_id_fkey")],
    TENSION_CAPS: [
        ("capability_membership_id", f"{MEMBERSHIPS}.id", TENSION_CAP_MEMBERSHIP_FK),
        ("tension_id", f"{TENSIONS}.id", "competency_inference_tension_capabilities_tension_id_fkey"),
    ],
    REFS: [
        ("inference_run_id", f"{RUNS}.id", "competency_inference_basis_refs_inference_run_id_fkey"),
        ("source_dependency_id", f"{DEPS}.id", "competency_inference_basis_refs_source_dependency_id_fkey"),
        ("source_observation_id", f"{OBS}.id", "competency_inference_basis_refs_source_observation_id_fkey"),
        ("source_revalidation_id", f"{REVALS}.id", "competency_inference_basis_refs_source_revalidation_id_fkey"),
        ("source_transfer_id", f"{TRANSFERS}.id", "competency_inference_basis_refs_source_transfer_id_fkey"),
        ("stage_claim_id", f"{CLAIMS}.id", "competency_inference_basis_refs_stage_claim_id_fkey"),
        ("tension_id", f"{TENSIONS}.id", "competency_inference_basis_refs_tension_id_fkey"),
    ],
    STATES: [
        ("active_inference_run_id", f"{RUNS}.id", "user_competency_states_active_inference_run_id_fkey"),
        ("user_id", "users.id", "user_competency_states_user_id_fkey"),
    ],
}

NO_SELF_PREDECESSOR_CHECK = "ck_competency_inference_runs_no_self_predecessor"
BASIS_STATUS_MODE_CHECK = "ck_competency_stage_claims_basis_status_mode"
SOURCE_KIND_CHECK = "ck_competency_inference_basis_refs_source_kind"
SOURCE_XOR_CHECK = "ck_competency_inference_basis_refs_source_xor"
POSITIVE_BASIS_CHECK = "ck_competency_inference_basis_refs_positive_basis_ref"
CONFIDENCE_REF_CHECK = "ck_competency_inference_basis_refs_confidence_ref"
MASTERY_REF_CHECK = "ck_competency_inference_basis_refs_mastery_ref"
TENSION_REF_CHECK = "ck_competency_inference_basis_refs_tension_ref"
# transition / validation : refs run-level (une contrainte par rôle).
RUN_LEVEL_REF_CHECKS = {role: f"ck_competency_inference_basis_refs_{role}_ref"
                        for role in ("transition", "validation")}
REF_ROLE_CHECK = "ck_competency_inference_basis_refs_ref_role"
DIMENSION_CHECK = "ck_competency_inference_basis_refs_confidence_dimension"

NO_SELF_PREDECESSOR_SQL = "predecessor_inference_run_id IS NULL OR predecessor_inference_run_id <> id"
BASIS_STATUS_MODE_SQL = (
    "(positive_basis_status = 'not_established' AND basis_mode = 'none') "
    "OR (positive_basis_status = 'established' "
    "AND basis_mode IN ('direct', 'implied_by_higher_claim'))"
)


def _xor_branch(kind, pg=False) -> str:
    """Branche du XOR : source_kind = kind, SA colonne correspondante
    renseignée et les trois autres NULL."""
    if pg:
        parts = [f"((source_kind)::text = '{kind}'::text)"]
        parts += [f"({column} IS {'NOT ' if k == kind else ''}NULL)" for k, column in SOURCE_COLUMNS.items()]
        return "(" + " AND ".join(parts) + ")"
    parts = [f"source_kind = '{kind}'"]
    parts += [f"{column} IS {'NOT ' if k == kind else ''}NULL" for k, column in SOURCE_COLUMNS.items()]
    return "(" + " AND ".join(parts) + ")"


SOURCE_XOR_SQL = " OR ".join(_xor_branch(kind) for kind in SOURCE_KINDS)
POSITIVE_BASIS_SQL = ("ref_role <> 'positive_basis' OR (stage_claim_id IS NOT NULL AND tension_id IS NULL "
                      "AND confidence_dimension IS NULL AND source_kind = 'observation')")
CONFIDENCE_REF_SQL = ("ref_role <> 'confidence' OR (stage_claim_id IS NOT NULL AND tension_id IS NULL "
                      "AND confidence_dimension IS NOT NULL)")
MASTERY_REF_SQL = ("ref_role <> 'mastery' OR (stage_claim_id IS NOT NULL AND tension_id IS NULL "
                   "AND confidence_dimension IS NULL)")
TENSION_REF_SQL = ("ref_role <> 'tension' OR (stage_claim_id IS NULL AND tension_id IS NOT NULL "
                   "AND confidence_dimension IS NULL)")
RUN_LEVEL_REF_SQL = {role: (f"ref_role <> '{role}' OR (stage_claim_id IS NULL AND tension_id IS NULL "
                            "AND confidence_dimension IS NULL)") for role in RUN_LEVEL_REF_CHECKS}

CHECKS = {
    RUNS: {
        "ck_competency_inference_runs_competency_code": _in_sql("competency_code", COMPETENCY_CODES),
        "ck_competency_inference_runs_execution_status": _in_sql("execution_status", EXECUTION_STATUSES),
        "ck_competency_inference_runs_interpretation_status":
            _in_sql("interpretation_status", INTERPRETATION_STATUSES),
        "ck_competency_inference_runs_previous_stage": _in_sql("previous_stage", CURRENT_STAGES),
        "ck_competency_inference_runs_current_stage": _in_sql("current_stage", CURRENT_STAGES),
        "ck_competency_inference_runs_transition": _in_sql("transition", TRANSITIONS),
        "ck_competency_inference_runs_transition_cause": _in_sql("transition_cause", TRANSITION_CAUSES),
        "ck_competency_inference_runs_tension_state": _in_sql("tension_state", TENSION_STATES),
        NO_SELF_PREDECESSOR_CHECK: NO_SELF_PREDECESSOR_SQL,
    },
    CLAIMS: {
        "ck_competency_stage_claims_stage": _in_sql("stage", CLAIM_STAGES),
        "ck_competency_stage_claims_positive_basis_status": _in_sql("positive_basis_status", BASIS_STATUSES),
        "ck_competency_stage_claims_basis_mode": _in_sql("basis_mode", BASIS_MODES),
        BASIS_STATUS_MODE_CHECK: BASIS_STATUS_MODE_SQL,
    },
    TENSIONS: {
        "ck_competency_inference_tensions_fragilized_stage": _in_sql("fragilized_stage", CLAIM_STAGES),
        "ck_competency_inference_tensions_scope_mode": _in_sql("scope_mode", TENSION_SCOPE_MODES),
        "ck_competency_inference_tensions_revision_status": _in_sql("revision_status", REVISION_STATUSES),
    },
    TENSION_CAPS: {},
    REFS: {
        REF_ROLE_CHECK: _in_sql("ref_role", REF_ROLES),
        DIMENSION_CHECK: _in_sql("confidence_dimension", CONFIDENCE_DIMENSIONS),
        SOURCE_KIND_CHECK: _in_sql("source_kind", SOURCE_KINDS),
        SOURCE_XOR_CHECK: SOURCE_XOR_SQL,
        POSITIVE_BASIS_CHECK: POSITIVE_BASIS_SQL,
        CONFIDENCE_REF_CHECK: CONFIDENCE_REF_SQL,
        MASTERY_REF_CHECK: MASTERY_REF_SQL,
        TENSION_REF_CHECK: TENSION_REF_SQL,
        **{RUN_LEVEL_REF_CHECKS[role]: RUN_LEVEL_REF_SQL[role] for role in RUN_LEVEL_REF_CHECKS},
    },
    STATES: {
        "ck_user_competency_states_competency_code": _in_sql("competency_code", COMPETENCY_CODES),
        "ck_user_competency_states_current_stage": _in_sql("current_stage", CURRENT_STAGES),
        "ck_user_competency_states_tension_state": _in_sql("tension_state", TENSION_STATES),
    },
}
UNIQUES = {table: {} for table in TABLES_IN_ORDER}
UNIQUES[RUNS] = {DEDUP_UNIQUE: ["inference_dedup_key"]}
UNIQUES[CLAIMS] = {CLAIM_UNIQUE: ["inference_run_id", "stage"]}
UNIQUES[STATES] = {STATE_ACTIVE_UNIQUE: ["active_inference_run_id"]}
INDEXES = {
    RUNS: {
        ONE_ACTIVE_INDEX: (["user_id", "competency_code"], True, "interpretation_status = 'active'"),
        "ix_competency_inference_runs_user_id": (["user_id"], False, None),
        "ix_competency_inference_runs_longitudinal_assessment_run_id":
            (["longitudinal_assessment_run_id"], False, None),
        "ix_competency_inference_runs_predecessor_inference_run_id": (["predecessor_inference_run_id"], False, None),
    },
    CLAIMS: {},
    TENSIONS: {"ix_competency_inference_tensions_inference_run_id": (["inference_run_id"], False, None)},
    TENSION_CAPS: {"ix_competency_inference_tension_caps_membership_id": (["capability_membership_id"], False, None)},
    REFS: {
        "ix_competency_inference_basis_refs_inference_run_id": (["inference_run_id"], False, None),
        "ix_competency_inference_basis_refs_stage_claim_id": (["stage_claim_id"], False, None),
        "ix_competency_inference_basis_refs_tension_id": (["tension_id"], False, None),
        "ix_competency_inference_basis_refs_source_observation_id": (["source_observation_id"], False, None),
        "ix_competency_inference_basis_refs_source_dependency_id": (["source_dependency_id"], False, None),
        "ix_competency_inference_basis_refs_source_transfer_id": (["source_transfer_id"], False, None),
        "ix_competency_inference_basis_refs_source_revalidation_id": (["source_revalidation_id"], False, None),
    },
    STATES: {},
}
assert {name for table in INDEXES.values() for name in table} == T6A_INDEXES


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_exactly_0001_to_0009():
    """0001 -> ... -> 0008 -> 0009 ; 0009 est la seule migration ajoutée par
    T6-A. Seules 0010 (R1-B) et 0011 (R1-C1), testées à part, ont été
    ajoutées depuis : 0011 est la tête."""
    script = _script_directory()
    assert script.get_heads() == [R1C1]
    assert script.get_bases() == [BASELINE]

    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert set(revisions) == {BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A, T6A, R1B, R1C1}
    assert revisions[R1C1].down_revision == R1B
    assert revisions[R1B].down_revision == T6A
    assert revisions[T6A].down_revision == T5A
    assert revisions[T5A].down_revision == T4A

    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [f"{rev}.py" for rev in (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A, T6A, R1B, R1C1)]


def test_revision_id_fits_alembic_version_column():
    """alembic_version.version_num est un VARCHAR(32)."""
    assert len(T6A) == 31
    for rev in _script_directory().walk_revisions():
        assert len(rev.revision) <= 32, rev.revision


def test_0001_to_0008_files_are_unchanged():
    """Migrations historiques immuables : T6-A ne modifie aucune table ni
    migration T0-T5."""
    versions = REPO_ROOT / "alembic" / "versions"
    for rev, expected in (
        (BASELINE, BASELINE_SHA256),
        (T1A, T1A_SHA256),
        (T1B1, T1B1_SHA256),
        (T1C2, T1C2_SHA256),
        (T2A, T2A_SHA256),
        (T3A, T3A_SHA256),
        (T4A, T4A_SHA256),
        (T5A, T5A_SHA256),
    ):
        assert hashlib.sha256((versions / f"{rev}.py").read_bytes()).hexdigest() == expected, rev


def _create_table_sql(table, columns, constraints) -> str:
    return f"CREATE TABLE {table} ( " + ", ".join(columns + constraints) + " )"


def _column_sql(table, column) -> str:
    key = (table, column)
    if key in UUID_COLUMNS or column in ("tension_id", "capability_membership_id"):
        sql_type = "UUID"
    elif key in TIMESTAMPTZ_COLUMNS:
        sql_type = "TIMESTAMP WITH TIME ZONE"
    elif key in JSONB_COLUMNS:
        sql_type = "JSONB"
    elif key in TEXT_COLUMNS:
        sql_type = "TEXT"
    elif key in BIGINT_COLUMNS:
        sql_type = "BIGINT"
    else:
        sql_type = "VARCHAR"
    return f"{column} {sql_type}" + ("" if key in NULLABLE else " NOT NULL")


def _columns_sql(table) -> list:
    return [_column_sql(table, column) for column in COLUMNS[table]]


def _checks_sql(table) -> list:
    return [f"CONSTRAINT {name} CHECK ({sql})" for name, sql in CHECKS[table].items()]


def _fk_sql(column, target, name=None) -> str:
    target_table = target.split(".")[0]
    prefix = f"CONSTRAINT {name} " if name else ""
    return f"{prefix}FOREIGN KEY({column}) REFERENCES {target_table} (id)"


def _index_sql(table) -> list:
    return [f"CREATE INDEX {name} ON {table} ({columns[0]})"
            for name, (columns, unique, _) in INDEXES[table].items() if not unique]


# Mots interdits comme segment (découpage sur « _ » et la ponctuation) d'un
# nom de colonne, de table ou d'identifiant T6 : jamais de score, pourcentage,
# ratio, pénalité, décroissance, XP, points, rang, poids, niveau global,
# fraîcheur, plan de validation ni stade numérique.
FORBIDDEN_SEGMENTS = {
    "score", "scores", "percent", "percentage", "ratio", "ratios", "penalty", "penalties", "decay", "xp",
    "point", "points", "rank", "ranking", "weight", "weights", "coefficient", "balance", "streak",
    "leaderboard", "overall", "highest", "numeric", "fresh", "freshness", "stale", "days", "age", "mastered",
    "next", "plan", "level", "ready", "count", "proof", "value",
}


def _segments(text) -> list:
    """Mots d'un identifiant ou d'un texte, en minuscules, découpés sur tout
    caractère non alphanumérique (dont « _ »)."""
    return re.findall(r"[a-z0-9]+", text.lower())


def test_offline_sql_of_0009_upgrade_is_exactly_the_t6a_structure():
    """Six CREATE TABLE et leurs index, dans l'ordre des dépendances ;
    aucun ALTER d'une table existante (users compris), aucune donnée, aucun
    objet hors tables / index (ni trigger, ni fonction, ni ENUM)."""
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "upgrade", f"{T5A}:{T6A}", "--sql",
    ).stdout
    expected = [
        _create_table_sql(RUNS, _columns_sql(RUNS), [
            "PRIMARY KEY (id)", *_checks_sql(RUNS),
            _fk_sql("user_id", "users.id"),
            _fk_sql("longitudinal_assessment_run_id", f"{ASSESSMENTS}.id"),
            _fk_sql("predecessor_inference_run_id", f"{RUNS}.id"),
            f"CONSTRAINT {DEDUP_UNIQUE} UNIQUE (inference_dedup_key)",
        ]),
        f"CREATE UNIQUE INDEX {ONE_ACTIVE_INDEX} ON {RUNS} (user_id, competency_code) "
        "WHERE interpretation_status = 'active'",
        *_index_sql(RUNS),
        _create_table_sql(CLAIMS, _columns_sql(CLAIMS), [
            "PRIMARY KEY (id)", *_checks_sql(CLAIMS),
            _fk_sql("inference_run_id", f"{RUNS}.id"),
            f"CONSTRAINT {CLAIM_UNIQUE} UNIQUE (inference_run_id, stage)",
        ]),
        _create_table_sql(TENSIONS, _columns_sql(TENSIONS), [
            "PRIMARY KEY (id)", *_checks_sql(TENSIONS),
            _fk_sql("inference_run_id", f"{RUNS}.id"),
        ]),
        *_index_sql(TENSIONS),
        _create_table_sql(TENSION_CAPS, _columns_sql(TENSION_CAPS), [
            "PRIMARY KEY (tension_id, capability_membership_id)",
            _fk_sql("tension_id", f"{TENSIONS}.id"),
            _fk_sql("capability_membership_id", f"{MEMBERSHIPS}.id", TENSION_CAP_MEMBERSHIP_FK),
        ]),
        *_index_sql(TENSION_CAPS),
        _create_table_sql(REFS, _columns_sql(REFS), [
            "PRIMARY KEY (id)", *_checks_sql(REFS),
            _fk_sql("inference_run_id", f"{RUNS}.id"),
            _fk_sql("stage_claim_id", f"{CLAIMS}.id"),
            _fk_sql("tension_id", f"{TENSIONS}.id"),
            _fk_sql("source_observation_id", f"{OBS}.id"),
            _fk_sql("source_dependency_id", f"{DEPS}.id"),
            _fk_sql("source_transfer_id", f"{TRANSFERS}.id"),
            _fk_sql("source_revalidation_id", f"{REVALS}.id"),
        ]),
        *_index_sql(REFS),
        _create_table_sql(STATES, _columns_sql(STATES), [
            "PRIMARY KEY (user_id, competency_code)", *_checks_sql(STATES),
            _fk_sql("user_id", "users.id"),
            _fk_sql("active_inference_run_id", f"{RUNS}.id"),
            f"CONSTRAINT {STATE_ACTIVE_UNIQUE} UNIQUE (active_inference_run_id)",
        ]),
        # Seule écriture de données : la version Alembic (aucun seed, aucun
        # backfill, aucun état généré pour les utilisateurs existants).
        "UPDATE alembic_version SET version_num='0009_competency_inference_state' "
        "WHERE alembic_version.version_num = '0008_longitudinal_relations'",
    ]
    assert _statements(sql) == expected
    upper = sql.upper()
    for forbidden in ("DROP ", "INSERT", "DELETE", "ALTER TABLE", "CREATE TYPE", "CREATE EXTENSION",
                      "CREATE TRIGGER", "CREATE FUNCTION", "CREATE PROCEDURE", "CREATE MATERIALIZED",
                      "CREATE VIEW", "CREATE RULE", "DEFAULT", "ON DELETE", "ON UPDATE", "CASCADE", "ENUM",
                      "SEQUENCE", "SERIAL", "PEDAGOGICAL_TAXONOMY_RELEASE_ID", "'LOW'", "'MEDIUM'", "'HIGH'"):
        assert forbidden not in upper, forbidden
    # Segments d'identifiants (découpés sur « _ ») : state_generation ne
    # contient pas le mot « ratio ».
    assert not set(_segments(sql)) & (FORBIDDEN_SEGMENTS | {"numeric", "integer", "smallint", "boolean", "real"})
    assert [s for s in _statements(sql) if s.upper().startswith("UPDATE")] == [expected[-1]]


def test_offline_sql_of_0009_downgrade_drops_only_t6a():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "downgrade", f"{T6A}:{T5A}", "--sql",
    ).stdout
    expected = []
    for table in reversed(TABLES_IN_ORDER):
        expected += [f"DROP INDEX {name}" for name in reversed(list(INDEXES[table]))
                     if name != ONE_ACTIVE_INDEX]
        if table == RUNS:
            expected.append(f"DROP INDEX {ONE_ACTIVE_INDEX}")
        expected.append(f"DROP TABLE {table}")
    expected.append("UPDATE alembic_version SET version_num='0008_longitudinal_relations' "
                    "WHERE alembic_version.version_num = '0009_competency_inference_state'")
    assert _statements(sql) == expected
    # Aucune table T0-T5 touchée.
    for forbidden in ("CASCADE", "DROP COLUMN", "ALTER TABLE", "DELETE", "INSERT"):
        assert forbidden not in sql.upper(), forbidden
    dropped = [s.split()[-1] for s in expected if s.startswith("DROP TABLE")]
    assert set(dropped) == T6A_TABLES and len(dropped) == 6


def test_metadata_declares_exactly_the_six_t6a_tables():
    assert set(Base.metadata.tables) == (REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES
                                         | T6A_TABLES | R1B_TABLES | R1C1_TABLES)
    for model, name in zip(T6A_MODELS, TABLES_IN_ORDER):
        assert model.__tablename__ == name
        assert model.__table__ is Base.metadata.tables[name]


def test_users_table_is_untouched():
    """users.level reste distinct du moteur pédagogique : aucune colonne de
    stade, d'état ou de niveau global ajoutée à users."""
    assert [c.name for c in User.__table__.columns] == ["id", "level", "created_at"]
    assert not User.__table__.foreign_keys


@pytest.mark.parametrize("model", T6A_MODELS, ids=lambda m: m.__name__)
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
        elif key in BIGINT_COLUMNS:
            assert type(col.type) is sa.BigInteger, key
        else:
            # Tout le reste (dont user_id, comme users.id) : VARCHAR sans longueur.
            assert type(col.type) is sa.String and col.type.length is None, key


def test_run_outputs_are_nullable_while_running():
    """Sorties NULLABLE (inférence en cours) ; entrées, versions et
    fingerprint d'entrée NOT NULL ; model_id / prompt_spec_version sont une
    provenance facultative."""
    table = CompetencyInferenceRun.__table__
    for column in RUN_OUTPUTS:
        assert table.c[column].nullable, column
    for column in ("user_id", "competency_code", "longitudinal_assessment_run_id", "execution_status",
                   "interpretation_status", "trigger", "positive_basis_version", "confidence_profile_version",
                   "state_decision_version", "validation_version", "inference_schema_version",
                   "evaluator_version", "input_fingerprint", "inference_dedup_key", "started_at", "created_at"):
        assert not table.c[column].nullable, column
    assert table.c.model_id.nullable and table.c.prompt_spec_version.nullable


def test_tension_scope_table_is_a_pure_composite_key():
    """Aucun id artificiel, aucun poids, score, stade ni horodatage."""
    table = CompetencyInferenceTensionCapability.__table__
    assert "id" not in table.c
    assert len(table.columns) == 2
    assert list(table.primary_key.columns) == list(table.columns)


@pytest.mark.parametrize("model", T6A_MODELS, ids=lambda m: m.__name__)
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


def test_basis_refs_have_four_explicit_source_foreign_keys():
    """Pas de FK polymorphique opaque (source_type + source_id) : chaque
    source a sa propre colonne et sa vraie FK."""
    table = CompetencyInferenceBasisRef.__table__
    targets = {fk.parent.name: fk.target_fullname for fk in table.foreign_keys}
    assert {column: targets[column] for column in SOURCE_COLUMNS.values()} == {
        "source_observation_id": f"{OBS}.id", "source_dependency_id": f"{DEPS}.id",
        "source_transfer_id": f"{TRANSFERS}.id", "source_revalidation_id": f"{REVALS}.id",
    }
    for column in ("source_id", "source_type", "source_table", "source_ref"):
        assert column not in table.c, column


@pytest.mark.parametrize("model", T6A_MODELS, ids=lambda m: m.__name__)
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
    est la PREMIÈRE colonne d'un index (PK composite ou UNIQUE compris) ;
    aucun index séparé ne double la première colonne d'une PK ou d'un
    UNIQUE ; l'index unique partiel (runs active seulement) ne remplace pas
    celui de user_id."""
    for model in T6A_MODELS:
        table = model.__table__
        leading = {}
        leading.setdefault(table.primary_key.columns[0].name, []).append("pk")
        for constraint in table.constraints:
            if isinstance(constraint, sa.UniqueConstraint):
                leading.setdefault(list(constraint.columns)[0].name, []).append(constraint.name)
        for index in table.indexes:
            if index.dialect_options["postgresql"]["where"] is None:
                leading.setdefault(index.columns[0].name, []).append(index.name)
        for fk in table.foreign_keys:
            assert fk.parent.name in leading, (table.name, fk.parent.name)
        for column, providers in leading.items():
            assert len(providers) == 1, (table.name, column, providers)


def test_vocabularies():
    """Stades : non_etabli uniquement comme current / previous stage, jamais
    comme stage claim ni comme stade fragilisé ; aucun faux stade
    (application_uncertain, between_*) ; trigger reste ouvert."""
    assert "non_etabli" not in CHECKS[CLAIMS]["ck_competency_stage_claims_stage"]
    assert "non_etabli" not in CHECKS[TENSIONS]["ck_competency_inference_tensions_fragilized_stage"]
    for model in T6A_MODELS:
        for constraint in model.__table__.constraints:
            if isinstance(constraint, sa.CheckConstraint):
                sql = str(constraint.sqltext)
                for fake in ("uncertain", "between", "expert", "promoted", "weak", "automatic", "overall",
                             "'low'", "'medium'", "'high'"):
                    assert fake not in sql, (constraint.name, fake)
    assert "trigger" not in " ".join(CHECKS[RUNS].values())
    for column in ("positive_basis_version", "confidence_profile_version", "state_decision_version",
                   "validation_version", "inference_schema_version", "evaluator_version", "input_fingerprint",
                   "output_fingerprint", "model_id", "prompt_spec_version"):
        assert column not in " ".join(sql for checks in CHECKS.values() for sql in checks.values()), column


def test_no_server_defaults_only_application_defaults():
    """Aucun server_default ni onupdate ; id (hors PK composites) et
    horodatages NOT NULL générés par l'application (UTC aware) ; aucune
    autre valeur inventée (ni stade, ni tension, ni statut, ni
    state_generation, ni completed_at)."""
    def call(default):
        assert default is not None and default.is_callable
        return default.arg(None)

    for model in T6A_MODELS:
        table = model.__table__
        for col in table.columns:
            assert col.server_default is None and col.server_onupdate is None, (table.name, col.name)
            assert col.onupdate is None, (table.name, col.name)
            if col.name in ("created_at", "started_at", "updated_at"):
                assert call(col.default).utcoffset().total_seconds() == 0
            elif col.name == "id":
                assert isinstance(call(col.default), uuid.UUID)
                assert call(col.default) != call(col.default)
            else:
                assert col.default is None, (table.name, col.name)


def test_jsonb_and_timestamptz_columns():
    jsonb, tz = set(), set()
    for model in T6A_MODELS:
        for col in model.__table__.columns:
            if isinstance(col.type, sa.JSON):
                assert type(col.type) is JSONB
                # Un None Python devient NULL SQL, jamais le JSON null.
                assert col.type.none_as_null is True, col.name
                jsonb.add((col.table.name, col.name))
            if isinstance(col.type, sa.DateTime):
                assert col.type.timezone is True, col.name
                tz.add((col.table.name, col.name))
    assert jsonb == JSONB_COLUMNS
    assert tz == TIMESTAMPTZ_COLUMNS


def test_no_relationships_foreign_keys_are_the_source_of_truth():
    for model in T6A_MODELS:
        assert not sa.inspect(model).relationships, model.__name__


def _models_tree():
    return ast.parse((REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8"))


def test_models_module_contains_no_service_logic():
    """models.py ne déclare que des modèles : aucune fonction hors du helper
    d'horodatage, aucune méthode sur les modèles T6-A."""
    tree = _models_tree()
    assert {n.name for n in tree.body if isinstance(n, ast.FunctionDef)} == {"_utcnow_aware"}
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    for model in T6A_MODELS:
        body = classes[model.__name__].body
        assert not [n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))], model.__name__


def test_model_docstrings_state_the_doctrine():
    """Les docstrings disent ce qu'est (et n'est pas) chaque structure."""
    docs = {model.__name__: " ".join((model.__doc__ or "").split()) for model in T6A_MODELS}
    for name, doc in docs.items():
        assert "T6-A" in doc and "0009_competency_inference_state" in doc, name
    assert "État DÉRIVÉ, jamais preuve" in docs["CompetencyInferenceRun"]
    assert "distincte du current_stage" in docs["CompetencyStageClaim"]
    assert "non_etabli n'est PAS une stage claim" in docs["CompetencyStageClaim"]
    assert "ni un stade, ni une pénalité" in docs["CompetencyInferenceTension"]
    assert docs["CompetencyInferenceTensionCapability"].startswith("Périmètre sémantique")
    assert "Provenance seulement : une ref n'est JAMAIS une preuve supplémentaire" in docs[
        "CompetencyInferenceBasisRef"]
    assert "Read-model / cache" in docs["UserCompetencyState"]
    assert "PAS la source de vérité" in docs["UserCompetencyState"]
    assert "chaîne parentale" in docs["UserCompetencyState"]
    migration = (REPO_ROOT / "alembic" / "versions" / f"{T6A}.py").read_text(encoding="utf-8")
    assert "Invariants volontairement reportés à T6-B / T6-C" in migration


# --- anti-dérive -------------------------------------------------------------

# Noms exacts interdits (cahier des charges T6-A).
FORBIDDEN_COLUMNS = (
    "competency_score", "confidence_score", "evidence_balance", "contradiction_penalty", "decay_rate",
    "mastery_points", "xp", "overall_user_level", "coverage_percent", "capability_stage", "capability_mastered",
    "next_capability", "streak", "leaderboard", "highest_positive_stage", "stage_rank", "stage_value",
    "numeric_stage", "confidence_level", "mastery_ready", "fresh", "stale", "days_since", "freshness_score",
    "last_validation_age", "active_validation_plan", "validation_plan", "pedagogical_taxonomy_release_id",
)


def _t6_model_sources() -> list:
    source = (REPO_ROOT / "core" / "models.py").read_text(encoding="utf-8")
    segments = [ast.get_source_segment(source, node) for node in _models_tree().body
                if isinstance(node, ast.ClassDef) and node.name in {m.__name__ for m in T6A_MODELS}]
    assert len(segments) == 6
    return segments


def test_no_score_numeric_stage_or_numeric_confidence_in_t6a():
    """Aucune colonne de score, pourcentage, ratio, pénalité, décroissance,
    XP, points, rang, poids, niveau global, fraîcheur, stade numérique ou
    confiance numérique ; seule colonne numérique : state_generation
    (compteur technique du cache). Inspection statique des modèles et de la
    migration (hors docstrings et commentaires, qui peuvent expliquer
    l'interdiction)."""
    for model in T6A_MODELS:
        table = model.__table__
        assert not set(_segments(table.name)) & FORBIDDEN_SEGMENTS, table.name
        for col in table.columns:
            assert not set(_segments(col.name)) & FORBIDDEN_SEGMENTS, (table.name, col.name)
            assert col.name not in FORBIDDEN_COLUMNS, (table.name, col.name)
            assert "taxonomy_release" not in col.name, (table.name, col.name)
            if isinstance(col.type, (sa.Integer, sa.Float, sa.Numeric, sa.Boolean)):
                assert (table.name, col.name) == (STATES, "state_generation"), (table.name, col.name)
    migration = (REPO_ROOT / "alembic" / "versions" / f"{T6A}.py").read_text(encoding="utf-8")
    for label, code in ((f"{T6A}.py", migration), *(("models", s) for s in _t6_model_sources())):
        tokens = _code_tokens(code)
        for identifier in FORBIDDEN_COLUMNS:
            assert identifier not in tokens.split("\n"), (label, identifier)
        assert not set(_segments(tokens)) & FORBIDDEN_SEGMENTS, (label, set(_segments(tokens)) & FORBIDDEN_SEGMENTS)
        for number_type in ("Integer", "Float", "Numeric", "Boolean", "SmallInteger"):
            assert number_type not in tokens.split("\n"), (label, number_type)


def test_no_active_validation_plan_is_persisted():
    """validation_needs (besoin latent dérivé) est autorisé ; aucune table ni
    colonne active_validation_plan / validation_plan n'existe (décision
    éphémère de l'adaptation future)."""
    assert "validation_needs" in CompetencyInferenceRun.__table__.c
    for name, table in Base.metadata.tables.items():
        assert "validation_plan" not in name, name
        for col in table.columns:
            assert "validation_plan" not in col.name, (name, col.name)


def test_no_global_level_or_competency_state_outside_t6a():
    """Aucun niveau global utilisateur ; les seules structures d'état de
    compétence sont les six tables T6-A (compétence-spécifiques C1..C12)."""
    for name, table in Base.metadata.tables.items():
        for fragment in ("overall", "global_level", "user_level", "leaderboard", "streak"):
            assert fragment not in name, name
            for col in table.columns:
                assert fragment not in col.name, (name, col.name)
        if name not in T6A_TABLES:
            for fragment in ("inference", "stage_claim", "competency_state", "tension"):
                assert fragment not in name, name
    for model in (CompetencyInferenceRun, UserCompetencyState):
        assert "competency_code" in model.__table__.c


T6B_OPERATIONS = ("start_competency_inference", "complete_inference", "activate_inference", "add_claim",
                  "add_tension", "update_user_state", "infer_stage", "complete_competency_inference",
                  "activate_competency_inference", "add_stage_claim", "add_basis_ref")


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


# T6-B : le service transactionnel core/inference_service.py (non branché,
# voir tests/test_inference_service.py) est le SEUL module T6 d'écriture ; il
# ne porte que ces deux opérations de la liste ci-dessus.
T6B_SERVICE = "core/inference_service.py"
T6B_SERVICE_OPERATIONS = ("start_competency_inference", "complete_competency_inference")
# T6-C1 : policies versionnées et moteur PUR de base positive (non branchés,
# sans base ni opération T6-B : voir tests/test_inference_positive_basis.py
# et tests/test_inference_policies.py). Aucun autre moteur T6-C.
T6C1_MODULES = ("core/inference_policies.py", "core/inference_positive_basis.py")
# T6-C2 : policies versionnées et moteurs PURS de confiance / tension / état
# (non branchés, sans base ni opération T6-B : voir
# tests/test_inference_confidence.py et tests/test_inference_state.py).
T6C2_MODULES = ("core/inference_state_policies.py", "core/inference_confidence.py", "core/inference_state.py")
# T6-C3 : policies finales et moteur PUR d'assemblage (cause de transition,
# besoins de validation latents, memberships, décision finale ;
# core/inference_engine.py porte l'unique point d'entrée infer_competency).
# Non branchés, sans base ni opération T6-B : voir
# tests/test_inference_transition.py, tests/test_inference_validation.py et
# tests/test_inference_engine.py.
T6C3_MODULES = ("core/inference_final_policies.py", "core/inference_transition.py", "core/inference_validation.py",
                "core/inference_engine.py")


def test_no_t6_service_engine_or_inference_anywhere():
    """T6-A = persistance seulement : aucun module d'inférence / d'état
    autre que le service transactionnel T6-B core/inference_service.py et,
    depuis T6-C1 / T6-C2 / T6-C3, les modules purs du moteur T6-C (dont
    core/inference_engine.py depuis T6-C3 ; aucun autre), aucune opération
    T6-B (démarrer, compléter, activer, ajouter claim / tension, écrire le
    cache, inférer un stade) ailleurs que dans ce service — les modules
    T6-C1 n'en portent aucune — et, dans ce service, seulement start /
    complete (jamais infer_stage, add_claim, activate...), hors docstrings
    et commentaires."""
    modules = [p.relative_to(REPO_ROOT).as_posix()
               for p in [*(REPO_ROOT / "core").rglob("*.py"), *(REPO_ROOT / "scripts").rglob("*.py"),
                         REPO_ROOT / "api.py"]]
    assert sorted(m for m in modules if any(w in m for w in ("inference", "competency_state", "stage_claim",
                                                              "tension", "user_state"))) == sorted(
        [T6B_SERVICE, *T6C1_MODULES, *T6C2_MODULES, *T6C3_MODULES])
    checked = 0
    for rel, source in _application_sources():
        checked += 1
        allowed = T6B_SERVICE_OPERATIONS if rel == T6B_SERVICE else ()
        for name in T6B_OPERATIONS:
            if name not in allowed:
                assert name not in source, (rel, name)
    assert checked > 0


def test_t6a_tables_are_not_wired_to_the_application():
    """Aucun branchement : hors core/models.py, la migration 0009 et, depuis
    T6-B, le service core/inference_service.py (seul point d'écriture
    applicatif des six tables, lui-même non branché : voir
    tests/test_inference_service.py),
    aucun code applicatif (api.py, core/ dont les services T2-B à T5-C,
    scripts/, frontend) ne mentionne ces modèles ou ces tables, et aucun
    autre code n'écrit user_competency_states."""
    allowed = {"core/models.py", f"alembic/versions/{T6A}.py", T6B_SERVICE}
    needles = (*(m.__name__ for m in T6A_MODELS), *T6A_TABLES)
    checked = 0
    for rel, source in _application_sources():
        checked += 1
        if rel not in allowed:
            for needle in needles:
                assert needle not in source, (rel, needle)
    assert checked > 0


def test_no_route_for_t6():
    """api.py inchangé : aucun endpoint d'inférence, de stade, de tension ou
    d'état de compétence."""
    source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    tokens = _code_tokens(source).lower()
    for word in ("inference", "stage_claim", "tension", "competency_state", "competencystate"):
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
        for segment in ("/inference", "/competency-state", "/competency_state", "/stage", "/tension",
                        "/mastery"):
            assert segment not in route, route


def test_no_llm_or_runtime_schema_mutation_in_t6a():
    """Ni la migration ni les modèles T6-A n'appellent un LLM ; aucun
    create_all ni mutation de schéma hors Alembic. model_id et
    prompt_spec_version ne sont que des colonnes de provenance."""
    migration = REPO_ROOT / "alembic" / "versions" / f"{T6A}.py"
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
    for label, code in ((migration.name, migration.read_text(encoding="utf-8")),
                        *(("models", s) for s in _t6_model_sources())):
        tokens = _code_tokens(code).lower()
        for word in ("anthropic", "openai", "claude", "llm", "classifier", "create_all", "requests", "httpx",
                     "bootstrap", "execute", "insert", "messages", "completion", "client"):
            assert word not in tokens, (label, word)


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

def _insert(table):
    jsonb = {name for (t, name) in JSONB_COLUMNS if t == table}
    return sa.text(
        f"INSERT INTO {table} ({', '.join(COLUMNS[table])}) VALUES ("
        + ", ".join(f"CAST(:{c} AS JSONB)" if c in jsonb else f":{c}" for c in COLUMNS[table]) + ")"
    )


INSERTS = {table: _insert(table) for table in TABLES_IN_ORDER}


def _inference(conn, assessment, /, **overrides) -> uuid.UUID:
    """Run T6 « running » minimal : toutes les sorties NULL."""
    params = {
        "id": uuid.uuid4(),
        "user_id": USER,
        "competency_code": "C7",
        "longitudinal_assessment_run_id": assessment,
        "predecessor_inference_run_id": None,
        "execution_status": "running",
        "interpretation_status": "candidate",
        "trigger": "longitudinal_run_activated",
        "previous_stage": None,
        "current_stage": None,
        "transition": None,
        "transition_cause": None,
        "tension_state": None,
        "unresolved_revision_context": None,
        "validation_needs": None,
        "state_decision_summary": None,
        "positive_basis_version": "positive-basis-1",
        "confidence_profile_version": "confidence-profile-1",
        "state_decision_version": "state-decision-1",
        "validation_version": "validation-1",
        "inference_schema_version": "inference-schema-1",
        "evaluator_version": "evaluator-1",
        "model_id": None,
        "prompt_spec_version": None,
        "input_fingerprint": "sha256:input",
        "output_fingerprint": None,
        "inference_dedup_key": f"dedup-{uuid.uuid4()}",
        "started_at": NOW,
        "completed_at": None,
        "created_at": NOW,
        "failure_code": None,
    }
    params.update(overrides)
    conn.execute(INSERTS[RUNS], params)
    return params["id"]


def _completed(conn, assessment, /, **overrides) -> uuid.UUID:
    """Run T6 completed avec des sorties plausibles (fournies par un futur
    service : la base ne les calcule pas)."""
    params = {
        "execution_status": "completed", "current_stage": "application", "tension_state": "none",
        "validation_needs": '[{"stage": "mastery", "need": "transfert dans un contexte nouveau"}]',
        "state_decision_summary": "Application établie directement ; Mastery non établie.",
        "output_fingerprint": "sha256:output", "completed_at": NOW,
    }
    params.update(overrides)
    return _inference(conn, assessment, **params)


def _claim(conn, run_id, /, stage="application", **overrides) -> uuid.UUID:
    params = {
        "id": uuid.uuid4(),
        "inference_run_id": run_id,
        "stage": stage,
        "positive_basis_status": "established",
        "basis_mode": "direct",
        "basis_summary": "Deux démonstrations autonomes du calcul de ROE.",
        "scope_summary": None,
        "confidence_profile": None,
        "mastery_assessment": None,
        "created_at": NOW,
    }
    params.update(overrides)
    conn.execute(INSERTS[CLAIMS], params)
    return params["id"]


def _tension(conn, run_id, /, **overrides) -> uuid.UUID:
    params = {
        "id": uuid.uuid4(),
        "inference_run_id": run_id,
        "fragilized_stage": "application",
        "scope_mode": "whole_competency",
        "summary": "Contradiction récente sur l'effet de levier.",
        "revision_status": "unresolved",
        "created_at": NOW,
    }
    params.update(overrides)
    conn.execute(INSERTS[TENSIONS], params)
    return params["id"]


def _tension_scope(conn, tension_id, membership_id) -> None:
    conn.execute(INSERTS[TENSION_CAPS], {"tension_id": tension_id, "capability_membership_id": membership_id})


def _ref(conn, run_id, /, *, role="positive_basis", claim=None, tension=None, dimension=None,
         kind="observation", sources=None, **overrides) -> uuid.UUID:
    """sources : {source_kind: id} ; par défaut, la source correspondant à
    kind doit être fournie via overrides ou sources."""
    params = {
        "id": uuid.uuid4(),
        "inference_run_id": run_id,
        "stage_claim_id": claim,
        "tension_id": tension,
        "ref_role": role,
        "confidence_dimension": dimension,
        "source_kind": kind,
        **{column: (sources or {}).get(k) for k, column in SOURCE_COLUMNS.items()},
        "created_at": NOW,
    }
    params.update(overrides)
    conn.execute(INSERTS[REFS], params)
    return params["id"]


def _state(conn, run_id, /, **overrides) -> None:
    params = {
        "user_id": USER,
        "competency_code": "C7",
        "active_inference_run_id": run_id,
        "current_stage": "application",
        "tension_state": "none",
        "state_generation": 1,
        "updated_at": NOW,
    }
    params.update(overrides)
    conn.execute(INSERTS[STATES], params)


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
    """Schéma = head (0009) + deux utilisateurs et une analysis_session ;
    tout ce que fait le test est annulé."""
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
def assessment(conn, release) -> uuid.UUID:
    return _assessment(conn, release, interpretation_status="active")


@pytest.fixture
def run(conn, assessment) -> uuid.UUID:
    return _completed(conn, assessment)


@pytest.fixture
def dossier(conn, assessment) -> dict:
    """Un objet de chaque sorte de source du dossier T5 (observation,
    dépendance, transfert, revalidation)."""
    a, b = _observation(conn), _observation(conn)
    contradiction = _contradictory(conn)
    for observation in (a, b, contradiction):
        _input(conn, assessment, observation)
    return {
        "observation": a,
        "dependency": _dependency(conn, assessment, b, source_observation=a),
        "transfer": _transfer(conn, assessment, a, b),
        "revalidation": _revalidation(conn, assessment, contradiction, b),
    }


# --- upgrade et structure --------------------------------------------------------

def test_pg_upgrade_from_empty_database_to_0009(pg_url, pg_engine):
    """Base vide -> 0009 ; Base.metadata == schéma migré (hors écarts R1-B,
    0010 testée à part) ; aucun ENUM, trigger ni fonction ; les six tables
    T6 sont VIDES (aucun seed, aucun état généré)."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T6A)
    assert _version(pg_engine) == T6A
    assert _tables(pg_engine) == (REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES
                                  | T6A_TABLES | {"alembic_version"})
    assert _without_r1b_changes(_without_r1c1_changes(_compare_metadata(pg_engine)), events_created=True) == []
    catalog = _global_catalog(pg_engine)
    assert (catalog["enums"], catalog["triggers"], catalog["functions"]) == (0, 0, 0)
    # Aucune séquence T6 (UUID applicatifs ; state_generation n'est pas un serial).
    assert not [s for s in catalog["sequences"] if any(t in s for t in T6A_TABLES)]
    assert _data(pg_engine, T6A_TABLES) == {name: [] for name in sorted(T6A_TABLES)}


def _pg_columns(table) -> list:
    types = {key: "uuid" for key in UUID_COLUMNS}
    types.update({key: "timestamp with time zone" for key in TIMESTAMPTZ_COLUMNS})
    types.update({key: "jsonb" for key in JSONB_COLUMNS})
    types.update({key: "text" for key in TEXT_COLUMNS})
    types.update({key: "bigint" for key in BIGINT_COLUMNS})
    types[(TENSION_CAPS, "tension_id")] = types[(TENSION_CAPS, "capability_membership_id")] = "uuid"
    return [(n, types.get((table, n), "character varying"), "YES" if (table, n) in NULLABLE else "NO", None)
            for n in COLUMNS[table]]


def _pg_ref_role_check(role_sql, requirements) -> str:
    return f"CHECK ((({role_sql}) OR ({' AND '.join(requirements)})))"


PG_CHECKS = {
    RUNS: {
        "ck_competency_inference_runs_competency_code": _pg_in_check("competency_code", COMPETENCY_CODES),
        "ck_competency_inference_runs_execution_status": _pg_in_check("execution_status", EXECUTION_STATUSES),
        "ck_competency_inference_runs_interpretation_status":
            _pg_in_check("interpretation_status", INTERPRETATION_STATUSES),
        "ck_competency_inference_runs_previous_stage": _pg_in_check("previous_stage", CURRENT_STAGES),
        "ck_competency_inference_runs_current_stage": _pg_in_check("current_stage", CURRENT_STAGES),
        "ck_competency_inference_runs_transition": _pg_in_check("transition", TRANSITIONS),
        "ck_competency_inference_runs_transition_cause": _pg_in_check("transition_cause", TRANSITION_CAUSES),
        "ck_competency_inference_runs_tension_state": _pg_in_check("tension_state", TENSION_STATES),
        NO_SELF_PREDECESSOR_CHECK:
            "CHECK (((predecessor_inference_run_id IS NULL) OR (predecessor_inference_run_id <> id)))",
    },
    CLAIMS: {
        "ck_competency_stage_claims_stage": _pg_in_check("stage", CLAIM_STAGES),
        "ck_competency_stage_claims_positive_basis_status": _pg_in_check("positive_basis_status", BASIS_STATUSES),
        "ck_competency_stage_claims_basis_mode": _pg_in_check("basis_mode", BASIS_MODES),
        BASIS_STATUS_MODE_CHECK:
            "CHECK (((((positive_basis_status)::text = 'not_established'::text) AND ((basis_mode)::text = "
            "'none'::text)) OR (((positive_basis_status)::text = 'established'::text) AND ((basis_mode)::text "
            "= ANY ((ARRAY['direct'::character varying, 'implied_by_higher_claim'::character varying])"
            "::text[])))))",
    },
    TENSIONS: {
        "ck_competency_inference_tensions_fragilized_stage": _pg_in_check("fragilized_stage", CLAIM_STAGES),
        "ck_competency_inference_tensions_scope_mode": _pg_in_check("scope_mode", TENSION_SCOPE_MODES),
        "ck_competency_inference_tensions_revision_status": _pg_in_check("revision_status", REVISION_STATUSES),
    },
    REFS: {
        REF_ROLE_CHECK: _pg_in_check("ref_role", REF_ROLES),
        DIMENSION_CHECK: _pg_in_check("confidence_dimension", CONFIDENCE_DIMENSIONS),
        SOURCE_KIND_CHECK: _pg_in_check("source_kind", SOURCE_KINDS),
        SOURCE_XOR_CHECK: "CHECK ((" + " OR ".join(_xor_branch(k, pg=True) for k in SOURCE_KINDS) + "))",
        POSITIVE_BASIS_CHECK: _pg_ref_role_check(
            "(ref_role)::text <> 'positive_basis'::text",
            ["(stage_claim_id IS NOT NULL)", "(tension_id IS NULL)", "(confidence_dimension IS NULL)",
             "((source_kind)::text = 'observation'::text)"]),
        CONFIDENCE_REF_CHECK: _pg_ref_role_check(
            "(ref_role)::text <> 'confidence'::text",
            ["(stage_claim_id IS NOT NULL)", "(tension_id IS NULL)", "(confidence_dimension IS NOT NULL)"]),
        MASTERY_REF_CHECK: _pg_ref_role_check(
            "(ref_role)::text <> 'mastery'::text",
            ["(stage_claim_id IS NOT NULL)", "(tension_id IS NULL)", "(confidence_dimension IS NULL)"]),
        TENSION_REF_CHECK: _pg_ref_role_check(
            "(ref_role)::text <> 'tension'::text",
            ["(stage_claim_id IS NULL)", "(tension_id IS NOT NULL)", "(confidence_dimension IS NULL)"]),
        **{RUN_LEVEL_REF_CHECKS[role]: _pg_ref_role_check(
            f"(ref_role)::text <> '{role}'::text",
            ["(stage_claim_id IS NULL)", "(tension_id IS NULL)", "(confidence_dimension IS NULL)"])
           for role in RUN_LEVEL_REF_CHECKS},
    },
    STATES: {
        "ck_user_competency_states_competency_code": _pg_in_check("competency_code", COMPETENCY_CODES),
        "ck_user_competency_states_current_stage": _pg_in_check("current_stage", CURRENT_STAGES),
        "ck_user_competency_states_tension_state": _pg_in_check("tension_state", TENSION_STATES),
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


def test_pg_exact_structure_of_the_six_tables(pg_url, pg_engine):
    """Colonnes, types, nullabilité, PK, FK (NO ACTION, noms réels), CHECK,
    UNIQUE et index exacts, lus dans le catalogue PostgreSQL ; aucune FK en
    cascade dans toute la base."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T6A)
    for table in TABLES_IN_ORDER:
        assert _catalog_columns(pg_engine, table) == _pg_columns(table), table
        assert _constraints(pg_engine, table) == _expected_constraints(table), table
        assert _indexdefs(pg_engine, table) == _expected_indexdefs(table), table
    assert _indexdefs(pg_engine, RUNS)[ONE_ACTIVE_INDEX] == (
        f"CREATE UNIQUE INDEX {ONE_ACTIVE_INDEX} ON public.{RUNS} USING btree (user_id, competency_code) "
        "WHERE ((interpretation_status)::text = 'active'::text)"
    )
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
            "AND (confdeltype <> 'a' OR confupdtype <> 'a')"
        )).scalar_one() == 0


@pytest.mark.parametrize("table", TABLES_IN_ORDER)
def test_pg_required_columns_are_not_null(conn, assessment, release, table):
    """Toute colonne hors NULLABLE est NOT NULL (aucun défaut inventé)."""
    run_id = _completed(conn, assessment)
    tension_id = _tension(conn, run_id)
    claim_id = _claim(conn, run_id)
    membership = _membership(conn, release, _definition(conn, "C7_A"))
    observation = _observation(conn)

    def insert(column):
        if table == RUNS:
            return lambda: _inference(conn, assessment, **{column: None})
        if table == CLAIMS:
            return lambda: _claim(conn, run_id, **{"stage": "mastery", column: None})
        if table == TENSIONS:
            return lambda: _tension(conn, run_id, **{column: None})
        if table == TENSION_CAPS:
            params = {"tension_id": tension_id, "capability_membership_id": membership, column: None}
            return lambda: conn.execute(INSERTS[TENSION_CAPS], params)
        if table == REFS:
            return lambda: _ref(conn, run_id, claim=claim_id, sources={"observation": observation},
                                **{column: None})
        other = _completed(conn, assessment)
        return lambda: _state(conn, other, **{column: None})

    for column in COLUMNS[table]:
        if (table, column) in NULLABLE:
            continue
        _refused(conn, f'null value in column "{column}"', insert(column))


# --- runs d'inférence ----------------------------------------------------------------

@pytest.mark.parametrize("column, values, refused", [
    ("competency_code", COMPETENCY_CODES, ("", "C0", "C13", "c7", "C07", "C7 ", "C7_A", "global")),
    ("execution_status", EXECUTION_STATUSES, ("", "Running", "pending", "done", "cancelled")),
    ("interpretation_status", INTERPRETATION_STATUSES, ("", "Active", "draft", "retired", "stale")),
    ("previous_stage", CURRENT_STAGES, ("", "expert", "none", "Discovery", "application_uncertain")),
    ("current_stage", CURRENT_STAGES, ("", "expert", "none", "non_établi", "application_uncertain",
                                       "between_comprehension_and_application", "0", "3")),
    ("transition", TRANSITIONS, ("", "promoted", "demoted", "downgraded", "unchanged")),
    ("transition_cause", TRANSITION_CAUSES, ("", "time_elapsed", "decay", "inactivity", "manual")),
    ("tension_state", TENSION_STATES, ("", "uncertain", "closed", "resolved", "high")),
])
def test_pg_run_vocabularies(conn, assessment, column, values, refused):
    for value in values:
        _inference(conn, assessment, **{column: value})
    for value in refused:
        _refused(conn, f"ck_competency_inference_runs_{column}",
                 lambda: _inference(conn, assessment, **{column: value}))
    assert _count(conn, RUNS) == len(values)


def test_pg_run_trigger_is_open_and_versions_are_free_strings(conn, assessment):
    for trigger in ("longitudinal_run_activated", "manual_replay", "evidence_invalidated", "un_futur_declencheur"):
        _inference(conn, assessment, trigger=trigger, positive_basis_version=f"pb-{trigger}",
                   model_id="un-modele", prompt_spec_version="spec-2026-09")
    assert _count(conn, RUNS) == 4


def test_pg_running_run_keeps_every_output_null(conn, assessment):
    """Un run running n'a aucune sortie : aucun placeholder non_etabli ;
    NULL reste NULL (JSONB none_as_null côté ORM, NULL SQL ici)."""
    run_id = _inference(conn, assessment)
    row = conn.execute(sa.text(f"SELECT {', '.join(RUN_OUTPUTS)} FROM {RUNS} WHERE id = :id"),
                       {"id": run_id}).one()
    assert tuple(row) == (None,) * len(RUN_OUTPUTS)


def test_pg_run_lifecycle_fields_are_not_cross_checked(conn, assessment):
    """Aucun CHECK croisé de lifecycle (complétion en T6-B) : completed sans
    sortie, failed avec sortie, running avec completed_at, transition sans
    cause... tous acceptés par la base."""
    _inference(conn, assessment, execution_status="completed")
    _inference(conn, assessment, execution_status="failed", failure_code="timeout", current_stage="discovery")
    _inference(conn, assessment, execution_status="running", completed_at=NOW)
    _inference(conn, assessment, transition="upgraded")
    _inference(conn, assessment, transition_cause="new_user_evidence")
    _inference(conn, assessment, interpretation_status="active", execution_status="running")
    assert _count(conn, RUNS) == 6


def test_pg_representable_decisions(conn, assessment):
    """Le schéma représente (sans les décider) : première inférence
    (predecessor / previous_stage / transition / cause NULL) ; non_etabli
    comme conclusion réelle (contradictions seules, sans tension) ;
    maintained, upgraded, revised_down ; Application maintenue sous tension
    ouverte avec contexte de révision non résolue (stage et tension
    orthogonaux)."""
    first = _completed(conn, assessment, current_stage="non_etabli", tension_state="none",
                       validation_needs=None)
    upgraded = _completed(conn, assessment, predecessor_inference_run_id=first, previous_stage="non_etabli",
                          current_stage="application", transition="upgraded",
                          transition_cause="new_user_evidence")
    tensioned = _completed(
        conn, assessment, predecessor_inference_run_id=upgraded, previous_stage="application",
        current_stage="application", transition="maintained", transition_cause="new_user_evidence",
        tension_state="open",
        unresolved_revision_context='{"fragilized": "application", "lower_stage": "non identifiable"}',
    )
    _tension(conn, tensioned, fragilized_stage="application", scope_mode="localized")
    _completed(conn, assessment, predecessor_inference_run_id=tensioned, previous_stage="application",
               current_stage="comprehension", transition="revised_down",
               transition_cause="evidence_integrity_change")
    _completed(conn, assessment, previous_stage="discovery", current_stage="non_etabli",
               transition="revised_down", transition_cause="pedagogical_reinterpretation")
    row = conn.execute(sa.text(
        f"SELECT predecessor_inference_run_id, previous_stage, transition, transition_cause FROM {RUNS} "
        "WHERE id = :id"), {"id": first}).one()
    assert tuple(row) == (None, None, None, None)
    assert conn.execute(sa.text(f"SELECT current_stage, tension_state, unresolved_revision_context FROM {RUNS} "
                                "WHERE id = :id"), {"id": tensioned}).one() == (
        "application", "open", {"fragilized": "application", "lower_stage": "non identifiable"})
    # Contradictions seules : non_etabli sans aucune tension (pas de pseudo-stade négatif).
    assert _count(conn, TENSIONS, "inference_run_id = :r", {"r": first}) == 0


def test_pg_predecessor(conn, assessment):
    """Self-FK nullable, NO ACTION ; un run n'est jamais son propre
    predecessor (INSERT comme UPDATE) ; predecessor inconnu refusé."""
    first = _completed(conn, assessment)
    second = _completed(conn, assessment, predecessor_inference_run_id=first)
    own = uuid.uuid4()
    _refused(conn, NO_SELF_PREDECESSOR_CHECK,
             lambda: _inference(conn, assessment, id=own, predecessor_inference_run_id=own),
             drop=((RUNS, "competency_inference_runs_predecessor_inference_run_id_fkey"),))
    _refused(conn, NO_SELF_PREDECESSOR_CHECK, lambda: conn.execute(sa.text(
        f"UPDATE {RUNS} SET predecessor_inference_run_id = id WHERE id = :id"), {"id": second}))
    _refused(conn, "competency_inference_runs_predecessor_inference_run_id_fkey",
             lambda: _inference(conn, assessment, predecessor_inference_run_id=uuid.uuid4()))
    _refused(conn, "competency_inference_runs_predecessor_inference_run_id_fkey",
             lambda: conn.execute(sa.text(f"DELETE FROM {RUNS} WHERE id = :id"), {"id": first}))
    assert _count(conn, RUNS) == 2


def test_pg_known_limit_predecessor_and_parent_coherence_are_left_to_t6b(conn, release, assessment):
    """LIMITE DOCUMENTÉE (T6-B) : la base accepte un predecessor d'un autre
    utilisateur ou d'une autre compétence, un run T5 parent d'un autre
    utilisateur / compétence ou non active / running, et un cycle A <-> B.
    Aucun trigger ne simule ces invariants cross-table."""
    other_user_run = _completed(conn, assessment, user_id=OTHER_USER)
    other_competency_run = _completed(conn, assessment, competency_code="C8")
    _completed(conn, assessment, predecessor_inference_run_id=other_user_run)
    _completed(conn, assessment, predecessor_inference_run_id=other_competency_run)
    running_parent = _assessment(conn, release, user_id=OTHER_USER, competency_code="C2",
                                 execution_status="running", interpretation_status="candidate")
    _completed(conn, running_parent, user_id=USER, competency_code="C7")
    a, b = _completed(conn, assessment), _completed(conn, assessment)
    conn.execute(sa.text(f"UPDATE {RUNS} SET predecessor_inference_run_id = :b WHERE id = :a"), {"a": a, "b": b})
    conn.execute(sa.text(f"UPDATE {RUNS} SET predecessor_inference_run_id = :a WHERE id = :b"), {"a": a, "b": b})
    assert _count(conn, RUNS) == 7


def test_pg_run_foreign_keys(conn, assessment):
    _refused(conn, "competency_inference_runs_user_id_fkey",
             lambda: _inference(conn, assessment, user_id="inconnu"))
    _refused(conn, "competency_inference_runs_longitudinal_assessment_run_id_fkey",
             lambda: _inference(conn, uuid.uuid4()))
    _inference(conn, assessment, user_id=OTHER_USER)


def test_pg_inference_dedup_key_is_globally_unique(conn, assessment):
    _inference(conn, assessment, inference_dedup_key="C7:u:v1")
    for overrides in ({}, {"user_id": OTHER_USER}, {"competency_code": "C8"},
                      {"interpretation_status": "obsolete"}):
        _refused(conn, DEDUP_UNIQUE,
                 lambda: _inference(conn, assessment, inference_dedup_key="C7:u:v1", **overrides))
    _inference(conn, assessment, inference_dedup_key="C7:u:v2")
    assert _count(conn, RUNS) == 2


def test_pg_at_most_one_active_inference_per_user_and_competency(conn, assessment):
    """U1/C7 active x2 refusé ; U1/C8 et U2/C7 autorisés ; candidate,
    superseded, obsolete multiples autorisés ; garanti à l'UPDATE ; bascule
    possible dans une même transaction."""
    _completed(conn, assessment, interpretation_status="active")
    _refused(conn, ONE_ACTIVE_INDEX, lambda: _completed(conn, assessment, interpretation_status="active"))
    _completed(conn, assessment, competency_code="C8", interpretation_status="active")
    _completed(conn, assessment, user_id=OTHER_USER, interpretation_status="active")
    for status in ("candidate", "candidate", "superseded", "superseded", "obsolete", "obsolete"):
        _completed(conn, assessment, interpretation_status=status)
    candidate = _completed(conn, assessment)
    promote = sa.text(f"UPDATE {RUNS} SET interpretation_status = 'active' WHERE id = :id")
    _refused(conn, ONE_ACTIVE_INDEX, lambda: conn.execute(promote, {"id": candidate}))
    conn.execute(sa.text(
        f"UPDATE {RUNS} SET interpretation_status = 'superseded' "
        "WHERE user_id = :u AND competency_code = 'C7' AND interpretation_status = 'active'"
    ), {"u": USER})
    conn.execute(promote, {"id": candidate})
    assert conn.execute(sa.text(
        f"SELECT id FROM {RUNS} WHERE user_id = :u AND competency_code = 'C7' "
        "AND interpretation_status = 'active'"), {"u": USER}).scalars().all() == [candidate]
    assert _count(conn, RUNS, "interpretation_status = 'active'") == 3


def test_pg_concurrent_active_inferences_are_serialized(pg_url, pg_engine):
    """L'invariant tient sous concurrence : la seconde transaction attend la
    première (index unique) puis échoue quand celle-ci commite."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with pg_engine.begin() as setup:
        release_id = _release(setup)
        assessment_id = _assessment(setup, release_id)
    try:
        with pg_engine.connect() as holder, pg_engine.connect() as waiter:
            holder_pid, waiter_pid = _backend_pid(holder), _backend_pid(waiter)
            holder.commit()
            waiter.commit()
            holder.begin()
            _completed(holder, assessment_id, interpretation_status="active", inference_dedup_key="first")
            outcome = {}

            def work():
                try:
                    with waiter.begin():
                        _completed(waiter, assessment_id, interpretation_status="active",
                                   inference_dedup_key="second")
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
            assert check.execute(sa.text(f"SELECT inference_dedup_key FROM {RUNS}")).scalars().all() == ["first"]
    finally:
        with pg_engine.begin() as cleanup:
            cleanup.execute(sa.text(f"DELETE FROM {RUNS}"))
            cleanup.execute(sa.text(f"DELETE FROM {ASSESSMENTS}"))
            cleanup.execute(sa.text("DELETE FROM pedagogical_taxonomy_releases"))


# --- stage claims ----------------------------------------------------------------------------

def test_pg_claim_stage_vocabulary_excludes_non_etabli(conn, run):
    for stage in CLAIM_STAGES:
        _claim(conn, run, stage=stage)
    other = _completed(conn, _assessment(conn, _release(conn)))
    for value in ("non_etabli", "", "expert", "none", "Mastery", "application_uncertain"):
        _refused(conn, "ck_competency_stage_claims_stage", lambda: _claim(conn, other, stage=value))
    assert _count(conn, CLAIMS) == 4


@pytest.mark.parametrize("column, value", [("positive_basis_status", "weak"), ("positive_basis_status", "partial"),
                                           ("positive_basis_status", ""), ("basis_mode", "automatic"),
                                           ("basis_mode", "inferred"), ("basis_mode", "")])
def test_pg_claim_basis_vocabularies(conn, run, column, value):
    """Refusé par le vocabulaire (vérifié seul, sans la cohérence
    status / mode)."""
    _refused(conn, f"ck_competency_stage_claims_{column}", lambda: _claim(conn, run, **{column: value}),
             drop=((CLAIMS, BASIS_STATUS_MODE_CHECK),))


@pytest.mark.parametrize("status, mode, accepted", [
    ("not_established", "none", True),
    ("established", "direct", True),
    ("established", "implied_by_higher_claim", True),
    ("not_established", "direct", False),
    ("not_established", "implied_by_higher_claim", False),
    ("established", "none", False),
])
def test_pg_claim_basis_status_and_mode_coherence(conn, run, status, mode, accepted):
    def insert():
        return _claim(conn, run, positive_basis_status=status, basis_mode=mode)
    if accepted:
        insert()
        assert _count(conn, CLAIMS) == 1
    else:
        _refused(conn, BASIS_STATUS_MODE_CHECK, insert)
        assert _count(conn, CLAIMS) == 0


def test_pg_four_claims_per_run_implied_by_higher_claim(conn, run):
    """Application établie directement ; Compréhension et Discovery
    implied_by_higher_claim ; Mastery not_established. UNIQUE(run, stage) ;
    le même stade dans un autre run est libre. Le plus haut stade établi
    reste dérivé : aucune colonne ne le persiste."""
    _claim(conn, run, stage="discovery", basis_mode="implied_by_higher_claim", basis_summary=None)
    _claim(conn, run, stage="comprehension", basis_mode="implied_by_higher_claim", basis_summary=None)
    _claim(conn, run, stage="application", basis_mode="direct",
           confidence_profile='{"diagnosticity": "deux démonstrations autonomes"}')
    _claim(conn, run, stage="mastery", positive_basis_status="not_established", basis_mode="none",
           mastery_assessment='{"transfer_variety": "un seul contexte", "longitudinality": "trois semaines"}')
    _refused(conn, CLAIM_UNIQUE, lambda: _claim(conn, run, stage="application"))
    _claim(conn, _completed(conn, _assessment(conn, _release(conn))), stage="application")
    assert _count(conn, CLAIMS, "inference_run_id = :r", {"r": run}) == 4
    assert "highest_positive_stage" not in COLUMNS[RUNS] + COLUMNS[CLAIMS]


def test_pg_known_limit_claim_completeness_is_left_to_t6b(conn, run):
    """LIMITE DOCUMENTÉE (T6-B / T6-C) : un run avec une seule claim, zéro
    claim, ou un profil de confiance sur une claim not_established est
    accepté. Le JSONB est libre (aucun mini-schéma en base)."""
    _claim(conn, run, stage="mastery", positive_basis_status="not_established", basis_mode="none",
           confidence_profile='{"coverage": "non pertinent"}')
    _completed(conn, _assessment(conn, _release(conn)))
    assert _count(conn, CLAIMS) == 1


def test_pg_claim_foreign_key(conn):
    _refused(conn, "competency_stage_claims_inference_run_id_fkey", lambda: _claim(conn, uuid.uuid4()))


# --- tensions ----------------------------------------------------------------------------------

@pytest.mark.parametrize("column, values, refused", [
    ("fragilized_stage", CLAIM_STAGES, ("non_etabli", "", "expert", "none")),
    ("scope_mode", TENSION_SCOPE_MODES, ("", "whole_observation", "global", "Localized")),
    ("revision_status", REVISION_STATUSES, ("", "resolved", "closed", "penalized")),
])
def test_pg_tension_vocabularies(conn, run, column, values, refused):
    for value in values:
        _tension(conn, run, **{column: value})
    for value in refused:
        _refused(conn, f"ck_competency_inference_tensions_{column}",
                 lambda: _tension(conn, run, **{column: value}))
    assert _count(conn, TENSIONS) == len(values)


def test_pg_tension_capability_scope(conn, release, run):
    """Même paire refusée (PK) ; même membership sur deux tensions ; FK
    tension et membership ; plusieurs lignes restent UNE tension."""
    c7_a = _membership(conn, release, _definition(conn, "C7_A"))
    c7_b = _membership(conn, release, _definition(conn, "C7_B"))
    first = _tension(conn, run, scope_mode="localized")
    second = _tension(conn, run, scope_mode="localized")
    _tension_scope(conn, first, c7_a)
    _tension_scope(conn, first, c7_b)
    _refused(conn, f"{TENSION_CAPS}_pkey", lambda: _tension_scope(conn, first, c7_a))
    _tension_scope(conn, second, c7_a)
    _refused(conn, "competency_inference_tension_capabilities_tension_id_fkey",
             lambda: _tension_scope(conn, uuid.uuid4(), c7_a))
    _refused(conn, TENSION_CAP_MEMBERSHIP_FK, lambda: _tension_scope(conn, first, uuid.uuid4()))
    _refused(conn, "competency_inference_tensions_inference_run_id_fkey", lambda: _tension(conn, uuid.uuid4()))
    assert (_count(conn, TENSION_CAPS), _count(conn, TENSIONS)) == (3, 2)


def test_pg_known_limit_tension_scope_coherence_is_left_to_t6b(conn, release, run):
    """LIMITE DOCUMENTÉE (T6-B) : localized sans ligne, whole_competency ou
    competency_only avec lignes, membership d'une autre release ou d'une
    autre compétence, tension sans claim positive : acceptés."""
    foreign = _membership(conn, _release(conn), _definition(conn, "C8_A"))
    local = _membership(conn, release, _definition(conn, "C7_A"))
    _tension(conn, run, scope_mode="localized")
    _tension_scope(conn, _tension(conn, run, scope_mode="whole_competency"), local)
    _tension_scope(conn, _tension(conn, run, scope_mode="competency_only"), foreign)
    assert (_count(conn, TENSIONS), _count(conn, TENSION_CAPS), _count(conn, CLAIMS)) == (3, 2, 0)


# --- basis refs ----------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", SOURCE_KINDS)
def test_pg_ref_single_source_accepted(conn, run, dossier, kind):
    """Une source seule, cohérente avec source_kind : acceptée (rôle
    run-level pour pouvoir tester les quatre sortes)."""
    _ref(conn, run, role="transition", kind=kind, sources={kind: dossier[kind]})
    assert _count(conn, REFS) == 1


@pytest.mark.parametrize("kind, provided", [
    (kind, provided)
    for kind in SOURCE_KINDS
    for provided in ((), *(tuple(sorted({kind, other})) for other in SOURCE_KINDS if other != kind),
                     *((other,) for other in SOURCE_KINDS if other != kind), tuple(SOURCE_KINDS))
])
def test_pg_ref_source_xor_refused(conn, run, dossier, kind, provided):
    """0 source, 2 sources, 4 sources ou source ne correspondant pas à
    source_kind (ex. observation + source_dependency_id) : refusé."""
    _refused(conn, SOURCE_XOR_CHECK, lambda: _ref(conn, run, role="transition", kind=kind,
                                                  sources={k: dossier[k] for k in provided}))
    assert _count(conn, REFS) == 0


@pytest.mark.parametrize("kind", ["", "Observation", "support_trace", "relation", "stage", "claim"])
def test_pg_ref_unknown_source_kind_is_refused(conn, run, dossier, kind):
    """Refusé par le vocabulaire ET par le XOR, chacun vérifié seul."""
    def insert():
        return _ref(conn, run, role="transition", kind=kind, sources={"observation": dossier["observation"]})
    _refused(conn, SOURCE_KIND_CHECK, insert, drop=((REFS, SOURCE_XOR_CHECK),))
    _refused(conn, SOURCE_XOR_CHECK, insert, drop=((REFS, SOURCE_KIND_CHECK),))


def test_pg_ref_role_and_dimension_vocabularies(conn, run, dossier):
    claim = _claim(conn, run)
    observation = {"observation": dossier["observation"]}
    for value in ("", "evidence", "proof", "score", "Positive_basis"):
        _refused(conn, REF_ROLE_CHECK, lambda: _ref(conn, run, role=value, sources=observation))
    for value in ("overall", "score", "level", "freshness", "", "Coverage"):
        _refused(conn, DIMENSION_CHECK,
                 lambda: _ref(conn, run, role="confidence", claim=claim, dimension=value, sources=observation))
    assert _count(conn, REFS) == 0


def test_pg_positive_basis_provenance(conn, run, dossier):
    """positive_basis + observation + stage_claim : accepté ; transfert,
    dépendance ou revalidation, sans claim, avec tension ou avec dimension :
    refusé. Seule une observation soutient directement une claim."""
    claim, tension = _claim(conn, run), _tension(conn, run)
    _ref(conn, run, claim=claim, sources={"observation": dossier["observation"]})
    for kind in ("transfer", "dependency", "revalidation"):
        _refused(conn, POSITIVE_BASIS_CHECK,
                 lambda: _ref(conn, run, claim=claim, kind=kind, sources={kind: dossier[kind]}))
    observation = {"observation": dossier["observation"]}
    _refused(conn, POSITIVE_BASIS_CHECK, lambda: _ref(conn, run, sources=observation))
    _refused(conn, POSITIVE_BASIS_CHECK, lambda: _ref(conn, run, claim=claim, tension=tension, sources=observation))
    _refused(conn, POSITIVE_BASIS_CHECK,
             lambda: _ref(conn, run, claim=claim, dimension="coverage", sources=observation))
    assert _count(conn, REFS) == 1


@pytest.mark.parametrize("kind", SOURCE_KINDS)
def test_pg_confidence_ref(conn, run, dossier, kind):
    """confidence : stage_claim_id + confidence_dimension requis (les cinq
    dimensions acceptées), tension_id absent ; toute source du dossier."""
    claim, tension = _claim(conn, run), _tension(conn, run)
    sources = {kind: dossier[kind]}
    for dimension in CONFIDENCE_DIMENSIONS:
        _ref(conn, run, role="confidence", claim=claim, dimension=dimension, kind=kind, sources=sources)
    _refused(conn, CONFIDENCE_REF_CHECK, lambda: _ref(conn, run, role="confidence", claim=claim, kind=kind,
                                                      sources=sources))
    _refused(conn, CONFIDENCE_REF_CHECK, lambda: _ref(conn, run, role="confidence", dimension="coverage",
                                                      kind=kind, sources=sources))
    _refused(conn, CONFIDENCE_REF_CHECK, lambda: _ref(conn, run, role="confidence", claim=claim, tension=tension,
                                                      dimension="coverage", kind=kind, sources=sources))
    assert _count(conn, REFS) == 5


@pytest.mark.parametrize("kind", SOURCE_KINDS)
def test_pg_tension_ref(conn, run, dossier, kind):
    """tension : tension_id requis ; stage_claim_id et confidence_dimension
    absents ; toute source du dossier."""
    claim, tension = _claim(conn, run), _tension(conn, run)
    sources = {kind: dossier[kind]}
    _ref(conn, run, role="tension", tension=tension, kind=kind, sources=sources)
    _refused(conn, TENSION_REF_CHECK, lambda: _ref(conn, run, role="tension", kind=kind, sources=sources))
    _refused(conn, TENSION_REF_CHECK, lambda: _ref(conn, run, role="tension", tension=tension, claim=claim,
                                                   kind=kind, sources=sources))
    _refused(conn, TENSION_REF_CHECK, lambda: _ref(conn, run, role="tension", tension=tension,
                                                   dimension="consistency", kind=kind, sources=sources))
    assert _count(conn, REFS) == 1


@pytest.mark.parametrize("kind", SOURCE_KINDS)
def test_pg_mastery_ref(conn, run, dossier, kind):
    """mastery : stage_claim_id requis ; tension_id et confidence_dimension
    absents."""
    mastery = _claim(conn, run, stage="mastery", positive_basis_status="not_established", basis_mode="none")
    tension = _tension(conn, run)
    sources = {kind: dossier[kind]}
    _ref(conn, run, role="mastery", claim=mastery, kind=kind, sources=sources)
    _refused(conn, MASTERY_REF_CHECK, lambda: _ref(conn, run, role="mastery", kind=kind, sources=sources))
    _refused(conn, MASTERY_REF_CHECK, lambda: _ref(conn, run, role="mastery", claim=mastery, tension=tension,
                                                   kind=kind, sources=sources))
    _refused(conn, MASTERY_REF_CHECK, lambda: _ref(conn, run, role="mastery", claim=mastery,
                                                   dimension="independence", kind=kind, sources=sources))
    assert _count(conn, REFS) == 1


@pytest.mark.parametrize("role", ["transition", "validation"])
@pytest.mark.parametrize("kind", SOURCE_KINDS)
def test_pg_run_level_refs(conn, run, dossier, role, kind):
    """transition / validation : refs run-level (ni claim, ni tension, ni
    dimension)."""
    claim, tension = _claim(conn, run), _tension(conn, run)
    sources = {kind: dossier[kind]}
    _ref(conn, run, role=role, kind=kind, sources=sources)
    for overrides in ({"claim": claim}, {"tension": tension}, {"dimension": "temporal_validation"}):
        _refused(conn, RUN_LEVEL_REF_CHECKS[role], lambda: _ref(conn, run, role=role, kind=kind, sources=sources,
                                                         **overrides))
    assert _count(conn, REFS) == 1


def test_pg_ref_foreign_keys(conn, run, dossier):
    claim = _claim(conn, run)
    observation = {"observation": dossier["observation"]}
    _refused(conn, "competency_inference_basis_refs_inference_run_id_fkey",
             lambda: _ref(conn, uuid.uuid4(), claim=claim, sources=observation))
    _refused(conn, "competency_inference_basis_refs_stage_claim_id_fkey",
             lambda: _ref(conn, run, claim=uuid.uuid4(), sources=observation))
    _refused(conn, "competency_inference_basis_refs_tension_id_fkey",
             lambda: _ref(conn, run, role="tension", tension=uuid.uuid4(), sources=observation))
    for kind in SOURCE_KINDS:
        _refused(conn, f"competency_inference_basis_refs_{SOURCE_COLUMNS[kind]}_fkey",
                 lambda: _ref(conn, run, role="validation", kind=kind, sources={kind: uuid.uuid4()}))
    assert _count(conn, REFS) == 0


def test_pg_refs_create_no_evidence(conn, run, dossier):
    """Plusieurs refs (positive, confiance, maîtrise, tension, transition,
    validation) vers les mêmes objets du dossier : aucune observation,
    relation T5 ni localisation créée ou modifiée (aucun trigger)."""
    observed = {t: conn.execute(sa.text(f"SELECT * FROM {t} ORDER BY 1")).all()
                for t in (OBS, DEPS, TRANSFERS, REVALS, "observation_capabilities", "longitudinal_assessment_inputs")}
    claim, tension = _claim(conn, run), _tension(conn, run)
    mastery = _claim(conn, run, stage="mastery", positive_basis_status="not_established", basis_mode="none")
    observation = {"observation": dossier["observation"]}
    _ref(conn, run, claim=claim, sources=observation)
    _ref(conn, run, claim=_claim(conn, run, stage="discovery", basis_mode="implied_by_higher_claim"),
         sources=observation)
    for kind in SOURCE_KINDS:
        _ref(conn, run, role="confidence", claim=claim, dimension="independence", kind=kind,
             sources={kind: dossier[kind]})
    _ref(conn, run, role="mastery", claim=mastery, kind="transfer", sources={"transfer": dossier["transfer"]})
    _ref(conn, run, role="tension", tension=tension, kind="revalidation",
         sources={"revalidation": dossier["revalidation"]})
    _ref(conn, run, role="transition", sources=observation)
    _ref(conn, run, role="validation", kind="dependency", sources={"dependency": dossier["dependency"]})
    assert _count(conn, REFS) == 10
    for table, rows in observed.items():
        assert conn.execute(sa.text(f"SELECT * FROM {table} ORDER BY 1")).all() == rows, table


def test_pg_known_limit_ref_coherence_is_left_to_t6b(conn, release, run, dossier):
    """LIMITE DOCUMENTÉE (T6-B) : la base accepte une source hors du
    dossier T5 consommé (autre run longitudinal, autre utilisateur), une
    claim ou une tension d'un autre run, une ref mastery vers une claim
    non-mastery."""
    other_run = _completed(conn, _assessment(conn, release, user_id=OTHER_USER, competency_code="C8"))
    foreign_claim, foreign_tension = _claim(conn, other_run), _tension(conn, other_run)
    outside = _observation(conn, "C8")
    _ref(conn, run, claim=foreign_claim, sources={"observation": outside})
    _ref(conn, run, role="tension", tension=foreign_tension, sources={"observation": outside})
    _ref(conn, run, role="mastery", claim=_claim(conn, run, stage="application"),
         sources={"observation": dossier["observation"]})
    assert _count(conn, REFS) == 3


# --- user_competency_states (cache) -----------------------------------------------------------------

def test_pg_user_state_primary_key_and_unique_active_run(conn, assessment):
    """PK (user_id, competency_code) ; active_inference_run_id UNIQUE ; FK
    user et run ; une ligne par compétence, jamais de niveau global."""
    c7 = _completed(conn, assessment, interpretation_status="active")
    _state(conn, c7)
    _refused(conn, f"{STATES}_pkey", lambda: _state(conn, _completed(conn, assessment)))
    _refused(conn, STATE_ACTIVE_UNIQUE, lambda: _state(conn, c7, competency_code="C8"))
    _refused(conn, STATE_ACTIVE_UNIQUE, lambda: _state(conn, c7, user_id=OTHER_USER))
    _state(conn, _completed(conn, assessment, competency_code="C8"), competency_code="C8", current_stage="discovery")
    _state(conn, _completed(conn, assessment, user_id=OTHER_USER), user_id=OTHER_USER,
           current_stage="non_etabli")
    _refused(conn, "user_competency_states_user_id_fkey",
             lambda: _state(conn, _completed(conn, assessment), user_id="inconnu"))
    _refused(conn, "user_competency_states_active_inference_run_id_fkey", lambda: _state(conn, uuid.uuid4(),
                                                                                         competency_code="C9"))
    assert _count(conn, STATES) == 3


@pytest.mark.parametrize("column, values, refused", [
    ("competency_code", COMPETENCY_CODES, ("C13", "C0", "", "global", "overall")),
    ("current_stage", CURRENT_STAGES, ("expert", "", "none", "application_uncertain", "3")),
    ("tension_state", TENSION_STATES, ("uncertain", "", "closed", "high")),
])
def test_pg_user_state_vocabularies(conn, assessment, column, values, refused):
    for index, value in enumerate(values):
        overrides = {column: value}
        if column != "competency_code":
            overrides["competency_code"] = COMPETENCY_CODES[index]
        _state(conn, _completed(conn, assessment), **overrides)
    for value in refused:
        _refused(conn, f"ck_user_competency_states_{column}",
                 lambda: _state(conn, _completed(conn, assessment), **{"competency_code": "C12", column: value}))
    assert _count(conn, STATES) == len(values)


def test_pg_user_state_generation_is_a_technical_bigint(conn, assessment):
    """state_generation : BIGINT NOT NULL, stocké tel quel (mutation
    définie en T6-B)."""
    _state(conn, _completed(conn, assessment), state_generation=2 ** 40)
    assert conn.execute(sa.text(f"SELECT state_generation FROM {STATES}")).scalar_one() == 2 ** 40


def test_pg_no_user_state_is_created_automatically(conn, assessment):
    """Aucune ligne créée par la migration ni par l'activation d'un run :
    absence de ligne = aucune inférence (distinct de non_etabli). Aucun
    trigger n'alimente le cache."""
    assert _count(conn, "users") == 2
    assert _count(conn, STATES) == 0
    _completed(conn, assessment, interpretation_status="active", current_stage="non_etabli")
    assert _count(conn, STATES) == 0
    assert conn.execute(sa.text("SELECT level FROM users WHERE id = :u"), {"u": USER}).scalar_one() == "debutant"


# --- aucune cascade destructive -----------------------------------------------------------------------

def test_pg_no_destructive_cascade(conn, release, assessment, dossier):
    """Supprimer un run T6, un run T5, une observation, une relation T5, un
    membership, un utilisateur, une claim ou une tension encore référencés
    est refusé ; rien n'est supprimé en cascade."""
    run_id = _completed(conn, assessment, interpretation_status="active")
    claim, tension = _claim(conn, run_id), _tension(conn, run_id, scope_mode="localized")
    membership = _membership(conn, release, _definition(conn, "C7_A"))
    _tension_scope(conn, tension, membership)
    _ref(conn, run_id, claim=claim, sources={"observation": dossier["observation"]})
    _ref(conn, run_id, role="tension", tension=tension, kind="transfer", sources={"transfer": dossier["transfer"]})
    for kind in ("dependency", "revalidation"):
        _ref(conn, run_id, role="validation", kind=kind, sources={kind: dossier[kind]})
    _state(conn, run_id)

    def delete(table, value):
        return lambda: conn.execute(sa.text(f"DELETE FROM {table} WHERE id = :id"), {"id": value})

    run_fks = [(t, f"{t}_inference_run_id_fkey") for t in (CLAIMS, TENSIONS, REFS)]
    run_fks.append((STATES, "user_competency_states_active_inference_run_id_fkey"))
    for table, fk in run_fks:
        _refused(conn, fk, delete(RUNS, run_id), drop=[(t, n) for t, n in run_fks if n != fk])
    _refused(conn, "competency_inference_runs_longitudinal_assessment_run_id_fkey", delete(ASSESSMENTS, assessment),
             drop=((DEPS, "observation_dependencies_run_id_fkey"), (TRANSFERS, "observation_transfers_run_id_fkey"),
                   (REVALS, "observation_revalidations_run_id_fkey"),
                   ("longitudinal_assessment_inputs", "longitudinal_assessment_inputs_run_id_fkey")))
    _refused(conn, "competency_inference_basis_refs_stage_claim_id_fkey", delete(CLAIMS, claim))
    _refused(conn, "competency_inference_basis_refs_tension_id_fkey", delete(TENSIONS, tension),
             drop=((TENSION_CAPS, "competency_inference_tension_capabilities_tension_id_fkey"),))
    _refused(conn, TENSION_CAP_MEMBERSHIP_FK, delete(MEMBERSHIPS, membership))
    for kind in ("dependency", "transfer", "revalidation"):
        table = {"dependency": DEPS, "transfer": TRANSFERS, "revalidation": REVALS}[kind]
        _refused(conn, f"competency_inference_basis_refs_{SOURCE_COLUMNS[kind]}_fkey", delete(table, dossier[kind]))
    _refused(conn, "competency_inference_basis_refs_source_observation_id_fkey", delete(OBS, dossier["observation"]),
             drop=((DEPS, "observation_dependencies_source_observation_id_fkey"),
                   (TRANSFERS, "observation_transfers_source_observation_id_fkey"),
                   ("longitudinal_assessment_inputs", "longitudinal_assessment_inputs_observation_id_fkey")))
    _refused(conn, "user_competency_states_user_id_fkey", lambda: conn.execute(
        sa.text("DELETE FROM users WHERE id = :id"), {"id": USER}),
        drop=[(t, f"{t}_user_id_fkey") for t in ("cognitive_events", "analysis_sessions", ASSESSMENTS, RUNS)])
    assert [_count(conn, t) for t in TABLES_IN_ORDER] == [1, 1, 1, 1, 4, 1]


# --- ORM ---------------------------------------------------------------------------------------------------

def test_pg_orm_insertion_uses_application_defaults(pg_url, pg_engine):
    """Insertion via l'ORM des six modèles : UUID, started_at, created_at et
    updated_at UTC aware générés côté application ; dict -> JSONB ; None
    Python sur un JSONB nullable -> NULL SQL (jamais le JSON null) ; PK
    composites."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with Session(pg_engine) as session:
        with session.begin():
            connection = session.connection()
            release_id = _release(connection)
            membership = _membership(connection, release_id, _definition(connection, "C7_A"))
            assessment_id = _assessment(connection, release_id, interpretation_status="active")
            observation = _observation(connection)
            run = CompetencyInferenceRun(
                user_id=USER, competency_code="C7", longitudinal_assessment_run_id=assessment_id,
                execution_status="running", interpretation_status="candidate", trigger="manual_replay",
                positive_basis_version="pb1", confidence_profile_version="cp1", state_decision_version="sd1",
                validation_version="v1", inference_schema_version="is1", evaluator_version="e1",
                input_fingerprint="sha256:in", inference_dedup_key="orm", validation_needs=None,
            )
            session.add(run)
            session.flush()
            claim = CompetencyStageClaim(inference_run_id=run.id, stage="application",
                                         positive_basis_status="established", basis_mode="direct",
                                         confidence_profile={"coverage": "deux capacités sur trois"})
            tension = CompetencyInferenceTension(inference_run_id=run.id, fragilized_stage="application",
                                                 scope_mode="localized", summary="Levier mal interprété",
                                                 revision_status="revalidation_needed")
            session.add_all([claim, tension])
            session.flush()
            ref = CompetencyInferenceBasisRef(inference_run_id=run.id, stage_claim_id=claim.id,
                                              ref_role="positive_basis", source_kind="observation",
                                              source_observation_id=observation)
            scope = CompetencyInferenceTensionCapability(tension_id=tension.id, capability_membership_id=membership)
            state = UserCompetencyState(user_id=USER, competency_code="C7", active_inference_run_id=run.id,
                                        current_stage="application", tension_state="open", state_generation=1)
            session.add_all([ref, scope, state])
            session.flush()
            for obj in (run, claim, tension, ref):
                session.refresh(obj)
                assert isinstance(obj.id, uuid.UUID)
                assert obj.created_at.utcoffset().total_seconds() == 0
            session.refresh(state)
            assert run.started_at.utcoffset().total_seconds() == 0
            assert state.updated_at.utcoffset().total_seconds() == 0
            assert (run.current_stage, run.completed_at, run.output_fingerprint) == (None, None, None)
            assert connection.execute(sa.text(
                f"SELECT validation_needs IS NULL FROM {RUNS} WHERE id = :id"), {"id": run.id}).scalar_one()
            assert claim.confidence_profile == {"coverage": "deux capacités sur trois"}
            assert claim.mastery_assessment is None
            assert session.get(CompetencyInferenceTensionCapability, (tension.id, membership)) is not None
            assert session.get(UserCompetencyState, (USER, "C7")) is state
            session.rollback()
    with pg_engine.connect() as c:
        assert [_count(c, t) for t in TABLES_IN_ORDER] == [0] * 6


def test_pg_models_timestamps_match_utc(pg_url, pg_engine):
    _upgrade_head_with_users(pg_url, pg_engine)
    with Session(pg_engine) as session:
        with session.begin():
            assessment_id = _assessment(session.connection(), _release(session.connection()))
            completed = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
            run = CompetencyInferenceRun(
                user_id=USER, competency_code="C12", longitudinal_assessment_run_id=assessment_id,
                execution_status="completed", interpretation_status="active", trigger="t",
                current_stage="non_etabli", tension_state="none", positive_basis_version="p",
                confidence_profile_version="c", state_decision_version="s", validation_version="v",
                inference_schema_version="i", evaluator_version="e", input_fingerprint="f",
                inference_dedup_key="k", completed_at=completed,
            )
            session.add(run)
            session.flush()
            session.refresh(run)
            assert run.completed_at == completed
            assert run.created_at.utcoffset().total_seconds() == 0
            session.rollback()


# --- upgrade / downgrade ------------------------------------------------------------------------------

EXISTING = REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES | {"alembic_version"}
DATA_TABLES = REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES


def _seed_t5(engine) -> uuid.UUID:
    """Un run longitudinal active (schéma 0008) avec snapshot, dépendance,
    transfert, revalidation et leurs périmètres, sur les données T0-T4 de
    _upgrade_to_0007_with_data."""
    with engine.begin() as conn:
        release_id = uuid.UUID("66666666-6666-6666-6666-666666666666")
        run_id = _assessment(conn, release_id, id=uuid.UUID("99999999-9999-9999-9999-999999999999"),
                             interpretation_status="active")
        supportive = uuid.UUID("55555555-5555-5555-5555-555555555555")
        contradiction = uuid.UUID("88888888-8888-8888-8888-888888888888")
        _input(conn, run_id, supportive)
        _input(conn, run_id, contradiction)
        membership = conn.execute(sa.text(f"SELECT id FROM {MEMBERSHIPS}")).scalar_one()
        edge = _dependency(conn, run_id, supportive, source_trace=uuid.UUID("33333333-3333-3333-3333-333333333333"))
        _scope(conn, "dependency_capabilities", edge, membership)
        _scope(conn, "transfer_capabilities", _transfer(conn, run_id, contradiction, supportive), membership)
        _scope(conn, "revalidation_capabilities", _revalidation(conn, run_id, contradiction, supportive), membership)
    return run_id


def test_pg_upgrade_0008_to_0009_preserves_everything_then_downgrade(pg_url, pg_engine):
    """0008 -> 0009 conserve schéma, catalogue et données de TOUTES les
    tables T0-T5 (users compris : aucun état généré pour les utilisateurs
    existants) ; les six tables T6 sont vides ; aucun ENUM, trigger,
    fonction ni séquence ajouté. Le downgrade supprime exactement T6 et
    restaure exactement 0008 ; le ré-upgrade produit la même structure."""
    _upgrade_to_0007_with_data(pg_url, pg_engine)
    _run_alembic(pg_url, "upgrade", T5A)
    assessment_id = _seed_t5(pg_engine)
    schema_0008 = _snapshot(pg_engine, EXISTING)
    catalog_0008 = _catalog(pg_engine, EXISTING)
    global_0008 = _global_catalog(pg_engine)
    data_0008 = _data(pg_engine, DATA_TABLES)
    assert all(data_0008[t] for t in DATA_TABLES)

    # --- upgrade 0008 -> 0009 --------------------------------------------
    _run_alembic(pg_url, "upgrade", T6A)
    assert _version(pg_engine) == T6A
    assert _tables(pg_engine) == EXISTING | T6A_TABLES
    assert _snapshot(pg_engine, EXISTING) == schema_0008
    assert _catalog(pg_engine, EXISTING) == catalog_0008
    assert _data(pg_engine, DATA_TABLES) == data_0008
    # Aucun backfill : ni run, ni claim, ni état (pas même non_etabli).
    assert _data(pg_engine, T6A_TABLES) == {name: [] for name in sorted(T6A_TABLES)}
    assert _global_catalog(pg_engine) == global_0008
    assert (global_0008["enums"], global_0008["triggers"], global_0008["functions"]) == (0, 0, 0)
    assert _without_r1b_changes(_without_r1c1_changes(_compare_metadata(pg_engine)), events_created=True) == []
    schema_t6 = _snapshot(pg_engine, T6A_TABLES)
    catalog_t6 = _catalog(pg_engine, T6A_TABLES)

    # Données T6 (supprimées par le downgrade, comme leurs tables).
    with pg_engine.begin() as conn:
        user_id = conn.execute(sa.text(f"SELECT user_id FROM {ASSESSMENTS} WHERE id = :id"),
                               {"id": assessment_id}).scalar_one()
        run_id = _completed(conn, assessment_id, user_id=user_id, interpretation_status="active")
        claim = _claim(conn, run_id)
        tension = _tension(conn, run_id, scope_mode="localized")
        membership = conn.execute(sa.text(f"SELECT id FROM {MEMBERSHIPS}")).scalar_one()
        _tension_scope(conn, tension, membership)
        _ref(conn, run_id, claim=claim, sources={"observation": uuid.UUID("55555555-5555-5555-5555-555555555555")})
        _state(conn, run_id, user_id=user_id)
    assert all(_data(pg_engine, T6A_TABLES)[t] for t in T6A_TABLES)

    # --- downgrade 0009 -> 0008 ------------------------------------------
    _run_alembic(pg_url, "downgrade", T5A)
    assert _version(pg_engine) == T5A
    assert _tables(pg_engine) == EXISTING
    _assert_absent(pg_engine, [*T6A_TABLES, *T6A_INDEXES])
    assert _snapshot(pg_engine, EXISTING) == schema_0008
    assert _catalog(pg_engine, EXISTING) == catalog_0008
    assert _global_catalog(pg_engine) == global_0008
    assert _data(pg_engine, DATA_TABLES) == data_0008

    # --- ré-upgrade 0008 -> 0009 -----------------------------------------
    _run_alembic(pg_url, "upgrade", T6A)
    assert _version(pg_engine) == T6A
    assert _snapshot(pg_engine, EXISTING) == schema_0008
    assert _snapshot(pg_engine, T6A_TABLES) == schema_t6
    assert _catalog(pg_engine, T6A_TABLES) == catalog_t6
    assert _data(pg_engine, DATA_TABLES) == data_0008
    assert _data(pg_engine, T6A_TABLES) == {name: [] for name in sorted(T6A_TABLES)}
    assert _without_r1b_changes(_without_r1c1_changes(_compare_metadata(pg_engine)), events_created=True) == []
