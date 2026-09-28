"""Tests de T2-A : migration 0005_cognitive_support_traces + modèles
CognitiveEvent / SupportTrace.

Même organisation que les tests des migrations 0002 à 0004 (dont on
réutilise les helpers) :

1. Tests sans base (toujours exécutés) : chaîne Alembic, intégrité de 0001
   à 0004, SQL PostgreSQL généré en mode offline, métadonnées des modèles,
   absence de tout branchement applicatif.

2. Tests contre un vrai PostgreSQL, uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t2a_test \\
           python -m pytest tests/test_migration_0005_cognitive_support_traces.py

T2-A = schéma + modèles seulement : aucune route ne lit ni n'écrit ces
tables (la capture arrive en T2-B).
"""
import hashlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from core.db import Base
from core.models import AnalysisSession, CognitiveEvent, SupportTrace, User
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
    REMAINING_TABLES,
    SESSION_ID,
    T1B1_SHA256,
    T1C2,
    T3A_INDEXES,
    T3A_TABLES,
    _assert_metadata_matches_0004,
    _catalog_columns,
    _code_tokens,
    _compare_metadata,
    _constraints,
    _data,
    _indexes,
    _seed_remaining_tables,
)

T2A = "0005_cognitive_support_traces"
EVENTS = "cognitive_events"
TRACES = "support_traces"
T2A_TABLES = {EVENTS, TRACES}
# sha256 de alembic/versions/0004_drop_company_analyses.py tel que mergé
# sur main (7955301, T1-C2) et déployé en production.
T1C2_SHA256 = "b7179c4b3dcd7010bff0abd7dfdf58fe94a08191645ec1bc9525ebb18f688d28"

EVENT_COLUMNS = [
    "id",
    "user_id",
    "analysis_session_id",
    "event_origin",
    "task_kind",
    "status",
    "conversation_key",
    "stimulus_snapshot",
    "user_work_snapshot",
    "started_at",
    "updated_at",
    "closed_at",
]
TRACE_COLUMNS = [
    "id",
    "cognitive_event_id",
    "sequence_no",
    "support_kind",
    "support_payload",
    "created_at",
]
EVENT_STATUSES = ("open", "finalized", "abandoned")
STATUS_CHECK_SQL = "status IN ('open', 'finalized', 'abandoned')"
JSONB_COLUMNS = {
    (EVENTS, "stimulus_snapshot"),
    (EVENTS, "user_work_snapshot"),
    (TRACES, "support_payload"),
}
TIMESTAMPTZ_COLUMNS = {
    (EVENTS, "started_at"),
    (EVENTS, "updated_at"),
    (EVENTS, "closed_at"),
    (TRACES, "created_at"),
}


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_revision_chain_is_0001_to_0005():
    """(1)(2) 0001 -> 0002 -> 0003 -> 0004 -> 0005. La tête de chaîne évolue
    avec les migrations suivantes (T3-A : voir
    tests/test_migration_0006_observation_layer.py) ; on vérifie ici les
    maillons jusqu'à 0005."""
    script = _script_directory()
    assert script.get_bases() == [BASELINE]

    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert {BASELINE, T1A, T1B1, T1C2, T2A} <= set(revisions)
    assert revisions[T2A].down_revision == T1C2
    assert revisions[T1C2].down_revision == T1B1
    assert revisions[T1B1].down_revision == T1A
    assert revisions[T1A].down_revision == BASELINE
    assert revisions[BASELINE].down_revision is None

    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[:5] == [f"{rev}.py" for rev in (BASELINE, T1A, T1B1, T1C2, T2A)]


def test_revision_id_fits_alembic_version_column():
    """alembic_version.version_num est un VARCHAR(32) (créé par Alembic, y
    compris par le stamp de production) : un identifiant plus long ferait
    échouer l'upgrade à l'écriture de la version."""
    for rev in _script_directory().walk_revisions():
        assert len(rev.revision) <= 32, rev.revision


def test_0001_to_0004_files_are_unchanged():
    """(3) Migrations historiques immuables."""
    versions = REPO_ROOT / "alembic" / "versions"
    for rev, expected in (
        (BASELINE, BASELINE_SHA256),
        (T1A, T1A_SHA256),
        (T1B1, T1B1_SHA256),
        (T1C2, T1C2_SHA256),
    ):
        assert hashlib.sha256((versions / f"{rev}.py").read_bytes()).hexdigest() == expected, rev


