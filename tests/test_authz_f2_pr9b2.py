"""R1 PR 9-B2 — préparation pédagogique, fournisseurs et candidat interne
dormant, contre un VRAI PostgreSQL (16 et 18 en CI).

VRAI runner, registre de PRODUCTION (dix-sept commandes), sessions F2
réelles, réservations B1 réelles ; fournisseurs et horloge SIMULÉS (aucun
réseau). Chaque test vérifie qu'aucun effet canonique n'est committé par B2
(état complet comparé, xmin compris) sauf la déclaration d'échec B1
explicitement attendue. Couvre :

- ReadDecryptagePreparationContext : propriété, capacité (état, epoch,
  jeton, échéance, tour), intention HMAC, trousseau, une seule instruction,
  aucune écriture, priorité de l'analyse active, sessions multiples, étape
  invalide, thèse historique, bornes, ordre, valeurs non finies, lignes
  d'autrui, panne SQL, ancre R1-C4 (obsolète au même ticker / changement de
  ticker), level ET level_revision ;
- orchestration : bail exigé, réautorisation ReadProfile, fournisseurs
  (EODHD, repli FMP, aucun yfinance), instrument introuvable / ambigu,
  classification et déclaration des échecs, budget par étape, un seul appel
  Claude, réponse inexploitable, aucune transaction ouverte pendant les
  appels externes, échéance du bail, secret.
"""
import logging
import uuid

import pytest
import sqlalchemy as sa

from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_generation as dg
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.runner import (AUTHORIZED, IDEMPOTENCY_CONFLICT, NOT_FOUND, REVISION_CONFLICT, SESSION_INVALID,
                                  UNAVAILABLE, AuthorizationOutcome)
from tests.authz_f2_pr9b1_support import CONV, legacy_route, reserve_command  # noqa: F401 — fixture
from tests.authz_f2_pr9b2_support import (DELETE, FMP_RATIOS, MODEL, REPLY, TICKER, FakeClaude, FakeClock, FakeEodhd,
                                          FakeFmp, PreparationEnv, fundamentals, reply)
from tests.authz_f2_support import BOB, USER_A, USER_B
from tests.f2_support import prepare_schema
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = PreparationEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


def lease_of(outcome) -> dr.DecryptageLease:
    return outcome.value.lease


def view_of(env, issued, lease, command, ticker=TICKER):
    outcome = env.read(issued, lease, command, ticker)
    assert outcome.status == AUTHORIZED, outcome
    return outcome.value


# --------------------------------------------------------------------------
# Lecture de préparation : capacité, propriété, intention
# --------------------------------------------------------------------------

def test_the_read_is_one_statement_without_any_write_and_carries_level_and_revision(env, alice):
    command, reserved = env.reserve(alice)
    lease = lease_of(reserved)
    env.set_profile("avance", 7)
    before = env.full_state()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(env.engine, "before_cursor_execute", capture)
    try:
        view = view_of(env, alice, lease, command)
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", capture)
    assert type(view) is dc.DecryptagePreparationView
    assert (view.reservation_id, view.lease_epoch, view.attempts_used) == (lease.reservation_id, 1, 1)
    assert (view.owner_user_id, view.conversation_key, view.client_turn_id) == (USER_A, CONV, command.client_turn_id)
    assert (view.ticker, view.level, view.level_revision) == (TICKER, "avance", 7)
    assert view.anchor == dc.PreparationAnchor(dc.ANCHOR_NONE, None, None)
    assert (view.active_session, view.facts, view.statements, view.latest_thesis) == (None, (), (), None)
    assert view.lease_expires_at == lease.lease_expires_at and view.read_at < view.lease_expires_at
    business = [s for s in statements if "WITH o AS" in s]
    assert len(business) == 1  # tout le contexte : UNE instruction (un seul instantané)
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
    assert env.full_state() == before  # aucune écriture, xmin compris
    assert "token" not in repr(view).lower() and lease.token.reveal().hex() not in repr(view)


