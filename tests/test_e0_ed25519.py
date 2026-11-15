"""E0 — adaptateur Ed25519 vers OpenSSL : validations D1 à D9 (spécification V0.2-R3 § 3.4).

Références INDÉPENDANTES de l'adaptateur : vecteurs RFC 8032 § 7.1 recopiés
(vectors/attestation_v1.json) et commandes `openssl` brutes. OpenSSL absent ou
sans Ed25519 : ÉCHEC, jamais `skip` (D1, D9).
"""
import base64
import os
import shutil
import subprocess

import pytest

from tests import e0_support as s

import e0_ed25519 as ed  # noqa: E402  (chemin ajouté par e0_support)

RFC = {v["name"]: v for v in s.vectors("attestation_v1.json")["rfc8032"]}
L = ed.GROUP_ORDER_L
P = ed.FIELD_P


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    s.block_external_network(monkeypatch)


# --------------------------------------------------------------------------
# D1, D2 : présence, version, capacité
# --------------------------------------------------------------------------

def test_d1_openssl_present_and_recent_enough():
    capability = ed.capability(refresh=True)
    major, minor, patch = (int(x) for x in capability["version"].split("."))
    assert (major, minor, patch) >= (1, 1, 1)
    assert capability["openssl"].startswith("OpenSSL ")


def test_d2_ed25519_capability_reported_by_the_tool():
    out = subprocess.run(["openssl", "list", "-public-key-algorithms"], capture_output=True, check=True).stdout
    assert b"ED25519" in out.upper()
    assert ed.capability()["ed25519"] is True


# --------------------------------------------------------------------------
# D3 : vecteurs RFC 8032 § 7.1
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["TEST 1", "TEST 2", "TEST 3", "TEST SHA(abc)"])
def test_d3_rfc8032_public_key_derivation(name):
    vector = RFC[name]
    assert ed.public_from_private(ed.pkcs8_from_seed(bytes.fromhex(vector["secret"]))).hex() == vector["public"]


@pytest.mark.parametrize("name", ["TEST 2", "TEST 3", "TEST SHA(abc)"])
def test_d3_rfc8032_signatures_byte_for_byte_and_deterministic(name):
    vector = RFC[name]
    key = ed.pkcs8_from_seed(bytes.fromhex(vector["secret"]))
    message = bytes.fromhex(vector["message"])
    first, second = ed.sign(key, message), ed.sign(key, message)
    assert first.hex() == vector["signature"] and first == second
    assert ed.verify(bytes.fromhex(vector["public"]), message, first) is True


def test_d3_rfc8032_test1_empty_message_is_refused_not_faked():
    """Limite consignée : `pkeyutl -rawin` (OpenSSL 3.0) refuse un message vide ;
    l'adaptateur refuse fermement au lieu de contourner."""
    vector = RFC["TEST 1"]
    assert vector["message"] == "" and vector["openssl_cli_signable"] is False
    with pytest.raises(ed.Ed25519Error):
        ed.sign(ed.pkcs8_from_seed(bytes.fromhex(vector["secret"])), b"")
    assert ed.verify(bytes.fromhex(vector["public"]), b"", bytes.fromhex(vector["signature"])) is False


def test_d3_adapter_agrees_with_the_raw_openssl_command(tmp_path):
    vector = RFC["TEST 3"]
    message = b"oryx-e0-attestation/v1\nindependent"
    key_file = tmp_path / "k.der"
    key_file.write_bytes(ed.PKCS8_PREFIX + bytes.fromhex(vector["secret"]))
    msg_file = tmp_path / "m"
    msg_file.write_bytes(message)
    raw = subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-keyform", "DER", "-inkey", str(key_file),
                          "-in", str(msg_file)], capture_output=True, check=True).stdout
    key_file.unlink()
    assert ed.sign(ed.pkcs8_from_seed(bytes.fromhex(vector["secret"])), message) == raw


# --------------------------------------------------------------------------
# D4 : rejets canoniques à la vérification
# --------------------------------------------------------------------------

def _good():
    vector = RFC["TEST 2"]
    return bytes.fromhex(vector["public"]), bytes.fromhex(vector["message"]), bytes.fromhex(vector["signature"])