def test_offline_sql_of_0005_upgrade_creates_exactly_the_two_tables():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "upgrade", f"{T1C2}:{T2A}", "--sql",
    ).stdout
    assert _statements(sql) == [
        "CREATE TABLE cognitive_events ( id UUID NOT NULL, user_id VARCHAR NOT NULL, "
        "analysis_session_id UUID, event_origin VARCHAR NOT NULL, task_kind VARCHAR NOT NULL, "
        "status VARCHAR NOT NULL, conversation_key VARCHAR, stimulus_snapshot JSONB NOT NULL, "
        "user_work_snapshot JSONB NOT NULL, started_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL, closed_at TIMESTAMP WITH TIME ZONE, "
        "PRIMARY KEY (id), "
        "CONSTRAINT ck_cognitive_events_status CHECK (status IN ('open', 'finalized', 'abandoned')), "
        "FOREIGN KEY(user_id) REFERENCES users (id), "
        "FOREIGN KEY(analysis_session_id) REFERENCES analysis_sessions (id) )",
        "CREATE TABLE support_traces ( id UUID NOT NULL, cognitive_event_id UUID NOT NULL, "
        "sequence_no INTEGER NOT NULL, support_kind VARCHAR NOT NULL, "
        "support_payload JSONB NOT NULL, created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "PRIMARY KEY (id), "
        "FOREIGN KEY(cognitive_event_id) REFERENCES cognitive_events (id), "
        "CONSTRAINT uq_support_traces_event_sequence UNIQUE (cognitive_event_id, sequence_no) )",
        # Seule écriture de données : la version Alembic (aucun backfill).
        "UPDATE alembic_version SET version_num='0005_cognitive_support_traces' "
        "WHERE alembic_version.version_num = '0004_drop_company_analyses'",
    ]
    for forbidden in ("ALTER TABLE", "DROP ", "INSERT", "CREATE INDEX", "CREATE TYPE",
                      "CREATE EXTENSION", "CREATE TRIGGER", "CREATE FUNCTION", "DEFAULT",
                      "ON DELETE", "ON UPDATE", "CASCADE", "ENUM"):
        assert forbidden not in sql.upper(), forbidden


def test_offline_sql_of_0005_downgrade_drops_only_the_two_tables():
    sql = _run_alembic(
        "postgresql://offline@localhost/offline",
        "downgrade", f"{T2A}:{T1C2}", "--sql",
    ).stdout
    assert _statements(sql) == [
        "DROP TABLE support_traces",
        "DROP TABLE cognitive_events",
        "UPDATE alembic_version SET version_num='0004_drop_company_analyses' "
        "WHERE alembic_version.version_num = '0005_cognitive_support_traces'",
    ]
    assert "CASCADE" not in sql.upper()


def test_metadata_declares_the_two_new_tables():
    """(4) Base.metadata = tables de 0004 + cognitive_events + support_traces
    (+ les tables de T3-A, testées à part)."""
    assert set(Base.metadata.tables) == REMAINING_TABLES | T2A_TABLES | T3A_TABLES
    assert CognitiveEvent.__table__ is Base.metadata.tables[EVENTS]
    assert SupportTrace.__table__ is Base.metadata.tables[TRACES]


def test_cognitive_event_columns_types_nullability_and_fks():
    """(5) Types, nullabilité et FK exacts de cognitive_events."""
    table = CognitiveEvent.__table__
    assert [c.name for c in table.columns] == EVENT_COLUMNS
    cols = table.columns

    assert [c.name for c in table.primary_key.columns] == ["id"]
    for name in ("id", "analysis_session_id"):
        assert isinstance(cols[name].type, sa.Uuid), name
        assert cols[name].type.as_uuid is True, name
    for name in ("user_id", "event_origin", "task_kind", "status", "conversation_key"):
        assert type(cols[name].type) is sa.String, name
        assert cols[name].type.length is None, name

    assert {c.name: c.nullable for c in table.columns} == {
        "id": False,
        "user_id": False,
        "analysis_session_id": True,
        "event_origin": False,
        "task_kind": False,
        "status": False,
        "conversation_key": True,
        "stimulus_snapshot": False,
        "user_work_snapshot": False,
        "started_at": False,
        "updated_at": False,
        "closed_at": True,
    }

    fks = sorted((fk.parent.name, fk.target_fullname) for fk in table.foreign_keys)
    assert fks == [("analysis_session_id", "analysis_sessions.id"), ("user_id", "users.id")]
    # Mêmes types que les PK ciblées (users.id reste VARCHAR).
    for fk in table.foreign_keys:
        assert type(fk.column.type) is type(fk.parent.type), fk.parent.name
    assert type(Base.metadata.tables["users"].c.id.type) is sa.String


