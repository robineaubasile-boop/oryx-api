"""Tests de T1-C2 : migration 0004_drop_company_analyses + suppression du
modèle CompanyAnalysis.

Même organisation que tests/test_migration_0002_analysis_sessions.py et
tests/test_migration_0003_analysis_session_links.py (dont on réutilise les
helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, intégrité de 0001,
   0002 et 0003, SQL PostgreSQL généré en mode offline, métadonnées des
   modèles, absence de CompanyAnalysis dans le code applicatif.

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t1c2_test \\
           python -m pytest tests/test_migration_0004_drop_company_analyses.py

Les parcours Décrypte / GET /theses / DELETE /theses sur le schéma 0004
sont couverts par tests/test_t1c1_decryptage_sessions.py (schéma =
`alembic upgrade head`).
"""
import ast
import hashlib
import subprocess
import uuid

import pytest
import sqlalchemy as sa

import core.models
from core.db import Base
from tests.pg_not_null import not_null_columns, without_verified_not_null
from tests.test_migration_0002_analysis_sessions import (
    BASELINE,
    BASELINE_SHA256,
    HISTORICAL_TABLES,
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

T1C2 = "0004_drop_company_analyses"
DROPPED = "company_analyses"
REMAINING_TABLES = (HISTORICAL_TABLES - {DROPPED}) | {"analysis_sessions"}
# Tables de T2-A (0005), T3-A (0006), T4-A (0007), T5-A (0008) et T6-A
# (0009), testées à part : déclarées dans les modèles, absentes du schéma
# 0004.
T2A_TABLES = {"cognitive_events", "support_traces"}
T3A_TABLES = {"observation_evaluation_runs", "pedagogical_observations"}
T4A_TABLES = {"pedagogical_taxonomy_releases", "core_capability_definitions",
              "capability_taxonomy_memberships", "observation_capabilities"}
# Index hors table (unique partiel) de T3-A : autogenerate le signale à part
# (add_index) tant que la table n'existe pas.
T3A_INDEXES = {"uq_observation_evaluation_runs_one_active_event"}
# Index de T4-A (0007) : unique partiel « une seule release active » et index
# des FK côté enfant (dont celui ajouté à observation_evaluation_runs).
T4A_INDEXES = {
    "uq_pedagogical_taxonomy_releases_one_active",
    "ix_capability_taxonomy_memberships_capability_definition_id",
    "ix_observation_capabilities_capability_membership_id",
    "ix_observation_evaluation_runs_pedagogical_taxonomy_release_id",
}
# Tables de T5-A (0008) : relations longitudinales du Niveau 5.
T5A_TABLES = {"longitudinal_assessment_runs", "longitudinal_assessment_inputs",
              "observation_dependencies", "dependency_capabilities",
              "observation_transfers", "transfer_capabilities",
              "observation_revalidations", "revalidation_capabilities"}
# Index de T5-A (0008) : unique partiel « un seul run longitudinal active par
# (user_id, competency_code) » et index des FK côté enfant.
T5A_INDEXES = {
    "uq_longitudinal_assessment_runs_one_active_user_competency",
    "ix_longitudinal_assessment_runs_user_id",
    "ix_longitudinal_assessment_runs_taxonomy_release_id",
    "ix_longitudinal_assessment_inputs_observation_id",
    "ix_observation_dependencies_run_id",
    "ix_observation_dependencies_target_observation_id",
    "ix_observation_dependencies_source_observation_id",
    "ix_observation_dependencies_source_support_trace_id",
    "ix_dependency_capabilities_capability_membership_id",
    "ix_observation_transfers_run_id",
    "ix_observation_transfers_source_observation_id",
    "ix_observation_transfers_target_observation_id",
    "ix_transfer_capabilities_capability_membership_id",
    "ix_observation_revalidations_run_id",
    "ix_observation_revalidations_source_contradiction",
    "ix_observation_revalidations_target_supportive",
    "ix_revalidation_capabilities_capability_membership_id",
}
# R1-B (0010, testé à part) : registre d'appartenance des conversations et
# identité idempotente des CognitiveEvents (quatre colonnes nullable,
# task_kind nullable, UNIQUE event_dedup_key).
R1B_TABLES = {"conversation_identities"}
R1B_EVENT_COLUMNS = ("event_dedup_key", "event_builder_version", "admission_version", "event_schema_version")
R1B_DEDUP_UNIQUE = "uq_cognitive_events_event_dedup_key"
# R1-C1 (0011, testé à part) : livraisons idempotentes des réponses
# assistant (une table runtime, ni SupportTrace ni CognitiveEvent).
R1C1_TABLES = {"assistant_deliveries"}
# R1-C2 (0012, testé à part) : liens cognitifs T2 des tours Décrypter (une
# table runtime et son index unique partiel).
R1C2_TABLES = {"decryptage_cognitive_links"}
R1C2_INDEXES = {"uq_decryptage_cognitive_links_opening_event"}
# R1-C4 (0013, testé à part) : affinité conversationnelle Décrypter (une
# colonne nullable + sa FK sur decryptage_cognitive_links, CHECK étendus).
# Identifiant de révision court (alembic_version.version_num VARCHAR(32)) ;
# fichier nommé selon la spec.
R1C4 = "0013_decryptage_conv_affinity"
R1C4_FILE = "0013_decryptage_conversation_affinity.py"
R1C4_LINK_COLUMN = "input_context_event_id"
R1C4_LINK_COLUMNS = (R1C4_LINK_COLUMN, "response_context_event_id")
R1C4_PROGRESS_COLUMN = "product_progress_action"
# R1-D1 (0014, testé à part) : lease persistée des runs d'évaluation (deux
# colonnes nullable sur observation_evaluation_runs, deux CHECK).
R1D1 = "0014_evaluation_run_leases"
R1D1_FILE = "0014_evaluation_run_leases.py"
R1D1_RUN_COLUMNS = ("lease_token", "lease_expires_at")
# R1 PR 1 (0015 à 0017, testés à part) : users.level_revision, registre
# d'idempotence portefeuille, onze tables F2 (dans l'ordre de création).
PR1_0015 = "0015_users_level_revision"
PR1_0015_FILE = "0015_users_level_revision.py"
PR1_0016 = "0016_portfolio_idempotency"
PR1_0016_FILE = "0016_portfolio_idempotency.py"
PR1_0017 = "0017_oryx_auth_foundations"
PR1_0017_FILE = "0017_oryx_auth_foundations.py"
PR1_FILES = [PR1_0015_FILE, PR1_0016_FILE, PR1_0017_FILE]
PR1_REVISIONS = {PR1_0015, PR1_0016, PR1_0017}
PR1_0016_TABLES = ("portfolio_write_idempotency",)
PR1_0017_TABLES = (
    "auth_identities", "invitations", "auth_operations", "auth_preauth_candidates", "auth_otp_requests",
    "auth_otp_verification_attempts", "auth_abuse_subjects", "auth_abuse_events", "oryx_sessions",
    "oryx_session_generations", "auth_operation_results",
)
PR1_0017_INDEXES = {
    "ix_auth_identities_user_id", "ix_invitations_invited_by_user_id", "uq_invitations_one_pending_email",
    "ix_auth_operations_invitation_id", "ix_auth_operations_auth_identity_id", "ix_auth_operations_user_id",
    "ix_auth_operations_target_user_id", "ix_auth_operations_initiating_session_id",
    "uq_auth_operations_one_committed_invitation", "uq_auth_preauth_candidates_one_open",
    "uq_auth_preauth_candidates_one_ever_bound", "uq_auth_otp_requests_one_live",
    "uq_auth_otp_verification_attempts_one_unresolved", "uq_auth_otp_verification_attempts_one_accepted",
    "ix_auth_abuse_events_window", "ix_auth_abuse_events_auth_operation_id", "ix_auth_abuse_events_otp_request_id",
    "ix_oryx_sessions_user_id",
}
PR1_TABLES = set(PR1_0016_TABLES + PR1_0017_TABLES)
# R1 PR 2 (0018, testée à part) : journal anti-abus F2 étendu (colonne
# auth_abuse_events.blocked_until, index unique partiel des marqueurs de
# refus, CHECK étendus) et table singleton auth_abuse_key_state. Tête.
PR2_0018 = "0018_f2_abuse_journal"
PR2_0018_FILE = "0018_f2_abuse_journal.py"
PR2_FILES = [PR2_0018_FILE]
PR2_REVISIONS = {PR2_0018}
PR2_0018_TABLES = ("auth_abuse_key_state",)
PR2_0018_INDEXES = {"uq_auth_abuse_events_refusal_bucket"}
PR2_0018_EVENT_COLUMN = "blocked_until"
PR2_TABLES = set(PR2_0018_TABLES)
# R1 PR 4 (0019, testée à part) : admission HTTP F2 (colonne
# auth_abuse_events.http_action, type auth_http_probe_attempt et CHECK
# étendus ; aucune table, aucun index).
PR4_0019 = "0019_f2_http_admission"
PR4_0019_FILE = "0019_f2_http_admission.py"
PR4_FILES = [PR4_0019_FILE]
PR4_0019_EVENT_COLUMN = "http_action"
# R1 PR 7 (0020, testée à part) : portfolio_positions.position_revision
# BIGINT NOT NULL DEFAULT 0 et son CHECK ; aucune table, aucun index.
PR7_0020 = "0020_position_revision"
PR7_0020_FILE = "0020_position_revision.py"
PR7_FILES = [PR7_0020_FILE]
PR7_0020_POSITION_COLUMN = "position_revision"
# R1 PR 9-B1 (0021, testée à part) : table decryptage_turn_reservations et
# UNIQUE composite uq_conversation_identities_key_owner. Identifiant de
# révision raccourci (VARCHAR(32)), fichier au nom autorisé. Tête.
PR9B1_0021 = "0021_decryptage_reservations"
PR9B1_0021_FILE = "0021_decryptage_turn_reservations.py"
PR9B1_FILES = [PR9B1_0021_FILE]
PR9B1_0021_TABLES = ("decryptage_turn_reservations",)
PR9B1_0021_OWNER_UNIQUE = "uq_conversation_identities_key_owner"
PR9B1_TABLES = set(PR9B1_0021_TABLES)
HEAD = PR9B1_0021
# Tables de T6-A (0009) : inférence de l'état C1-C12 du Niveau 6.
T6A_TABLES = {"competency_inference_runs", "competency_stage_claims",
              "competency_inference_tensions", "competency_inference_tension_capabilities",
              "competency_inference_basis_refs", "user_competency_states"}
# Index de T6-A (0009) : unique partiel « un seul run d'inférence active par
# (user_id, competency_code) » et index des FK côté enfant.
T6A_INDEXES = {
    "uq_competency_inference_runs_one_active_user_competency",
    "ix_competency_inference_runs_user_id",
    "ix_competency_inference_runs_longitudinal_assessment_run_id",
    "ix_competency_inference_runs_predecessor_inference_run_id",
    "ix_competency_inference_tensions_inference_run_id",
    "ix_competency_inference_tension_caps_membership_id",
    "ix_competency_inference_basis_refs_inference_run_id",
    "ix_competency_inference_basis_refs_stage_claim_id",
    "ix_competency_inference_basis_refs_tension_id",
    "ix_competency_inference_basis_refs_source_observation_id",
    "ix_competency_inference_basis_refs_source_dependency_id",
    "ix_competency_inference_basis_refs_source_transfer_id",
    "ix_competency_inference_basis_refs_source_revalidation_id",
}
# sha256 de alembic/versions/0003_analysis_session_links.py tel que mergé
# sur main (2f1845e, T1-C1) et déployé en production.
T1B1_SHA256 = "42303c53253fcf3264f7a4122651bba4f768e1719a1fc89dafc849bacad71e46"
NOT_EMPTY_MESSAGE = "T1-C2 : company_analyses n'est pas vide, DROP TABLE annulé."


def _statements(sql: str) -> list:
    """Instructions SQL du script offline (sans BEGIN/COMMIT ni commentaires).
    Alembic termine chaque instruction par ';' + fin de ligne : le bloc DO du
    garde-fou, qui contient des ';' internes, reste donc une seule instruction."""
    body = "\n".join(l for l in sql.splitlines() if not l.startswith("--"))
    stmts = [" ".join(s.split()) for s in body.split(";\n")]
    stmts = [s.rstrip(";") for s in stmts]
    return [s for s in stmts if s and s not in ("BEGIN", "COMMIT")]


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_0001_0002_0003_0004():
    # La tête de chaîne évolue avec les migrations suivantes (T2-A : voir
    # tests/test_migration_0005_cognitive_support_traces.py) ; on vérifie ici
    # les maillons 0001 -> 0002 -> 0003 -> 0004.
    script = _script_directory()
    assert script.get_bases() == [BASELINE]

    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert {BASELINE, T1A, T1B1, T1C2} <= set(revisions)
    assert revisions[T1C2].down_revision == T1B1
    assert revisions[T1B1].down_revision == T1A
    assert revisions[T1A].down_revision == BASELINE
    assert revisions[BASELINE].down_revision is None

    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[:4] == [f"{rev}.py" for rev in (BASELINE, T1A, T1B1, T1C2)]


def test_0001_0002_0003_files_are_unchanged():
    versions = REPO_ROOT / "alembic" / "versions"
    for rev, expected in ((BASELINE, BASELINE_SHA256), (T1A, T1A_SHA256), (T1B1, T1B1_SHA256)):
        assert hashlib.sha256((versions / f"{rev}.py").read_bytes()).hexdigest() == expected, rev


def test_offline_sql_of_0004_upgrade_is_guard_then_plain_drop():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "upgrade", f"{T1B1}:{T1C2}", "--sql",
    ).stdout
    stmts = _statements(sql)
    assert stmts[0] == "LOCK TABLE company_analyses IN ACCESS EXCLUSIVE MODE"
    # Garde-fou : table non vide -> exception, avant le DROP.
    assert stmts[1].startswith("DO $$ BEGIN IF EXISTS (SELECT 1 FROM company_analyses) THEN RAISE EXCEPTION ")
    assert stmts[1].endswith("n''est pas vide, DROP TABLE annulé. La table doit être vide avant la "
                             "migration 0004_drop_company_analyses.'; END IF; END $$")
    assert stmts[2:] == [
        "DROP TABLE company_analyses",
        "UPDATE alembic_version SET version_num='0004_drop_company_analyses' "
        "WHERE alembic_version.version_num = '0003_analysis_session_links'",
    ]
    assert "CASCADE" not in sql.upper()
    assert [s for s in stmts if s.upper().startswith("DROP")] == ["DROP TABLE company_analyses"]


