"""Contrôles statiques et de dormance de R1 PR 9-A (V2.2-02 / V2.2-03 /
V2.2-04), sans base de données.

Ils complètent tests/test_authz_f2_boundary.py (exception nominative du
pont) et tests/test_authz_f2_dormancy.py (matrice d'imports) :

1. module privilégié UNIQUE : seul core/authz_f2/_decryptage_orm_bridge.py
   construit une Session ; aucun routeur ni commande ne le référence ;
2. configuration normative exacte de la Session, refus à l'exécution de
   commit / rollback / begin non imbriqué / close / reset / invalidate /
   connection ;
3. inventaire FERMÉ des opérations (types exacts -> points d'entrée privés),
   opérations sans user_id, Session, connexion ni fonction ;
4. graphe d'appels FERMÉ depuis les points d'entrée, calculé sur les
   services réels : liste exacte des fonctions atteintes ; aucune ne
   commite, n'annule, n'ouvre de transaction ni de connexion, n'écrit de
   SQL textuel ; begin_nested() seulement dans les trois SAVEPOINT
   historiques ; jamais _lock_delivery ni lock_delivery_for_ack (UUID non
   filtré) ;
5. imports transitifs inventoriés, aucun réseau, aucune connexion à
   l'import (même avec DATABASE_URL) ;
6. registre de production inchangé (quatorze commandes), runner public
   inchangé, api:app ne charge pas le pont, aucune migration.
"""
import ast
import dataclasses
import inspect
import json
import subprocess
import sys
import uuid

import pytest
import sqlalchemy as sa

from core import assistant_delivery as ad
from core.authz_f2 import _adapters
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import commands as c
from core.authz_f2.runner import AuthorizedTransactionRunner, UnknownCommand
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

BRIDGE = REPO_ROOT / "core" / "authz_f2" / "_decryptage_orm_bridge.py"
SERVICES = {
    "core.assistant_delivery": REPO_ROOT / "core" / "assistant_delivery.py",
    "core.decryptage_cognitive_runtime": REPO_ROOT / "core" / "decryptage_cognitive_runtime.py",
    "core.decryptage_progress": REPO_ROOT / "core" / "decryptage_progress.py",
    "core.cognitive_capture": REPO_ROOT / "core" / "cognitive_capture.py",
    "core.interaction_identity": REPO_ROOT / "core" / "interaction_identity.py",
}
BRIDGE_ALIASES = {"ad": "core.assistant_delivery", "dcr": "core.decryptage_cognitive_runtime",
                  "dp": "core.decryptage_progress"}

# Graphe d'appels FERMÉ attendu (fonctions des services atteintes depuis
# les deux points d'entrée du pont). Toute nouvelle dépendance doit être
# ajoutée ici, explicitement.
REACHABLE = {
    "_finalize_turn": {
        "core.assistant_delivery": {
            "claim_delivery", "visible_content_fingerprint", "bind_locked_analysis_session", "_validate_identity",
            "_require_identifier", "_require_surface", "_require_uuid", "_require_ordinal", "_require_fingerprint",
            "_require_response_payload", "_json_dict", "_json_copy", "_require_schema_version",
            "_require_existing_user", "_resolve_conversation", "_delivery_by_identity", "_same_request_or_collision",
            "_is_turn_unique_violation", "_utcnow", "_require_text", "_require_owned", "_bind_locked"},
        "core.interaction_identity": {"register_or_resolve_conversation", "_require_identifier", "_resolve",
                                      "_is_primary_key_violation", "_utcnow"},
        "core.decryptage_cognitive_runtime": {
            "runtime_private_metadata", "capture_user_turn", "answered_step", "record_turn_progress",
            "_runtime_version", "_lock_link", "_lock_active_analysis_session", "_conversation_anchor_event_id",
            "_anchor_event_id", "_lock_turn_events", "_session_ticker", "_append_contribution",
            "_conversation_support_refs", "_utcnow", "_event_step"},
        "core.cognitive_capture": {"append_user_contribution", "_require_uuid", "_require_exact_uuid",
                                   "_require_identifier", "_require_text", "_lock_event", "_utcnow"},
        "core.decryptage_progress": {"apply_construction_these_progress", "find_active_analysis_session",
                                     "_progress_action"},
    },
    "_acknowledge": {
        "core.assistant_delivery": {"lock_owned_delivery", "acknowledge_locked_delivery", "_require_uuid",
                                    "_require_identifier", "_require_schema_version", "_require_owned",
                                    "_mark_delivered", "_utcnow"},
        "core.decryptage_cognitive_runtime": {
            "capture_delivered_response", "_runtime_version", "_lock_link", "_capture_response_v1",
            "_capture_response_v2", "_step_marker", "_event_session_id", "_lock_analysis_session",
            "_lock_open_events", "_record_response", "_event_step", "_open_event", "_close_event",
            "_support_trace", "_visible_stimulus", "_utcnow"},
        "core.cognitive_capture": {
            "open_event_idempotent", "finalize_event", "abandon_event", "add_support_trace", "_close",
            "_lock_open_event", "_lock_event", "_require_identifier", "_require_text", "_require_uuid",
            "_require_exact_uuid", "_require_source_turn_refs", "_require_ordinal", "_json_dict", "_json_copy",
            "_event_dedup_key", "_check_analysis_session_owner", "_event_by_dedup_key",
            "_same_event_or_collision", "_json_equal", "_json_number", "_is_dedup_violation", "_utcnow"},
        "core.interaction_identity": {"register_or_resolve_conversation", "_require_identifier", "_resolve",
                                      "_is_primary_key_violation", "_utcnow"},
    },
}
SAVEPOINT_OWNERS = {("core.assistant_delivery", "claim_delivery"),
                    ("core.interaction_identity", "register_or_resolve_conversation"),
                    ("core.cognitive_capture", "open_event_idempotent")}