def test_a_foreign_or_absent_reservation_is_not_found(env, alice):
    command, reserved = env.reserve(alice)
    bob = env.confirmed(BOB, USER_B)
    lease = lease_of(reserved)
    assert env.read(bob, lease, command).status == NOT_FOUND  # capacité d'autrui, même jeton exact
    forged = dr.DecryptageLease(uuid.uuid4(), 1, lease.token, lease.lease_expires_at, CONV, command.client_turn_id, 1)
    assert env.read(alice, forged, command).status == NOT_FOUND


def test_stale_or_forged_capabilities_are_revision_conflicts(env, alice):
    command, reserved = env.reserve(alice)
    lease = lease_of(reserved)
    wrong_token = dr.DecryptageLease(lease.reservation_id, 1, dr.LeaseToken.generate(), lease.lease_expires_at, CONV,
                                     command.client_turn_id, 1)
    wrong_epoch = dr.DecryptageLease(lease.reservation_id, 2, lease.token, lease.lease_expires_at, CONV,
                                     command.client_turn_id, 1)
    for forged in (wrong_token, wrong_epoch):
        assert env.read(alice, forged, command).status == REVISION_CONFLICT
    # Capacité d'un AUTRE tour du même propriétaire.
    other_command, other = env.reserve(alice, reserve_command())
    mixed = c.ReadDecryptagePreparationContext(
        lease.reservation_id, 1, lease.token, CONV, other_command.client_turn_id, command.ticker_input,
        command.question, command.context, command.last_method_id, TICKER)
    assert env.run(alice, mixed).status == REVISION_CONFLICT
    # Bail expiré, libéré (backoff) puis bloqué : jamais lisible.
    env.expire(lease.reservation_id)
    assert env.read(alice, lease, command).status == REVISION_CONFLICT
    released = lease_of(other)
    assert env.run(alice, c.RecordDecryptageAttemptFailure.from_lease(released, dr.GENERATION_UNAVAILABLE)).status \
        == AUTHORIZED
    assert env.read(alice, released, other_command).status == REVISION_CONFLICT


def test_inputs_differing_from_the_reserved_intent_are_idempotency_conflicts(env, alice):
    command, reserved = env.reserve(alice, reserve_command(question="Pourquoi ?", last_method_id="construction_these"))
    lease = lease_of(reserved)
    for altered in (reserve_command(command.client_turn_id, question="Autre ?", last_method_id="construction_these"),
                    reserve_command(command.client_turn_id, question="Pourquoi ?", last_method_id=None),
                    reserve_command(command.client_turn_id, ticker="AIR.PA", question="Pourquoi ?",
                                    last_method_id="construction_these")):
        assert env.read(alice, lease, altered).status == IDEMPOTENCY_CONFLICT
    # Normalisation B1 identique : espaces de bord, casse du ticker.
    same = reserve_command(command.client_turn_id, ticker=" mc.pa ", question="  Pourquoi ?  ",
                           last_method_id="construction_these")
    assert env.read(alice, lease, same).status == AUTHORIZED


def test_without_the_decryptage_keyring_the_read_is_unavailable_before_any_transaction(env, alice, caplog):
    command, reserved = env.reserve(alice)
    calls = []
    runner = env.b1_runner(decryptage_keys=None, hook=calls.append)
    with caplog.at_level(logging.WARNING, logger="oryx.authz_f2"):
        read = c.ReadDecryptagePreparationContext.from_lease(lease_of(reserved), command, TICKER)
        assert runner.run(alice.credential(), read).status == UNAVAILABLE
    assert calls == [] and "error=decryptage_keyring_missing" in caplog.text


def test_a_revoked_session_cannot_read_even_with_the_exact_lease(env, alice):
    command, reserved = env.reserve(alice)
    assert env.service.revoke_session(alice.session_id, "logout") is True
    assert env.read(alice, lease_of(reserved), command).status == SESSION_INVALID


# --------------------------------------------------------------------------
# Contexte pédagogique
# --------------------------------------------------------------------------

