"""Tests de T2-B : service interne de capture cognitive
(core/cognitive_capture.py).

Aucune migration dans ce chantier : le schéma testé est celui produit par
`alembic upgrade head` (= 0005_cognitive_support_traces, T2-A, puis
0006_observation_layer depuis T3-A, 0007_pedagogical_taxonomy depuis
T4-A et 0008_longitudinal_relations depuis T5-A, qui n'ajoutent que des
tables non utilisées par ce service).

1. Tests sans base (toujours exécutés) : aucune migration T2-B, API publique
   exacte (aucune fonction update/delete), aucun commit/rollback ni
   dépendance FastAPI dans le service, aucun branchement applicatif,
   validation structurelle des entrées, requête de verrouillage FOR UPDATE.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T1/T2-A) :
   uniquement si ORYX_TEST_DATABASE_URL pointe vers une base DÉDIÉE dont le
   nom contient "test" (schéma public détruit et recréé). Sinon SKIPPÉS :
   rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t2b_test \\
           python -m pytest tests/test_cognitive_capture.py

   Les sessions sont créées comme core.db.SessionLocal (autoflush=False) ;
   la transaction appartient toujours au test (l'appelant). Les tests de
   concurrence utilisent deux connexions réelles et attendent, via
   pg_blocking_pids(), que la seconde soit effectivement bloquée par le
   verrou de la première avant de la libérer : aucun sleep() arbitraire.
"""
import ast
import inspect
import itertools
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from core import cognitive_capture as cc
from core.cognitive_capture import (
    CognitiveCaptureError,
    CognitiveEventClosed,
    CognitiveEventNotFound,
    InvalidCognitivePayload,
)
from core.models import SupportTrace, User
from tests.test_migration_0002_analysis_sessions import (
    BASELINE,
    REPO_ROOT,
    T1A,
    _script_directory,
    pg_url,  # noqa: F401 — fixture
)
from tests.test_migration_0003_analysis_session_links import T1B1
from tests.test_migration_0004_drop_company_analyses import SESSION_ID, T1C2, _code_tokens
from tests.test_migration_0005_cognitive_support_traces import T2A, USER, _upgrade_head_with_users

T3A = "0006_observation_layer"
T4A = "0007_pedagogical_taxonomy"
T5A = "0008_longitudinal_relations"

SERVICE_PATH = REPO_ROOT / "core" / "cognitive_capture.py"
PUBLIC_API = {
    "open_event",
    "append_user_work",
    "add_support_trace",
    "finalize_event",
    "abandon_event",
    "get_event",
    "get_support_traces",
}
MUTATIONS = {
    "append_user_work": lambda db, eid: cc.append_user_work(db, event_id=eid, work={"text": "x"}),
    "add_support_trace": lambda db, eid: cc.add_support_trace(
        db, event_id=eid, support_kind="hint", support_payload={"text": "x"}),
    "finalize_event": lambda db, eid: cc.finalize_event(db, event_id=eid),
    "abandon_event": lambda db, eid: cc.abandon_event(db, event_id=eid),
}
STIMULUS = {
    "question": "Que mesure le ROE ?",
    "metric": "roe",
    "displayed_data": {"roe": 0.24, "ticker": "MC.PA", "history": [0.21, 0.22, None]},
}
INVALID_PAYLOADS = [
    None,
    [],
    "texte",
    42,
    ({"a": 1},),
    {1: "clé int"},
    {None: "clé None"},
    {True: "clé bool"},
    {("a", "b"): "clé tuple"},
    {"t": (1, 2)},
    {"s": {1, 2}},
    {"nan": float("nan")},
    {"inf": float("inf")},
    {"neg_inf": [float("-inf")]},
    {"decimal": Decimal("1.5")},
    {"date": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    {"uuid": uuid.uuid4()},
    {"bytes": b"x"},
    {"objet": object()},
    {"nul": "a\x00b"},
    {"a\x00": "clé avec NUL"},
    {"nested": {"deep": [{"ok": 1}, {"ko": Decimal("2")}]}},
]


class _Color(str, Enum):
    RED = "red"


INVALID_PAYLOADS.append({"enum": _Color.RED})


class _NoDB:
    """Session factice : la validation structurelle doit échouer avant tout
    accès à la base."""

    def __getattr__(self, name):
        raise AssertionError(f"accès à la base inattendu : {name}")


def _open_kwargs(**overrides):
    kwargs = {
        "user_id": USER,
        "event_origin": "education",
        "task_kind": "explain_concept",
        "stimulus_snapshot": {"question": "Que mesure le ROE ?"},
    }
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_no_migration_added_by_t2b_head_is_0008():
    """T2-B n'a ajouté aucune migration ; les seules ajoutées depuis sont
    0006 (T3-A), 0007 (T4-A) et 0008 (T5-A), qui est la tête."""
    script = _script_directory()
    assert script.get_heads() == [T5A]
    assert {rev.revision for rev in script.walk_revisions()} == {BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A}
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [f"{rev}.py" for rev in (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A)]


def test_public_api_is_exactly_the_capture_functions():
    """Pas de CRUD générique, pas d'update/delete de SupportTrace, pas de
    get_or_create ni de recherche du dernier événement ouvert."""
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == PUBLIC_API
    for name in PUBLIC_API:
        params = list(inspect.signature(getattr(cc, name)).parameters.values())
        assert params[0].name == "db" and params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:]), name


