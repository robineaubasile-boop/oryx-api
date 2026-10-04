"""Tests de T2-B / R1-B : service interne de capture cognitive
(core/cognitive_capture.py).

Le schéma testé est celui produit par `alembic upgrade head` (=
0010_r1b_event_idempotence depuis R1-B : registre conversation_identities,
identité event_dedup_key + versions, task_kind nullable).

1. Tests sans base (toujours exécutés) : API publique exacte (aucune voie
   d'écriture legacy ni fonction update/delete), aucun commit/rollback ni
   dépendance FastAPI dans le service (un seul SAVEPOINT local, ciblé),
   aucun branchement applicatif, aucun T3, validation structurelle des
   entrées, formule figée d'event_dedup_key, requête de verrouillage FOR
   UPDATE.

2. Tests contre un vrai PostgreSQL : uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (schéma public
   détruit et recréé). Sinon SKIPPÉS : rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_r1b_test \\
           python -m pytest tests/test_cognitive_capture.py

   Les sessions sont créées comme core.db.SessionLocal (autoflush=False) ;
   la transaction appartient toujours au test (l'appelant). Les tests de
   concurrence utilisent deux connexions réelles et attendent, via
   pg_blocking_pids(), que la seconde soit effectivement bloquée par la
   première avant de la libérer : aucun sleep() arbitraire.
"""
import ast
import hashlib
import inspect
import itertools
import json
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
    AnalysisSessionNotFound,
    AnalysisSessionOwnershipConflict,
    CognitiveCaptureError,
    CognitiveEventClosed,
    CognitiveEventNotFound,
    ContributionIdentityCollision,
    EventDedupCollision,
    InvalidCognitivePayload,
    UnsupportedCognitiveEventSchema,
)
from core.interaction_identity import ConversationOwnershipConflict
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
from tests.test_migration_0005_cognitive_support_traces import OTHER_USER, T2A, USER, _upgrade_head_with_users

T3A = "0006_observation_layer"
T4A = "0007_pedagogical_taxonomy"
T5A = "0008_longitudinal_relations"
T6A = "0009_competency_inference_state"
R1B = "0010_r1b_event_idempotence"
R1C1 = "0011_assistant_deliveries"

SERVICE_PATH = REPO_ROOT / "core" / "cognitive_capture.py"
PUBLIC_API = {
    "open_event_idempotent",
    "append_user_contribution",
    "add_support_trace",
    "finalize_event",
    "abandon_event",
    "get_event",
    "get_support_traces",
}
MUTATIONS = {
    "append_user_contribution": lambda db, eid: cc.append_user_contribution(db, event_id=eid, **_contribution()),
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
BLANK_OR_INVALID_STRINGS = ["", " ", "\t\n", None, 42, b"x", "a\x00b", "\ud800"]
TURN = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TURN_2 = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


class _Color(str, Enum):
    RED = "red"


class _UUIDSubclass(uuid.UUID):
    pass


INVALID_PAYLOADS.append({"enum": _Color.RED})


class _NoDB:
    """Session factice : la validation structurelle doit échouer avant tout
    accès à la base."""

    def __getattr__(self, name):
        raise AssertionError(f"accès à la base inattendu : {name}")


def _open_kwargs(**overrides):
    """Arguments d'ouverture ; chaque appel a sa propre identité de
    segmentation (tour source frais) sauf si source_turn_refs est fourni."""
    user_id = overrides.get("user_id", USER)
    kwargs = {
        "user_id": user_id,
        "conversation_key": f"conv-{user_id}",
        "source_turn_refs": (uuid.uuid4(),),
        "segmentation_ordinal": 1,
        "event_origin": "education",
        "task_kind": "explain_concept",
        "stimulus_snapshot": {"question": "Que mesure le ROE ?"},
        "event_builder_version": cc.EVENT_BUILDER_VERSION,
        "admission_version": cc.ADMISSION_VERSION,
    }
    kwargs.update(overrides)
    return kwargs


def _contribution(**overrides):
    kwargs = {
        "contribution_id": uuid.uuid4(),
        "source_turn_ref": uuid.uuid4(),
        "surface": "web-chat",
        "session_ref": "conv-u",
        "text_excerpt": "rentabilité des capitaux propres",
    }
    kwargs.update(overrides)
    return kwargs


def capture_event(session, *, text="rentabilité des capitaux propres", close=None, **overrides):
    """Helper partagé par les tests T3+ : ouvre un événement R1-B, y ajoute
    une contribution, puis le ferme éventuellement (sans commit)."""
    event = cc.open_event_idempotent(session, **_open_kwargs(**overrides))
    cc.append_user_contribution(session, event_id=event.id, **_contribution(
        session_ref=event.conversation_key, text_excerpt=text))
    if close:
        getattr(cc, close)(session, event_id=event.id)
    return event


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_head_is_0011_after_r1b_and_r1c1():
    """T2-B n'a ajouté aucune migration ; R1-B ajoute 0010, R1-C1 ajoute
    0011 (assistant_deliveries, sans lien avec ce service), la tête."""
    script = _script_directory()
    assert script.get_heads() == [R1C1]
    assert {rev.revision for rev in script.walk_revisions()} == {BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A,
                                                                 T6A, R1B, R1C1}
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [f"{rev}.py" for rev in (BASELINE, T1A, T1B1, T1C2, T2A, T3A, T4A, T5A, T6A, R1B, R1C1)]


def test_public_api_is_exactly_the_capture_functions():
    """Pas de CRUD générique, pas d'update/delete de SupportTrace, pas de
    get_or_create générique, et plus aucune voie d'écriture legacy
    (open_event / append_user_work bruts)."""
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {f for f in functions if not f.startswith("_")} == PUBLIC_API
    assert not hasattr(cc, "open_event") and not hasattr(cc, "append_user_work")
    for name in PUBLIC_API:
        params = list(inspect.signature(getattr(cc, name)).parameters.values())
        assert params[0].name == "db" and params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:]), name


def test_open_and_append_signatures_are_exact():
    """L'appelant ne fournit ni event_dedup_key, ni event_schema_version, ni
    phase, occurred_at ou support_refs_before ; les versions de politique
    sont explicites (aucun défaut)."""
    params = inspect.signature(cc.open_event_idempotent).parameters
    assert list(params) == ["db", "user_id", "conversation_key", "source_turn_refs", "segmentation_ordinal",
                            "event_origin", "stimulus_snapshot", "event_builder_version", "admission_version",
                            "task_kind", "analysis_session_id"]
    assert {n: p.default for n, p in params.items() if p.default is not inspect.Parameter.empty} == {
        "task_kind": None, "analysis_session_id": None}
    assert list(inspect.signature(cc.append_user_contribution).parameters) == [
        "db", "event_id", "contribution_id", "source_turn_ref", "surface", "session_ref", "text_excerpt"]


def test_versions_are_explicit_and_stable():
    assert cc.EVENT_BUILDER_VERSION == "cognitive-event-builder-1"
    assert cc.ADMISSION_VERSION == "cognitive-event-admission-1"
    assert cc.EVENT_SCHEMA_VERSION == "cognitive-event-v2"
    assert cc.EVENT_DEDUP_SCHEMA_VERSION == "cognitive-event-dedup-v1"
    assert cc.DEDUP_CONSTRAINT == "uq_cognitive_events_event_dedup_key"