def test_the_active_analysis_has_priority_and_carries_its_open_step_in_deterministic_order(env, alice):
    command, reserved = env.reserve(alice)
    session_id = env.add_session(step="valorisation")
    env.add_thesis("Ancienne thèse terminée.")  # jamais lue avec une session active
    late = env.add_statement(session_id, "moat", "Marque.", date="2026-02-01 00:00:00")
    early = env.add_statement(session_id, "business", "Luxe.", date="2026-01-01 00:00:00")
    same_day = env.add_statement(session_id, "chiffres", "Marges.", date="2026-02-01 00:00:00")
    f2 = env.add_fact(session_id, "roe", 24.0, date="2026-01-02 00:00:00")
    f1 = env.add_fact(session_id, "operating_margin", 26.0, date="2026-01-01 00:00:00")
    env.add_session(ticker="AIR.PA", step="moat")  # autre ticker : ignoré
    view = view_of(env, alice, lease_of(reserved), command)
    assert view.active_session == dc.PreparationActiveSession(session_id, "valorisation")
    assert [s.statement_id for s in view.statements] == [early, late, same_day]  # (statement_date, id)
    assert [(f.fact_id, f.fact_type, f.value) for f in view.facts] == [(f1, "operating_margin", 26.0),
                                                                     (f2, "roe", 24.0)]
    assert view.latest_thesis is None


def test_multiple_active_sessions_are_refused_closed_without_choosing(env, alice):
    command, reserved = env.reserve(alice)
    env.add_session(step="business")
    env.add_session(step="moat", offset=10)
    before = env.full_state()
    outcome = env.read(alice, lease_of(reserved), command)
    assert (outcome.status, outcome.value) == (AUTHORIZED,
                                               dc.DecryptagePreparationRefused(dc.MULTIPLE_ACTIVE_SESSIONS))
    assert env.full_state() == before  # ni réparation, ni suppression


@pytest.mark.parametrize("step", [None, "swot_final", "inconnue", "x" * 40])
def test_an_unusable_open_step_is_refused_never_reset_to_business(env, alice, step):
    command, reserved = env.reserve(alice)
    env.add_session(step=step)
    assert env.read(alice, lease_of(reserved), command).value == dc.DecryptagePreparationRefused(
        dc.ACTIVE_SESSION_STEP_INVALID)


def test_without_an_active_analysis_the_latest_thesis_is_read_nulls_last(env, alice):
    command, reserved = env.reserve(alice)
    env.add_session(status="completed", step="swot_final")
    old = env.add_thesis("Thèse 2025.", created_at="2025-06-01 00:00:00")
    newest = env.add_thesis("Thèse 2026.", created_at="2026-03-04 00:00:00")
    env.add_thesis("Thèse sans date.", created_at=None)
    env.add_thesis("Autre utilisateur.", user_id=USER_A, ticker="AIR.PA")
    view = view_of(env, alice, lease_of(reserved), command)
    assert view.active_session is None and view.facts == () and view.statements == ()
    assert view.latest_thesis == dc.PreparationThesis(newest, "Thèse 2026.", "04/03/2026")
    assert old != newest


@pytest.mark.parametrize("case", ["too_many_statements", "statement_too_long", "statements_total", "thesis_too_long",
                                  "too_many_facts", "step_too_long", "fact_type_too_long", "thesis_date_infinite"])
