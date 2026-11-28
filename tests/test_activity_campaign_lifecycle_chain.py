"""V0-T08 Lot 2C PR 4 — chaînes RÉELLES du cycle de vie des campagnes,
PostgreSQL réel (16 et 18 en CI).

1. Chaîne nominale : campagne -> cohorte -> places sélectionnées par de
   véritables invitations F2 (F2AuthService.create_invitation) -> activation
   F2 réelle des invitations (begin / complete_invitation_activation) ->
   rattachement et autorisation des places -> activation de la campagne ->
   admission J0 -> visite et interaction -> opération Portfolio F2 effective
   (vrai runner, vraie frontière du bloc B) -> certification B ->
   finalisation C (reprise PR 1) -> terminaison PR 4 -> purge nominale de la
   preuve PR 1 -> purge de l'événement et de l'attribution PR 2 -> purge des
   participations PR 3 -> purge des places, de la cohorte et de la campagne
   PR 4 -> refus des anciens rejeux. Utilisateurs, invitations, opérations
   d'authentification, sessions F2, reçus et positions intacts.
2. Chaîne dégradée : preuves pending jamais résolue, certified jamais
   finalisée, rejected et indeterminate résolues extrêmement tard, recorded
   cohérente encore présente ; blocage PR 3, purge exceptionnelle PR 4,
   PR 2, PR 3, purge définitive PR 4, sans aucune attente supplémentaire de
   168 heures.
"""
import uuid
from datetime import timedelta

from core import _activity_campaign_lifecycle as cb
from core import _activity_event_lifecycle as eb
from core import _activity_participation_lifecycle as pb
from core import _activity_proof_lifecycle as lb
from core.authz_f2.runner import AUTHORIZED
from core.product_analytics import campaigns as pc
from core.product_analytics import cohort as ch
from core.product_analytics import common, proofs
from core.product_analytics import participation as pp
from core.product_analytics import participation_lifecycle as pl
from core.product_analytics import sessions as ps
from core.product_analytics.common import ActivitySessionRefused, ProductAnalyticsError
from tests.activity_proof_support import (
    LAPTOP,
    NOTICE,
    POLICY,
    REGISTRY,
    certify,
    committed,
    open_origin,
    open_wave,
    operation,
    register,
    rows,
    settle,
    touch,
    utc,
)
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
from tests.test_activity_participation_lifecycle_chain import _restore_clock, _shift_clock
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

HOURS_168 = timedelta(hours=168)
DAYS_90 = timedelta(days=90)
CAMPAIGNS, COHORTS, SEATS = "product_campaigns", "product_campaign_cohorts", "product_cohort_seats"
F2_TABLES = ("users", "invitations", "auth_operations", "oryx_sessions", "portfolio_positions",
             "portfolio_write_idempotency")


def _f2_side(analytics):
    return {table: rows(analytics, f"SELECT t.*, t.xmin::text AS xmin FROM {table} t ORDER BY t::text")
            for table in F2_TABLES}


def _count(analytics, table, campaign_id):
    column = "id" if table == CAMPAIGNS else "campaign_id"
    return rows(analytics, f"SELECT count(*) AS n FROM {table} WHERE {column} = :c", c=campaign_id)[0]["n"]


def _campaign(analytics, campaign_id):
    found = rows(analytics, f"SELECT * FROM {CAMPAIGNS} WHERE id = :c", c=campaign_id)
    return found[0] if found else None


def _cohort_through_f2(env, analytics):
    """Campagne à cohorte dont les cinq places sont sélectionnées par de
    véritables invitations F2 ; le premier testeur active son invitation par
    le vrai moteur F2, rattache sa place, l'autorise, puis est admis (J0)."""
    now = common.utcnow()

    def define(s):
        campaign = pc.create_campaign(s, code=f"wave-0-{uuid.uuid4().hex[:10]}", name="Vague 0",
                                      purpose="Mesurer l'usage des surfaces Oryx.",
                                      close_by_at=now + timedelta(days=13), **POLICY)
        ch.define_cohort(s, campaign_id=campaign.id, cohort_policy_version="v0-cohort-v1",
                         admission_closes_at=now + timedelta(hours=6))
        return campaign.id
    campaign_id = committed(analytics, define)
    invitations = [env.service.create_invitation(cohort="wave-0") for _ in range(5)]
    seats = [committed(analytics, lambda s, i=i: ch.add_cohort_seat(s, campaign_id=campaign_id,
                                                                    invitation_id=i.invitation_id)[0].id)
             for i in invitations]
    committed(analytics, lambda s: ch.activate_cohort_campaign(s, campaign_id=campaign_id))
    begin = env.service.begin_invitation_activation(token=invitations[0].token)
    assert begin.status == "candidate_issued", begin
    done = env.service.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate)
    assert done.status == "invitation_activated", done
    issued = done.session
    assert env.service.authenticate_session(issued.credential()) is not None
    tester = env.owner(issued)
    linked = env.link_email(issued, "v0-tester@example.com")  # liaison OTP réelle (ACT-L1-PR1)
    assert linked.status == "email_linked", linked
    committed(analytics, lambda s: ch.bind_seat_to_activated_invitation(s, campaign_id=campaign_id, seat_id=seats[0]))
    committed(analytics, lambda s: ch.record_seat_authorization(s, campaign_id=campaign_id, user_id=tester,
                                                                notice_version=NOTICE))
    admission = committed(analytics, lambda s: ch.admit_at_first_eligible_login(s, campaign_id=campaign_id,
                                                                                user_id=tester))
    assert admission.created
    return campaign_id, issued, tester, invitations, seats


