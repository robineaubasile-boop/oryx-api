"""Tests de T1-B1 : migration 0003_analysis_session_links.

Même organisation que tests/test_migration_0002_analysis_sessions.py
(dont on réutilise les helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, intégrité de 0001
   et 0002, SQL PostgreSQL généré en mode offline, métadonnées des modèles.

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t1a_test \\
           python -m pytest tests/test_migration_0003_analysis_session_links.py
"""
import hashlib
import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core.db import Base
from core.models import AnalysisFact, AnalysisSession, InvestmentThesis, UserStatement
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

T1B1 = "0003_analysis_session_links"
LINKED_TABLES = ("analysis_facts", "user_statements", "investment_theses")
UNLINKED_TABLES = HISTORICAL_TABLES - set(LINKED_TABLES)
LINKED_MODELS = (AnalysisFact, UserStatement, InvestmentThesis)
# sha256 de alembic/versions/0002_analysis_sessions.py tel que mergé sur
# main (e8837eb, PR #172) et déployé en production.
T1A_SHA256 = "bfd7799fd8a28ddd78129d33c93e3918b154009ca6804c08bde1ba29e8f7aeb0"


def _fk_name(table: str) -> str:
    return f"{table}_analysis_session_id_fkey"


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_0001_0002_0003():
    # La tête de chaîne évolue avec les migrations suivantes (T1-C2 : voir
    # tests/test_migration_0004_drop_company_analyses.py) ; on vérifie ici
    # seulement les maillons 0001 -> 0002 -> 0003.
    script = _script_directory()
    assert script.get_bases() == [BASELINE]

    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert {BASELINE, T1A, T1B1} <= set(revisions)
    assert revisions[T1B1].down_revision == T1A
    assert revisions[T1A].down_revision == BASELINE
    assert revisions[BASELINE].down_revision is None


def test_0001_and_0002_files_are_unchanged():
    versions = REPO_ROOT / "alembic" / "versions"
    assert hashlib.sha256((versions / f"{BASELINE}.py").read_bytes()).hexdigest() == BASELINE_SHA256
    assert hashlib.sha256((versions / f"{T1A}.py").read_bytes()).hexdigest() == T1A_SHA256


def _statements(sql: str) -> list:
    """Instructions SQL du script offline (sans BEGIN/COMMIT ni commentaires)."""
    body = "\n".join(l for l in sql.splitlines() if not l.startswith("--"))
    stmts = [" ".join(s.split()) for s in body.split(";")]
    return [s for s in stmts if s and s not in ("BEGIN", "COMMIT")]


def test_offline_sql_of_0003_upgrade_is_exactly_three_columns_and_three_fks():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "upgrade", f"{T1A}:{T1B1}", "--sql",
    ).stdout
    assert _statements(sql) == [
        *(f"ALTER TABLE {t} ADD COLUMN analysis_session_id UUID" for t in LINKED_TABLES),
        *(
            f"ALTER TABLE {t} ADD CONSTRAINT {_fk_name(t)} "
            "FOREIGN KEY(analysis_session_id) REFERENCES analysis_sessions (id)"
            for t in LINKED_TABLES
        ),
        # Seule écriture de données : la version Alembic (aucun backfill).
        "UPDATE alembic_version SET version_num='0003_analysis_session_links' "
        "WHERE alembic_version.version_num = '0002_analysis_sessions'",
    ]


def test_offline_sql_of_0003_downgrade_drops_only_the_fks_then_the_columns():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "downgrade", f"{T1B1}:{T1A}", "--sql",
    ).stdout
    assert _statements(sql) == [
        *(f"ALTER TABLE {t} DROP CONSTRAINT {_fk_name(t)}" for t in LINKED_TABLES),
        *(f"ALTER TABLE {t} DROP COLUMN analysis_session_id" for t in LINKED_TABLES),
        "UPDATE alembic_version SET version_num='0002_analysis_sessions' "
        "WHERE alembic_version.version_num = '0003_analysis_session_links'",
    ]