def test_support_trace_columns_types_nullability_and_fks():
    """(5) Types, nullabilité et FK exacts de support_traces."""
    table = SupportTrace.__table__
    assert [c.name for c in table.columns] == TRACE_COLUMNS
    cols = table.columns

    assert [c.name for c in table.primary_key.columns] == ["id"]
    for name in ("id", "cognitive_event_id"):
        assert isinstance(cols[name].type, sa.Uuid), name
        assert cols[name].type.as_uuid is True, name
    assert type(cols.sequence_no.type) is sa.Integer
    assert type(cols.support_kind.type) is sa.String
    assert cols.support_kind.type.length is None

    assert {c.name: c.nullable for c in table.columns} == {name: False for name in TRACE_COLUMNS}
    assert cols.sequence_no.autoincrement in ("auto", False)
    assert not cols.sequence_no.primary_key

    fks = [(fk.parent.name, fk.target_fullname) for fk in table.foreign_keys]
    assert fks == [("cognitive_event_id", "cognitive_events.id")]


def test_only_status_check_on_cognitive_events():
    """(6)(7) Exactement un CHECK (status) ; aucun CHECK sur event_origin,
    task_kind, conversation_key ni support_kind (vocabulaires extensibles)."""
    checks = [c for c in CognitiveEvent.__table__.constraints if isinstance(c, sa.CheckConstraint)]
    assert len(checks) == 1
    assert checks[0].name == "ck_cognitive_events_status"
    assert str(checks[0].sqltext) == STATUS_CHECK_SQL
    assert not [c for c in SupportTrace.__table__.constraints if isinstance(c, sa.CheckConstraint)]
    for name in ("event_origin", "task_kind", "conversation_key"):
        assert name not in str(checks[0].sqltext), name


def test_support_traces_unique_is_exactly_event_sequence():
    """(8) UNIQUE(cognitive_event_id, sequence_no), et aucun autre UNIQUE."""
    uniques = [c for c in SupportTrace.__table__.constraints if isinstance(c, sa.UniqueConstraint)]
    assert len(uniques) == 1
    assert uniques[0].name == "uq_support_traces_event_sequence"
    assert [c.name for c in uniques[0].columns] == ["cognitive_event_id", "sequence_no"]
    assert not [c for c in CognitiveEvent.__table__.constraints if isinstance(c, sa.UniqueConstraint)]
    for table in (CognitiveEvent.__table__, SupportTrace.__table__):
        # Aucun index supplémentaire en T2-A.
        assert not table.indexes, table.name
        assert not any(c.unique or c.index for c in table.columns), table.name


def test_no_foreign_key_cascades():
    """(9) Aucune FK avec ON DELETE / ON UPDATE, dans tout Base.metadata."""
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.ondelete is None and fk.onupdate is None, (table.name, fk.parent.name)


def test_no_server_defaults_only_application_defaults():
    """(10) Aucun server_default (UUID / timestamp / JSONB) : les valeurs par
    défaut sont générées par l'application."""
    for table in (CognitiveEvent.__table__, SupportTrace.__table__):
        for col in table.columns:
            assert col.server_default is None, (table.name, col.name)
            assert col.server_onupdate is None, (table.name, col.name)

    def call(default):
        assert default is not None and default.is_callable
        return default.arg(None)

    events, traces = CognitiveEvent.__table__.c, SupportTrace.__table__.c
    for col in (events.id, traces.id):
        assert isinstance(call(col.default), uuid.UUID)
        assert call(col.default) != call(col.default)
    # Snapshots : un nouvel objet vide à chaque appel (jamais partagé).
    first, second = call(events.stimulus_snapshot.default), call(events.stimulus_snapshot.default)
    assert first == {} and second == {} and first is not second
    first, second = call(events.user_work_snapshot.default), call(events.user_work_snapshot.default)
    assert first == [] and second == [] and first is not second
    # Timestamps UTC aware.
    for col in (events.started_at, events.updated_at, traces.created_at):
        assert call(col.default).utcoffset().total_seconds() == 0, col.name
    assert call(events.updated_at.onupdate).utcoffset().total_seconds() == 0
    # Aucune valeur inventée : pas de défaut pour ces colonnes.
    for col in (events.user_id, events.analysis_session_id, events.event_origin, events.task_kind,
                events.status, events.conversation_key, events.closed_at, traces.cognitive_event_id,
                traces.sequence_no, traces.support_kind, traces.support_payload):
        assert col.default is None and col.onupdate is None, col.name


def test_jsonb_columns():
    """(11) JSONB pour stimulus_snapshot, user_work_snapshot, support_payload."""
    found = set()
    for table in (CognitiveEvent.__table__, SupportTrace.__table__):
        for col in table.columns:
            if isinstance(col.type, sa.JSON):
                assert type(col.type) is JSONB, (table.name, col.name)
                # Un None Python devient NULL SQL (refusé), jamais le JSON null.
                assert col.type.none_as_null is True, (table.name, col.name)
                found.add((table.name, col.name))
    assert found == JSONB_COLUMNS


