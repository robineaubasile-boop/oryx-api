"""R1 PR 9-B1 — empreinte HMAC des intentions, trousseau Décrypter,
admission structurelle, jeton de bail et capacité : contrôles PURS (sans
base de données).

Vecteurs déterministes avec des clés FICTIVES (jamais une clé réelle) :
chaque vecteur est recalculé ici par une implémentation INDÉPENDANTE de la
trame (struct + hmac), puis comparé à la valeur figée. Couvre : premier
calcul, retry identique, intention divergente, rotation de clé, ancienne
clé, clé absente, version inconnue, NULL distinct de "", espaces et Unicode
(aucune normalisation implicite), ticker normalisé, UUID canonique, bornes
de taille, secret jamais exposé (repr, str, pickle, copie, asdict,
encodeur JSON FastAPI, AuthorizationOutcome complet).
"""
import copy
import dataclasses
import hashlib
import hmac
import json
import pickle
import struct
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_fingerprint as d
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.portfolio_fingerprint import PortfolioKeyring
from core.authz_f2.runner import AUTHORIZED, AuthorizationOutcome
from core.authz_f2.principal import AuthorizedPrincipal

K1 = bytes(range(32))
K2 = bytes(range(31, -1, -1))
TURN = uuid.UUID("123e4567-e89b-42d3-a456-426614174000")
OTHER_TURN = uuid.UUID("123e4567-e89b-42d3-a456-426614174001")
VECTORS = {
    "base": "a2f2e0991778c42837c0682ebe0253bd9a9e98eca20a440600805ac8edda45eb",
    "empty_method": "0e978688e40998b194c5a9ccf48ed1345e4bffab33413ada5a9fc4c35d62cdd3",
    "method": "846e6fe2e502a3b535c31d0da8ffdb654885d7e57e0602fe08ffee906c4ad14d",
    "key_d2": "ebeed5fb7e5c75c33784a0b86a4a9c484463b1941d326d797c4412919c2138b0",
    "unicode_nfc": "803c158d98d424cb92c6a9c5e22c849d0c538ebdb985fa25bba85bf389eda795",
    "unicode_nfd": "2755d7925001446227dcdf225d7ce872c472867193afd2032d35a56ee0a95f80",
    "eszett": "b2b756e9c1b89dc0cde765648eba0708e5aeb21e77c0df223fdcede8ebb52f48",
    "other_user": "6b6541a85d439d9e526e5b6bde85fcfa72fae824b08407d293002b1fedaa7614",
    "other_turn": "795784ec6359a44b6b04b207ac5185efd45518fd74050fc244cc4446d3ca01c0",
}
# Paramètres de chaque vecteur : saisies BRUTES (avant normalisation locale).
INPUTS = {
    "base": {},
    "empty_method": {"last_method_id": ""},
    "method": {"last_method_id": "construction_these"},
    "key_d2": {"key_version": "d2"},
    "unicode_nfc": {"question": "Qu'est-ce que la résilience ?", "context": "Contexte\n  avec  espaces\tinternes"},
    "unicode_nfd": {"question": "Qu'est-ce que la résilience ?", "context": "Contexte\n  avec  espaces\tinternes"},
    "eszett": {"ticker_input": "straße"},
    "other_user": {"user_id": "user-b"},
    "other_turn": {"turn": OTHER_TURN},
}
BASE = {"key_version": "d1", "user_id": "user-a", "conversation_key": "conv-1", "turn": TURN,
        "ticker_input": " mc.pa ", "question": "  Pourquoi ?  ", "context": "", "last_method_id": None}
FRAME_V1_BASE_HEX = (
    "4f5259582d444543525950544147452d5245534552564154494f4e000000001f016f7279782d646563727970746167652d72657365"
    "72766174696f6e2d7631000000030164310000000701757365722d610000000701636f6e762d310000000b0164656372797074616765"
    "000000250131323365343536372d653839622d343264332d613435362d34323636313431373430303000000002013100000006014d43"
    "2e50410000000b01506f757271756f69203f00000001010000000100")


