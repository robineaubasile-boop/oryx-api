"""binary64-decimal-v1, validation des saisies, vues diagnostiques et
résultats portfolio-write-result-v1 (R1 PR 7) — sans base de données.

Les vecteurs de rendu couvrent les subnormaux, les extrêmes binary64, les
changements d'exposant de repr (1e15 / 1e16, 1e-4 / 1e-5, 1e21 / 1e22),
les zéros signés et les valeurs spéciales historiques. L'indépendance
vis-à-vis de extra_float_digits PostgreSQL est prouvée contre une vraie
base (tests/test_authz_f2_portfolio.py).
"""
import dataclasses
import json
import math
import sys
import uuid

import pytest

from core.authz_f2 import commands as c
from core.authz_f2 import portfolio_results as r
from core.authz_f2 import portfolio_values as v

R = v.render_binary64_decimal_v1

RENDER_VECTORS = [
    (0.0, "0"), (-0.0, "0"), (1.0, "1"), (4.5, "4.5"), (100.0, "100"), (-3.25, "-3.25"),
    (0.1, "0.1"), (0.1 + 0.2, "0.30000000000000004"), (100.1, "100.1"), (1 / 3, "0.3333333333333333"),
    (1e15, "1000000000000000"), (1e16, "10000000000000000"), (123456789012345.67, "123456789012345.67"),
    (1e21, "1000000000000000000000"), (1e22, "10000000000000000000000"),
    (1e-4, "0.0001"), (1e-5, "0.00001"), (1e-6, "0.000001"), (1.5e-7, "0.00000015"),
    (2.0 ** 53, "9007199254740992"), (2.0 ** 53 + 2, "9007199254740994"),
    (float("nan"), "NaN"), (float("inf"), "Infinity"), (float("-inf"), "-Infinity"),
]


@pytest.mark.parametrize("number,expected", RENDER_VECTORS, ids=[repr(n) for n, _ in RENDER_VECTORS])
def test_render_vectors(number, expected):
    assert R(number) == expected
    assert v.is_rendered_number(expected)
    if math.isfinite(number):
        assert float(expected) == number  # aller-retour exact


def test_subnormals_and_binary64_extremes():
    smallest = 5e-324  # 2^-1074, plus petit subnormal
    assert R(smallest) == "0." + "0" * 323 + "5"
    assert R(-smallest) == "-0." + "0" * 323 + "5"
    largest_subnormal = 2.2250738585072009e-308
    smallest_normal = sys.float_info.min
    assert R(smallest_normal) == "0." + "0" * 307 + "22250738585072014"
    assert R(largest_subnormal) == "0." + "0" * 307 + "2225073858507201"  # repr la plus courte
    assert R(sys.float_info.max) == "17976931348623157" + "0" * 292
    assert R(-sys.float_info.max) == "-17976931348623157" + "0" * 292
    for number in (smallest, largest_subnormal, smallest_normal, sys.float_info.max, sys.float_info.epsilon):
        assert float(R(number)) == number and "e" not in R(number).lower()


def test_render_requires_an_exact_float():
    for bad in (1, True, "1", None, 1.5j):
        with pytest.raises(TypeError):
            R(bad)


def test_rendered_number_parser_rejects_non_canonical_text():
    for bad in ("-0", "01", "1.0", "1.", ".5", "1e3", "+1", " 1", "nan", "inf", "-NaN", "٣", "0.30000000000000005",
                1.0, None):
        assert not v.is_rendered_number(bad), bad


QUANTITY_OK = ["4", "0.000001", "1000000000000", "999999999999.5", "123456789.123456", "12345678901.2345",
               "0.1", "100.1"]
QUANTITY_BAD = ["0", "0.0", "-1", "+1", "1e3", "1E3", "01", "00", "1.", ".5", "1.10", "1.0", "0.0000001",
                "1000000000000.000001", "1000000000001", "1234567890123456", "12345678901234.56", " 1", "1 ",
                "1,5", "١", "１", "²", "NaN", "Infinity", "", "1" * 22, 4, 4.0, True, None, b"4"]


@pytest.mark.parametrize("text", QUANTITY_OK)
def test_valid_quantities(text):
    assert v.check_quantity(text) == text
    number = v.to_float(text)
    assert R(number) == text  # stockage puis relecture : même représentation