def test_timestamptz_columns():
    """(12) TIMESTAMPTZ pour started_at, updated_at, closed_at, created_at."""
    found = set()
    for table in (CognitiveEvent.__table__, SupportTrace.__table__):
        for col in table.columns:
            if isinstance(col.type, sa.DateTime):
                assert col.type.timezone is True, (table.name, col.name)
                found.add((table.name, col.name))
    assert found == TIMESTAMPTZ_COLUMNS


def test_no_relationships_and_existing_models_untouched():
    """Les FK suffisent en T2-A : aucune relationship ORM, et aucun modèle
    existant ne gagne d'attribut vers les nouvelles tables."""
    for model in (CognitiveEvent, SupportTrace, AnalysisSession, User):
        assert not sa.inspect(model).relationships, model.__name__
    for name in REMAINING_TABLES:
        for col in Base.metadata.tables[name].columns:
            assert all(fk.column.table.name not in T2A_TABLES for fk in col.foreign_keys), name


def test_no_evaluation_concept_in_t2a_tables():
    """T2 est descriptif : aucune colonne ni table d'évaluation (score,
    compétence, niveau, confiance, observation, C1-C12...). Les tables
    d'observation de T3-A (niveau 3, testées à part) sont les seules à
    porter ces notions."""
    forbidden = ("score", "level", "stage", "competenc", "capabilit", "mastery", "confidence",
                 "observation", "taxonomy", "evidence", "progress", "inference", "signal")
    for table in (CognitiveEvent.__table__, SupportTrace.__table__):
        for col in table.columns:
            assert not any(word in col.name for word in forbidden), (table.name, col.name)
            assert not (col.name[:1] in ("c", "C") and col.name[1:].isdigit()), col.name
    for name in set(Base.metadata.tables) - T3A_TABLES:
        assert not any(word in name for word in forbidden), name


def test_t2a_tables_are_not_wired_to_the_application():
    """Aucun branchement : hors core/models.py, la migration 0005, le
    service de capture T2-B (core/cognitive_capture.py, lui-même non
    branché : voir tests/test_cognitive_capture.py) et la migration 0006
    de T3-A (dont les FK et la down_revision référencent cognitive_events),
    aucun code applicatif (api.py, core/, scripts/, frontend) ne mentionne
    ces modèles ou ces tables."""
    allowed = {"core/models.py", f"alembic/versions/{T2A}.py", "core/cognitive_capture.py",
               "alembic/versions/0006_observation_layer.py"}
    needles = ("CognitiveEvent", "SupportTrace", "cognitive_event", "support_trace")
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


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

USER = "u"
OTHER_USER = "u2"


def _catalog(engine, tables) -> dict:
    """Description catalogue PostgreSQL (colonnes, contraintes avec règles
    ON DELETE/UPDATE, index) des tables données, en plus de _snapshot."""
    return {
        t: {
            "columns": _catalog_columns(engine, t),
            "constraints": _constraints(engine, t),
            "indexes": sorted(_indexes(engine, t)),
        }
        for t in sorted(tables)
    }


def _global_catalog(engine) -> dict:
    """Objets de schéma hors tables : types ENUM, séquences, triggers
    utilisateur, fonctions du schéma public."""
    with engine.connect() as conn:
        return {
            "enums": conn.execute(sa.text("SELECT count(*) FROM pg_type WHERE typtype = 'e'")).scalar_one(),
            "sequences": sorted(conn.execute(sa.text(
                "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'"
            )).scalars().all()),
            "triggers": conn.execute(sa.text(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"
            )).scalar_one(),
            "functions": conn.execute(sa.text(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public'"
            )).scalar_one(),
        }


def _upgrade_head_with_users(pg_url, pg_engine) -> None:
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    with pg_engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:a, 'debutant'), (:b, 'debutant')"),
                     {"a": USER, "b": OTHER_USER})
        conn.execute(sa.text(
            "INSERT INTO analysis_sessions (id, user_id, ticker, status, started_at, updated_at) "
            "VALUES (:id, :u, 'MC.PA', 'in_progress', now(), now())"
        ), {"id": SESSION_ID, "u": USER})


