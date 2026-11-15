"""E0 — classification réseau V / I-a / I-b / I-c / I-d / F (protocole § 6.4, spécification V0.2-R3 § 2.3, § 6.2).

Références indépendantes du classificateur : vectors/ipclass_v1.json (issues
écrites à la main) et config/ip_special_registry.v1.json (table IANA v1). La
table générée côté Worker (e0_registry.generated.js) relève de PC-2.
"""
import ipaddress

import pytest

from tests import e0_support as s

import e0_ipclass as ic  # noqa: E402
import e0_redact as rd  # noqa: E402

VECTORS = s.vectors("ipclass_v1.json")["cases"]
REGISTRY = s.config("ip_special_registry.v1.json")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    s.block_external_network(monkeypatch)


@pytest.mark.parametrize("case", VECTORS, ids=lambda c: c["name"])
def test_shared_vectors(case):
    result = ic.classify_request(case["view"])
    expected = case["expected"]
    assert (result.cls, result.reason) == (expected["class"], expected["reason"])
    if "canonical" in expected:
        assert result.canonical_ip == expected["canonical"]
    if "canonical_input" in expected:
        assert result.canonical_input == expected["canonical_input"]
    if result.cls != ic.CLASS_V:
        assert result.canonical_ip is None


def test_vectors_cover_every_class_and_reason():
    classes = {c["expected"]["class"] for c in VECTORS}
    reasons = {c["expected"]["reason"] for c in VECTORS}
    assert classes == set(ic.CLASSES)
    assert reasons >= {"candidate", "not_https", "entry_host_mismatch", "entry_port_explicit", "worker_marker",
                       "cloudflare_asn", "ip_absent", "ip_empty", "ip_multiple", "ip_unparsable", "ip_mapped",
                       "ip_class_e", "ip_documentation", "ip_non_global", "cloudflare_substitute"}


@pytest.mark.parametrize("case", VECTORS, ids=lambda c: c["name"])
def test_classification_is_identical_with_or_without_engagement(case):
    """Étape 1 indépendante de toute donnée de la sonde : ajouter ou retirer des
    engagements (valides, invalides, discordants) ne change rien."""
    base = ic.classify_request(case["view"])
    for extra in ([("x-e0-cmp-pre-key", "00" * 32), ("x-e0-cmp-pre-mac", "11" * 32)],
                  [("x-e0-cmp-pre-key", "garbage")],
                  [("x-e0-cmp-pre-mac", "22" * 32), ("x-e0-cmp-pre-mac", "33" * 32)]):
        view = dict(case["view"], headers=list(case["view"]["headers"]) + [list(h) for h in extra])
        assert ic.classify_request(view) == base
    stripped = [h for h in case["view"]["headers"] if not h[0].lower().startswith("x-e0-cmp-pre-")]
    assert ic.classify_request(dict(case["view"], headers=stripped)) == base


def test_classifier_source_never_reads_engagement_headers():
    source = (s.BENCH_PY / "e0_ipclass.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    assert "cmp-pre" not in code and "cmp_pre" not in code


def test_i_a_list_mirrors_the_versioned_config():
    configured = [entry["address"] for entry in s.config("i_a_addresses.v1.json")["addresses"]]
    assert list(ic.I_A_ADDRESSES) == configured == ["2a06:98c0:3600::103"]


def test_iana_table_mirrors_the_versioned_registry():
    for version, table in ((4, rd.IPV4_TABLE), (6, rd.IPV6_TABLE)):
        expected = [(e["prefix"], e["globally_reachable"], e["category"], e["clear"])
                    for e in REGISTRY["ipv4" if version == 4 else "ipv6"]]
        assert list(table) == expected
    assert rd.IPV6_GLOBAL_UNICAST_SPACE == REGISTRY["ipv6_global_unicast_space"]


def test_iana_registry_entries_are_valid_networks():
    for family in ("ipv4", "ipv6"):
        prefixes = [e["prefix"] for e in REGISTRY[family]]
        assert len(prefixes) == len(set(prefixes))
        for entry in REGISTRY[family]:
            network = ipaddress.ip_network(entry["prefix"], strict=True)  # aucun bit d'hôte
            assert network.version == (4 if family == "ipv4" else 6)
            assert entry["globally_reachable"] in (True, False, None)


@pytest.mark.parametrize("text, canonical, was_canonical", [
    ("192.0.0.9", "192.0.0.9", True),
    ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1", False),
    ("2001:DB8::1", "2001:db8::1", False),
    ("::ffff:192.0.2.1", "::ffff:192.0.2.1", True),   # RFC 5952 § 5 : notation mixte
    ("::ffff:c000:201", "::ffff:192.0.2.1", False),
])
def test_strict_parser_canonicalises_and_records_canonicity(text, canonical, was_canonical):
    parsed = ic.parse_strict(text)
    assert parsed.canonical == canonical and parsed.was_canonical is was_canonical


@pytest.mark.parametrize("text", ["", " 1.2.3.4", "1.2.3.4 ", "01.2.3.4", "1.2.3", "1.2.3.4.5", "1.2.3.4:80",
                                  "[::1]", "fe80::1%1", "::1/128", "0x1.2.3.4", "1e2.0.0.1", "１.2.3.4", "::g",
                                  "1.2.3.4\n", None, 42])
def test_strict_parser_refusals(text):
    assert ic.parse_strict(text) is None


def test_signable_address_requires_canonical_global_non_i_a():
    assert ic.signable_address("192.0.0.9") == "192.0.0.9"
    assert ic.signable_address("2001:1::1") == "2001:1::1"
    for refused in ("2001:0001::1", "203.0.113.1", "10.0.0.1", "2a06:98c0:3600::103", "::ffff:c000:9", "240.0.0.1",
                    "2001:db8::1", "fd00::1", "2002:c000:209::1", "64:ff9b::c000:209"):
        assert ic.signable_address(refused) is None, refused


def test_public_result_never_carries_the_address():
    result = ic.classify(scheme="https", entry_host=s.PUBLIC_HOST, entry_port_explicit=False,
                         expected_entry_host=s.PUBLIC_HOST, cf_connecting_ip="192.0.0.9", cf_worker_present=False,
                         cf_ew_via_present=False, asn=None)
    assert result.cls == "V" and "192.0.0.9" not in repr(result.public())


def test_missing_expected_entry_host_is_never_v():
    result = ic.classify(scheme="https", entry_host="", entry_port_explicit=False, expected_entry_host="",
                         cf_connecting_ip="192.0.0.9", cf_worker_present=False, cf_ew_via_present=False, asn=None)
    assert result.cls == "I-d"
