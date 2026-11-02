"""R1 PR 9-B2 — tests SANS base de données : politiques pures, budget,
validation de la réponse, snapshot, candidat, commande, transports.

Aucun réseau : les transports réels sont exercés avec des sessions HTTP
simulées (requests) et un transport httpx2 MockTransport (Anthropic).
"""
import copy
import dataclasses
import json
import logging
import pickle
import uuid
from datetime import datetime, timedelta, timezone

import httpx2
import pytest
import requests
import sqlalchemy as sa

import core.ticker_resolver as legacy_resolver
from core import decryptage_providers as providers
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_generation as dg
from core.authz_f2 import decryptage_instrument as di
from core.authz_f2 import decryptage_market_data as dm
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.decryptage_fingerprint import DecryptageKeyring
from core.authz_f2.principal import AuthorizedPrincipal
from core.authz_f2.runner import AUTHORIZED, AuthorizationOutcome, AuthorizedTransactionRunner, UnknownCommand
from tests.authz_f2_pr9b2_support import (DELETE, FMP_PROFILE, FMP_RATIOS, ISIN, REALTIME, FakeClaude, FakeClock,
                                          FakeEodhd, FakeFmp, fundamentals)
from tests.f2_support import make_keyring

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
TURN = uuid.UUID("123e4567-e89b-42d3-a456-426614174000")
MC = di.InstrumentResolution(di.IDENTIFIED, "MC.PA", "MC", "PA", di.EXACT_SYMBOL, "LVMH", ISIN)


def row(code, exchange, name, isin, kind="Common Stock", country=""):
    return {"Code": code, "Exchange": exchange, "Name": name, "Type": kind, "Country": country, "Isin": isin}


# --------------------------------------------------------------------------
# Instrument
# --------------------------------------------------------------------------

def test_the_copied_resolver_rules_equal_the_historical_ones():
    assert di.PEA_EXCHANGES == legacy_resolver.PEA_EXCHANGES
    assert di.OTHER_EXCHANGES == legacy_resolver.OTHER_EXCHANGES
    assert di.ALLOWED_TYPES == legacy_resolver.ALLOWED_TYPES
    assert di.KNOWN_TICKER_OVERRIDES == legacy_resolver._KNOWN_TICKER_OVERRIDES
    assert di.US_TICKER_PATTERN.pattern == legacy_resolver.US_TICKER_PATTERN.pattern
    for name in ("LVMH Moët Hennessy Louis Vuitton SE", "The Coca-Cola Co", "Siemens Limited", "L'Oréal S.A.",
                 "Alphabet Inc Class A", "AIRBUS SE", "Shopify Inc (CAD Hedged)", "BRK 1/100", ""):
        assert di.normalize_company_name(name) == legacy_resolver._normalize_company_name(name)
    for code in ("LVMHF", "SIEGY", "AAPL", "", "ABCY"):
        assert di.is_likely_otc_adr(code) == legacy_resolver._is_likely_otc_adr(code)
    for text in ("HermÃ¨s", "Hermès", ""):
        assert di.fix_mojibake(text) == legacy_resolver._fix_mojibake(text)


@pytest.mark.parametrize("listings,query", [
    ([row("AMZ", "XETRA", "Amazon.com Inc", "US0231351067"), row("AMZN", "US", "Amazon.com Inc", "US0231351067")],
     "AMAZON.COM"),
    ([row("SIE", "XETRA", "Siemens AG", "DE0007236101"), row("SIEGY", "US", "Siemens AG", "US8261975010"),
      row("0P6M", "LSE", "Siemens AG", "DE0007236101")], "SIEMENS"),
    ([row("ASML", "AS", "ASML Holding NV", "NL0010273215"), row("ASML", "US", "ASML Holding NV", "NL0010273215")],
     "ASML HOLDING"),
])
def test_the_listing_rank_is_the_historical_one_for_a_single_company(listings, query, capsys):
    legacy = legacy_resolver._pick_best_match(copy.deepcopy(listings), prefer_us=False, query=query)
    capsys.readouterr()
    resolution = di.resolve(query, listings)
    assert resolution.status == di.IDENTIFIED and resolution.ticker == legacy


def test_known_overrides_need_no_call_and_keep_the_historical_format():
    assert di.known_override("LVMH") == di.InstrumentResolution(di.IDENTIFIED, "MC.PA", "MC", "PA", di.KNOWN_OVERRIDE)
    assert di.known_override("MICROSOFT").ticker == "MSFT.US" and di.known_override("APPLE") is None
    assert di.resolve("LVMH", None).ticker == "MC.PA"


