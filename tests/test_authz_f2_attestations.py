"""V0-T08 2B-2 PR 0 — contrat d'attestation interne F2 et frontières, sans base
de données.

1. contrat : quatre champs exacts, immuable, vocabulaire et couples fermés,
   aucune donnée financière, IA ou d'authentification ; fabriques TOTALES
   (jamais une exception) ; enveloppe et liaison ;
2. contrats publics inchangés : AuthorizationOutcome, DeliveryAcknowledged,
   PortfolioWriteResult, surface publique du runner ;
3. producteurs fermés : seuls le pont ACK (chemin attesté) et l'adaptateur
   portefeuille construisent une attestation ; le runner ne fait que la retirer
   et la lier ; aucune instruction SQL, aucun verrou ajouté ;
4. dormance : aucun appelant de _run_attested hors des tests, aucune route,
   aucun analytics importé ou raccordé, aucune migration ; V0-T08 2B-2 PR 2 :
   seule exception nominative, la frontière privée core/_activity_proof_bridge.py
   (un appel, marqueurs exacts, elle-même sans appelant).
"""
import ast
import dataclasses
import inspect
import uuid

import pytest

from core.authz_f2 import _adapters
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import attestations as att
from core.authz_f2 import commands as c
from core.authz_f2 import portfolio_fingerprint as fp
from core.authz_f2 import portfolio_results as results
from core.authz_f2.runner import AttestedOutcome, AuthorizationOutcome, AuthorizedTransactionRunner
from tests.test_migration_0002_analysis_sessions import REPO_ROOT