FORBIDDEN_ATTRIBUTE_CALLS = {"commit", "rollback", "close", "begin", "connection", "get_bind", "invalidate",
                             "reset", "exec_driver_sql", "raw_connection", "execution_options_engine", "connect",
                             "detach", "get_transaction", "expunge_all", "text"}
FORBIDDEN_NAMES = {"SessionLocal", "get_db", "sessionmaker", "create_engine", "Session", "engine",
                   "anthropic", "requests", "httpx", "fetch_financial_data", "normalize_ticker"}


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _functions(path) -> dict:
    return {n.name: n for n in _tree(path).body if isinstance(n, ast.FunctionDef)}


def _imported_names(path) -> dict:
    """Nom local -> (module, fonction) pour `from core.x import f` et
    alias de module pour `from core import x [as y]`."""
    names = {}
    for node in _tree(path).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module == "core":
                    names[local] = (f"core.{alias.name}", None)
                elif node.module in SERVICES:
                    names[local] = (node.module, alias.name)
    return names


def _callees(module, fn, imported):
    out = set()
    for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in imported and imported[func.id][1] is not None:
                out.add(imported[func.id])
            else:
                out.add((module, func.id))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            target = imported.get(func.value.id)
            if target is not None and target[1] is None:
                out.add((target[0], func.attr))
    return out


def _reachable(entry: str) -> dict:
    functions = {m: _functions(p) for m, p in SERVICES.items()}
    imported = {m: _imported_names(p) for m, p in SERVICES.items()}
    bridge_fn = _functions(BRIDGE)[entry]
    pending = {(BRIDGE_ALIASES[f.value.id], f.attr) for f in (n.func for n in ast.walk(bridge_fn)
                                                              if isinstance(n, ast.Call))
               if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in BRIDGE_ALIASES}
    seen = set()
    while pending:
        module, name = pending.pop()
        if (module, name) in seen or name not in functions.get(module, {}):
            continue
        seen.add((module, name))
        pending |= _callees(module, functions[module][name], imported[module])
    out = {}
    for module, name in seen:
        out.setdefault(module, set()).add(name)
    return out


# --------------------------------------------------------------------------
# 1-2. Module privilégié unique, configuration et garde de la Session
# --------------------------------------------------------------------------