def test_exceptions_are_a_small_business_hierarchy():
    for exc in (CognitiveEventNotFound, CognitiveEventClosed, InvalidCognitivePayload):
        assert issubclass(exc, CognitiveCaptureError)
    assert issubclass(CognitiveCaptureError, Exception)
    assert not issubclass(CognitiveCaptureError, (LookupError, ValueError))


def test_service_owns_no_transaction_and_does_not_depend_on_fastapi():
    source = SERVICE_PATH.read_text(encoding="utf-8")
    tokens = set(_code_tokens(source).split("\n"))
    for forbidden in ("commit", "rollback", "begin", "begin_nested", "close", "SessionLocal",
                      "get_db", "HTTPException", "utcnow", "delete", "update", "merge"):
        assert forbidden not in tokens, forbidden

    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {"math", "uuid", "datetime", "sqlalchemy", "core.models"}


def test_service_introduces_no_evaluation_vocabulary():
    """T2-B capture, n'évalue pas : aucun identifiant ni chaîne de code du
    service ne porte de notion d'évaluation (docstrings exclues)."""
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).lower()
    for word in ("capabilit", "stage", "score", "confidence", "evidence", "contradiction",
                 "mastery", "difficult", "inference", "observation", "taxonomy", "progress",
                 "level"):
        assert word not in tokens, word


def test_service_is_not_wired_to_the_application():
    """Aucune route ni module applicatif n'importe le service ; seul le
    service (et models.py, la migration 0005) mentionne les modèles."""
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
        if rel != "core/cognitive_capture.py":
            assert "cognitive_capture" not in source, rel
    assert checked > 0


def test_lifecycle_fields_cannot_be_supplied_to_open_event():
    for field in ("status", "user_work_snapshot", "started_at", "updated_at", "closed_at", "id"):
        with pytest.raises(TypeError):
            cc.open_event(_NoDB(), **_open_kwargs(**{field: None}))
    with pytest.raises(TypeError):
        cc.add_support_trace(_NoDB(), event_id=uuid.uuid4(), support_kind="hint",
                             support_payload={}, sequence_no=1)


@pytest.mark.parametrize("field", ["user_id", "event_origin", "task_kind"])
@pytest.mark.parametrize("value", ["", " ", "\t\n", None, 42])
def test_open_event_rejects_blank_required_strings(field, value):
    with pytest.raises(InvalidCognitivePayload, match=field):
        cc.open_event(_NoDB(), **_open_kwargs(**{field: value}))


@pytest.mark.parametrize("payload", INVALID_PAYLOADS)
def test_open_event_rejects_non_json_stimulus(payload):
    with pytest.raises(InvalidCognitivePayload):
        cc.open_event(_NoDB(), **_open_kwargs(stimulus_snapshot=payload))


def test_open_event_rejects_circular_stimulus():
    cyclic = {"a": []}
    cyclic["a"].append(cyclic)
    with pytest.raises(InvalidCognitivePayload, match="circulaire"):
        cc.open_event(_NoDB(), **_open_kwargs(stimulus_snapshot=cyclic))


@pytest.mark.parametrize("overrides", [
    {"analysis_session_id": str(SESSION_ID)},
    {"analysis_session_id": 1},
    {"conversation_key": ""},
    {"conversation_key": "  "},
    {"conversation_key": 12},
])
def test_open_event_rejects_invalid_optional_links(overrides):
    with pytest.raises(InvalidCognitivePayload):
        cc.open_event(_NoDB(), **_open_kwargs(**overrides))