@pytest.mark.parametrize("text", QUANTITY_BAD, ids=[repr(t) for t in QUANTITY_BAD])
def test_invalid_quantities(text):
    with pytest.raises(v.ValueRejected):
        v.check_quantity(text)


def test_domains_of_price_and_target():
    assert v.check_purchase_price(None) is None and v.check_target_percent(None) is None
    assert v.check_purchase_price("0") == "0" and v.check_purchase_price("1000000000000") == "1000000000000"
    assert v.check_target_percent("0") == "0" and v.check_target_percent("100") == "100"
    for bad in ("1000000000000.1", "-0.5"):
        with pytest.raises(v.ValueRejected):
            v.check_purchase_price(bad)
    for bad in ("100.000001", "101", "-1"):
        with pytest.raises(v.ValueRejected):
            v.check_target_percent(bad)


def test_length_bound_is_checked_before_parsing():
    with pytest.raises(v.ValueRejected):
        v.check_quantity("9" * 10_000)  # jamais converti en Decimal
    assert v.MAX_DECIMAL_CHARS == 21 and v.MAX_SIGNIFICANT_DIGITS == 15


@pytest.mark.parametrize("ticker", ["AIR.PA", "A", "BRK-B", "A^B", "EURUSD=X", "0", "A" * 32, "MC.PA"])
def test_valid_tickers(ticker):
    assert v.check_ticker(ticker) == ticker


@pytest.mark.parametrize("ticker", ["", "^FCHI", "air.pa", " AIR.PA", "AIR.PA ", ".PA", "-A", "A" * 33, "AÉ", "A B", "A/B",
                                    "A\n", "Ａ", None, 1])
def test_invalid_tickers_are_never_trimmed_or_uppercased(ticker):
    with pytest.raises(v.ValueRejected):
        v.check_ticker(ticker)


def test_envelopes():
    for ok in (None, "PEA", "CTO"):
        assert v.check_envelope(ok) == ok
    for bad in ("pea", "PEA ", "AV", "", 1):
        with pytest.raises(v.ValueRejected):
            v.check_envelope(bad)


def test_float8send_decoding_is_exact():
    import struct
    for number in (0.1 + 0.2, 5e-324, -0.0, float("inf")):
        raw = struct.pack(">d", number)
        assert struct.pack(">d", v.float_from_float8send(raw)) == raw
        assert struct.pack(">d", v.float_from_float8send(memoryview(raw))) == raw
    for bad in (b"\x00" * 7, b"\x00" * 9, None, "x" * 8):
        with pytest.raises(TypeError):
            v.float_from_float8send(bad)


# --------------------------------------------------------------------------
# Vues diagnostiques
# --------------------------------------------------------------------------