def test_the_bridge_is_the_only_privileged_module():
    # Aucun module applicatif n'importe le pont (les docstrings de la
    # frontière peuvent le nommer ; aucune instruction ne le charge).
    for root in (REPO_ROOT / "core", REPO_ROOT / "scripts", REPO_ROOT / "ci"):
        for path in root.rglob("*.py"):
            for node in ast.walk(_tree(path)):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [f"{node.module}.{a.name}" for a in node.names]
                elif isinstance(node, ast.Call) and ast.unparse(node.func) in ("importlib.import_module",
                                                                                "__import__"):
                    names = [ast.unparse(a) for a in node.args]
                assert not any("_decryptage_orm_bridge" in n for n in names), path
    for path in (REPO_ROOT / "api.py", REPO_ROOT / "web-v2" / "index.html"):
        assert "_decryptage_orm_bridge" not in path.read_text(encoding="utf-8")
    # Les seuls constructeurs de Session de la frontière : le pont.
    for path in sorted((REPO_ROOT / "core" / "authz_f2").glob("*.py")) + sorted(
            (REPO_ROOT / "core" / "authz_f2_http").glob("*.py")):
        names = {n.id for n in ast.walk(_tree(path)) if isinstance(n, ast.Name)}
        assert ("Session" in names) == (path == BRIDGE), path.name


def test_session_configuration_is_exactly_the_normative_one():
    (cls,) = [n for n in _tree(BRIDGE).body if isinstance(n, ast.ClassDef) and n.name == "_BridgeSession"]
    assert [ast.unparse(b) for b in cls.bases] == ["Session"]
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    assert [ast.unparse(s) for s in init.body] == [
        "super().__init__(bind=conn, autoflush=False, join_transaction_mode='rollback_only')"]
    sessions = [n for n in ast.walk(_tree(BRIDGE)) if isinstance(n, ast.Call) and ast.unparse(n.func) == "_BridgeSession"]
    assert [ast.unparse(n) for n in sessions] == ["_BridgeSession(conn)"]  # une Session par opération


@pytest.mark.parametrize("method", ["commit", "rollback", "close", "reset", "invalidate", "connection", "begin"])
def test_bridge_session_refuses_to_drive_the_transaction(method):
    engine = sa.create_engine("postgresql+psycopg2://nobody@127.0.0.1:1/oryx_never_test")
    db = bridge._BridgeSession(engine)
    with pytest.raises(bridge.TransactionOwnershipViolation) as refused:
        getattr(db, method)()
    assert refused.value.alert == "decryptage_bridge_transaction_violation"
    assert engine.pool.checkedout() == 0  # rien n'a été ouvert
    db._release()


def test_errors_of_the_bridge_are_closed_runner_alerts():
    for exc, alert in ((bridge.TransactionOwnershipViolation, "decryptage_bridge_transaction_violation"),
                       (bridge.TransactionLost, "decryptage_bridge_transaction_lost"),
                       (bridge.ProvenanceIncoherent, "decryptage_bridge_provenance_incoherent")):
        assert issubclass(exc, _adapters.AdapterInvariantBroken) and exc.alert == alert


# --------------------------------------------------------------------------
# 3. Inventaire fermé
# --------------------------------------------------------------------------