@pytest.mark.parametrize("name", sorted(MUTATIONS) + ["get_event", "get_support_traces"])
@pytest.mark.parametrize("event_id", [None, "", str(uuid.uuid4()), 1])
def test_event_id_must_be_a_uuid(name, event_id):
    call = MUTATIONS.get(name) or (lambda db, eid: getattr(cc, name)(db, event_id=eid))
    with pytest.raises(InvalidCognitivePayload, match="event_id"):
        call(_NoDB(), event_id)


def test_json_copy_accepts_json_values_and_returns_independent_copy():
    shared = {"k": 1}
    payload = OrderedDict(
        s="é", i=-3, big=10 ** 30, f=0.5, t=True, n=None, e={}, l=[], nested=[{"a": [1, "b", None]}],
        shared_twice=[shared, shared],
    )
    copy = cc._json_dict(payload, "p")
    assert copy == payload and type(copy) is dict
    assert copy["nested"] is not payload["nested"]
    assert copy["nested"][0] is not payload["nested"][0]
    payload["nested"][0]["a"].append("mutation")
    shared["k"] = 2
    assert copy["nested"] == [{"a": [1, "b", None]}]
    assert copy["shared_twice"] == [{"k": 1}, {"k": 1}]


def test_lock_query_is_select_for_update_on_cognitive_events():
    """La requête de verrouillage exécutée par toutes les mutations d'un
    événement existant est un SELECT ... FOR UPDATE (vérifié aussi, dans
    l'ordre réel d'exécution, contre PostgreSQL plus bas)."""
    captured = []

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _RecordingDB:
        def execute(self, statement):
            captured.append(statement)
            return _Result()

    for name, call in MUTATIONS.items():
        captured.clear()
        with pytest.raises(CognitiveEventNotFound):
            call(_RecordingDB(), uuid.uuid4())
        assert len(captured) == 1, name
        sql = " ".join(str(captured[0].compile(dialect=postgresql.dialect())).split())
        assert sql.startswith("SELECT cognitive_events.id"), name
        assert "FROM cognitive_events WHERE cognitive_events.id = " in sql, name
        assert sql.endswith("FOR UPDATE"), name


def test_utcnow_is_utc_aware():
    now = cc._utcnow()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine(pg_url):  # noqa: F811
    """Schéma = head (0008) + deux utilisateurs et une analysis_session,
    créé une fois pour le module ; chaque test nettoie ses événements."""
    eng = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    _upgrade_head_with_users(pg_url, eng)
    yield eng
    eng.dispose()


@pytest.fixture
def Sessions(engine):
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)  # = core.db.SessionLocal
    yield factory
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM support_traces"))
        conn.execute(sa.text("DELETE FROM cognitive_events"))
        conn.execute(sa.text("DELETE FROM users WHERE id NOT IN ('u', 'u2')"))


@pytest.fixture
def db(Sessions):
    session = Sessions()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def clock(monkeypatch):
    """Horloge déterministe : chaque appel à _utcnow avance d'une seconde.
    Deux timestamps égaux prouvent donc un unique `now` par opération."""
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    ticks = itertools.count()
    monkeypatch.setattr(cc, "_utcnow", lambda: base + timedelta(seconds=next(ticks)))
    return base


def _row(engine, event_id):
    """État persisté, lu par une connexion indépendante (donc seulement ce
    qui a été commité)."""
    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT status, stimulus_snapshot, user_work_snapshot, started_at, updated_at, closed_at "
            "FROM cognitive_events WHERE id = :id"
        ), {"id": event_id}).mappings().one_or_none()
        return dict(row) if row else None


def _traces(engine, event_id):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT sequence_no, support_kind, support_payload FROM support_traces "
            "WHERE cognitive_event_id = :id ORDER BY sequence_no"
        ), {"id": event_id})]


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def _committed_event(Sessions, *, work=(), traces=(), close=None, **overrides):
    """Crée et commite un événement (en tant qu'appelant), éventuellement
    enrichi puis fermé."""
    with Sessions() as session:
        event_id = cc.open_event(session, **_open_kwargs(**overrides)).id
        for item in work:
            cc.append_user_work(session, event_id=event_id, work=item)
        for kind in traces:
            cc.add_support_trace(session, event_id=event_id, support_kind=kind, support_payload={"k": kind})
        if close:
            getattr(cc, close)(session, event_id=event_id)
        session.commit()
    return event_id