def test_models_declare_nullable_uuid_fk_to_analysis_sessions():
    for model in LINKED_MODELS:
        table = model.__table__
        col = table.c.analysis_session_id
        # Colonne ajoutée en dernier, comme ADD COLUMN.
        assert list(table.c.keys())[-1] == "analysis_session_id", table.name
        assert isinstance(col.type, sa.Uuid), table.name
        assert col.type.as_uuid is True, table.name
        assert col.nullable is True, table.name
        assert col.default is None and col.server_default is None, table.name
        assert not col.unique and not col.index, table.name

        fks = list(col.foreign_keys)
        assert len(fks) == 1, table.name
        assert fks[0].target_fullname == "analysis_sessions.id", table.name
        assert fks[0].ondelete is None and fks[0].onupdate is None, table.name
        # Même type que la PK ciblée.
        assert type(fks[0].column.type) is type(col.type)

        assert not table.indexes, table.name
        assert not [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)], table.name


def test_only_the_three_linked_models_changed():
    # company_analyses n'a plus de modèle depuis T1-C2 (0004).
    for name in (UNLINKED_TABLES - {"company_analyses"}) | {"analysis_sessions"}:
        assert "analysis_session_id" not in Base.metadata.tables[name].c, name
    expected_columns = {
        "analysis_facts": ["id", "user_id", "ticker", "fact_date", "fact_type", "fact_value"],
        "user_statements": ["id", "user_id", "ticker", "step", "statement_date", "statement_text"],
        "investment_theses": ["id", "user_id", "ticker", "thesis_text", "created_at"],
    }
    for name, legacy in expected_columns.items():
        assert list(Base.metadata.tables[name].c.keys()) == [*legacy, "analysis_session_id"], name
    assert not hasattr(AnalysisSession, "facts")  # pas de relationship ORM en T1-B1


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

LEGACY_SESSION_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _seed_historical_rows(engine) -> None:
    """Lignes représentatives de l'historique, écrites en 0002 avec les
    seules colonnes que connaît le code legacy."""
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level, created_at) VALUES "
                             "('hist', 'debutant', '2026-01-01 10:00')"))
        conn.execute(sa.text("INSERT INTO company_analyses (user_id, ticker, current_step, created_at, updated_at) "
                             "VALUES ('hist', 'AAPL', 'construction_these', '2026-01-02', '2026-01-03')"))
        # Une AnalysisSession existe déjà pour le même (user, ticker) : 0003
        # ne doit PAS pour autant y rattacher les lignes historiques.
        conn.execute(sa.text(
            "INSERT INTO analysis_sessions (id, user_id, ticker, status, started_at, updated_at) "
            "VALUES (:id, 'hist', 'AAPL', 'in_progress', '2026-01-02+00', '2026-01-02+00')"
        ), {"id": LEGACY_SESSION_ID})
        for i in range(3):
            conn.execute(sa.text(
                "INSERT INTO analysis_facts (user_id, ticker, fact_date, fact_type, fact_value) "
                "VALUES ('hist', 'AAPL', :d, :t, :v)"
            ), {"d": datetime(2026, 1, 2 + i), "t": f"fact_{i}", "v": float(i)})
            conn.execute(sa.text(
                "INSERT INTO user_statements (user_id, ticker, step, statement_date, statement_text) "
                "VALUES ('hist', 'AAPL', 'construction_these', :d, :s)"
            ), {"d": datetime(2026, 1, 2 + i), "s": f"statement {i}"})
        conn.execute(sa.text("INSERT INTO investment_theses (user_id, ticker, thesis_text, created_at) "
                             "VALUES ('hist', 'AAPL', 'these historique', '2026-01-04')"))


def _rows(engine, table: str, columns: list) -> list:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY 1, 2"
        )).all()]


def _all_rows(engine) -> dict:
    """Contenu complet des tables présentes en 0002 (colonnes de 0002)."""
    insp = sa.inspect(engine)
    snap = _snapshot(engine, HISTORICAL_TABLES | {"analysis_sessions"})
    return {
        t: _rows(engine, t, [c for c, *_ in snap[t]["columns"] if c != "analysis_session_id"])
        for t in sorted(insp.get_table_names()) if t != "alembic_version"
    }