def test_offline_sql_of_0004_downgrade_recreates_only_company_analyses():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "downgrade", f"{T1C2}:{T1B1}", "--sql",
    ).stdout
    assert _statements(sql) == [
        "CREATE TABLE company_analyses ( id SERIAL NOT NULL, user_id VARCHAR NOT NULL, "
        "ticker VARCHAR NOT NULL, current_step VARCHAR NOT NULL, "
        "created_at TIMESTAMP WITHOUT TIME ZONE, updated_at TIMESTAMP WITHOUT TIME ZONE, "
        "PRIMARY KEY (id), FOREIGN KEY(user_id) REFERENCES users (id) )",
        "UPDATE alembic_version SET version_num='0003_analysis_session_links' "
        "WHERE alembic_version.version_num = '0004_drop_company_analyses'",
    ]
    for forbidden in ("CASCADE", "INSERT", "UNIQUE", "CREATE INDEX", "ON DELETE"):
        assert forbidden not in sql.upper(), forbidden


def test_metadata_no_longer_declares_company_analyses():
    assert DROPPED not in Base.metadata.tables
    assert set(Base.metadata.tables) == PR1_TABLES | PR2_TABLES | (REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES
                                        | T6A_TABLES | R1B_TABLES | R1C1_TABLES | R1C2_TABLES)
    assert not hasattr(core.models, "CompanyAnalysis")
    for table in Base.metadata.tables.values():
        assert all(fk.column.table.name != DROPPED for fk in table.foreign_keys), table.name


