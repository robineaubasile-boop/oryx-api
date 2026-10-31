"""R1 PR 9-B1 — contrôles statiques et sans base de la réservation Décrypter.

Complètent tests/test_authz_f2_boundary.py (inventaire BF1 nominatif) et
tests/test_authz_f2_dormancy.py (matrice d'imports, dormance de api:app) :

- deux familles de clés exclusives (Spec.keyed = portefeuille seulement,
  Spec.decryptage_keyed = Décrypter seulement), refus des deux à la fois à
  la construction et, par défense, dans le runner avant toute transaction ;
- trousseau Décrypter de type EXACT, indépendant des clés F2 et
  portefeuille, jamais substitué ;
- commandes de types exacts (sous-classes refusées) ;
- adaptateurs B1 : aucune Session, aucun service ORM, aucun pont, aucune
  table cognitive ; mêmes noms de contraintes d'identité que le service
  historique ; prédicats de collision fermés (SQLSTATE 23505 ET nom exact) ;
- service historique des conversations : interface publique inchangée ;
- aucun module applicatif (routes, workers, scripts, frontends) n'invoque
  les commandes B1.
"""
import ast
import dataclasses
import inspect
import uuid

import pytest
import sqlalchemy as sa

from core import interaction_identity as ii
from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.decryptage_fingerprint import DecryptageKeyring
from core.authz_f2.portfolio_fingerprint import PortfolioKeyring
from core.authz_f2.runner import UNAVAILABLE, AuthorizationConfigError, AuthorizedTransactionRunner, UnknownCommand
from tests.f2_support import make_keyring
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

ENGINE_URL = "postgresql+psycopg2://nobody@127.0.0.1:1/oryx_never_test"
K = bytes(range(100, 132))


def _engine():
    return sa.create_engine(ENGINE_URL)


def test_a_spec_never_carries_two_key_families():
    with pytest.raises(_adapters.SpecError):
        _adapters.Spec(c.DECRYPTAGE_MUTATION, _adapters._no_business_lock, _adapters._reserve_turn, keyed=True,
                       decryptage_keyed=True)
    with pytest.raises(_adapters.SpecError):
        _adapters.Spec(c.READ, _adapters._no_business_lock, _adapters._read_profile, decryptage_keyed=1)
    assert _adapters.Spec(c.READ, _adapters._no_business_lock, _adapters._read_profile).decryptage_keyed is False


def test_the_runner_refuses_a_smuggled_double_family_before_any_transaction(monkeypatch, caplog):
    spec = _adapters.Spec(c.DECRYPTAGE_MUTATION, _adapters._no_business_lock, _adapters._reserve_turn,
                          decryptage_keyed=True)
    object.__setattr__(spec, "keyed", True)  # contournement du constructeur (défense en profondeur)
    monkeypatch.setattr(_adapters, "REGISTRY", {**_adapters.REGISTRY, c.ReserveDecryptageTurn: spec})
    engine = _engine()
    runner = AuthorizedTransactionRunner(engine, make_keyring(), portfolio_keyring=PortfolioKeyring({"p": K[::-1]}, "p"),
                                         decryptage_keyring=DecryptageKeyring({"d1": K}, "d1"))
    command = c.ReserveDecryptageTurn("conv", uuid.uuid4(), "MC.PA", "", "", None)
    assert runner.run(None, command).status == UNAVAILABLE
    assert "alert=key_families_conflict" in caplog.text
    assert engine.pool.checkedout() == 0  # aucune connexion, aucune transaction


def test_the_decryptage_keyring_has_an_exact_type_and_independent_keys():
    f2 = make_keyring()
    engine = _engine()
    for wrong in (PortfolioKeyring({"d1": K}, "d1"), {"d1": K}, f2):
        with pytest.raises(AuthorizationConfigError):
            AuthorizedTransactionRunner(engine, f2, decryptage_keyring=wrong)

    class Sub(DecryptageKeyring):
        __slots__ = ()

    with pytest.raises(AuthorizationConfigError):
        AuthorizedTransactionRunner(engine, f2, decryptage_keyring=Sub({"d1": K}, "d1"))
    f2_key = f2.key("abuse", f2.current_version("abuse"))
    with pytest.raises(AuthorizationConfigError, match="F2"):
        AuthorizedTransactionRunner(engine, f2, decryptage_keyring=DecryptageKeyring({"d1": f2_key}, "d1"))
    with pytest.raises(AuthorizationConfigError, match="portefeuille"):
        AuthorizedTransactionRunner(engine, f2, portfolio_keyring=PortfolioKeyring({"p1": K}, "p1"),
                                    decryptage_keyring=DecryptageKeyring({"d1": K}, "d1"))
    AuthorizedTransactionRunner(engine, f2)  # facultatif : les anciennes compositions restent valides
    assert engine.pool.checkedout() == 0


