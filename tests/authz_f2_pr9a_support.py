"""Banc d'essai ISOLÉ de R1 PR 9-A (V2.2-04) : commandes de test de types
exacts, registre fermé de test, observation des écritures et parcours
historiques réels.

Le registre de test n'existe que dans ce module de tests : il réunit les
seize commandes de production (R1 PR 9-B1) et deux commandes de banc
(BenchTurn, BenchAck) dont l'adaptateur d'exécution appelle le VRAI pont
(core.authz_f2._decryptage_orm_bridge._execute) sous le VRAI runner, plan
DECRYPTAGE_MUTATION. La fixture `bench` le substitue à
core.authz_f2._adapters.REGISTRY le temps d'un test (monkeypatch), puis
vérifie que l'objet de production d'origine (seize commandes) est
restauré. Aucun paramètre du runner, aucun drapeau ni variable
d'environnement n'active ces commandes ; hors de cette fixture, le runner
les refuse (UnknownCommand).

Les parcours historiques (api.decryptage, api.ack_assistant_delivery)
sont appelés tels quels, avec des doubles des seuls appels externes
(ticker, données financières, Claude) et une Session par appel issue d'un
moteur dédié (application_name oryx-legacy-test).
"""
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from types import MappingProxyType

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import api
import core.db
from core.authz_f2 import _adapters
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import commands as c
from tests.authz_f2_support import BOB as BOB_EMAIL
from tests.authz_f2_support import USER_A, USER_B, AuthzEnv
from tests.f2_support import APP_NAME, scalar

LEGACY_APP = "oryx-legacy-test"
TICKER = "MC.PA"
DATA = {"name": "LVMH", "current_price": 600.0, "currency": "EUR", "operating_margin": 0.26, "roe": 0.24,
        "net_cash": -1.0e9}
DISCLAIMER = "Analyse éducative uniquement. Ne constitue pas un conseil en investissement."
STEPS = ("business", "moat", "chiffres", "valorisation", "risques", "swot_final")
PRODUCTION_REGISTRY = _adapters.REGISTRY


@dataclass(frozen=True, slots=True)
class BenchTurn(c.Command):
    """Commande de TEST : finalisation d'un tour Décrypter par le pont."""
    operation: bridge.TurnFinalization


@dataclass(frozen=True, slots=True)
class BenchAck(c.Command):
    """Commande de TEST : ACK d'une livraison par le pont."""
    operation: bridge.DeliveryAcknowledgement


def _bench_execute(conn, ctx, command, locked):
    return bridge._execute(conn, ctx, command.operation)


BENCH_REGISTRY = MappingProxyType({
    **PRODUCTION_REGISTRY,
    BenchTurn: _adapters.Spec(c.DECRYPTAGE_MUTATION, _adapters._no_business_lock, _bench_execute),
    BenchAck: _adapters.Spec(c.DECRYPTAGE_MUTATION, _adapters._no_business_lock, _bench_execute),
})


@pytest.fixture
def bench(monkeypatch):
    """Registre fermé de TEST pendant un test, puis registre de production
    d'origine (même objet, seize commandes depuis R1 PR 9-B1)."""
    assert _adapters.REGISTRY is PRODUCTION_REGISTRY and len(PRODUCTION_REGISTRY) == 16
    monkeypatch.setattr(_adapters, "REGISTRY", BENCH_REGISTRY)
    yield BENCH_REGISTRY
    monkeypatch.undo()
    assert _adapters.REGISTRY is PRODUCTION_REGISTRY and len(_adapters.REGISTRY) == 16
    assert BenchTurn not in _adapters.REGISTRY and BenchAck not in _adapters.REGISTRY


def fingerprint(turn_id) -> str:
    return hashlib.sha256(f"bench:{turn_id}".encode()).hexdigest()


def payload(text: str, ticker=TICKER) -> str:
    return json.dumps({"success": True, "ticker": ticker, "name": "LVMH", "method_used": "construction_these",
                       "analysis": text, "price": 600.0, "currency": "EUR", "disclaimer": DISCLAIMER},
                      sort_keys=True, ensure_ascii=False)


def turn(conversation_key, marker, text="", *, ticker=TICKER, turn_id=None, method="construction_these",
         reply=None):
    turn_id = turn_id or uuid.uuid4()
    return BenchTurn(bridge.TurnFinalization(
        conversation_key, turn_id, fingerprint(turn_id), ticker, text, method, marker,
        payload(reply or f"Réponse {marker}.", ticker), json.dumps(DATA, sort_keys=True)))


def ack(conversation_key, assistant_turn_id):
    return BenchAck(bridge.DeliveryAcknowledgement(assistant_turn_id, conversation_key))


class Pause:
    """Point d'interruption déterministe : à `point`, signale `reached` puis
    attend `release` (la transaction et ses verrous restent ouverts)."""

    def __init__(self, point):
        self.point = point
        self.reached, self.release = threading.Event(), threading.Event()

    def __call__(self, point):
        if point == self.point:
            self.reached.set()
            assert self.release.wait(30), "pause non relâchée"