def _code_tokens(source: str) -> str:
    """Identifiants et chaînes d'un module Python, hors docstrings et
    commentaires (qui peuvent légitimement raconter l'historique)."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)
    }
    tokens = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(node.attr)
        elif isinstance(node, ast.alias):
            tokens.append(node.name)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            tokens.append(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            tokens.append(node.value)
    return "\n".join(tokens)


def test_no_company_analysis_usage_in_application_code():
    """Aucun code applicatif ne référence CompanyAnalysis ; company_analyses
    n'apparaît plus (hors docstrings/commentaires) que dans l'historique des
    migrations et dans le manifeste figé du préflight T0-C1 (baseline
    pré-stamp, volontairement non modifié)."""
    allowed_table_mentions = {
        "alembic/versions/0001_current_oryx_baseline.py",
        "alembic/versions/0004_drop_company_analyses.py",
        "scripts/schema_preflight.py",
    }
    checked = 0
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if (path.suffix not in (".py", ".js", ".html") or not path.is_file()
                or rel.startswith(("tests/", ".git/")) or "node_modules" in path.parts):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            source = _code_tokens(source)
        if rel.startswith("alembic/versions/"):
            # L'identifiant de révision de 0004 (down_revision des migrations
            # suivantes) n'est pas un usage de la table.
            source = source.replace(T1C2, "")
        checked += 1
        assert "CompanyAnalysis" not in source, rel
        if rel not in allowed_table_mentions:
            assert DROPPED not in source, rel
    assert checked > 0


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

SESSION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _seed_remaining_tables(engine) -> None:
    """Données représentatives dans toutes les tables conservées (0003)."""
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level, created_at) VALUES "
                             "('u', 'debutant', '2026-01-01 10:00')"))
        conn.execute(sa.text("INSERT INTO portfolio_positions (user_id, ticker, quantity, created_at, updated_at) "
                             "VALUES ('u', 'AAPL', 3, '2026-01-02', '2026-01-02')"))
        conn.execute(sa.text(
            "INSERT INTO analysis_sessions (id, user_id, ticker, status, current_step, started_at, updated_at) "
            "VALUES (:id, 'u', 'AAPL', 'in_progress', 'moat', '2026-01-02+00', '2026-01-02+00')"
        ), {"id": SESSION_ID})
        for sid in (None, SESSION_ID):
            conn.execute(sa.text("INSERT INTO analysis_facts (user_id, ticker, fact_date, fact_type, fact_value, "
                                 "analysis_session_id) VALUES ('u', 'AAPL', '2026-01-02', 'roe', 0.2, :s)"),
                         {"s": sid})
            conn.execute(sa.text("INSERT INTO user_statements (user_id, ticker, step, statement_date, "
                                 "statement_text, analysis_session_id) "
                                 "VALUES ('u', 'AAPL', 'business', '2026-01-02', 'txt', :s)"), {"s": sid})
            conn.execute(sa.text("INSERT INTO investment_theses (user_id, ticker, thesis_text, created_at, "
                                 "analysis_session_id) VALUES ('u', 'AAPL', 'these', '2026-01-03', :s)"),
                         {"s": sid})


def _data(engine, tables) -> dict:
    with engine.connect() as conn:
        return {
            t: [tuple(r) for r in conn.execute(sa.text(f"SELECT * FROM {t} ORDER BY 1")).all()]
            for t in sorted(tables)
        }


def _constraints(engine, table) -> list:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT conname, contype, pg_get_constraintdef(oid), confdeltype, confupdtype "
            "FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) ORDER BY conname"
        ), {"t": f"public.{table}"}).all()]


def _catalog_columns(engine, table) -> list:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t "
            "ORDER BY ordinal_position"
        ), {"t": table}).all()]


def _indexes(engine, table) -> list:
    with engine.connect() as conn:
        return conn.execute(sa.text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"
        ), {"t": table}).scalars().all()


def _pr1_revisions_applied(engine) -> int:
    """Nombre de révisions R1 PR 1 (0015, 0016, 0017), R1 PR 2 (0018),
    R1 PR 4 (0019), R1 PR 7 (0020) puis R1 PR 9-B1 (0021) présentes en base."""
    with engine.connect() as conn:
        if conn.execute(sa.text("SELECT to_regclass('public.alembic_version')")).scalar() is None:
            return 0
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    return {PR1_0015: 1, PR1_0016: 2, PR1_0017: 3, PR2_0018: 4, PR4_0019: 5, PR7_0020: 6, PR9B1_0021: 7}.get(version, 0)


def _without_pr1_changes(diff, applied: int) -> list:
    """Retire du diff compare_metadata EXACTEMENT les écarts des révisions
    R1 PR 1, R1 PR 2, R1 PR 4 et R1 PR 7 non encore appliquées
    (users.level_revision, tables et index de 0016 / 0017, puis table,
    colonne et index de 0018, puis colonne de 0019, puis
    portfolio_positions.position_revision de 0020, puis table et UNIQUE
    composite de 0021), et échoue si l'un d'eux manque ou si un écart
    apparaît alors que sa révision est appliquée. Retourne le reste. Avant
    0017, les colonnes blocked_until et http_action arrivent avec la table
    auth_abuse_events (add_table)."""
    expected = []
    if applied < 1:
        expected.append(("add_column", "users.level_revision"))
    if applied < 2:
        expected += [("add_table", t) for t in PR1_0016_TABLES]
    if applied < 3:
        expected += [("add_table", t) for t in PR1_0017_TABLES] + [("add_index", i) for i in PR1_0017_INDEXES]
    if applied < 4:
        expected += [("add_table", t) for t in PR2_0018_TABLES] + [("add_index", i) for i in PR2_0018_INDEXES]
    if applied == 3:
        expected.append(("add_column", f"auth_abuse_events.{PR2_0018_EVENT_COLUMN}"))
    if 3 <= applied < 5:
        expected.append(("add_column", f"auth_abuse_events.{PR4_0019_EVENT_COLUMN}"))
    if applied < 6:
        expected.append(("add_column", f"portfolio_positions.{PR7_0020_POSITION_COLUMN}"))
    if applied < 7:
        expected += [("add_table", t) for t in PR9B1_0021_TABLES] + [("add_constraint", PR9B1_0021_OWNER_UNIQUE)]
    found, rest = [], []
    for d in diff:
        key = None
        if isinstance(d, tuple) and d[0] == "add_column" and d[2] == "users" and d[3].name == "level_revision":
            key = ("add_column", "users.level_revision")
        elif (isinstance(d, tuple) and d[0] == "add_column" and d[2] == "auth_abuse_events"
              and d[3].name == PR2_0018_EVENT_COLUMN and d[3].nullable):
            key = ("add_column", f"auth_abuse_events.{PR2_0018_EVENT_COLUMN}")
        elif (isinstance(d, tuple) and d[0] == "add_column" and d[2] == "auth_abuse_events"
              and d[3].name == PR4_0019_EVENT_COLUMN and d[3].nullable):
            key = ("add_column", f"auth_abuse_events.{PR4_0019_EVENT_COLUMN}")
        elif (isinstance(d, tuple) and d[0] == "add_column" and d[2] == "portfolio_positions"
              and d[3].name == PR7_0020_POSITION_COLUMN and not d[3].nullable):
            key = ("add_column", f"portfolio_positions.{PR7_0020_POSITION_COLUMN}")
        elif (isinstance(d, tuple) and d[0] == "add_constraint" and isinstance(d[1], sa.UniqueConstraint)
              and d[1].name == PR9B1_0021_OWNER_UNIQUE and d[1].table.name == "conversation_identities"
              and [col.name for col in d[1].columns] == ["conversation_key", "user_id"]):
            key = ("add_constraint", PR9B1_0021_OWNER_UNIQUE)
        elif isinstance(d, tuple) and d[0] == "add_table" and d[1].name in (
                PR1_0016_TABLES + PR1_0017_TABLES + PR2_0018_TABLES + PR9B1_0021_TABLES):
            key = ("add_table", d[1].name)
        elif isinstance(d, tuple) and d[0] == "add_index" and d[1].name in PR1_0017_INDEXES | PR2_0018_INDEXES:
            key = ("add_index", d[1].name)
        if key:
            found.append(key)
        else:
            rest.append(d)
    assert sorted(found) == sorted(expected), diff
    return rest


def _compare_metadata(engine) -> list:
    """compare_metadata(base, Base.metadata), sans les écarts R1 PR 1,
    R1 PR 2, R1 PR 4, R1 PR 7 et R1 PR 9-B1 des révisions 0015 à 0021 pas encore appliquées (retirés
    exactement par _without_pr1_changes). À la tête, aucun écart n'est
    retiré."""
    return _without_pr1_changes(_raw_compare_metadata(engine), _pr1_revisions_applied(engine))


def _raw_compare_metadata(engine) -> list:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)


def _without_r1b_changes(diff, *, events_created: bool) -> list:
    """Retire du diff compare_metadata d'un schéma antérieur à 0010
    EXACTEMENT les écarts introduits par R1-B, et échoue si l'un d'eux
    manque : la table conversation_identities et, si cognitive_events existe
    déjà (schéma >= 0005), ses quatre colonnes VARCHAR nullable, task_kind
    NOT NULL -> NULL et l'UNIQUE event_dedup_key. Retourne le reste."""
    found, rest = [], []
    for d in diff:
        if (isinstance(d, list) and len(d) == 1 and d[0][0] == "modify_nullable"
                and d[0][2:4] == ("cognitive_events", "task_kind") and d[0][5:] == (False, True)):
            found.append(("modify_nullable", "task_kind"))
        elif isinstance(d, tuple) and d[0] == "add_table" and d[1].name in R1B_TABLES:
            found.append(("add_table", d[1].name))
        elif (isinstance(d, tuple) and d[0] == "add_column" and d[2] == "cognitive_events"
              and d[3].name in R1B_EVENT_COLUMNS and d[3].nullable and type(d[3].type) is sa.String):
            found.append(("add_column", d[3].name))
        elif isinstance(d, tuple) and d[0] == "add_constraint" and d[1].name == R1B_DEDUP_UNIQUE:
            found.append(("add_constraint", d[1].name))
        else:
            rest.append(d)
    expected = [("add_table", t) for t in R1B_TABLES]
    if events_created:
        expected += [("add_column", c) for c in R1B_EVENT_COLUMNS]
        expected += [("modify_nullable", "task_kind"), ("add_constraint", R1B_DEDUP_UNIQUE)]
    assert sorted(found) == sorted(expected)
    return rest