def test_the_nominal_chain_ends_with_the_atomic_purge_of_seats_cohort_and_campaign(env, analytics, monkeypatch):
    campaign_id, issued, tester, invitations, seats = _cohort_through_f2(env, analytics)
    visit_id, interaction_key = touch(analytics, campaign_id, tester, environment=LAPTOP)  # visite, interaction
    command = create_command()
    result = run(env, analytics, issued, command, campaign_id)  # opération F2 réelle, certification B
    assert result.outcome.status == AUTHORIZED and result.proof.state == proofs.CERTIFIED
    recovered = lb.recover_certified_page(analytics)  # bloc C (reprise PR 1)
    assert (recovered.examined, recovered.finalized) == (1, 1)
    proof = proof_of(analytics, command.operation_id)
    [event] = events(analytics, tester)
    f2_before = _f2_side(analytics)
    close_by = utc(_campaign(analytics, campaign_id)["close_by_at"])
    # 11. Terminaison PR 4 (échéance atteinte, exécution tardive : fin effective = close_by_at).
    _shift_clock(monkeypatch, close_by + timedelta(hours=2))
    report = cb.terminate_campaigns_page(analytics)
    assert (report.examined, report.closed) == (1, 1)
    assert utc(_campaign(analytics, campaign_id)["closed_at"]) == close_by
    deadline = close_by + DAYS_90
    _restore_clock(monkeypatch)
    # 12. Purge nominale de la preuve (168 h depuis la finalisation).
    _shift_clock(monkeypatch, utc(proof["purge_after_at"]) + timedelta(seconds=1))
    assert lb.purge_resolved_page(analytics).purged == 1
    assert cb.purge_campaign_proofs_page(analytics).examined == 0  # campagne non échue : aucun candidat
    assert cb.purge_campaigns_page(analytics).examined == 0
    _restore_clock(monkeypatch)
    # 13. Événement et attribution (90 jours depuis occurred_at, avant l'échéance de campagne).
    expiry = utc(event["occurred_at"]) + DAYS_90
    assert expiry < deadline
    _shift_clock(monkeypatch, expiry + timedelta(seconds=1))
    assert eb.expire_events_page(analytics).purged == 1
    assert (events(analytics, tester), attributions(analytics)) == ([], [])
    _restore_clock(monkeypatch)
    # 14-15. Échéance commune : PR 3 puis purge définitive PR 4.
    _shift_clock(monkeypatch, deadline)
    report = cb.purge_campaigns_page(analytics)
    assert (report.examined, report.blocked) == (1, 1)  # participations encore présentes : constatées
    assert pb.purge_participations_page(analytics).purged == 1
    report = cb.purge_campaigns_page(analytics)
    assert (report.examined, report.purged, report.seats, report.cohorts) == (1, 1, 5, 1)
    assert [_count(analytics, t, campaign_id) for t in (CAMPAIGNS, COHORTS, SEATS)] == [0, 0, 0]
    assert _f2_side(analytics) == f2_before  # utilisateurs, invitations, opérations, sessions F2, reçus, positions
    assert rows(analytics, f"SELECT count(*) AS n FROM {REGISTRY}")[0]["n"] == 0
    # 16. Anciens rejeux refusés, sans aucune écriture.
    now = common.utcnow()
    replays = (
        lambda s: ch.admit_at_first_eligible_login(s, campaign_id=campaign_id, user_id=tester),
        lambda s: ch.bind_seat_to_activated_invitation(s, campaign_id=campaign_id, seat_id=seats[0]),
        lambda s: ch.add_cohort_seat(s, campaign_id=campaign_id, invitation_id=invitations[1].invitation_id),
        lambda s: pc.activate_campaign(s, campaign_id=campaign_id),
        lambda s: pp.enroll_participant(s, campaign_id=campaign_id, user_id=tester, window_start_at=now,
                                        window_end_at=now + timedelta(days=1), authorization_recorded_at=now,
                                        notice_version=NOTICE),
        lambda s: proofs.open_origin(s, campaign_id=campaign_id, environment_key=LAPTOP,
                                     operation=operation(tester)))
    for replay in replays:
        try:
            committed(analytics, replay)
            raise AssertionError("rejeu accepté")
        except ProductAnalyticsError:
            pass
    try:
        committed(analytics, lambda s: ps.touch_activity_session(s, campaign_id=campaign_id, user_id=tester,
                                                                 environment_key=LAPTOP,
                                                                 interaction_key=interaction_key))
        raise AssertionError("visite recréée")
    except ActivitySessionRefused as refused:
        assert refused.reason == "not_participant"
    assert _campaign(analytics, campaign_id) is None and _f2_side(analytics) == f2_before
    assert cb.purge_campaigns_page(analytics).examined == 0 and cb.terminate_campaigns_page(analytics).examined == 0
    del visit_id


