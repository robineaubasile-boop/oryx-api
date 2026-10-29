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
  -> T_authorization, compatible avec l'ordre global F2 ; R1 PR 7 :
  isolation vérifiée d'abord, puis reçu avant position.
- Aucun client réseau (Claude, EODHD, Supabase, Resend, HTTP) n'est
  importé par la frontière.
- R1 PR 8 : registre fermé à quatorze commandes ; trois lectures sous le
  plan READ inchangé, sans verrou métier ni écriture ; instructions
  fermées (une par page ou par thèse), toujours filtrées par le user_id
  serveur, tickers sous COLLATE "C", thesis_text jamais lu hors du détail
  et projeté sous borne, provenance jointe sur id ET user_id ET ticker.
- R1 PR 9-A : plan DECRYPTAGE_MUTATION (users FOR NO KEY UPDATE, instruction
  fermée) sans aucune commande de production ; exception NOMINATIVE pour le
  seul pont ORM privé core/authz_f2/_decryptage_orm_bridge.py (Session
  sur la connexion du runner, rollback_only), chacune de ses capacités
  étant bornée ici et dans tests/test_authz_f2_pr9a_boundary.py ; les
  autres modules restent soumis à toutes les interdictions.
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
# R1 PR 9-A : unique module privilégié (exception nominative, V2.2-03).
BRIDGE = AUTHZ / "_decryptage_orm_bridge.py"
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


