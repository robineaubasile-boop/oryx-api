"""V0-T08 Lot 2C PR 2 — chaîne RÉELLE : opération F2 effective (vrai runner,
vraie frontière du bloc B) -> preuve certified -> bloc C (reprise par la
frontière de la PR 1) -> recorded -> purge nominale de la preuve (168 h) ->
purge de l'événement et de son attribution (90 jours) ; puis la même chaîne
avec une échéance d'événement raccourcie pendant que la preuve recorded est
encore conservée. Reçu F2, position, visites et interactions restent
intacts. PostgreSQL réel (16 et 18 en CI).
"""
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from core import _activity_event_lifecycle as eb
from core import _activity_proof_lifecycle as lb
from core.authz_f2.runner import AUTHORIZED
from core.models import ProductActivityEvent
from core.product_analytics import proofs, retention
from tests.activity_proof_support import ATTRIBUTIONS, EVENTS, REGISTRY, open_wave, rows, utc
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


def _untouched(analytics):
    return {table: rows(analytics, f"SELECT t.*, t.xmin::text AS xmin FROM {table} t ORDER BY t::text")
            for table in ("product_activity_session_interactions", "product_activity_sessions",
                          "product_campaign_participants", "product_campaigns")}


def _f2(env):
    return env.receipts(), rows(env.observer, "SELECT * FROM portfolio_positions WHERE user_id = :u", u=USER_A)


def _recorded_through_the_chain(env, analytics, alice):
    campaign = open_wave(analytics, USER_A)
    command = create_command()
    result = run(env, analytics, alice, command, campaign)
    assert result.outcome.status == AUTHORIZED and result.proof.state == proofs.CERTIFIED
    recovered = lb.recover_certified_page(analytics)
    assert (recovered.examined, recovered.finalized) == (1, 1)
    proof = proof_of(analytics, command.operation_id)
    [event], [attribution] = events(analytics), attributions(analytics)
    assert (proof["status"], proof["recorded_event_id"], event["idempotency_key"]) == ("recorded", event["id"],
                                                                                       proof["id"])
    return command, proof, event, attribution


def _shift_clock(monkeypatch, target):
    """Horloge PostgreSQL du service décalée (jamais fournie par l'appelant)."""
    real = proofs.server_clock
    offset = target - datetime.now(timezone.utc)
    monkeypatch.setattr(proofs, "server_clock", lambda db: real(db) + offset)


def test_f2_to_b_to_c_then_the_nominal_proof_purge_then_the_event_and_attribution_purge(env, analytics, alice,
                                                                                       monkeypatch):
    command, proof, event, attribution = _recorded_through_the_chain(env, analytics, alice)
    f2_before, untouched = _f2(env), _untouched(analytics)
    integrity = eb.inspect_events_page(analytics)
    assert (integrity.examined, integrity.valid) == (1, 1)
    # Avant l'échéance (horloge PostgreSQL réelle) : rien n'est sélectionné.
    early = eb.expire_events_page(analytics)
    assert (early.selected, early.examined) == (True, 0)
    # 168 heures : purge nominale de la preuve (PR 1, inchangée) ; événement et attribution intacts.
    _shift_clock(monkeypatch, utc(proof["purge_after_at"]) + timedelta(seconds=1))
    assert lb.purge_resolved_page(analytics).purged == 1 and proof_of(analytics, command.operation_id) is None
    assert events(analytics) == [event] and attributions(analytics) == [attribution]
    assert eb.expire_events_page(analytics).examined == 0  # 90 jours non échus
    # 90 jours depuis occurred_at : l'événement et son attribution, ensemble.
    expiry = utc(event["occurred_at"]) + DAYS_90
    assert utc(event["next_scope_expires_at"]) == utc(event["purge_after_at"]) == expiry
    _shift_clock(monkeypatch, expiry + timedelta(seconds=1))
    report = eb.expire_events_page(analytics)
    assert (report.selected, report.examined, report.purged, report.next_cursor) == (True, 1, 1, None)
    assert (events(analytics), attributions(analytics), rows(analytics, f"SELECT id FROM {REGISTRY}")) == ([], [], [])
    assert _untouched(analytics) == untouched and _f2(env) == f2_before  # ni visite, ni interaction, ni F2
    # Réexécutions explicites : rien n'est recréé.
    assert eb.expire_event(analytics, event_id=event["id"]) == eb.EventOutcome(eb.ABSENT)
    assert lb.recover_certified_page(analytics).examined == 0
    assert eb.inspect_events_page(analytics).examined == 0


def test_a_shortened_event_deadline_removes_the_still_recorded_proof_with_the_attribution(env, analytics, alice,
                                                                                        monkeypatch):
    command, proof, event, attribution = _recorded_through_the_chain(env, analytics, alice)
    f2_before, untouched = _f2(env), _untouched(analytics)
    occurred = utc(event["occurred_at"])
    # Échéance raccourcie (simulation, hors service, d'une future révision de finalité).
    [scope] = retention.parse_retention_scopes(event["retention_scopes"])
    short = retention.RetentionScope(scope.kind, scope.campaign_id, scope.policy_version,
                                     occurred + timedelta(hours=2))
    with analytics.begin() as conn:
        conn.execute(sa.update(ProductActivityEvent.__table__)
                     .where(ProductActivityEvent.__table__.c.id == event["id"])
                     .values(retention_scopes=retention.build_retention_document([short]),
                             next_scope_expires_at=short.expires_at, purge_after_at=short.expires_at))
    _shift_clock(monkeypatch, short.expires_at + timedelta(seconds=1))
    assert datetime.now(timezone.utc) < utc(proof["purge_after_at"])  # la preuve est dans ses 168 heures
    assert lb.purge_resolved_page(analytics).examined == 0  # la PR 1 ne la supprime pas avant 168 h
    report = eb.expire_events_page(analytics)
    assert (report.examined, report.purged) == (1, 1)
    assert proof_of(analytics, command.operation_id) is None  # supprimée AVEC l'attribution qu'elle référence
    assert (events(analytics), attributions(analytics)) == ([], [])
    assert _untouched(analytics) == untouched and _f2(env) == f2_before
    assert rows(analytics, f"SELECT count(*) AS n FROM {EVENTS}")[0]["n"] == 0
    assert rows(analytics, f"SELECT count(*) AS n FROM {ATTRIBUTIONS}")[0]["n"] == 0
    del attribution