def _assert_utc(value):
    assert value.tzinfo is not None and value.utcoffset() == timedelta(0)


# --- open_event -------------------------------------------------------------

def test_pg_open_event_creates_open_event(engine, db, clock):
    event = cc.open_event(db, **_open_kwargs(stimulus_snapshot=STIMULUS))
    assert isinstance(event.id, uuid.UUID)
    assert event.status == "open"
    assert event.user_id == USER
    assert event.event_origin == "education" and event.task_kind == "explain_concept"
    assert event.analysis_session_id is None and event.conversation_key is None
    assert event.stimulus_snapshot == STIMULUS
    assert event.user_work_snapshot == []
    assert event.started_at == event.updated_at == clock
    assert event.closed_at is None

    # Ligne matérialisée par le flush, relue depuis la base dans la
    # transaction de l'appelant.
    row = db.execute(sa.text(
        "SELECT status, stimulus_snapshot, user_work_snapshot, jsonb_typeof(user_work_snapshot), "
        "started_at, updated_at, closed_at, analysis_session_id, conversation_key "
        "FROM cognitive_events WHERE id = :id"
    ), {"id": event.id}).one()
    assert row[:4] == ("open", STIMULUS, [], "array")
    _assert_utc(row.started_at)
    assert row.started_at == row.updated_at == clock
    assert row.closed_at is None and row.analysis_session_id is None and row.conversation_key is None

    db.commit()
    assert _row(engine, event.id)["status"] == "open"
    assert cc.get_event(db, event_id=event.id) is event


def test_pg_open_event_real_clock_timestamps_are_utc_aware_and_equal(engine, db):
    event = cc.open_event(db, **_open_kwargs())
    db.commit()
    stored = _row(engine, event.id)
    for value in (event.started_at, event.updated_at, stored["started_at"], stored["updated_at"]):
        _assert_utc(value)
    assert event.started_at == event.updated_at
    assert stored["started_at"] == stored["updated_at"] == event.started_at
    assert stored["closed_at"] is None


def test_pg_open_event_optional_links(engine, db):
    linked = cc.open_event(db, **_open_kwargs(event_origin="decryptage", task_kind="interpret_metric"),
                           analysis_session_id=SESSION_ID, conversation_key="conv-42")
    unlinked = cc.open_event(db, **_open_kwargs(event_origin="coach"))
    db.commit()
    db.expire_all()
    assert linked.analysis_session_id == SESSION_ID and linked.conversation_key == "conv-42"
    assert unlinked.analysis_session_id is None and unlinked.conversation_key is None
    assert linked.id != unlinked.id


def test_pg_open_event_vocabularies_are_extensible(db):
    for origin, kind in (("education", "explain_concept"), ("coach", "compare_options"),
                         ("academy", "apply_formula"), ("rallye", "identify_risk"),
                         ("portfolio", "build_thesis"), ("origine_future_42", "tache_future_42")):
        event = cc.open_event(db, **_open_kwargs(event_origin=origin, task_kind=kind))
        assert (event.event_origin, event.task_kind) == (origin, kind)


def test_pg_open_event_accepts_empty_stimulus(db):
    event = cc.open_event(db, **_open_kwargs(stimulus_snapshot={}))
    db.expire_all()
    assert event.stimulus_snapshot == {}


def test_pg_open_event_copies_stimulus_defensively(engine, db):
    stimulus = {"question": "q", "displayed_data": {"roe": [0.2, 0.3]}}
    event = cc.open_event(db, **_open_kwargs(stimulus_snapshot=stimulus))
    stimulus["question"] = "modifiée"
    stimulus["displayed_data"]["roe"].append(9.9)
    stimulus["ajout"] = True
    expected = {"question": "q", "displayed_data": {"roe": [0.2, 0.3]}}
    assert event.stimulus_snapshot == expected
    db.commit()
    assert _row(engine, event.id)["stimulus_snapshot"] == expected


def test_pg_unknown_user_is_left_to_the_database_fk(db):
    with pytest.raises(sa.exc.IntegrityError, match="cognitive_events_user_id_fkey"):
        cc.open_event(db, **_open_kwargs(user_id="ghost"))


# --- transactions : l'appelant possède commit/rollback ----------------------