def _function(tree, name):
    (node,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    return node


def test_sql_is_executed_only_by_the_audited_adapters():
    for path in BOUNDARY_FILES:
        tree = _tree(path)
        executes = [n for n in _calls(tree) if isinstance(n.func, ast.Attribute) and n.func.attr == "execute"
                    and not (isinstance(n.func.value, ast.Name) and n.func.value.id == "spec")]  # spec.execute : adaptateur
        texts = [n for n in _calls(tree) if isinstance(n.func, ast.Attribute) and n.func.attr == "text"]
        if path == ADAPTERS:
            assert executes and texts
        elif path == BRIDGE:
            # Aucun SQL textuel ; seules lectures ORM sans verrou de la
            # propriété transitive (select(...) construit, jamais du texte).
            assert texts == [], path.name
            owner = _function(tree, "_require_owned_provenance")
            assert {id(n) for n in executes} == {id(n) for n in _calls(owner) if isinstance(n.func, ast.Attribute)
                                                and n.func.attr == "execute"}
            assert len(executes) == 3
            for call in executes:
                (statement,) = call.args
                assert isinstance(statement, ast.Call) and ast.unparse(statement).startswith("select("), path.name
                assert "with_for_update" not in ast.unparse(statement)
        else:
            assert executes == [] and texts == [], path.name


def test_no_boundary_module_controls_a_transaction():
    for path in BOUNDARY_FILES:
        tree = _tree(path)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        if path == BRIDGE:
            # Exception nominative R1 PR 9-A : la Session du pont, close() de
            # son seul nettoyage, begin(nested=True) des SAVEPOINT des
            # services, get_transaction() du contrôle de la transaction du
            # runner ; rien d'autre (ni commit, rollback, connect, ...).
            assert _attr_calls(tree) & TRANSACTION_METHODS == {"close", "begin", "get_transaction"}
            located = {(n.func.attr, f.name) for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                       for n in _calls(f) if isinstance(n.func, ast.Attribute) and n.func.attr in TRANSACTION_METHODS}
            assert located == {("close", "_release"), ("begin", "begin"), ("get_transaction", "_require_live"),
                               ("get_transaction", "_execute")}
            for call in _calls(tree):
                if isinstance(call.func, ast.Attribute) and call.func.attr in ("close", "begin"):
                    assert ast.unparse(call) in ("super().close()", "super().begin(nested=True)"), ast.unparse(call)
            assert not names & {"SessionLocal", "get_db", "sessionmaker", "create_engine"}, path.name
            assert "Session" in names
            continue
        assert not _attr_calls(tree) & TRANSACTION_METHODS, path.name
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


# R1 PR 7 : écritures portefeuille admises, chacune exactement préfixée.
WRITES = {
    "_UPDATE_PROFILE_LEVEL": "UPDATE users SET level = :l, level_revision = level_revision + 1 ",
    "_CREATE_POSITION": "INSERT INTO portfolio_positions (user_id, ticker, quantity, purchase_price, target_percent, "
                        "envelope, created_at, updated_at) VALUES (:u, ",
    "_REPLACE_POSITION": "UPDATE portfolio_positions SET ticker = :ticker, ",
    "_DELETE_POSITION": "DELETE FROM portfolio_positions WHERE id = :id AND user_id = :u AND position_revision = :r ",
    "_INSERT_RECEIPT": "INSERT INTO portfolio_write_idempotency (user_id, ",
}


def test_adapter_statements_are_owner_scoped_and_non_transactional():
    statements = {k: v for k, v in _adapter_statements().items()
                  if not k.endswith(("_COLUMNS", "_RETURNING"))}
    assert set(statements) == {
        "_LOCK_PROFILE[FOR KEY SHARE]", "_LOCK_PROFILE[FOR UPDATE]", "_LOCK_PROFILE[FOR NO KEY UPDATE]",
        "_READ_PROFILE", "_LIST_POSITIONS",
        "_READ_POSITION", "_LIST_THESES", "_LIST_THESES_FOR_TICKER", "_READ_DELIVERY", "_LIST_CONVERSATION_DELIVERIES",
        "_UPDATE_PROFILE_LEVEL", "_TRANSACTION_ISOLATION", "_LOCK_RECEIPT", "_LOCK_POSITION", "_CREATE_POSITION",
        "_REPLACE_POSITION", "_DELETE_POSITION", "_INSERT_RECEIPT",
        # R1 PR 8 : une instruction fermée par page ou par thèse.
        "_ANALYSIS_OVERVIEW_PAGE[first]", "_ANALYSIS_OVERVIEW_PAGE[after]", "_THESES_PAGE[first]",
        "_THESES_PAGE[first+ticker]", "_THESES_PAGE[after_date]", "_THESES_PAGE[after_date+ticker]",
        "_THESES_PAGE[after_null]", "_THESES_PAGE[after_null+ticker]", "_READ_THESIS"}
    # Seule requête sans user_id : l'isolation active de la transaction.
    assert statements.pop("_TRANSACTION_ISOLATION") == "SELECT current_setting('transaction_isolation')"
    for name, sql in statements.items():
        assert ";" not in sql and "--" not in sql and "/*" not in sql, name
        prefix = WRITES.get(name, "SELECT ")
        assert sql.startswith(prefix), name
        assert not FORBIDDEN_SQL.search(sql.replace("FOR UPDATE", "").replace(prefix, "")), name
        assert "NO KEY" not in sql or sql == "SELECT id FROM users WHERE id = :u FOR NO KEY UPDATE", name
        assert ":u" in sql, name  # toujours le user_id serveur
        if name in ("_CREATE_POSITION", "_INSERT_RECEIPT"):
            assert "VALUES (:u, " in sql and "WHERE" not in sql, name  # propriétaire = user_id serveur
        elif ":id" in sql:
            assert re.search(r"WHERE id = :id AND user_id = :u", sql), name
        elif "users" not in sql:
            assert "WHERE user_id = :u" in sql, name
        else:  # profil : toujours la ligne du user_id serveur
            assert "FROM users WHERE id = :u" in sql or "users SET" in sql and "WHERE id = :u AND " in sql, name
    # CAS : toute écriture conditionnelle d'une position porte la révision attendue.
    for name in ("_REPLACE_POSITION", "_DELETE_POSITION"):
        assert "WHERE id = :id AND user_id = :u AND position_revision = :r " in statements[name], name
    assert "position_revision = position_revision + 1 WHERE" in statements["_REPLACE_POSITION"]
    assert statements["_LOCK_RECEIPT"].endswith(
        "WHERE user_id = :u AND operation_type = :t AND operation_id = :o FOR UPDATE")
    for name in ("_LIST_POSITIONS", "_READ_POSITION", "_LOCK_POSITION", "_CREATE_POSITION", "_REPLACE_POSITION"):
        # flottants lus en octets IEEE 754, jamais par la sortie texte
        assert ("float8send(quantity) AS quantity, float8send(purchase_price) AS purchase_price, "
                "float8send(target_percent) AS target_percent, envelope") in statements[name], name
    for table in ("portfolio_positions", "investment_theses", "assistant_deliveries", "portfolio_write_idempotency"):
        assert any(table in sql for sql in statements.values()), table


def test_closed_registry_and_lock_plans():
    reads = {c.ReadProfile, c.ListPositions, c.ReadPosition, c.ListTheses, c.ListThesesForTicker, c.ReadDelivery,
             c.ListConversationDeliveries,
             # R1 PR 8
             c.ListAnalysisOverviewPage, c.ListThesesPage, c.ReadThesis}
    mutations = {c.UpdateProfileLevel: c.PROFILE_MUTATION, c.CreatePortfolioPosition: c.PORTFOLIO_MUTATION,
                 c.ReplacePortfolioPosition: c.PORTFOLIO_MUTATION, c.DeletePortfolioPosition: c.PORTFOLIO_MUTATION}
    assert set(_adapters.REGISTRY) == reads | set(mutations) and len(_adapters.REGISTRY) == 14
    # R1 PR 7 : la mutation de démonstration sans CAS n'est plus invocable.
    assert c.UpdatePositionQuantity not in _adapters.REGISTRY
    for command, spec in _adapters.REGISTRY.items():
        assert spec.plan is mutations.get(command, c.READ)
        assert spec.lock.__module__ == spec.execute.__module__ == _adapters.__name__
        assert spec.lock.__name__.startswith("_") and spec.execute.__name__.startswith("_")
        if spec.plan is c.PORTFOLIO_MUTATION:
            assert spec.keyed and spec.states == _adapters.PORTFOLIO_STATES
            assert spec.lock is _adapters._lock_portfolio and spec.execute is _adapters._write_portfolio
        else:
            assert not spec.keyed and spec.states == (type(None),)
    assert _adapters.PORTFOLIO_STATES == (_adapters.ReceiptFound, _adapters.PositionFound, _adapters.PositionMissing,
                                          _adapters.CreateReady)
    assert c.READ == c.LockPlan("read", "FOR KEY SHARE", ())
    # R1 PR 6 : users FOR UPDATE, session, génération ; aucun verrou métier.
    assert c.PROFILE_MUTATION == c.LockPlan("profile_mutation", "FOR UPDATE", ())
    assert not c.READ.requires_read_committed and not c.PROFILE_MUTATION.requires_read_committed
    # R1 PR 7 : reçu puis position, sous READ COMMITTED vérifié.
    assert c.PORTFOLIO_MUTATION == c.LockPlan("portfolio_mutation", "FOR UPDATE",
                                              ("portfolio_write_idempotency", "portfolio_positions"), True)
    # R1 PR 9-A : users FOR NO KEY UPDATE, aucun verrou métier préparatoire,
    # READ COMMITTED exigé ; aucune commande de production ne l'utilise.
    assert c.DECRYPTAGE_MUTATION == c.LockPlan("decryptage_mutation", "FOR NO KEY UPDATE", (), True)
    assert c.PLANS == (c.READ, c.PROFILE_MUTATION, c.PORTFOLIO_MUTATION, c.DECRYPTAGE_MUTATION)
    assert all(spec.plan is not c.DECRYPTAGE_MUTATION for spec in _adapters.REGISTRY.values())
    assert _adapters._LOCK_PROFILE["FOR NO KEY UPDATE"] == "SELECT id FROM users WHERE id = :u FOR NO KEY UPDATE"
    assert dict(_adapters.ISOLATION_ALERTS) == {"portfolio_mutation": "portfolio_isolation_refused",
                                                "decryptage_mutation": "decryptage_isolation_refused"}
    assert {plan.name for plan in c.PLANS if plan.requires_read_committed} == set(_adapters.ISOLATION_ALERTS)
    assert not hasattr(c, "USER_MUTATION")
    assert c.SESSION_LOCK == c.GENERATION_LOCK == "FOR SHARE"
    assert set(_adapters._LOCK_PROFILE) == {plan.users_lock for plan in c.PLANS}
    assert dict(_adapters.OPERATION_TYPES) == {c.CreatePortfolioPosition: "portfolio_position_create",
                                               c.ReplacePortfolioPosition: "portfolio_position_update",
                                               c.DeletePortfolioPosition: "portfolio_position_delete"}


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
        receiving = set()
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if "conn" in [a.arg for a in node.args.args + node.args.kwonlyargs]:
                    receiving.add(node.name)
        # R1 PR 9-A : seules trois fonctions PRIVÉES du pont reçoivent la
        # connexion du runner (point d'entrée, contrôle, Session du pont).
        assert receiving == ({"_execute", "_require_live", "__init__"} if path == BRIDGE else set()), path.name
    public_adapters = {n.name for n in _tree(ADAPTERS).body if isinstance(n, ast.FunctionDef)
                       and not n.name.startswith("_")}
    # Seuls appels du runner hors registre : verrou users, isolation (R1 PR 7).
    assert public_adapters == {"lock_profile", "require_read_committed"}


def test_runner_lock_order_and_t_authorization():
    source = inspect.getsource(AuthorizedTransactionRunner._run)
    order = [source.index(marker) for marker in ("_adapters.require_read_committed(", "_adapters.lock_profile(",
                                                 "sessions.lock_session(",
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
        for table in ("portfolio_positions", "investment_theses", "assistant_deliveries",
                      "portfolio_write_idempotency"):
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
    les commandes PORTFOLIO_MUTATION verrouillent des lignes métiers, dans
    l'ordre de leur plan (reçu, puis position) ; les lectures et la
    mutation de profil n'en prennent aucun."""
    statements = _adapter_statements()
    locking = {name for name, sql in statements.items() if re.search(r"\bFOR (UPDATE|SHARE|NO KEY UPDATE|KEY SHARE)\b",
                                                                     sql)}
    assert locking == {"_LOCK_PROFILE[FOR KEY SHARE]", "_LOCK_PROFILE[FOR UPDATE]", "_LOCK_PROFILE[FOR NO KEY UPDATE]",
                       "_LOCK_RECEIPT", "_LOCK_POSITION"}
    locked_tables = [re.search(r"FROM (\w+)", statements[name]).group(1) for name in ("_LOCK_RECEIPT",
                                                                                    "_LOCK_POSITION")]
    for spec in _adapters.REGISTRY.values():
        if not spec.plan.business_locks:  # READ et PROFILE_MUTATION
            assert spec.lock is _adapters._no_business_lock
        else:
            assert tuple(locked_tables) == spec.plan.business_locks
    source = inspect.getsource(_adapters._lock_portfolio)
    assert source.index("_LOCK_RECEIPT") < source.index("CreateReady()") < source.index("_LOCK_POSITION")


def test_pr8_statements_are_closed_collated_and_bounded():
    """R1 PR 8 : instructions fermées des trois lectures (plan READ, aucune
    écriture ni verrou), tickers comparés, partitionnés, joints et triés
    sous COLLATE "C", prédicats keyset fermés, thesis_text absent de la
    synthèse et de la liste, provenance jointe sur id ET user_id ET ticker
    ET user_id serveur, valeurs restituées sous borne."""
    overview = _adapters._ANALYSIS_OVERVIEW_PAGE
    theses = _adapters._THESES_PAGE
    assert set(overview) == {"first", "after"}
    assert set(theses) == {"first", "first+ticker", "after_date", "after_date+ticker", "after_null",
                           "after_null+ticker"}
    pr8 = list(overview.values()) + list(theses.values()) + [_adapters._READ_THESIS]
    for sql in pr8:
        assert sql.startswith("SELECT ") and not re.search(r"\bFOR (UPDATE|SHARE|NO KEY UPDATE|KEY SHARE)\b", sql)
        assert "CASE WHEN octet_length(ticker) <= 512 THEN ticker END AS ticker" in sql or \
            "CASE WHEN octet_length(k.ticker) <= 512 THEN k.ticker END AS ticker" in sql
        # Hors de la projection bornée, toute référence à un ticker (égalité,
        # union, partition, jointure, keyset, tri) est sous collation "C".
        stripped = sql
        for alias in ("k.", ""):
            stripped = stripped.replace(f"CASE WHEN octet_length({alias}ticker) <= 512 THEN {alias}ticker END AS ticker, "
                                        f"octet_length({alias}ticker) AS ticker_bytes", "")
        stripped = stripped.replace(" AS ticker,", ",").replace(" AS ticker ", " ")
        references = list(re.finditer(r"\b(?:\w+\.)?ticker\b", stripped))
        assert references, sql
        for match in references:
            assert stripped[match.end():].startswith(' COLLATE "C"'), stripped[match.start() - 40:match.end() + 20]
    for sql in list(overview.values()) + list(theses.values()):
        assert "thesis_text" not in sql
    for sql in overview.values():
        assert sql.count("WHERE user_id = :u") == 4  # union (2) + session active + dernière thèse
        assert "ORDER BY started_at DESC, updated_at DESC, id DESC" in sql
        assert "ORDER BY created_at DESC NULLS LAST, id DESC" in sql
        assert sql.endswith('ORDER BY k.ticker COLLATE "C" ASC LIMIT :n')
    assert 'WHERE k.ticker COLLATE "C" > CAST(:a AS text) COLLATE "C" ' in overview["after"]
    provenance = ("WHERE s.id = investment_theses.analysis_session_id AND s.user_id = investment_theses.user_id "
                  'AND s.user_id = :u AND s.ticker COLLATE "C" = investment_theses.ticker COLLATE "C"')
    for name, sql in theses.items():
        assert provenance in sql and sql.endswith(
            "ORDER BY investment_theses.created_at DESC NULLS LAST, investment_theses.id DESC LIMIT :n"), name
        assert ('ticker COLLATE "C" = CAST(:t AS text) COLLATE "C"' in sql) == name.endswith("+ticker"), name
        assert ("(created_at < :d OR (created_at = :d AND id < :i) OR created_at IS NULL)" in sql) == (
            name.startswith("after_date")), name
        assert ("(created_at IS NULL AND id < :i)" in sql) == name.startswith("after_null"), name
    assert provenance in _adapters._READ_THESIS
    assert "CASE WHEN octet_length(thesis_text) <= 131072 THEN thesis_text END" in _adapters._READ_THESIS
    assert _adapters._READ_THESIS.endswith("FROM investment_theses WHERE id = :id AND user_id = :u")
    # L'identifiant brut analysis_session_id n'est jamais projeté : il ne
    # sert qu'à la comparaison de la provenance relationnelle.
    for sql in pr8:
        assert "analysis_session_id" not in sql.replace("s.id = investment_theses.analysis_session_id", "")