class Background(threading.Thread):
    def __init__(self, fn):
        super().__init__(daemon=True)
        self.fn = fn
        self.result = self.error = None
        self.finished = threading.Event()

    def run(self):
        try:
            self.result = self.fn()
        except BaseException as exc:  # noqa: BLE001 — rapporté au test
            self.error = exc
        finally:
            self.finished.set()

    def outcome(self, timeout=30):
        self.join(timeout)
        assert not self.is_alive(), "acteur bloqué"
        if self.error is not None:
            raise self.error
        return self.result


def wait_until(predicate, timeout=8.0, message="condition non atteinte"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


class BenchEnv(AuthzEnv):
    """AuthzEnv (moteur F2 réel, sessions F2 réelles) + moteur historique."""

    def __init__(self, pg_url):
        super().__init__(pg_url)
        self.legacy_engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool,
                                              connect_args={"application_name": LEGACY_APP})
        self.Sessions = sessionmaker(bind=self.legacy_engine, autocommit=False, autoflush=False)

    def dispose(self):
        self.legacy_engine.dispose()
        super().dispose()

    # -- observation ------------------------------------------------------

    def rows(self, sql, **params):
        with self.observer.connect() as conn:
            return [dict(r) for r in conn.execute(sa.text(sql), params).mappings()]

    def deliveries(self, user_id=USER_A):
        return self.rows("SELECT id, status, conversation_key, analysis_session_id, xmin::text AS xmin "
                         "FROM assistant_deliveries WHERE user_id = :u ORDER BY generated_at, id", u=user_id)

    def links(self):
        return self.rows("SELECT l.assistant_delivery_id, l.input_action, l.capture_state, l.response_action, "
                         "l.response_event_id, l.support_trace_id, l.product_progress_action, l.xmin::text AS xmin "
                         "FROM decryptage_cognitive_links l ORDER BY l.created_at")

    def events(self):
        return self.rows("SELECT id, status, analysis_session_id, user_work_snapshot, conversation_key, "
                         "xmin::text AS xmin FROM cognitive_events ORDER BY started_at, id")

    def sessions(self):
        return self.rows("SELECT id, user_id, ticker, status, current_step, completed_at, xmin::text AS xmin "
                         "FROM analysis_sessions ORDER BY started_at, id")

    def snapshot(self):
        """Empreinte complète de l'état Décrypter (xmin compris : inchangé =
        aucune écriture durable)."""
        tables = {"assistant_deliveries": "id", "decryptage_cognitive_links": "assistant_delivery_id",
                  "cognitive_events": "id", "support_traces": "id", "analysis_sessions": "id",
                  "user_statements": "id", "analysis_facts": "id", "investment_theses": "id",
                  "conversation_identities": "conversation_key"}
        return {t: self.rows(f"SELECT *, xmin::text AS xmin FROM {t} ORDER BY {k}") for t, k in tables.items()}

    def deadlocks(self):
        return scalar(self.observer, "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")

    def waiting(self, application_name=APP_NAME):
        return scalar(self.observer, "SELECT count(*) FROM pg_stat_activity WHERE application_name = :a "
                                     "AND wait_event_type = 'Lock'", a=application_name)


class Externals:
    """Doubles des seuls appels externes de la route historique."""

    def __init__(self):
        self.texts = []

    def resolve(self, raw):
        return raw.strip().upper()

    def fetch(self, ticker):
        return {"success": True, "data": dict(DATA)}

    def __call__(self, *args, **kwargs):
        return self

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        text = self.texts.pop(0) if self.texts else "Réponse."
        block = type("Block", (), {"type": "text", "text": text})()
        return type("Resp", (), {"content": [block], "stop_reason": "end_turn"})()


@pytest.fixture
def legacy(benv, monkeypatch):
    fake = Externals()
    monkeypatch.setattr(core.db, "SessionLocal", benv.Sessions)
    monkeypatch.setattr(api.anthropic, "Anthropic", fake)
    monkeypatch.setattr(api, "fetch_financial_data", fake.fetch)
    monkeypatch.setattr(api, "normalize_ticker", fake.resolve)
    return fake


def legacy_turn(benv, fake, conversation_key, marker, question="", *, user_id=USER_A, ticker=TICKER):
    """Un tour /decryptage historique réel (marqueur privé dans la réponse)."""
    fake.texts = [f"Réponse historique {marker}.\n<!--ORYX_STEP:{marker}-->"]
    with benv.Sessions() as session:
        return api.decryptage(api.DecryptageRequest(
            ticker=ticker, question=question, context="h" if question else "",
            last_method_id="construction_these" if question else None, user_id=user_id,
            conversation_key=conversation_key, client_turn_id=str(uuid.uuid4())), db=session)


def legacy_ack(benv, conversation_key, assistant_turn_id, *, user_id=USER_A):
    with benv.Sessions() as session:
        return api.ack_assistant_delivery(uuid.UUID(str(assistant_turn_id)), api.AssistantDeliveryAckRequest(
            user_id=user_id, conversation_key=conversation_key), db=session)


__all__ = ["BOB_EMAIL", "USER_A", "USER_B"]