def test_out_of_bounds_contents_are_refused_never_truncated(env, alice, case):
    command, reserved = env.reserve(alice)
    if case == "thesis_too_long":
        env.add_thesis("x" * (dc.THESIS_TEXT_MAX_BYTES + 1))
    elif case == "thesis_date_infinite":
        env.add_thesis("Thèse.", created_at="infinity")
    else:
        session_id = env.add_session()
        if case == "too_many_statements":
            for i in range(dc.MAX_STATEMENTS + 1):
                env.add_statement(session_id, "business", f"t{i}")
        elif case == "statement_too_long":
            env.add_statement(session_id, "business", "é" * (dc.STATEMENT_TEXT_MAX_BYTES // 2 + 1))
        elif case == "statements_total":
            for _ in range(5):
                env.add_statement(session_id, "business", "x" * dc.STATEMENT_TEXT_MAX_BYTES)
        elif case == "too_many_facts":
            for i in range(dc.MAX_FACTS + 1):
                env.add_fact(session_id, "roe", float(i))
        elif case == "step_too_long":
            env.add_statement(session_id, "s" * 33, "texte")
        else:
            env.add_fact(session_id, "f" * 65, 1.0)
    assert env.read(alice, lease_of(reserved), command).value == dc.DecryptagePreparationRefused(
        dc.CONTEXT_OUT_OF_BOUNDS)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_historical_facts_are_refused(env, alice, value):
    command, reserved = env.reserve(alice)
    env.add_fact(env.add_session(), "roe", value)
    assert env.read(alice, lease_of(reserved), command).value == dc.DecryptagePreparationRefused(
        dc.CONTEXT_VALUE_INVALID)


def test_null_fact_values_stay_absent_never_zero(env, alice):
    command, reserved = env.reserve(alice)
    fact = env.add_fact(env.add_session(), "roe", None)
    assert view_of(env, alice, lease_of(reserved), command).facts == (dc.PreparationFact(fact, "roe", None),)


def test_foreign_rows_attached_to_the_active_session_are_an_invariant_break(env, alice, caplog):
    command, reserved = env.reserve(alice)
    env.confirmed(BOB, USER_B)
    session_id = env.add_session()
    env.add_statement(session_id, "business", "Texte d'autrui.", user_id=USER_B)
    with caplog.at_level(logging.ERROR, logger="oryx.authz_f2"):
        assert env.read(alice, lease_of(reserved), command).status == UNAVAILABLE
    assert "alert=decryptage_preparation_incoherent" in caplog.text and "autrui" not in caplog.text


def test_contributions_and_facts_are_strictly_the_owner_s(env, alice):
    command, reserved = env.reserve(alice)
    env.confirmed(BOB, USER_B)
    mine = env.add_session()
    bobs = env.add_session(user_id=USER_B)
    env.add_statement(bobs, "business", "Bob.", user_id=USER_B)
    env.add_fact(bobs, "roe", 1.0, user_id=USER_B)
    statement = env.add_statement(mine, "business", "Alice.")
    view = view_of(env, alice, lease_of(reserved), command)
    assert view.active_session.analysis_session_id == mine
    assert [s.statement_id for s in view.statements] == [statement] and view.facts == ()


def test_a_sql_failure_is_unavailable_never_an_absent_analysis(env, alice, monkeypatch):
    command, reserved = env.reserve(alice)
    env.add_session()
    monkeypatch.setattr(_adapters, "_PREPARATION_CONTEXT",
                        _adapters._PREPARATION_CONTEXT.replace("FROM analysis_sessions s, o", "FROM missing_table s, o"))
    assert env.read(alice, lease_of(reserved), command).status == UNAVAILABLE


def test_a_stale_same_ticker_anchor_is_refused_and_a_real_ticker_change_is_navigation(env, alice, legacy_route):
    command, reserved = env.reserve(alice)
    lease = lease_of(reserved)
    env.anchor_conversation(legacy_route)
    [session] = env.sessions()
    view = view_of(env, alice, lease, command)
    assert (view.anchor.state, view.anchor.analysis_session_id) == (dc.ANCHOR_CURRENT, session["id"])
    assert view.active_session.analysis_session_id == session["id"]
    # Autre entreprise demandée : navigation, jamais obsolète.
    other_command, other = env.reserve(alice, reserve_command(ticker="AIR.PA"))
    other_view = view_of(env, alice, lease_of(other), other_command, "AIR.PA")
    assert other_view.anchor.state == dc.ANCHOR_OTHER_TICKER and other_view.active_session is None
    # La tentative de l'ancre n'est plus active : même ticker => stale, avant tout appel.
    env.execute("UPDATE analysis_sessions SET status = 'abandoned' WHERE id = :i", i=session["id"])
    assert env.read(alice, lease, command).value == dc.DecryptagePreparationRefused(dc.STALE_CONVERSATION_CONTEXT)
    assert view_of(env, alice, lease_of(other), other_command, "AIR.PA").anchor.state == dc.ANCHOR_OTHER_TICKER


def test_a_closed_anchor_event_is_stale_even_with_the_session_still_active(env, alice, legacy_route):
    command, reserved = env.reserve(alice)
    env.anchor_conversation(legacy_route)
    env.execute("UPDATE cognitive_events SET status = 'finalized', closed_at = clock_timestamp()")
    assert env.read(alice, lease_of(reserved), command).value == dc.DecryptagePreparationRefused(
        dc.STALE_CONVERSATION_CONTEXT)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def test_a_full_generation_produces_an_internal_candidate_and_commits_nothing(env, alice, caplog):
    command, reserved = env.reserve(alice, reserve_command(question="", context=""))
    lease = lease_of(reserved)
    orchestrator = env.orchestrator()
    before = env.full_state()
    with caplog.at_level(logging.DEBUG):
        outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert outcome.status == dg.GENERATED, outcome
    candidate = outcome.candidate
    assert type(candidate) is dcand.GeneratedDecryptageCandidate
    assert env.full_state() == before  # aucune écriture : réservation, cognition, profil (xmin compris)
    [row] = env.reservations()
    assert (row["state"], row["resolved_ticker"], row["assistant_delivery_id"]) == ("reserved", None, None)
    assert env.count("assistant_deliveries") == 0
    assert env.provider_calls() == ["search", "fundamentals", "realtime", "create_message"]
    assert len(env.claude.requests) == 1 and env.claude.requests[0]["model"] == MODEL
    assert env.claude.requests[0]["max_tokens"] == dg.MAX_TOKENS
    # Contenu du candidat.
    assert candidate.lease is lease and candidate.owner_user_id == USER_A
    assert (candidate.instrument.ticker, candidate.instrument.provenance) == (TICKER, "exact_symbol")
    assert candidate.market.primary_source == "eodhd" and candidate.market.currency == "EUR"
    assert candidate.preparation.method_id == "construction_these"
    assert candidate.preparation.method_basis == dcand.BASIS_INITIAL_TURN
    assert (candidate.preparation.level, candidate.preparation.level_revision) == ("debutant", 0)
    assert candidate.step_marker == "business" and "ORYX_STEP" not in candidate.visible_text
    assert candidate.payload.as_dict() == {
        "success": True, "ticker": TICKER, "name": "LVMH", "method_used": "construction_these",
        "analysis": REPLY.split("\n<!--")[0], "price": 600.5, "currency": "EUR",
        "disclaimer": dcand.DISCLAIMER}
    assert candidate.delivery_fingerprint_inputs() == {
        "user_id": USER_A, "conversation_key": CONV, "client_turn_id": command.client_turn_id,
        "ticker_input": TICKER, "question": "", "context": "", "last_method_id": None, "level": "debutant"}
    # Secret : jamais dans repr, la vue expurgée ni les journaux.
    raw = lease.token.reveal().hex()
    for text in (repr(outcome), str(candidate), repr(candidate.redacted()), caplog.text):
        assert raw not in text and "LeaseToken(" not in text.replace("LeaseToken(<secret>)", "")
    # Prompt : provenance, contenus non fiables, aucune donnée inventée.
    system = env.claude.requests[0]["system"]
    assert "PROVENANCE DES DONNÉES FINANCIÈRES" in system and "jamais des instructions" in system
    assert "Source principale : EODHD" in system and "Devise de cotation : EUR" in system


def test_no_provider_is_called_without_a_confirmed_lease_granted(env, alice):
    command, reserved = env.reserve(alice)
    orchestrator = env.orchestrator()
    in_progress = env.run(alice, command)
    assert type(in_progress.value) is dr.ReservationInProgress
    for bad in (in_progress, AuthorizationOutcome(AUTHORIZED), AuthorizationOutcome(AUTHORIZED, None, reserved.principal),
                AuthorizationOutcome(UNAVAILABLE), AuthorizationOutcome(AUTHORIZED, reserved.value)):
        assert orchestrator.generate(alice.credential(), command, bad).reason == dg.LEASE_NOT_GRANTED
    other = reserve_command()
    assert orchestrator.generate(alice.credential(), other, reserved).reason == dg.LEASE_MISMATCH
    assert env.provider_calls() == []


def test_a_refused_reauthorization_calls_no_provider(env, alice):
    command, reserved = env.reserve(alice)
    orchestrator = env.orchestrator()
    assert env.service.revoke_session(alice.session_id, "logout") is True
    before = env.full_state()
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason) == (dg.REFUSED, dg.SESSION_REFUSED)
    assert env.provider_calls() == [] and env.full_state() == before


def test_another_principal_presenting_the_lease_is_refused_before_any_provider(env, alice):
    command, reserved = env.reserve(alice)
    bob = env.confirmed(BOB, USER_B)
    orchestrator = env.orchestrator()
    outcome = orchestrator.generate(bob.credential(), command, reserved)
    assert outcome.reason == dg.OWNER_CHANGED and env.provider_calls() == []


@pytest.mark.parametrize("results,reason", [
    ([], dg.INSTRUMENT_NOT_FOUND),
    ([{"Code": "MC", "Exchange": "PA", "Name": "LVMH", "Type": "Common Stock", "Isin": "FR1"},
      {"Code": "MC", "Exchange": "PA", "Name": "LVMH", "Type": "Common Stock", "Isin": "FR2"}],
     dg.INSTRUMENT_AMBIGUOUS)])
def test_unidentified_instruments_are_refused_without_declaration(env, alice, results, reason):
    command, reserved = env.reserve(alice)
    orchestrator = env.orchestrator(eodhd=FakeEodhd(FakeClock(), responses={"search": results}))
    before = env.full_state()
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason, outcome.declaration) == (dg.REFUSED, reason, None)
    assert env.provider_calls() == ["search"] and env.full_state() == before