def test_closed_inventory_of_exact_operation_types():
    assert dict(bridge.OPERATIONS) == {bridge.TurnFinalization: bridge._finalize_turn,
                                       bridge.DeliveryAcknowledgement: bridge._acknowledge}
    for operation, entry in bridge.OPERATIONS.items():
        assert entry.__module__ == bridge.__name__ and entry.__name__.startswith("_")
        assert dataclasses.is_dataclass(operation) and operation.__dataclass_params__.frozen
        assert not issubclass(operation, c.Command)  # jamais une commande du registre
        fields = {f.name for f in dataclasses.fields(operation)}
        assert not fields & {"user_id", "owner", "session", "db", "conn", "connection", "engine", "callback", "sql"}
    assert list(inspect.signature(bridge._execute).parameters) == ["conn", "ctx", "operation"]
    public = {n.name for n in _tree(BRIDGE).body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == set()


@pytest.mark.parametrize("value", [print, lambda: None, object(), b"x", ["x"]])
def test_operations_carry_only_validated_data(value):
    base = dict(conversation_key="conv", client_turn_id=uuid.uuid4(), request_fingerprint="a" * 64, ticker="MC.PA",
                user_text="", method_id="construction_these", step_marker="business",
                response_payload=json.dumps({"analysis": "x"}), financial_snapshot=None)
    bridge.TurnFinalization(**base)
    for field in base:
        with pytest.raises((ValueError, TypeError)):
            bridge.TurnFinalization(**{**base, field: value})
    with pytest.raises(ValueError):
        bridge.DeliveryAcknowledgement(str(uuid.uuid4()), "conv")
    with pytest.raises(ValueError):
        bridge.TurnFinalization(**{**base, "step_marker": "inconnu"})
    with pytest.raises(ValueError):
        bridge.TurnFinalization(**{**base, "response_payload": json.dumps({"other": "x"})})


# --------------------------------------------------------------------------
# 4. Graphe d'appels fermé
# --------------------------------------------------------------------------

def test_entry_points_call_the_services_in_the_historical_order():
    finalize = ast.unparse(_functions(BRIDGE)["_finalize_turn"])
    order = ["ad.claim_delivery(", "dcr.capture_user_turn(", "dcr.answered_step(",
             "dp.apply_construction_these_progress(", "ad.bind_locked_analysis_session(",
             "dcr.record_turn_progress(", "db.flush()", "return TurnFinalized("]
    assert [finalize.index(m) for m in order] == sorted(finalize.index(m) for m in order)
    acknowledge = ast.unparse(_functions(BRIDGE)["_acknowledge"])
    order = ["ad.lock_owned_delivery(", "_require_owned_provenance(", "dcr.capture_delivered_response(",
             "ad.acknowledge_locked_delivery(", "db.flush()", "return DeliveryAcknowledged("]
    assert [acknowledge.index(m) for m in order] == sorted(acknowledge.index(m) for m in order)
    execute = ast.unparse(_functions(BRIDGE)["_execute"])
    order = ["OPERATIONS.get(type(operation))", "conn.get_transaction()", "_require_live(conn, outer)",
             "_BridgeSession(conn)", "entry(session, ctx.principal.user_id, operation)", "_require_live(conn, outer)",
             "session._release()", "return result"]
    positions, start = [], 0
    for marker in order:
        start = execute.index(marker, start)
        positions.append(start)
    assert positions == sorted(positions)


@pytest.mark.parametrize("entry", sorted(REACHABLE))
def test_closed_call_graph_of_each_entry_point(entry):
    reachable = _reachable(entry)
    assert reachable == REACHABLE[entry]
    # Jamais la primitive historique non filtrée ni les routes legacy.
    assert not reachable.get("core.assistant_delivery", set()) & {
        "_lock_delivery", "lock_delivery_for_ack", "acknowledge_delivery", "bind_analysis_session",
        "preflight_delivery"}


@pytest.mark.parametrize("entry", sorted(REACHABLE))
def test_reachable_services_never_own_the_transaction(entry):
    for module, names in _reachable(entry).items():
        functions = _functions(SERVICES[module])
        for name in names:
            fn = functions[name]
            attrs = {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            assert not attrs & FORBIDDEN_ATTRIBUTE_CALLS, (module, name, attrs & FORBIDDEN_ATTRIBUTE_CALLS)
            if "begin_nested" in attrs:
                assert (module, name) in SAVEPOINT_OWNERS, (module, name)
            called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert "text" not in called, (module, name)  # aucun SQL textuel
            ids = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            assert not ids & FORBIDDEN_NAMES, (module, name, ids & FORBIDDEN_NAMES)
    for module, path in SERVICES.items():
        imports = {n.module for n in ast.walk(_tree(path)) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(_tree(path)) if isinstance(n, ast.Import) for a in n.names}
        assert "core.db" not in imports and not any(i and i.startswith(("api", "fastapi", "anthropic"))
                                                     for i in imports), module


def test_savepoints_translate_only_their_own_unique_violation():
    """Chaque SAVEPOINT atteint ne traduit QUE sa contrainte (nom rapporté
    par PostgreSQL) ; toute autre IntegrityError est propagée."""
    for module, name in SAVEPOINT_OWNERS:
        source = ast.unparse(_functions(SERVICES[module])[name])
        handler = source[source.index("except IntegrityError as exc:"):]
        assert handler.split("\n")[1].strip().startswith("if not _is_") and "raise" in handler.split("\n")[2]


def test_owner_centric_primitive_filters_inside_the_locking_statement():
    statement = ast.unparse(_functions(SERVICES["core.assistant_delivery"])["lock_owned_delivery"])
    query = statement[statement.index("select(AssistantDelivery)"):statement.index(".scalar_one_or_none()")]
    assert ".where(AssistantDelivery.id == assistant_turn_id, AssistantDelivery.user_id == user_id, " \
           "AssistantDelivery.conversation_key == conversation_key).with_for_update()" in query
    assert ".execution_options(populate_existing=True)" in query
    # Primitive historique inchangée (UUID seul) et toujours celle des routes legacy.
    legacy = ast.unparse(_functions(SERVICES["core.assistant_delivery"])["_lock_delivery"])
    assert ".where(AssistantDelivery.id == delivery_id).with_for_update()" in legacy
    assert "_lock_delivery(db, assistant_turn_id)" in ast.unparse(
        _functions(SERVICES["core.assistant_delivery"])["lock_delivery_for_ack"])
    assert "_lock_delivery(db, delivery_id)" in ast.unparse(
        _functions(SERVICES["core.assistant_delivery"])["bind_analysis_session"])


def test_the_provenance_check_reads_without_locking():
    source = ast.unparse(_functions(BRIDGE)["_require_owned_provenance"])
    assert "with_for_update" not in source and "populate_existing" not in source
    assert source.count("db.execute(") == 4


def test_the_provenance_check_covers_every_cognitive_reference_of_a_link():
    """Toute FK de decryptage_cognitive_links vers cognitive_events ou
    support_traces (modèle réel) est relue par la vérification de propriété
    transitive ; le parent de la SupportTrace et les AnalysisSessions des
    events aussi. Une nouvelle colonne de référence ferait échouer ce test."""
    from core.models import DecryptageCognitiveLink

    targets = {}
    for fk in DecryptageCognitiveLink.__table__.foreign_keys:
        targets.setdefault(fk.column.table.name, set()).add(fk.parent.name)
    assert targets == {
        "assistant_deliveries": {"assistant_delivery_id"},
        "cognitive_events": {"input_event_id", "input_context_event_id", "response_context_event_id",
                             "context_exit_event_id", "response_event_id"},
        "support_traces": {"support_trace_id"},
    }
    assert {col.key for col in bridge._LINK_EVENT_COLUMNS} == targets["cognitive_events"]
    assert bridge._LINK_TRACE_COLUMN.key == "support_trace_id"
    source = ast.unparse(_functions(BRIDGE)["_require_owned_provenance"])
    assert "select(*_LINK_EVENT_COLUMNS, _LINK_TRACE_COLUMN)" in source
    assert "select(SupportTrace.cognitive_event_id).where(SupportTrace.id == trace_id)" in source
    assert "events.add(parent)" in source
    # Ordre : trace (et son parent) avant les events, events avant les sessions.
    order = ["SupportTrace.cognitive_event_id", "CognitiveEvent.user_id", "AnalysisSession.user_id"]
    assert [source.index(m) for m in order] == sorted(source.index(m) for m in order)


# --------------------------------------------------------------------------
# 5. Imports transitifs, réseau, connexions
# --------------------------------------------------------------------------

IMPORT_PROBE = r'''
import json, socket, sys
attempts = []
def guard(self, address):
    attempts.append(repr(address))
    raise OSError("réseau interdit")
socket.socket.connect = guard
socket.create_connection = lambda *a, **k: guard(None, a[0] if a else None)
before = set(sys.modules)
import core.authz_f2._decryptage_orm_bridge  # noqa
import psycopg2
loaded = sorted(m for m in set(sys.modules) - before if m == "core" or m.startswith("core."))
import core.db
print(json.dumps({"loaded": loaded, "attempts": attempts, "engine": core.db.engine is not None,
                  "network": sorted(m for m in ("anthropic", "requests", "httpx", "yfinance", "api", "fastapi",
                                                "core.data_fetcher", "core.market_lookup") if m in sys.modules)}))
'''
TRANSITIVE_CORE_IMPORTS = [
    "core", "core.assistant_delivery", "core.authz_f2", "core.authz_f2._adapters",
    "core.authz_f2._decryptage_orm_bridge", "core.authz_f2.commands", "core.authz_f2.portfolio_fingerprint",
    "core.authz_f2.portfolio_results", "core.authz_f2.portfolio_values", "core.authz_f2.principal",
    "core.cognitive_capture", "core.db", "core.decryptage_cognitive_runtime", "core.decryptage_progress",
    "core.interaction_identity", "core.models"]


@pytest.mark.parametrize("database_url", [None, "postgresql://u:p@127.0.0.1:1/oryx_never_test"])
def test_importing_the_bridge_is_inventoried_and_opens_nothing(database_url):
    extra = {} if database_url is None else {"DATABASE_URL": database_url}
    out = subprocess.run([sys.executable, "-c", IMPORT_PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env={**_clean_env({}), **extra}, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["loaded"] == TRANSITIVE_CORE_IMPORTS
    assert result["attempts"] == [] and result["network"] == []
    # core.db crée un Engine paresseux si DATABASE_URL existe (jamais
    # connecté) ; le pont ne l'utilise jamais (test PostgreSQL dédié).
    assert result["engine"] is (database_url is not None)


# --------------------------------------------------------------------------
# 6. Registre de production, runner, api:app, migrations
# --------------------------------------------------------------------------

def test_production_registry_stays_closed_at_fourteen_commands():
    assert len(_adapters.REGISTRY) == 14
    assert all(spec.plan is not c.DECRYPTAGE_MUTATION for spec in _adapters.REGISTRY.values())
    assert all(spec.execute.__module__ == _adapters.__name__ for spec in _adapters.REGISTRY.values())
    assert not set(bridge.OPERATIONS) & set(_adapters.REGISTRY)
    assert list(inspect.signature(AuthorizedTransactionRunner.run).parameters) == ["self", "credential", "command"]
    assert list(inspect.signature(AuthorizedTransactionRunner.__init__).parameters) == [
        "self", "engine", "keyring", "portfolio_keyring", "clock", "fault_hook"]
    for path in (REPO_ROOT / "core" / "authz_f2_http").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "DECRYPTAGE_MUTATION" not in text and "TurnFinalization" not in text and "decryptage" not in text.lower()


def test_bench_commands_are_unknown_outside_the_isolated_fixture():
    """Processus neuf, sans fixture : le registre de production refuse les
    commandes de banc et les opérations brutes du pont, avant toute
    transaction (aucune base n'est nécessaire)."""
    code = ("import uuid\n"
            "from core.authz_f2 import _adapters\n"
            "from core.authz_f2.runner import AuthorizedTransactionRunner, UnknownCommand\n"
            "from tests.authz_f2_pr9a_support import BenchAck, BenchTurn, turn, ack\n"
            "runner = AuthorizedTransactionRunner.__new__(AuthorizedTransactionRunner)\n"
            "refused = []\n"
            "for command in (turn('c', 'business'), ack('c', uuid.uuid4()), turn('c', 'business').operation):\n"
            "    try:\n"
            "        runner.run(None, command)\n"
            "    except UnknownCommand:\n"
            "        refused.append(type(command).__name__)\n"
            "print(len(_adapters.REGISTRY), refused)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({}), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "14 ['BenchTurn', 'BenchAck', 'TurnFinalization']"


def test_real_entrypoint_never_loads_the_bridge():
    code = ("import sys, json\nimport api\n"
            "print(json.dumps(sorted(m for m in sys.modules if 'authz' in m or 'orm_bridge' in m)))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({"ORYX_DECRYPTAGE_F2": "1", "ORYX_PR9A_ENABLED": "1", "ORYX_F2_ENABLED": "1"}),
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
    source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    for forbidden in ("orm_bridge", "DECRYPTAGE_MUTATION", "TurnFinalization", "DeliveryAcknowledgement",
                      "lock_owned_delivery", "/api/v2/me/decryptage", "/api/v2/me/assistant-deliveries"):
        assert forbidden not in source, forbidden


def test_no_migration_and_no_new_table():
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(versions) == 20 and versions[-1] == "0020_position_revision.py"
    assert not any("0021" in name for name in versions)
    source = BRIDGE.read_text(encoding="utf-8") + (REPO_ROOT / "core" / "assistant_delivery.py").read_text("utf-8")
    assert "decryptage_turn_reservations" not in source and "__tablename__" not in source


def test_runner_registry_lookup_refuses_unknown_types_first():
    runner = AuthorizedTransactionRunner.__new__(AuthorizedTransactionRunner)
    with pytest.raises(UnknownCommand):
        runner.run(None, bridge.DeliveryAcknowledgement(uuid.uuid4(), "conv"))
    assert ad.lock_owned_delivery.__module__ == "core.assistant_delivery"