@pytest.fixture
def conn(pg_url, pg_engine):
    """Schéma = head (0006 depuis T3-A) + deux utilisateurs et une analysis_session ;
    tout ce que fait le test est annulé."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with pg_engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()


INSERT_EVENT = sa.text(
    "INSERT INTO cognitive_events (id, user_id, analysis_session_id, event_origin, task_kind, "
    "status, conversation_key, stimulus_snapshot, user_work_snapshot, started_at, updated_at, "
    "closed_at) VALUES (:id, :user_id, :analysis_session_id, :event_origin, :task_kind, :status, "
    ":conversation_key, CAST(:stimulus AS JSONB), CAST(:work AS JSONB), now(), now(), :closed_at)"
)
INSERT_TRACE = sa.text(
    "INSERT INTO support_traces (id, cognitive_event_id, sequence_no, support_kind, "
    "support_payload, created_at) VALUES (:id, :event_id, :seq, :kind, CAST(:payload AS JSONB), now())"
)


def _event(conn, **overrides) -> uuid.UUID:
    params = {
        "id": uuid.uuid4(),
        "user_id": USER,
        "analysis_session_id": None,
        "event_origin": "education",
        "task_kind": "explain_concept",
        "status": "open",
        "conversation_key": None,
        "stimulus": '{"prompt": "Qu\'est-ce que le ROE ?", "ticker": "MC.PA"}',
        "work": '[{"type": "message", "text": "rentabilité", "sequence": 1}]',
        "closed_at": None,
    }
    params.update(overrides)
    conn.execute(INSERT_EVENT, params)
    return params["id"]


def _trace(conn, event_id, seq, **overrides) -> uuid.UUID:
    params = {"id": uuid.uuid4(), "event_id": event_id, "seq": seq, "kind": "hint",
              "payload": '{"text": "Pense au résultat net rapporté aux capitaux propres."}'}
    params.update(overrides)
    conn.execute(INSERT_TRACE, params)
    return params["id"]


def _refused(conn, match, statement, params=None):
    with pytest.raises(sa.exc.IntegrityError, match=match):
        with conn.begin_nested():
            conn.execute(statement, params or {})


def _assert_metadata_matches_0005(engine) -> None:
    """Au schéma 0005, Base.metadata ne diffère que par les tables de T3-A,
    créées seulement en 0006 ; tout le reste correspond exactement."""
    diff = _compare_metadata(engine)
    assert sorted((d[0], d[1].name) for d in diff) == sorted(
        [("add_table", t) for t in T3A_TABLES] + [("add_index", i) for i in T3A_INDEXES]
    )


def test_pg_upgrade_from_empty_database_to_0005(pg_url, pg_engine):
    """(13)(30) Base vide -> 0005 ; Base.metadata == schéma (hors T3-A).
    La tête est 0006 depuis T3-A (testée à part)."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T2A)
    assert _version(pg_engine) == T2A
    assert _tables(pg_engine) == REMAINING_TABLES | T2A_TABLES | {"alembic_version"}
    _assert_metadata_matches_0005(pg_engine)
    assert _global_catalog(pg_engine)["enums"] == 0
    assert _global_catalog(pg_engine)["triggers"] == 0


def test_pg_cognitive_events_exact_structure(pg_url, pg_engine):
    """(15) Structure exacte de cognitive_events lue dans le catalogue."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T2A)
    assert _catalog_columns(pg_engine, EVENTS) == [
        ("id", "uuid", "NO", None),
        ("user_id", "character varying", "NO", None),
        ("analysis_session_id", "uuid", "YES", None),
        ("event_origin", "character varying", "NO", None),
        ("task_kind", "character varying", "NO", None),
        ("status", "character varying", "NO", None),
        ("conversation_key", "character varying", "YES", None),
        ("stimulus_snapshot", "jsonb", "NO", None),
        ("user_work_snapshot", "jsonb", "NO", None),
        ("started_at", "timestamp with time zone", "NO", None),
        ("updated_at", "timestamp with time zone", "NO", None),
        ("closed_at", "timestamp with time zone", "YES", None),
    ]
    assert _constraints(pg_engine, EVENTS) == [
        ("ck_cognitive_events_status", "c",
         "CHECK (((status)::text = ANY ((ARRAY['open'::character varying, "
         "'finalized'::character varying, 'abandoned'::character varying])::text[])))", " ", " "),
        ("cognitive_events_analysis_session_id_fkey", "f",
         "FOREIGN KEY (analysis_session_id) REFERENCES analysis_sessions(id)", "a", "a"),
        ("cognitive_events_pkey", "p", "PRIMARY KEY (id)", " ", " "),
        ("cognitive_events_user_id_fkey", "f", "FOREIGN KEY (user_id) REFERENCES users(id)", "a", "a"),
    ]
    assert _indexes(pg_engine, EVENTS) == ["cognitive_events_pkey"]


def test_pg_support_traces_exact_structure(pg_url, pg_engine):
    """(16) Structure exacte de support_traces lue dans le catalogue."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T2A)
    assert _catalog_columns(pg_engine, TRACES) == [
        ("id", "uuid", "NO", None),
        ("cognitive_event_id", "uuid", "NO", None),
        ("sequence_no", "integer", "NO", None),
        ("support_kind", "character varying", "NO", None),
        ("support_payload", "jsonb", "NO", None),
        ("created_at", "timestamp with time zone", "NO", None),
    ]
    assert _constraints(pg_engine, TRACES) == [
        ("support_traces_cognitive_event_id_fkey", "f",
         "FOREIGN KEY (cognitive_event_id) REFERENCES cognitive_events(id)", "a", "a"),
        ("support_traces_pkey", "p", "PRIMARY KEY (id)", " ", " "),
        ("uq_support_traces_event_sequence", "u", "UNIQUE (cognitive_event_id, sequence_no)", " ", " "),
    ]
    # Seuls index : PK et celui, implicite, de la contrainte UNIQUE.
    assert sorted(_indexes(pg_engine, TRACES)) == ["support_traces_pkey", "uq_support_traces_event_sequence"]
    with pg_engine.connect() as conn:
        # Aucune FK de la base n'est en cascade / set null / set default.
        assert conn.execute(sa.text(
            "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
            "AND (confdeltype <> 'a' OR confupdtype <> 'a')"
        )).scalar_one() == 0