def test_exact_symbols_are_identified_and_a_raw_input_is_never_a_proof():
    assert di.resolve("MC.PA", [row("MC", "PA", "LVMH", ISIN)]).ticker == "MC.PA"
    assert di.resolve("MC.PA", [row("MC", "AS", "LVMH", ISIN)]).status == di.NOT_FOUND
    assert di.resolve("AAPL", [row("AAPL", "US", "Apple Inc", "US0378331005")]).ticker == "AAPL"
    # strip().upper() / fallback brut : jamais une identification.
    assert di.resolve("XYZQ", []).status == di.NOT_FOUND
    assert di.resolve("XYZQ", None).status == di.NOT_FOUND
    assert di.resolve("XYZQ", [row("ABC", "PA", "Autre Société", "FR0000000001")]).status == di.NOT_FOUND
    # Type non admis (indice, obligation) : jamais retenu.
    assert di.resolve("MC.PA", [row("MC", "PA", "LVMH", ISIN, kind="Bond")]).status == di.NOT_FOUND


def test_homonyms_and_uncertain_identities_are_ambiguous_never_chosen():
    # Même code, deux entreprises (Airbus à Paris, AAR Corp aux US) : jamais départagées.
    homonym_code = [row("AIR", "PA", "Airbus SE", "NL0000235190"), row("AIR", "US", "AAR Corp", "US0003611052")]
    assert di.resolve("AIR", homonym_code).status == di.AMBIGUOUS
    # Même nom normalisé, deux ISIN (Merck & Co / Merck KGaA) : refus, jamais au nombre de pays.
    merck = [row("MRK", "US", "Merck & Co Inc", "US58933Y1055"), row("MRK", "XETRA", "Merck KGaA", "DE0006599905")]
    assert di.resolve("MERCK", merck).status == di.AMBIGUOUS
    # Nom seulement apparenté (préfixe) : incertain ; nom exact À CÔTÉ d'un nom
    # apparenté d'une autre entreprise : homonymie possible, refus.
    assert di.resolve("FERRARI", [row("FGRP", "MI", "Ferrari Group PLC", "IT0001")]).status == di.AMBIGUOUS
    assert di.resolve("FERRARI", [row("RACE", "MI", "Ferrari NV", "NL0011585146"),
                                  row("FGRP", "MI", "Ferrari Group PLC", "IT0001")]).status == di.AMBIGUOUS
    # Identité sans ISIN : non vérifiable.
    assert di.resolve("ORANGE", [row("ORA", "PA", "Orange SA", None)]).status == di.AMBIGUOUS
    # Code et nom désignant deux entreprises.
    clash = [row("TOTAL", "PA", "Total Produce", "IE0001"), row("TTE", "PA", "Total", "FR0000120271")]
    assert di.resolve("TOTAL", clash).status == di.AMBIGUOUS
    # Une seule entreprise (un ISIN) sur plusieurs places : identifiée.
    single = [row("ORA", "PA", "Orange SA", "FR0000133308"), row("FTE", "XETRA", "Orange SA", "FR0000133308")]
    resolved = di.resolve("ORANGE", single)
    assert (resolved.ticker, resolved.provenance, resolved.isin) == ("ORA.PA", di.EXACT_COMPANY_NAME, "FR0000133308")


@pytest.mark.parametrize("raw", [{"Code": "MC"}, ["x"], [{"Code": 1}], [row("MC", "PA", "L", ISIN)] * 51,
                                 [{"Code": "MC\x00"}]])
def test_malformed_search_results_are_an_unusable_provider_result(raw):
    assert di.resolve("MC.PA", raw).status == di.UNAVAILABLE


def test_a_resolution_carries_no_instrument_unless_identified():
    with pytest.raises(ValueError):
        di.InstrumentResolution(di.NOT_FOUND, "MC.PA")
    with pytest.raises(ValueError):
        di.InstrumentResolution(di.IDENTIFIED, "mc.pa", "mc", "pa", di.EXACT_SYMBOL)
    with pytest.raises(ValueError):
        di.InstrumentResolution(di.IDENTIFIED, "MC.PA", "AI", "PA", di.EXACT_SYMBOL)


# --------------------------------------------------------------------------
# Données financières
# --------------------------------------------------------------------------

def test_eodhd_parsing_keeps_identity_currency_units_and_periods():
    snap = dm.parse_eodhd(MC, fundamentals(), REALTIME)
    assert snap.sufficient and (snap.primary_source, snap.currency, snap.provider_symbol) == ("eodhd", "EUR", "MC.PA")
    assert snap.price_as_of == datetime.fromtimestamp(1793000000, tz=timezone.utc).isoformat()
    assert snap.field("current_price") == dm.FinancialField("current_price", "present", 600.5, "eodhd", "REALTIME")
    assert snap.field("revenue_growth").period == "CAGR:FY2023-12-31..FY2025-12-31"
    assert snap.value("revenue_growth") == round(((90 / 79) ** 0.5 - 1) * 100, 2)
    assert (snap.value("operating_margin"), snap.field("operating_margin").period) == (26.0, "FY:2025-12-31")
    assert (snap.value("roe"), snap.field("roe").period) == (24.0, "TTM")
    assert snap.value("fcf_per_share") == 29.8 and snap.value("net_cash") == -30000000000.0
    assert snap.value("roic") == round(23.4e9 * 0.75 / (65e9 + 40e9 - 10e9) * 100, 2)
    assert snap.field("current_price").unit == "currency" and snap.field("roe").unit == "percent"
    data = snap.prompt_data()
    data["roe"] = 0
    assert snap.value("roe") == 24.0  # copie : l'instantané reste immuable
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.currency = "USD"


