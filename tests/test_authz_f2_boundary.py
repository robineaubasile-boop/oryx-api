"""Contrôles statiques de la frontière d'autorisation (R1 PR 5, BF1 / BF2).

Sans base de données. La garantie BF1 repose sur une interface restreinte
(commandes typées, registre fermé, contexte sans connexion), sur
l'inventaire des adaptateurs SQL et sur ces tests négatifs ; elle ne
prétend pas isoler le processus contre du code Python hostile.

- Le SQL n'existe que dans core/authz_f2/_adapters.py, en constantes, sans
  instruction transactionnelle ni composition dynamique ; toute requête est
  filtrée par le user_id serveur, et toute requête individuelle par
  l'identifiant ET ce user_id.
- Aucun module de la frontière n'appelle commit(), rollback(), begin(),
  begin_nested(), connect() ni SessionLocal / get_db ; seul le runner
  ouvre des transactions (core.auth_f2.txn.transaction).
- Plus aucune interface « operation(conn, principal) » ; le runner
  n'expose que run(credential, command).
- Ordre des verrous du runner : users -> session -> génération -> métier
  -> T_authorization, compatible avec l'ordre global F2.
- Aucun client réseau (Claude, EODHD, Supabase, Resend, HTTP) n'est
  importé par la frontière.
"""
import ast
import inspect
import re

from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2.runner import AuthorizedTransactionRunner
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

AUTHZ = REPO_ROOT / "core" / "authz_f2"
AUTHZ_HTTP = REPO_ROOT / "core" / "authz_f2_http"
BOUNDARY_FILES = sorted(list(AUTHZ.glob("*.py")) + list(AUTHZ_HTTP.glob("*.py")))
ADAPTERS = AUTHZ / "_adapters.py"
TRANSACTION_METHODS = {"commit", "rollback", "begin", "begin_nested", "connect", "close", "invalidate", "detach",
                       "exec_driver_sql", "execution_options", "raw_connection", "get_transaction"}
FORBIDDEN_SQL = re.compile(r"\b(COMMIT|ROLLBACK|BEGIN|SAVEPOINT|RELEASE|SET|RESET|LOCK|DELETE|INSERT|TRUNCATE|DROP|"
                           r"ALTER|CREATE|GRANT|REVOKE|COPY|DO|CALL|EXECUTE|PREPARE|LISTEN|NOTIFY)\b", re.I)


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _calls(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _attr_calls(tree):
    return {n.func.attr for n in _calls(tree) if isinstance(n.func, ast.Attribute)}


def test_sql_is_executed_only_by_the_audited_adapters():
    for path in BOUNDARY_FILES:
        tree = _tree(path)
        executes = [n for n in _calls(tree) if isinstance(n.func, ast.Attribute) and n.func.attr == "execute"
                    and not (isinstance(n.func.value, ast.Name) and n.func.value.id == "spec")]  # spec.execute : adaptateur
        texts = [n for n in _calls(tree) if isinstance(n.func, ast.Attribute) and n.func.attr == "text"]
        if path == ADAPTERS:
            assert executes and texts
        else:
            assert executes == [] and texts == [], path.name


def test_no_boundary_module_controls_a_transaction():
    for path in BOUNDARY_FILES:
        tree = _tree(path)
        assert not _attr_calls(tree) & TRANSACTION_METHODS, path.name
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not names & {"SessionLocal", "get_db", "sessionmaker", "Session", "create_engine"}, path.name
    runner_tree = _tree(AUTHZ / "runner.py")
    opened = [n for n in _calls(runner_tree) if isinstance(n.func, ast.Name) and n.func.id == "transaction"]
    assert len(opened) == 2  # phase A, phase B


def test_sql_is_never_composed_from_data():
    tree = _tree(ADAPTERS)
    for call in _calls(tree):
        if isinstance(call.func, ast.Attribute) and call.func.attr == "text":
            (argument,) = call.args
            assert isinstance(argument, ast.Name), ast.dump(argument)  # constante ou paramètre `sql` des aides
    helper_args = {"_rows", "_first"}
    for call in _calls(tree):
        if isinstance(call.func, ast.Name) and call.func.id in helper_args:
            sql = call.args[1]
            assert isinstance(sql, (ast.Name, ast.Subscript)), ast.dump(sql)
            name = sql.value.id if isinstance(sql, ast.Subscript) else sql.id
            assert re.fullmatch(r"_[A-Z_]+", name), name
    assert not any(isinstance(node, (ast.JoinedStr, ast.BinOp)) for node in ast.walk(tree)
                   if not _is_constant_concatenation(node))


def _is_constant_concatenation(node):
    """Seules concaténations admises : constantes SQL entre elles (`_A + "..."`)."""
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.Add) and all(
            isinstance(side, ast.Constant) or (isinstance(side, ast.Name) and re.fullmatch(r"_[A-Z_]+", side.id))
            or _is_constant_concatenation(side) for side in (node.left, node.right))
    return False