def _declared(env, outcome, failure):
    assert (outcome.status, outcome.reason, outcome.declaration) == (dg.PROVIDER_FAILED, failure, dg.DECLARED)
    [row] = env.reservations()
    assert (row["state"], row["lease_token_hash"], row["attempts_used"]) == ("reserved", None, 1)
    assert row["retry_not_before_at"] is not None  # backoff B1 de 30 s
    assert env.count("assistant_deliveries") == 0 and env.count("analysis_sessions") == 0


def test_a_search_outage_is_a_declared_market_data_failure(env, alice):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    orchestrator = env.orchestrator(clock, eodhd=FakeEodhd(clock, responses={"search": TimeoutError("lent")}))
    _declared(env, orchestrator.generate(alice.credential(), command, reserved), dr.MARKET_DATA_UNAVAILABLE)
    assert env.provider_calls() == ["search"]


def test_fmp_is_a_controlled_fallback_and_yfinance_is_never_reached(env, alice, monkeypatch):
    import core.data_fetcher as legacy
    for name in ("fetch_financial_data", "_fetch_fcf_yfinance", "_fetch_yfinance_full", "_fetch_eod", "_fetch_fmp"):
        monkeypatch.setattr(legacy, name, lambda *a, **k: pytest.fail("chemin historique atteint"))
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    orchestrator = env.orchestrator(clock, eodhd=FakeEodhd(clock, responses={"fundamentals": ConnectionError("x")}))
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert outcome.status == dg.GENERATED, outcome
    market = outcome.candidate.market
    assert market.primary_source == "fmp" and market.field("revenue_growth").status == "absent"  # YoY != CAGR
    assert market.value("operating_margin") == 26.0 and market.field("operating_margin").period == "FY:2025-12-31"
    assert env.provider_calls() == ["search", "fundamentals", "profile", "ratios", "create_message"]


