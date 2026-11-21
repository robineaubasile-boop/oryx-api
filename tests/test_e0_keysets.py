"""E0 — séparation des clés courantes et des clés historiques d'audit (spécification V0.2-R3 § 3.5, § 6.2)."""
import json

import pytest

from tests import e0_support as s

import e0_attest as at  # noqa: E402
import e0_keysets as ks  # noqa: E402

NOW = 1763200000000


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    s.block_external_network(monkeypatch)


def rec(seed, aud):
    return s.key_record(seed, aud)


def env(current, audit=(), retired=()):
    return {"E0_VERIFY_KEYS": json.dumps(list(current)), "E0_AUDIT_PUBKEYS": json.dumps(list(audit)),
            "E0_RETIRED_KIDS": json.dumps(list(retired))}


def verify(sets, value, phase="2"):
    v = at.PhaseVerifier(phase=phase, keysets=sets, public_host=s.PUBLIC_HOST, w_ms=30000, s_ms=5000,
                         nonce_store=at.InMemoryNonceStore(), clock_ms=lambda: NOW)
    return v.evaluate([value], method="GET", host_values=[s.ORIGIN_HOST],
                      raw_target="/e0/guarded/AAAAAAAAAAAAAAAAAAAAAA")


def test_retired_key_is_recognised_by_audit_but_never_accepted():
    sets = ks.KeySets.from_environment("2", s.keyset_environment("2"))
    old = s.attestation(seed=s.RFC_SEED_1A, aud="oryx-e0-1a", ts_ms=NOW)
    result = verify(sets, old)
    assert result.att_crypto_valid is True and result.key_origin == "audit"
    assert result.att_policy_accepted is False and result.refusal == "aud_not_current_phase"


def test_no_historical_key_regains_acceptance_power():
    """Une clé de même audience que la phase, présente seulement à l'audit, ne
    peut jamais être acceptée — même non listée comme retirée."""
    sets = ks.KeySets.from_environment("2", env([rec(s.RFC_SEED_OTHER, "oryx-e0-2")],
                                                [rec(s.RFC_SEED_2, "oryx-e0-2")]))
    kid = s.kid_of(s.RFC_SEED_2, "oryx-e0-2")
    assert sets.lookup(kid).origin == "audit"
    assert sets.accepting(kid) is None
    result = verify(sets, s.attestation(seed=s.RFC_SEED_2, ts_ms=NOW))
    assert result.att_crypto_valid and result.key_origin == "audit"
    assert result.refusal == "kid_unknown" and not result.att_policy_accepted


def test_accepting_never_returns_an_audit_key():
    sets = ks.KeySets.from_environment("2", s.keyset_environment("2"))
    retired = s.kid_of(s.RFC_SEED_1A, "oryx-e0-1a")
    current = s.kid_of(s.RFC_SEED_2, "oryx-e0-2")
    assert sets.accepting(retired) is None and sets.lookup(retired).origin == "audit"
    assert sets.accepting(current).origin == "current"
    assert sets.is_retired(retired) and not sets.is_retired(current)
    assert not any(name for name in dir(sets) if "accept" in name and name != "accepting")


@pytest.mark.parametrize("build", [
    lambda: env([]),                                                                    # aucune clé courante
    lambda: env([rec(s.RFC_SEED_1A, "oryx-e0-1a")]),                                    # audience d'une autre phase
    lambda: env([rec(s.RFC_SEED_2, "oryx-e0-2")], [rec(s.RFC_SEED_2, "oryx-e0-2")]),    # kid dans les deux ensembles
    lambda: env([rec(s.RFC_SEED_2, "oryx-e0-2")], [], [s.kid_of(s.RFC_SEED_2, "oryx-e0-2")]),  # retiré courant
    lambda: env([rec(s.RFC_SEED_2, "oryx-e0-2")], [], [s.kid_of(s.RFC_SEED_1A, "oryx-e0-1a")]),  # retiré hors audit
])
def test_inconsistent_key_sets_are_refused(build):
    with pytest.raises(ks.KeySetError):
        ks.KeySets.from_environment("2", build())


def test_phase_1a_and_phase_2_key_sets_are_disjoint_by_audience():
    with pytest.raises(ks.KeySetError):
        ks.KeySets.from_environment("1a", s.keyset_environment("2"))
    with pytest.raises(ks.KeySetError):
        ks.KeySets.from_environment("2", s.keyset_environment("1a"))
    for phase in ("1b", "1c", "2"):
        assert ks.KeySets.from_environment(phase, s.keyset_environment("2")).audience == "oryx-e0-2"


@pytest.mark.parametrize("mutate", [
    lambda r: dict(r, kid=r["kid"][:-1] + ("0" if r["kid"][-1] != "0" else "1")),  # kid non dérivé
    lambda r: dict(r, pub=r["pub"] + "A"),                                             # base64url invalide
    lambda r: dict(r, pub=r["pub"][:-1]),                                              # longueur
    lambda r: dict(r, aud="oryx-e0-3"),                                                # audience inconnue
    lambda r: dict(r, extra="x"),                                                      # champ en trop
    lambda r: {k: v for k, v in r.items() if k != "aud"},                               # champ manquant
])
def test_malformed_key_entries_are_refused(mutate):
    entry = mutate(rec(s.RFC_SEED_2, "oryx-e0-2"))
    with pytest.raises(ks.KeySetError):
        ks.parse_key_list(json.dumps([entry]), ks.ORIGIN_CURRENT)


def test_key_list_bounds_and_json():
    with pytest.raises(ks.KeySetError):
        ks.parse_key_list("{", ks.ORIGIN_CURRENT)
    with pytest.raises(ks.KeySetError):
        ks.parse_key_list(json.dumps([rec(s.RFC_SEED_2, "oryx-e0-2")] * 2), ks.ORIGIN_CURRENT)
    with pytest.raises(ks.KeySetError):
        ks.parse_retired(json.dumps(["not-a-kid"]))
    assert ks.parse_key_list(None, ks.ORIGIN_AUDIT) == ()


def test_kid_is_derived_from_the_public_key_and_audience():
    public = s.public_key(s.RFC_SEED_2)
    import hashlib
    assert ks.derive_kid("oryx-e0-2", public) == "e0-2-" + hashlib.sha256(public).hexdigest()[:16]
    assert ks.derive_kid("oryx-e0-1a", public).startswith("e0-1a-")


def test_summary_is_public_only():
    sets = ks.KeySets.from_environment("2", s.keyset_environment("2"))
    text = json.dumps(sets.summary())
    assert "pub_sha256" in text and s.RFC_SEED_2 not in text and s.RFC_SEED_1A not in text
    assert sets.summary()["retired"] == [s.kid_of(s.RFC_SEED_1A, "oryx-e0-1a")]