@pytest.mark.parametrize("bad", ["NaN", float("nan"), float("inf"), "NA", True, None, "1e999", "12,5", ""])
def test_absent_or_invalid_numbers_stay_absent_never_zero(bad):
    data = fundamentals(Highlights__ReturnOnEquityTTM=bad, SharesStats__SharesOutstanding=bad,
                        **{"Financials__Balance_Sheet__yearly__2025-12-31__cashAndShortTermInvestments": bad})
    snap = dm.parse_eodhd(MC, data, dict(REALTIME, close=bad))
    for name in ("roe", "fcf_per_share", "net_cash", "current_price", "roic"):
        field = snap.field(name)
        assert field.status == "absent" and field.value is None, name
    assert snap.field("debt_to_equity").present  # aucune contamination des champs valides


def test_zero_shares_never_yield_a_per_share_value():
    snap = dm.parse_eodhd(MC, fundamentals(SharesStats__SharesOutstanding=0), REALTIME)
    assert snap.field("fcf_per_share").status == "absent"


def test_statements_in_another_currency_are_never_merged_with_the_quote():
    snap = dm.parse_eodhd(MC, fundamentals(statement_currency="USD"), REALTIME)
    for name in ("eps", "fcf_per_share", "net_cash"):
        assert snap.field(name).status == "currency_mismatch", name
    assert snap.field("operating_margin").present and snap.field("current_price").present  # ratios sans devise


def test_gbx_quotes_are_converted_explicitly_and_traced():
    snap = dm.parse_eodhd(MC, fundamentals(currency="GBX", statement_currency="GBP"), REALTIME)
    assert (snap.currency, snap.unit_conversion, snap.value("current_price")) == ("GBP", "GBX->GBP", 6.005)
    assert snap.field("eps").present


@pytest.mark.parametrize("override", [dict(code="AI"), dict(isin="FR0000000000"), dict(currency="euro"),
                                      dict(currency=None)])
def test_identity_or_currency_mismatches_make_the_response_unusable(override):
    assert dm.parse_eodhd(MC, fundamentals(**override), REALTIME) is None
    assert dm.parse_eodhd(MC, ["pas un dict"], REALTIME) is None


def test_a_realtime_quote_of_another_symbol_is_ignored():
    snap = dm.parse_eodhd(MC, fundamentals(), dict(REALTIME, code="AI.PA"))
    assert snap.field("current_price").status == "absent" and snap.price_as_of is None


def test_the_cagr_needs_consecutive_fiscal_years_and_no_estimate_is_used():
    gap = fundamentals(**{"Financials__Income_Statement__yearly__2024-12-31": DELETE})
    snap = dm.parse_eodhd(MC, gap, REALTIME)
    assert snap.field("revenue_growth").status == "absent"  # 2025 -> 2023 : jamais présenté comme annuel
    estimate = fundamentals(Highlights__EarningsShare=None, Highlights__EPSEstimateCurrentYear=30.0)
    assert dm.parse_eodhd(MC, estimate, REALTIME).field("eps").status == "absent"


def test_no_default_tax_rate_is_ever_assumed():
    snap = dm.parse_eodhd(MC, fundamentals(**{
        "Financials__Income_Statement__yearly__2025-12-31__incomeTaxExpense": DELETE}), REALTIME)
    assert snap.field("roic").status == "absent"


def test_fmp_fallback_never_presents_yoy_growth_as_a_cagr():
    snap = dm.parse_fmp(MC, FMP_PROFILE, FMP_RATIOS)
    assert snap.primary_source == "fmp" and snap.sufficient
    assert snap.field("revenue_growth").status == "absent" and snap.field("eps").status == "absent"
    assert snap.field("roe").period == "FY:2025-12-31"
    assert dm.parse_fmp(MC, [dict(FMP_PROFILE[0], symbol="AI.PA")], FMP_RATIOS) is None
    assert dm.parse_fmp(MC, [dict(FMP_PROFILE[0], currency="GBX")], FMP_RATIOS) is None
    other = dm.parse_fmp(MC, FMP_PROFILE, [dict(FMP_RATIOS[0], reportedCurrency="USD")])
    assert other.field("fcf_per_share").status == "currency_mismatch"


def test_complements_require_the_same_definition_period_and_currency():
    base = dm.parse_eodhd(MC, fundamentals(**{
        "Financials__Income_Statement__yearly__2025-12-31__grossProfit": DELETE,
        "Financials__Cash_Flow__yearly__2025-12-31__freeCashFlow": DELETE}), REALTIME)
    assert set(base.missing_complementable()) == {"gross_margin_latest", "fcf_per_share"}
    assert dm.complement(base, [dict(FMP_RATIOS[0], date="2024-12-31")]) is base
    assert dm.complement(base, [dict(FMP_RATIOS[0], symbol="AI.PA")]) is base
    usd = dm.complement(base, [dict(FMP_RATIOS[0], reportedCurrency="USD")])
    assert usd.complemented == ("gross_margin_latest",)  # FCF monétaire en USD : refusé
    full = dm.complement(base, FMP_RATIOS)
    assert full.complemented == ("gross_margin_latest", "fcf_per_share")
    assert full.value("operating_margin") == base.value("operating_margin")  # jamais écrasé