def test_the_degraded_chain_purges_extremely_late_proofs_at_the_campaign_deadline_without_waiting(
        env, analytics, alice, monkeypatch):
    del env, alice  # base propre ; profil USER_A créé par le moteur F2
    campaign_id = open_wave(analytics, USER_A, close_days=30)
    states = {}

    def proof_of_kind():
        op = operation(USER_A)
        origin = open_origin(analytics, op, campaign_id=campaign_id)
        return origin, op, register(analytics, origin, op).proof_id
    states["pending"] = proof_of_kind()
    origin, op, proof_id = proof_of_kind()
    assert certify(analytics, origin, op, proof_id).state == proofs.CERTIFIED
    states["certified"] = (origin, op, proof_id)
    states["late_rejected"] = proof_of_kind()
    states["late_indeterminate"] = proof_of_kind()
    origin, op, proof_id = proof_of_kind()
    assert certify(analytics, origin, op, proof_id).state == proofs.CERTIFIED
    assert lb.recover_certified_proof(analytics, proof_id=proof_id).outcome == lb.FINALIZED
    states["recorded"] = (origin, op, proof_id)
    real_utcnow = common.utcnow
    monkeypatch.setattr(common, "utcnow", lambda: real_utcnow() + timedelta(days=3))
    committed(analytics, lambda s: pc.close_campaign(s, campaign_id=campaign_id))  # clôture à J+3
    monkeypatch.setattr(common, "utcnow", real_utcnow)
    deadline = pl.participation_retention_deadline(
        committed(analytics, lambda s: s.get(pc.ProductCampaign, campaign_id)))
    # Résolutions EXTRÊMEMENT tardives (la veille de l'échéance) : leurs 168 h dépassent l'échéance parente.
    _shift_clock(monkeypatch, deadline - timedelta(days=1))
    monkeypatch.setattr(common, "utcnow", lambda: deadline - timedelta(days=1))
    assert settle(analytics, *states["late_indeterminate"]).state == proofs.INDETERMINATE
    assert certify(analytics, *states["late_rejected"]).state == proofs.REJECTED
    monkeypatch.setattr(common, "utcnow", real_utcnow)
    _restore_clock(monkeypatch)
    _shift_clock(monkeypatch, deadline + timedelta(minutes=1))
    late = [proof_of(analytics, states[k][1].operation_id) for k in ("late_rejected", "late_indeterminate")]
    assert all(utc(p["purge_after_at"]) > deadline + timedelta(days=5) for p in late)  # 168 h non écoulées
    # 1. PR 3 bloquée par les preuves résiduelles.
    assert pb.purge_participation(analytics, campaign_id=campaign_id, user_id=USER_A) == pb.PurgeOutcome(
        pb.BLOCKED, pl.PROOF_REMAINING)
    # La preuve recorded historiquement légitime a dépassé ses 168 h bien avant : purge nominale PR 1 ;
    # les résolutions tardives, elles, ne sont pas échues (168 h non écoulées).
    assert lb.purge_resolved_page(analytics).purged == 1
    assert proof_of(analytics, states["recorded"][1].operation_id) is None
    # 2. Purge exceptionnelle PR 4 : une transaction par preuve, aucune attente supplémentaire.
    report = cb.purge_campaign_proofs_page(analytics)
    assert (report.examined, report.purged, report.resolved_and_purged) == (4, 2, 2)
    assert rows(analytics, f"SELECT count(*) AS n FROM {REGISTRY}")[0]["n"] == 0
    # 3. Événement et attribution de la preuve recorded : PR 2 (déjà échus).
    assert len(events(analytics)) == 1
    assert eb.expire_events_page(analytics).purged == 1
    # 4. PR 3 ; 5. purge définitive PR 4.
    assert pb.purge_participations_page(analytics).purged == 1
    report = cb.purge_campaigns_page(analytics)
    assert (report.examined, report.purged) == (1, 1)
    assert _campaign(analytics, campaign_id) is None