def test_valid_view():
    view = c.position_view(7, 3, "AIR.PA", 4.5, 100.1, 20.0, "CTO")
    assert view == c.PositionView(7, 3, "AIR.PA", "4.5", "100.1", "20", "CTO", "valid", ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.quantity = "5"


@pytest.mark.parametrize("fields,issues", [
    (("air.pa", 1.0, None, None, None), ("ticker_invalid",)),
    (("AIR.PA", -2.0, None, None, None), ("quantity_invalid",)),
    (("AIR.PA", 0.0, None, None, None), ("quantity_invalid",)),
    (("AIR.PA", -0.0, None, None, None), ("quantity_invalid",)),
    (("AIR.PA", 0.1 + 0.2, None, None, None), ("quantity_invalid",)),  # 17 chiffres significatifs
    (("AIR.PA", 1.0, -1.0, None, None), ("purchase_price_invalid",)),
    (("AIR.PA", 1.0, 1e13, None, None), ("purchase_price_invalid",)),
    (("AIR.PA", 1.0, None, 150.0, None), ("target_percent_invalid",)),
    (("AIR.PA", 1.0, None, None, "AV"), ("envelope_invalid",)),
    (("AIR.PA", float("nan"), float("inf"), float("-inf"), None),
     ("quantity_invalid", "purchase_price_invalid", "target_percent_invalid")),
    (("AIR.PA", 5e-324, None, None, None), ("quantity_invalid",)),
    (("AIR.PA", sys.float_info.max, None, None, None), ("quantity_invalid",)),
    (("", -1.0, -1.0, -1.0, ""), tuple(v.ISSUE_CODES)),
])
def test_legacy_rows_are_readable_with_ordered_closed_issues(fields, issues):
    view = c.position_view(1, 0, *fields)
    assert view.data_status == "legacy_invalid" and view.validation_issues == issues
    assert set(issues) <= set(v.ISSUE_CODES)
    payload = r.view_payload(view)
    assert json.loads(r.canonical_json(payload)) == payload  # toujours sérialisable, sans constante JSON
    assert r.view_from_payload(payload) == view


def test_incoherent_views_cannot_exist():
    good = ("AIR.PA", "4", None, None, "PEA", "valid", ())
    for bad in ((0, 0) + good, (1, -1) + good, (1, 2**63) + good, (True, 0) + good, (1, True) + good,
                (1, 0, "AIR.PA", 4.0, None, None, "PEA", "valid", ()),
                (1, 0, "AIR.PA", "4.0", None, None, "PEA", "valid", ()),
                (1, 0, "AIR.PA", "4", None, None, "PEA", "legacy_invalid", ()),
                (1, 0, "AIR.PA", "4", None, None, "PEA", "valid", ["x"]),
                (1, 0, "air", "4", None, None, "PEA", "valid", ()),
                (1, 0, None, "4", None, None, "PEA", "legacy_invalid", ("ticker_invalid",)),
                (1, 0, "AIR.PA", "4", None, None, 3, "legacy_invalid", ("envelope_invalid",))):
        with pytest.raises((ValueError, TypeError)):
            c.PositionView(*bad)


# --------------------------------------------------------------------------
# portfolio-write-result-v1
# --------------------------------------------------------------------------

OP = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _created(position_id=42, ticker="AIR.PA"):
    view = c.position_view(position_id, 0, ticker, 4.0, None, None, "PEA")
    return r.build_payload("portfolio_position_create", OP, position_id, view=view)


def test_created_result_is_canonical_and_closed():
    result = r.render_v1("portfolio_position_create", OP, 42, _created())
    assert result.status == 201 and result.body == (
        b'{"operation_id":"11111111-1111-4111-8111-111111111111","operation_type":"portfolio_position_create",'
        b'"outcome":"created","position":{"data_status":"valid","envelope":"PEA","numeric_encoding":'
        b'"binary64-decimal-v1","position_id":42,"position_revision":"0","purchase_price":null,"quantity":"4",'
        b'"target_percent":null,"ticker":"AIR.PA","validation_issues":[]},"position_id":42}')
    assert r.location(result) == "/api/v2/me/portfolio/42"
    assert r.same_result(result, "portfolio_position_create", OP)
    assert not r.same_result(result, "portfolio_position_update", OP)
    assert not r.same_result(result, "portfolio_position_create", uuid.uuid4())


def test_deleted_result():
    payload = r.build_payload("portfolio_position_delete", OP, 42, deleted_revision=2**63 - 1)
    result = r.render_v1("portfolio_position_delete", OP, 42, payload)
    assert result.status == 200 and result.deleted_revision == 2**63 - 1 and r.location(result) is None
    assert json.loads(result.body) == {"operation_id": str(OP), "operation_type": "portfolio_position_delete",
                                       "position_id": 42, "outcome": "deleted",
                                       "deleted_revision": "9223372036854775807"}


def test_replay_from_jsonb_text_reproduces_the_same_bytes():
    first = r.render_v1("portfolio_position_create", OP, 42, _created())
    # Texte JSONB de PostgreSQL : espaces et ordre de clés différents.
    jsonb_text = json.dumps(json.loads(first.body), indent=1, sort_keys=False)
    again = r.render_v1("portfolio_position_create", OP, 42, r.parse_json(jsonb_text))
    assert again == first and again.body == first.body


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("outcome"),
    lambda p: p.update(extra=1),
    lambda p: p.update(outcome="replaced"),
    lambda p: p.update(position_id=43),
    lambda p: p.update(position_id="42"),
    lambda p: p.update(position_id=True),
    lambda p: p.update(operation_id=str(OP).replace("-", "")),
    lambda p: p.update(operation_type="portfolio_position_update"),
    lambda p: p["position"].update(position_revision="1"),
    lambda p: p["position"].update(position_revision="01"),
    lambda p: p["position"].update(position_revision=0),
    lambda p: p["position"].update(numeric_encoding="binary64-decimal-v2"),
    lambda p: p["position"].update(quantity="4.0"),
    lambda p: p["position"].update(quantity=4),
    lambda p: p["position"].update(data_status="legacy_invalid"),
    lambda p: p["position"].update(validation_issues=["ticker_invalid"]),
    lambda p: p["position"].update(user_id="user-b"),
    lambda p: p["position"].pop("ticker"),
    lambda p: p["position"].update(position_id=41),
])
def test_corrupted_payloads_are_refused(mutate):
    payload = json.loads(json.dumps(_created()))
    mutate(payload)
    with pytest.raises(r.ResultInvalid):
        r.render_v1("portfolio_position_create", OP, 42, payload)


