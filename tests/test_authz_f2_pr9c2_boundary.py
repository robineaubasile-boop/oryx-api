"""R1 PR 9-C2 — frontières statiques du replay canonique et de l'ACK F2 (sans
base de données).

1. inventaires fermés : vingt commandes, trois opérations du pont
   (inchangées), trois contrats de capacités ; commandes de champs EXACTS,
   admission structurelle complète, aucune normalisation ; aucun trousseau ;
2. contrats et scellement : AckAdmissionPort pur, une implémentation exacte,
   construite par le seul adaptateur ACK ; CAPABILITIES fermé (sonde OU
   attestation pour l'ACK, jamais un isinstance) ;
3. protocole : ordre de l'adaptateur ACK, vérifications du pont AVANT la
   provenance et la capture, preuve sans HMAC, sans fournisseur, replay sans
   resérialisation, constantes recopiées égales aux services ;
4. dormance : aucune route, aucune activation, aucune migration.
"""
import ast
import dataclasses
import json
import subprocess
import sys
import uuid

import pytest

from core import assistant_delivery as ad
from core import decryptage_cognitive_runtime as dcr
from core.authz_f2 import _adapters
from core.authz_f2 import _bridge_contracts as contracts
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2 import decryptage_market_data as dm
from tests.test_authz_f2_pr9a_boundary import BRIDGE, _functions, _tree
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0004_drop_company_analyses import _code_tokens

ADAPTERS = REPO_ROOT / "core" / "authz_f2" / "_adapters.py"
CONTRACTS = REPO_ROOT / "core" / "authz_f2" / "_bridge_contracts.py"
C2_COMMANDS = (c.ReadCanonicalDecryptageDelivery, c.AcknowledgeDecryptageDelivery)


def _source(path, name) -> str:
    return ast.unparse(_functions(path)[name])


def _method(path, cls, name) -> str:
    (node,) = [n for n in _tree(path).body if isinstance(n, ast.ClassDef) and n.name == cls]
    (method,) = [m for m in node.body if isinstance(m, ast.FunctionDef) and m.name == name]
    return ast.unparse(method)


def _in_order(source, markers):
    positions, start = [], 0
    for marker in markers:
        start = source.index(marker, start)
        positions.append(start)
    return positions == sorted(positions)


# --------------------------------------------------------------------------
# 1. Inventaires fermés et commandes
# --------------------------------------------------------------------------

def test_twenty_commands_three_operations_three_contracts():
    assert len(_adapters.REGISTRY) == 20
    replay, ack = (_adapters.REGISTRY[cmd] for cmd in C2_COMMANDS)
    assert (replay.plan, replay.lock, replay.execute, replay.keyed, replay.decryptage_keyed, replay.states) == (
        c.READ, _adapters._no_business_lock, _adapters._read_canonical_delivery, False, False, (type(None),))
    assert (ack.plan, ack.lock, ack.execute, ack.keyed, ack.decryptage_keyed, ack.states) == (
        c.DECRYPTAGE_MUTATION, _adapters._no_business_lock, _adapters._acknowledge_delivery, False, False,
        (type(None),))
    assert c.PLANS == (c.READ, c.PROFILE_MUTATION, c.PORTFOLIO_MUTATION, c.DECRYPTAGE_MUTATION)  # aucun plan nouveau
    assert set(bridge.OPERATIONS) == {bridge.TurnFinalization, bridge.DeliveryAcknowledgement,
                                      bridge.CandidateFinalization}
    assert contracts.CONTRACTS == (contracts.TransactionProbe, contracts.FinalizationReadPort,
                                   contracts.AckAdmissionPort)
    assert [contracts.implementation(k) for k in contracts.CONTRACTS] == [
        _adapters._TransactionProbe, _adapters._FinalizationReadPort, _adapters._AckAdmissionPort]
    assert not set(C2_COMMANDS) & set(bridge.OPERATIONS)
    assert c.ReadDelivery in _adapters.REGISTRY and c.ListConversationDeliveries in _adapters.REGISTRY  # distinctes


