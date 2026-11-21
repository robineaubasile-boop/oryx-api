"""E0 — attestation v1 : AT-L01 à AT-L04, PA2 à PA15, bornes temporelles (spécification V0.2-R3 § 5.1, § 6.2).

Références indépendantes : vectors/attestation_v1.json (charges utiles écrites
champ par champ et signatures produites par `openssl pkeyutl` brut, hors des
modules testés). Le croisement WebCrypto (D6, AT-L01 côté Worker) relève du
job web (point de contrôle PC-2).
"""
import json

import pytest

from tests import e0_support as s

import e0_attest as at  # noqa: E402
import e0_ed25519 as ed  # noqa: E402
import e0_keysets as ks  # noqa: E402
import e0_version as ver  # noqa: E402

VECTORS = s.vectors("attestation_v1.json")
NOW = 1763200000000
W, S = 30000, 5000
TID = "/e0/guarded/AAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    s.block_external_network(monkeypatch)


def keysets(phase="2"):
    return ks.KeySets.from_environment(phase, s.keyset_environment(phase))


def verifier(phase="2", *, now=NOW, sets=None, store=None):
    return at.PhaseVerifier(phase=phase, keysets=sets or keysets(phase), public_host=s.PUBLIC_HOST, w_ms=W,
                            s_ms=S, nonce_store=store if store is not None else at.InMemoryNonceStore(),
                            clock_ms=lambda: now)


def evaluate(value, *, phase="2", method="GET", host=s.ORIGIN_HOST, target=TID, v=None, **kw):
    return (v or verifier(phase, **kw)).evaluate([value] if isinstance(value, str) else value, method=method,
                                                 host_values=[host] if isinstance(host, str) else host,
                                                 raw_target=target)


def signed(seed=s.RFC_SEED_2, **fields):
    fields.setdefault("ts_ms", NOW)
    return s.attestation(seed=seed, **fields)


# --------------------------------------------------------------------------
# AT-L01 : fixture croisée reproduite octet pour octet
# --------------------------------------------------------------------------

@pytest.mark.parametrize("vector", VECTORS["attestations"], ids=lambda v: v["name"])
def test_at_l01_vector_payload_header_and_signature_byte_for_byte(vector):
    payload = at.encode_payload(vector["fields"])
    assert payload == vector["payload_ascii"].encode("ascii")
    assert at.b64url_encode(payload) == vector["payload_b64url"]
    seed = {"TEST 2": s.RFC_SEED_1A, "TEST 3": s.RFC_SEED_2}[vector["rfc8032_seed"]]
    signature = ed.sign(s.private_der(seed), at.signed_message(payload))
    assert signature.hex() == vector["signature_hex"]
    assert at.encode_header(payload, signature) == vector["header"]
    parsed_payload, fields, parsed_signature = at.parse_header(vector["header"])
    assert (parsed_payload, fields, parsed_signature) == (payload, vector["fields"], signature)
    public = at.b64url_decode_canonical(vector["public_key_b64url"])
    assert ed.verify(public, at.signed_message(payload), signature)
    assert ks.derive_kid(vector["fields"]["aud"], public) == vector["fields"]["kid"]


def test_at_l01_signed_message_uses_the_domain_prefix():
    assert ver.ATTESTATION_DOMAIN == b"oryx-e0-attestation/v1\n" == VECTORS["domain"].replace("\\n", "\n").encode()
    payload = at.encode_payload(VECTORS["attestations"][0]["fields"])
    assert at.signed_message(payload) == b"oryx-e0-attestation/v1\n" + payload


def test_at_l01_valid_vectors_are_accepted_by_the_phase_verifier():
    for vector in VECTORS["attestations"]:
        fields = vector["fields"]
        phase = "1a" if fields["aud"] == "oryx-e0-1a" else "2"
        sets = ks.KeySets(phase, (ks.KeyRef(fields["kid"], fields["aud"],
                                             at.b64url_decode_canonical(vector["public_key_b64url"]), "current"),),
                          (), frozenset())
        v = at.PhaseVerifier(phase=phase, keysets=sets, public_host=fields["h"], w_ms=W, s_ms=S,
                             nonce_store=at.InMemoryNonceStore(), clock_ms=lambda: int(fields["ts"]))
        result = v.evaluate([vector["header"]], method=fields["m"], host_values=[fields["hi"]], raw_target=fields["t"])
        if fields["ip"] in ("192.0.0.9", "192.0.0.10", "2001:1::1"):
            assert result.att_policy_accepted, (vector["name"], result.refusal)
            assert result.att_crypto_valid and result.key_origin == "current"