def keyring(current="d1"):
    return d.DecryptageKeyring({"d1": K1, "d2": K2}, current)


def _independent(params) -> str:
    """Implémentation INDÉPENDANTE de oryx-decryptage-reservation-v1."""
    def field(value):
        payload = b"\x00" if value is None else b"\x01" + value.encode("utf-8")
        return struct.pack(">I", len(payload)) + payload
    values = ["oryx-decryptage-reservation-v1", params["key_version"], params["user_id"], params["conversation_key"],
              "decryptage", str(params["turn"]), "1", params["ticker_input"].strip().upper(),
              params["question"].strip(), params["context"].strip(), params["last_method_id"]]
    frame = b"ORYX-DECRYPTAGE-RESERVATION\x00" + b"".join(field(v) for v in values)
    key = {"d1": K1, "d2": K2}[params["key_version"]]
    return hmac.new(key, frame, hashlib.sha256).hexdigest()


def _module(params, keys=None) -> str:
    q, ctx = d.normalize_texts(params["question"], params["context"])
    return d.fingerprint(keys or keyring(), d.FINGERPRINT_VERSION, params["key_version"], user_id=params["user_id"],
                         conversation_key=params["conversation_key"], source_user_turn_id=params["turn"],
                         ticker=d.normalize_ticker(params["ticker_input"]), question=q, context=ctx,
                         last_method_id=params["last_method_id"])


# --------------------------------------------------------------------------
# Trame et vecteurs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(VECTORS))
def test_known_vectors_are_reproduced_by_an_independent_implementation(name):
    params = {**BASE, **INPUTS[name]}
    assert _independent(params) == VECTORS[name]
    assert _module(params) == VECTORS[name]
    assert d.FINGERPRINT_RE.fullmatch(VECTORS[name])


def test_frame_bytes_are_exactly_the_frozen_canonicalization():
    frame = d.frame_v1(key_version="d1", user_id="user-a", conversation_key="conv-1", source_user_turn_id=TURN,
                       ticker="MC.PA", question="Pourquoi ?", context="", last_method_id=None)
    assert frame.hex() == FRAME_V1_BASE_HEX
    assert frame.startswith(b"ORYX-DECRYPTAGE-RESERVATION\x00")
    assert d.FIELDS == ("fingerprint_version", "fingerprint_key_version", "user_id", "conversation_key", "surface",
                        "source_user_turn_id", "delivery_ordinal", "ticker", "question", "context", "last_method_id")
    assert d.FINGERPRINT_VERSION == "oryx-decryptage-reservation-v1"


def test_every_vector_is_distinct_and_retry_is_deterministic():
    assert len(set(VECTORS.values())) == len(VECTORS)
    assert _module(BASE) == _module(dict(BASE)) == VECTORS["base"]
    # Mêmes saisies après normalisation locale : même intention.
    assert _module({**BASE, "ticker_input": "MC.PA", "question": "Pourquoi ?"}) == VECTORS["base"]
    # Espaces INTÉRIEURS, casse de la question, ticker différent : autre intention.
    for change in ({"question": "Pourquoi  ?"}, {"question": "pourquoi ?"}, {"ticker_input": "MC.P"},
                   {"context": "x"}, {"conversation_key": "conv-2"}):
        assert _module({**BASE, **change}) != VECTORS["base"], change


def test_null_and_empty_last_method_id_are_distinct_and_never_normalized():
    assert VECTORS["empty_method"] != VECTORS["base"]
    assert _module({**BASE, "last_method_id": " construction_these"}) != VECTORS["method"]