@pytest.mark.parametrize("cls", C2_COMMANDS)
def test_commands_carry_exactly_the_three_identity_fields(cls):
    fields = [(f.name, f.type) for f in dataclasses.fields(cls)]
    assert fields == [("reservation_id", uuid.UUID), ("conversation_key", str), ("assistant_turn_id", uuid.UUID)]
    assert cls.__dataclass_params__.frozen and issubclass(cls, c.Command)
    good = dict(reservation_id=uuid.uuid4(), conversation_key="conv-1", assistant_turn_id=uuid.uuid4())
    command = cls(**good)
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.conversation_key = "autre"
    for field, value in (("reservation_id", str(uuid.uuid4())), ("assistant_turn_id", str(uuid.uuid4())),
                         ("reservation_id", None), ("assistant_turn_id", 1), ("conversation_key", " conv-1"),
                         ("conversation_key", "conv-1 "), ("conversation_key", ""), ("conversation_key", "x" * 201),
                         ("conversation_key", None), ("conversation_key", b"conv"), ("conversation_key", "\ud800")):
        with pytest.raises(ValueError):
            cls(**{**good, field: value})
    assert cls(**{**good, "conversation_key": "x" * 200}).conversation_key == "x" * 200  # borne B1 inchangée
    assert cls(**{**good, "conversation_key": "Conv-É"}).conversation_key == "Conv-É"  # aucune normalisation
    with pytest.raises(TypeError):
        cls(**good, user_id="u")
    sub = type("Sub", (uuid.UUID,), {})
    with pytest.raises(ValueError):  # type EXACT : jamais une sous-classe
        cls(**{**good, "reservation_id": sub(str(uuid.uuid4()))})


def test_commands_never_reach_a_keyring_nor_the_b1_hmac():
    for name in ("_read_canonical_delivery", "_acknowledge_delivery", "_canonical_proof", "_canonical_payload",
                 "_terminal"):
        tokens = set(_code_tokens(_source(ADAPTERS, name)).split("\n"))
        assert not tokens & {"keys", "_fingerprint", "fingerprint", "same_fingerprint", "DecryptageKeyring",
                             "_TurnIntent", "_recognize_finalized", "_lease_clock", "lease_token"}, name
    assert list(ast.parse(_source(ADAPTERS, "_read_canonical_delivery")).body[0].args.args[i].arg
                for i in range(4)) == ["conn", "ctx", "command", "locked"]
    assert len(ast.parse(_source(ADAPTERS, "_acknowledge_delivery")).body[0].args.args) == 4  # aucune clé reçue


def test_the_replay_view_is_closed():
    assert [(f.name, f.type) for f in dataclasses.fields(c.CanonicalDecryptageDelivery)] == [
        ("reservation_id", uuid.UUID), ("assistant_turn_id", uuid.UUID), ("delivery_status", str),
        ("payload_json", bytes)]
    assert c.CanonicalDecryptageDelivery.__dataclass_params__.frozen
    assert c.CANONICAL_DELIVERY_STATUSES == (ad.PENDING, ad.DELIVERED)
    view = c.CanonicalDecryptageDelivery(uuid.uuid4(), uuid.uuid4(), "pending", b'{"analysis": "secret"}')
    assert "secret" not in repr(view) and "secret" not in str(view)


# --------------------------------------------------------------------------
# 2. Contrats, scellement, sélection fermée des capacités
# --------------------------------------------------------------------------

def test_contracts_stay_pure_and_the_ack_contract_exposes_one_check():
    tree = _tree(CONTRACTS)
    assert [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))] == []
    assert issubclass(contracts.AckAdmissionPort, contracts.TransactionProbe)
    assert not issubclass(contracts.AckAdmissionPort, contracts.FinalizationReadPort)
    own = {n for n, v in vars(contracts.AckAdmissionPort).items() if callable(v) and not n.startswith("__")}
    assert own == {"require_delivery"}
    params = list(ast.parse(_method(CONTRACTS, "AckAdmissionPort", "require_delivery")).body[0].args.kwonlyargs)
    assert [p.arg for p in params] == ["assistant_turn_id", "user_id", "conversation_key", "surface",
                                       "source_user_turn_id", "delivery_ordinal", "delivery_schema_version"]
    implementation = _adapters._AckAdmissionPort
    assert implementation.__mro__[1] is _adapters._TransactionProbe  # sonde héritée, non sérialisable, non copiable
    assert {n for n in dir(implementation) if not n.startswith("_")} == {"user_id", "bound_to", "require_live",
                                                                       "require_delivery"}


def test_capabilities_selection_is_closed_and_exact():
    assert dict(bridge.CAPABILITIES) == {
        bridge.TurnFinalization: (contracts.TransactionProbe,),
        bridge.DeliveryAcknowledgement: (contracts.TransactionProbe, contracts.AckAdmissionPort),
        bridge.CandidateFinalization: (contracts.FinalizationReadPort,)}
    execute = _source(BRIDGE, "_execute")
    assert "type(probe) not in _admitted_capabilities(operation)" in execute
    for name in ("_execute", "_admitted_capabilities", "_require_probe", "_acknowledge",
                 "_require_attested_canonical"):
        assert "isinstance" not in _source(BRIDGE, name) and "issubclass" not in _source(BRIDGE, name), name
    assert "contracts.implementation(k)" in _source(BRIDGE, "_admitted_capabilities")
    assert "contracts.CONTRACTS" in _source(BRIDGE, "_require_probe")