def test_eodhd_gaps_are_complemented_only_for_the_same_fiscal_year(env, alice):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    eodhd = FakeEodhd(clock, responses={"fundamentals": fundamentals(
        **{"Financials__Income_Statement__yearly__2025-12-31__grossProfit": DELETE})})
    stale_ratios = [dict(FMP_RATIOS[0], date="2024-12-31", grossProfitMargin=0.5)]
    orchestrator = env.orchestrator(clock, eodhd=eodhd, fmp=FakeFmp(clock, responses={"ratios": stale_ratios}))
    market = orchestrator.generate(alice.credential(), command, reserved).candidate.market
    assert market.field("gross_margin_latest").status == "absent" and market.complemented == ()  # autre exercice
    command2, reserved2 = env.reserve(alice)
    clock = FakeClock()
    orchestrator = env.orchestrator(clock, eodhd=FakeEodhd(clock, responses={"fundamentals": fundamentals(
        **{"Financials__Income_Statement__yearly__2025-12-31__grossProfit": DELETE})}))
    market = orchestrator.generate(alice.credential(), command2, reserved2).candidate.market
    assert market.complemented == ("gross_margin_latest",) and market.field("gross_margin_latest").source == "fmp"
    assert market.value("gross_margin_latest") == 68.0


def test_all_market_sources_down_is_a_declared_failure_and_claude_is_never_called(env, alice):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    orchestrator = env.orchestrator(clock, eodhd=FakeEodhd(clock, responses={"fundamentals": None}),
                                    fmp=FakeFmp(clock, responses={"profile": ConnectionError("x")}))
    _declared(env, orchestrator.generate(alice.credential(), command, reserved), dr.MARKET_DATA_UNAVAILABLE)
    assert "create_message" not in env.provider_calls()