def test_d4_s_greater_or_equal_to_l_is_rejected_before_openssl(monkeypatch):
    public, message, signature = _good()
    s_value = int.from_bytes(signature[32:], "little")
    malleable = signature[:32] + (s_value + L).to_bytes(32, "little")
    equal_l = signature[:32] + L.to_bytes(32, "little")
    calls = []
    original = ed._run
    monkeypatch.setattr(ed, "_run", lambda argv, **kw: calls.append(argv) or original(argv, **kw))
    assert ed.signature_is_canonical(malleable) is False and ed.signature_is_canonical(equal_l) is False
    assert ed.verify(public, message, malleable) is False
    assert ed.verify(public, message, equal_l) is False
    assert not any("-verify" in argv for argv in calls)


def test_d4_non_canonical_r_and_a_are_rejected():
    public, message, signature = _good()
    non_canonical_y = (1 + P).to_bytes(32, "little")  # y = 1 encodé y + p (≥ p)
    assert ed.verify(public, message, non_canonical_y + signature[32:]) is False
    assert ed.verify(non_canonical_y, message, signature) is False
    with pytest.raises(ed.Ed25519Error):
        ed.check_public_key(non_canonical_y)
    all_ones = b"\xff" * 31 + b"\x7f"  # y = 2^255 - 1 ≥ p
    assert ed.verify(all_ones, message, signature) is False


@pytest.mark.parametrize("length", [0, 31, 33, 63, 65])
def test_d4_wrong_lengths_are_rejected(length):
    public, message, signature = _good()
    blob = (signature * 2)[:length]
    if length in (63, 65):
        assert ed.verify(public, message, blob) is False
    assert ed.verify(blob, message, signature) is False


def test_d4_strict_containers():
    seed = bytes.fromhex(RFC["TEST 2"]["secret"])
    with pytest.raises(ed.Ed25519Error):
        ed.check_pkcs8(ed.PKCS8_PREFIX + seed + b"\x00")
    with pytest.raises(ed.Ed25519Error):
        ed.check_pkcs8(b"\x30" + ed.PKCS8_PREFIX[1:] + seed[:-1])
    with pytest.raises(ed.Ed25519Error):
        ed.public_from_spki(ed.SPKI_PREFIX + b"\x00" * 31)
    with pytest.raises(ed.Ed25519Error):
        ed.pkcs8_from_seed(seed[:31])


# --------------------------------------------------------------------------
# D5 : messages et clés altérés
# --------------------------------------------------------------------------

def test_d5_one_mutated_byte_of_message_signature_or_key_is_rejected():
    vector = RFC["TEST SHA(abc)"]
    public, message, signature = (bytes.fromhex(vector[k]) for k in ("public", "message", "signature"))
    assert ed.verify(public, message, signature) is True
    for index in (0, 17, len(message) - 1):
        mutated = bytearray(message)
        mutated[index] ^= 0x01
        assert ed.verify(public, bytes(mutated), signature) is False
    for index in (0, 31, 32, 63):
        mutated = bytearray(signature)
        mutated[index] ^= 0x01
        assert ed.verify(public, message, bytes(mutated)) is False
    other = bytes.fromhex(RFC["TEST 3"]["public"])
    assert ed.verify(other, message, signature) is False


# --------------------------------------------------------------------------
# D7 : PKCS#8 (interopérabilité ; le croisement WebCrypto relève du job web)
# --------------------------------------------------------------------------

def test_d7_generated_keys_are_pkcs8_v1_and_round_trip():
    private, public = ed.generate()
    assert len(private) == 48 and private.startswith(ed.PKCS8_PREFIX)
    assert ed.public_from_private(private) == public
    signature = ed.sign(private, b"oryx-e0-attestation/v1\nround-trip")
    assert ed.verify(public, b"oryx-e0-attestation/v1\nround-trip", signature) is True
    other_private, _ = ed.generate()
    assert other_private != private


# --------------------------------------------------------------------------
# D8 : non-fuite de la clé privée
# --------------------------------------------------------------------------