def test_the_attestation_is_built_only_by_the_ack_adapter_and_closed_in_a_finally():
    built = {(ast.unparse(n.func), f.name) for f in ast.walk(_tree(ADAPTERS)) if isinstance(f, ast.FunctionDef)
             for n in ast.walk(f) if isinstance(n, ast.Call) and ast.unparse(n.func) == "_AckAdmissionPort"}
    assert built == {("_AckAdmissionPort", "_acknowledge_delivery")}
    node = _functions(ADAPTERS)["_acknowledge_delivery"]
    [guarded] = [n for n in ast.walk(node) if isinstance(n, ast.Try)]
    assert [ast.unparse(s) for s in guarded.finalbody] == ["port._close()"]
    assert "bridge._execute(" in ast.unparse(guarded.body)
    tokens = _code_tokens(ast.unparse(node)).split("\n")
    assert "_transaction_probe" not in tokens and "_TransactionProbe" not in tokens  # jamais le chemin historique


# --------------------------------------------------------------------------
# 3. Protocole
# --------------------------------------------------------------------------

def test_ack_adapter_follows_the_frozen_order():
    source = _source(ADAPTERS, "_acknowledge_delivery")
    assert _in_order(source, [
        "_first(conn, _LOCK_RESERVATION, id=command.reservation_id, u=user_id)", "return ABSENT",
        "_canonical_proof(conn, user_id, command)", "if proof is ABSENT:", "if proof is CONFLICT:",
        "_AckAdmissionPort(_CAPABILITY, conn, user_id, proof)", "bridge._execute(conn, ctx, bridge.DeliveryAcknowledgement(",
        "port._close()", "type(outcome) is not bridge.DeliveryAcknowledged",
        "outcome.assistant_turn_id != command.assistant_turn_id", "outcome.response_action is None",
        "return outcome"])
    assert "except" not in source  # aucune erreur convertie en résultat
    replay = _source(ADAPTERS, "_read_canonical_delivery")
    assert "bridge._execute" not in replay and "_LOCK_RESERVATION" not in replay


def test_canonical_proof_decides_in_the_frozen_order():
    source = _source(ADAPTERS, "_canonical_proof")
    assert _in_order(source, [
        "_first(conn, _CANONICAL_DELIVERY", "row['owned'] is not True", "return ABSENT",
        "_reservation(row, user_id)", "res.state != dr.FINALIZED", "return CONFLICT",
        "res.conversation_key != command.conversation_key or res.assistant_delivery_id != command.assistant_turn_id",
        "return CONFLICT", "if not _terminal(res):", "decryptage_reservation_incoherent",
        "row['d_id'] != res.assistant_delivery_id", "decryptage_delivery_incoherent",
        "bridge.COGNITIVE_RUNTIME_V2", "decryptage_cognitive_link_incoherent",
        "row['missing_traces'], row['foreign_events'], row['foreign_sessions']", "decryptage_provenance_incoherent",
        "_canonical_payload(row['d_payload'], res.resolved_ticker, row['d_visible_fingerprint'])",
        "return _CanonicalProof("])
    users = [f.name for f in ast.walk(_tree(ADAPTERS)) if isinstance(f, ast.FunctionDef)
             and "_CANONICAL_DELIVERY" in {n.id for n in ast.walk(f) if isinstance(n, ast.Name)}]
    assert users == ["_canonical_proof"]  # UNE instruction, un seul point d'exécution


def test_the_replay_never_reserializes_the_payload():
    source = _source(ADAPTERS, "_canonical_payload")
    assert "dumps" not in source and "return text.encode('utf-8')" in source
    assert "parse_float=Decimal, parse_int=Decimal" in source  # jamais un float prématuré
    assert "float(" not in source
    assert "Decimal(repr(as_float)) == value" in _source(ADAPTERS, "_binary64_decimal")
    assert _adapters.CANONICAL_PAYLOAD_KEYS == set(dcand.CandidatePayload(
        "MC.PA", "LVMH", None, "x", None, "EUR").as_dict())
    assert not _adapters.CANONICAL_PAYLOAD_KEYS & set(ad.RESERVED_RESPONSE_KEYS)