def test_exceptions_are_a_small_business_hierarchy():
    for exc in (CognitiveEventNotFound, CognitiveEventClosed, InvalidCognitivePayload, AnalysisSessionNotFound,
                AnalysisSessionOwnershipConflict, EventDedupCollision, UnsupportedCognitiveEventSchema,
                ContributionIdentityCollision):
        assert issubclass(exc, CognitiveCaptureError)
        assert not issubclass(exc, sa.exc.IntegrityError)
    assert issubclass(CognitiveCaptureError, Exception)
    assert not issubclass(CognitiveCaptureError, (LookupError, ValueError))


def _calls(tree, attr) -> list:
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr]


def test_service_owns_no_transaction_and_does_not_depend_on_fastapi():
    """Jamais commit / rollback / begin / close. Une seule exception : UN
    SAVEPOINT local (with db.begin_nested()) autour de l'INSERT de
    open_event_idempotent, pour traduire une collision d'identité."""
    source = SERVICE_PATH.read_text(encoding="utf-8")
    tokens = _code_tokens(source).split("\n")
    for forbidden in ("commit", "rollback", "begin", "close", "SessionLocal", "get_db", "HTTPException",
                      "utcnow", "delete", "update", "merge", "expunge", "invalidate", "connection"):
        assert forbidden not in tokens, forbidden
    assert tokens.count("begin_nested") == 1

    tree = ast.parse(source)
    (call,) = _calls(tree, "begin_nested")
    owner = next(f for f in tree.body if isinstance(f, ast.FunctionDef)
                 and any(n is call for n in ast.walk(f)))
    assert owner.name == "open_event_idempotent"
    withs = [w for w in ast.walk(owner) if isinstance(w, ast.With)]
    assert [w.items[0].context_expr for w in withs] == [call]
    # Le SAVEPOINT n'enveloppe que l'INSERT (add + flush) ; begin_nested()
    # flushe d'abord les écritures en attente de l'appelant, que son
    # annulation ne touche donc pas (testé contre PostgreSQL).
    assert [ast.unparse(stmt) for stmt in withs[0].body] == ["db.add(event)", "db.flush()"]

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{alias.name}" if node.module == "core" else node.module
                         for alias in node.names}
    assert imported == {"hashlib", "json", "math", "uuid", "datetime", "decimal", "sqlalchemy", "sqlalchemy.exc",
                        "core.interaction_identity", "core.models"}


def test_service_does_not_touch_t3_or_any_evaluation():
    source = SERVICE_PATH.read_text(encoding="utf-8")
    for name in ("observation_service", "start_evaluation_run", "add_observation", "complete_evaluation_run",
                 "taxonomy_service", "longitudinal_service", "inference_service", "anthropic"):
        assert name not in source, name


def test_service_introduces_no_evaluation_vocabulary():
    """T2-B capture, n'évalue pas : aucun identifiant ni chaîne de code du
    service ne porte de notion d'évaluation (docstrings exclues)."""
    tokens = _code_tokens(SERVICE_PATH.read_text(encoding="utf-8")).lower()
    for word in ("capabilit", "competenc", "stage", "score", "confidence", "evidence", "contradiction",
                 "mastery", "difficult", "inference", "observation", "taxonomy", "progress",
                 "level"):
        assert word not in tokens, word


def test_service_is_not_wired_to_the_application():
    """Aucune route ni module applicatif n'importe le service de capture ;
    api.py et web-v2 n'y font aucune référence. Seule exception, pour le
    REGISTRE d'identité uniquement (pas la capture) : R1-C1 résout la
    conversation_key d'une livraison assistant Décrypter via
    register_or_resolve_conversation, depuis core/assistant_delivery.py
    (aucun CognitiveEvent ni SupportTrace créé : voir
    tests/test_assistant_delivery.py)."""
    registry_names = ("interaction_identity", "register_or_resolve_conversation")
    capture_names = ("cognitive_capture", "open_event_idempotent", "append_user_contribution", "add_support_trace",
                     "finalize_event", "abandon_event")
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
        if rel not in ("core/cognitive_capture.py", "core/interaction_identity.py"):
            for name in capture_names:
                assert name not in source, (rel, name)
            if rel != "core/assistant_delivery.py":
                for name in registry_names:
                    assert name not in source, (rel, name)
    assert checked > 0


def test_lifecycle_and_derived_fields_cannot_be_supplied():
    for field in ("status", "user_work_snapshot", "started_at", "updated_at", "closed_at", "id",
                  "event_dedup_key", "event_schema_version"):
        with pytest.raises(TypeError):
            cc.open_event_idempotent(_NoDB(), **_open_kwargs(**{field: None}))
    for field in ("phase", "occurred_at", "support_refs_before", "text"):
        with pytest.raises(TypeError):
            cc.append_user_contribution(_NoDB(), event_id=uuid.uuid4(), **_contribution(**{field: None}))
    with pytest.raises(TypeError):
        cc.add_support_trace(_NoDB(), event_id=uuid.uuid4(), support_kind="hint",
                             support_payload={}, sequence_no=1)


@pytest.mark.parametrize("field", ["user_id", "conversation_key", "event_origin", "event_builder_version",
                                   "admission_version"])
@pytest.mark.parametrize("value", BLANK_OR_INVALID_STRINGS)
def test_open_rejects_invalid_required_strings(field, value):
    with pytest.raises(InvalidCognitivePayload, match=field):
        cc.open_event_idempotent(_NoDB(), **_open_kwargs(**{field: value}))


@pytest.mark.parametrize("value", ["", " ", "\t\n", 42, b"x", "a\x00b"])
def test_open_rejects_invalid_task_kind(value):
    """None reste valide (démonstration sans tâche imposée, testé contre
    PostgreSQL) ; une chaîne vide n'en est jamais l'équivalent."""
    with pytest.raises(InvalidCognitivePayload, match="task_kind"):
        cc.open_event_idempotent(_NoDB(), **_open_kwargs(task_kind=value))


def test_conversation_key_is_mandatory_for_new_events():
    kwargs = _open_kwargs()
    del kwargs["conversation_key"]
    with pytest.raises(TypeError):
        cc.open_event_idempotent(_NoDB(), **kwargs)
    with pytest.raises(InvalidCognitivePayload, match="conversation_key"):
        cc.open_event_idempotent(_NoDB(), **_open_kwargs(conversation_key=None))


@pytest.mark.parametrize("payload", INVALID_PAYLOADS)
def test_open_rejects_non_json_stimulus(payload):
    with pytest.raises(InvalidCognitivePayload):
        cc.open_event_idempotent(_NoDB(), **_open_kwargs(stimulus_snapshot=payload))


def test_open_rejects_circular_stimulus():
    cyclic = {"a": []}
    cyclic["a"].append(cyclic)
    with pytest.raises(InvalidCognitivePayload, match="circulaire"):
        cc.open_event_idempotent(_NoDB(), **_open_kwargs(stimulus_snapshot=cyclic))


@pytest.mark.parametrize("value", [str(SESSION_ID), 1, "", True])
def test_open_rejects_invalid_analysis_session_id(value):
    with pytest.raises(InvalidCognitivePayload, match="analysis_session_id"):
        cc.open_event_idempotent(_NoDB(), **_open_kwargs(analysis_session_id=value))