AUTHZ = REPO_ROOT / "core" / "authz_f2"
AUTHZ_HTTP = REPO_ROOT / "core" / "authz_f2_http"
MODULE = AUTHZ / "attestations.py"
ADAPTERS = AUTHZ / "_adapters.py"
BRIDGE = AUTHZ / "_decryptage_orm_bridge.py"
RUNNER = AUTHZ / "runner.py"
# V0-T08 2B-2 PR 2 : frontière privée F2 -> analytics, hors des deux domaines.
PROOF_BRIDGE = "core/_activity_proof_bridge.py"
PROOF_BRIDGE_MARKERS = ["_run_attested", "AttestedOutcome", "BusinessAttestation", "authz_f2 import attestations"]
OWNER = "user-a"
OP = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _functions(path):
    return {n.name: n for n in ast.walk(_tree(path)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _att_calls(path) -> set:
    """(fonction englobante, attribut att.* appelé)."""
    found = set()
    for fn in _functions(path).values():
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "att":
                found.add((fn.name, node.func.attr))
    return found


# --------------------------------------------------------------------------
# 1. Contrat
# --------------------------------------------------------------------------

def test_the_attestation_has_exactly_four_immutable_fields():
    assert [(f.name, f.type) for f in dataclasses.fields(att.BusinessAttestation)] == [
        ("owner_user_id", str), ("operation", str), ("operation_id", uuid.UUID), ("qualification", str)]
    params = att.BusinessAttestation.__dataclass_params__
    assert params.frozen and hasattr(att.BusinessAttestation, "__slots__")
    a = att.BusinessAttestation(OWNER, att.DECRYPTAGE_DELIVERY_ACK, OP, att.DELIVERY_FIRST_ACK)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
        a.qualification = att.DELIVERY_REPLAY
    with pytest.raises((AttributeError, TypeError)):
        a.amount = 1
    assert OWNER not in repr(a) and str(OP) not in repr(a)  # jamais le propriétaire ni l'identifiant dans un journal


def test_the_vocabulary_and_the_couples_are_closed():
    assert att.OPERATIONS == ("decryptage_delivery_ack", "portfolio_position_create", "portfolio_position_update",
                              "portfolio_position_delete")
    assert att.QUALIFICATIONS == ("delivery_first_ack", "delivery_replay", "portfolio_created", "portfolio_updated",
                                  "portfolio_deleted", "portfolio_noop", "portfolio_replay")
    assert dict(att.ADMISSIBLE) == {
        "decryptage_delivery_ack": {"delivery_first_ack", "delivery_replay"},
        "portfolio_position_create": {"portfolio_created", "portfolio_replay"},
        "portfolio_position_update": {"portfolio_updated", "portfolio_noop", "portfolio_replay"},
        "portfolio_position_delete": {"portfolio_deleted", "portfolio_replay"}}
    assert att.NEW_EFFECTS == {"delivery_first_ack", "portfolio_created", "portfolio_updated", "portfolio_deleted"}
    # Natures portefeuille = types d'opération des reçus (identité durable du triplet).
    assert (att.PORTFOLIO_CREATE, att.PORTFOLIO_UPDATE, att.PORTFOLIO_DELETE) == (fp.CREATE, fp.UPDATE, fp.DELETE)
    assert set(_adapters.OPERATION_TYPES.values()) == {att.PORTFOLIO_CREATE, att.PORTFOLIO_UPDATE,
                                                       att.PORTFOLIO_DELETE}
    for operation, allowed in att.ADMISSIBLE.items():
        for qualification in att.QUALIFICATIONS:
            if qualification in allowed:
                assert att.BusinessAttestation(OWNER, operation, OP, qualification).new_effect is (
                    qualification in att.NEW_EFFECTS)
            else:
                with pytest.raises(att.AttestationInvalid):
                    att.BusinessAttestation(OWNER, operation, OP, qualification)


@pytest.mark.parametrize("fields", [
    ("", att.PORTFOLIO_CREATE, OP, att.PORTFOLIO_CREATED), ("x" * 256, att.PORTFOLIO_CREATE, OP, att.PORTFOLIO_CREATED),
    (None, att.PORTFOLIO_CREATE, OP, att.PORTFOLIO_CREATED), (1, att.PORTFOLIO_CREATE, OP, att.PORTFOLIO_CREATED),
    (OWNER, "portfolio_position_move", OP, att.PORTFOLIO_REPLAY), (OWNER, ["x"], OP, att.PORTFOLIO_REPLAY),
    (OWNER, att.PORTFOLIO_CREATE, str(OP), att.PORTFOLIO_CREATED),
    (OWNER, att.PORTFOLIO_CREATE, type("U", (uuid.UUID,), {})(str(OP)), att.PORTFOLIO_CREATED),
    (OWNER, att.PORTFOLIO_CREATE, OP, "portfolio_created "), (OWNER, att.PORTFOLIO_CREATE, OP, None),
])
def test_out_of_contract_attestations_are_refused_and_factories_never_raise(fields):
    with pytest.raises(att.AttestationInvalid):
        att.BusinessAttestation(*fields)
    assert att.portfolio(*fields) is None  # fabrique totale : statistique perdue, jamais une exception


@pytest.mark.parametrize("first", [1, 0, None, "true"])
def test_the_ack_factory_needs_an_exact_boolean(first):
    assert att.delivery_ack(OWNER, OP, first=first) is None
    assert att.delivery_ack(OWNER, "not-a-uuid", first=True) is None
    assert att.delivery_ack(OWNER, OP, first=True).qualification == att.DELIVERY_FIRST_ACK
    assert att.delivery_ack(OWNER, OP, first=False).qualification == att.DELIVERY_REPLAY


def test_envelope_and_binding():
    value = object()
    a = att.portfolio(OWNER, att.PORTFOLIO_UPDATE, OP, att.PORTFOLIO_NOOP)
    assert att.attach(value, None) is value and att.unwrap(value) == (value, None)
    wrapped = att.attach(value, a)
    assert type(wrapped) is att.Attested and att.unwrap(wrapped) == (value, a)
    assert repr(wrapped) == "Attested(<private>)"
    good = {"owner_user_id": OWNER, "operation": att.PORTFOLIO_UPDATE, "operation_id": OP}
    assert att.bound(a, **good)
    for change in ({"owner_user_id": "user-b"}, {"operation": att.PORTFOLIO_DELETE},
                   {"operation_id": uuid.uuid4()}):
        assert not att.bound(a, **{**good, **change})  # jamais interchangeable
    assert att.bound(dataclasses.replace(a), **good)  # copie exacte : même type, admise
    forged = type("BusinessAttestation", (), {"owner_user_id": OWNER, "operation": att.PORTFOLIO_UPDATE,
                                              "operation_id": OP})()
    assert not att.bound(forged, **good) and not att.bound(None, **good)


def test_the_attestation_carries_no_financial_ai_or_authentication_data():
    names = {f.name for f in dataclasses.fields(att.BusinessAttestation)}
    for forbidden in ("ticker", "quantity", "price", "amount", "target", "envelope", "payload", "analysis",
                      "content", "text", "session", "generation", "secret", "token", "credential", "email",
                      "fingerprint", "at", "timestamp"):
        assert not {n for n in names if forbidden in n.split("_")}, forbidden


# --------------------------------------------------------------------------
# 2. Contrats publics inchangés
# --------------------------------------------------------------------------

def test_public_results_keep_their_exact_shapes():
    assert [f.name for f in dataclasses.fields(AuthorizationOutcome)] == ["status", "value", "principal"]
    assert [f.name for f in dataclasses.fields(bridge.DeliveryAcknowledged)] == [
        "assistant_turn_id", "delivery_status", "response_action"]
    assert [f.name for f in dataclasses.fields(results.PortfolioWriteResult)] == [
        "operation_type", "operation_id", "position_id", "status", "view", "deleted_revision", "body"]
    assert [f.name for f in dataclasses.fields(AttestedOutcome)] == ["outcome", "attestation"]


def test_the_runner_public_surface_is_unchanged_and_the_attested_exit_is_private():
    assert {n for n in vars(AuthorizedTransactionRunner) if not n.startswith("_")} == {"run"}
    assert list(inspect.signature(AuthorizedTransactionRunner.run).parameters) == ["self", "credential", "command"]
    assert list(inspect.signature(AuthorizedTransactionRunner._run_attested).parameters) == [
        "self", "credential", "command"]
    run = ast.unparse(_functions(RUNNER)["run"])
    assert "return self._authorize(credential, command)[0]" in run  # même chemin, même outcome
    attested = ast.unparse(_functions(RUNNER)["_run_attested"])
    assert "self._authorize(credential, command)" in attested and "outcome.status != AUTHORIZED" in attested


def test_the_runner_retains_the_attestation_only_after_the_transaction_block():
    run = _functions(RUNNER)["_run"]
    source = ast.unparse(run)
    # Retrait AVANT les contrôles des sentinelles, juste après la répartition historique.
    order = ["spec.execute(conn, context, command, business)", "value, attested = att.unwrap(value)",
             "if value is _adapters.ABSENT:", "if value is _adapters.CONFLICT:", "self._hook('executed')",
             "return (AuthorizationOutcome(AUTHORIZED, value, principal), attested)"]
    assert [source.index(m) for m in order] == sorted(source.index(m) for m in order)
    [with_node] = [n for n in ast.walk(run) if isinstance(n, ast.With)]
    final = run.body[-1]
    assert isinstance(final, ast.Return) and final.lineno > with_node.end_lineno  # après le COMMIT
    assert "AuthorizationOutcome(refused.status), None" in source  # ROLLBACK : aucune attestation
    authorize = ast.unparse(_functions(RUNNER)["_authorize"])
    assert "self._retain(name, command, outcome, attested)" in authorize
    assert authorize.count("AuthorizationOutcome(UNAVAILABLE), None") == 4
    retain = ast.unparse(_functions(RUNNER)["_retain"])
    assert "outcome.status == AUTHORIZED" in retain and "att.bound(" in retain and "except Exception" in retain
    assert dict(_adapters.ATTESTED_COMMANDS) == {
        c.AcknowledgeDecryptageDelivery: (att.DECRYPTAGE_DELIVERY_ACK, "assistant_turn_id"),
        c.CreatePortfolioPosition: (att.PORTFOLIO_CREATE, "operation_id"),
        c.ReplacePortfolioPosition: (att.PORTFOLIO_UPDATE, "operation_id"),
        c.DeletePortfolioPosition: (att.PORTFOLIO_DELETE, "operation_id")}


# --------------------------------------------------------------------------
# 3. Producteurs fermés, aucune instruction ni verrou ajouté
# --------------------------------------------------------------------------

def test_only_the_ack_bridge_and_the_portfolio_adapter_produce_attestations():
    assert _att_calls(BRIDGE) == {("_acknowledge", "attach"), ("_acknowledge", "delivery_ack")}
    # Correctif d'audit : plus aucune construction directe d'enveloppe hors du module (attach TOTAL).
    assert _att_calls(ADAPTERS) == {("_acknowledge_delivery", "unwrap"), ("_acknowledge_delivery", "attach"),
                                    ("_attest_portfolio", "attach"), ("_attest_portfolio", "portfolio")}
    assert _att_calls(RUNNER) == {("_run", "unwrap"), ("_retain", "bound")}
    for path in sorted(AUTHZ_HTTP.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "attestation" not in text.lower() and "_run_attested" not in text, path.name


def test_the_bridge_observes_the_status_under_the_delivery_lock_before_any_mutation():
    source = ast.unparse(_functions(BRIDGE)["_acknowledge"])
    order = ["ad.lock_owned_delivery(", "_require_attested_canonical(db, delivery, probe)",
             "first_ack = delivery.status == ad.PENDING", "_require_owned_provenance(",
             "dcr.capture_delivered_response(", "ad.acknowledge_locked_delivery(", "db.flush()",
             "if attested and (link is None", "if attested:\n        return att.attach(",
             "att.delivery_ack(user_id, delivery.id, first=first_ack)",
             "return DeliveryAcknowledged(delivery.id, delivery.status, link.response_action if link is not None "
             "else None)"]
    assert [source.index(m) for m in order] == sorted(source.index(m) for m in order)
    # La nouveauté n'est jamais déduite du statut final ni de la preuve pré-verrou.
    assert source.count("ad.PENDING") == 1 and "proof" not in source
    adapter = ast.unparse(_functions(ADAPTERS)["_acknowledge_delivery"])
    assert "proof.delivery_status" not in adapter and "delivery_ack" not in adapter  # l'adaptateur ne qualifie rien


def test_no_statement_and_no_lock_were_added():
    def executed(fn):
        return sorted(ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Call)
                      and ast.unparse(n.func) in ("_first", "_rows", "conn.execute", "db.execute"))

    functions = _functions(ADAPTERS)
    assert executed(functions["_write_portfolio"]) == sorted([
        "_first(conn, _CREATE_POSITION, u=user_id, **_write_values(command, at))",
        "_first(conn, _DELETE_POSITION, id=position_id, u=user_id, r=command.expected_revision)",
        "_first(conn, _REPLACE_POSITION, id=position_id, u=user_id, r=command.expected_revision, "
        "**_write_values(command, at))",
        "conn.execute(sa.text(_INSERT_RECEIPT), {'u': user_id, 't': operation_type, 'o': command.operation_id, "
        "'fv': fp.FINGERPRINT_VERSION, 'kv': keys.current_version, 'f': computed, 'rv': results.RESULT_VERSION, "
        "'s': result.status, 'payload': result.body.decode('utf-8'), 'pid': position_id, 'at': ctx.authorized_at})"])
    assert executed(functions["_acknowledge_delivery"]) == [
        "_first(conn, _LOCK_RESERVATION, id=command.reservation_id, u=user_id)"]
    assert executed(functions["_attest_portfolio"]) == []
    assert executed(_functions(BRIDGE)["_acknowledge"]) == []
    for name in ("_run", "_authorize", "_retain", "_run_attested"):
        assert "execute(" not in ast.unparse(_functions(RUNNER)[name]).replace("spec.execute(", ""), name


def test_the_attestation_module_is_pure():
    tree = _tree(MODULE)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported == {"uuid", "dataclasses", "types"}
    tokens = MODULE.read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "execute(", "os.environ", "getenv", "socket", "requests", "httpx",
                      "product_analytics", "logging"):
        assert forbidden not in tokens, forbidden


# --------------------------------------------------------------------------
# 4. Dormance
# --------------------------------------------------------------------------

def test_nothing_outside_the_boundary_and_the_tests_uses_the_attested_exit():
    markers = ("_run_attested", "AttestedOutcome", "BusinessAttestation", "core.authz_f2.attestations",
               "authz_f2 import attestations")
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if (not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts or "node_modules" in path.parts
                or path.suffix not in (".py", ".js", ".html", ".ts", ".json", ".sh", ".yml", ".yaml", ".toml")):
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in ("tests", "docs") or AUTHZ in path.parents:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if relative.as_posix() == PROOF_BRIDGE:
            # V0-T08 2B-2 PR 2 (nominatif) : l'UNIQUE consommateur de la sortie
            # attestée, marqueurs EXACTS, un seul appel (sans appelant lui-même).
            assert [m for m in markers if m in text] == PROOF_BRIDGE_MARKERS, relative
            calls = [n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == "_run_attested"]
            assert [ast.unparse(n) for n in calls] == ["runner._run_attested(credential, command)"], relative
            continue
        if any(m in text for m in markers):
            offenders.append(str(relative))
    assert offenders == []  # ni api.py, ni routes, ni analytics, ni workers, ni scripts
    users = {p.name for p in AUTHZ.glob("*.py") if any(m in p.read_text(encoding="utf-8") for m in markers)}
    # __init__.py : docstring du paquet seulement (aucun import, IMPORT_MATRIX).
    assert users == {"__init__.py", "attestations.py", "_adapters.py", "_decryptage_orm_bridge.py", "runner.py"}
    callers = {p.name for p in AUTHZ.glob("*.py") if "._run_attested(" in p.read_text(encoding="utf-8")}
    assert callers == set()  # aucun appelant de production, même dans la frontière


def test_the_boundary_never_imports_product_analytics_and_analytics_never_sees_attestations():
    for path in sorted(list(AUTHZ.glob("*.py")) + list(AUTHZ_HTTP.glob("*.py"))):
        for node in ast.walk(_tree(path)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not [n for n in names if "product_analytics" in n], path.name
    analytics = REPO_ROOT / "core" / "product_analytics"
    if analytics.exists():
        for path in analytics.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "attestation" not in text.lower() and "authz_f2" not in text, path.name


# --------------------------------------------------------------------------
# 5. Correctif d'audit PR 0 : construction et transport des enveloppes TOTAUX
# --------------------------------------------------------------------------

def _raise(*args, **kwargs):
    raise RuntimeError("panne d'attestation injectée")


class _NotATuple:
    """Type d'enveloppe impossible : tuple.__new__ le refuse (TypeError)."""


# Pannes qui doivent abandonner l'attestation SEULE (valeur métier intacte,
# aucune attestation exposée). Partagées avec les tests PostgreSQL.
DROPPING_FAULTS = ("attestation_init", "factory_build", "envelope_type", "envelope_verification", "binding",
                   "foreign_object")


def inject_fault(monkeypatch, fault):
    if fault == "attestation_init":      # construction du contrat
        monkeypatch.setattr(att.BusinessAttestation, "__post_init__", _raise)
    elif fault == "factory_build":       # fabrique interne
        monkeypatch.setattr(att, "_build", _raise)
    elif fault == "envelope_type":       # construction de l'enveloppe
        monkeypatch.setattr(att, "_ENVELOPE", _NotATuple)
    elif fault == "envelope_verification":  # enveloppe construite mais jugée non conforme
        monkeypatch.setattr(att, "ENVELOPE_SIZE", 3)
    elif fault == "binding":             # liaison après COMMIT (runner)
        monkeypatch.setattr(att, "bound", _raise)
    elif fault == "foreign_object":      # objet transporté qui n'est pas une attestation
        monkeypatch.setattr(att, "delivery_ack", lambda *a, **k: object())
        monkeypatch.setattr(att, "portfolio", lambda *a, **k: object())
    elif fault == "envelope_accessors":  # méthodes redéfinissables de l'enveloppe : jamais utilisées
        monkeypatch.setattr(att.Attested, "value", property(_raise))
        monkeypatch.setattr(att.Attested, "attestation", property(_raise))
    else:
        raise AssertionError(fault)


@pytest.mark.parametrize("fault", ["attestation_init", "factory_build"])
def test_factories_stay_total_under_injected_faults(monkeypatch, fault):
    inject_fault(monkeypatch, fault)
    assert att.delivery_ack(OWNER, OP, first=True) is None
    assert att.portfolio(OWNER, att.PORTFOLIO_CREATE, OP, att.PORTFOLIO_CREATED) is None


@pytest.mark.parametrize("fault", ["envelope_type", "envelope_verification"])
def test_attach_returns_the_exact_original_value_when_the_envelope_fails(monkeypatch, fault):
    a = att.portfolio(OWNER, att.PORTFOLIO_CREATE, OP, att.PORTFOLIO_CREATED)
    inject_fault(monkeypatch, fault)
    for value in (object(), results, None, (1, 2), _adapters.ABSENT):
        assert att.attach(value, a) is value  # même objet : jamais une copie ni une substitution
        assert att.unwrap(att.attach(value, a)) == (value, None)  # aucune attestation transportée


def test_the_envelope_is_verified_before_it_leaves_attach():
    a = att.portfolio(OWNER, att.PORTFOLIO_CREATE, OP, att.PORTFOLIO_CREATED)
    value = object()
    envelope = att.attach(value, a)
    assert type(envelope) is att.Attested and tuple.__len__(envelope) == att.ENVELOPE_SIZE
    assert tuple.__getitem__(envelope, 0) is value and tuple.__getitem__(envelope, 1) is a
    assert att.unwrap(envelope) == (value, a) and att.unwrap(envelope)[0] is value


def test_transport_never_uses_overridable_envelope_methods(monkeypatch):
    a = att.portfolio(OWNER, att.PORTFOLIO_CREATE, OP, att.PORTFOLIO_CREATED)
    value = object()
    inject_fault(monkeypatch, "envelope_accessors")
    envelope = att.attach(value, a)
    assert type(envelope) is att.Attested and att.unwrap(envelope) == (value, a)


@pytest.mark.parametrize("value", [None, 0, "", (), ("a", "b"), [1, 2], {"value": 1}, object(), results,
                                   _adapters.ABSENT, _adapters.CONFLICT, _adapters.IDEMPOTENCY_CONFLICT])
def test_unwrap_is_total_for_every_business_value(value):
    unwrapped, attestation = att.unwrap(value)
    assert unwrapped is value and attestation is None  # un tuple ordinaire n'est jamais une enveloppe


@pytest.mark.parametrize("items", [(), (1,), (1, 2, 3)])
def test_a_malformed_envelope_is_a_closed_refusal_never_a_substituted_value(items):
    """Impossible à produire par attach (taille vérifiée) ; si elle existait, sa
    valeur métier ne serait pas restituable : refus fermé (ROLLBACK, 503), jamais
    une enveloppe ni une valeur rendue comme résultat."""
    with pytest.raises(att.AttestationInvalid):
        att.unwrap(tuple.__new__(att.Attested, items))


def test_bound_is_total():
    class Exploding:
        def __getattribute__(self, name):
            raise RuntimeError("attribut illisible")

    exploding = Exploding()
    assert att.bound(exploding, owner_user_id=OWNER, operation=att.PORTFOLIO_CREATE, operation_id=OP) is False


def test_transport_functions_contain_their_own_guards():
    functions = _functions(MODULE)
    for name in ("attach", "delivery_ack", "portfolio", "bound", "_build"):
        assert any(isinstance(n, ast.Try) for n in ast.walk(functions[name])), name
    attach = ast.unparse(functions["attach"])
    assert "tuple.__new__(_ENVELOPE, (value, attestation))" in attach and "return value" in attach
    unwrap = ast.unparse(functions["unwrap"])
    assert "type(value) is not _ENVELOPE" in unwrap and "tuple.__getitem__(value, 0)" in unwrap
    assert ".value" not in unwrap and ".attestation" not in unwrap  # aucune méthode redéfinissable