def _without_r1c1_changes(diff) -> list:
    """Retire du diff compare_metadata d'un schéma antérieur à 0011
    EXACTEMENT l'écart introduit par R1-C1 (la table assistant_deliveries),
    et échoue s'il manque. Retourne le reste."""
    found = [d for d in diff if isinstance(d, tuple) and d[0] == "add_table" and d[1].name in R1C1_TABLES]
    assert sorted(d[1].name for d in found) == sorted(R1C1_TABLES)
    return [d for d in diff if not any(d is f for f in found)]


def _without_r1c2_changes(diff) -> list:
    """Retire du diff compare_metadata d'un schéma antérieur à 0012
    EXACTEMENT l'écart introduit par R1-C2 (la table
    decryptage_cognitive_links et son index unique partiel), et échoue s'il
    manque. Retourne le reste."""
    found = [d for d in diff if isinstance(d, tuple) and (
        (d[0] == "add_table" and d[1].name in R1C2_TABLES) or (d[0] == "add_index" and d[1].name in R1C2_INDEXES))]
    assert sorted((d[0], d[1].name) for d in found) == sorted(
        [("add_table", t) for t in R1C2_TABLES] + [("add_index", i) for i in R1C2_INDEXES])
    return [d for d in diff if not any(d is f for f in found)]