@pytest.mark.parametrize("refs", [
    (),
    [TURN],
    (str(TURN),),
    (TURN.hex,),
    (TURN, TURN),
    (TURN, TURN_2, TURN),
    None,
    TURN,
    (True,),
    (1,),
    (TURN, None),
    (_UUIDSubclass(str(TURN)),),
], ids=repr)
def test_source_turn_refs_must_be_a_non_empty_tuple_of_distinct_uuids(refs):
    with pytest.raises(InvalidCognitivePayload, match="source_turn_refs"):
        cc.open_event_idempotent(_NoDB(), **_open_kwargs(source_turn_refs=refs))


@pytest.mark.parametrize("ordinal", [0, -1, True, False, 1.0, "1", None, 2 ** 0.5])
def test_segmentation_ordinal_must_be_an_int_at_least_one(ordinal):
    with pytest.raises(InvalidCognitivePayload, match="segmentation_ordinal"):
        cc.open_event_idempotent(_NoDB(), **_open_kwargs(segmentation_ordinal=ordinal))


@pytest.mark.parametrize("field, value", [
    ("contribution_id", str(uuid.uuid4())), ("contribution_id", None), ("contribution_id", 1),
    ("contribution_id", _UUIDSubclass(str(TURN))),
    ("source_turn_ref", str(uuid.uuid4())), ("source_turn_ref", None), ("source_turn_ref", (TURN,)),
    *[("surface", v) for v in BLANK_OR_INVALID_STRINGS],
    *[("session_ref", v) for v in BLANK_OR_INVALID_STRINGS],
    *[("text_excerpt", v) for v in BLANK_OR_INVALID_STRINGS],
], ids=repr)
def test_append_contribution_rejects_invalid_inputs_before_db(field, value):
    with pytest.raises(InvalidCognitivePayload, match=field):
        cc.append_user_contribution(_NoDB(), event_id=uuid.uuid4(), **_contribution(**{field: value}))


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


@pytest.mark.parametrize("a, b, equal", [
    ({"x": 1}, {"x": 1.0}, True),
    ({"x": 10 ** 16}, {"x": 1e16}, True),
    ({"x": 10 ** 30}, {"x": 1e30}, True),  # 1e30 relu depuis JSONB : int exact
    ({"x": 15 * 10 ** 299}, {"x": 1.5e300}, True),
    ({"x": 10 ** 30 + 1}, {"x": 1e30}, False),
    ({"x": 0.1}, {"x": 0.30000000000000004 - 0.2}, False),
    ({"x": True}, {"x": 1}, False),
    ({"x": False}, {"x": 0}, False),
    ({"x": [True]}, {"x": [1]}, False),
    ({"x": None}, {"x": False}, False),
    ({"x": "1"}, {"x": 1}, False),
    ({"x": [1, 2]}, {"x": [2, 1]}, False),
    ({"x": {"a": 1, "b": 2}}, {"x": {"b": 2, "a": 1}}, True),
    ({"x": {}}, {"x": {"y": None}}, False),
    ({}, {}, True),
])
def test_json_equality_follows_jsonb_semantics_but_never_confuses_bool_and_number(a, b, equal):
    assert cc._json_equal(a, b) is equal
    assert cc._json_equal(b, a) is equal


# --- event_dedup_key : formule figée ----------------------------------------

IDENTITY = {
    "user_id": "u",
    "conversation_key": "conv-1",
    "source_turn_refs": (TURN, TURN_2),
    "segmentation_ordinal": 1,
    "event_builder_version": "cognitive-event-builder-1",
}


