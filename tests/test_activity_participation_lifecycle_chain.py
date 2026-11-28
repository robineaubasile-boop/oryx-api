"""V0-T08 Lot 2C PR 3 — chaîne RÉELLE : admission J0 (vrai service) ->
visite et interactions (2B-1) -> opération F2 effective (vrai runner, vraie
frontière du bloc B) -> certification B -> finalisation atomique C (reprise
par la frontière de la PR 1) -> finalisation de la visite par inactivité
(PR 3, parcours A) -> purge nominale de la preuve (PR 1, 168 h) -> purge de
l'événement et de l'attribution (PR 2, 90 jours depuis occurred_at) ->
échéance de la participation (90 jours depuis la fin effective) -> purge
atomique (PR 3, parcours B) -> refus de recréation par les chemins
conformes. Puis la même chaîne avec une preuve résiduelle qui bloque la
purge. Reçus F2, positions, campagne et sa ligne restent intacts.
PostgreSQL réel (16 et 18 en CI).
"""
import uuid
from datetime import datetime, timedelta, timezone

from core import _activity_event_lifecycle as eb
from core import _activity_participation_lifecycle as pb
from core import _activity_proof_lifecycle as lb
from core.authz_f2.runner import AUTHORIZED
from core.models import ProductCampaign
from core.product_analytics import campaigns as pc
from core.product_analytics import common, proofs
from core.product_analytics import participation as pp
from core.product_analytics import participation_lifecycle as pl
from core.product_analytics import sessions as ps
from core.product_analytics.common import ActivitySessionRefused, ParticipationRefused
from tests.activity_proof_support import LAPTOP, NOTICE, REGISTRY, committed, open_wave, rows, touch, utc
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

HOURS_2160 = timedelta(hours=2160)
DAYS_90 = timedelta(days=90)
CAMPAIGNS = "product_campaigns"


def _f2(env):
    return env.receipts(), rows(env.observer, "SELECT * FROM portfolio_positions WHERE user_id = :u", u=USER_A)


def _campaign_side(analytics):
    return {table: rows(analytics, f"SELECT t.*, t.xmin::text AS xmin FROM {table} t ORDER BY t::text")
            for table in (CAMPAIGNS, "product_campaign_cohorts", "product_cohort_seats")}


def _couple(analytics, campaign_id):
    return tuple(rows(analytics, f"SELECT count(*) AS n FROM {table} WHERE campaign_id = :c AND user_id = :u",
                      c=campaign_id, u=USER_A)[0]["n"]
                 for table in ("product_campaign_participants", "product_activity_sessions",
                               "product_activity_session_interactions"))


def _shift_clock(monkeypatch, target):
    """Horloge PostgreSQL des services décalée (jamais fournie par l'appelant)."""
    real = proofs.server_clock
    offset = target - datetime.now(timezone.utc)
    monkeypatch.setattr(proofs, "server_clock", lambda db: real(db) + offset)
    monkeypatch.setattr(proofs, "_chain_real_clock", real, raising=False)


def _restore_clock(monkeypatch):
    monkeypatch.setattr(proofs, "server_clock", proofs._chain_real_clock)


def _certified_through_the_chain(env, analytics, alice):
    campaign = open_wave(analytics, USER_A)  # 1. admission réelle à J0
    visit_id, _ = touch(analytics, campaign, USER_A, environment=LAPTOP)  # 2-3. visite, interaction acceptée
    command = create_command()
    result = run(env, analytics, alice, command, campaign)  # 4-5. opération F2, certification B
    assert result.outcome.status == AUTHORIZED and result.proof.state == proofs.CERTIFIED
    return campaign, visit_id, command


