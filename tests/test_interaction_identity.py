"""Tests de R1-B : registre d'appartenance des conversations
(core/interaction_identity.py).

1. Tests sans base (toujours exécutés) : API publique exacte, aucune
   transaction possédée (un seul SAVEPOINT local, ciblé), aucun
   branchement applicatif, validation structurelle.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T2-B) :
   uniquement si ORYX_TEST_DATABASE_URL pointe vers une base DÉDIÉE dont
   le nom contient "test". Sinon SKIPPÉS : rien n'est simulé avec SQLite.
   Les courses utilisent deux connexions réelles et pg_blocking_pids().
"""
import ast
import inspect

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from core import interaction_identity as ii
from core.interaction_identity import (
    ConversationOwnershipConflict,
    InteractionIdentityError,
    InvalidConversationIdentity,
)
from core.models import ConversationIdentity, User
from tests.test_cognitive_capture import _NoDB, _run_blocked, _Statements
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_migration_0005_cognitive_support_traces import OTHER_USER, USER, _upgrade_head_with_users

SERVICE_PATH = REPO_ROOT / "core" / "interaction_identity.py"
INVALID = ["", " ", "\n", None, 3, b"k", "a\x00b"]


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_public_api_is_exactly_register_or_resolve():
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == {"register_or_resolve_conversation"}
    params = list(inspect.signature(ii.register_or_resolve_conversation).parameters.values())
    assert [p.name for p in params] == ["db", "user_id", "conversation_key"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:])


def test_exceptions():
    for exc in (InvalidConversationIdentity, ConversationOwnershipConflict):
        assert issubclass(exc, InteractionIdentityError)
        assert not issubclass(exc, sa.exc.IntegrityError)
    assert not issubclass(InteractionIdentityError, (LookupError, ValueError))


def test_service_owns_no_transaction_beyond_one_local_savepoint():
    source = SERVICE_PATH.read_text(encoding="utf-8")
    tokens = _code_tokens(source).split("\n")
    for forbidden in ("commit", "rollback", "begin", "close", "SessionLocal", "get_db", "HTTPException",
                      "delete", "update", "merge", "with_for_update"):
        assert forbidden not in tokens, forbidden
    assert tokens.count("begin_nested") == 1
    tree = ast.parse(source)
    (with_node,) = [w for w in ast.walk(tree) if isinstance(w, ast.With)]
    assert ast.unparse(with_node.items[0].context_expr) == "db.begin_nested()"
    assert [ast.unparse(stmt) for stmt in with_node.body] == ["db.add(identity)", "db.flush()"]

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
    assert imported == {"datetime", "sqlalchemy", "sqlalchemy.exc", "core.models"}


def test_registry_carries_no_conversation_content_or_pedagogy():
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).lower()
    for word in ("message", "history", "surface", "title", "summary", "route", "level", "competenc",
                 "stage", "score", "observation", "cognitive"):
        assert word not in tokens, word


@pytest.mark.parametrize("field", ["user_id", "conversation_key"])
@pytest.mark.parametrize("value", INVALID)
def test_invalid_inputs_are_refused_before_db(field, value):
    kwargs = {"user_id": USER, "conversation_key": "c", field: value}
    with pytest.raises(InvalidConversationIdentity, match=field):
        ii.register_or_resolve_conversation(_NoDB(), **kwargs)


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine(pg_url):  # noqa: F811
    eng = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    _upgrade_head_with_users(pg_url, eng)
    yield eng
    eng.dispose()


@pytest.fixture
def Sessions(engine):
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)  # = core.db.SessionLocal
    yield factory
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM conversation_identities"))
        conn.execute(sa.text("DELETE FROM users WHERE id NOT IN ('u', 'u2')"))


@pytest.fixture
def db(Sessions):
    session = Sessions()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _rows(engine):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT conversation_key, user_id FROM conversation_identities ORDER BY 1"))]


def test_pg_new_conversation_is_registered_without_commit(engine, db):
    identity = ii.register_or_resolve_conversation(db, user_id=USER, conversation_key="c1")
    assert isinstance(identity, ConversationIdentity)
    assert (identity.conversation_key, identity.user_id) == ("c1", USER)
    assert identity.created_at.utcoffset().total_seconds() == 0
    assert _rows(engine) == []  # rien de commité par le service
    db.commit()
    assert _rows(engine) == [("c1", USER)]


