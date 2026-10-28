"""Empreinte HMAC oryx-portfolio-request-v1 et trousseau portefeuille (R1 PR 7).

Sans base de données. Les huit vecteurs normatifs sont reproduits à
l'octet près : longueur de trame, HMAC et (quand fourni) SHA-256 de la
trame. Les deux trames de référence CREATE et DELETE sont écrites ici en
littéraux d'octets, indépendamment de l'encodeur de production. Clés
FICTIVES uniquement (k1 = 00..1f, k2 = 1f..00).
"""
import hashlib
import hmac
import inspect
import struct
import uuid

import pytest

from core.authz_f2 import portfolio_fingerprint as fp
from core.auth_f2_http.csrf import CsrfKeyring
from tests.authz_f2_support import K1, K2, portfolio_keyring
from tests.f2_support import make_keyring

CREATE_FRAME = (
    b"ORYX-PORTFOLIO-WRITE\x00"
    b"\x00\x00\x00\x1a\x01oryx-portfolio-request-v1"
    b"\x00\x00\x00\x03\x01k1"
    b"\x00\x00\x00\x0c\x01test-user-a"
    b"\x00\x00\x00\x1a\x01portfolio_position_create"
    b"\x00\x00\x00%\x0111111111-1111-4111-8111-111111111111"
    b"\x00\x00\x00\x01\x00"
    b"\x00\x00\x00\x01\x00"
    b"\x00\x00\x00\x07\x01AIR.PA"
    b"\x00\x00\x00\x02\x014"
    b"\x00\x00\x00\x01\x00"
    b"\x00\x00\x00\x01\x00"
    b"\x00\x00\x00\x04\x01PEA"
)
DELETE_FRAME = (
    b"ORYX-PORTFOLIO-WRITE\x00"
    b"\x00\x00\x00\x1a\x01oryx-portfolio-request-v1"
    b"\x00\x00\x00\x03\x01k1"
    b"\x00\x00\x00\x0c\x01test-user-a"
    b"\x00\x00\x00\x1a\x01portfolio_position_delete"
    b"\x00\x00\x00%\x0133333333-3333-4333-8333-333333333333"
    b"\x00\x00\x00\x03\x0142"
    b"\x00\x00\x00\x02\x018"
    + b"\x00\x00\x00\x01\x00" * 5
)

OP_CREATE = uuid.UUID("11111111-1111-4111-8111-111111111111")
OP_UPDATE = uuid.UUID("22222222-2222-4222-8222-222222222222")
OP_DELETE = uuid.UUID("33333333-3333-4333-8333-333333333333")
CREATE = dict(user_id="test-user-a", operation_type=fp.CREATE, operation_id=OP_CREATE, position_id=None,
              expected_revision=None, ticker="AIR.PA", quantity="4", purchase_price=None, target_percent=None,
              envelope="PEA")
UPDATE = dict(user_id="test-user-a", operation_type=fp.UPDATE, operation_id=OP_UPDATE, position_id=42,
              expected_revision=7, ticker="AIR.PA", quantity="4.5", purchase_price="100.1", target_percent="20",
              envelope="CTO")
DELETE = dict(user_id="test-user-a", operation_type=fp.DELETE, operation_id=OP_DELETE, position_id=42,
              expected_revision=8, ticker=None, quantity=None, purchase_price=None, target_percent=None,
              envelope=None)

# (nom, version de clé, champs, longueur de trame, HMAC, SHA-256 de la trame ou None)
VECTORS = [
    ("create", "k1", CREATE, 190, "a1ca9277cee69dfd6f17a4d59eeeb03f45c7bed45244cae5ec91acda1551f178",
     "693a31b9d4ca8a62eb1f066b0ee887d97484169965441bbe985ad43098e7b1b9"),
    ("create_target_zero", "k1", {**CREATE, "target_percent": "0"}, 191,
     "336a1e13ddd8f733badb6ac759075ad0beec6c0fc920bd6f48cd8d0e257f0690", None),
    ("create_other_owner", "k1", {**CREATE, "user_id": "test-user-b"}, 190,
     "e8b52988265e91bab428c5d1699a0a6a5eb9541dd38bb279dd877de5faf01a48", None),
    ("create_other_operation", "k1",
     {**CREATE, "operation_id": uuid.UUID("44444444-4444-4444-8444-444444444444")}, 190,
     "93d78397ee7c88fc4fa638da24ea10e7e5ec643a9a6866c6cf142586eea13d41", None),
    ("create_k2", "k2", CREATE, 190, "b19aac16d74b057862f2442dbf06556c39b1acc8946bc1d27f0d567f6740f54a", None),
    ("update_r7", "k1", UPDATE, 202, "02f7df95ed54673383a03ba75a4bc75da2ba8ca049bc671ec3a5c20aeb589924", None),
    ("update_r8", "k1", {**UPDATE, "expected_revision": 8}, 202,
     "253c923a490c804b4cadbdd0f7696918d96b66470a7c99069bf0a8e0065d804d", None),
    ("delete_r8", "k1", DELETE, 183, "2b134527d162394f7b3dcf36f3234559c4d865d6f43004ca42769e2c276ac825",
     "a177615881ee1009af4e5a60c8000744c83ddd20e8fcc0f7ced4257259fad9ff"),
]


