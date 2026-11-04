"""Contrôles statiques, unitaires et de dormance de R1 PR 9-C1-B, sans base
de données.

1. inventaires fermés : 18 commandes, 3 opérations, capacités exactes ;
   commande et opération ne portent que le candidat ;
2. frontière SQL / ORM : aucun SQL textuel hors des adaptateurs, une seule
   Session, aucun cycle d'import (deux ordres Python), contrats communs
   purs, capacités scellées, aucun second runner, aucun import dynamique ;
3. protocole : ordre exact des phases dans le pont et dans les adaptateurs,
   UPDATE B1 seulement après le retour du pont, seules exceptions métier
   exactes converties, aucun `except Exception` qui fabriquerait un
   résultat ; graphe d'appels fermé de la troisième opération ;
4. sélecteur de méthode partagé (B2 et C1-B) et projection financière ;
5. dormance : aucune route, api:app intact, aucune migration, aucun secret
   dans les représentations.
"""
import ast
import inspect
import json
import subprocess
import sys

import pytest

from core import decryptage_progress as dp
from core.authz_f2 import _adapters
from core.authz_f2 import _bridge_contracts as contracts
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2 import decryptage_finalization as dfin
from core.authz_f2 import decryptage_market_data as dm
from core.authz_f2 import decryptage_method_selection as dms
from core.authz_f2 import decryptage_reservation as dr
from core.pedagogie_library import lookup_method
from tests.test_authz_f2_pr9a_boundary import BRIDGE, _functions, _reachable, _tree
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

AUTHZ = REPO_ROOT / "core" / "authz_f2"
ADAPTERS = AUTHZ / "_adapters.py"
CONTRACTS = AUTHZ / "_bridge_contracts.py"
SELECTION = AUTHZ / "decryptage_method_selection.py"
FINALIZATION = AUTHZ / "decryptage_finalization.py"

# Graphe d'appels FERMÉ de la troisième opération (fonctions des services
# atteintes depuis son point d'entrée ; Prepare / Apply : REACHABLE_C1A).
REACHABLE_C1B = {
    "_finalize_candidate": {
        "core.assistant_delivery": {
            "claim_delivery", "visible_content_fingerprint", "_delivery_by_identity", "_is_turn_unique_violation",
            "_json_copy", "_json_dict", "_require_existing_user", "_require_fingerprint", "_require_identifier",
            "_require_ordinal", "_require_response_payload", "_require_schema_version", "_require_surface",
            "_require_text", "_require_uuid", "_resolve_conversation", "_same_request_or_collision", "_utcnow",
            "_validate_identity"},
        "core.decryptage_cognitive_runtime": {"runtime_private_metadata"},
        "core.interaction_identity": {"register_or_resolve_conversation", "_require_identifier", "_resolve",
                                      "_is_identity_collision", "_utcnow"},
    },
    "_delivery_request_fingerprint": {
        "core.assistant_delivery": {"decryptage_request_fingerprint", "_require_identifier", "_require_text",
                                    "_require_uuid", "_sha256_canonical_json"},
    },
}


def _source(path, name) -> str:
    return ast.unparse(_functions(path)[name])


def _class_method(path, cls, name):
    (node,) = [n for n in _tree(path).body if isinstance(n, ast.ClassDef) and n.name == cls]
    (method,) = [m for m in node.body if isinstance(m, ast.FunctionDef) and m.name == name]
    return method


def _in_order(source, markers):
    positions, start = [], 0
    for marker in markers:
        start = source.index(marker, start)
        positions.append(start)
    return positions == sorted(positions)


# --------------------------------------------------------------------------
# 1. Inventaires fermés
# --------------------------------------------------------------------------

def test_eighteen_commands_three_operations_exact_capabilities():
    assert len(_adapters.REGISTRY) == 18
    spec = _adapters.REGISTRY[c.FinalizeDecryptageCandidate]
    assert (spec.plan, spec.lock, spec.execute, spec.decryptage_keyed, spec.keyed, spec.states) == (
        c.DECRYPTAGE_MUTATION, _adapters._no_business_lock, _adapters._finalize_candidate, True, False, (type(None),))
    assert c.PLANS == (c.READ, c.PROFILE_MUTATION, c.PORTFOLIO_MUTATION, c.DECRYPTAGE_MUTATION)  # aucun plan nouveau
    assert len(bridge.OPERATIONS) == 3 and set(bridge.OPERATIONS) == set(bridge.CAPABILITIES)
    assert bridge.CAPABILITIES[bridge.CandidateFinalization] is contracts.FinalizationReadPort
    assert not set(bridge.OPERATIONS) & set(_adapters.REGISTRY)