def test_pg_open_event_does_not_commit_and_caller_rollback_discards_it(engine, db):
    """Le service ne commite pas : ni l'événement ni une écriture métier
    déjà en attente dans la même Session ne sont visibles d'une autre
    transaction ; le rollback de l'appelant annule tout."""
    db.add(User(id="pending-user", level="debutant"))
    event = cc.open_event(db, **_open_kwargs())
    event_id = event.id
    assert db.execute(sa.text("SELECT count(*) FROM cognitive_events WHERE id = :id"),
                      {"id": event_id}).scalar_one() == 1
    assert _row(engine, event_id) is None
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users WHERE id = 'pending-user'")).scalar_one() == 0

    db.rollback()
    assert _row(engine, event_id) is None
    assert _count(engine, "cognitive_events") == 0
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users WHERE id = 'pending-user'")).scalar_one() == 0


def test_pg_caller_rollback_discards_mutations_of_existing_event(engine, Sessions, db):
    event_id = _committed_event(Sessions, work=[{"n": 1}], traces=["hint"])
    before, traces_before = _row(engine, event_id), _traces(engine, event_id)

    cc.append_user_work(db, event_id=event_id, work={"n": 2})
    cc.add_support_trace(db, event_id=event_id, support_kind="formula", support_payload={"f": "x"})
    cc.finalize_event(db, event_id=event_id)
    assert _row(engine, event_id) == before  # rien de commité par le service
    db.rollback()

    assert _row(engine, event_id) == before
    assert _traces(engine, event_id) == traces_before
    # L'événement est toujours open et mutable dans une nouvelle transaction.
    assert cc.get_event(db, event_id=event_id).status == "open"
    assert cc.add_support_trace(db, event_id=event_id, support_kind="hint",
                                support_payload={}).sequence_no == 2


# --- append_user_work -------------------------------------------------------

def test_pg_append_user_work_preserves_order(engine, db, clock):
    event = cc.open_event(db, **_open_kwargs(stimulus_snapshot=STIMULUS))
    works = [{"type": "message", "text": "rentabilité"}, {}, {"type": "choice", "value": 2, "ok": None}]
    for index, work in enumerate(works, start=1):
        returned = cc.append_user_work(db, event_id=event.id, work=work)
        assert returned is event
        assert event.updated_at == clock + timedelta(seconds=index)
    assert event.user_work_snapshot == works
    assert event.stimulus_snapshot == STIMULUS
    assert event.started_at == clock and event.status == "open"
    db.commit()

    # La réassignation complète du JSONB est bien persistée.
    stored = _row(engine, event.id)
    assert stored["user_work_snapshot"] == works
    assert stored["stimulus_snapshot"] == STIMULUS
    assert stored["started_at"] == clock
    assert stored["updated_at"] == clock + timedelta(seconds=len(works))
    assert stored["closed_at"] is None


def test_pg_append_user_work_across_transactions(engine, Sessions):
    event_id = _committed_event(Sessions, work=[{"n": 1}])
    with Sessions() as session:
        cc.append_user_work(session, event_id=event_id, work={"n": 2})
        session.commit()
    with Sessions() as session:
        cc.append_user_work(session, event_id=event_id, work={"n": 3})
        session.commit()
    assert _row(engine, event_id)["user_work_snapshot"] == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_pg_append_user_work_copies_defensively(engine, db):
    event = cc.open_event(db, **_open_kwargs())
    work = {"text": "réponse", "parts": ["a"]}
    cc.append_user_work(db, event_id=event.id, work=work)
    work["text"] = "modifiée"
    work["parts"].append("b")
    assert event.user_work_snapshot == [{"text": "réponse", "parts": ["a"]}]
    db.commit()
    assert _row(engine, event.id)["user_work_snapshot"] == [{"text": "réponse", "parts": ["a"]}]


@pytest.mark.parametrize("work", INVALID_PAYLOADS)
def test_pg_append_user_work_rejects_invalid_payload(engine, Sessions, db, work):
    event_id = _committed_event(Sessions, work=[{"n": 1}])
    before = _row(engine, event_id)
    with pytest.raises(InvalidCognitivePayload):
        cc.append_user_work(db, event_id=event_id, work=work)
    assert cc.get_event(db, event_id=event_id).user_work_snapshot == [{"n": 1}]
    db.commit()
    assert _row(engine, event_id) == before


def test_pg_append_user_work_unknown_event(db):
    with pytest.raises(CognitiveEventNotFound):
        cc.append_user_work(db, event_id=uuid.uuid4(), work={})


# --- add_support_trace ------------------------------------------------------