def test_no_implicit_unicode_normalization_and_local_upper_only():
    assert VECTORS["unicode_nfc"] != VECTORS["unicode_nfd"]
    assert d.normalize_ticker("straße") == "STRASSE"  # str.upper() local, rien d'autre
    assert d.normalize_ticker("　mc.pa　") == "MC.PA"  # strip() Python (blancs Unicode)
    assert d.normalize_texts("  a  b  ", "\n c \n") == ("a  b", "c")


def test_rotation_uses_the_recorded_key_version_and_refuses_a_missing_key():
    rotated = keyring("d2")
    assert rotated.current_version == "d2"
    assert _module(BASE, rotated) == VECTORS["base"]  # ancienne version encore lisible
    assert _module({**BASE, "key_version": "d2"}, rotated) == VECTORS["key_d2"]
    only_new = d.DecryptageKeyring({"d2": K2}, "d2")
    with pytest.raises(d.UnknownDecryptageKey):
        _module(BASE, only_new)  # ancienne clé retirée : refus fermé, jamais de repli
    with pytest.raises(d.UnknownDecryptageKey):
        d.fingerprint(keyring(), "oryx-decryptage-reservation-v2", "d1", user_id="u", conversation_key="k",
                      source_user_turn_id=TURN, ticker="A", question="", context="", last_method_id=None)
    with pytest.raises(d.UnknownDecryptageKey):
        d.fingerprint(keyring(), None, "d1", user_id="u", conversation_key="k", source_user_turn_id=TURN,
                      ticker="A", question="", context="", last_method_id=None)


def test_constant_time_comparison_requires_a_canonical_recorded_value():
    assert d.same_fingerprint(VECTORS["base"], VECTORS["base"])
    for bad in (VECTORS["base"].upper(), VECTORS["base"][:-1], None, b"x", VECTORS["key_d2"]):
        assert not d.same_fingerprint(VECTORS["base"], bad)


def test_the_portfolio_and_decryptage_keyrings_are_distinct_types_without_fallback():
    portfolio = PortfolioKeyring({"d1": K1}, "d1")
    with pytest.raises(TypeError):
        d.fingerprint(portfolio, d.FINGERPRINT_VERSION, "d1", user_id="u", conversation_key="k",
                      source_user_turn_id=TURN, ticker="A", question="", context="", last_method_id=None)
    assert not issubclass(d.DecryptageKeyring, PortfolioKeyring) and not issubclass(PortfolioKeyring,
                                                                                     d.DecryptageKeyring)


@pytest.mark.parametrize("keys,current", [
    ({}, "d1"), ({"D1": K1}, "D1"), ({"d1": K1[:31]}, "d1"), ({"d1": K1, "d2": K1}, "d1"), ({"d1": K1}, "d2"),
    ({"d1": "x" * 32}, "d1"), ([("d1", K1)], "d1")])
def test_invalid_decryptage_keyrings_are_refused(keys, current):
    with pytest.raises(d.DecryptageKeyringError):
        d.DecryptageKeyring(keys, current)


def test_keyring_never_reveals_a_key():
    keys = keyring()
    assert repr(keys) == "DecryptageKeyring(<2>)" and K1.hex() not in repr(keys)
    with pytest.raises(TypeError):
        pickle.dumps(keys)
    assert keys.has_key_value(K2) and not keys.has_key_value(bytes(32))


# --------------------------------------------------------------------------
# Admission structurelle (avant tout accès SQL)
# --------------------------------------------------------------------------

def reserve(**changes):
    base = dict(conversation_key="conv-1", client_turn_id=TURN, ticker_input="MC.PA", question="Q", context="",
                last_method_id=None)
    return c.ReserveDecryptageTurn(**{**base, **changes})


def test_bounds_are_accepted_exactly_at_the_limit_and_never_truncated():
    command = reserve(conversation_key="k" * 200, ticker_input="  " + "a" * 32 + "  ",
                      question="é" * 8192, context="x" * (32 * 1024), last_method_id="m" * 64)
    assert command.ticker == "A" * 32 and len(command.texts[0].encode()) == 16 * 1024
    reserve(question="q" * (16 * 1024), context="c" * (32 * 1024))  # somme = 48 Kio
    reserve(question="  " + "q" * (16 * 1024) + "  ")  # borne appliquée à la valeur signée (strippée)


