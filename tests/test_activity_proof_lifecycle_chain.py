"""V0-T08 Lot 2C PR 1 — chaîne RÉELLE : opération F2 effective (vrai runner,
vraie frontière du bloc B) -> preuve certified -> reprise par la frontière
du cycle de vie (bloc C inchangé) -> recorded -> 168 heures -> suppression
du registre ; événement, attribution, reçu F2, position, visite et
interactions restent intacts. PostgreSQL réel (16 et 18 en CI).
"""
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from core import _activity_proof_lifecycle as lb
from core.authz_f2.runner import AUTHORIZED
from core.product_analytics import finalization as fz
from core.product_analytics import proofs
from tests.activity_proof_support import REGISTRY, open_wave, rows, utc
from tests.authz_f2_support import USER_A, create_command
from tests.test_activity_event_finalization_chain import (  # noqa: F401 — fixtures schema, analytics, env, alice
    alice,
    analytics,
    attributions,
    env,
    events,
    proof_of,
    run,
    schema,
)
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

HOURS_168 = timedelta(hours=168)
DAYS_90 = timedelta(days=90)


def _analytics_world(analytics):
    return {table: rows(analytics, f"SELECT t.*, t.xmin::text AS xmin FROM {table} t ORDER BY t::text")
            for table in ("product_activity_events", "product_activity_event_attributions",
                          "product_activity_session_interactions", "product_activity_sessions",
                          "product_campaign_participants", "product_campaigns")}


def test_an_effective_f2_operation_is_recorded_then_its_proof_is_purged_after_168_hours(env, analytics, alice,
                                                                                        monkeypatch):
    campaign = open_wave(analytics, USER_A)
    command = create_command()
    result = run(env, analytics, alice, command, campaign)
    assert result.outcome.status == AUTHORIZED and result.proof.state == proofs.CERTIFIED
    certified = proof_of(analytics, command.operation_id)
    # Reprise de la preuve PERSISTÉE par la frontière du cycle de vie : bloc C seul, aucun rejeu F2.
    f2_before = (env.receipts(), rows(env.observer, "SELECT * FROM portfolio_positions WHERE user_id = :u", u=USER_A))
    interactions = rows(analytics, "SELECT * FROM product_activity_session_interactions ORDER BY recorded_at")
    recovered = lb.recover_certified_page(analytics)
    assert (recovered.selected, recovered.examined, recovered.finalized, recovered.next_cursor) == (True, 1, 1, None)
    recorded = proof_of(analytics, command.operation_id)
    [event], [attribution] = events(analytics), attributions(analytics)
    assert (recorded["status"], recorded["recorded_event_id"], recorded["certified_at"]) == (
        "recorded", event["id"], certified["certified_at"])
    assert (event["source_operation_kind"], event["source_operation_id"], event["user_id"], event["idempotency_key"]) \
        == (certified["source_operation_kind"], command.operation_id, USER_A, certified["id"])
    assert {k: attribution[k] for k in fz.IDENTITY_FIELDS} == {k: recorded[k] for k in fz.IDENTITY_FIELDS}
    assert attribution["certified_at"] == recorded["certified_at"] == event["resolved_at"]
    assert rows(analytics, "SELECT * FROM product_activity_session_interactions ORDER BY recorded_at") == interactions
    world = _analytics_world(analytics)
    due = utc(recorded["purge_after_at"])
    assert due - utc(recorded["resolved_at"]) == HOURS_168
    # Avant l'échéance (horloge PostgreSQL réelle) : rien n'est sélectionné ni supprimé.
    early = lb.purge_resolved_page(analytics)
    assert (early.selected, early.examined) == (True, 0) and proof_of(analytics, command.operation_id) == recorded
    # 168 heures plus tard (horloge PostgreSQL du service décalée en test ; jamais fournie par l'appelant).
    real = proofs.server_clock
    offset = due + timedelta(seconds=1) - datetime.now(timezone.utc)  # bornes exactes : test_activity_proof_lifecycle
    monkeypatch.setattr(proofs, "server_clock", lambda db: real(db) + offset)
    purged = lb.purge_resolved_page(analytics)
    assert (purged.examined, purged.purged, purged.absent, purged.unknown) == (1, 1, 0, 0)
    assert proof_of(analytics, command.operation_id) is None
    # Données définitives intactes (xmin compris), exploitables jusqu'à leurs 90 jours ; F2 inchangé.
    assert _analytics_world(analytics) == world
    assert utc(event["purge_after_at"]) == utc(event["occurred_at"]) + DAYS_90 > due
    assert (env.receipts(), rows(env.observer, "SELECT * FROM portfolio_positions WHERE user_id = :u",
                                 u=USER_A)) == f2_before
    # Réexécutions explicites : rien n'est recréé, ni preuve, ni événement, ni interaction.
    assert lb.purge_proof(analytics, proof_id=certified["id"]) == lb.LifecycleOutcome(lb.ABSENT, fz.ABSENT, None)
    assert lb.recover_certified_proof(analytics, proof_id=certified["id"]).outcome == lb.ABSENT
    assert lb.recover_certified_page(analytics).examined == 0
    assert rows(analytics, f"SELECT id FROM {REGISTRY}") == [] and _analytics_world(analytics) == world
    with analytics.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM product_activity_events e JOIN "
                                    "product_activity_event_attributions a ON a.event_id = e.id")).scalar_one() == 1