def test_pg_valid_open_event_is_accepted(conn):
    """(17)(19) Événement open valide, analysis_session_id NULL accepté ;
    les snapshots JSONB sont conservés tels quels."""
    event_id = _event(conn)
    row = conn.execute(sa.text(
        "SELECT status, analysis_session_id, stimulus_snapshot, user_work_snapshot, closed_at, "
        "jsonb_typeof(stimulus_snapshot), jsonb_typeof(user_work_snapshot) "
        "FROM cognitive_events WHERE id = :id"
    ), {"id": event_id}).one()
    assert row == (
        "open", None,
        {"prompt": "Qu'est-ce que le ROE ?", "ticker": "MC.PA"},
        [{"type": "message", "text": "rentabilité", "sequence": 1}],
        None, "object", "array",
    )


def test_pg_all_statuses_and_extensible_vocabularies_are_accepted(conn):
    """Les trois statuts sont acceptés ; event_origin, task_kind et
    support_kind acceptent n'importe quelle valeur (pas de CHECK)."""
    for status in EVENT_STATUSES:
        _event(conn, status=status)
    event_id = _event(conn, event_origin="nouvelle_origine_future", task_kind="nouvelle_tache_future",
                      conversation_key="conv-42")
    _trace(conn, event_id, 1, kind="nouveau_type_d_aide_futur")
    assert conn.execute(sa.text("SELECT count(*) FROM cognitive_events")).scalar_one() == 4


def test_pg_invalid_status_is_refused(conn):
    """(18) Statut hors vocabulaire (ou NULL) refusé par PostgreSQL."""
    for status in ("closed", "OPEN", "in_progress", ""):
        with pytest.raises(sa.exc.IntegrityError, match="ck_cognitive_events_status"):
            with conn.begin_nested():
                _event(conn, status=status)
    with pytest.raises(sa.exc.IntegrityError, match="null value in column \"status\""):
        with conn.begin_nested():
            _event(conn, status=None)


def test_pg_snapshots_and_required_columns_are_not_null(conn):
    for column, param in (("stimulus_snapshot", "stimulus"), ("user_work_snapshot", "work"),
                          ("event_origin", "event_origin"), ("task_kind", "task_kind"),
                          ("user_id", "user_id")):
        with pytest.raises(sa.exc.IntegrityError, match=f"null value in column \"{column}\""):
            with conn.begin_nested():
                _event(conn, **{param: None})
    event_id = _event(conn)
    for column, param in (("support_payload", "payload"), ("support_kind", "kind")):
        with pytest.raises(sa.exc.IntegrityError, match=f"null value in column \"{column}\""):
            with conn.begin_nested():
                _trace(conn, event_id, 1, **{param: None})
    with pytest.raises(sa.exc.IntegrityError, match="null value in column \"sequence_no\""):
        with conn.begin_nested():
            _trace(conn, event_id, None)