def test_market_snapshot_fingerprint_is_deterministic():
    a = dm.parse_eodhd(MC, fundamentals(), REALTIME)
    b = dm.parse_eodhd(MC, fundamentals(), REALTIME)
    assert dcand.market_fingerprint(a) == dcand.market_fingerprint(b)
    assert dcand.market_fingerprint(a) != dcand.market_fingerprint(dm.parse_eodhd(MC, fundamentals(), None))


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------

def test_the_budget_is_frozen_at_240_seconds_with_its_step_caps():
    assert dg.TOTAL_BUDGET_SECONDS == 240.0 and dg.MARGIN_SECONDS == 5.0
    assert dict(dg.STEP_CAPS) == {"f2": 20.0, "resolution": 15.0, "market_data": 80.0, "generation": 105.0,
                                  "validation": 15.0}
    assert sum(dg.STEP_CAPS.values()) + dg.MARGIN_SECONDS == dg.TOTAL_BUDGET_SECONDS
    assert dr.LEASE_DURATION.total_seconds() - dg.TOTAL_BUDGET_SECONDS == 120.0


def test_each_step_gets_at_most_its_cap_the_global_rest_and_the_lease_rest():
    clock = FakeClock()
    budget = dg.GenerationBudget(clock)
    assert budget.allowance("generation") == 105.0
    assert budget.call("market_data", lambda t: (clock.advance(70.0), t)[1]) == 80.0
    assert budget.allowance("market_data") == 10.0
    clock.advance(150.0)  # global : 235 - 220 = 15
    assert budget.allowance("generation") == 15.0
    with pytest.raises(dg.BudgetExhausted):
        budget.call("generation", lambda t: pytest.fail("aucun appel sous le seuil"))
    budget.bound_lease(clock() + 3.0)
    assert budget.allowance("validation") == 3.0
    clock.advance(3.0)
    assert budget.lease_expired()


def test_a_late_result_is_an_overrun_and_time_is_accounted():
    clock = FakeClock()
    budget = dg.GenerationBudget(clock)
    with pytest.raises(dg.StepOverrun):
        budget.call("resolution", lambda t: clock.advance(t + 0.01))
    assert budget.used("resolution") == pytest.approx(15.01)
    with pytest.raises(dg.BudgetExhausted):
        budget.call("resolution", lambda t: None)
    with pytest.raises(ZeroDivisionError):
        budget.call("market_data", lambda t: 1 / 0)
    assert not budget.exhausted()
    clock.advance(241.0)
    assert budget.exhausted() and budget.allowance("f2") == 0.0


# --------------------------------------------------------------------------
# Validation de la réponse
# --------------------------------------------------------------------------

def test_markers_are_removed_and_only_the_closed_vocabulary_is_recognized():
    ok = dcand.validate_response({"stop_reason": "end_turn", "content": [
        {"type": "thinking"}, {"type": "text", "text": "Texte.\n<!--ORYX_STEP:moat-->"}]})
    assert (ok.visible_text, ok.step_marker, ok.diagnostics) == ("Texte.", "moat", ())
    unknown = dcand.validate_response({"stop_reason": "end_turn", "content": [
        {"type": "text", "text": "Texte.<!--ORYX_STEP:valo-->"}]})
    assert (unknown.visible_text, unknown.step_marker, unknown.diagnostics) == ("Texte.", None, ("unknown_marker",))
    both = dcand.validate_response({"stop_reason": "end_turn", "content": [
        {"type": "text", "text": "A<!--ORYX_STEP:moat--> B<!--ORYX_STEP:risques-->"}]})
    assert (both.step_marker, both.diagnostics) == (None, ("contradictory_markers",))
    same = dcand.validate_response({"stop_reason": "end_turn", "content": [
        {"type": "text", "text": "A<!--ORYX_STEP:moat--> B<!--ORYX_STEP:moat-->"}]})
    assert same.step_marker == "moat" and same.visible_text == "A B"
    none = dcand.validate_response({"stop_reason": "stop_sequence", "content": [{"type": "text", "text": "Sans."}]})
    assert none.step_marker is None and none.diagnostics == ()