def test_dispatch_gives_each_family_only_to_its_commands():
    source = inspect.getsource(AuthorizedTransactionRunner._run)
    assert ("if spec.keyed:\n                    value = spec.execute(conn, context, command, business, "
            "self._portfolio, self._hook)\n                elif spec.decryptage_keyed:\n                    value = "
            "spec.execute(conn, context, command, business, self._decryptage)\n                else:\n"
            "                    value = spec.execute(conn, context, command, business)") in source
    keyed = {cmd for cmd, spec in _adapters.REGISTRY.items() if spec.keyed}
    decryptage = {cmd for cmd, spec in _adapters.REGISTRY.items() if spec.decryptage_keyed}
    assert keyed == {c.CreatePortfolioPosition, c.ReplacePortfolioPosition, c.DeletePortfolioPosition}
    assert decryptage == {c.ReserveDecryptageTurn, c.RecordDecryptageAttemptFailure}
    assert list(inspect.signature(AuthorizedTransactionRunner.run).parameters) == ["self", "credential", "command"]
    assert {n for n in vars(AuthorizedTransactionRunner) if not n.startswith("_")} == {"run"}
    assert "_decryptage" in AuthorizedTransactionRunner.__slots__


def test_subclasses_of_the_b1_commands_are_refused():
    @dataclasses.dataclass(frozen=True, slots=True)
    class Smuggled(c.ReserveDecryptageTurn):
        pass

    runner = AuthorizedTransactionRunner(_engine(), make_keyring(), decryptage_keyring=DecryptageKeyring({"d": K}, "d"))
    with pytest.raises(UnknownCommand):
        runner.run(None, Smuggled("conv", uuid.uuid4(), "MC.PA", "", "", None))