def _without_r1c4_changes(diff) -> list:
    """Retire du diff compare_metadata d'un schéma 0012 EXACTEMENT les
    écarts introduits par R1-C4 (colonnes input_context_event_id et
    response_context_event_id de decryptage_cognitive_links et leurs FK,
    colonne product_progress_action ; les CHECK ne sont pas comparés par
    Alembic), et échoue s'ils manquent.
    Retourne le reste."""
    def is_r1c4(d):
        if not isinstance(d, tuple):
            return False
        if d[0] == "add_column":
            return (d[2] == "decryptage_cognitive_links"
                    and d[3].name in (*R1C4_LINK_COLUMNS, R1C4_PROGRESS_COLUMN))
        if d[0] == "add_fk":
            return (d[1].parent.name == "decryptage_cognitive_links"
                    and len(d[1].columns) == 1 and [c.name for c in d[1].columns][0] in R1C4_LINK_COLUMNS)
        return False

    found = [d for d in diff if is_r1c4(d)]
    assert sorted(d[0] for d in found) == ["add_column", "add_column", "add_column", "add_fk", "add_fk"], diff
    return [d for d in diff if not is_r1c4(d)]


def _without_r1d1_changes(diff) -> list:
    """Retire du diff compare_metadata d'un schéma 0006..0013 EXACTEMENT les
    écarts introduits par R1-D1 (colonnes lease_token et lease_expires_at
    d'observation_evaluation_runs ; les CHECK ne sont pas comparés par
    Alembic), et échoue s'ils manquent. Retourne le reste."""
    def is_r1d1(d):
        return (isinstance(d, tuple) and d[0] == "add_column" and d[2] == "observation_evaluation_runs"
                and d[3].name in R1D1_RUN_COLUMNS and d[3].nullable)

    found = [d for d in diff if is_r1d1(d)]
    assert sorted(d[3].name for d in found) == sorted(R1D1_RUN_COLUMNS), diff
    return [d for d in diff if not is_r1d1(d)]