def test_pg_analysis_session_fk(conn):
    """(20)(21) analysis_session_id valide accepté ; inexistant refusé."""
    event_id = _event(conn, analysis_session_id=SESSION_ID)
    assert conn.execute(sa.text("SELECT analysis_session_id FROM cognitive_events WHERE id = :id"),
                        {"id": event_id}).scalar_one() == SESSION_ID
    with pytest.raises(sa.exc.IntegrityError, match="cognitive_events_analysis_session_id_fkey"):
        with conn.begin_nested():
            _event(conn, analysis_session_id=uuid.uuid4())
    # Pas de cascade : supprimer une session référencée est refusé.
    _refused(conn, "cognitive_events_analysis_session_id_fkey",
             sa.text("DELETE FROM analysis_sessions WHERE id = :id"), {"id": SESSION_ID})


def test_pg_user_fk(conn):
    """(22) user_id inexistant refusé ; supprimer un utilisateur ayant des
    événements est refusé (pas de cascade)."""
    with pytest.raises(sa.exc.IntegrityError, match="cognitive_events_user_id_fkey"):
        with conn.begin_nested():
            _event(conn, user_id="ghost")
    _event(conn, user_id=OTHER_USER)
    _refused(conn, "cognitive_events_user_id_fkey",
             sa.text("DELETE FROM users WHERE id = :u"), {"u": OTHER_USER})


def test_pg_support_traces_sequence_and_fk(conn):
    """(23)(24)(25)(26) Trace valide acceptée ; même (événement, sequence_no)
    refusé ; même sequence_no sur deux événements autorisé ; événement
    inexistant refusé."""
    first, second = _event(conn), _event(conn)
    _trace(conn, first, 1)
    _trace(conn, first, 2, kind="formula", payload='{"formula": "ROE = résultat net / capitaux propres"}')
    with pytest.raises(sa.exc.IntegrityError, match="uq_support_traces_event_sequence"):
        with conn.begin_nested():
            _trace(conn, first, 1, kind="explanation")
    _trace(conn, second, 1)
    with pytest.raises(sa.exc.IntegrityError, match="support_traces_cognitive_event_id_fkey"):
        with conn.begin_nested():
            _trace(conn, uuid.uuid4(), 1)
    rows = conn.execute(sa.text(
        "SELECT cognitive_event_id, sequence_no, support_kind, support_payload "
        "FROM support_traces ORDER BY cognitive_event_id = :f DESC, sequence_no"
    ), {"f": first}).all()
    assert [tuple(r) for r in rows] == [
        (first, 1, "hint", {"text": "Pense au résultat net rapporté aux capitaux propres."}),
        (first, 2, "formula", {"formula": "ROE = résultat net / capitaux propres"}),
        (second, 1, "hint", {"text": "Pense au résultat net rapporté aux capitaux propres."}),
    ]


def test_pg_deleting_referenced_event_is_refused(conn):
    """(27) Aucun CASCADE : un événement référencé par une trace ne peut pas
    être supprimé ; la trace reste intacte."""
    event_id = _event(conn)
    _trace(conn, event_id, 1)
    _refused(conn, "support_traces_cognitive_event_id_fkey",
             sa.text("DELETE FROM cognitive_events WHERE id = :id"), {"id": event_id})
    assert conn.execute(sa.text("SELECT count(*) FROM support_traces WHERE cognitive_event_id = :id"),
                        {"id": event_id}).scalar_one() == 1


def test_pg_orm_insertion_uses_application_defaults(pg_url, pg_engine):
    """Insertion via l'ORM : UUID, snapshots vides et timestamps UTC aware
    générés côté application."""
    _upgrade_head_with_users(pg_url, pg_engine)
    with Session(pg_engine) as session:
        with session.begin():
            event = CognitiveEvent(user_id=USER, event_origin="coach", task_kind="interpret_metric",
                                   status="open")
            other = CognitiveEvent(user_id=USER, event_origin="coach", task_kind="interpret_metric",
                                   status="open", analysis_session_id=SESSION_ID,
                                   stimulus_snapshot={"prompt": "p", "displayed_data": {"roe": 0.24}},
                                   user_work_snapshot=[{"type": "message", "text": "t", "sequence": 1}])
            session.add_all([event, other])
            session.flush()
            trace = SupportTrace(cognitive_event_id=event.id, sequence_no=1, support_kind="rephrase",
                                 support_payload={"text": "Autrement dit..."})
            session.add(trace)
            session.flush()
            session.refresh(event)
            session.refresh(other)
            session.refresh(trace)

            assert isinstance(event.id, uuid.UUID) and isinstance(trace.id, uuid.UUID)
            assert event.id != other.id
            assert event.stimulus_snapshot == {} and event.user_work_snapshot == []
            assert other.stimulus_snapshot == {"prompt": "p", "displayed_data": {"roe": 0.24}}
            assert other.user_work_snapshot == [{"type": "message", "text": "t", "sequence": 1}]
            assert other.analysis_session_id == SESSION_ID
            assert event.analysis_session_id is None and event.conversation_key is None
            assert event.closed_at is None
            for value in (event.started_at, event.updated_at, trace.created_at):
                assert value.tzinfo is not None and value.utcoffset().total_seconds() == 0
            assert trace.support_payload == {"text": "Autrement dit..."}
            session.rollback()

    # Un None Python n'est jamais stocké comme le JSON null : sur les
    # snapshots (défaut applicatif), l'ORM applique le défaut ; sur
    # support_payload (sans défaut), NULL SQL refusé par NOT NULL.
    with Session(pg_engine) as session:
        with session.begin():
            event = CognitiveEvent(user_id=USER, event_origin="coach", task_kind="x", status="open",
                                   stimulus_snapshot=None, user_work_snapshot=None)
            session.add(event)
            session.flush()
            assert session.execute(sa.text(
                "SELECT jsonb_typeof(stimulus_snapshot), jsonb_typeof(user_work_snapshot) "
                "FROM cognitive_events WHERE id = :id"
            ), {"id": event.id}).one() == ("object", "array")
            with pytest.raises(sa.exc.IntegrityError, match="null value in column \"support_payload\""):
                with session.begin_nested():
                    session.add(SupportTrace(cognitive_event_id=event.id, sequence_no=1,
                                             support_kind="hint", support_payload=None))
            session.rollback()
    with pg_engine.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM cognitive_events")).scalar_one() == 0
        assert c.execute(sa.text("SELECT count(*) FROM support_traces")).scalar_one() == 0