@pytest.mark.parametrize("cls", [c.FinalizeDecryptageCandidate, bridge.CandidateFinalization])
def test_the_command_and_the_operation_carry_only_the_candidate(cls):
    import dataclasses
    assert [f.name for f in dataclasses.fields(cls)] == ["candidate"]
    assert cls.__dataclass_params__.frozen
    for value in (None, object(), {"candidate": 1}, "x", print):
        with pytest.raises(ValueError):
            cls(value)


def test_a_subclass_of_the_candidate_is_refused():
    sub = type("Sub", (dcand.GeneratedDecryptageCandidate,), {"__slots__": ()})
    fake = object.__new__(sub)
    with pytest.raises(ValueError):
        c.FinalizeDecryptageCandidate(fake)


def test_finalization_views_are_closed_and_secret_free():
    for view in dfin.FINALIZE_RESULTS:
        import dataclasses
        fields = {f.name for f in dataclasses.fields(view)}
        assert fields <= {"reservation_id", "assistant_delivery_id", "reason", "blocked_reason"}, view
        assert view.__dataclass_params__.frozen
    assert dfin.FINALIZE_RESULTS[-1] is dr.ReservationBlocked


# --------------------------------------------------------------------------
# 2. Frontière SQL / ORM et imports
# --------------------------------------------------------------------------

def test_no_textual_sql_outside_the_sql_adapters():
    for path in sorted(AUTHZ.glob("*.py")):
        tree = _tree(path)
        texts = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and ((isinstance(n.func, ast.Attribute) and n.func.attr == "text")
                      or (isinstance(n.func, ast.Name) and n.func.id == "text"))]
        if path == ADAPTERS:
            continue
        assert texts == [], path.name
    # La projection B2 n'est exécutée qu'en UN endroit (lecture B2 et port C1-B).
    users = [f.name for f in ast.walk(_tree(ADAPTERS)) if isinstance(f, ast.FunctionDef)
             and "_PREPARATION_CONTEXT" in {n.id for n in ast.walk(f) if isinstance(n, ast.Name)}]
    assert users == ["_preparation_outcome"]


def test_contracts_module_is_pure():
    tree = _tree(CONTRACTS)
    assert [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))] == []
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"execute", "text", "Session", "Engine", "connect", "environ", "getenv", "socket"}
    # Mêmes objets réexportés : aucune seconde exception ni sentinelle.
    assert _adapters.AdapterInvariantBroken is contracts.AdapterInvariantBroken
    assert (_adapters.ABSENT, _adapters.CONFLICT, _adapters.IDEMPOTENCY_CONFLICT) == (
        contracts.ABSENT, contracts.CONFLICT, contracts.IDEMPOTENCY_CONFLICT)
    assert _adapters.ABSENT is contracts.ABSENT and bridge.TransactionLost is contracts.TransactionLost
    classes = [n.name for n in ast.walk(_tree(ADAPTERS)) if isinstance(n, ast.ClassDef)]
    assert "AdapterInvariantBroken" not in classes and "_Absent" not in classes


@pytest.mark.parametrize("order", [("_decryptage_orm_bridge", "_adapters"), ("_adapters", "_decryptage_orm_bridge"),
                                   ("runner", "_decryptage_orm_bridge"), ("commands", "_adapters")])