def test_the_full_chain_ends_with_an_atomic_participation_purge_and_no_recreation(env, analytics, alice,
                                                                                 monkeypatch):
    campaign, visit_id, command = _certified_through_the_chain(env, analytics, alice)
    recovered = lb.recover_certified_page(analytics)  # 6. bloc C par la reprise de la PR 1
    assert (recovered.examined, recovered.finalized) == (1, 1)
    proof = proof_of(analytics, command.operation_id)
    [event], [attribution] = events(analytics), attributions(analytics)
    del attribution
    f2_before = _f2(env)
    # 7. visite finalisée par inactivité (instant réel décalé, jamais idle_expires_at).
    visits = rows(analytics, "SELECT * FROM product_activity_sessions WHERE finalized_at IS NULL")
    assert visit_id in {v["id"] for v in visits}  # la visite de J0 et celle de l'origine F2 (environnement du pont)
    _shift_clock(monkeypatch, max(utc(v["idle_expires_at"]) for v in visits) + timedelta(seconds=1))
    finalized = pb.finalize_sessions_page(analytics)
    assert (finalized.examined, finalized.finalized) == (len(visits), len(visits))
    assert rows(analytics, "SELECT count(*) AS n FROM product_activity_sessions WHERE finalized_at IS NULL "
                           "OR end_reason <> 'inactivity'")[0]["n"] == 0
    _restore_clock(monkeypatch)
    # Clôture une heure après l'activité (horloge Python des services historiques).
    real_utcnow = common.utcnow
    monkeypatch.setattr(common, "utcnow", lambda: real_utcnow() + timedelta(hours=1))
    committed(analytics, lambda s: pc.close_campaign(s, campaign_id=campaign))
    monkeypatch.setattr(common, "utcnow", real_utcnow)
    side = _campaign_side(analytics)
    deadline = utc(rows(analytics, f"SELECT closed_at FROM {CAMPAIGNS} WHERE id = :c", c=campaign)[0]["closed_at"]
                   ) + HOURS_2160
    # 8. purge nominale de la preuve (168 h) ; la participation n'est pas échue.
    _shift_clock(monkeypatch, utc(proof["purge_after_at"]) + timedelta(seconds=1))
    assert lb.purge_resolved_page(analytics).purged == 1
    assert pb.purge_participations_page(analytics).examined == 0
    # 9. événement et attribution (90 jours depuis occurred_at, avant la fin de campagne + 90 jours).
    expiry = utc(event["occurred_at"]) + DAYS_90
    assert expiry < deadline
    _restore_clock(monkeypatch)
    _shift_clock(monkeypatch, expiry + timedelta(seconds=1))
    assert eb.expire_events_page(analytics).purged == 1
    assert (events(analytics), attributions(analytics)) == ([], [])
    report = pb.purge_participations_page(analytics)
    assert (report.selected, report.examined) == (True, 0)  # échéance de participation non atteinte
    assert pb.purge_participation(analytics, campaign_id=campaign, user_id=USER_A) == pb.PurgeOutcome(pb.NOT_DUE)
    # 10-11. échéance de la participation : purge atomique de toute la participation.
    _restore_clock(monkeypatch)
    _shift_clock(monkeypatch, deadline)
    before = _couple(analytics, campaign)
    assert before[0] == 1 and before[1] >= 1 and before[2] >= 2  # interaction de la visite + origine F2
    report = pb.purge_participations_page(analytics)
    assert (report.examined, report.purged, report.interactions, report.sessions) == (1, 1, before[2], before[1])
    assert _couple(analytics, campaign) == (0, 0, 0)
    assert _campaign_side(analytics) == side and _f2(env) == f2_before  # campagne, cohorte, places, F2 intactes
    assert rows(analytics, f"SELECT count(*) AS n FROM {REGISTRY}")[0]["n"] == 0
    # 12. aucun chemin conforme ne recrée la participation.
    now = common.utcnow()
    for attempt in (
            lambda s: pp.enroll_at_first_eligible_login(s, campaign_id=campaign, user_id=USER_A,
                                                        authorization_recorded_at=now, notice_version=NOTICE),
            lambda s: pp.enroll_participant(s, campaign_id=campaign, user_id=USER_A, window_start_at=now,
                                            window_end_at=now + timedelta(days=1), authorization_recorded_at=now,
                                            notice_version=NOTICE)):
        try:
            committed(analytics, attempt)
            raise AssertionError("participation recréée")
        except ParticipationRefused:
            pass
    try:
        committed(analytics, lambda s: ps.touch_activity_session(s, campaign_id=campaign, user_id=USER_A,
                                                                 environment_key=LAPTOP, interaction_key=uuid.uuid4()))
        raise AssertionError("visite recréée")
    except ActivitySessionRefused as refused:
        assert refused.reason == "not_participant"
    assert _couple(analytics, campaign) == (0, 0, 0)
    assert pb.purge_participations_page(analytics).examined == 0
    assert lb.recover_certified_page(analytics).examined == 0 and eb.inspect_events_page(analytics).examined == 0


def test_a_residual_proof_blocks_the_participation_purge_at_its_deadline(env, analytics, alice, monkeypatch):
    campaign, visit_id, command = _certified_through_the_chain(env, analytics, alice)
    # Le bloc C n'est jamais exécuté : la preuve certified reste dans le registre.
    committed(analytics, lambda s: pc.close_campaign(
        s, campaign_id=campaign))
    deadline = pl.participation_retention_deadline(committed(
        analytics, lambda s: s.get(ProductCampaign, campaign)))
    f2_before, before = _f2(env), _couple(analytics, campaign)
    _shift_clock(monkeypatch, deadline + timedelta(days=1))
    assert lb.purge_resolved_page(analytics).examined == 0  # une preuve certified n'est pas purgeable
    report = pb.purge_participations_page(analytics)
    assert (report.examined, report.blocked, report.purged) == (1, 1, 0)
    assert pb.purge_participation(analytics, campaign_id=campaign, user_id=USER_A) == pb.PurgeOutcome(
        pb.BLOCKED, pl.PROOF_REMAINING)
    assert pb.finalize_session(analytics, session_id=visit_id) == pb.SessionOutcome(
        pb.NOT_ADMISSIBLE, pl.RETENTION_EXPIRED)  # aucune mutation après l'échéance
    assert proof_of(analytics, command.operation_id)["status"] == proofs.CERTIFIED  # constatée, jamais résolue
    assert _couple(analytics, campaign) == before and _f2(env) == f2_before