@pytest.mark.parametrize("changes", [
    {"conversation_key": ""}, {"conversation_key": "k" * 201}, {"conversation_key": " conv"},
    {"conversation_key": "conv\x00"}, {"conversation_key": "\ud800"}, {"conversation_key": None},
    {"client_turn_id": str(TURN)}, {"client_turn_id": None},
    {"ticker_input": "   "}, {"ticker_input": "a" * 33}, {"ticker_input": "ß" * 17}, {"ticker_input": None},
    {"ticker_input": "MC\x00"},
    {"question": "q" * (16 * 1024 + 1)}, {"question": "é" * 8193}, {"question": None}, {"question": "\ud800"},
    {"context": "c" * (32 * 1024 + 1)}, {"context": b"x"},
    {"question": "q" * (16 * 1024), "context": "c" * (32 * 1024 + 1)},
    {"last_method_id": "m" * 65}, {"last_method_id": 1}, {"last_method_id": "m\x00"},
])
def test_structural_admission_refuses_before_any_sql(changes):
    with pytest.raises(ValueError):
        reserve(**changes)


def test_reserve_command_carries_no_server_authority():
    fields = {f.name for f in dataclasses.fields(c.ReserveDecryptageTurn)}
    assert fields == {"conversation_key", "client_turn_id", "ticker_input", "question", "context", "last_method_id"}
    forbidden = {"user_id", "owner", "lease_epoch", "token", "lease_token", "lease_expires_at", "attempts_used",
                 "request_fingerprint", "fingerprint", "fingerprint_key_version", "state", "blocked_reason", "level"}
    assert not fields & forbidden
    failure = {f.name for f in dataclasses.fields(c.RecordDecryptageAttemptFailure)}
    assert failure == {"reservation_id", "lease_epoch", "lease_token", "failure"}
    assert not {"force", "reset", "unlock", "override", "block"} & {
        n.lower() for n in dir(c) if "Decryptage" in n}


@pytest.mark.parametrize("changes", [
    {"reservation_id": str(TURN)}, {"lease_epoch": 0}, {"lease_epoch": True}, {"lease_epoch": 2**63},
    {"lease_token": b"\x00" * 32}, {"lease_token": "secret"}, {"failure": "other"}, {"failure": None}])
def test_failure_command_requires_an_exact_server_capability(changes):
    base = dict(reservation_id=TURN, lease_epoch=1, lease_token=dr.LeaseToken.generate(),
                failure=dr.GENERATION_UNAVAILABLE)
    c.RecordDecryptageAttemptFailure(**base)
    with pytest.raises(ValueError):
        c.RecordDecryptageAttemptFailure(**{**base, **changes})


# --------------------------------------------------------------------------
# Politique, jeton et capacité
# --------------------------------------------------------------------------

def test_frozen_lease_policy():
    assert dr.LEASE_DURATION == timedelta(seconds=360) and dr.MAX_ATTEMPTS == 3
    assert dr.BACKOFFS == {1: timedelta(seconds=30), 2: timedelta(seconds=120)}
    assert dr.STATES == ("reserved", "blocked", "finalized")
    assert dr.BLOCKED_REASONS == ("legacy_collision", "attempts_exhausted")
    with pytest.raises(ValueError):
        dr.backoff_after(3)


