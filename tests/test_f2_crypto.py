"""Conformité du protocole F2-C2-V1 (C.2.1 parties IV et V) — R1 PR 2.

Les valeurs attendues V1-V4 sont celles FIXÉES par C.2.1 (recopiées ici,
jamais calculées par le code testé). Elles sont vérifiées trois fois :

  1. core.auth_f2.crypto (implémentation livrée) ;
  2. une référence indépendante écrite dans ce fichier : FRAME par
     concaténation d'octets et HMAC-SHA256 reconstruit selon la RFC 2104
     avec hashlib seul (ni le module hmac, ni core.auth_f2) ;
  3. OpenSSL en ligne de commande (openssl dgst -mac HMAC), sur les octets
     de la référence indépendante.

Puis les tests négatifs obligatoires : autre candidat, autre session,
autre génération, generation_no différent, mauvaise version de clé, zéros
initiaux du code OTP, empreintes mal formées, encodages non canoniques.
"""
import hashlib
import shutil
import subprocess
import uuid

import pytest

from core.auth_f2 import crypto
from core.auth_f2.crypto import CryptoContractError, Redacted
from core.auth_f2.keys import Keyring, KeyringError, UnknownKeyVersion
from tests.f2_support import make_keyring

# Entrées officielles C.2.1 §18 (fictives).
C = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
K_CANDIDATE = b"\x01" * 32
K_DERIVE = b"\x02" * 32
K_ACTIVE = b"\x03" * 32
K_OTP = b"\x04" * 32
K_DERIVE_VERSION = "test-v1"
O = uuid.UUID("11111111-1111-4111-8111-111111111111")
G = uuid.UUID("22222222-2222-4222-8222-222222222222")
S = uuid.UUID("33333333-3333-4333-8333-333333333333")
R = uuid.UUID("44444444-4444-4444-8444-444444444444")
Q = uuid.UUID("55555555-5555-4555-8555-555555555555")
N = 1
CODE_GENERATION = 1
PURPOSE = "sign_in"
OTP_CODE = "00123456"

# Résultats officiels C.2.1 §19 à §22.
V1 = "39dfc8162dcd992d77278a29a2873f31054c60208f8dc301e09650af56481a57"
V2_HEX = "94c4c3247dc10abe76d3c8a0265e27039f3b2b38913d4cf4077f4cad8aabad84"
V2_B64 = "lMTDJH3BCr5208igJl4nA587KziRPUz0B39MrYqrrYQ"
V3 = "f4b54af5a545376686d8cc691c5f2fe8c5053054ac2f0d20fbc9aac7a6ffaaf7"
V4 = "68ddf7b9c167057385cb4e46af75e31b70ede16221e7f8b7b3deb20e4457a1b7"


# --------------------------------------------------------------------------
# Référence indépendante (RFC 2104 + concaténation d'octets)
# --------------------------------------------------------------------------

def ref_hmac_sha256(key: bytes, message: bytes) -> bytes:
    block = 64
    if len(key) > block:
        key = hashlib.sha256(key).digest()
    key = key.ljust(block, b"\x00")
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key) + message).digest()
    return hashlib.sha256(bytes(b ^ 0x5C for b in key) + inner).digest()


def ref_frame(tag: bytes, *fields: bytes) -> bytes:
    out = b"ORYX-F2-C2" + b"\x00" + len(tag).to_bytes(2, "big") + tag
    for f in fields:
        out += len(f).to_bytes(4, "big") + f
    return out


def ref_messages():
    """(clé, message) des quatre vecteurs, construits sans core.auth_f2."""
    candidate = (K_CANDIDATE, ref_frame(b"candidate", O.bytes, G.bytes, C))
    prk = ref_hmac_sha256(K_DERIVE, C)
    info = ref_frame(b"active-derive", b"f2-c2-hkdf-sha256-v1", K_DERIVE_VERSION.encode(), O.bytes, G.bytes,
                     S.bytes, R.bytes, N.to_bytes(4, "big"))
    active = (prk, info + b"\x01")
    a = ref_hmac_sha256(*active)
    session = (K_ACTIVE, ref_frame(b"active", S.bytes, R.bytes, a))
    otp = (K_OTP, ref_frame(b"otp", Q.bytes, CODE_GENERATION.to_bytes(4, "big"), PURPOSE.encode(), OTP_CODE.encode()))
    return {"prk": (K_DERIVE, C), "V1": candidate, "V2": active, "V3": session, "V4": otp}


def openssl_hmac(key: bytes, message: bytes) -> str:
    out = subprocess.run(["openssl", "dgst", "-sha256", "-mac", "HMAC", "-macopt", f"hexkey:{key.hex()}"],
                         input=message, capture_output=True, check=True).stdout.decode()
    return out.strip().split()[-1]


# --------------------------------------------------------------------------
# 1. Vecteurs officiels
# --------------------------------------------------------------------------

def test_v1_candidate_hash():
    assert crypto.candidate_hash(K_CANDIDATE, O, G, C) == V1


