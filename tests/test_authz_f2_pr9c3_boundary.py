"""R1 PR 9-C3 — composition, frontières statiques et dormance de
l'orchestration HTTP F2 Décrypter (sans base de données).

1. composition : sans configuration C3, application et runner historiques
   inchangés ; configuration complète : exactement trois routes, UN runner,
   orchestrateur B2 construit avec CE runner ; configuration partielle,
   trousseau manquant, clés partagées, transports ou modèle invalides refusés ;
   rien n'est lu, ouvert ni appelé à l'import ou à la construction ;
2. contrats purs : machine d'état d'une place, horodatage RFC 3339, enveloppe
   canonique, matrices fermées, constantes figées ;
3. frontières statiques : imports fermés, aucune transaction, aucun SQL,
   aucune capacité privée, ACK jamais appelé par /turns ni /replay, aucune
   déclaration B1 de substitution ;
4. dormance : api:app sans route C3 quelles que soient les variables, aucun
   module C3 chargé, aucun frontend modifié, aucune migration, vingt commandes.
"""
import ast
import dataclasses
import json
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from core.auth_f2.service import SessionCredential
from core.auth_f2.crypto import Redacted
from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_generation as dg
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.decryptage_fingerprint import DecryptageKeyring
from core.authz_f2.runner import UNAVAILABLE, AuthorizedTransactionRunner
from core.authz_f2_http import decryptage as c3
from core.authz_f2_http.isolated import PersonalHttpConfigError, build_isolated_personal_app, make_personal_app
from core.authz_f2_http.routes import PersonalDependencies
from tests.authz_f2_pr9b1_support import D1, D2
from tests.authz_f2_pr9b2_support import MODEL, FakeClaude, FakeClock, FakeEodhd, FakeFmp
from tests.authz_f2_pr9c3_support import C3_PATHS, Recorder
from tests.authz_f2_support import K1, K2, portfolio_keyring
from tests.f2_http_support import ORIGIN, csrf_keyring
from tests.f2_support import make_keyring
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

C3_MODULE = REPO_ROOT / "core" / "authz_f2_http" / "decryptage.py"
ISOLATED = REPO_ROOT / "core" / "authz_f2_http" / "isolated.py"
HISTORICAL_ROUTES = [
    ("GET", "/api/v2/me"), ("PUT", "/api/v2/me/level"), ("GET", "/api/v2/me/portfolio"),
    ("GET", "/api/v2/me/portfolio/{position_id}"), ("POST", "/api/v2/me/portfolio"),
    ("PUT", "/api/v2/me/portfolio/{position_id}"), ("DELETE", "/api/v2/me/portfolio/{position_id}"),
    ("GET", "/api/v2/me/analysis-overview"), ("GET", "/api/v2/me/theses"), ("GET", "/api/v2/me/theses/{thesis_id}"),
]


def _engine():
    # Aucune connexion n'est ouverte par la construction (vérifié ci-dessous).
    return sa.create_engine("postgresql+psycopg2://u:p@127.0.0.1:1/oryx_never_test", poolclass=sa.pool.NullPool)


def _transports(clock=None):
    clock = clock or FakeClock()
    return clock, FakeEodhd(clock), FakeFmp(clock), FakeClaude(clock)


def _config(**overrides):
    clock, eodhd, fmp, claude = _transports()
    fields = dict(decryptage_keyring=DecryptageKeyring({"d1": D1}, "d1"), eodhd=eodhd, fmp=fmp, claude=claude,
                  model=MODEL, monotonic=clock)
    fields.update(overrides)
    return c3.DecryptageHttpConfig(**fields)


def _app(decryptage=None, **overrides):
    kwargs = dict(engine=_engine(), keyring=make_keyring(), allowed_origins=[ORIGIN], csrf_keyring=csrf_keyring(),
                  portfolio_keyring=portfolio_keyring())
    kwargs.update(overrides)
    return build_isolated_personal_app(decryptage=decryptage, **kwargs)