def test_pg_upgrade_0004_to_0005_preserves_everything_then_downgrade(pg_url, pg_engine):
    """(14)(28)(29)(30) 0004 -> 0005 conserve schéma et données de toutes les
    tables existantes ; le downgrade retire uniquement les deux tables et
    restaure exactement 0004 ; le ré-upgrade fonctionne."""
    existing = REMAINING_TABLES | {"alembic_version"}
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", T1C2)
    _seed_remaining_tables(pg_engine)
    schema_0004 = _snapshot(pg_engine, existing)
    catalog_0004 = _catalog(pg_engine, existing)
    global_0004 = _global_catalog(pg_engine)
    data_0004 = _data(pg_engine, REMAINING_TABLES)
    assert all(data_0004[t] for t in REMAINING_TABLES)
    # alembic_version reste le VARCHAR(32) créé par Alembic.
    with pg_engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = 'alembic_version' AND column_name = 'version_num'"
        )).scalar_one() == 32

    # --- upgrade 0004 -> 0005 --------------------------------------------
    _run_alembic(pg_url, "upgrade", T2A)
    assert _version(pg_engine) == T2A
    assert _tables(pg_engine) == existing | T2A_TABLES
    assert _snapshot(pg_engine, existing) == schema_0004
    assert _catalog(pg_engine, existing) == catalog_0004
    assert _data(pg_engine, REMAINING_TABLES) == data_0004
    assert _data(pg_engine, T2A_TABLES) == {EVENTS: [], TRACES: []}
    # Aucun ENUM, aucune séquence, aucun trigger, aucune fonction ajoutés.
    assert _global_catalog(pg_engine) == global_0004
    schema_0005 = _snapshot(pg_engine, T2A_TABLES)
    catalog_0005 = _catalog(pg_engine, T2A_TABLES)
    _assert_metadata_matches_0005(pg_engine)

    # --- downgrade 0005 -> 0004 ------------------------------------------
    _run_alembic(pg_url, "downgrade", T1C2)
    assert _version(pg_engine) == T1C2
    assert _tables(pg_engine) == existing
    with pg_engine.connect() as conn:
        for table in T2A_TABLES:
            assert conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}).scalar_one() is None
    assert _snapshot(pg_engine, existing) == schema_0004
    assert _catalog(pg_engine, existing) == catalog_0004
    assert _global_catalog(pg_engine) == global_0004
    assert _data(pg_engine, REMAINING_TABLES) == data_0004
    _assert_metadata_matches_0004(pg_engine)

    # --- ré-upgrade 0004 -> 0005 -----------------------------------------
    _run_alembic(pg_url, "upgrade", T2A)
    assert _version(pg_engine) == T2A
    assert _tables(pg_engine) == existing | T2A_TABLES
    assert _snapshot(pg_engine, existing) == schema_0004
    assert _catalog(pg_engine, existing) == catalog_0004
    assert _snapshot(pg_engine, T2A_TABLES) == schema_0005
    assert _catalog(pg_engine, T2A_TABLES) == catalog_0005
    assert _data(pg_engine, REMAINING_TABLES) == data_0004
    _assert_metadata_matches_0005(pg_engine)