def test_v2_active_secret_and_base64url():
    active = crypto.derive_active_secret(K_DERIVE, K_DERIVE_VERSION, O, G, S, R, N, C)
    assert active.hex() == V2_HEX and len(active) == 32
    assert crypto.b64url_encode(active) == V2_B64 and len(V2_B64) == 43
    assert crypto.b64url_decode_secret(V2_B64) == active


def test_v3_active_secret_hash():
    assert crypto.active_secret_hash(K_ACTIVE, S, R, bytes.fromhex(V2_HEX)) == V3


def test_v4_otp_hmac():
    assert crypto.otp_hmac(K_OTP, Q, CODE_GENERATION, PURPOSE, OTP_CODE) == V4


def test_independent_reference_reproduces_the_official_vectors():
    messages = ref_messages()
    assert ref_hmac_sha256(*messages["V1"]).hex() == V1
    assert ref_hmac_sha256(*messages["V2"]).hex() == V2_HEX
    assert ref_hmac_sha256(*messages["V3"]).hex() == V3
    assert ref_hmac_sha256(*messages["V4"]).hex() == V4


def test_openssl_reproduces_the_official_vectors():
    assert shutil.which("openssl"), "OpenSSL en ligne de commande est requis par ce test de conformité"
    messages = ref_messages()
    prk = bytes.fromhex(openssl_hmac(*messages["prk"]))
    assert openssl_hmac(*messages["V1"]) == V1
    assert openssl_hmac(prk, messages["V2"][1]) == V2_HEX
    assert openssl_hmac(*messages["V3"]) == V3
    assert openssl_hmac(*messages["V4"]) == V4


def test_frame_bytes_match_the_independent_encoding():
    messages = ref_messages()
    assert crypto.frame("candidate", O.bytes, G.bytes, C) == messages["V1"][1]
    assert crypto.frame("otp", Q.bytes, crypto.uint32_field(1), b"sign_in", b"00123456") == messages["V4"][1]
    assert crypto.frame("x") == b"ORYX-F2-C2\x00\x00\x01x"
    assert crypto.frame("x", b"") == b"ORYX-F2-C2\x00\x00\x01x\x00\x00\x00\x00"


# --------------------------------------------------------------------------
# 2. Différences de contexte
# --------------------------------------------------------------------------

OTHER = uuid.UUID("66666666-6666-4666-8666-666666666666")


@pytest.mark.parametrize("operation,candidate,secret", [
    (OTHER, G, C), (O, OTHER, C), (O, G, bytes(32)), (O, G, C[:-1] + b"\x20"),
])
def test_candidate_hash_changes_with_any_input(operation, candidate, secret):
    assert crypto.candidate_hash(K_CANDIDATE, operation, candidate, secret) != V1


@pytest.mark.parametrize("change", ["operation", "candidate", "session", "generation", "generation_no",
                                    "key_version", "key", "secret"])
def test_active_secret_changes_with_any_input(change):
    args = dict(k_derive=K_DERIVE, k_derive_version=K_DERIVE_VERSION, operation_id=O, candidate_id=G,
                session_id=S, generation_id=R, generation_no=N, secret=C)
    args.update({
        "operation": {"operation_id": OTHER}, "candidate": {"candidate_id": OTHER}, "session": {"session_id": OTHER},
        "generation": {"generation_id": OTHER}, "generation_no": {"generation_no": 2},
        "key_version": {"k_derive_version": "test-v2"}, "key": {"k_derive": b"\x05" * 32}, "secret": {"secret": bytes(32)},
    }[change])
    assert crypto.derive_active_secret(**args).hex() != V2_HEX


@pytest.mark.parametrize("session,generation", [(OTHER, R), (S, OTHER), (R, S)])
def test_active_hash_is_bound_to_session_and_generation(session, generation):
    assert crypto.active_secret_hash(K_ACTIVE, session, generation, bytes.fromhex(V2_HEX)) != V3


def test_otp_leading_zeros_are_significant_and_codes_are_never_integers():
    assert crypto.otp_hmac(K_OTP, Q, 1, "sign_in", "00123457") != V4
    assert crypto.otp_hmac(K_OTP, Q, 2, "sign_in", OTP_CODE) != V4
    assert crypto.otp_hmac(K_OTP, Q, 1, "email_linking", OTP_CODE) != V4
    assert crypto.otp_hmac(K_OTP, OTHER, 1, "sign_in", OTP_CODE) != V4
    for bad in ("123456", "0123456", "000123456", 123456, "0012345\n", "００１２３４５６", "0012345٦", " 0012345",
                "0012345a", ""):
        assert not crypto.is_valid_otp_code(bad)
        with pytest.raises(CryptoContractError):
            crypto.otp_hmac(K_OTP, Q, 1, "sign_in", bad)
    with pytest.raises(CryptoContractError):
        crypto.otp_hmac(K_OTP, Q, 1, "login", OTP_CODE)