def test_lease_token_has_256_bits_and_is_stored_only_as_a_digest():
    tokens = {dr.LeaseToken.generate().reveal() for _ in range(64)}
    assert len(tokens) == 64 and all(len(t) == 32 for t in tokens)
    token = dr.LeaseToken(bytes(32))
    assert token.digest() == hashlib.sha256(b"ORYX-DECRYPTAGE-LEASE-TOKEN\x00" + bytes(32)).hexdigest()
    assert token.matches(token.digest()) and not token.matches(dr.LeaseToken(b"\x01" * 32).digest())
    assert not token.matches(None) and not token.matches(token.digest().upper()[:63])
    with pytest.raises(ValueError):
        dr.LeaseToken(bytes(31))


def _lease(token):
    at = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    return dr.DecryptageLease(uuid.uuid4(), 2, token, at, "conv-1", TURN, 1)


def _assert_hidden(text: str, token):
    raw = token.reveal()
    for form in (raw.hex(), raw.hex().upper(), repr(raw), raw.decode("latin-1"), token.digest()):
        assert form not in text


def test_the_token_never_appears_in_any_representation_of_the_full_outcome():
    token = dr.LeaseToken.generate()
    granted = dr.LeaseGranted(_lease(token), 2)
    principal = AuthorizedPrincipal("user-a", uuid.uuid4(), uuid.uuid4(), "supabase_email_sign_in")
    outcome = AuthorizationOutcome(AUTHORIZED, granted, principal)
    failure = c.RecordDecryptageAttemptFailure.from_lease(granted.lease, dr.GENERATION_UNAVAILABLE)
    for obj in (token, granted.lease, granted, outcome, failure):
        _assert_hidden(repr(obj), token)
        _assert_hidden(str(obj), token)
        _assert_hidden(f"{obj}", token)
        _assert_hidden(f"{obj!r}", token)
    assert "<secret>" in repr(outcome)


@pytest.mark.parametrize("serialize", [
    pickle.dumps, copy.copy, copy.deepcopy, dataclasses.asdict, json.dumps,
    lambda o: json.dumps(o, default=vars), lambda o: json.dumps(o, default=str)])
def test_no_serialization_path_exposes_the_token(serialize):
    token = dr.LeaseToken.generate()
    granted = dr.LeaseGranted(_lease(token), 2)
    outcome = AuthorizationOutcome(AUTHORIZED, granted, None)
    for obj in (token, granted.lease, granted, outcome):
        try:
            produced = serialize(obj)
        except (TypeError, ValueError):
            continue  # refus : rien n'est produit
        text = produced if isinstance(produced, str) else repr(produced)
        _assert_hidden(text, token)


def test_fastapi_encoders_refuse_or_hide_the_capability():
    from fastapi.encoders import jsonable_encoder
    token = dr.LeaseToken.generate()
    outcome = AuthorizationOutcome(AUTHORIZED, dr.LeaseGranted(_lease(token), 2), None)
    for obj in (outcome, outcome.value, outcome.value.lease, token):
        try:
            encoded = jsonable_encoder(obj)
        except (TypeError, ValueError):
            continue
        _assert_hidden(json.dumps(encoded, default=str), token)


def test_capability_and_views_are_immutable_and_validated():
    token = dr.LeaseToken.generate()
    lease = _lease(token)
    with pytest.raises(dataclasses.FrozenInstanceError):
        lease.lease_epoch = 3
    with pytest.raises(TypeError):
        hash(token)
    naive = datetime(2026, 10, 31, 12, 0)
    for bad in (dict(lease_epoch=0), dict(token=token.reveal()), dict(lease_expires_at=naive),
                dict(delivery_ordinal=2)):
        with pytest.raises(ValueError):
            dataclasses.replace(lease, **bad)
    with pytest.raises(ValueError):
        dr.LeaseGranted(lease, 4)
    with pytest.raises(ValueError):
        dr.ReservationBlocked(uuid.uuid4(), "other")
    with pytest.raises(ValueError):
        dr.AttemptFailureRecorded(uuid.uuid4(), "reserved", None, None, 1)
    with pytest.raises(ValueError):
        dr.AttemptFailureRecorded(uuid.uuid4(), "finalized", None, None, 1)