def _adapter_statements():
    out = {}
    for name, value in vars(_adapters).items():
        if re.fullmatch(r"_[A-Z_]+", name):
            if isinstance(value, str):
                out[name] = value
            elif isinstance(value, dict):
                out.update({f"{name}[{k}]": v for k, v in value.items()})
    return out


def test_adapter_statements_are_owner_scoped_and_non_transactional():
    statements = {k: v for k, v in _adapter_statements().items() if not k.endswith("_COLUMNS")}
    assert set(statements) == {
        "_LOCK_PROFILE[FOR KEY SHARE]", "_LOCK_PROFILE[FOR UPDATE]", "_READ_PROFILE", "_LIST_POSITIONS",
        "_READ_POSITION", "_LIST_THESES", "_LIST_THESES_FOR_TICKER", "_READ_DELIVERY", "_LIST_CONVERSATION_DELIVERIES",
        "_LOCK_POSITION", "_UPDATE_POSITION_QUANTITY"}
    for name, sql in statements.items():
        assert ";" not in sql and "--" not in sql and "/*" not in sql, name
        assert sql.startswith(("SELECT ", "UPDATE portfolio_positions ")), name
        assert not FORBIDDEN_SQL.search(sql.replace("FOR UPDATE", "").replace("UPDATE portfolio_positions SET", "")), name
        assert ":u" in sql, name  # toujours le user_id serveur
        if ":id" in sql:
            assert re.search(r"WHERE id = :id AND user_id = :u", sql), name
        elif "users" not in sql:
            assert "WHERE user_id = :u" in sql, name
    for table in ("portfolio_positions", "investment_theses", "assistant_deliveries"):
        assert any(table in sql for sql in statements.values()), table


def test_closed_registry_and_lock_plans():
    reads = {c.ReadProfile, c.ListPositions, c.ReadPosition, c.ListTheses, c.ListThesesForTicker, c.ReadDelivery,
             c.ListConversationDeliveries}
    assert set(_adapters.REGISTRY) == reads | {c.UpdatePositionQuantity}
    for command, spec in _adapters.REGISTRY.items():
        assert spec.plan is (c.USER_MUTATION if command is c.UpdatePositionQuantity else c.READ)
        assert spec.lock.__module__ == spec.execute.__module__ == _adapters.__name__
        assert spec.lock.__name__.startswith("_") and spec.execute.__name__.startswith("_")
    assert c.READ == c.LockPlan("read", "FOR KEY SHARE", ())
    assert c.USER_MUTATION == c.LockPlan("user_mutation", "FOR UPDATE", ("portfolio_positions",))
    assert c.SESSION_LOCK == c.GENERATION_LOCK == "FOR SHARE"
    assert set(_adapters._LOCK_PROFILE) == {plan.users_lock for plan in c.PLANS}