def test_pg_add_support_trace_allocates_sequence(engine, db, clock):
    event = cc.open_event(db, **_open_kwargs())
    other = cc.open_event(db, **_open_kwargs())
    kinds = ["rephrase", "hint", "formula", "worked_example", "explanation", "feedback",
             "answer_reveal", "data_provided", "aide_future_42"]
    for expected_seq, kind in enumerate(kinds, start=1):
        trace = cc.add_support_trace(db, event_id=event.id, support_kind=kind,
                                     support_payload={"shown": kind})
        assert isinstance(trace.id, uuid.UUID)
        assert (trace.cognitive_event_id, trace.sequence_no, trace.support_kind) == (event.id, expected_seq, kind)
        # Un seul `now` : created_at de la trace == updated_at de l'événement.
        _assert_utc(trace.created_at)
        assert trace.created_at == event.updated_at > event.started_at
    # Séquence propre à chaque événement.
    assert cc.add_support_trace(db, event_id=other.id, support_kind="hint",
                                support_payload={}).sequence_no == 1
    db.commit()

    assert _traces(engine, event.id) == [(i, k, {"shown": k}) for i, k in enumerate(kinds, start=1)]
    assert [t.sequence_no for t in cc.get_support_traces(db, event_id=event.id)] == list(range(1, len(kinds) + 1))
    stored = _row(engine, event.id)
    assert stored["updated_at"] == max(t.created_at for t in cc.get_support_traces(db, event_id=event.id))
    assert stored["user_work_snapshot"] == [] and stored["status"] == "open"


def test_pg_add_support_trace_continues_after_existing_traces(engine, Sessions, db):
    event_id = _committed_event(Sessions, traces=["hint", "formula"])
    trace = cc.add_support_trace(db, event_id=event_id, support_kind="feedback", support_payload={})
    assert trace.sequence_no == 3
    db.commit()
    assert [seq for seq, _, _ in _traces(engine, event_id)] == [1, 2, 3]


def test_pg_add_support_trace_copies_payload_defensively(engine, db):
    event = cc.open_event(db, **_open_kwargs())
    payload = {"text": "Pense aux capitaux propres.", "data": {"equity": [1, 2]}}
    trace = cc.add_support_trace(db, event_id=event.id, support_kind="hint", support_payload=payload)
    payload["text"] = "modifiée"
    payload["data"]["equity"].append(3)
    expected = {"text": "Pense aux capitaux propres.", "data": {"equity": [1, 2]}}
    assert trace.support_payload == expected
    db.commit()
    assert _traces(engine, event.id) == [(1, "hint", expected)]


@pytest.mark.parametrize("kind", ["", "   ", None, 3])
def test_pg_add_support_trace_rejects_blank_kind(engine, Sessions, db, kind):
    event_id = _committed_event(Sessions)
    before = _row(engine, event_id)
    with pytest.raises(InvalidCognitivePayload, match="support_kind"):
        cc.add_support_trace(db, event_id=event_id, support_kind=kind, support_payload={})
    db.commit()
    assert _traces(engine, event_id) == [] and _row(engine, event_id) == before


@pytest.mark.parametrize("payload", INVALID_PAYLOADS)
def test_pg_add_support_trace_rejects_invalid_payload(engine, Sessions, db, payload):
    event_id = _committed_event(Sessions)
    with pytest.raises(InvalidCognitivePayload):
        cc.add_support_trace(db, event_id=event_id, support_kind="hint", support_payload=payload)
    db.commit()
    assert _traces(engine, event_id) == []


def test_pg_add_support_trace_unknown_event(db):
    with pytest.raises(CognitiveEventNotFound):
        cc.add_support_trace(db, event_id=uuid.uuid4(), support_kind="hint", support_payload={})


# --- get_event / get_support_traces -----------------------------------------

def test_pg_get_event_and_traces_unknown_event(db):
    with pytest.raises(CognitiveEventNotFound):
        cc.get_event(db, event_id=uuid.uuid4())
    with pytest.raises(CognitiveEventNotFound):
        cc.get_support_traces(db, event_id=uuid.uuid4())