def test_b1_adapters_use_only_the_runner_connection_and_closed_sql():
    tree = ast.parse((REPO_ROOT / "core" / "authz_f2" / "_adapters.py").read_text(encoding="utf-8"))
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    b1 = ("_register_conversation", "_insert_reservation", "_lease_clock", "_reservation", "_turn", "_fingerprint",
          "_mutated", "_block", "_lease", "_grant", "_legacy_delivery", "_finalized", "_decide_reserved",
          "_reserve_turn", "_record_attempt_failure")
    for name in b1:
        node = functions[name]
        identifiers = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        assert not identifiers & {"Session", "SessionLocal", "bridge", "ad", "ii", "dcr", "dp", "create_engine",
                                  "engine", "anthropic", "requests", "httpx", "fetch_financial_data"}, name
        called = {n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not called & {"commit", "rollback", "connect", "begin", "close", "invalidate", "exec_driver_sql"}, name
        assert ("begin_nested" in called) == (name in ("_register_conversation", "_insert_reservation")), name
    statements = [v for k, v in vars(_adapters).items() if k.startswith(("_CONVERSATION", "_REGISTER", "_LOCK_RESERV",
                                                                          "_LEGACY", "_LINKED", "_INSERT_RESERV",
                                                                          "_GRANT", "_BLOCK", "_RELEASE"))]
    flat = " ".join(s if isinstance(s, str) else " ".join(s.values()) for s in statements)
    for table in ("analysis_sessions", "cognitive_events", "support_traces", "decryptage_cognitive_links",
                  "user_statements", "analysis_facts", "investment_theses", "portfolio_"):
        assert table not in flat, table
    assert "INSERT INTO assistant_deliveries" not in flat and "UPDATE assistant_deliveries" not in flat
    assert "assistant_delivery_id =" not in flat.replace("assistant_delivery_id = NULL", "")  # aucun lien écrit


def test_identity_constraint_names_are_shared_with_the_historical_service_and_the_migration():
    import importlib.util
    path = REPO_ROOT / "alembic" / "versions" / "0021_decryptage_turn_reservations.py"
    spec = importlib.util.spec_from_file_location("m0021", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert _adapters.CONVERSATION_IDENTITY_CONSTRAINTS == ii.IDENTITY_COLLISION_CONSTRAINTS == (
        "conversation_identities_pkey", m.OWNER_UNIQUE)
    assert _adapters.RESERVATION_TURN_CONSTRAINT == m.TURN_UNIQUE
    assert _adapters.UNIQUE_VIOLATION == ii.UNIQUE_VIOLATION == "23505"


class _Diag:
    def __init__(self, sqlstate, constraint_name):
        self.sqlstate, self.constraint_name = sqlstate, constraint_name


class _Orig(Exception):
    def __init__(self, sqlstate, constraint_name):
        super().__init__("x")
        self.diag = _Diag(sqlstate, constraint_name)


@pytest.mark.parametrize("sqlstate,name,expected", [
    ("23505", "conversation_identities_pkey", True), ("23505", "uq_conversation_identities_key_owner", True),
    ("23505", "uq_conversation_identities_key_owner_x", False), ("23505", "uq_other", False),
    ("23503", "conversation_identities_pkey", False), ("23503", "conversation_identities_user_id_fkey", False),
    ("23514", "uq_conversation_identities_key_owner", False), (None, "conversation_identities_pkey", False),
    ("23505", None, False)])
def test_the_historical_collision_predicate_is_closed(sqlstate, name, expected):
    exc = sa.exc.IntegrityError("INSERT", {}, _Orig(sqlstate, name))
    assert ii._is_identity_collision(exc) is expected
    assert (_adapters._sqlstate_and_constraint(exc)[0] == "23505"
            and _adapters._sqlstate_and_constraint(exc)[1] in _adapters.CONVERSATION_IDENTITY_CONSTRAINTS) is expected


def test_the_historical_conversation_service_keeps_its_interface():
    assert list(inspect.signature(ii.register_or_resolve_conversation).parameters) == [
        "db", "user_id", "conversation_key"]
    public = {n for n in vars(ii) if not n.startswith("_") and callable(getattr(ii, n))
              and getattr(getattr(ii, n), "__module__", None) == ii.__name__}
    assert public == {"register_or_resolve_conversation", "InteractionIdentityError", "InvalidConversationIdentity",
                      "ConversationOwnershipConflict"}
    source = inspect.getsource(ii.register_or_resolve_conversation)
    for kept in ("with db.begin_nested():", "if not _is_identity_collision(exc):", "winner = _resolve(",
                 "if winner is None:"):
        assert kept in source
    assert "commit(" not in source and "rollback(" not in source
    assert "populate_existing=True" in inspect.getsource(ii._resolve)


def test_no_application_module_invokes_the_b1_commands():
    markers = ("ReserveDecryptageTurn", "RecordDecryptageAttemptFailure", "DecryptageKeyring", "LeaseGranted",
               "decryptage_turn_reservations")
    allowed = {REPO_ROOT / "core" / "authz_f2", REPO_ROOT / "tests", REPO_ROOT / "docs", REPO_ROOT / "alembic"}
    for path in list(REPO_ROOT.rglob("*.py")) + list(REPO_ROOT.rglob("*.html")) + list(REPO_ROOT.rglob("*.js")):
        if any(a in path.parents for a in allowed) or "node_modules" in path.parts or ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits = [m for m in markers if m in text]
        if path == REPO_ROOT / "core" / "models.py":
            assert hits == ["decryptage_turn_reservations"], hits  # modèle de la table, seul
            continue
        assert hits == [], (path, hits)


def test_failure_classes_and_reasons_are_closed_vocabularies():
    assert dr.FAILURE_CLASSES == ("market_data_unavailable", "generation_unavailable", "generation_unusable")
    assert not hasattr(c, "ForceBlockReservation") and not hasattr(c, "ResetReservation")
    assert {n for n in dir(c) if "Decryptage" in n} == {"ReserveDecryptageTurn", "RecordDecryptageAttemptFailure"}
