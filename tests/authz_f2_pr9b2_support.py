"""Outils des tests R1 PR 9-B2 : préparation et génération Décrypter F2
(dormantes) contre un VRAI PostgreSQL, sous le VRAI runner et le registre
de PRODUCTION (dix-sept commandes ; aucun registre de test).

- PreparationEnv étend ReservationEnv (R1 PR 9-B1) : réservation réelle,
  sessions F2 réelles, données de contexte insérées explicitement par la
  connexion d'observation (AnalysisSession, faits, contributions, thèses),
  ancres conversationnelles produites par la VRAIE route /decryptage et le
  VRAI ACK historiques (doubles des seuls appels externes) ;
- FakeClock : horloge monotone simulée (aucune attente réelle) ;
- FakeEodhd / FakeFmp / FakeClaude : transports injectés, simulés, sans
  réseau ; chaque appel est enregistré (méthode, argument, timeout_s) et
  peut avancer l'horloge, lever une exception ou exécuter une action
  (mutation concurrente pendant l'appel, sonde des transactions ouvertes).
"""
import copy
import uuid

import sqlalchemy as sa

from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.decryptage_generation import DecryptageGenerationOrchestrator
from core.authz_f2.runner import AUTHORIZED
from tests.authz_f2_pr9a_support import legacy_ack
from tests.authz_f2_pr9b1_support import CONV, ReservationEnv, reserve_command
from tests.authz_f2_support import USER_A
from tests.f2_support import APP_NAME

MODEL = "claude-test-model"
TICKER = "MC.PA"
ISIN = "FR0000121014"
SEARCH_MC = [{"Code": "MC", "Exchange": "PA", "Name": "LVMH Moet Hennessy Louis Vuitton SE", "Type": "Common Stock",
              "Country": "France", "Isin": ISIN}]


def fundamentals(code="MC", currency="EUR", statement_currency="EUR", isin=ISIN, **overrides):
    data = {
        "General": {"Code": code, "Exchange": "PA", "Name": "LVMH", "Sector": "Consumer Cyclical",
                    "CurrencyCode": currency, "ISIN": isin, "UpdatedAt": "2026-10-30"},
        "Highlights": {"ReturnOnEquityTTM": 0.24, "EarningsShare": 25.1},
        "SharesStats": {"SharesOutstanding": 500000000},
        "Financials": {
            "Income_Statement": {"yearly": {
                "2025-12-31": {"totalRevenue": "90000000000", "operatingIncome": "23400000000",
                               "grossProfit": "61200000000", "incomeTaxExpense": "6000000000",
                               "incomeBeforeTax": "24000000000", "currency_symbol": statement_currency},
                "2024-12-31": {"totalRevenue": 84000000000, "currency_symbol": statement_currency},
                "2023-12-31": {"totalRevenue": 79000000000, "currency_symbol": statement_currency}}},
            "Balance_Sheet": {"yearly": {
                "2025-12-31": {"cashAndShortTermInvestments": 10000000000, "shortLongTermDebtTotal": 40000000000,
                               "totalStockholderEquity": 65000000000}}},
            "Cash_Flow": {"yearly": {"2025-12-31": {"freeCashFlow": 14900000000}}},
        },
    }
    for path, value in overrides.items():
        target = data
        *parents, leaf = path.split("__")
        for key in parents:
            target = target[key]
        if value is _DELETE:
            target.pop(leaf, None)
        else:
            target[leaf] = value
    return data


_DELETE = object()
DELETE = _DELETE
REALTIME = {"code": "MC.PA", "timestamp": 1793000000, "close": 600.5}
FMP_PROFILE = [{"symbol": TICKER, "companyName": "LVMH", "sector": "Consumer Cyclical", "currency": "EUR",
                "price": 601.0, "isin": ISIN}]