def test_pg_get_support_traces_orders_by_sequence_no(engine, Sessions, db):
    """L'ordre restitué ne dépend ni de l'ordre d'insertion ni de created_at :
    traces insérées dans le désordre, created_at inversés."""
    event_id = _committed_event(Sessions)
    with engine.begin() as conn:
        for seq, minutes in ((3, 1), (1, 3), (4, 0), (2, 2)):
            conn.execute(sa.text(
                "INSERT INTO support_traces (id, cognitive_event_id, sequence_no, support_kind, "
                "support_payload, created_at) VALUES (:id, :e, :s, 'hint', CAST(:p AS JSONB), "
                "now() - make_interval(mins => :m))"
            ), {"id": uuid.uuid4(), "e": event_id, "s": seq, "p": f'{{"n": {seq}}}', "m": minutes})

    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(" ".join(statement.split()))

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        traces = cc.get_support_traces(db, event_id=event_id)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert [t.sequence_no for t in traces] == [1, 2, 3, 4]
    assert [t.support_payload for t in traces] == [{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}]
    assert all(isinstance(t, SupportTrace) for t in traces)
    assert statements[-1].endswith("ORDER BY support_traces.sequence_no ASC")
    assert cc.add_support_trace(db, event_id=event_id, support_kind="hint", support_payload={}).sequence_no == 5


def test_pg_get_support_traces_empty(db):
    event = cc.open_event(db, **_open_kwargs())
    assert cc.get_support_traces(db, event_id=event.id) == []


# --- lifecycle --------------------------------------------------------------

@pytest.mark.parametrize("close, status", [("finalize_event", "finalized"), ("abandon_event", "abandoned")])
def test_pg_open_event_can_be_closed(engine, db, clock, close, status):
    event = cc.open_event(db, **_open_kwargs())
    cc.append_user_work(db, event_id=event.id, work={"n": 1})
    closed = getattr(cc, close)(db, event_id=event.id)
    assert closed is event
    assert event.status == status
    assert event.closed_at == event.updated_at == clock + timedelta(seconds=2)
    assert event.started_at == clock
    db.commit()
    stored = _row(engine, event.id)
    assert stored["status"] == status
    assert stored["closed_at"] is not None
    _assert_utc(stored["closed_at"])
    assert stored["closed_at"] == stored["updated_at"] == clock + timedelta(seconds=2)
    assert stored["user_work_snapshot"] == [{"n": 1}]


@pytest.mark.parametrize("first", ["finalize_event", "abandon_event"])
@pytest.mark.parametrize("attempt", sorted(MUTATIONS))
def test_pg_closed_event_is_immutable(engine, Sessions, db, first, attempt):
    """finalized et abandoned sont terminaux : aucune production, aucune
    aide, aucune seconde transition (pas de no-op) ; l'état persisté —
    snapshots, timestamps, traces — reste strictement inchangé."""
    event_id = _committed_event(Sessions, work=[{"n": 1}], traces=["hint"], close=first,
                                stimulus_snapshot=STIMULUS)
    before, traces_before = _row(engine, event_id), _traces(engine, event_id)

    with pytest.raises(CognitiveEventClosed):
        MUTATIONS[attempt](db, event_id)
    event = cc.get_event(db, event_id=event_id)
    assert (event.status, event.closed_at, event.updated_at) == (
        before["status"], before["closed_at"], before["updated_at"])
    db.commit()

    assert _row(engine, event_id) == before
    assert _traces(engine, event_id) == traces_before
    assert before["stimulus_snapshot"] == STIMULUS
    assert before["closed_at"] == before["updated_at"]


# --- verrouillage -----------------------------------------------------------

@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_pg_every_mutation_locks_the_event_before_anything_else(engine, Sessions, db, name):
    """Premier ordre SQL de chaque mutation : SELECT ... FOR UPDATE sur la
    ligne de l'événement (le statut n'est lu qu'ensuite)."""
    event_id = _committed_event(Sessions)
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(" ".join(statement.split()))

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        MUTATIONS[name](db, event_id)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements[0].startswith("SELECT cognitive_events.id")
    assert "FROM cognitive_events WHERE cognitive_events.id = " in statements[0]
    assert statements[0].endswith("FOR UPDATE")
    assert not any("FOR UPDATE" in s for s in statements[1:])


def test_pg_lock_reloads_a_stale_event_already_in_the_session(Sessions, db):
    """L'événement déjà chargé (open) dans la Session est rechargé sous le
    verrou : une fermeture commitée entre-temps par une autre transaction
    est vue, la mutation est refusée."""
    event_id = _committed_event(Sessions)
    stale = cc.get_event(db, event_id=event_id)
    assert stale.status == "open"
    with Sessions() as other:
        cc.finalize_event(other, event_id=event_id)
        other.commit()
    with pytest.raises(CognitiveEventClosed):
        cc.append_user_work(db, event_id=event_id, work={})
    assert stale.status == "finalized"