def _assert_metadata_matches_0004(engine) -> None:
    """Au schéma 0004, Base.metadata ne diffère que par les tables de T2-A,
    T3-A, T4-A, T5-A, T6-A et R1-B, créées seulement en 0005, 0006, 0007,
    0008, 0009 et 0010 ; tout le reste correspond exactement."""
    diff = _without_r1b_changes(_without_r1c1_changes(_without_r1c2_changes(_compare_metadata(engine))),
                                events_created=False)
    assert sorted((d[0], d[1].name) for d in diff) == sorted(
        [("add_table", t) for t in T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES | T6A_TABLES]
        + [("add_index", i) for i in T3A_INDEXES | T4A_INDEXES | T5A_INDEXES | T6A_INDEXES]
    )


def _failed_upgrade(pg_url) -> subprocess.CalledProcessError:
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _run_alembic(pg_url, "upgrade", T1C2)
    return exc.value


def test_pg_upgrade_from_empty_database_to_0004(pg_url, pg_engine):
    # La tête est 0005 depuis T2-A (testée à part) ; on vérifie ici 0004.
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T1C2)
    assert _version(pg_engine) == T1C2
    assert _tables(pg_engine) == REMAINING_TABLES | {"alembic_version"}
    _assert_metadata_matches_0004(pg_engine)