def test_no_operation_conn_principal_contract_remains():
    pattern = re.compile(r"operation\s*\(\s*conn\s*,\s*principal")
    for root in (REPO_ROOT / "core", REPO_ROOT / "scripts"):
        for path in root.rglob("*.py"):
            assert not pattern.search(path.read_text(encoding="utf-8")), path
    assert not pattern.search((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    assert list(inspect.signature(AuthorizedTransactionRunner.run).parameters) == ["self", "credential", "command"]
    assert {name for name in vars(AuthorizedTransactionRunner) if not name.startswith("_")} == {"run"}
    # Aucune fonction publique de la frontière ne reçoit une connexion.
    for path in BOUNDARY_FILES:
        if path == ADAPTERS:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert "conn" not in [a.arg for a in node.args.args + node.args.kwonlyargs], (path.name, node.name)
    public_adapters = {n.name for n in _tree(ADAPTERS).body if isinstance(n, ast.FunctionDef)
                       and not n.name.startswith("_")}
    assert public_adapters == {"lock_profile"}  # seul verrou appelé par le runner hors registre


def test_runner_lock_order_and_t_authorization():
    source = inspect.getsource(AuthorizedTransactionRunner._run)
    order = [source.index(marker) for marker in ("_adapters.lock_profile(", "sessions.lock_session(",
                                                 "spec.lock(", 'self._hook("locked")', "self._clock(conn)",
                                                 "self._decide(", 'self._hook("authorized")', "spec.execute(")]
    assert order == sorted(order)
    assert "GENERATION_LOCK" in source
    service = (REPO_ROOT / "core" / "auth_f2" / "service.py").read_text(encoding="utf-8")
    global_order = ("invitation -> profil users ->\nopération -> candidat -> demande OTP -> tentative -> session ->\n"
                    "génération -> identité")
    assert global_order in service
    ranks = global_order.replace("\n", " ").split(" -> ")
    assert ranks.index("profil users") < ranks.index("session") < ranks.index("génération")
    # Aucune transaction F2 ne verrouille les tables métiers (rang « après
    # génération » sans conflit possible avec l'ordre F2).
    for path in (REPO_ROOT / "core" / "auth_f2").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for table in ("portfolio_positions", "investment_theses", "assistant_deliveries"):
            assert table not in text, (path.name, table)
    sessions_source = (REPO_ROOT / "core" / "auth_f2" / "sessions.py").read_text(encoding="utf-8")
    assert "SELECT * FROM oryx_sessions WHERE id = :s FOR SHARE" in sessions_source


def test_boundary_imports_no_network_client():
    forbidden = {"httpx", "requests", "anthropic", "urllib.request", "http", "socket", "ssl", "aiohttp", "yfinance",
                 "supabase", "resend", "core.data_fetcher", "core.market_lookup", "core.ticker_resolver",
                 "core.auth_f2.http_providers", "core.auth_f2.providers", "core.evaluation_provider", "core.db"}
    for path in BOUNDARY_FILES:
        for node in ast.walk(_tree(path)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not any(name == f or name.startswith(f + ".") for f in forbidden), (path.name, name)


def test_business_locks_match_the_announced_plan():
    """Un verrou hors du plan annoncé est refusé par le contrat : seules
    les commandes USER_MUTATION verrouillent une ligne métier, et seulement
    dans les tables de leur plan ; les lectures n'en prennent aucun."""
    statements = _adapter_statements()
    locking = {name for name, sql in statements.items() if re.search(r"\bFOR (UPDATE|SHARE|NO KEY UPDATE|KEY SHARE)\b",
                                                                     sql)}
    assert locking == {"_LOCK_PROFILE[FOR KEY SHARE]", "_LOCK_PROFILE[FOR UPDATE]", "_LOCK_POSITION"}
    lock_sql = {"_lock_position": statements["_LOCK_POSITION"]}
    for spec in _adapters.REGISTRY.values():
        if spec.plan is c.READ:
            assert spec.lock is _adapters._no_business_lock
        else:
            tables = set(re.findall(r"FROM (\w+)", lock_sql[spec.lock.__name__]))
            assert tables and tables <= set(spec.plan.business_locks)