def test_pg_retry_returns_the_same_row_without_writing(engine, Sessions, db):
    with Sessions() as session:
        created = ii.register_or_resolve_conversation(session, user_id=USER, conversation_key="c1")
        created_at = created.created_at
        assert ii.register_or_resolve_conversation(session, user_id=USER, conversation_key="c1") is created
        session.commit()
    with _Statements(engine) as statements:
        again = ii.register_or_resolve_conversation(db, user_id=USER, conversation_key="c1")
    assert all(s.startswith("SELECT") for s in statements)
    assert (again.user_id, again.created_at) == (USER, created_at)
    db.commit()
    assert _rows(engine) == [("c1", USER)]


def test_pg_other_owner_is_refused_and_never_reassigned(engine, Sessions, db):
    with Sessions() as session:
        ii.register_or_resolve_conversation(session, user_id=USER, conversation_key="c1")
        session.commit()
    with pytest.raises(ConversationOwnershipConflict):
        ii.register_or_resolve_conversation(db, user_id=OTHER_USER, conversation_key="c1")
    # La transaction reste utilisable.
    ii.register_or_resolve_conversation(db, user_id=OTHER_USER, conversation_key="c2")
    db.commit()
    assert _rows(engine) == [("c1", USER), ("c2", OTHER_USER)]


def test_pg_caller_rollback_discards_the_registration(engine, db):
    db.add(User(id="pending-user", level="debutant"))
    db.flush()
    ii.register_or_resolve_conversation(db, user_id="pending-user", conversation_key="c1")
    db.rollback()
    assert _rows(engine) == []
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users WHERE id = 'pending-user'")).scalar_one() == 0


def test_pg_unknown_user_fails_on_the_fk_without_ghost_row(engine, db):
    """SAVEPOINT : transaction de l'appelant intacte, aucune ligne fantôme,
    et son écriture en attente (non flushée) n'est pas perdue."""
    db.add(User(id="pending-user", level="debutant"))
    with pytest.raises(sa.exc.IntegrityError, match="conversation_identities_user_id_fkey"):
        ii.register_or_resolve_conversation(db, user_id="ghost", conversation_key="c1")
    assert db.execute(sa.text("SELECT count(*) FROM conversation_identities")).scalar_one() == 0
    ii.register_or_resolve_conversation(db, user_id="pending-user", conversation_key="c1")
    db.commit()
    assert _rows(engine) == [("c1", "pending-user")]


def test_pg_concurrent_registration_by_the_same_user_resolves_to_one_row(engine, Sessions):
    """Course réelle : le second INSERT attend le premier sur la PK, reçoit
    la violation après son commit, revient au SAVEPOINT et retourne la
    ligne gagnante ; sa transaction reste utilisable."""
    with Sessions() as first, Sessions() as second:
        ii.register_or_resolve_conversation(first, user_id=USER, conversation_key="c1")

        def action(session):
            with _Statements(engine) as statements:
                identity = ii.register_or_resolve_conversation(session, user_id=USER, conversation_key="c1")
            assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in statements)
            ii.register_or_resolve_conversation(session, user_id=USER, conversation_key="c2")
            return identity.user_id

        result, error = _run_blocked(engine, first, second, action)
    assert error is None and result == USER
    assert _rows(engine) == [("c1", USER), ("c2", USER)]


def test_pg_concurrent_registration_by_another_user_fails_closed(engine, Sessions):
    with Sessions() as first, Sessions() as second:
        ii.register_or_resolve_conversation(first, user_id=USER, conversation_key="c1")

        def action(session):
            try:
                ii.register_or_resolve_conversation(session, user_id=OTHER_USER, conversation_key="c1")
            except ConversationOwnershipConflict:
                return session.execute(sa.text("SELECT 1")).scalar_one()
            raise AssertionError("conflit attendu")

        result, error = _run_blocked(engine, first, second, action)
    assert error is None and result == 1
    assert _rows(engine) == [("c1", USER)]


def test_pg_concurrent_registration_after_a_rollback_belongs_to_the_survivor(engine, Sessions):
    with Sessions() as first, Sessions() as second:
        ii.register_or_resolve_conversation(first, user_id=USER, conversation_key="c1")
        result, error = _run_blocked(
            engine, first, second,
            lambda s: ii.register_or_resolve_conversation(s, user_id=OTHER_USER, conversation_key="c1").user_id,
            release="rollback")
    assert error is None and result == OTHER_USER
    assert _rows(engine) == [("c1", OTHER_USER)]
