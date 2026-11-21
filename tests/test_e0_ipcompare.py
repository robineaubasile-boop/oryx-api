"""E0 — contrat de comparaison IP pré- et post-signature (spécification V0.2-R3 § 2.3, § 2.6, § 6.2).

Références indépendantes : vectors/commitment_v1.json et vectors/ipcompare_v1.json
(MAC calculés par `openssl mac` brut, issues attendues écrites à la main).
"""
import hashlib
import hmac
import json
import pickle

import pytest

from tests import e0_support as s

import e0_ipclass as ic  # noqa: E402
import e0_ipcompare as cmp  # noqa: E402

COMMIT = s.vectors("commitment_v1.json")
IPCMP = s.vectors("ipcompare_v1.json")
KEY = bytes.fromhex(COMMIT["key_hex"])


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    s.block_external_network(monkeypatch)


def test_rfc4231_reference_vectors_with_the_standard_library():
    """Ancre indépendante : la bibliothèque HMAC elle-même (RFC 4231)."""
    for vector in COMMIT["rfc4231"]:
        assert hmac.new(bytes.fromhex(vector["key_hex"]), bytes.fromhex(vector["data_hex"]),
                        hashlib.sha256).hexdigest() == vector["hmac_sha256"]


@pytest.mark.parametrize("vector", COMMIT["engagements"], ids=lambda v: v["domain"][-6:] + ":" + v["ip_input"])
def test_commitment_vectors(vector):
    assert cmp.canonical_ip(vector["ip_input"]) == vector["ip_canonical"]
    assert cmp.mac(KEY, vector["domain"], vector["ip_input"]) == vector["mac"]


def test_domains_are_separated():
    pre = cmp.mac(KEY, "oryx-e0-ipcompare/pre/v1", "192.0.0.9")
    post = cmp.mac(KEY, "oryx-e0-ipcompare/post/v1", "192.0.0.9")
    assert pre != post
    with pytest.raises(ValueError):
        cmp.mac(KEY, "oryx-e0-ipcompare/other/v1", "192.0.0.9")
    with pytest.raises(ValueError):
        cmp.mac(KEY[:16], "oryx-e0-ipcompare/pre/v1", "192.0.0.9")


def test_key_id_vector():
    assert cmp.key_id(KEY) == COMMIT["key_id"]
    assert cmp.KEY_ID.fullmatch(COMMIT["key_id"])


# --------------------------------------------------------------------------
# Pré-signature : détection, validation, comparaison (::detect_validate_compare)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("vector", IPCMP["pre_signature"], ids=lambda v: v["name"])
def test_detect_validate_compare_vectors(vector):
    result = cmp.pre_signature_check(vector["key_values"], vector["mac_values"], IPCMP["candidate_ip"])
    assert {"entered": result.entered, "effect": result.effect, "reason": result.reason} == vector["expected"]


def test_absent_engagement_is_a_no_op():
    result = cmp.pre_signature_check([], [], "192.0.0.9")
    assert result == cmp.PreCheck(False, cmp.EFFECT_NONE, None)


def test_step_two_can_never_promote_nor_sign():
    """Toutes les issues possibles : `none` ou `refuse`, jamais une promotion."""
    effects = set()
    for vector in IPCMP["pre_signature"]:
        effects.add(cmp.pre_signature_check(vector["key_values"], vector["mac_values"], "192.0.0.9").effect)
    assert effects == {cmp.EFFECT_NONE, cmp.EFFECT_REFUSE}
    source = (s.BENCH_PY / "e0_ipcompare.py").read_text(encoding="utf-8").split('"""', 2)[2]
    for forbidden in ("sign(", "CLASS_V", '"V"', "promote", "e0_ed25519"):
        assert forbidden not in source, forbidden


def test_self_consistent_forged_engagement_never_promotes_i_or_f_to_v():
    """Un adversaire qui engage l'adresse qu'il forge obtient au mieux « aucun
    effet » : la classe vient de l'étape 1 seule."""
    forged_ip = "203.0.113.9"
    key = bytes(32)
    commit = cmp.mac(key, "oryx-e0-ipcompare/pre/v1", forged_ip)
    view = {"scheme": "https", "entry_host": s.PUBLIC_HOST, "entry_port_explicit": False,
            "expected_entry_host": s.PUBLIC_HOST, "asn": None,
            "headers": [("cf-connecting-ip", forged_ip), ("x-e0-cmp-pre-key", key.hex()),
                        ("x-e0-cmp-pre-mac", commit)]}
    assert ic.classify_request(view).cls == "F"
    marked = dict(view, headers=view["headers"] + [("cf-worker", "atk.e0-zone.example")])
    assert ic.classify_request(marked).cls == "I-b"
    assert cmp.pre_signature_check([key.hex()], [commit], forged_ip).effect == cmp.EFFECT_NONE


def test_mismatch_is_constant_time_compare(monkeypatch):
    calls = []
    original = cmp.hmac.compare_digest
    monkeypatch.setattr(cmp.hmac, "compare_digest", lambda a, b: calls.append(1) or original(a, b))
    mac_other = cmp.mac(KEY, "oryx-e0-ipcompare/pre/v1", "192.0.0.10")
    mac_ok = cmp.mac(KEY, "oryx-e0-ipcompare/pre/v1", "192.0.0.9")
    assert cmp.pre_signature_check([KEY.hex()], [mac_ok + "," + mac_other], "192.0.0.9").effect == "none"
    assert len(calls) == 2  # toutes les valeurs comparées, sans court-circuit


# --------------------------------------------------------------------------
# Post-signature
# --------------------------------------------------------------------------

@pytest.mark.parametrize("vector", IPCMP["post_signature"], ids=lambda v: v["name"])
def test_post_signature_comparison_vectors(vector):
    result = cmp.compare_post(vector["origin"]["eng"], vector["origin"]["key_id"], vector["witness"]["eng"],
                              vector["witness"]["key_id"])
    assert result["match"] == vector["expected"]


def test_divergent_key_id_is_inconclusive_never_mismatch():
    eng = cmp.mac(KEY, "oryx-e0-ipcompare/post/v1", "2001:1::1")
    result = cmp.compare_post(eng, cmp.key_id(KEY), eng, cmp.key_id(bytes(32)))
    assert result == {"match": "inconclusive", "basis": "cmp_key_post_id_divergent"}


def test_post_key_is_never_serialisable_nor_printed():
    key = cmp.PostKey(KEY, expected_id=COMMIT["key_id"])
    assert COMMIT["key_hex"] not in repr(key) and COMMIT["key_hex"] not in str(key)
    with pytest.raises(TypeError):
        pickle.dumps(key)
    with pytest.raises(TypeError):
        json.dumps(key)
    assert key.eng_post("2001:1::1") == cmp.mac(KEY, "oryx-e0-ipcompare/post/v1", "2001:1::1")
    with pytest.raises(ValueError):
        cmp.PostKey(KEY, expected_id="cmp-0000000000000000")


def test_engagement_never_contains_the_address_or_key():
    eng = cmp.PostKey(KEY).eng_post("192.0.0.9")
    assert len(eng) == 64 and "192.0.0.9" not in eng and COMMIT["key_hex"] not in eng