FMP_RATIOS = [{"symbol": TICKER, "date": "2025-12-31", "period": "FY", "reportedCurrency": "EUR",
               "operatingProfitMargin": 0.26, "grossProfitMargin": 0.68, "returnOnEquity": 0.23,
               "debtEquityRatio": 0.6, "freeCashFlowPerShare": 29.8}]
REPLY = "Commençons par le **business**. Qu'est-ce que LVMH vend, et à qui ?\n<!--ORYX_STEP:business-->"


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Transport:
    """Réponses programmées par méthode : valeur, exception, ou callable(arg)."""

    def __init__(self, clock, responses, delays=None, actions=None):
        self.clock = clock
        self.responses = dict(responses)
        self.delays = dict(delays or {})
        self.actions = dict(actions or {})
        self.calls = []

    def _call(self, method, arg, timeout_s):
        self.calls.append((method, arg, timeout_s))
        action = self.actions.get(method)
        if action is not None:
            action()
        delay = self.delays.get(method, 0.0)
        self.clock.advance(delay(timeout_s) if callable(delay) else delay)
        response = self.responses.get(method)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(arg)
        return copy.deepcopy(response)

    def methods(self):
        return [m for m, _, _ in self.calls]


class FakeEodhd(_Transport):
    def __init__(self, clock, **kwargs):
        responses = {"search": SEARCH_MC, "fundamentals": fundamentals(), "realtime": REALTIME}
        responses.update(kwargs.pop("responses", {}))
        super().__init__(clock, responses, **kwargs)

    def search(self, query, *, timeout_s):
        return self._call("search", query, timeout_s)

    def fundamentals(self, symbol, *, timeout_s):
        return self._call("fundamentals", symbol, timeout_s)

    def realtime(self, symbol, *, timeout_s):
        return self._call("realtime", symbol, timeout_s)


class FakeFmp(_Transport):
    def __init__(self, clock, **kwargs):
        responses = {"profile": FMP_PROFILE, "ratios": FMP_RATIOS}
        responses.update(kwargs.pop("responses", {}))
        super().__init__(clock, responses, **kwargs)

    def profile(self, symbol, *, timeout_s):
        return self._call("profile", symbol, timeout_s)

    def ratios(self, symbol, *, timeout_s):
        return self._call("ratios", symbol, timeout_s)