# --------------------------------------------------------------------------
# AT-L02 : violations PA2 à PA7, PA14, PA15
# --------------------------------------------------------------------------

def test_nominal_acceptance():
    result = evaluate(signed())
    assert result.att_policy_accepted and result.refusal is None
    assert result.att_present and result.att_crypto_valid and result.key_origin == "current"


def test_pa2_algorithm_and_version_are_fixed():
    good = signed()
    for value in ("v2." + good[3:], "V1." + good[3:], good[3:], "v1" + good[2:].replace(".", ":", 1)):
        assert evaluate(value).refusal == "grammar"
    fields = s.attestation_fields(ts_ms=NOW)
    fields["v"] = "2"
    with pytest.raises(at.AttestationFormatError):
        at.encode_payload(fields)


@pytest.mark.parametrize("name, request_overrides, code", [
    ("method", {"method": "POST"}, "bind_method"),
    ("hi", {"host": "e0-origin-other.up.railway.app"}, "bind_hi"),
    ("hi_double", {"host": [s.ORIGIN_HOST, s.ORIGIN_HOST]}, "bind_hi"),
    ("hi_absent", {"host": []}, "bind_hi"),
    ("target", {"target": "/e0/guarded/BBBBBBBBBBBBBBBBBBBBBB"}, "bind_target"),
    ("target_query", {"target": TID + "?x=1"}, "bind_target"),
    ("target_encoded", {"target": "/e0/guarded%2FAAAAAAAAAAAAAAAAAAAAAA"}, "bind_target"),
])
def test_pa3_bindings(name, request_overrides, code):
    assert evaluate(signed(), **request_overrides).refusal == code


def test_pa4_audience_of_another_phase_is_refused_even_if_crypto_valid():
    old = signed(seed=s.RFC_SEED_1A, aud="oryx-e0-1a")
    result = evaluate(old)
    assert result.refusal == "aud_not_current_phase"
    assert result.att_crypto_valid is True and result.key_origin == "audit"
    assert result.att_policy_accepted is False


@pytest.mark.parametrize("age, code", [(-S - 1, "ts_future"), (-S, None), (0, None), (W, None),
                                       (W + 1, "ts_expired")])
def test_pa5_time_window_bounds(age, code):
    """âge = maintenant − ts : accepté si et seulement si −S ≤ âge ≤ W."""
    assert evaluate(signed(ts_ms=NOW - age)).refusal == code


def test_pa6_replay_in_boot_is_refused():
    store = at.InMemoryNonceStore()
    v = verifier(store=store)
    value = signed()
    assert evaluate(value, v=v).att_policy_accepted
    second = evaluate(value, v=v)
    assert second.refusal == "replay_in_boot" and not second.att_policy_accepted and second.att_crypto_valid


def test_pa7_non_ambiguity():
    value = signed()
    assert evaluate([]).refusal == "att_absent"
    assert evaluate([value, value]).refusal == "att_multiple"
    assert evaluate(value + ", " + value).refusal == "grammar"
    assert evaluate(value + " ").refusal == "grammar"
    long_target = "/e0/guarded/" + "a" * 2100
    fields = s.attestation_fields(ts_ms=NOW, t=long_target)
    with pytest.raises(at.AttestationFormatError):
        at.encode_payload(fields)
    assert evaluate("v1." + "A" * 4200 + "." + "A" * 86).refusal == "grammar"


@pytest.mark.parametrize("h, code", [
    ("e0-edge.acct.workers.dev", "bind_h"),
    ("atk.e0-zone.example", "bind_h"),
    ("abc123-e0-edge.acct.workers.dev", "bind_h"),
])
def test_pa14_entry_host_must_be_exactly_the_frozen_public_host(h, code):
    assert evaluate(signed(h=h)).refusal == code


@pytest.mark.parametrize("h", ["app.e0-zone.example.", "APP.e0-zone.example", "app.e0-zone.example:443",
                               "192.0.2.1", "localhost"])
def test_pa14_non_canonical_entry_hosts_cannot_even_be_encoded(h):
    with pytest.raises(at.AttestationFormatError):
        at.encode_payload(s.attestation_fields(ts_ms=NOW, h=h))