def _backend_pid(session) -> int:
    return session.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()


def _wait_until_blocked_by(engine, waiting_pid, holder_pid, timeout=10.0):
    """Attend (condition observable, pas un délai arbitraire) que la
    connexion `waiting_pid` soit bloquée par un verrou de `holder_pid`."""
    deadline = time.monotonic() + timeout
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        while time.monotonic() < deadline:
            if conn.execute(sa.text("SELECT :h = ANY(pg_blocking_pids(:w))"),
                            {"h": holder_pid, "w": waiting_pid}).scalar_one():
                return
            time.sleep(0.005)
    pytest.fail(f"la connexion {waiting_pid} n'a jamais attendu le verrou de {holder_pid}")


def _run_blocked(engine, holder, waiter, action):
    """`holder` détient déjà le verrou de l'événement (transaction ouverte).
    `action(waiter)` est lancée dans un thread ; une fois qu'elle est
    réellement bloquée par `holder`, holder commite. Retourne
    (résultat, exception) de l'action, dont la transaction est commitée si
    elle a réussi."""
    holder_pid, waiter_pid = _backend_pid(holder), _backend_pid(waiter)
    outcome = {}

    def work():
        try:
            outcome["result"] = action(waiter)
            waiter.commit()
        except Exception as exc:  # noqa: BLE001 — restitué au test
            outcome["error"] = exc
            waiter.rollback()

    thread = threading.Thread(target=work)
    thread.start()
    try:
        _wait_until_blocked_by(engine, waiter_pid, holder_pid)
        assert "result" not in outcome and "error" not in outcome
        holder.commit()
    finally:
        if thread.is_alive() and holder.in_transaction():
            holder.rollback()
        thread.join(timeout=10)
    assert not thread.is_alive()
    return outcome.get("result"), outcome.get("error")


def test_pg_concurrent_support_traces_get_distinct_ordered_sequences(engine, Sessions):
    event_id = _committed_event(Sessions)
    with Sessions() as first, Sessions() as second:
        assert cc.add_support_trace(first, event_id=event_id, support_kind="hint",
                                    support_payload={"from": "first"}).sequence_no == 1
        trace, error = _run_blocked(engine, first, second, lambda s: cc.add_support_trace(
            s, event_id=event_id, support_kind="formula", support_payload={"from": "second"}).sequence_no)
    assert error is None
    assert trace == 2
    assert _traces(engine, event_id) == [
        (1, "hint", {"from": "first"}),
        (2, "formula", {"from": "second"}),
    ]


def test_pg_concurrent_appends_are_serialized(engine, Sessions):
    """Sans verrou, la seconde transaction réécrirait user_work_snapshot à
    partir d'un état obsolète et perdrait la première production."""
    event_id = _committed_event(Sessions, work=[{"n": 0}])
    with Sessions() as first, Sessions() as second:
        # État [{"n": 0}] déjà chargé (référence conservée : l'identity map
        # ne garde que des références faibles).
        seen = cc.get_event(second, event_id=event_id)
        assert seen.user_work_snapshot == [{"n": 0}]
        cc.append_user_work(first, event_id=event_id, work={"n": 1})
        _, error = _run_blocked(engine, first, second,
                                lambda s: cc.append_user_work(s, event_id=event_id, work={"n": 2}))
    assert error is None
    assert _row(engine, event_id)["user_work_snapshot"] == [{"n": 0}, {"n": 1}, {"n": 2}]


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_pg_mutation_waiting_on_a_closing_transaction_is_refused(engine, Sessions, name):
    """Une production, une aide ou une transition qui attend le verrou d'une
    finalisation en cours voit l'événement fermé une fois le verrou obtenu."""
    event_id = _committed_event(Sessions, work=[{"n": 1}], traces=["hint"])
    with Sessions() as closer, Sessions() as other:
        seen = cc.get_event(other, event_id=event_id)  # référence conservée
        assert seen.status == "open"
        cc.finalize_event(closer, event_id=event_id)
        _, error = _run_blocked(engine, closer, other, lambda s: MUTATIONS[name](s, event_id))
        assert isinstance(error, CognitiveEventClosed)
        assert seen.status == "finalized"
    stored = _row(engine, event_id)
    assert stored["status"] == "finalized"
    assert stored["user_work_snapshot"] == [{"n": 1}]
    assert stored["closed_at"] == stored["updated_at"]
    assert _traces(engine, event_id) == [(1, "hint", {"k": "hint"})]