def test_pg_upgrade_0003_to_0004_drops_only_company_analyses_then_downgrade(pg_url, pg_engine):
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T1B1)
    _seed_remaining_tables(pg_engine)
    schema_0003 = _snapshot(pg_engine, REMAINING_TABLES)
    dropped_0003 = _snapshot(pg_engine, {DROPPED})
    dropped_constraints_0003 = _constraints(pg_engine, DROPPED)
    dropped_columns_0003 = _catalog_columns(pg_engine, DROPPED)
    data_0003 = _data(pg_engine, REMAINING_TABLES)
    assert _data(pg_engine, {DROPPED}) == {DROPPED: []}

    # Référence de la structure 0003 (identique à 0001).
    assert dropped_columns_0003 == [
        ("id", "integer", "NO", "nextval('company_analyses_id_seq'::regclass)"),
        ("user_id", "character varying", "NO", None),
        ("ticker", "character varying", "NO", None),
        ("current_step", "character varying", "NO", None),
        ("created_at", "timestamp without time zone", "YES", None),
        ("updated_at", "timestamp without time zone", "YES", None),
    ]
    assert without_verified_not_null(pg_engine, DROPPED, dropped_constraints_0003,
                                     not_null_columns(dropped_columns_0003)) == [
        ("company_analyses_pkey", "p", "PRIMARY KEY (id)", " ", " "),
        ("company_analyses_user_id_fkey", "f", "FOREIGN KEY (user_id) REFERENCES users(id)", "a", "a"),
    ]
    assert _indexes(pg_engine, DROPPED) == ["company_analyses_pkey"]

    # --- upgrade 0003 -> 0004 (table vide) --------------------------------
    _run_alembic(pg_url, "upgrade", T1C2)
    assert _version(pg_engine) == T1C2
    assert _tables(pg_engine) == REMAINING_TABLES | {"alembic_version"}
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT to_regclass('public.company_analyses')")).scalar_one() is None
        assert conn.execute(sa.text(
            "SELECT to_regclass('public.company_analyses_id_seq')"
        )).scalar_one() is None
    # Toutes les autres tables : schéma et données strictement inchangés.
    assert _snapshot(pg_engine, REMAINING_TABLES) == schema_0003
    assert _data(pg_engine, REMAINING_TABLES) == data_0003
    _assert_metadata_matches_0004(pg_engine)

    # --- downgrade 0004 -> 0003 ------------------------------------------
    _run_alembic(pg_url, "downgrade", T1B1)
    assert _version(pg_engine) == T1B1
    assert _tables(pg_engine) == REMAINING_TABLES | {DROPPED, "alembic_version"}
    assert _snapshot(pg_engine, {DROPPED}) == dropped_0003
    assert _catalog_columns(pg_engine, DROPPED) == dropped_columns_0003
    assert _constraints(pg_engine, DROPPED) == dropped_constraints_0003
    assert _indexes(pg_engine, DROPPED) == ["company_analyses_pkey"]
    # Structure seulement : aucune ligne restaurée.
    assert _data(pg_engine, {DROPPED}) == {DROPPED: []}
    assert _snapshot(pg_engine, REMAINING_TABLES) == schema_0003
    assert _data(pg_engine, REMAINING_TABLES) == data_0003

    # --- ré-upgrade 0003 -> 0004 -----------------------------------------
    _run_alembic(pg_url, "upgrade", T1C2)
    assert _version(pg_engine) == T1C2
    assert _tables(pg_engine) == REMAINING_TABLES | {"alembic_version"}
    assert _snapshot(pg_engine, REMAINING_TABLES) == schema_0003
    assert _data(pg_engine, REMAINING_TABLES) == data_0003
    _assert_metadata_matches_0004(pg_engine)


