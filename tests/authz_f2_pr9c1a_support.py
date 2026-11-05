"""Banc d'essai ISOLÉ de R1 PR 9-C1-A : répétition (« rehearsal ») des
primitives privées du pont ORM sous le VRAI runner, sans aucune commande de
production ni finalisation de réservation.

Le banc PR 9-A (tests/authz_f2_pr9a_support.py) est réutilisé tel quel
(BenchTurn / BenchAck : chemin legacy de capture et de progression). Ce
module ajoute, le temps d'un test seulement (fixture `c1a`) :

- une commande de TEST BenchRehearsal (plan DECRYPTAGE_MUTATION) dans un
  registre fermé de test ;
- une opération de TEST Rehearsal dans l'inventaire du pont
  (bridge.OPERATIONS), dont le point d'entrée _rehearse compose les
  primitives C1-A dans l'ordre figé : SAVEPOINT C1 (_business_savepoint) ->
  claim de la livraison (service historique, comme la phase B legacy) ->
  Prepare (_prepare_turn) -> revalidation cognitive facultative
  (_cognitive_context_matches) -> Apply (_apply_prepared_turn) -> RELEASE.
  Aucune réservation B1 n'est lue ni écrite : ce n'est PAS une
  finalisation, et aucun succès de finalisation n'est fabriqué.

Les points d'observation (SCRIPT) sont des fonctions de TEST appelées aux
étapes nommées ; la production n'en a aucun. La fixture vérifie qu'après le
test le registre de production (dix-huit commandes) et l'inventaire du pont
(trois opérations, R1 PR 9-C1-B) sont exactement les objets d'origine.
"""
import json
import sys
import uuid
from dataclasses import dataclass
from types import MappingProxyType

import pytest

from core import assistant_delivery as ad
from core import decryptage_cognitive_runtime as dcr
from core.authz_f2 import _adapters
from core.authz_f2 import _bridge_contracts as contracts
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import commands as c
from tests.authz_f2_pr9a_support import BENCH_REGISTRY, PRODUCTION_REGISTRY, TICKER, BenchTurn, turn

PRODUCTION_OPERATIONS = bridge.OPERATIONS
PRODUCTION_CAPABILITIES = bridge.CAPABILITIES
STAGES = ("before_savepoint", "claimed", "prepared", "applied", "after_savepoint")


@dataclass(frozen=True, slots=True)
class Expected:
    """Contexte B2 attendu (types fermés de core.authz_f2.decryptage_context)."""
    ticker: str
    anchor: object
    active_session: object


@dataclass(frozen=True, slots=True)
class Rehearsal:
    """Opération de TEST : un tour complet par les primitives C1-A."""
    turn: bridge.TurnFinalization
    expected: object = None  # Expected ou None (aucune revalidation)


@dataclass(frozen=True, slots=True)
class BenchRehearsal(c.Command):
    operation: Rehearsal


@dataclass(frozen=True, slots=True)
class RehearsalOutcome:
    """Vue de TEST (aucune instance ORM)."""
    created: bool
    savepoint: str            # released / rolled_back
    reason: object
    context: object           # bridge.PreparedCognitiveContext ou None
    applied: object           # bridge.PreparedTurnApplied ou None
    assistant_turn_id: object


class Script:
    """Points d'observation de TEST : stage -> fonction(state)."""

    def __init__(self):
        self.actions = {}
        self.seen = []

    def at(self, stage, action):
        assert stage in STAGES
        self.actions[stage] = action

    def __call__(self, stage, state):
        self.seen.append(stage)
        action = self.actions.get(stage)
        if action is not None:
            action(state)


SCRIPT = Script()


class State:
    """État visible des points d'observation (références du test seulement)."""

    def __init__(self, db, user_id, operation):
        self.db, self.user_id, self.operation = db, user_id, operation
        self.scope = self.delivery = self.prepared = self.applied = self.probe = None
        self.skip_apply = False  # Prepare seul (aucune application), puis RELEASE


