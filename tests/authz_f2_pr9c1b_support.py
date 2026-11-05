"""Outils des tests R1 PR 9-C1-B : finalisation Décrypter F2 (dormante)
contre un VRAI PostgreSQL, sous le VRAI runner et le registre de PRODUCTION
(vingt commandes depuis R1 PR 9-C2 ; aucun registre de test).

FinalizationEnv étend PreparationEnv (R1 PR 9-B2) : réservation B1 réelle,
candidat produit par le VRAI orchestrateur B2 (transports simulés, aucun
réseau), puis FinalizeDecryptageCandidate exécutée par le VRAI runner.
Les états committés sont toujours observés par une connexion indépendante.
"""
import uuid

import pytest
import sqlalchemy as sa

from core.authz_f2 import commands as c
from core.authz_f2 import decryptage_generation as dg
from tests.authz_f2_pr9b1_support import CONV, reserve_command
from tests.authz_f2_pr9b2_support import FakeClaude, FakeClock, PreparationEnv, reply
from tests.authz_f2_support import USER_A
from tests.f2_support import prepare_schema

REPLY_MOAT = "Passons au **moat** : qu'est-ce qui protège LVMH ?\n<!--ORYX_STEP:moat-->"
REPLY_BUSINESS = "Commençons par le **business**. Qu'est-ce que LVMH vend, et à qui ?\n<!--ORYX_STEP:business-->"


class FinalizationEnv(PreparationEnv):
    """PreparationEnv + génération d'un candidat réel et finalisation."""

    def generate(self, issued, command=None, *, text=REPLY_BUSINESS, before_claude=None, orchestrator=None):
        """Réservation réelle puis génération réelle : (commande, bail, candidat)."""
        command = command or reserve_command()
        command, reserved = self.reserve(issued, command)
        clock = FakeClock()
        claude = FakeClaude(clock, text=text, actions={"create_message": before_claude} if before_claude else None)
        outcome = (orchestrator or self.orchestrator(clock, claude=claude)).generate(issued.credential(), command,
                                                                                     reserved)
        assert outcome.status == dg.GENERATED, outcome
        return command, reserved.value.lease, outcome.candidate

    def finalize(self, issued, candidate, **kwargs):
        return self.run(issued, c.FinalizeDecryptageCandidate(candidate), **kwargs)

    def delivery_rows(self, user_id=USER_A):
        return self.rows("SELECT id, conversation_key, source_user_turn_id, status, request_fingerprint, "
                         "response_payload::text AS payload, analysis_session_id, xmin::text AS xmin "
                         "FROM assistant_deliveries WHERE user_id = :u ORDER BY generated_at, id", u=user_id)

    def decryptage_state(self):
        """Toutes les tables touchées par une finalisation (xmin compris)."""
        state = self.full_snapshot()
        return state

    def counts(self):
        out = {}
        with self.observer.connect() as conn:
            for table in ("assistant_deliveries", "decryptage_cognitive_links", "cognitive_events", "support_traces",
                          "analysis_sessions", "user_statements", "investment_theses", "analysis_facts",
                          "decryptage_turn_reservations", "conversation_identities"):
                out[table] = conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        return out


def next_turn(conversation_key=CONV, *, question="", context="", last_method_id=None, ticker="MC.PA"):
    return reserve_command(uuid.uuid4(), conversation_key, ticker, question, context, last_method_id)


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


__all__ = ["CONV", "FakeClaude", "FakeClock", "FinalizationEnv", "REPLY_BUSINESS", "REPLY_MOAT", "next_turn",
           "reply", "schema"]