@pytest.mark.parametrize("response,failure", [
    (TimeoutError("lent"), dr.GENERATION_UNAVAILABLE),
    (ConnectionError("529"), dr.GENERATION_UNAVAILABLE),
    (reply(""), dr.GENERATION_UNUSABLE),
    (reply("   \n<!--ORYX_STEP:business-->"), dr.GENERATION_UNUSABLE),
    (reply("Texte tronqué", "max_tokens"), dr.GENERATION_UNUSABLE),
    ({"stop_reason": "end_turn", "content": []}, dr.GENERATION_UNUSABLE),
    ({"stop_reason": "end_turn", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
     dr.GENERATION_UNUSABLE),
])
def test_claude_failures_are_classified_and_declared_after_one_call(env, alice, response, failure):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    orchestrator = env.orchestrator(clock, claude=FakeClaude(clock, responses={"create_message": response}))
    _declared(env, orchestrator.generate(alice.credential(), command, reserved), failure)
    assert env.claude.methods() == ["create_message"]  # un seul appel, aucun retry


def test_no_transaction_is_open_during_any_external_call(env, alice):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    seen = []

    def probe():
        seen.append((env.open_f2_transactions(), env.engine.pool.checkedout()))

    actions = {m: probe for m in ("search", "fundamentals", "realtime")}
    orchestrator = env.orchestrator(clock, eodhd=FakeEodhd(clock, actions=actions),
                                    claude=FakeClaude(clock, actions={"create_message": probe}))
    assert orchestrator.generate(alice.credential(), command, reserved).status == dg.GENERATED
    assert seen == [(0, 0)] * 4


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------

def test_each_provider_call_receives_at_most_its_remaining_allowance(env, alice):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    orchestrator = env.orchestrator(clock, eodhd=FakeEodhd(clock, delays={"search": 4.0, "fundamentals": 30.0}))
    assert orchestrator.generate(alice.credential(), command, reserved).status == dg.GENERATED
    timeouts = {m: t for m, _, t in env.eodhd.calls + env.claude.calls}
    assert timeouts["search"] == pytest.approx(15.0)
    assert timeouts["fundamentals"] == pytest.approx(80.0) and timeouts["realtime"] == pytest.approx(50.0)
    assert timeouts["create_message"] == pytest.approx(105.0)


@pytest.mark.parametrize("step,failure", [("search", dr.MARKET_DATA_UNAVAILABLE),
                                          ("fundamentals", dr.MARKET_DATA_UNAVAILABLE),
                                          ("create_message", dr.GENERATION_UNAVAILABLE)])
def test_a_late_provider_result_is_discarded_and_classified_by_its_step(env, alice, step, failure):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    late = {step: lambda allowance: allowance + 0.5}
    eodhd = FakeEodhd(clock, delays={k: v for k, v in late.items() if k != "create_message"})
    claude = FakeClaude(clock, delays={k: v for k, v in late.items() if k == "create_message"})
    fmp = FakeFmp(clock, responses={"profile": None})
    orchestrator = env.orchestrator(clock, eodhd=eodhd, fmp=fmp, claude=claude)
    _declared(env, orchestrator.generate(alice.credential(), command, reserved), failure)
    if step != "create_message":
        assert env.claude.calls == []  # aucun appel Claude après un échec des données


def test_no_claude_call_is_started_when_the_remaining_time_is_insufficient(env, alice):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    # Données lentes mais valides : 15 + 80 consommés -> il reste < 20 s sur le bail indicatif raccourci.
    env.force(lease_of(reserved).reservation_id, "lease_expires_at = clock_timestamp() + interval '90 seconds'")
    eodhd = FakeEodhd(clock, delays={"search": 14.0, "fundamentals": 40.0, "realtime": 39.0})
    orchestrator = env.orchestrator(clock, eodhd=eodhd)
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason) == (dg.REFUSED, dg.BUDGET_EXHAUSTED)
    assert env.claude.calls == []