def test_d8_private_key_never_in_argv_never_on_disk(monkeypatch, tmp_path):
    seed = bytes.fromhex(RFC["TEST 3"]["secret"])
    private = ed.pkcs8_from_seed(seed)
    forms = [seed, seed.hex().encode(), private, base64.b64encode(private), base64.urlsafe_b64encode(private),
             base64.b64encode(seed)]
    seen_argv, written = [], []
    original_run, original_scratch_write = ed._run, ed._Scratch.write

    def spy_run(argv, **kwargs):
        seen_argv.append(list(argv))
        return original_run(argv, **kwargs)

    def spy_write(self, name, data):
        written.append((name, data))
        return original_scratch_write(self, name, data)

    monkeypatch.setattr(ed, "_run", spy_run)
    monkeypatch.setattr(ed._Scratch, "write", spy_write)
    scratch_parent = "/dev/shm" if os.path.isdir("/dev/shm") else None
    before = set(os.listdir(scratch_parent)) if scratch_parent else set()
    signature = ed.sign(private, b"oryx-e0-attestation/v1\nd8")
    ed.public_from_private(private)
    assert ed.verify(ed.public_from_private(private), b"oryx-e0-attestation/v1\nd8", signature)
    for argv in seen_argv:
        joined = " ".join(argv).encode()
        assert not any(form in joined for form in forms)
    for _, data in written:
        assert not any(form in data for form in forms)
    assert all(name in ("message", "signature", "public.der") for name, _ in written)
    if scratch_parent:
        leftovers = {n for n in set(os.listdir(scratch_parent)) - before if n.startswith("e0-ed25519-")}
        assert leftovers == set()


def test_d8_signing_passes_the_key_through_an_anonymous_pipe(monkeypatch):
    calls = []
    original = ed._run
    monkeypatch.setattr(ed, "_run", lambda argv, **kw: calls.append((argv, kw)) or original(argv, **kw))
    ed.sign(ed.pkcs8_from_seed(bytes.fromhex(RFC["TEST 2"]["secret"])), b"x")
    argv, kwargs = calls[-1]
    inkey = argv[argv.index("-inkey") + 1]
    assert inkey.startswith("/dev/fd/") and kwargs["pass_fds"] and kwargs["feed"]


# --------------------------------------------------------------------------
# D9 : refus fermé, aucun repli
# --------------------------------------------------------------------------

def test_d9_absent_openssl_is_a_closed_refusal(monkeypatch):
    monkeypatch.setattr(ed.shutil, "which", lambda *a, **k: None)
    with pytest.raises(ed.Ed25519Unavailable):
        ed.capability(refresh=True)
    with pytest.raises(ed.Ed25519Unavailable):
        ed.verify(*_good())
    with pytest.raises(ed.Ed25519Unavailable):
        ed.sign(ed.pkcs8_from_seed(b"\x01" * 32), b"x")
    monkeypatch.undo()
    assert ed.capability(refresh=True)["ed25519"] is True


def test_d9_tool_without_ed25519_or_too_old_is_refused(monkeypatch, tmp_path):
    fake = tmp_path / "openssl"
    fake.write_text("#!/bin/sh\nif [ \"$1\" = version ]; then echo 'OpenSSL 3.0.13 30 Jan 2024'; "
                    "else echo 'RSA'; fi\n")
    fake.chmod(0o755)
    monkeypatch.setattr(ed.shutil, "which", lambda *a, **k: str(fake))
    with pytest.raises(ed.Ed25519Unavailable, match="sans Ed25519"):
        ed.capability(refresh=True)
    fake.write_text("#!/bin/sh\necho 'OpenSSL 1.1.0l 10 Sep 2019'\n")
    with pytest.raises(ed.Ed25519Unavailable, match="1.1.1"):
        ed.capability(refresh=True)
    monkeypatch.undo()
    assert ed.capability(refresh=True)["ed25519"] is True


def test_d9_no_python_crypto_fallback_in_the_adapter():
    source = (s.BENCH_PY / "e0_ed25519.py").read_text(encoding="utf-8")
    for forbidden in ("import cryptography", "from cryptography", "nacl", "ed25519 import", "hashlib.sha512",
                      "pow(", "def _point", "def scalarmult"):
        assert forbidden not in source, forbidden
    assert shutil.which("openssl") is not None