@pytest.mark.parametrize("raw", [
    None, {"content": []}, {"stop_reason": "end_turn", "content": "texte"},
    {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Coupé"}]},
    {"stop_reason": "refusal", "content": [{"type": "text", "text": "Non."}]},
    {"stop_reason": "end_turn", "content": [{"type": "text", "text": "  "}]},
    {"stop_reason": "end_turn", "content": [{"type": "text", "text": "<!--ORYX_STEP:moat-->"}]},
    {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Texte <!--ORYX_STEP:moat"}]},
    {"stop_reason": "end_turn", "content": [{"type": "text", "text": "a\x00b"}]},
    {"stop_reason": "end_turn", "content": [{"type": "text", "text": "x" * (dcand.RESPONSE_MAX_BYTES + 1)}]},
    {"stop_reason": "end_turn", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
    {"stop_reason": "end_turn", "content": [{"type": "text", "text": 3}]},
])
def test_unusable_responses_are_refused(raw):
    with pytest.raises(dcand.ResponseUnusable):
        dcand.validate_response(raw)


# --------------------------------------------------------------------------
# Snapshot et candidat
# --------------------------------------------------------------------------

def preparation_view(**overrides):
    session_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
    fields = dict(
        contract_version=dc.CONTRACT_VERSION, reservation_id=uuid.UUID("00000000-0000-4000-8000-0000000000aa"),
        lease_epoch=1, attempts_used=1, lease_expires_at=NOW + timedelta(seconds=300), read_at=NOW,
        owner_user_id="user-a", conversation_key="conv-1", client_turn_id=TURN, ticker="MC.PA", level="debutant",
        level_revision=0, anchor=dc.PreparationAnchor(dc.ANCHOR_NONE, None, None),
        active_session=dc.PreparationActiveSession(session_id, "moat"),
        facts=(dc.PreparationFact(1, "roe", 20.0), dc.PreparationFact(2, "inconnu", 1.0),
               dc.PreparationFact(3, "roe", 24.0), dc.PreparationFact(4, "eps", None)),
        statements=(dc.PreparationStatement(1, "business", "Luxe."), dc.PreparationStatement(2, None, "Sans étape"),
                    dc.PreparationStatement(3, "moat", "   ")),
        latest_thesis=None)
    fields.update(overrides)
    return dc.DecryptagePreparationView(**fields)


def test_the_snapshot_keeps_only_what_the_prompt_uses_with_historical_rules():
    snap = dcand.build_snapshot(preparation_view(), "MC.PA", "construction_these", dcand.BASIS_ACTIVE_ANALYSIS)
    assert [f.fact_id for f in snap.facts_used] == [3]  # dernier par type injecté, valeur connue
    assert [s.statement_id for s in snap.statements_used] == [1]  # étape et texte non vides
    assert snap.in_progress_analysis() == {"current_step": "moat", "statements": [{"step": "business",
                                                                                   "text": "Luxe."}],
                                           "facts": {"roe": 24.0}}
    assert snap.fingerprint == dcand.build_snapshot(preparation_view(), "MC.PA", "construction_these",
                                                    dcand.BASIS_ACTIVE_ANALYSIS).fingerprint
    for change in (dict(level="avance"), dict(level_revision=1), dict(lease_epoch=2), dict(owner_user_id="user-b"),
                   dict(active_session=dc.PreparationActiveSession(uuid.uuid4(), "moat")),
                   dict(statements=(dc.PreparationStatement(1, "business", "Luxe!"),))):
        other = dcand.build_snapshot(preparation_view(**change), "MC.PA", "construction_these",
                                     dcand.BASIS_ACTIVE_ANALYSIS)
        assert other.fingerprint != snap.fingerprint, change
    canonical = dcand.canonical_bytes(snap.canonical())
    assert b'{"binary64":"24"}' in canonical and json.loads(canonical)["version"] == dcand.SNAPSHOT_VERSION


def test_the_snapshot_enforces_the_priority_of_the_active_analysis():
    with pytest.raises(ValueError):
        dcand.build_snapshot(preparation_view(), "MC.PA", "ratios_valorisation", dcand.BASIS_LOOKUP)
    with pytest.raises(ValueError):
        dcand.build_snapshot(preparation_view(), "MC.PA", "construction_these", dcand.BASIS_INITIAL_TURN)
    no_session = preparation_view(active_session=None, facts=(), statements=(),
                                  latest_thesis=dc.PreparationThesis(9, "Thèse.", None))
    initial = dcand.build_snapshot(no_session, "MC.PA", "construction_these", dcand.BASIS_INITIAL_TURN)
    assert initial.existing_thesis() == {"text": "Thèse.", "date": "date non enregistrée"}
    lookup = dcand.build_snapshot(no_session, "MC.PA", None, dcand.BASIS_LOOKUP)
    assert lookup.thesis_used is None and lookup.method_id is None
    with pytest.raises(ValueError):
        dataclasses.replace(initial, fingerprint="0" * 64)


def test_two_distinct_fingerprints_are_never_interchangeable():
    snap = dcand.build_snapshot(preparation_view(), "MC.PA", "construction_these", dcand.BASIS_ACTIVE_ANALYSIS)
    assert snap.fingerprint != dcand.canonical_bytes(snap.canonical())
    assert dcand.SNAPSHOT_DOMAIN != dcand.MARKET_DOMAIN and dcand.SNAPSHOT_DOMAIN.startswith(b"ORYX-DECRYPTAGE-PREP")


def lease(epoch=1):
    return dr.DecryptageLease(uuid.UUID("00000000-0000-4000-8000-0000000000aa"), epoch, dr.LeaseToken.generate(),
                              NOW + timedelta(seconds=300), "conv-1", TURN, 1)


def candidate(**overrides):
    market = dm.parse_eodhd(MC, fundamentals(), REALTIME)
    snap = dcand.build_snapshot(preparation_view(), "MC.PA", "construction_these", dcand.BASIS_ACTIVE_ANALYSIS)
    the_lease = overrides.pop("lease", None) or lease()
    fields = dict(contract_version=dcand.CANDIDATE_VERSION,
                  turn=dcand.TurnInputs("conv-1", TURN, "MC.PA", "Pourquoi ?", "", None), lease=the_lease,
                  owner_user_id="user-a", instrument=MC, market=market,
                  market_fingerprint=dcand.market_fingerprint(market), preparation=snap, prepared_read_at=NOW,
                  prepared_lease_expires_at=the_lease.lease_expires_at,
                  payload=dcand.CandidatePayload("MC.PA", "LVMH", "construction_these", "Texte.", 600.5, "EUR"),
                  step_marker="moat", response_diagnostics=(), model="claude-test-model")
    fields.update(overrides)
    return dcand.GeneratedDecryptageCandidate(**fields)


def test_the_candidate_is_closed_immutable_and_never_serialized():
    cand = candidate()
    secret = cand.lease.token.reveal().hex()
    for text in (repr(cand), str(cand), f"{cand}", repr(cand.redacted())):
        assert secret not in text and "Texte." not in text
    with pytest.raises(dataclasses.FrozenInstanceError):
        cand.step_marker = "risques"
    for operation in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            operation(cand)
    with pytest.raises(TypeError):
        json.dumps(cand)
    with pytest.raises(TypeError):
        hash(cand)
    payload = cand.payload.as_dict()
    payload["analysis"] = "modifié"
    assert cand.payload.as_dict()["analysis"] == "Texte."
    # Aucun conteneur mutable dans le candidat ni ses sous-objets.
    def walk(value, seen=()):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            for f in dataclasses.fields(value):
                yield from walk(getattr(value, f.name))
        elif isinstance(value, tuple):
            for item in value:
                yield from walk(item)
        else:
            yield value
    assert not any(isinstance(v, (dict, list, set, bytearray)) for v in walk(cand))


@pytest.mark.parametrize("override", [
    dict(owner_user_id="user-b"), dict(lease="epoch2"), dict(step_marker="inconnu"),
    dict(response_diagnostics=("unknown_marker",)), dict(market_fingerprint="0" * 64), dict(model=""),
    dict(payload=dcand.CandidatePayload("MC.PA", "LVMH", "autre", "Texte.", 600.5, "EUR")),
    dict(payload=dcand.CandidatePayload("MC.PA", "LVMH", "construction_these", "Texte.", 1.0, "EUR")),
    dict(turn=dcand.TurnInputs("conv-2", TURN, "MC.PA", "Pourquoi ?", "", None)),
    dict(prepared_lease_expires_at=NOW)])
def test_an_incoherent_candidate_cannot_be_built(override):
    if override.get("lease") == "epoch2":
        override["lease"] = lease(2)
    with pytest.raises(ValueError):
        candidate(**override)


def test_the_payload_refuses_non_finite_prices_and_empty_text():
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            dcand.CandidatePayload("MC.PA", "LVMH", None, "Texte.", bad, "EUR")
    with pytest.raises(ValueError):
        dcand.CandidatePayload("MC.PA", "LVMH", None, "  ", None, "EUR")
    with pytest.raises(ValueError):
        dcand.CandidatePayload("MC.PA", "LVMH", None, "Texte.", None, "EUR", disclaimer="autre")


def test_the_disclaimer_is_the_historical_one():
    import api
    assert dcand.DISCLAIMER == api.DECRYPTAGE_DISCLAIMER


# --------------------------------------------------------------------------
# Commande READ
# --------------------------------------------------------------------------

def read_command(**overrides):
    fields = dict(reservation_id=uuid.uuid4(), lease_epoch=1, lease_token=dr.LeaseToken.generate(),
                  conversation_key="conv-1", client_turn_id=TURN, ticker_input=" mc.pa ", question=" Q ",
                  context="", last_method_id=None, resolved_ticker="MC.PA")
    fields.update(overrides)
    return c.ReadDecryptagePreparationContext(**fields)


@pytest.mark.parametrize("override", [
    dict(reservation_id=str(uuid.uuid4())), dict(lease_epoch=0), dict(lease_epoch=True), dict(lease_token=b"x" * 32),
    dict(conversation_key=" conv"), dict(client_turn_id=str(TURN)), dict(ticker_input="  "),
    dict(question="x" * (16 * 1024 + 1)), dict(last_method_id="m" * 65), dict(resolved_ticker="mc.pa"),
    dict(resolved_ticker="MC PA"), dict(resolved_ticker=None), dict(question="a\x00")])
def test_the_read_command_admits_its_inputs_structurally(override):
    with pytest.raises(ValueError):
        read_command(**override)


def test_the_read_command_carries_no_owner_and_never_reveals_the_token():
    command = read_command()
    assert "user_id" not in {f.name for f in dataclasses.fields(command)}
    assert command.lease_token.reveal().hex() not in repr(command)
    assert (command.ticker, command.texts) == ("MC.PA", ("Q", ""))
    for operation in (pickle.dumps, copy.deepcopy):
        with pytest.raises(TypeError):
            operation(command)
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.resolved_ticker = "AI.PA"
    reservation = c.ReserveDecryptageTurn("conv-1", TURN, "MC.PA", "Q", "", None)
    with pytest.raises(ValueError):
        c.ReadDecryptagePreparationContext.from_lease(lease(), c.ReserveDecryptageTurn("conv-2", TURN, "MC.PA", "",
                                                                                       "", None), "MC.PA")
    assert c.ReadDecryptagePreparationContext.from_lease(lease(), reservation, "MC.PA").client_turn_id == TURN


def test_subclasses_of_the_read_command_are_refused():
    @dataclasses.dataclass(frozen=True, slots=True)
    class Smuggled(c.ReadDecryptagePreparationContext):
        pass

    runner = AuthorizedTransactionRunner(sa.create_engine("postgresql+psycopg2://nobody@127.0.0.1:1/oryx_never_test"),
                                         make_keyring(), decryptage_keyring=DecryptageKeyring({"d": bytes(32)}, "d"))
    with pytest.raises(UnknownCommand):
        runner.run(None, Smuggled(**{f.name: getattr(read_command(), f.name)
                                     for f in dataclasses.fields(c.ReadDecryptagePreparationContext)}))


# --------------------------------------------------------------------------
# Orchestrateur sans base
# --------------------------------------------------------------------------

def _unconnected_runner():
    engine = sa.create_engine("postgresql+psycopg2://nobody@127.0.0.1:1/oryx_never_test")
    return engine, AuthorizedTransactionRunner(engine, make_keyring(),
                                               decryptage_keyring=DecryptageKeyring({"d": bytes(range(32))}, "d"))


def test_without_lease_granted_nothing_is_called_not_even_postgresql():
    engine, runner = _unconnected_runner()
    clock = FakeClock()
    eodhd, fmp, claude = FakeEodhd(clock), FakeFmp(clock), FakeClaude(clock)
    orchestrator = dg.DecryptageGenerationOrchestrator(runner, eodhd=eodhd, fmp=fmp, claude=claude, model="m",
                                                       clock=clock)
    reservation = c.ReserveDecryptageTurn("conv-1", TURN, "MC.PA", "", "", None)
    principal = AuthorizedPrincipal("user-a", uuid.uuid4(), uuid.uuid4(), "otp")
    for reserved in (AuthorizationOutcome(AUTHORIZED, None, principal),
                     AuthorizationOutcome(AUTHORIZED, dr.ReservationBlocked(uuid.uuid4(), "legacy_collision"),
                                          principal),
                     AuthorizationOutcome(AUTHORIZED, dr.LeaseGranted(lease(), 1), None),
                     AuthorizationOutcome("not_found"), "LeaseGranted", None):
        assert orchestrator.generate(None, reservation, reserved).reason == dg.LEASE_NOT_GRANTED
    assert eodhd.calls == fmp.calls == claude.calls == [] and engine.pool.checkedout() == 0


def test_the_orchestrator_needs_the_real_runner_and_complete_transports():
    engine, runner = _unconnected_runner()
    clock = FakeClock()
    with pytest.raises(TypeError):
        dg.DecryptageGenerationOrchestrator(object(), eodhd=FakeEodhd(clock), fmp=FakeFmp(clock),
                                            claude=FakeClaude(clock), model="m")
    with pytest.raises(TypeError):
        dg.DecryptageGenerationOrchestrator(runner, eodhd=object(), fmp=FakeFmp(clock), claude=FakeClaude(clock),
                                            model="m")
    with pytest.raises(TypeError):
        dg.DecryptageGenerationOrchestrator(runner, eodhd=FakeEodhd(clock), fmp=FakeFmp(clock),
                                            claude=FakeClaude(clock), model="")
    orchestrator = dg.DecryptageGenerationOrchestrator(runner, eodhd=FakeEodhd(clock), fmp=FakeFmp(clock),
                                                       claude=FakeClaude(clock), model="m")
    assert repr(orchestrator) == "DecryptageGenerationOrchestrator()"
    with pytest.raises(TypeError):
        pickle.dumps(orchestrator)


def test_outcomes_are_closed():
    with pytest.raises(ValueError):
        dg.GenerationOutcome(dg.REFUSED, "inventé")
    with pytest.raises(ValueError):
        dg.GenerationOutcome(dg.PROVIDER_FAILED, dr.GENERATION_UNAVAILABLE)  # issue de déclaration exigée
    with pytest.raises(ValueError):
        dg.GenerationOutcome(dg.PROVIDER_FAILED, dg.BUDGET_EXHAUSTED, declaration=dg.DECLARED)
    assert set(dg.REFUSAL_REASONS).isdisjoint(dr.FAILURE_CLASSES)  # une panne SQL n'est jamais un échec fournisseur


# --------------------------------------------------------------------------
# Transports réels (sans réseau)
# --------------------------------------------------------------------------

class _Response:
    def __init__(self, status=200, chunks=(b"[]",), clock=None, step=0.0):
        self.status_code, self._chunks, self._clock, self._step = status, chunks, clock, step
        self.closed = False

    def iter_content(self, size):
        for chunk in self._chunks:
            if self._clock is not None:
                self._clock.advance(self._step)
            yield chunk

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def test_http_transport_is_bounded_unretried_and_never_logs(caplog):
    clock = FakeClock()
    session = _Session(_Response(chunks=(b'[{"Code": "MC"}]',)))
    transport = providers.EodhdTransport("secret-key", session=session, clock=clock)
    with caplog.at_level(logging.DEBUG):
        assert transport.search("MC.PA", timeout_s=12.0) == [{"Code": "MC"}]
    url, kwargs = session.calls[0]
    assert url == "https://eodhd.com/api/search/MC.PA" and kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True and kwargs["timeout"] == (5.0, 5.0)
    assert kwargs["params"]["api_token"] == "secret-key" and "secret-key" not in caplog.text
    assert session.response.closed and "secret" not in repr(transport)
    transport.fundamentals("MC.PA", timeout_s=2.0)
    assert session.calls[-1][1]["timeout"] == (2.0, 2.0)
    with pytest.raises(TimeoutError):
        transport.realtime("MC.PA", timeout_s=0)
    assert len(session.calls) == 2  # aucun appel sans temps disponible


@pytest.mark.parametrize("response,error", [
    (_Response(status=500), ConnectionError), (_Response(chunks=(b"[NaN]",)), ValueError),
    (_Response(chunks=(b"{",)), ValueError), (_Response(chunks=(b"x" * (256 * 1024 + 1),)), ValueError),
    (_Response(status=301), ConnectionError)])
def test_http_transport_errors_are_closed(response, error):
    transport = providers.EodhdTransport("k", session=_Session(response), clock=FakeClock())
    with pytest.raises(error):
        transport.search("MC", timeout_s=5.0)
    assert response.closed


def test_http_transport_maps_timeouts_and_absences():
    clock = FakeClock()
    slow = _Response(chunks=(b"[", b"]"), clock=clock, step=3.0)
    with pytest.raises(TimeoutError):
        providers.FmpTransport("k", session=_Session(slow), clock=clock).profile("MC.PA", timeout_s=5.0)
    with pytest.raises(TimeoutError):
        providers.FmpTransport("k", session=_Session(error=requests.exceptions.ReadTimeout()),
                               clock=clock).ratios("MC.PA", timeout_s=5.0)
    with pytest.raises(ConnectionError):
        providers.FmpTransport("k", session=_Session(error=requests.exceptions.ConnectionError()),
                               clock=clock).ratios("MC.PA", timeout_s=5.0)
    assert providers.FmpTransport("k", session=_Session(_Response(status=404)), clock=clock).profile(
        "MC.PA", timeout_s=5.0) is None


def test_the_default_http_session_never_retries():
    session = providers._session()
    for prefix in ("https://", "http://"):
        retries = session.get_adapter(prefix + "eodhd.com").max_retries
        assert retries.total == 0 and retries.read in (0, False)


def _claude(handler):
    return providers.ClaudeTransport("sk-test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def test_the_claude_transport_makes_exactly_one_request_without_sdk_retry():
    requests_seen = []

    def overloaded(request):
        requests_seen.append(request)
        return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}})

    transport = _claude(overloaded)
    assert transport.max_retries == 0 and "sk-test" not in repr(transport)
    with pytest.raises(ConnectionError):
        transport.create_message(model="m", system="s", user="u", max_tokens=10, timeout_s=30.0)
    assert len(requests_seen) == 1
    assert requests_seen[0].extensions["timeout"]["read"] == 30.0


def test_the_claude_transport_maps_timeouts_and_parses_blocks():
    def timeout(request):
        raise httpx2.ReadTimeout("lent", request=request)

    with pytest.raises(TimeoutError):
        _claude(timeout).create_message(model="m", system="s", user="u", max_tokens=10, timeout_s=30.0)
    with pytest.raises(TimeoutError):
        _claude(timeout).create_message(model="m", system="s", user="u", max_tokens=10, timeout_s=0)

    def ok(request):
        body = json.loads(request.content)
        assert body["system"] == "s" and body["messages"] == [{"role": "user", "content": "u"}]
        return httpx2.Response(200, json={"id": "msg", "type": "message", "role": "assistant", "model": "m",
                                          "content": [{"type": "text", "text": "Bonjour"}], "stop_reason": "end_turn",
                                          "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    assert _claude(ok).create_message(model="m", system="s", user="u", max_tokens=10, timeout_s=30.0) == {
        "stop_reason": "end_turn", "content": [{"type": "text", "text": "Bonjour"}]}