def test_key_fixtures_are_the_normative_fictive_keys():
    assert K1 == bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
    assert K2 == bytes(reversed(K1))


def test_reference_frames_match_the_normative_sha256_and_hmac():
    # Indépendant de l'encodeur de production : octets littéraux.
    assert len(CREATE_FRAME) == 190 and len(DELETE_FRAME) == 183
    assert hashlib.sha256(CREATE_FRAME).hexdigest() == VECTORS[0][5]
    assert hashlib.sha256(DELETE_FRAME).hexdigest() == VECTORS[-1][5]
    assert hmac.new(K1, CREATE_FRAME, hashlib.sha256).hexdigest() == VECTORS[0][4]
    assert hmac.new(K1, DELETE_FRAME, hashlib.sha256).hexdigest() == VECTORS[-1][4]
    assert fp.frame_v1(key_version="k1", **CREATE) == CREATE_FRAME
    assert fp.frame_v1(key_version="k1", **DELETE) == DELETE_FRAME


@pytest.mark.parametrize("name,key_version,fields,length,expected,sha", VECTORS, ids=[v[0] for v in VECTORS])
def test_normative_vectors_are_reproduced_byte_for_byte(name, key_version, fields, length, expected, sha):
    frame = fp.frame_v1(key_version=key_version, **fields)
    assert len(frame) == length
    if sha is not None:
        assert hashlib.sha256(frame).hexdigest() == sha
    assert fp.fingerprint(portfolio_keyring(), fp.FINGERPRINT_VERSION, key_version, **fields) == expected
    # Recalcul indépendant à partir de la trame et de la clé fictive.
    key = {"k1": K1, "k2": K2}[key_version]
    assert hmac.new(key, frame, hashlib.sha256).hexdigest() == expected


def _independent_frame(values):
    """Encodeur de référence minimal du test (u32be || 00 | 01 || UTF-8)."""
    out = b"ORYX-PORTFOLIO-WRITE\x00"
    for value in values:
        payload = b"\x00" if value is None else b"\x01" + value.encode("utf-8")
        out += struct.pack(">I", len(payload)) + payload
    return out


def _values(key_version, fields):
    order = ("user_id", "operation_type", "operation_id", "position_id", "expected_revision", "ticker", "quantity",
             "purchase_price", "target_percent", "envelope")
    return [fp.FINGERPRINT_VERSION, key_version] + [None if fields[k] is None else str(fields[k]) for k in order]


@pytest.mark.parametrize("field,other", [
    ("user_id", "test-user-a2"), ("operation_type", fp.DELETE), ("operation_id", uuid.UUID(int=1)),
    ("position_id", 43), ("expected_revision", 9), ("ticker", "AIR.PB"), ("quantity", "4.6"),
    ("purchase_price", "100.2"), ("target_percent", "21"), ("envelope", "PEA"),
])
def test_each_field_changes_the_fingerprint_in_isolation(field, other):
    keys = portfolio_keyring()
    base = fp.fingerprint(keys, fp.FINGERPRINT_VERSION, "k1", **UPDATE)
    changed = {**UPDATE, field: other}
    assert fp.frame_v1(key_version="k1", **changed) == _independent_frame(_values("k1", changed))
    assert fp.fingerprint(keys, fp.FINGERPRINT_VERSION, "k1", **changed) != base


def test_null_markers_and_zero_are_distinct():
    keys = portfolio_keyring()
    null = fp.fingerprint(keys, fp.FINGERPRINT_VERSION, "k1", **CREATE)
    zero = fp.fingerprint(keys, fp.FINGERPRINT_VERSION, "k1", **{**CREATE, "target_percent": "0"})
    empty = fp.frame_v1(key_version="k1", **{**CREATE, "target_percent": ""})
    assert null != zero
    assert b"\x00\x00\x00\x01\x00" in fp.frame_v1(key_version="k1", **CREATE)
    assert b"\x00\x00\x00\x02\x010" in fp.frame_v1(key_version="k1", **{**CREATE, "target_percent": "0"})
    assert b"\x00\x00\x00\x01\x01" in empty  # chaîne vide présente : jamais confondue avec null
    # Déplacer une valeur d'un champ voisin à l'autre change la trame.
    shifted = {**CREATE, "purchase_price": "0", "target_percent": None}
    assert fp.frame_v1(key_version="k1", **shifted) != fp.frame_v1(key_version="k1", **{**CREATE,
                                                                                         "target_percent": "0"})