def test_copied_constants_equal_the_services():
    assert bridge.COGNITIVE_RUNTIME_V2 == dcr.COGNITIVE_RUNTIME_VERSION_V2 == "decryptage-cognitive-runtime-v2"
    assert bridge.RUNTIME_VERSION_KEY == dcr.RUNTIME_VERSION_KEY == "cognitive_runtime_version"
    assert bridge.CANONICAL_COUPLES == ((ad.PENDING, dcr.AWAITING_DELIVERY), (ad.DELIVERED, dcr.CAPTURED))
    assert bridge.RESPONSE_ACTIONS == dcr.RESPONSE_ACTIONS and bridge.DELIVERED == ad.DELIVERED
    assert "'cognitive_runtime_version'" in _adapters._CANONICAL_DELIVERY
    assert dcand.DISCLAIMER == "Analyse éducative uniquement. Ne constitue pas un conseil en investissement."
    assert dcand.RESPONSE_MAX_BYTES == 64 * 1024 and dm.CURRENCY_RE.pattern == "[A-Z]{3}"
    assert bridge._visible_content_fingerprint("é") == ad.visible_content_fingerprint("é")


def test_the_bridge_checks_the_attestation_before_provenance_and_capture():
    attested = _source(BRIDGE, "_require_attested_canonical")
    assert _in_order(attested, ["probe.require_delivery(", "metadata.get(RUNTIME_VERSION_KEY) != COGNITIVE_RUNTIME_V2",
                                "select(DecryptageCognitiveLink.capture_version,", ".with_for_update()",
                                "link.capture_version != COGNITIVE_RUNTIME_V2",
                                "(delivery.status, link.capture_state) not in CANONICAL_COUPLES"])
    acknowledge = _source(BRIDGE, "_acknowledge")
    assert "if attested:\n            raise AckProvenanceIncoherent" in acknowledge  # jamais un 404 pour une attestation
    assert issubclass(bridge.AckProvenanceIncoherent, contracts.AdapterInvariantBroken)
    assert bridge.AckProvenanceIncoherent.alert == "decryptage_bridge_ack_provenance_incoherent"
    for name in ("_require_attested_canonical", "_acknowledge"):  # aucun service nouveau, aucune progression
        tokens = set(_code_tokens(_source(BRIDGE, name)).split("\n"))
        assert not tokens & {"dp", "apply_construction_these_progress", "record_turn_progress", "capture_user_turn",
                             "prepare_locked_cognitive_turn", "claim_delivery"}, name


def test_c2_paths_call_no_provider_and_no_network():
    for name in ("_read_canonical_delivery", "_acknowledge_delivery", "_canonical_proof", "_canonical_payload"):
        tokens = set(_code_tokens(_source(ADAPTERS, name)).split("\n"))
        assert not tokens & {"anthropic", "claude", "eodhd", "fmp", "requests", "httpx", "urlopen", "socket",
                             "DecryptageGenerationOrchestrator", "generate"}, name


# --------------------------------------------------------------------------
# 4. Dormance
# --------------------------------------------------------------------------

def test_no_route_no_activation_no_migration():
    names = ("ReadCanonicalDecryptageDelivery", "AcknowledgeDecryptageDelivery", "CanonicalDecryptageDelivery",
             "AckAdmissionPort")
    for path in (REPO_ROOT / "api.py", REPO_ROOT / "web-v2" / "index.html"):
        text = path.read_text(encoding="utf-8")
        assert not [n for n in names if n in text], path
    for path in (REPO_ROOT / "core" / "authz_f2_http").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "decryptage.py":
            # R1 PR 9-C3 (nominatif) : /replay et /ack invoquent les deux
            # commandes et vérifient la vue fermée ; jamais l'attestation privée.
            assert [n for n in names if n in text] == list(names[:3]), path.name
            continue
        if path.name == "isolated.py":
            assert not [n for n in names if n in text], path.name
            continue
        assert not [n for n in names if n in text] and "decryptage" not in text.lower(), path.name
    naming = set()
    for root in (REPO_ROOT / "core", REPO_ROOT / "scripts", REPO_ROOT / "ci"):
        for path in root.rglob("*.py"):
            if any(n in _code_tokens(path.read_text(encoding="utf-8")).split("\n") for n in names[:2]):
                naming.add(path.relative_to(REPO_ROOT).as_posix())
    assert naming == {"core/authz_f2/commands.py", "core/authz_f2/_adapters.py", "core/authz_f2_http/decryptage.py"}
    versions = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert len(versions) == 21 and versions[-1] == "0021_decryptage_turn_reservations.py"


def test_real_entrypoint_never_loads_the_c2_paths():
    code = ("import sys, json\nimport api\n"
            "print(json.dumps(sorted(m for m in sys.modules if 'authz' in m)))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                         env=_clean_env({"ORYX_DECRYPTAGE_F2": "1", "ORYX_F2_ENABLED": "1"}), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