def test_strict_json_parsing():
    for bad in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":1.5}', '{"a":Infinity}', "", "[", None, b"{}"):
        with pytest.raises(r.ResultInvalid):
            r.parse_json(bad)


def test_result_size_is_bounded():
    assert r.MAX_RESULT_BYTES == 16384
    # Ticker historique géant (jamais saisi par PR 7) : un résultat > 16 Kio est refusé.
    payload = _created(ticker="X" * 17000)
    with pytest.raises(r.ResultInvalid):
        r.render_v1("portfolio_position_create", OP, 42, payload)


def test_result_versions_are_kept():
    assert r.RENDERERS == {"portfolio-write-result-v1": r.render_v1}
    assert v.RENDERERS == {"binary64-decimal-v1": v.render_binary64_decimal_v1}


def test_new_commands_are_frozen_typed_data_without_owner():
    command = c.ReplacePortfolioPosition(OP, 42, 7, "AIR.PA", "4.5", "100.1", "20", "CTO")
    assert {f.name for f in dataclasses.fields(command)} == {
        "operation_id", "position_id", "expected_revision", "ticker", "quantity", "purchase_price", "target_percent",
        "envelope"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.expected_revision = 8
    for command_type in (c.CreatePortfolioPosition, c.ReplacePortfolioPosition, c.DeletePortfolioPosition):
        names = {f.name for f in dataclasses.fields(command_type)}
        assert "user_id" not in names and not any(callable(getattr(command_type, n, None)) for n in names)

    class Text(str):
        pass

    class Number(int):
        pass

    bad_cases = [
        lambda: c.CreatePortfolioPosition(str(OP), "AIR.PA", "4", None, None, None),
        lambda: c.CreatePortfolioPosition(OP, Text("AIR.PA"), "4", None, None, None),
        lambda: c.CreatePortfolioPosition(OP, "AIR.PA", 4, None, None, None),
        lambda: c.CreatePortfolioPosition(OP, "AIR.PA", "4", 0, None, None),
        lambda: c.CreatePortfolioPosition(OP, "AIR.PA", "4", None, "100.5", None),
        lambda: c.CreatePortfolioPosition(OP, "AIR.PA", "4", None, None, "pea"),
        lambda: c.ReplacePortfolioPosition(OP, True, 0, "AIR.PA", "4", None, None, None),
        lambda: c.ReplacePortfolioPosition(OP, 1, True, "AIR.PA", "4", None, None, None),
        lambda: c.ReplacePortfolioPosition(OP, 1, Number(0), "AIR.PA", "4", None, None, None),
        lambda: c.ReplacePortfolioPosition(OP, 1, -1, "AIR.PA", "4", None, None, None),
        lambda: c.ReplacePortfolioPosition(OP, 1, 2**63, "AIR.PA", "4", None, None, None),
        lambda: c.ReplacePortfolioPosition(OP, 2**31, 0, "AIR.PA", "4", None, None, None),
        lambda: c.DeletePortfolioPosition(OP, 1, "0"),
        lambda: c.DeletePortfolioPosition(OP, 0, 0),
        lambda: c.DeletePortfolioPosition(None, 1, 0),
    ]
    for case in bad_cases:
        with pytest.raises(ValueError):
            case()
    assert c.DeletePortfolioPosition(OP, 1, 2**63 - 1).expected_revision == 2**63 - 1
    assert c.replace_revisions(7) == (7, 8) and c.replace_revisions(2**63 - 1) == (2**63 - 1,)