def test_unicode_and_long_values_are_length_prefixed_in_utf8():
    fields = {**CREATE, "user_id": "utilisateur-é" + "x" * 300}
    frame = fp.frame_v1(key_version="k1", **fields)
    encoded = ("utilisateur-é" + "x" * 300).encode("utf-8")
    assert struct.pack(">I", len(encoded) + 1) + b"\x01" + encoded in frame
    assert frame == _independent_frame(_values("k1", fields))


def test_operation_id_is_rendered_canonically_lowercase():
    upper = uuid.UUID("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")
    frame = fp.frame_v1(key_version="k1", **{**CREATE, "operation_id": upper})
    assert b"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" in frame and b"AAAA" not in frame


@pytest.mark.parametrize("bad", [True, 1.0, b"AIR.PA", ["x"]])
def test_frame_refuses_unexpected_types(bad):
    with pytest.raises(TypeError):
        fp.frame_v1(key_version="k1", **{**CREATE, "ticker": bad})


@pytest.mark.parametrize("bad_version", ["", "K1", "-k1", "k" * 33, "k 1", "k1\n", None, 1])
def test_key_versions_follow_the_closed_grammar(bad_version):
    with pytest.raises(fp.PortfolioKeyringError):
        fp.PortfolioKeyring({bad_version: K1}, bad_version)
    with pytest.raises((ValueError, fp.UnknownPortfolioKey)):
        fp.fingerprint(portfolio_keyring(), fp.FINGERPRINT_VERSION, bad_version, **CREATE)


def test_keyring_refuses_inconsistent_configurations():
    for keys, current in (({}, "k1"), ({"k1": b"short"}, "k1"), ({"k1": K1, "k2": K1}, "k1"),
                          ({"k1": K1}, "k2"), ({"k1": bytearray(K1)}, "k1"), ([("k1", K1)], "k1")):
        with pytest.raises(fp.PortfolioKeyringError):
            fp.PortfolioKeyring(keys, current)
    keys = portfolio_keyring()
    assert "00" not in repr(keys) and repr(keys) == "PortfolioKeyring(<2>)"
    with pytest.raises(TypeError):
        __import__("pickle").dumps(keys)
    assert keys.versions() == ("k1", "k2") and keys.current_version == "k1"


def test_unknown_version_or_key_is_refused_without_fallback():
    keys = portfolio_keyring({"k2": K2}, "k2")
    with pytest.raises(fp.UnknownPortfolioKey):
        fp.fingerprint(keys, fp.FINGERPRINT_VERSION, "k1", **CREATE)  # jamais la clé courante à la place
    with pytest.raises(fp.UnknownPortfolioKey):
        fp.fingerprint(keys, "oryx-portfolio-request-v2", "k2", **CREATE)
    with pytest.raises(fp.UnknownPortfolioKey):
        fp.fingerprint(keys, None, "k2", **CREATE)


def test_rotation_keeps_old_receipts_verifiable():
    before = portfolio_keyring({"k1": K1}, "k1")
    recorded = fp.fingerprint(before, fp.FINGERPRINT_VERSION, "k1", **CREATE)
    # Étape 1 : trousseau capable de lire les deux clés, courante encore k1 ;
    # étape 3 : bascule de la courante vers k2 ; l'ancienne reste lisible.
    for during in (portfolio_keyring({"k1": K1, "k2": K2}, "k1"), portfolio_keyring({"k1": K1, "k2": K2}, "k2")):
        assert fp.same_fingerprint(fp.fingerprint(during, fp.FINGERPRINT_VERSION, "k1", **CREATE), recorded)
    # Redémarrage / restauration avec trousseau conservé : même résultat.
    restored = fp.PortfolioKeyring({"k2": bytes(K2), "k1": bytes(K1)}, "k2")
    assert fp.same_fingerprint(fp.fingerprint(restored, fp.FINGERPRINT_VERSION, "k1", **CREATE), recorded)
    # Ancienne clé retirée : refus fermé (jamais un rejeu sous k2).
    with pytest.raises(fp.UnknownPortfolioKey):
        fp.fingerprint(portfolio_keyring({"k2": K2}, "k2"), fp.FINGERPRINT_VERSION, "k1", **CREATE)


def test_comparison_is_constant_time_and_strict():
    good = VECTORS[0][4]
    assert fp.same_fingerprint(good, good)
    assert not fp.same_fingerprint(good, good.upper())
    assert not fp.same_fingerprint(good, good[:-1])
    assert not fp.same_fingerprint(good, None)
    assert "compare_digest" in inspect.getsource(fp.same_fingerprint)


def test_portfolio_keys_are_independent_of_f2_and_csrf_test_keys():
    f2 = make_keyring()
    keys = portfolio_keyring()
    from core.auth_f2.keys import PURPOSES
    for purpose in PURPOSES:
        for version in f2.versions(purpose):
            assert not keys.has_key_value(f2.key(purpose, version))
    assert not keys.has_key_value(bytes([0xC0]) * 32)
    assert isinstance(CsrfKeyring({"c1": bytes([0xC0]) * 32}, "c1"), CsrfKeyring)