def test_pg_upgrade_aborts_before_drop_when_company_analyses_is_not_empty(pg_url, pg_engine):
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T1B1)
    _seed_remaining_tables(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO company_analyses (user_id, ticker, current_step, created_at, updated_at) "
                             "VALUES ('u', 'AAPL', 'moat', '2026-01-02', '2026-01-03')"))
    schema_before = _snapshot(pg_engine, REMAINING_TABLES | {DROPPED})
    data_before = _data(pg_engine, REMAINING_TABLES | {DROPPED})

    err = _failed_upgrade(pg_url)
    assert "RuntimeError" in err.stderr
    assert NOT_EMPTY_MESSAGE in err.stderr
    assert "(1 ligne(s) présente(s))" in err.stderr

    # Rien n'a changé : version, table, ligne, autres tables.
    assert _version(pg_engine) == T1B1
    assert DROPPED in _tables(pg_engine)
    assert _snapshot(pg_engine, REMAINING_TABLES | {DROPPED}) == schema_before
    assert _data(pg_engine, REMAINING_TABLES | {DROPPED}) == data_before


def test_pg_offline_guard_aborts_before_drop_when_company_analyses_is_not_empty(pg_url, pg_engine):
    """Le script généré en mode --sql porte le même garde-fou."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T1B1)
    with pg_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('u', 'debutant')"))
        conn.execute(sa.text("INSERT INTO company_analyses (user_id, ticker, current_step) "
                             "VALUES ('u', 'AAPL', 'moat')"))
    sql = _run_alembic(pg_url, "upgrade", f"{T1B1}:{T1C2}", "--sql").stdout
    body = _statements(sql)
    assert body[2] == "DROP TABLE company_analyses"
    with pg_engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(sa.exc.DBAPIError, match="n'est pas vide, DROP TABLE annulé"):
                for stmt in body:
                    conn.exec_driver_sql(stmt)
        finally:
            trans.rollback()
    assert _version(pg_engine) == T1B1
    assert _data(pg_engine, {DROPPED})[DROPPED][0][1:4] == ("u", "AAPL", "moat")


def test_pg_upgrade_fails_cleanly_on_unexpected_dependency(pg_url, pg_engine):
    """Pas de CASCADE : une dépendance inattendue (ici une FK entrante)
    bloque le DROP ; la migration échoue sans rien supprimer."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T1B1)
    with pg_engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE t1c2_probe (id serial PRIMARY KEY, "
            "company_analysis_id integer REFERENCES company_analyses(id))"
        ))
    schema_before = _snapshot(pg_engine, REMAINING_TABLES | {DROPPED, "t1c2_probe"})

    err = _failed_upgrade(pg_url)
    assert "DependentObjectsStillExist" in err.stderr

    assert _version(pg_engine) == T1B1
    assert _snapshot(pg_engine, REMAINING_TABLES | {DROPPED, "t1c2_probe"}) == schema_before

    # Dépendance retirée : la migration passe.
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE t1c2_probe"))
    _run_alembic(pg_url, "upgrade", T1C2)
    assert _version(pg_engine) == T1C2
    assert DROPPED not in _tables(pg_engine)