def test_a_slow_authorization_read_exhausts_the_f2_budget_without_declaring(env, alice):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    env.hooks["executed"] = lambda: clock.advance(25.0)  # ReadProfile au-delà du plafond F2 de 20 s
    orchestrator = env.orchestrator(clock)
    before = env.full_state()
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason, outcome.declaration) == (dg.REFUSED, dg.BUDGET_EXHAUSTED, None)
    assert env.provider_calls() == [] and env.full_state() == before


def test_no_candidate_is_delivered_once_the_indicative_lease_has_passed(env, alice):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    env.force(lease_of(reserved).reservation_id, "lease_expires_at = clock_timestamp() + interval '60 seconds'")
    claude = FakeClaude(clock, delays={"create_message": lambda allowance: allowance})  # jusqu'à l'échéance exacte
    orchestrator = env.orchestrator(clock, claude=claude)
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason) == (dg.REFUSED, dg.LEASE_EXPIRED)
    assert env.claude.calls[0][2] < 60.0  # borné par le bail indicatif, pas par les 105 s du plafond


def test_the_active_analysis_drives_the_prompt_and_the_snapshot(env, alice):
    command, reserved = env.reserve(alice, reserve_command(question="Ils vendent du luxe.", context="historique",
                                                           last_method_id="ratios_valorisation"))
    session_id = env.add_session(step="moat")
    env.add_statement(session_id, "business", "Du luxe.")
    env.add_fact(session_id, "roe", 20.0)
    env.add_fact(session_id, "roe", 24.0, date="2026-01-03 00:00:00")
    env.set_profile("avance", 3)
    orchestrator = env.orchestrator()
    candidate = orchestrator.generate(alice.credential(), command, reserved).candidate
    prep = candidate.preparation
    assert (prep.method_id, prep.method_basis) == ("construction_these", dcand.BASIS_ACTIVE_ANALYSIS)
    assert prep.active_session.current_step == "moat" and [f.value for f in prep.facts_used] == [24.0]
    assert (prep.level, prep.level_revision) == ("avance", 3)
    system = env.claude.requests[0]["system"]
    assert "Étape ACTUELLEMENT OUVERTE : Composante 2 (Moat)" in system and "NIVEAU UTILISATEUR : AVANCÉ" in system
    assert '"Du luxe."' in system and "ROE : 24.0" in system
    assert "HISTORIQUE DE CONVERSATION" in env.claude.requests[0]["user"]
    assert prep.fingerprint == prep.compute_fingerprint()


def test_a_validation_overrun_is_a_refusal_never_a_provider_failure(env, alice, monkeypatch):
    command, reserved = env.reserve(alice)
    clock = FakeClock()
    orchestrator = env.orchestrator(clock)
    original = dcand.validate_response

    def slow(raw):
        clock.advance(dg.STEP_CAPS["validation"] + 1.0)
        return original(raw)

    monkeypatch.setattr(dcand, "validate_response", slow)
    before = env.full_state()
    outcome = orchestrator.generate(alice.credential(), command, reserved)
    assert (outcome.status, outcome.reason, outcome.declaration) == (dg.REFUSED, dg.BUDGET_EXHAUSTED, None)
    assert env.full_state() == before and len(env.claude.requests) == 1