def test_pa15_phase_1a_verifier_refuses_the_phase_2_audience_and_key():
    v = verifier("1a")
    assert evaluate(signed(), v=v).refusal == "aud_not_current_phase"
    accepted = evaluate(signed(seed=s.RFC_SEED_1A, aud="oryx-e0-1a"), v=v)
    assert accepted.att_policy_accepted


def test_pa15_kid_of_another_audience_is_a_grammar_error():
    fields = s.attestation_fields(ts_ms=NOW, kid=s.kid_of(s.RFC_SEED_1A, "oryx-e0-1a"))
    with pytest.raises(at.AttestationFormatError):
        at.encode_payload(fields)


def test_pa15_retired_kid_recognised_by_audit_never_accepted():
    retired = s.key_record(s.RFC_SEED_2, "oryx-e0-2")
    current = s.key_record(s.RFC_SEED_OTHER, "oryx-e0-2")
    sets = ks.KeySets.from_environment("2", {"E0_VERIFY_KEYS": json.dumps([current]),
                                              "E0_AUDIT_PUBKEYS": json.dumps([retired]),
                                              "E0_RETIRED_KIDS": json.dumps([retired["kid"]])})
    result = evaluate(signed(seed=s.RFC_SEED_2), sets=sets)
    assert result.refusal == "kid_retired"
    assert result.att_crypto_valid is True and result.key_origin == "audit" and not result.att_policy_accepted
    assert evaluate(signed(seed=s.RFC_SEED_OTHER), sets=sets).att_policy_accepted


def test_unknown_kid_and_forged_signature():
    stranger = "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f8"
    assert evaluate(signed(seed=stranger)).refusal == "kid_unknown"
    forged = s.attestation(seed=s.RFC_SEED_OTHER, ts_ms=NOW, kid=s.kid_of(s.RFC_SEED_2, "oryx-e0-2"))
    result = evaluate(forged)
    assert result.refusal == "sig_invalid" and result.att_crypto_valid is False and result.key_origin is None


@pytest.mark.parametrize("ip", ["203.0.113.7", "2001:db8::1", "10.0.0.1", "2a06:98c0:3600::103", "240.0.0.1",
                                "::ffff:192.0.0.9"])
def test_signed_non_v_address_is_refused_after_signature(ip):
    result = evaluate(signed(ip=ip))
    assert result.refusal == "ip_not_v" and result.att_crypto_valid is True


def test_non_canonical_ip_cannot_be_encoded():
    for ip in ("2001:0001::1", "192.0.0.09", "[2001:1::1]"):
        with pytest.raises(at.AttestationFormatError):
            at.encode_payload(s.attestation_fields(ts_ms=NOW, ip=ip))


# --------------------------------------------------------------------------
# AT-L03 : mutations, troncatures, base64url non canonique
# --------------------------------------------------------------------------

def test_at_l03_every_payload_byte_mutation_is_refused():
    value = signed()
    payload, fields, signature = at.parse_header(value)
    v = verifier()
    for index in range(len(payload)):
        mutated = bytearray(payload)
        mutated[index] ^= 0x01
        candidate = at.encode_header(bytes(mutated), signature) if len(mutated) else value
        result = evaluate(candidate, v=v)
        assert not result.att_policy_accepted, index
        assert not (result.att_crypto_valid and bytes(mutated) != payload), index


def test_at_l03_every_signature_byte_mutation_is_refused():
    value = signed()
    payload, _, signature = at.parse_header(value)
    v = verifier()
    for index in range(64):
        mutated = bytearray(signature)
        mutated[index] ^= 0x80
        result = evaluate(at.encode_header(payload, bytes(mutated)), v=v)
        assert not result.att_policy_accepted and not result.att_crypto_valid, index


def test_at_l03_every_field_substitution_breaks_the_signature():
    value = signed()
    payload, fields, signature = at.parse_header(value)
    replacements = {"aud": "oryx-e0-1a", "ts": str(NOW + 1), "nonce": s.nonce(), "ip": "192.0.0.10", "m": "POST",
                    "h": "other.e0-zone.example", "hi": "e0-origin-x.up.railway.app", "t": "/e0/observe"}
    for name, replacement in replacements.items():
        changed = dict(fields, **{name: replacement})
        if name == "aud":
            changed["kid"] = s.kid_of(s.RFC_SEED_1A, "oryx-e0-1a")
        forged = at.encode_header(at.encode_payload(changed), signature)
        result = evaluate(forged)
        assert result.att_crypto_valid is False and not result.att_policy_accepted, name