def test_generation_number_and_uuid_are_distinct_fields():
    """L'UUID de génération R et le numéro N ne sont pas interchangeables."""
    assert crypto.uint32_field(1) == b"\x00\x00\x00\x01"
    for bad in (-1, 2 ** 32, True, 1.0, "1"):
        with pytest.raises(CryptoContractError):
            crypto.uint32_field(bad)
    with pytest.raises(CryptoContractError):
        crypto.derive_active_secret(K_DERIVE, K_DERIVE_VERSION, O, G, S, str(R), N, C)


# --------------------------------------------------------------------------
# 3. Formats, versions, clés
# --------------------------------------------------------------------------

def test_malformed_hashes_are_never_equal():
    assert crypto.hashes_equal(V1, V1)
    assert not crypto.hashes_equal(V1, V3)
    for bad in (V1.upper(), V1[:-1], V1 + "0", V1[:-1] + "g", None, bytes.fromhex(V1), " " + V1[1:]):
        assert not crypto.hashes_equal(bad, bad)
        assert not crypto.hashes_equal(V1, bad)


@pytest.mark.parametrize("value", [
    V2_B64[:-1] + "R",  # bits de remplissage non nuls : non canonique
    V2_B64 + "=", V2_B64[:-1], V2_B64 + "A", V2_B64.replace("l", "+", 1), V2_B64.replace("l", "/", 1),
    " " + V2_B64[1:], None, b"x" * 43,
])
def test_transported_secret_decoding_is_strict(value):
    with pytest.raises(CryptoContractError):
        crypto.b64url_decode_secret(value)


def test_derivation_version_format_and_unknown_protocols():
    assert crypto.derivation_version("test-v1") == "f2-c2-hkdf-sha256-v1:test-v1"
    assert crypto.parse_derivation_version("f2-c2-hkdf-sha256-v1:test-v1") == "test-v1"
    for bad in ("f2-hkdf-v1", "f2-c2-hkdf-sha256-v2:test-v1", "f2-c2-hkdf-sha256-v1:", "f2-c2-hkdf-sha256-v1:a b",
                "f2-c2-hkdf-sha256-v1:a:b", None):
        with pytest.raises(CryptoContractError):
            crypto.parse_derivation_version(bad)


def test_keys_and_secrets_have_exact_lengths():
    with pytest.raises(CryptoContractError):
        crypto.candidate_hash(b"\x01" * 31, O, G, C)
    with pytest.raises(CryptoContractError):
        crypto.candidate_hash(K_CANDIDATE, O, G, C[:31])
    with pytest.raises(CryptoContractError):
        crypto.frame("é")
    with pytest.raises(CryptoContractError):
        crypto.frame("")
    with pytest.raises(CryptoContractError):
        crypto.abuse_scope_hash(K_OTP, "mail", "x@y")
    assert len(crypto.new_secret()) == 32 and crypto.new_secret() != crypto.new_secret()


def test_keyring_resolves_exact_versions_without_substitution():
    keyring = make_keyring({p: ("t1", "t2") for p in ("candidate", "derive", "active", "otp", "invitation", "abuse")},
                           {p: "t2" for p in ("candidate", "derive", "active", "otp", "invitation", "abuse")})
    assert keyring.key("derive", "t1") != keyring.key("derive", "t2")
    assert keyring.current_version("derive") == "t2"
    with pytest.raises(UnknownKeyVersion):
        keyring.key("derive", "t0")
    with pytest.raises(UnknownKeyVersion):
        keyring.key("derive", None)
    assert "t1" not in repr(keyring) and keyring.key("derive", "t1").hex() not in repr(keyring)


def test_keyring_rejects_shared_or_malformed_keys():
    base = {p: {"v1": bytes([i + 1]) * 32} for i, p in enumerate(
        ("candidate", "derive", "active", "otp", "invitation", "abuse"))}
    current = {p: "v1" for p in base}
    Keyring(base, current)
    shared = dict(base, otp={"v1": base["derive"]["v1"]})
    with pytest.raises(KeyringError):
        Keyring(shared, current)
    with pytest.raises(KeyringError):
        Keyring(dict(base, otp={"v1": b"\x09" * 16}), current)
    with pytest.raises(KeyringError):
        Keyring(dict(base, otp={"v 1": b"\x09" * 32}), dict(current, otp="v 1"))
    with pytest.raises(KeyringError):
        Keyring(base, dict(current, otp="v2"))
    with pytest.raises(KeyringError):
        Keyring({p: base[p] for p in list(base)[:5]}, current)


def test_redacted_never_reveals_its_value():
    secret = Redacted(V2_B64)
    assert V2_B64 not in repr(secret) and V2_B64 not in str(secret) and V2_B64 not in f"{secret}"
    assert secret.reveal() == V2_B64 and secret == Redacted(V2_B64) and secret != Redacted(V2_B64[:-1] + "A")
    with pytest.raises(TypeError):
        hash(secret)
    import pickle
    with pytest.raises(TypeError):
        pickle.dumps(secret)