def _routes(app):
    return sorted((m, r.path) for r in app.routes for m in getattr(r, "methods", ()) if m != "HEAD")


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _functions(path):
    return {n.name: n for n in ast.walk(_tree(path)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


# --------------------------------------------------------------------------
# 1. Composition
# --------------------------------------------------------------------------

def test_without_c3_the_personal_application_is_the_historical_one(monkeypatch):
    runners = Recorder(monkeypatch, AuthorizedTransactionRunner)
    app = _app()
    assert _routes(app) == sorted(HISTORICAL_ROUTES)
    [(args, kwargs)] = runners.calls
    assert "decryptage_keyring" not in kwargs  # runner historique : aucun trousseau Décrypter
    deps = PersonalDependencies(None, frozenset({ORIGIN}))
    assert _routes(make_personal_app(deps)) == sorted(HISTORICAL_ROUTES)  # make_personal_app : jamais de C3
    # Sans trousseau, toute commande Décrypter est refusée AVANT toute transaction.
    command = c.ReserveDecryptageTurn("conv", uuid.uuid4(), "MC.PA", "", "", None)
    outcome = runners.instances[0].run(SessionCredential(uuid.uuid4(), Redacted("A" * 43)), command)
    assert outcome.status == UNAVAILABLE


def test_a_complete_configuration_adds_exactly_the_three_c3_routes(monkeypatch):
    runners = Recorder(monkeypatch, AuthorizedTransactionRunner)
    generators = Recorder(monkeypatch, dg.DecryptageGenerationOrchestrator)
    capacities = Recorder(monkeypatch, c3._TurnCapacity)
    config = _config()
    app = _app(config)
    added = sorted(set(_routes(app)) - set(HISTORICAL_ROUTES))
    assert added == sorted(("POST", "/api/v2" + p) for p in (c3.TURNS, c3.REPLAY, c3.ACK))
    assert [p for _, p in added] == sorted(C3_PATHS)
    assert set(HISTORICAL_ROUTES) <= set(_routes(app))
    [runner] = runners.instances  # runner UNIQUE de la composition
    assert runners.calls[0][1]["decryptage_keyring"] is config.decryptage_keyring
    [generator] = generators.instances
    (args, kwargs) = generators.calls[0]
    assert args[0] is runner  # orchestrateur B2 construit avec CE runner
    assert (kwargs["eodhd"], kwargs["fmp"], kwargs["claude"], kwargs["model"], kwargs["clock"]) == (
        config.eodhd, config.fmp, config.claude, MODEL, config.monotonic)
    assert kwargs["price_recent_max_age"] is None  # comportement B2 inchangé par défaut
    [capacity] = capacities.instances
    assert (capacity.size, capacity.in_use, capacity.released) == (2, 0, 0)
    assert app.openapi_url is None and app.docs_url is None


def test_the_router_never_accepts_an_external_orchestrator_or_runner():
    import inspect
    assert list(inspect.signature(c3.build_decryptage_router).parameters) == ["deps", "config"]
    config = _config()
    for runner in (None, object()):
        with pytest.raises(TypeError):
            c3.build_decryptage_router(PersonalDependencies(runner, frozenset({ORIGIN})), config)
    runner = AuthorizedTransactionRunner(_engine(), make_keyring(), decryptage_keyring=config.decryptage_keyring)
    with pytest.raises(TypeError):
        c3.build_decryptage_router(PersonalDependencies(runner, frozenset({ORIGIN})), {"model": MODEL})
    assert list(inspect.signature(build_isolated_personal_app).parameters)[-1] == "decryptage"


@pytest.mark.parametrize("value", [object(), {"decryptage_keyring": None}, "c3", True])
def test_a_foreign_configuration_object_is_refused(value):
    with pytest.raises(PersonalHttpConfigError):
        _app(value)


@pytest.mark.parametrize("missing", ["decryptage_keyring", "eodhd", "fmp", "claude", "model"])
def test_a_partial_configuration_is_refused(missing):
    clock, eodhd, fmp, claude = _transports()
    fields = dict(decryptage_keyring=DecryptageKeyring({"d1": D1}, "d1"), eodhd=eodhd, fmp=fmp, claude=claude,
                  model=MODEL, monotonic=clock)
    fields.pop(missing)
    with pytest.raises(TypeError):
        c3.DecryptageHttpConfig(**fields)
    with pytest.raises((TypeError, ValueError)):
        c3.DecryptageHttpConfig(**{**fields, missing: None})


class _Partial:
    def search(self, query, *, timeout_s):
        return []


@pytest.mark.parametrize("field, value", [
    ("decryptage_keyring", None), ("decryptage_keyring", {"d1": D1}), ("decryptage_keyring", portfolio_keyring()),
    ("eodhd", _Partial()), ("fmp", object()), ("claude", FakeEodhd(FakeClock())), ("eodhd", None),
    ("model", ""), ("model", " claude"), ("model", "claude model"), ("model", "x" * 129), ("model", None),
    ("model", b"claude"), ("model", "modèle"), ("monotonic", None), ("monotonic", lambda: float("nan")),
    ("monotonic", lambda: "1"), ("price_recent_max_age", 60), ("price_recent_max_age", timedelta(0)),
], ids=lambda v: repr(v)[:24])
def test_an_invalid_configuration_piece_is_refused(field, value):
    with pytest.raises((TypeError, ValueError)):
        _config(**{field: value})


def test_the_configuration_is_closed_and_secret_free():
    config = _config()
    assert repr(config) == str(config) == "DecryptageHttpConfig(<private>)"
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.model = "autre"
    with pytest.raises(TypeError):
        __import__("pickle").dumps(config)
    assert [f.name for f in dataclasses.fields(c3.DecryptageHttpConfig)] == [
        "decryptage_keyring", "eodhd", "fmp", "claude", "model", "monotonic", "price_recent_max_age"]
    assert c3.valid_model("claude-opus-5-5") and c3.valid_model("us.anthropic.claude-x:0")
    assert _config(price_recent_max_age=timedelta(minutes=20)).price_recent_max_age == timedelta(minutes=20)


@pytest.mark.parametrize("shared", ["f2", "csrf", "portfolio"])
def test_keys_shared_between_families_are_refused(shared):
    f2 = make_keyring()
    key = {"f2": lambda: f2.key("active", f2.versions("active")[0]), "csrf": lambda: bytes([0xC0]) * 32,
           "portfolio": lambda: K1}[shared]()
    with pytest.raises(PersonalHttpConfigError):
        _app(_config(decryptage_keyring=DecryptageKeyring({"d1": D1, "d9": key}, "d1")), keyring=f2)
    _app(_config(decryptage_keyring=DecryptageKeyring({"d1": D1, "d2": D2}, "d1")), keyring=f2)  # distinctes : admis
    assert K2 != D1


def test_historical_composition_checks_still_apply_with_c3():
    with pytest.raises(PersonalHttpConfigError):
        _app(_config(), allowed_origins=["http://oryx.test"])
    with pytest.raises(PersonalHttpConfigError):
        _app(_config(), csrf_keyring=object())
    with pytest.raises(PersonalHttpConfigError):
        _app(_config(), engine=sa.create_engine("sqlite://"))
    serializable = sa.create_engine("postgresql+psycopg2://u:p@127.0.0.1:1/oryx_never_test",
                                    isolation_level="SERIALIZABLE")
    with pytest.raises(PersonalHttpConfigError):
        _app(_config(), engine=serializable)


CONSTRUCTION_PROBE = r'''
import json, socket, sys
attempts = []
socket.socket.connect = lambda self, address: attempts.append(repr(address))
socket.create_connection = lambda *a, **k: attempts.append("create_connection")
import psycopg2
psycopg2.connect = lambda *a, **k: attempts.append("psycopg2")
import core.authz_f2_http.decryptage as c3
import core.authz_f2_http.isolated as isolated
imported = sorted(m for m in ("api", "anthropic", "requests", "httpx2", "yfinance", "core.decryptage_providers")
                  if m in sys.modules)
import sqlalchemy as sa
from core.authz_f2.decryptage_fingerprint import DecryptageKeyring
from core.auth_f2.keys import Keyring
class T:
    def search(self, q, *, timeout_s): raise AssertionError("réseau")
    fundamentals = realtime = profile = ratios = search
    def create_message(self, **k): raise AssertionError("réseau")
from tests.f2_support import make_keyring
from tests.f2_http_support import csrf_keyring
config = c3.DecryptageHttpConfig(DecryptageKeyring({"d1": bytes(range(100, 132))}, "d1"), T(), T(), T(), "m")
app = isolated.build_isolated_personal_app(
    engine=sa.create_engine("postgresql+psycopg2://u:p@127.0.0.1:1/oryx_never_test"), keyring=make_keyring(),
    allowed_origins=["https://oryx.test"], csrf_keyring=csrf_keyring(), decryptage=config)
print(json.dumps({"attempts": attempts, "imported": imported, "threads": __import__("threading").active_count()}))
'''


@pytest.mark.parametrize("flags", [{}, {"ANTHROPIC_API_KEY": "sk-x", "EOD_API_KEY": "e", "FMP_API_KEY": "f",
                                        "ORYX_DECRYPTAGE_F2": "1", "ORYX_PR9C3_ENABLED": "1",
                                        "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/oryx_never_test"}])
def test_importing_and_composing_c3_reads_nothing_opens_nothing_and_calls_nothing(flags):
    out = subprocess.run([sys.executable, "-c", CONSTRUCTION_PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env={**_clean_env({}), **flags}, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result == {"attempts": [], "imported": [], "threads": 1}  # aucun fil démarré à la construction


# --------------------------------------------------------------------------
# 2. Contrats purs
# --------------------------------------------------------------------------

def test_a_place_is_released_exactly_once_whatever_the_order():
    capacity = c3._TurnCapacity(2)
    a, b = capacity.try_acquire(), capacity.try_acquire()
    assert capacity.try_acquire() is None and capacity.in_use == 2
    # Démarrage puis abandon (annulation après démarrage) : le fil possède la place.
    assert a.start() is True and a.abandon() is False and capacity.in_use == 2
    assert a.start() is False  # jamais deux démarrages
    a.finish()
    a.finish()
    assert (a.state, capacity.in_use, capacity.released) == (c3.FINISHED, 1, 1)
    # Abandon avant démarrage : la requête rend la place ; un démarrage tardif ne fait rien.
    assert b.abandon() is True and b.abandon() is False and b.start() is False
    b.finish()
    assert (b.state, capacity.in_use, capacity.released) == (c3.ABANDONED, 0, 2)
    assert capacity.try_acquire() is not None
    with pytest.raises(ValueError):
        c3._TurnCapacity(0)


def test_racing_start_and_abandon_never_leak_nor_double_release():
    capacity = c3._TurnCapacity(c3.MAX_CONCURRENT_TURNS)
    outcomes = []
    lock = threading.Lock()

    def race():
        for _ in range(2000):
            slot = capacity.try_acquire()
            if slot is None:
                continue
            barrier = threading.Barrier(2)
            results = {}

            def worker():
                barrier.wait()
                results["started"] = slot.start()
                if results["started"]:
                    slot.finish()

            thread = threading.Thread(target=worker)
            thread.start()
            barrier.wait()
            results["abandoned"] = slot.abandon()
            thread.join()
            with lock:
                outcomes.append((results["started"], results["abandoned"]))

    threads = [threading.Thread(target=race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes and all(started != abandoned for started, abandoned in outcomes)  # exactement un propriétaire
    assert capacity.in_use == 0 and capacity.released == len(outcomes)


def test_rfc3339_is_utc_with_six_microsecond_digits_and_z():
    paris = timezone(timedelta(hours=1))
    assert c3.rfc3339_utc(datetime(2026, 11, 7, 12, 30, tzinfo=paris)) == "2026-11-07T11:30:00.000000Z"
    assert c3.rfc3339_utc(datetime(2026, 11, 7, 11, 32, 0, 120, tzinfo=timezone.utc)) == \
        "2026-11-07T11:32:00.000120Z"
    assert c3.rfc3339_utc(datetime(1, 1, 1, tzinfo=timezone.utc)) == "0001-01-01T00:00:00.000000Z"
    for value in (datetime(2026, 1, 1), "2026-01-01T00:00:00Z", None):
        with pytest.raises(ValueError):
            c3.rfc3339_utc(value)


def _view(payload: bytes, status="pending"):
    return c.CanonicalDecryptageDelivery(uuid.UUID(int=1), uuid.UUID(int=2), status, payload)


def test_the_canonical_envelope_inserts_the_jsonb_bytes_untouched():
    payload = '{"price": 600.50, "name": "Société \\"Générale\\" ✓", "analysis": "\\u2028é"}'.encode("utf-8")
    body = c3.canonical_body(_view(payload))
    assert body == (b'{"assistant_turn_id":"00000000-0000-0000-0000-000000000002","delivery_status":"pending",'
                    b'"payload":' + payload + b',"reservation_id":"00000000-0000-0000-0000-000000000001"}')
    parsed = json.loads(body, parse_float=Decimal)
    assert parsed["payload"]["price"] == Decimal("600.50") and str(parsed["payload"]["price"]) == "600.50"
    for bad in (b'["x"]', b'{"a": 1', b'{"a": NaN}', b'{"a": 1, "a": 2}', b'{"a": 1}, "x": {}', b"\xff",
                b"1", b'"text"', b'{"a":1}  ,"reservation_id":"x"'):
        with pytest.raises(ValueError):
            c3.canonical_body(_view(bad))


def test_the_ack_view_is_checked_structurally_and_exactly():
    from core.authz_f2 import _decryptage_orm_bridge as bridge  # tests seulement : la couche HTTP ne le nomme pas
    tid = uuid.uuid4()
    assert c3.acknowledged(bridge.DeliveryAcknowledged(tid, "delivered", "opened_event"), tid)
    assert c3.acknowledged(bridge.DeliveryAcknowledged(tid, "delivered", None), tid)
    assert not c3.acknowledged(bridge.DeliveryAcknowledged(tid, "delivered", None), uuid.uuid4())
    Shape = dataclasses.make_dataclass("DeliveryAcknowledged", [("assistant_turn_id", uuid.UUID),
                                                                 ("delivery_status", str)], frozen=True)
    Mutable = dataclasses.make_dataclass("DeliveryAcknowledged", [("assistant_turn_id", uuid.UUID),
                                                                   ("delivery_status", str), ("response_action", str)])
    for forged in (Shape(tid, "delivered"), Mutable(tid, "delivered", None), object(), None, {"assistant_turn_id": tid}):
        assert not c3.acknowledged(forged, tid)
    assert [f.name for f in dataclasses.fields(bridge.DeliveryAcknowledged)] == list(c3._ACK_FIELDS)


def test_the_closed_matrices_cover_exactly_the_frozen_vocabularies():
    assert set(c3._B2_REFUSALS) == set(dg.REFUSAL_REASONS)
    assert c3.B2_ALERT_REASONS == {dg.OWNER_CHANGED, dg.LEASE_NOT_GRANTED, dg.LEASE_MISMATCH, dg.LEASE_ALREADY_USED}
    assert set(c3._PROVIDER_REPLIES) == set(dg.DECLARATIONS)
    assert (c3._PROVIDER_REPLIES[dg.DECLARED].status, c3._PROVIDER_REPLIES[dg.DECLARED].code) == (
        503, "provider_unavailable")
    for reason in dc.REFUSAL_REASONS + (dg.CONTEXT_CHANGED,):
        assert (c3._B2_REFUSALS[reason].status, c3._B2_REFUSALS[reason].code) == (409, "context_conflict")
    # ACT-L1-PR1 : + identity_link_required dans chaque matrice du runner (403).
    assert set(c3._C2_REFUSALS) == {"session_invalid", "not_found", "revision_conflict", "unavailable",
                                    "identity_link_required"}
    assert set(c3._B1_REFUSALS) == set(c3._C1B_REFUSALS) == {"session_invalid", "not_found",
                                                            "idempotency_conflict", "revision_conflict",
                                                            "unavailable", "identity_link_required"}
    for matrix, key in ((c3._B1_REFUSALS, "identity_link_required"), (c3._C2_REFUSALS, "identity_link_required"),
                        (c3._B2_REFUSALS, dg.IDENTITY_REQUIRED)):
        assert (matrix[key].status, matrix[key].code) == (403, "identity_link_required")
    codes = {r.code for m in (c3._B1_REFUSALS, c3._C2_REFUSALS, c3._B2_REFUSALS, c3._PROVIDER_REPLIES)
             for r in m.values()}
    assert codes == {"session_invalid", "not_found", "idempotency_conflict", "revision_conflict",
                     "temporarily_unavailable", "context_conflict", "instrument_unavailable", "provider_unavailable",
                     "identity_link_required"}


def test_frozen_constants():
    assert (c3.C3_BUDGET_SECONDS, c3.MIN_BEFORE_GENERATION_SECONDS, c3.MIN_BEFORE_FINALIZATION_SECONDS,
            c3.MIN_AFTER_FINALIZATION_SECONDS) == (270.0, 255.0, 15.0, 5.0)
    assert c3.MAX_CONCURRENT_TURNS == 2
    assert (c3.TURN_MAX_BODY_BYTES, c3.IDENTITY_MAX_BODY_BYTES) == (384 * 1024, 4096)
    assert c3.TURN_KEYS == {"conversation_key", "client_turn_id", "ticker_input", "question", "context",
                            "last_method_id"}
    assert c3.IDENTITY_KEYS == {"reservation_id", "conversation_key", "assistant_turn_id"}
    # B1 / B2 inchangés.
    assert dr.LEASE_DURATION == timedelta(seconds=360) and dr.MAX_ATTEMPTS == 3
    assert dr.BACKOFFS == {1: timedelta(seconds=30), 2: timedelta(seconds=120)}
    assert dg.TOTAL_BUDGET_SECONDS == 240.0


# --------------------------------------------------------------------------
# 3. Frontières statiques
# --------------------------------------------------------------------------

def test_c3_imports_are_closed():
    imported = set()
    for node in ast.walk(_tree(C3_MODULE)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    core = {m for m in imported if m.startswith("core")}
    assert core == {"core.auth_f2.crypto", "core.auth_f2.service", "core.auth_f2_http.cookies",
                    "core.auth_f2_http.provenance", "core.auth_f2_http.routes", "core.authz_f2",
                    "core.authz_f2.decryptage_candidate", "core.authz_f2.decryptage_fingerprint",
                    "core.authz_f2.principal", "core.authz_f2.runner", "core.authz_f2_http.provenance",
                    "core.authz_f2_http.routes"}
    tokens = set(_code_tokens(C3_MODULE.read_text(encoding="utf-8")).split("\n"))
    for forbidden in ("_adapters", "_decryptage_orm_bridge", "_bridge_contracts", "decryptage_providers", "db",
                      "models", "Session", "sessionmaker", "create_engine", "environ", "getenv", "os",
                      "anthropic", "requests", "httpx", "socket", "transaction", "SessionLocal", "TransactionProbe",
                      "AckAdmissionPort", "FinalizationReadPort", "DECRYPTAGE_MUTATION", "REGISTRY"):
        assert forbidden not in tokens, forbidden


def test_c3_never_controls_a_transaction_nor_executes_sql():
    tree = _tree(C3_MODULE)
    attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not attrs & {"commit", "rollback", "begin", "begin_nested", "connect", "close", "execute", "text",
                        "exec_driver_sql", "execution_options", "raw_connection", "get_transaction", "connection"}
    runs = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(".run")]
    assert runs and all(r.startswith("self._runner.run(credential, ") for r in runs)  # UNIQUE voie : le runner


def test_turns_never_acknowledges_and_never_declares_in_place_of_b2():
    funcs = _functions(C3_MODULE)
    for name in ("turn", "_generate", "_finalize", "_replay", "_read", "replay", "turns"):
        tokens = set(_code_tokens(ast.unparse(funcs[name])).split("\n"))
        assert "AcknowledgeDecryptageDelivery" not in tokens and "acknowledge" not in tokens, name
    users = {f for f, node in funcs.items() if "AcknowledgeDecryptageDelivery" in ast.unparse(node)}
    assert users == {"ack", "build_decryptage_router"}  # la seule route /ack (imbriquée dans le constructeur)
    finalize = {f for f, node in funcs.items() if "FinalizeDecryptageCandidate" in ast.unparse(node)}
    assert finalize == {"_finalize"}
    reserve = {f for f, node in funcs.items() if "ReserveDecryptageTurn" in ast.unparse(node)}
    assert reserve == {"turn_command"}
    source = C3_MODULE.read_text(encoding="utf-8")
    assert "RecordDecryptageAttemptFailure" not in source  # jamais de déclaration de substitution
    assert "generated.candidate.payload" not in source and ".as_dict(" not in source  # jamais le candidat
    assert "dumps" not in ast.unparse(funcs["canonical_body"])  # payload jamais resérialisé


def test_the_turn_handler_acquires_before_b1_and_the_worker_owns_the_place():
    turns = ast.unparse(_functions(C3_MODULE)["turns"])
    order = ["await admit(", "capacity.try_acquire()", "capacity_unavailable", "started = _read_clock(",
             "def work():", "if not slot.start():", "flow.turn(", "slot.finish()", "await _submit(work)",
             "slot.abandon()"]
    positions = [turns.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert turns.count("slot.finish()") == 1 and turns.count("slot.abandon()") == 1
    work = next(n for n in ast.walk(_functions(C3_MODULE)["turns"]) if isinstance(n, ast.FunctionDef)
                and n.name == "work")
    [guarded] = [n for n in work.body if isinstance(n, ast.Try)]
    assert [ast.unparse(s) for s in guarded.finalbody] == ["slot.finish()"]  # le fil rend SA place
    outer = [n for n in _functions(C3_MODULE)["turns"].body if isinstance(n, ast.Try) and n.finalbody]
    assert [ast.unparse(s) for s in outer[-1].finalbody] == ["slot.abandon()"]  # la requête : seulement sans démarrage


def test_the_admission_order_is_the_personal_mutation_order():
    admit = ast.unparse(_functions(C3_MODULE)["admit"])
    order = ["personal_mutation_origin(", "parse_cookie_headers(", "_check_csrf(", "_generation_id(",
             "query_string", "_check_content_type(", "_announced_length(request, limit=limit)",
             "_read_body(request, announced, limit=limit)", "cookies.actives.get(generation_id)", "SessionCredential("]
    positions = [admit.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_isolated_mentions_c3_only_through_the_closed_configuration():
    tokens = _code_tokens(ISOLATED.read_text(encoding="utf-8")).split("\n")
    decryptage = sorted({t for t in tokens if "ecryptage" in t})
    assert decryptage == ["DecryptageHttpConfig", "build_decryptage_router", "decryptage", "decryptage_keyring",
                          "decryptage_router"]


# --------------------------------------------------------------------------
# 4. Dormance
# --------------------------------------------------------------------------

PROBE = r'''
import asyncio, json, socket, sys
attempts = []
def guard(self, address):
    attempts.append(repr(address))
    raise OSError("réseau interdit")
socket.socket.connect = guard
import api, httpx
GEN = "11111111-2222-4333-8444-555555555555"
COOKIE = "__Host-oryx-active-" + GEN + "=" + "A" * 43 + "; __Host-oryx-csrf-binding=" + "B" * 43
BODY = (b'{"conversation_key":"c","client_turn_id":"11111111-1111-4111-8111-111111111111","ticker_input":"MC.PA",'
        b'"question":"","context":"","last_method_id":null}')
CASES = [("POST", "/api/v2/me/decryptage/turns"), ("POST", "/api/v2/me/decryptage/replay"),
         ("POST", "/api/v2/me/decryptage/ack"), ("GET", "/api/v2/me/decryptage/turns"),
         ("POST", "/api/v2/me/decryptage"), ("POST", "/api/v2/me/decryptage/turns?x=1")]
async def probe():
    transport = httpx.ASGITransport(app=api.app, client=("192.0.2.1", 1))
    out = []
    async with httpx.AsyncClient(transport=transport, base_url="https://oryx.test") as client:
        for method, path in CASES:
            r = await client.request(method, path, headers={"origin": "https://oryx.test", "cookie": COOKIE,
                                     "x-oryx-generation-id": GEN, "x-oryx-csrf": "c1.1.AAAA.x",
                                     "content-type": "application/json"}, content=BODY if method == "POST" else None)
            out.append([method, path, r.status_code, r.headers.get_list("set-cookie")])
    return out
responses = asyncio.run(probe())
loaded = sorted(m for m in sys.modules if "authz" in m or "auth_f2" in m or m.startswith("core.authz_f2")
                or "decryptage_generation" in m or "decryptage_providers" in m)
routes = sorted({(m.lower(), r.path) for r in api.app.routes for m in getattr(r, "methods", ()) if m != "HEAD"})
print(json.dumps({"responses": responses, "loaded": loaded, "attempts": attempts, "routes": routes}))
'''


@pytest.mark.parametrize("flags", [{}, {"ORYX_DECRYPTAGE_F2": "1", "ORYX_PR9C3_ENABLED": "1", "ORYX_F2_ENABLED": "1",
                                        "ORYX_DECRYPTAGE_HTTP": "true", "ORYX_V2_DECRYPTAGE": "on",
                                        "ORYX_DECRYPTAGE_KEYS": json.dumps({"d1": "A" * 43}),
                                        "ANTHROPIC_API_KEY": "sk-x", "EOD_API_KEY": "e", "FMP_API_KEY": "f"}])
def test_the_real_entrypoint_has_no_c3_route_whatever_the_environment(flags):
    from tests.test_f2_dormancy import MAIN_ROUTES
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env(flags), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert [r[2] for r in result["responses"]] == [404] * 6
    assert all(r[3] == [] for r in result["responses"])
    assert result["loaded"] == [] and result["attempts"] == []
    assert [tuple(r) for r in result["routes"] if not r[1].startswith(("/docs", "/openapi", "/redoc"))] == sorted(
        set(MAIN_ROUTES))


def test_no_production_file_mentions_c3():
    markers = ("/api/v2/me/decryptage", "DecryptageHttpConfig", "build_decryptage_router", "authz_f2_http.decryptage",
               "capacity_unavailable", "provider_unavailable", "instrument_unavailable", "reservation_blocked")
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if (not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts or "node_modules" in path.parts
                or path.suffix not in (".py", ".js", ".html", ".ts", ".json", ".sh", ".yml", ".yaml", ".toml",
                                       ".ini", ".cfg", ".txt")):
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in ("tests", "docs") or path in (C3_MODULE, ISOLATED):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders += [(str(relative), m) for m in markers if m in text]
    assert offenders == []
    for path in (REPO_ROOT / "api.py", REPO_ROOT / "web-v2" / "index.html"):
        text = path.read_text(encoding="utf-8")
        assert "decryptage/turns" not in text and "authz_f2" not in text and "/api/v2/" not in text, path


def test_no_new_command_plan_operation_or_migration():
    from tests.test_authz_f2_pr9c2_boundary import test_twenty_commands_three_operations_three_contracts
    test_twenty_commands_three_operations_three_contracts()
    assert len(_adapters.REGISTRY) == 20
    assert c.PLANS == (c.READ, c.PROFILE_MUTATION, c.PORTFOLIO_MUTATION, c.DECRYPTAGE_MUTATION)
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(versions) == 21 and versions[-1] == "0021_decryptage_turn_reservations.py"