def test_pg_upgrade_from_empty_database_to_0003(pg_url, pg_engine):
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T1B1)
    assert _version(pg_engine) == T1B1
    assert _tables(pg_engine) == HISTORICAL_TABLES | {"analysis_sessions", "alembic_version"}


def test_pg_upgrade_0002_to_0003_preserves_history_then_downgrade(pg_url, pg_engine):
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T1A)
    _seed_historical_rows(pg_engine)
    schema_0002 = _snapshot(pg_engine, HISTORICAL_TABLES | {"analysis_sessions"})
    data_0002 = _all_rows(pg_engine)

    # --- upgrade 0002 -> 0003 -------------------------------------------
    _run_alembic(pg_url, "upgrade", T1B1)
    assert _version(pg_engine) == T1B1
    assert _tables(pg_engine) == HISTORICAL_TABLES | {"analysis_sessions", "alembic_version"}
    schema_0003 = _snapshot(pg_engine, HISTORICAL_TABLES | {"analysis_sessions"})

    # Tables non concernées : strictement identiques.
    for t in UNLINKED_TABLES | {"analysis_sessions"}:
        assert schema_0003[t] == schema_0002[t], t
    # Tables liées : exactement une colonne en fin de table + une FK en plus.
    for t in LINKED_TABLES:
        before, after = schema_0002[t], schema_0003[t]
        assert after["columns"] == before["columns"] + [("analysis_session_id", "UUID", True, None)], t
        for key in ("pk", "uniques", "checks", "indexes"):
            assert after[key] == before[key], (t, key)
        new_fks = [fk for fk in after["fks"] if fk not in before["fks"]]
        assert [fk for fk in after["fks"] if fk in before["fks"]] == before["fks"], t
        assert len(new_fks) == 1, t
        assert new_fks[0]["name"] == _fk_name(t)
        assert new_fks[0]["constrained_columns"] == ["analysis_session_id"]
        assert new_fks[0]["referred_table"] == "analysis_sessions"
        assert new_fks[0]["referred_columns"] == ["id"]

    with pg_engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT table_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'analysis_session_id' "
            "ORDER BY table_name"
        )).all()
        assert [tuple(r) for r in rows] == [(t, "uuid", "YES", None) for t in sorted(LINKED_TABLES)]

        # FK nouvelles : NO ACTION en suppression et mise à jour (pas de cascade).
        fks = conn.execute(sa.text(
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid), confdeltype, confupdtype "
            "FROM pg_constraint WHERE contype = 'f' "
            "AND confrelid = 'public.analysis_sessions'::regclass ORDER BY 1"
        )).all()
        assert [tuple(r) for r in fks] == [
            (t, _fk_name(t), "FOREIGN KEY (analysis_session_id) REFERENCES analysis_sessions(id)", "a", "a")
            for t in sorted(LINKED_TABLES)
        ]
        # Aucune FK de la base n'est en cascade / set null / set default.
        assert conn.execute(sa.text(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
            "AND (confdeltype <> 'a' OR confupdtype <> 'a')"
        )).scalar_one() == 0

    # Historique intact : mêmes lignes, analysis_session_id NULL partout,
    # aucune session créée (pas de backfill).
    assert _all_rows(pg_engine) == data_0002
    with pg_engine.connect() as conn:
        for t in LINKED_TABLES:
            total, linked = conn.execute(sa.text(
                f"SELECT count(*), count(analysis_session_id) FROM {t}"
            )).one()
            assert total > 0 and linked == 0, t
        assert conn.execute(sa.text("SELECT count(*) FROM analysis_sessions")).scalar_one() == 1

    # Les modèles SQLAlchemy correspondent exactement au schéma migré, à
    # l'exception de company_analyses, encore présente en 0003 mais dont le
    # modèle est supprimé depuis T1-C2 (la table disparaît en 0004), et des
    # tables de T2-A et T3-A (et de l'index unique partiel de T3-A),
    # déclarées dans les modèles mais créées seulement en 0005 et 0006.
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    with pg_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert sorted((d[0], d[1].name) for d in diff) == [
        ("add_index", "uq_observation_evaluation_runs_one_active_event"),
        ("add_table", "cognitive_events"),
        ("add_table", "observation_evaluation_runs"),
        ("add_table", "pedagogical_observations"),
        ("add_table", "support_traces"),
        ("remove_table", "company_analyses"),
    ]

    # --- compatibilité avec le code legacy (transaction annulée) ---------
    with Session(pg_engine) as session:
        with session.begin():
            # Mêmes constructions que api.py (_track_construction_these_progress),
            # sans analysis_session_id.
            session.add(UserStatement(user_id="hist", ticker="MSFT", step="x", statement_text="legacy"))
            session.add(InvestmentThesis(user_id="hist", ticker="MSFT", thesis_text="legacy"))
            session.add(AnalysisFact(user_id="hist", ticker="MSFT", fact_type="roe", fact_value=1.0,
                                     fact_date=datetime(2026, 2, 1)))
            session.flush()
            for model in LINKED_MODELS:
                objs = session.query(model).filter(model.ticker == "MSFT").all()
                assert len(objs) == 1 and objs[0].analysis_session_id is None, model
            session.rollback()

    with pg_engine.connect() as conn:
        trans = conn.begin()
        try:
            # INSERT listant uniquement les colonnes connues de l'ancien code
            # (ce qu'émet l'ORM d'avant T1-B1).
            conn.execute(sa.text("INSERT INTO analysis_facts (user_id, ticker, fact_date, fact_type, fact_value) "
                                 "VALUES ('hist', 'MSFT', now(), 'roe', 1.0)"))
            conn.execute(sa.text("INSERT INTO user_statements (user_id, ticker, step, statement_date, statement_text) "
                                 "VALUES ('hist', 'MSFT', 'x', now(), 'legacy')"))
            conn.execute(sa.text("INSERT INTO investment_theses (user_id, ticker, thesis_text, created_at) "
                                 "VALUES ('hist', 'MSFT', 'legacy', now())"))

            # Rattachement explicite à une session existante : accepté.
            for t in LINKED_TABLES:
                conn.execute(sa.text(f"UPDATE {t} SET analysis_session_id = :s WHERE ticker = 'MSFT'"),
                             {"s": LEGACY_SESSION_ID})
            # Session inexistante : refusé par la FK.
            for t in LINKED_TABLES:
                with pytest.raises(sa.exc.IntegrityError, match=_fk_name(t)):
                    with conn.begin_nested():
                        conn.execute(sa.text(f"UPDATE {t} SET analysis_session_id = :s WHERE ticker = 'MSFT'"),
                                     {"s": uuid.uuid4()})
            # Pas de cascade : supprimer une session référencée est refusé.
            with pytest.raises(sa.exc.IntegrityError, match="_analysis_session_id_fkey"):
                with conn.begin_nested():
                    conn.execute(sa.text("DELETE FROM analysis_sessions WHERE id = :s"),
                                 {"s": LEGACY_SESSION_ID})
        finally:
            trans.rollback()
    assert _all_rows(pg_engine) == data_0002

    # --- downgrade 0003 -> 0002 ------------------------------------------
    _run_alembic(pg_url, "downgrade", T1A)
    assert _version(pg_engine) == T1A
    assert _snapshot(pg_engine, HISTORICAL_TABLES | {"analysis_sessions"}) == schema_0002
    assert _all_rows(pg_engine) == data_0002

    # --- ré-upgrade -------------------------------------------------------
    _run_alembic(pg_url, "upgrade", T1B1)
    assert _version(pg_engine) == T1B1
    assert _snapshot(pg_engine, HISTORICAL_TABLES | {"analysis_sessions"}) == schema_0003
    assert _all_rows(pg_engine) == data_0002