def test_no_import_cycle_in_either_python_order(order):
    code = ("import importlib, json, sys\n"
            f"for m in {list(order)!r}:\n"
            "    importlib.import_module('core.authz_f2.' + m)\n"
            "from core.authz_f2 import _bridge_contracts as k\n"
            "print(json.dumps([k.implementation(k.TransactionProbe).__name__,\n"
            "                  k.implementation(k.FinalizationReadPort).__name__]))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({}), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == ["_TransactionProbe", "_FinalizationReadPort"]


def test_bridge_alone_has_no_sql_capability_until_the_adapters_provide_one():
    code = ("import sys\n"
            "import core.authz_f2._decryptage_orm_bridge as b\n"
            "from core.authz_f2 import _bridge_contracts as k\n"
            "print('core.authz_f2._adapters' in sys.modules, k.implementation(k.TransactionProbe))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({}), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "False None"


def test_no_second_runner_and_no_dynamic_import_in_c1b():
    for path in (ADAPTERS, BRIDGE, CONTRACTS, SELECTION, FINALIZATION):
        tokens = _code_tokens(path.read_text(encoding="utf-8")).split("\n")
        assert "AuthorizedTransactionRunner" not in tokens and "runner" not in tokens, path.name
        tree = _tree(path)
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and ast.unparse(n.func) in ("importlib.import_module", "__import__", "exec", "eval")], path.name
    for path in (SELECTION, FINALIZATION):
        imported = {n.module for n in ast.walk(_tree(path)) if isinstance(n, ast.ImportFrom)}
        assert not imported & {"core.authz_f2.runner", "core.authz_f2._adapters", "core.authz_f2._decryptage_orm_bridge",
                               "core.authz_f2.decryptage_generation", "core.db", "core.models"}, path.name


def test_capabilities_are_built_only_by_the_adapters():
    built = {(ast.unparse(n.func), f.name) for f in ast.walk(_tree(ADAPTERS)) if isinstance(f, ast.FunctionDef)
             for n in ast.walk(f) if isinstance(n, ast.Call) and ast.unparse(n.func) in (
                 "_TransactionProbe", "_FinalizationReadPort")}
    assert built == {("_TransactionProbe", "_transaction_probe"), ("_FinalizationReadPort", "_finalize_candidate")}
    for path in (BRIDGE, CONTRACTS):
        assert "_CAPABILITY" not in _code_tokens(path.read_text(encoding="utf-8")).split("\n"), path.name


# --------------------------------------------------------------------------
# 3. Protocole
# --------------------------------------------------------------------------

def test_bridge_finalization_follows_the_frozen_phases():
    source = _source(BRIDGE, "_finalize_candidate")
    assert _in_order(source, [
        "candidate.owner_user_id != user_id", "with _business_savepoint(db, probe) as scope:",
        "ad.claim_delivery(", "except ad.AssistantTurnIdentityCollision as exc:", "if not created:",
        "_prepare_turn(db, scope, user_id, delivery,", "except dcr.AmbiguousActiveAnalysisSessions as exc:",
        "except dcr.StaleConversationContext as exc:", "probe.read_preparation(db, scope)",
        "_same_preparation(candidate, current, prepared.context)", "_apply_prepared_turn(db, scope, user_id, prepared,",
        "if scope.rolled_back:", "return applied"])
    handlers = [h for h in ast.walk(_functions(BRIDGE)["_finalize_candidate"]) if isinstance(h, ast.ExceptHandler)]
    assert sorted(ast.unparse(h.type) for h in handlers) == [
        "ad.AssistantTurnIdentityCollision", "dcr.AmbiguousActiveAnalysisSessions", "dcr.StaleConversationContext"]
    for handler in handlers:  # exception EXACTE seulement (une sous-classe est propagée)
        assert ast.unparse(handler.body[0]).startswith("if not _exactly(exc, ")
    assert "except Exception" not in source and "except BaseException" not in source
    same = _source(BRIDGE, "_same_preparation")
    assert _in_order(same, ["dms.select_method(", "dcand.build_snapshot(view, turn.ticker_input,",
                            "rebuilt.fingerprint != snapshot.fingerprint or rebuilt.canonical() != snapshot.canonical()",
                            "_cognitive_context_matches("])


def test_adapters_finalization_checks_b1_before_any_delivery_and_updates_after_release():
    source = _source(ADAPTERS, "_finalize_candidate")
    assert _in_order(source, [
        "candidate.owner_user_id != user_id", "_LOCK_RESERVATION", "_lease_clock(conn)", "_reservation(row, user_id)",
        "_fingerprint(keys, res.fingerprint_version, res.fingerprint_key_version, user_id, intent)",
        "res.state == dr.FINALIZED", "_recognize_finalized(", "res.state == dr.BLOCKED", "_lease_is_held(",
        "_FinalizationReadPort(_CAPABILITY,", "bridge._execute(", "port._close()",
        "type(outcome) is bridge.PreparedTurnApplied", "_finalize_reservation(conn, user_id, res, intent, outcome)"])
    # Aucune mutation B1 avant le pont ; l'UPDATE final après son retour seulement.
    before_bridge = source[:source.index("bridge._execute(")]
    assert "_FINALIZE_RESERVATION" not in before_bridge and "_BLOCK_PROVEN_COLLISION" not in before_bridge
    finalize = _source(ADAPTERS, "_finalize_reservation")
    assert _in_order(finalize, ["_finalization_clock(conn)", "_FINALIZE_RESERVATION", "if row is None:",
                                "return CONFLICT"])
    collision = _source(ADAPTERS, "_block_proven_collision")
    assert _in_order(collision, ["_legacy_delivery(", "_finalization_clock(conn)", "_BLOCK_PROVEN_COLLISION",
                                 "return CONFLICT"])
    for name in ("_finalize_candidate", "_finalize_reservation", "_block_proven_collision", "_recognize_finalized"):
        assert "except" not in _source(ADAPTERS, name), name  # aucune erreur convertie en résultat
    # Mapping fermé des motifs d'annulation locale (freeze V1.2.1).
    for reason, result in (("CONTEXT_CHANGED", "dfin.FinalizationContextChanged("),
                           ("PREPARATION_REFUSED", "dfin.FinalizationPreparationRefused("),
                           ("LEGACY_COLLISION", "_block_proven_collision("), ("LEASE_STALE", "return CONFLICT"),
                           ("INTENT_MISMATCH", "return IDEMPOTENCY_CONFLICT"), ("NOT_FOUND", "return ABSENT")):
        assert _in_order(source, [f"outcome.reason == bridge.{reason}", result]), reason


def test_already_finalized_recognition_uses_the_candidate_level_and_writes_nothing():
    source = _source(ADAPTERS, "_recognize_finalized")
    assert "bridge._delivery_request_fingerprint(candidate)" in source
    assert "_first(conn, _LINKED_DELIVERY" in source
    assert not {"_FINALIZE_RESERVATION", "_BLOCK_PROVEN_COLLISION", "_execute", "level"} & set(
        _code_tokens(source).split("\n"))
    fingerprint = _source(BRIDGE, "_delivery_request_fingerprint")
    assert "candidate.delivery_fingerprint_inputs()" in fingerprint
    inputs = dcand.GeneratedDecryptageCandidate.delivery_fingerprint_inputs
    assert "self.preparation.level" in inspect.getsource(inputs)


def test_read_port_checks_its_scope_before_any_query():
    method = ast.unparse(_class_method(ADAPTERS, "_FinalizationReadPort", "read_preparation"))
    assert _in_order(method, ["if self._consumed:", "object.__setattr__(self, '_consumed', True)",
                              "type(db) is not bridge._BridgeSession", "scope is not self._scope",
                              "scope._require_current(db)", "self._conn.get_transaction() is not outer",
                              "_preparation_outcome(self._conn, self._keys, self._user_id, self._intent)"])
    assert "outcome is ABSENT or outcome is IDEMPOTENCY_CONFLICT" in method  # impossibles sous verrou : 503


@pytest.mark.parametrize("entry", sorted(REACHABLE_C1B))
def test_closed_call_graph_of_the_finalization(entry):
    assert _reachable(entry) == REACHABLE_C1B[entry]


# --------------------------------------------------------------------------
# 4. Sélecteur de méthode partagé et projection financière
# --------------------------------------------------------------------------

@pytest.mark.parametrize("has_session, question, context, basis", [
    (True, "x", "h", dcand.BASIS_ACTIVE_ANALYSIS),
    (True, "", "", dcand.BASIS_ACTIVE_ANALYSIS),
    (False, "", "", dcand.BASIS_INITIAL_TURN),
    (False, "Quel moat ?", "", dcand.BASIS_INITIAL_TURN),
    (False, "Quel est le moat de LVMH ?", "historique", dcand.BASIS_LOOKUP),
    (False, "", "historique", dcand.BASIS_LOOKUP),
])
def test_method_selection_keeps_the_frozen_rules(has_session, question, context, basis):
    selection = dms.select_method(has_active_session=has_session, question=question, context=context,
                                  last_method_id=None, market_name="LVMH", ticker="MC.PA")
    assert selection.basis == basis
    if basis == dcand.BASIS_LOOKUP:
        text = question or "analyser bilan états financiers LVMH"
        expected = lookup_method(text, context=context, last_method_id=None, log=False)
        assert selection.method == expected
    else:
        assert selection.method_id == "construction_these"
    assert dms.company_name(None, "MC.PA") == "MC.PA" and dms.company_name("LVMH", "MC.PA") == "LVMH"
    with pytest.raises(TypeError):
        dms.select_method(has_active_session=1, question="", context="", last_method_id=None, market_name=None,
                          ticker="MC.PA")


def test_generation_and_finalization_call_the_same_selector():
    generation = (AUTHZ / "decryptage_generation.py").read_text(encoding="utf-8")
    assert "dms.select_method(" in generation and "lookup_method" not in _code_tokens(generation).split("\n")
    assert "dms.select_method(" in _source(BRIDGE, "_same_preparation")
    assert _code_tokens(SELECTION.read_text(encoding="utf-8")).split("\n").count("lookup_method") >= 1


def test_progress_facts_projection_keeps_values_absences_and_zero():
    assert dfin.PROGRESS_FACT_FIELDS == tuple(dp._FACT_FIELDS)
    fields = []
    for name in dm.FIELD_NAMES:
        if name == "roe":
            fields.append(dm.FinancialField(name, dm.PRESENT, 0.0, "eodhd", "TTM"))
        elif name == "eps":
            fields.append(dm.FinancialField(name, dm.CURRENCY_MISMATCH))
        elif name == "operating_margin":
            fields.append(dm.FinancialField(name, dm.PRESENT, 26.5, "eodhd", "FY:2025-12-31"))
        else:
            fields.append(dm.FinancialField(name, dm.ABSENT))
    try:
        market = dm.FinancialSnapshot(dm.SNAPSHOT_VERSION, "MC.PA", "MC.PA", "MC", None, "eodhd", "LVMH", None,
                                      "EUR", None, None, None, None, None, None, None, tuple(fields), (),
                                      dm.PRICE_UNAVAILABLE)
    except ValueError as exc:  # pragma: no cover — contrat du snapshot
        pytest.fail(f"instantané de test hors contrat : {exc}")
    facts = dfin.progress_facts(market)
    assert facts["roe"] == 0.0 and facts["operating_margin"] == 26.5  # zéro réel conservé
    assert facts["eps"] is None and facts["net_cash"] is None          # devise incohérente / absence
    assert set(facts) == set(dfin.PROGRESS_FACT_FIELDS)
    with pytest.raises(TypeError):
        dfin.progress_facts({"roe": 1.0})


# --------------------------------------------------------------------------
# 5. Dormance
# --------------------------------------------------------------------------

def test_no_route_no_activation_no_migration():
    api_source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    for name in ("FinalizeDecryptageCandidate", "_bridge_contracts", "CandidateFinalization",
                 "decryptage_method_selection", "decryptage_finalization", "authz_f2"):
        assert name not in api_source, name
    for path in (REPO_ROOT / "core" / "authz_f2_http").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "Finalize" not in text and "decryptage" not in text.lower(), path.name
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(versions) == 21 and versions[-1] == "0021_decryptage_turn_reservations.py"
    assert dr.LEASE_DURATION.total_seconds() == 360 and dr.MAX_ATTEMPTS == 3
    assert dr.BLOCKED_REASONS == ("legacy_collision", "attempts_exhausted") and dr.STATES == (
        "reserved", "blocked", "finalized")


def test_real_entrypoint_never_loads_the_finalization():
    code = ("import sys, json\nimport api\n"
            "print(json.dumps(sorted(m for m in sys.modules if 'authz' in m or 'decryptage_method' in m "
            "or 'decryptage_finalization' in m)))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({"ORYX_DECRYPTAGE_F2": "1", "ORYX_F2_ENABLED": "1"}), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
