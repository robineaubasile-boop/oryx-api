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
    assert set(Base.metadata.tables) == (REMAINING_TABLES | T2A_TABLES | T3A_TABLES | T4A_TABLES | T5A_TABLES
                                        | T6A_TABLES | R1B_TABLES)
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


def _compare_metadata(engine) -> list:
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


def _assert_metadata_matches_0004(engine) -> None:
    """Au schéma 0004, Base.metadata ne diffère que par les tables de T2-A,
    T3-A, T4-A, T5-A, T6-A et R1-B, créées seulement en 0005, 0006, 0007,
    0008, 0009 et 0010 ; tout le reste correspond exactement."""
    diff = _without_r1b_changes(_compare_metadata(engine), events_created=False)
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
    assert dropped_constraints_0003 == [
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