def test_dedup_key_formula_is_frozen():
    """SHA-256 hex minuscule du JSON canonique (clés triées, séparateurs
    compacts, UTF-8 sans échappement ASCII)."""
    canonical = (
        '{"conversation_key":"conv-1","dedup_schema_version":"cognitive-event-dedup-v1",'
        '"event_builder_version":"cognitive-event-builder-1","segmentation_ordinal":1,'
        '"source_turn_refs":["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"],'
        '"user_id":"u"}'
    )
    key = cc._event_dedup_key(**IDENTITY)
    assert key == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert len(key) == 64 and key == key.lower() and set(key) <= set("0123456789abcdef")
    # Non ASCII : hashé en UTF-8 sans échappement \\u.
    accented = cc._event_dedup_key(**{**IDENTITY, "conversation_key": "conv-é"})
    expected = json.dumps({"conversation_key": "conv-é", "dedup_schema_version": "cognitive-event-dedup-v1",
                           "event_builder_version": "cognitive-event-builder-1", "segmentation_ordinal": 1,
                           "source_turn_refs": [str(TURN), str(TURN_2)], "user_id": "u"},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert "conv-é" in expected
    assert accented == hashlib.sha256(expected.encode("utf-8")).hexdigest()


def test_dedup_key_is_deterministic():
    assert cc._event_dedup_key(**IDENTITY) == cc._event_dedup_key(**dict(IDENTITY))
    assert cc._event_dedup_key(**IDENTITY) == cc._event_dedup_key(
        **{**IDENTITY, "source_turn_refs": (uuid.UUID(str(TURN)), uuid.UUID(TURN_2.hex))})


@pytest.mark.parametrize("change", [
    {"user_id": "u2"},
    {"conversation_key": "conv-2"},
    {"source_turn_refs": (TURN,)},
    {"source_turn_refs": (TURN_2, TURN)},
    {"source_turn_refs": (TURN, uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"))},
    {"segmentation_ordinal": 2},
    {"event_builder_version": "cognitive-event-builder-2"},
], ids=lambda c: next(iter(c)))
def test_every_identity_component_changes_the_key(change):
    assert cc._event_dedup_key(**{**IDENTITY, **change}) != cc._event_dedup_key(**IDENTITY)


def test_dedup_key_takes_only_the_segmentation_identity():
    """Ni texte, stimulus, task_kind, origine, analysis_session_id,
    horodatage ni identifiant d'événement : la signature EST la formule."""
    assert list(inspect.signature(cc._event_dedup_key).parameters) == [
        "user_id", "conversation_key", "source_turn_refs", "segmentation_ordinal", "event_builder_version"]
    with pytest.raises(TypeError):
        cc._event_dedup_key(**IDENTITY, stimulus_snapshot={})
    tokens = _code_tokens(inspect.getsource(cc._event_dedup_key))
    for forbidden in ("hash", "random", "uuid4", "_utcnow", "now", "time"):
        assert forbidden not in tokens.split("\n"), forbidden


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
    """Schéma = head (0011 depuis R1-C1) + deux utilisateurs et une analysis_session,
    créé une fois pour le module ; chaque test nettoie ses écritures."""
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
        conn.execute(sa.text("DELETE FROM conversation_identities"))
        conn.execute(sa.text("DELETE FROM analysis_sessions WHERE id <> :s"), {"s": SESSION_ID})
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


EVENT_COLUMNS = ("status, user_id, conversation_key, event_origin, task_kind, analysis_session_id, "
                 "stimulus_snapshot, user_work_snapshot, started_at, updated_at, closed_at, event_dedup_key, "
                 "event_builder_version, admission_version, event_schema_version")


def _row(engine, event_id):
    """État persisté, lu par une connexion indépendante (donc seulement ce
    qui a été commité)."""
    with engine.connect() as conn:
        row = conn.execute(sa.text(f"SELECT {EVENT_COLUMNS} FROM cognitive_events WHERE id = :id"),
                           {"id": event_id}).mappings().one_or_none()
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


def _conversations(engine):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT conversation_key, user_id FROM conversation_identities ORDER BY 1"))]


def _committed_event(Sessions, *, contributions=0, traces=(), close=None, **overrides):
    """Crée et commite un événement (en tant qu'appelant), éventuellement
    enrichi puis fermé."""
    with Sessions() as session:
        event_id = cc.open_event_idempotent(session, **_open_kwargs(**overrides)).id
        for n in range(contributions):
            cc.append_user_contribution(session, event_id=event_id, **_contribution(text_excerpt=f"réponse {n}"))
        for kind in traces:
            cc.add_support_trace(session, event_id=event_id, support_kind=kind, support_payload={"k": kind})
        if close:
            getattr(cc, close)(session, event_id=event_id)
        session.commit()
    return event_id


def _legacy_event(engine, *, status="open", work='[{"text": "réponse"}]'):
    """Ligne pré-R1-B : aucune identité ni version, conversation_key NULL,
    user_work_snapshot sans contribution_id (jamais backfillée)."""
    event_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO cognitive_events (id, user_id, event_origin, task_kind, status, stimulus_snapshot, "
            "user_work_snapshot, started_at, updated_at, closed_at) VALUES (:id, :u, 'education', "
            "'interpret_metric', :s, CAST('{}' AS JSONB), CAST(:w AS JSONB), now(), now(), "
            "CASE WHEN :s = 'open' THEN NULL ELSE now() END)"
        ), {"id": event_id, "u": USER, "s": status, "w": work})
    return event_id


class _Statements:
    """Enregistre les ordres SQL émis par le moteur pendant le bloc."""

    def __init__(self, engine):
        self.engine, self.items = engine, []

    def _record(self, conn, cursor, statement, *args):
        self.items.append(" ".join(statement.split()))

    def __enter__(self):
        sa.event.listen(self.engine, "before_cursor_execute", self._record)
        return self.items

    def __exit__(self, *exc):
        sa.event.remove(self.engine, "before_cursor_execute", self._record)


def _writes(statements):
    return [s for s in statements if not s.upper().startswith("SELECT")]


def _assert_utc(value):
    assert value.tzinfo is not None and value.utcoffset() == timedelta(0)


# --- open_event_idempotent : nouvelle identité ------------------------------

def test_pg_open_creates_a_versioned_open_event(engine, db, clock):
    refs = (TURN, TURN_2)
    event = cc.open_event_idempotent(db, **_open_kwargs(stimulus_snapshot=STIMULUS, source_turn_refs=refs,
                                                        segmentation_ordinal=2))
    assert isinstance(event.id, uuid.UUID)
    assert event.status == "open"
    assert (event.user_id, event.conversation_key) == (USER, f"conv-{USER}")
    assert event.event_origin == "education" and event.task_kind == "explain_concept"
    assert event.analysis_session_id is None
    assert event.stimulus_snapshot == STIMULUS
    assert event.user_work_snapshot == []
    assert event.started_at == event.updated_at == clock
    assert event.closed_at is None
    assert event.event_dedup_key == cc._event_dedup_key(
        user_id=USER, conversation_key=f"conv-{USER}", source_turn_refs=refs, segmentation_ordinal=2,
        event_builder_version=cc.EVENT_BUILDER_VERSION)
    assert (event.event_builder_version, event.admission_version, event.event_schema_version) == (
        "cognitive-event-builder-1", "cognitive-event-admission-1", "cognitive-event-v2")

    # Ligne matérialisée par le flush, relue dans la transaction de l'appelant.
    row = db.execute(sa.text(
        f"SELECT {EVENT_COLUMNS}, jsonb_typeof(user_work_snapshot) AS work_type FROM cognitive_events "
        "WHERE id = :id"), {"id": event.id}).mappings().one()
    assert (row["status"], row["stimulus_snapshot"], row["user_work_snapshot"], row["work_type"]) == (
        "open", STIMULUS, [], "array")
    assert row["event_dedup_key"] == event.event_dedup_key
    assert row["event_schema_version"] == cc.EVENT_SCHEMA_VERSION
    _assert_utc(row["started_at"])
    # Conversation enregistrée pour ce user dans la même transaction.
    assert db.execute(sa.text("SELECT user_id FROM conversation_identities WHERE conversation_key = :k"),
                      {"k": f"conv-{USER}"}).scalar_one() == USER

    # Rien n'est commité par le service.
    assert _row(engine, event.id) is None and _conversations(engine) == []
    db.commit()
    assert _row(engine, event.id)["status"] == "open"
    assert _conversations(engine) == [(f"conv-{USER}", USER)]
    assert cc.get_event(db, event_id=event.id) is event


def test_pg_open_real_clock_timestamps_are_utc_aware_and_equal(engine, db):
    event = cc.open_event_idempotent(db, **_open_kwargs())
    db.commit()
    stored = _row(engine, event.id)
    for value in (event.started_at, event.updated_at, stored["started_at"], stored["updated_at"]):
        _assert_utc(value)
    assert stored["started_at"] == stored["updated_at"] == event.started_at
    assert stored["closed_at"] is None


def test_pg_open_without_task_kind_stores_null(engine, db):
    """Démonstration spontanée : aucune valeur factice inventée."""
    event = cc.open_event_idempotent(db, **_open_kwargs(task_kind=None))
    db.commit()
    assert event.task_kind is None and _row(engine, event.id)["task_kind"] is None


def test_pg_open_vocabularies_are_extensible(db):
    for origin, kind in (("education", "explain_concept"), ("coach", "compare_options"),
                         ("academy", "apply_formula"), ("rallye", "identify_risk"),
                         ("portfolio", "build_thesis"), ("origine_future_42", "tache_future_42")):
        event = cc.open_event_idempotent(db, **_open_kwargs(event_origin=origin, task_kind=kind))
        assert (event.event_origin, event.task_kind) == (origin, kind)


def test_pg_open_accepts_empty_stimulus(db):
    event = cc.open_event_idempotent(db, **_open_kwargs(stimulus_snapshot={}))
    db.expire_all()
    assert event.stimulus_snapshot == {}


def test_pg_open_copies_stimulus_defensively(engine, db):
    stimulus = {"question": "q", "displayed_data": {"roe": [0.2, 0.3]}}
    event = cc.open_event_idempotent(db, **_open_kwargs(stimulus_snapshot=stimulus))
    stimulus["question"] = "modifiée"
    stimulus["displayed_data"]["roe"].append(9.9)
    stimulus["ajout"] = True
    expected = {"question": "q", "displayed_data": {"roe": [0.2, 0.3]}}
    assert event.stimulus_snapshot == expected
    db.commit()
    assert _row(engine, event.id)["stimulus_snapshot"] == expected


def test_pg_same_turns_different_ordinals_are_distinct_events(db):
    first = cc.open_event_idempotent(db, **_open_kwargs(source_turn_refs=(TURN,), segmentation_ordinal=1))
    second = cc.open_event_idempotent(db, **_open_kwargs(source_turn_refs=(TURN,), segmentation_ordinal=2))
    assert first.id != second.id and first.event_dedup_key != second.event_dedup_key


def test_pg_same_turn_uuid_for_two_users_never_shares_an_event(db):
    mine = cc.open_event_idempotent(db, **_open_kwargs(source_turn_refs=(TURN,)))
    theirs = cc.open_event_idempotent(db, **_open_kwargs(user_id=OTHER_USER, source_turn_refs=(TURN,)))
    assert mine.id != theirs.id
    assert (mine.user_id, theirs.user_id) == (USER, OTHER_USER)


def test_pg_new_builder_version_is_a_new_identity(db):
    old = cc.open_event_idempotent(db, **_open_kwargs(source_turn_refs=(TURN,)))
    new = cc.open_event_idempotent(db, **_open_kwargs(source_turn_refs=(TURN,),
                                                      event_builder_version="cognitive-event-builder-2"))
    assert old.id != new.id and new.event_builder_version == "cognitive-event-builder-2"


# --- conversation et analysis_session ---------------------------------------

def test_pg_open_refuses_a_conversation_owned_by_another_user(engine, Sessions, db):
    _committed_event(Sessions, conversation_key="shared")
    with pytest.raises(ConversationOwnershipConflict):
        cc.open_event_idempotent(db, **_open_kwargs(user_id=OTHER_USER, conversation_key="shared"))
    db.commit()
    assert _conversations(engine) == [("shared", USER)]
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM cognitive_events WHERE user_id = :u"),
                            {"u": OTHER_USER}).scalar_one() == 0


def test_pg_open_with_an_owned_analysis_session(engine, db):
    event = cc.open_event_idempotent(db, **_open_kwargs(event_origin="decryptage", analysis_session_id=SESSION_ID))
    db.commit()
    assert _row(engine, event.id)["analysis_session_id"] == SESSION_ID


def test_pg_open_refuses_an_analysis_session_of_another_user(engine, db):
    """Ownership vérifié par le service, pas seulement par la FK (qui
    accepterait la session d'un autre utilisateur) ; rien n'est écrit, pas
    même l'enregistrement de la conversation."""
    with pytest.raises(AnalysisSessionOwnershipConflict):
        cc.open_event_idempotent(db, **_open_kwargs(user_id=OTHER_USER, analysis_session_id=SESSION_ID))
    assert db.execute(sa.text("SELECT count(*) FROM conversation_identities")).scalar_one() == 0
    db.commit()
    assert _count(engine, "cognitive_events") == 0 and _conversations(engine) == []


def test_pg_open_refuses_an_unknown_analysis_session(engine, db):
    with pytest.raises(AnalysisSessionNotFound):
        cc.open_event_idempotent(db, **_open_kwargs(analysis_session_id=uuid.uuid4()))
    db.commit()
    assert _count(engine, "cognitive_events") == 0 and _conversations(engine) == []


def test_pg_unknown_user_is_left_to_the_database_fk_without_poisoning_the_caller(engine, db):
    """SAVEPOINT : la transaction de l'appelant reste utilisable, et son
    écriture en attente (non flushée) n'est pas perdue par le retour au
    SAVEPOINT."""
    db.add(User(id="pending-user", level="debutant"))
    with pytest.raises(sa.exc.IntegrityError, match="conversation_identities_user_id_fkey"):
        cc.open_event_idempotent(db, **_open_kwargs(user_id="ghost"))
    event = cc.open_event_idempotent(db, **_open_kwargs())
    db.commit()
    assert _count(engine, "cognitive_events") == 1 and _row(engine, event.id) is not None
    assert _conversations(engine) == [(f"conv-{USER}", USER)]
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users WHERE id = 'pending-user'")).scalar_one() == 1


# --- open_event_idempotent : retry ------------------------------------------

def test_pg_retry_in_the_same_transaction_returns_the_same_event_without_writing(engine, db, clock):
    kwargs = _open_kwargs(source_turn_refs=(TURN, TURN_2), stimulus_snapshot=STIMULUS, task_kind=None)
    first = cc.open_event_idempotent(db, **kwargs)
    with _Statements(engine) as statements:
        again = cc.open_event_idempotent(db, **kwargs)
    assert again is first
    assert _writes(statements) == []
    assert first.started_at == first.updated_at == clock
    db.commit()
    assert _count(engine, "cognitive_events") == 1


def test_pg_retry_after_commit_returns_the_committed_event_untouched(engine, Sessions, db):
    kwargs = _open_kwargs(source_turn_refs=(TURN,), stimulus_snapshot=STIMULUS)
    event_id = _committed_event(Sessions, **kwargs)
    before = _row(engine, event_id)
    with _Statements(engine) as statements:
        again = cc.open_event_idempotent(db, **kwargs)
    assert again.id == event_id
    assert _writes(statements) == []
    db.commit()
    assert _row(engine, event_id) == before and _count(engine, "cognitive_events") == 1


@pytest.mark.parametrize("close", ["finalize_event", "abandon_event"])
def test_pg_retry_after_closing_returns_the_closed_event_without_mutation(engine, Sessions, db, close):
    """Un retry de création ne crée aucune nouvelle réalité, même après
    fermeture : l'événement fermé est retourné tel quel."""
    kwargs = _open_kwargs(source_turn_refs=(TURN,))
    event_id = _committed_event(Sessions, contributions=1, traces=["hint"], close=close, **kwargs)
    before, traces_before = _row(engine, event_id), _traces(engine, event_id)
    with _Statements(engine) as statements:
        again = cc.open_event_idempotent(db, **kwargs)
    assert again.id == event_id and again.status == before["status"] != "open"
    assert _writes(statements) == []
    db.commit()
    assert _row(engine, event_id) == before and _traces(engine, event_id) == traces_before
    assert _count(engine, "cognitive_events") == 1


def test_pg_retry_compares_json_like_jsonb(engine, Sessions, db):
    """1 et 1.0 (et un grand float relu en int depuis JSONB) sont le même
    stimulus ; un booléen n'est jamais un nombre."""
    stimulus = {"x": 1.0, "big": 1e16, "huge": 1e30, "tiny": 1.5e-300, "flag": True, "ratio": 0.1}
    kwargs = _open_kwargs(source_turn_refs=(TURN,), stimulus_snapshot=stimulus)
    event_id = _committed_event(Sessions, **kwargs)
    assert cc.open_event_idempotent(db, **kwargs).id == event_id  # relu depuis JSONB
    assert cc.open_event_idempotent(db, **{**kwargs, "stimulus_snapshot": {**stimulus, "x": 1, "big": 10 ** 16,
                                                                           "huge": 10 ** 30}}).id == event_id
    with pytest.raises(EventDedupCollision, match="stimulus_snapshot"):
        cc.open_event_idempotent(db, **{**kwargs, "stimulus_snapshot": {**stimulus, "flag": 1}})


# --- open_event_idempotent : collision --------------------------------------

@pytest.mark.parametrize("field, value", [
    ("stimulus_snapshot", {"question": "Autre question ?"}),
    ("event_origin", "coach"),
    ("task_kind", "interpret_metric"),
    ("task_kind", None),
    ("analysis_session_id", SESSION_ID),
    ("admission_version", "cognitive-event-admission-2"),
])
def test_pg_same_identity_with_diverging_structural_field_is_a_collision(engine, Sessions, db, field, value):
    kwargs = _open_kwargs(source_turn_refs=(TURN,), stimulus_snapshot=STIMULUS)
    event_id = _committed_event(Sessions, contributions=1, **kwargs)
    before = _row(engine, event_id)
    with pytest.raises(EventDedupCollision, match=field) as error:
        cc.open_event_idempotent(db, **{**kwargs, field: value})
    # Message : noms de champs et id technique, jamais le contenu.
    assert "Que mesure" not in str(error.value) and "Autre question" not in str(error.value)
    db.commit()
    assert _row(engine, event_id) == before and _count(engine, "cognitive_events") == 1


def test_pg_existing_event_of_another_schema_version_is_a_collision(engine, Sessions, db):
    kwargs = _open_kwargs(source_turn_refs=(TURN,))
    event_id = _committed_event(Sessions, **kwargs)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE cognitive_events SET event_schema_version = 'cognitive-event-v3'"))
    before = _row(engine, event_id)
    with pytest.raises(EventDedupCollision, match="event_schema_version"):
        cc.open_event_idempotent(db, **kwargs)
    db.commit()
    assert _row(engine, event_id) == before


@pytest.mark.parametrize("field, value", [("user_id", OTHER_USER), ("conversation_key", "conv-autre")])
def test_pg_forced_key_collision_on_namespace_fields_is_detected(engine, Sessions, db, monkeypatch, field, value):
    """Même si deux identités produisaient la même clé (collision forcée),
    user_id / conversation_key divergents ne retournent jamais l'événement
    d'un autre namespace."""
    monkeypatch.setattr(cc, "_event_dedup_key", lambda **_: "f" * 64)
    event_id = _committed_event(Sessions, conversation_key="conv-a")
    before = _row(engine, event_id)
    with pytest.raises(EventDedupCollision, match=field):
        # (un autre utilisateur ne peut pas réutiliser conv-a : registre)
        overrides = {"conversation_key": "conv-b"} if field == "user_id" else {}
        cc.open_event_idempotent(db, **_open_kwargs(**{"conversation_key": "conv-a", **overrides, field: value}))
    db.commit()
    assert _row(engine, event_id) == before and _count(engine, "cognitive_events") == 1


# --- transactions : l'appelant possède commit/rollback ----------------------

def test_pg_open_does_not_commit_and_caller_rollback_discards_everything(engine, db):
    """Écriture métier en attente + registre de conversation + événement +
    contribution : une seule transaction, annulée en bloc par l'appelant."""
    db.add(User(id="pending-user", level="debutant"))
    db.flush()
    event = cc.open_event_idempotent(db, **_open_kwargs(user_id="pending-user"))
    cc.append_user_contribution(db, event_id=event.id, **_contribution())
    event_id = event.id
    assert db.execute(sa.text("SELECT count(*) FROM cognitive_events WHERE id = :id"),
                      {"id": event_id}).scalar_one() == 1
    assert _row(engine, event_id) is None and _conversations(engine) == []
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users WHERE id = 'pending-user'")).scalar_one() == 0

    db.rollback()
    assert _row(engine, event_id) is None
    assert _count(engine, "cognitive_events") == 0 and _conversations(engine) == []
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users WHERE id = 'pending-user'")).scalar_one() == 0


def test_pg_caller_rollback_discards_mutations_of_existing_event(engine, Sessions, db):
    event_id = _committed_event(Sessions, contributions=1, traces=["hint"])
    before, traces_before = _row(engine, event_id), _traces(engine, event_id)

    cc.append_user_contribution(db, event_id=event_id, **_contribution())
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


# --- append_user_contribution -----------------------------------------------

def test_pg_contributions_get_server_side_phase_time_and_support_refs(engine, db, clock):
    event = cc.open_event_idempotent(db, **_open_kwargs(stimulus_snapshot=STIMULUS))
    first = _contribution(text_excerpt="rentabilité")
    returned = cc.append_user_contribution(db, event_id=event.id, **first)
    assert returned is event
    entry = {
        "contribution_id": str(first["contribution_id"]),
        "source_turn_ref": str(first["source_turn_ref"]),
        "phase": 1,
        "surface": "web-chat",
        "session_ref": "conv-u",
        "occurred_at": (clock + timedelta(seconds=1)).isoformat(),
        "text": "rentabilité",
        "support_refs_before": [],
    }
    assert event.user_work_snapshot == [entry]
    assert event.updated_at == clock + timedelta(seconds=1)
    assert datetime.fromisoformat(entry["occurred_at"]) == event.updated_at
    assert entry["occurred_at"].endswith("+00:00")

    second = _contribution(text_excerpt="  bénéfice / capitaux propres\n", surface="decryptage",
                           session_ref="session-42")
    cc.append_user_contribution(db, event_id=event.id, **second)
    assert [c["phase"] for c in event.user_work_snapshot] == [1, 2]
    assert event.user_work_snapshot[1]["text"] == "  bénéfice / capitaux propres\n"  # aucune normalisation
    assert (event.user_work_snapshot[1]["surface"], event.user_work_snapshot[1]["session_ref"]) == (
        "decryptage", "session-42")
    assert event.updated_at == clock + timedelta(seconds=2)
    assert event.stimulus_snapshot == STIMULUS and event.started_at == clock and event.status == "open"
    db.commit()

    stored = _row(engine, event.id)
    assert stored["user_work_snapshot"] == event.user_work_snapshot
    assert stored["updated_at"] == clock + timedelta(seconds=2)
    assert stored["event_dedup_key"] == event.event_dedup_key  # jamais recalculée
    assert set(stored["user_work_snapshot"][0]) == {"contribution_id", "source_turn_ref", "phase", "surface",
                                                    "session_ref", "occurred_at", "text", "support_refs_before"}


def test_pg_contributions_across_transactions(engine, Sessions):
    event_id = _committed_event(Sessions, contributions=1)
    for _ in range(2):
        with Sessions() as session:
            cc.append_user_contribution(session, event_id=event_id, **_contribution())
            session.commit()
    assert [c["phase"] for c in _row(engine, event_id)["user_work_snapshot"]] == [1, 2, 3]


def test_pg_support_refs_before_are_captured_under_the_event_lock(engine, db):
    event = cc.open_event_idempotent(db, **_open_kwargs())
    cc.append_user_contribution(db, event_id=event.id, **_contribution())
    s1 = cc.add_support_trace(db, event_id=event.id, support_kind="hint", support_payload={"t": "indice"})
    cc.append_user_contribution(db, event_id=event.id, **_contribution())
    s2 = cc.add_support_trace(db, event_id=event.id, support_kind="explanation", support_payload={"t": "expl"})
    cc.append_user_contribution(db, event_id=event.id, **_contribution())
    assert [(s1.sequence_no, s2.sequence_no)] == [(1, 2)]
    expected = [[], [str(s1.id)], [str(s1.id), str(s2.id)]]
    assert [c["support_refs_before"] for c in event.user_work_snapshot] == expected
    db.commit()
    assert [c["support_refs_before"] for c in _row(engine, event.id)["user_work_snapshot"]] == expected


def test_pg_support_refs_before_follow_sequence_no_not_insertion_order(engine, Sessions, db):
    event_id = _committed_event(Sessions)
    ids = {}
    with engine.begin() as conn:
        for seq in (2, 1):
            ids[seq] = uuid.uuid4()
            conn.execute(sa.text(
                "INSERT INTO support_traces (id, cognitive_event_id, sequence_no, support_kind, support_payload, "
                "created_at) VALUES (:id, :e, :s, 'hint', CAST('{}' AS JSONB), now())"
            ), {"id": ids[seq], "e": event_id, "s": seq})
    event = cc.append_user_contribution(db, event_id=event_id, **_contribution())
    assert event.user_work_snapshot[0]["support_refs_before"] == [str(ids[1]), str(ids[2])]


def test_pg_contribution_retry_is_a_no_op(engine, db, clock):
    event = cc.open_event_idempotent(db, **_open_kwargs())
    c1 = _contribution()
    cc.append_user_contribution(db, event_id=event.id, **c1)
    cc.add_support_trace(db, event_id=event.id, support_kind="hint", support_payload={})
    db.commit()
    before = _row(engine, event.id)
    with _Statements(engine) as statements:
        again = cc.append_user_contribution(db, event_id=event.id, **dict(c1))
    assert again is event
    # Seul ordre émis : le verrou ; aucune écriture.
    assert len(statements) == 1 and statements[0].endswith("FOR UPDATE")
    db.commit()
    stored = _row(engine, event.id)
    assert stored == before
    # Ni nouvelle phase, ni occurred_at, ni support_refs_before recalculés
    # (S1 a été ajoutée après C1).
    assert len(stored["user_work_snapshot"]) == 1
    assert stored["user_work_snapshot"][0]["phase"] == 1
    assert stored["user_work_snapshot"][0]["support_refs_before"] == []
    assert stored["user_work_snapshot"][0]["occurred_at"] == (clock + timedelta(seconds=1)).isoformat()
    assert stored["updated_at"] == clock + timedelta(seconds=2)  # celui de la trace, pas du retry


@pytest.mark.parametrize("field, value", [
    ("text_excerpt", "autre réponse"),
    ("text_excerpt", "rentabilité des capitaux propres "),
    ("source_turn_ref", TURN),
    ("surface", "decryptage"),
    ("session_ref", "conv-autre"),
])
def test_pg_contribution_collision(engine, Sessions, db, field, value):
    event_id = _committed_event(Sessions)
    c1 = _contribution()
    with Sessions() as session:
        cc.append_user_contribution(session, event_id=event_id, **c1)
        session.commit()
    before = _row(engine, event_id)
    with pytest.raises(ContributionIdentityCollision, match="text" if field == "text_excerpt" else field) as error:
        cc.append_user_contribution(db, event_id=event_id, **{**c1, field: value})
    assert "rentabilité" not in str(error.value) and "autre réponse" not in str(error.value)
    db.commit()
    assert _row(engine, event_id) == before


@pytest.mark.parametrize("close", ["finalize_event", "abandon_event"])
def test_pg_contribution_on_a_closed_event(engine, Sessions, db, close):
    """Nouvelle contribution refusée ; retry exact = no-op ; retry divergent
    = collision (pas un « closed » générique)."""
    event_id = _committed_event(Sessions)
    c1 = _contribution()
    with Sessions() as session:
        cc.append_user_contribution(session, event_id=event_id, **c1)
        getattr(cc, close)(session, event_id=event_id)
        session.commit()
    before = _row(engine, event_id)

    with pytest.raises(CognitiveEventClosed):
        cc.append_user_contribution(db, event_id=event_id, **_contribution())
    assert cc.append_user_contribution(db, event_id=event_id, **c1).id == event_id
    with pytest.raises(ContributionIdentityCollision):
        cc.append_user_contribution(db, event_id=event_id, **{**c1, "text_excerpt": "corrigée"})
    db.commit()
    assert _row(engine, event_id) == before


def test_pg_contribution_unknown_event(db):
    with pytest.raises(CognitiveEventNotFound):
        cc.append_user_contribution(db, event_id=uuid.uuid4(), **_contribution())


# --- lignes legacy pré-R1-B --------------------------------------------------

def test_pg_legacy_event_is_never_enriched_with_v2_contributions(engine, db):
    event_id = _legacy_event(engine)
    before = _row(engine, event_id)
    assert before["event_schema_version"] is None and before["event_dedup_key"] is None
    with pytest.raises(UnsupportedCognitiveEventSchema):
        cc.append_user_contribution(db, event_id=event_id, **_contribution())
    db.commit()
    assert _row(engine, event_id) == before


def test_pg_legacy_event_stays_readable_and_keeps_its_lifecycle(engine, db):
    """Lecture, aides et fermeture inchangées pour un événement legacy
    open ; aucune conversion ni backfill."""
    event_id = _legacy_event(engine)
    event = cc.get_event(db, event_id=event_id)
    assert event.user_work_snapshot == [{"text": "réponse"}] and event.conversation_key is None
    trace = cc.add_support_trace(db, event_id=event_id, support_kind="hint", support_payload={})
    assert cc.get_support_traces(db, event_id=event_id) == [trace]
    cc.finalize_event(db, event_id=event_id)
    db.commit()
    stored = _row(engine, event_id)
    assert stored["status"] == "finalized" and stored["user_work_snapshot"] == [{"text": "réponse"}]
    assert (stored["event_dedup_key"], stored["event_builder_version"], stored["admission_version"],
            stored["event_schema_version"]) == (None, None, None, None)


# --- add_support_trace ------------------------------------------------------

def test_pg_add_support_trace_allocates_sequence(engine, db, clock):
    event = cc.open_event_idempotent(db, **_open_kwargs())
    other = cc.open_event_idempotent(db, **_open_kwargs())
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
    event = cc.open_event_idempotent(db, **_open_kwargs())
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

    with _Statements(engine) as statements:
        traces = cc.get_support_traces(db, event_id=event_id)
    assert [t.sequence_no for t in traces] == [1, 2, 3, 4]
    assert [t.support_payload for t in traces] == [{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}]
    assert all(isinstance(t, SupportTrace) for t in traces)
    assert statements[-1].endswith("ORDER BY support_traces.sequence_no ASC")
    assert cc.add_support_trace(db, event_id=event_id, support_kind="hint", support_payload={}).sequence_no == 5


def test_pg_get_support_traces_empty(db):
    event = cc.open_event_idempotent(db, **_open_kwargs())
    assert cc.get_support_traces(db, event_id=event.id) == []


# --- lifecycle --------------------------------------------------------------

@pytest.mark.parametrize("close, status", [("finalize_event", "finalized"), ("abandon_event", "abandoned")])
def test_pg_open_event_can_be_closed(engine, db, clock, close, status):
    event = cc.open_event_idempotent(db, **_open_kwargs())
    cc.append_user_contribution(db, event_id=event.id, **_contribution())
    closed = getattr(cc, close)(db, event_id=event.id)
    assert closed is event
    assert event.status == status
    assert event.closed_at == event.updated_at == clock + timedelta(seconds=2)
    assert event.started_at == clock
    db.commit()
    stored = _row(engine, event.id)
    assert stored["status"] == status
    _assert_utc(stored["closed_at"])
    assert stored["closed_at"] == stored["updated_at"] == clock + timedelta(seconds=2)
    assert [c["phase"] for c in stored["user_work_snapshot"]] == [1]


@pytest.mark.parametrize("first", ["finalize_event", "abandon_event"])
@pytest.mark.parametrize("attempt", sorted(MUTATIONS))
def test_pg_closed_event_is_immutable(engine, Sessions, db, first, attempt):
    """finalized et abandoned sont terminaux : aucune nouvelle production,
    aucune aide, aucune seconde transition (pas de no-op) ; l'état persisté
    — snapshots, timestamps, traces — reste strictement inchangé."""
    event_id = _committed_event(Sessions, contributions=1, traces=["hint"], close=first,
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
    with _Statements(engine) as statements:
        MUTATIONS[name](db, event_id)
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
        cc.append_user_contribution(db, event_id=event_id, **_contribution())
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


def _run_blocked(engine, holder, waiter, action, *, release="commit"):
    """`holder` détient déjà un verrou (transaction ouverte). `action(waiter)`
    est lancée dans un thread ; une fois qu'elle est réellement bloquée par
    `holder`, holder commite (ou annule). Retourne (résultat, exception) de
    l'action, dont la transaction est commitée si elle a réussi."""
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
        getattr(holder, release)()
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


def test_pg_concurrent_contributions_are_serialized(engine, Sessions):
    """Sans verrou, la seconde transaction réécrirait user_work_snapshot à
    partir d'un état obsolète, perdrait la première contribution et
    allouerait la même phase."""
    event_id = _committed_event(Sessions, contributions=1)
    with Sessions() as first, Sessions() as second:
        seen = cc.get_event(second, event_id=event_id)  # référence conservée
        assert len(seen.user_work_snapshot) == 1
        cc.append_user_contribution(first, event_id=event_id, **_contribution(text_excerpt="premier"))
        _, error = _run_blocked(engine, first, second, lambda s: cc.append_user_contribution(
            s, event_id=event_id, **_contribution(text_excerpt="second")))
    assert error is None
    work = _row(engine, event_id)["user_work_snapshot"]
    assert [(c["phase"], c["text"]) for c in work] == [(1, "réponse 0"), (2, "premier"), (3, "second")]


def test_pg_concurrent_retry_of_the_same_contribution_is_recorded_once(engine, Sessions):
    event_id = _committed_event(Sessions)
    c1 = _contribution()
    with Sessions() as first, Sessions() as second:
        cc.append_user_contribution(first, event_id=event_id, **c1)
        result, error = _run_blocked(engine, first, second,
                                     lambda s: cc.append_user_contribution(s, event_id=event_id, **c1).id)
    assert error is None and result == event_id
    work = _row(engine, event_id)["user_work_snapshot"]
    assert [(c["contribution_id"], c["phase"]) for c in work] == [(str(c1["contribution_id"]), 1)]


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_pg_mutation_waiting_on_a_closing_transaction_is_refused(engine, Sessions, name):
    """Une production, une aide ou une transition qui attend le verrou d'une
    finalisation en cours voit l'événement fermé une fois le verrou obtenu."""
    event_id = _committed_event(Sessions, contributions=1, traces=["hint"])
    with Sessions() as closer, Sessions() as other:
        seen = cc.get_event(other, event_id=event_id)  # référence conservée
        assert seen.status == "open"
        cc.finalize_event(closer, event_id=event_id)
        _, error = _run_blocked(engine, closer, other, lambda s: MUTATIONS[name](s, event_id))
        assert isinstance(error, CognitiveEventClosed)
        assert seen.status == "finalized"
    stored = _row(engine, event_id)
    assert stored["status"] == "finalized"
    assert [c["text"] for c in stored["user_work_snapshot"]] == ["réponse 0"]
    assert stored["closed_at"] == stored["updated_at"]
    assert _traces(engine, event_id) == [(1, "hint", {"k": "hint"})]


# --- concurrence d'ouverture (SAVEPOINT + UNIQUE) ---------------------------

def _registered(Sessions, user_id=USER):
    """Conversation déjà commitée : la course porte alors sur l'événement
    seul (et non sur le registre)."""
    from core.interaction_identity import register_or_resolve_conversation
    with Sessions() as session:
        register_or_resolve_conversation(session, user_id=user_id, conversation_key=f"conv-{user_id}")
        session.commit()


def _open_after_losing_the_race(engine, kwargs):
    """Action du perdant : une écriture métier en attente (non flushée),
    puis l'ouverture ; prouve que sa transaction reste utilisable (une
    écriture de plus) et que l'écriture en attente n'est pas perdue par le
    retour au SAVEPOINT."""
    def action(session):
        session.add(User(id="loser-pending", level="debutant"))
        with _Statements(engine) as statements:
            event = cc.open_event_idempotent(session, **kwargs)
        assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in statements), statements
        cc.add_support_trace(session, event_id=event.id, support_kind="hint", support_payload={"by": "loser"})
        return event.id
    return action


def test_pg_concurrent_open_of_the_same_identity_resolves_to_one_event(engine, Sessions):
    """Course réelle : le perdant est bloqué par l'INSERT non commité du
    gagnant sur l'index UNIQUE, reçoit la violation, revient à son SAVEPOINT,
    relit l'événement gagnant et le retourne ; sa transaction reste saine."""
    _registered(Sessions)
    kwargs = _open_kwargs(source_turn_refs=(TURN,), stimulus_snapshot=STIMULUS)
    with Sessions() as winner, Sessions() as loser:
        winner_id = cc.open_event_idempotent(winner, **kwargs).id
        loser_id, error = _run_blocked(engine, winner, loser, _open_after_losing_the_race(engine, kwargs))
    assert error is None
    assert loser_id == winner_id
    assert _count(engine, "cognitive_events") == 1
    assert _traces(engine, winner_id) == [(1, "hint", {"by": "loser"})]
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users WHERE id = 'loser-pending'")).scalar_one() == 1


def test_pg_concurrent_open_with_diverging_content_fails_closed(engine, Sessions):
    _registered(Sessions)
    kwargs = _open_kwargs(source_turn_refs=(TURN,), stimulus_snapshot=STIMULUS)
    with Sessions() as winner, Sessions() as loser:
        winner_id = cc.open_event_idempotent(winner, **kwargs).id

        def action(session):
            try:
                cc.open_event_idempotent(session, **{**kwargs, "event_origin": "coach"})
            except EventDedupCollision:
                # Transaction toujours utilisable après la collision.
                return session.execute(sa.text("SELECT 1")).scalar_one()
            raise AssertionError("collision attendue")

        result, error = _run_blocked(engine, winner, loser, action)
    assert error is None and result == 1
    assert _count(engine, "cognitive_events") == 1
    assert _row(engine, winner_id)["event_origin"] == "education"


def test_pg_concurrent_open_after_the_first_transaction_rolls_back(engine, Sessions):
    """Si le premier annule, le second crée l'événement (le premier n'a
    jamais existé)."""
    _registered(Sessions)
    kwargs = _open_kwargs(source_turn_refs=(TURN,))
    with Sessions() as first, Sessions() as second:
        first_id = cc.open_event_idempotent(first, **kwargs).id
        second_id, error = _run_blocked(engine, first, second,
                                        lambda s: cc.open_event_idempotent(s, **kwargs).id, release="rollback")
    assert error is None and second_id != first_id
    assert _count(engine, "cognitive_events") == 1 and _row(engine, second_id)["status"] == "open"