def _rehearse(db, user_id, operation, probe):
    t = operation.turn
    state = State(db, user_id, operation)
    state.probe = probe
    SCRIPT("before_savepoint", state)
    created = False
    with bridge._business_savepoint(db, probe) as scope:
        state.scope = scope
        payload = json.loads(t.response_payload)
        delivery, created = ad.claim_delivery(
            db, user_id=user_id, conversation_key=t.conversation_key, surface=ad.DECRYPTAGE_SURFACE,
            source_user_turn_id=t.client_turn_id, delivery_ordinal=bridge.DELIVERY_ORDINAL,
            request_fingerprint=t.request_fingerprint,
            visible_content_fingerprint=ad.visible_content_fingerprint(payload["analysis"]),
            response_payload=payload, private_metadata=dcr.runtime_private_metadata(t.step_marker),
            delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION)
        state.delivery = delivery
        turn_id = delivery.id
        SCRIPT("claimed", state)
        if created:  # même règle que le legacy : seul l'appel qui crée capture
            state.prepared = bridge._prepare_turn(db, scope, user_id, delivery, ticker=t.ticker,
                                                  user_text=t.user_text)
            SCRIPT("prepared", state)
            expected = operation.expected
            if expected is not None and not bridge._cognitive_context_matches(
                    state.prepared.context, ticker=expected.ticker, anchor=expected.anchor,
                    active_session=expected.active_session):
                raise bridge._LocalRollback(bridge.CONTEXT_CHANGED)
            if not state.skip_apply:
                state.applied = bridge._apply_prepared_turn(
                    db, scope, user_id, state.prepared, method_id=t.method_id, step_marker=t.step_marker,
                    financial_snapshot=t.financial_snapshot)
                SCRIPT("applied", state)
    SCRIPT("after_savepoint", state)
    context = state.prepared.context if state.prepared is not None else None
    return RehearsalOutcome(created, "released" if scope.released else "rolled_back", scope.reason, context,
                            state.applied if scope.released else None, turn_id)


def _bench_execute(conn, ctx, command, locked):
    # R1 PR 9-C1-B : la sonde de transaction (SQL) appartient aux adaptateurs ;
    # une opération historique du pont ne reçoit qu'elle (jamais la relecture B2).
    probe = _adapters._transaction_probe(conn, ctx)
    try:
        return bridge._execute(conn, ctx, command.operation, probe)
    finally:
        probe._close()


C1A_REGISTRY = MappingProxyType({
    **BENCH_REGISTRY,
    BenchRehearsal: _adapters.Spec(c.DECRYPTAGE_MUTATION, _adapters._no_business_lock, _bench_execute),
})
C1A_OPERATIONS = MappingProxyType({**PRODUCTION_OPERATIONS, Rehearsal: _rehearse})
# R1 PR 9-C1-B : l'opération de test ne reçoit que la sonde (jamais la relecture B2).
C1A_CAPABILITIES = MappingProxyType({**PRODUCTION_CAPABILITIES, Rehearsal: contracts.TransactionProbe})


@pytest.fixture
def c1a(monkeypatch, bench):
    """Registre et inventaire de TEST le temps d'un test, puis objets de
    production d'origine (18 commandes, 3 opérations)."""
    # R1 PR 9-C1-B : trois opérations de production dans l'inventaire du pont.
    assert bridge.OPERATIONS is PRODUCTION_OPERATIONS and len(PRODUCTION_OPERATIONS) == 3
    assert bridge.CAPABILITIES is PRODUCTION_CAPABILITIES
    script = Script()
    monkeypatch.setattr(_adapters, "REGISTRY", C1A_REGISTRY)
    monkeypatch.setattr(bridge, "OPERATIONS", C1A_OPERATIONS)
    monkeypatch.setattr(bridge, "CAPABILITIES", C1A_CAPABILITIES)
    monkeypatch.setattr(sys.modules[__name__], "SCRIPT", script)
    yield script
    monkeypatch.undo()
    assert _adapters.REGISTRY is PRODUCTION_REGISTRY and len(_adapters.REGISTRY) == 18
    assert bridge.OPERATIONS is PRODUCTION_OPERATIONS and len(bridge.OPERATIONS) == 3
    assert bridge.CAPABILITIES is PRODUCTION_CAPABILITIES
    assert BenchRehearsal not in _adapters.REGISTRY and Rehearsal not in bridge.OPERATIONS


def rehearsal(conversation_key, marker, text="", *, ticker=TICKER, turn_id=None, method="construction_these",
              expected=None):
    """Commande de TEST : même tour que tests.authz_f2_pr9a_support.turn,
    exécuté par les primitives C1-A."""
    legacy: BenchTurn = turn(conversation_key, marker, text, ticker=ticker, turn_id=turn_id or uuid.uuid4(),
                             method=method)
    return BenchRehearsal(Rehearsal(legacy.operation, expected))