class FakeClaude(_Transport):
    def __init__(self, clock, text=REPLY, **kwargs):
        responses = {"create_message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}}
        responses.update(kwargs.pop("responses", {}))
        super().__init__(clock, responses, **kwargs)
        self.requests = []

    def create_message(self, *, model, system, user, max_tokens, timeout_s):
        self.requests.append({"model": model, "system": system, "user": user, "max_tokens": max_tokens})
        return self._call("create_message", model, timeout_s)


def reply(text, stop_reason="end_turn"):
    return {"stop_reason": stop_reason, "content": [{"type": "text", "text": text}]}


class PreparationEnv(ReservationEnv):
    """ReservationEnv + contexte pédagogique et orchestrateur B2."""

    def reserve(self, issued, command=None):
        command = command or reserve_command()
        outcome = self.run(issued, command)
        assert outcome.status == AUTHORIZED and type(outcome.value) is dr.LeaseGranted, outcome
        return command, outcome

    def read(self, issued, lease, command, ticker=TICKER, **kwargs):
        return self.run(issued, c.ReadDecryptagePreparationContext.from_lease(lease, command, ticker), **kwargs)

    def orchestrator(self, clock=None, *, eodhd=None, fmp=None, claude=None, runner=None, price_recent_max_age=None):
        clock = clock or FakeClock()
        self.clock = clock
        self.eodhd = eodhd or FakeEodhd(clock)
        self.fmp = fmp or FakeFmp(clock)
        self.claude = claude or FakeClaude(clock)
        return DecryptageGenerationOrchestrator(runner or self.b1_runner(), eodhd=self.eodhd, fmp=self.fmp,
                                                claude=self.claude, model=MODEL, clock=clock,
                                                price_recent_max_age=price_recent_max_age)

    def provider_calls(self):
        return self.eodhd.methods() + self.fmp.methods() + self.claude.methods()

    # -- données de contexte explicites ------------------------------------

    def execute(self, sql, **params):
        with self.observer.begin() as conn:
            conn.execute(sa.text(sql), params)

    def insert(self, sql, **params):
        """INSERT ... RETURNING id, committé ; rend l'identifiant."""
        with self.observer.begin() as conn:
            return conn.execute(sa.text(sql), params).scalar_one()

    def add_session(self, *, user_id=USER_A, ticker=TICKER, status="in_progress", step="business", offset=0):
        session_id = uuid.uuid4()
        self.execute("INSERT INTO analysis_sessions (id, user_id, ticker, status, current_step, started_at, "
                     "updated_at) VALUES (:i, :u, :t, :s, :c, clock_timestamp() + make_interval(secs => :o), "
                     "clock_timestamp() + make_interval(secs => :o))",
                     i=session_id, u=user_id, t=ticker, s=status, c=step, o=offset)
        return session_id

    def add_fact(self, session_id, fact_type, value, *, user_id=USER_A, ticker=TICKER, date="2026-01-01 00:00:00"):
        return self.insert("INSERT INTO analysis_facts (user_id, ticker, fact_type, fact_value, fact_date, "
                            "analysis_session_id) VALUES (:u, :t, :f, CAST(:v AS float8), CAST(:d AS timestamp), "
                            ":s) RETURNING id", u=user_id, t=ticker, f=fact_type, v=value, d=date,
                            s=session_id)

    def add_statement(self, session_id, step, text, *, user_id=USER_A, ticker=TICKER, date="2026-01-01 00:00:00"):
        return self.insert("INSERT INTO user_statements (user_id, ticker, step, statement_text, statement_date, "
                            "analysis_session_id) VALUES (:u, :t, :p, :x, CAST(:d AS timestamp), :s) RETURNING id",
                            u=user_id, t=ticker, p=step, x=text, d=date, s=session_id)

    def add_thesis(self, text, *, user_id=USER_A, ticker=TICKER, created_at="2026-01-01 00:00:00", session_id=None):
        return self.insert("INSERT INTO investment_theses (user_id, ticker, thesis_text, created_at, "
                            "analysis_session_id) VALUES (:u, :t, :x, CAST(:d AS timestamp), :s) RETURNING id",
                            u=user_id, t=ticker, x=text, d=created_at, s=session_id)

    def anchor_conversation(self, fake, conversation_key=CONV, *, marker="business", ticker=TICKER, user_id=USER_A):
        """Tour historique RÉEL (route /decryptage) puis ACK RÉEL : la
        conversation est ancrée à l'event ouvert par cette réponse."""
        import api
        fake.texts = [f"Réponse historique {marker}.\n<!--ORYX_STEP:{marker}-->"]
        with self.Sessions() as session:
            delivered = api.decryptage(api.DecryptageRequest(
                ticker=ticker, question="", context="", last_method_id=None, user_id=user_id,
                conversation_key=conversation_key, client_turn_id=str(uuid.uuid4())), db=session)
        legacy_ack(self, conversation_key, delivered["assistant_turn_id"], user_id=user_id)
        return delivered

    def open_f2_transactions(self) -> int:
        """Connexions F2 du runner en transaction (ouverte ou en cours)."""
        with self.observer.connect() as conn:
            return conn.execute(sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = :a AND pid <> pg_backend_pid() "
                "AND state IN ('active', 'idle in transaction', 'idle in transaction (aborted)')"),
                {"a": APP_NAME}).scalar_one()

    def full_state(self):
        state = self.full_snapshot()
        state["users"] = self.rows("SELECT id, level, level_revision, xmin::text AS xmin FROM users ORDER BY id")
        return state