def test_at_l03_truncations_and_trailing_bytes():
    value = signed()
    payload, _, signature = at.parse_header(value)
    for cut in range(1, len(value)):
        assert not evaluate(value[:cut]).att_policy_accepted
    with pytest.raises(at.AttestationFormatError):
        at.decode_payload(payload + b"x")
    with pytest.raises(at.AttestationFormatError):
        at.decode_payload(payload[:-1])
    with pytest.raises(at.AttestationFormatError):
        at.decode_payload(b"01:1;" + payload[4:])


def test_at_l03_non_canonical_base64url_is_refused():
    value = signed()
    prefix, body, sig = value.split(".")
    for bad_body in (body + "=", "+" + body[1:], "/" + body[1:], body + "A", body[:-1] + "="):
        assert evaluate(".".join((prefix, bad_body, sig))).refusal == "grammar"
    last = sig[-1]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    sibling = alphabet[alphabet.index(last) ^ 1]  # même 6 bits utiles sauf bit de bourrage
    assert evaluate(".".join((prefix, body, sig[:-1] + sibling))).refusal == "grammar"
    with pytest.raises(at.AttestationFormatError):
        at.b64url_decode_canonical("QQ=")
    with pytest.raises(at.AttestationFormatError):
        at.b64url_decode_canonical("QR")  # bits de bourrage non nuls


# --------------------------------------------------------------------------
# AT-L04 : ordre PA8, consommation du nonce après succès seulement
# --------------------------------------------------------------------------

def test_at_l04_nonce_consumed_only_after_every_check():
    store = at.InMemoryNonceStore()
    v = verifier(store=store)
    value = signed()
    for overrides in ({"method": "POST"}, {"host": "e0-origin-x.up.railway.app"}, {"target": "/e0/observe"}):
        assert not evaluate(value, v=v, **overrides).att_policy_accepted
        assert len(store) == 0
    expired = verifier(now=NOW + W + 1, store=store)
    assert evaluate(value, v=expired).refusal == "ts_expired" and len(store) == 0
    assert evaluate(value, v=v).att_policy_accepted and len(store) == 1
    assert evaluate(value, v=v).refusal == "replay_in_boot"


def test_at_l04_signature_is_checked_before_time_and_bindings():
    forged = s.attestation(seed=s.RFC_SEED_OTHER, ts_ms=NOW - 10 * W, kid=s.kid_of(s.RFC_SEED_2, "oryx-e0-2"))
    assert evaluate(forged, method="POST").refusal == "sig_invalid"


def test_refusal_codes_are_exactly_the_specified_set():
    assert ver.REFUSAL_CODES == ("att_absent", "att_multiple", "grammar", "aud_not_current_phase", "kid_unknown",
                                 "kid_retired", "sig_invalid", "ts_future", "ts_expired", "bind_method", "bind_h",
                                 "bind_hi", "bind_target", "ip_not_v", "replay_in_boot")


def test_evaluation_record_never_carries_issued_evidence_nor_the_ip():
    result = evaluate(signed())
    record = result.record()
    assert "att_issued_evidence" not in json.dumps(record)
    assert s.SIGNABLE_V4 not in json.dumps(record)
    assert result.take_signed_ip() == s.SIGNABLE_V4 and result.take_signed_ip() is None


def test_version_constants():
    assert ver.PROTOCOL == "E0-V0.1c" and ver.FORMAT == 1
    assert ver.PHASE_AUDIENCE == {"1a": "oryx-e0-1a", "1b": "oryx-e0-2", "1c": "oryx-e0-2", "2": "oryx-e0-2"}
    assert ver.ATTESTATION_FIELDS == ("v", "aud", "kid", "ts", "nonce", "ip", "c", "m", "h", "hi", "t")
    assert ver.ATTESTATION_HEADER == "x-oryx-e0-attestation"
    protocol = (s.REPO_ROOT / ver.PROTOCOL_DOCUMENT).read_bytes()
    import hashlib
    assert hashlib.sha256(protocol).hexdigest() == ver.PROTOCOL_SHA256
