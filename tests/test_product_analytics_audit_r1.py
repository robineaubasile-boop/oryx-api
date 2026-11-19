"""Tests du correctif d'audit R1 de V0-T08 Lot 2A (PR #248).

1. Politique de retard « aucune fenêtre de grâce » : un événement n'est
   accepté que s'il est ENREGISTRÉ pendant que la campagne collecte
   (statut active ET maintenant < close_by_at) et pendant que la fenêtre de
   participation est ouverte (maintenant < window_end_at). Une campagne
   restée techniquement active après son échéance, ou une fenêtre terminée,
   n'accepte plus aucun événement, même antidaté dans la période autorisée.
2. Horloge fraîche : chaque décision temporelle est prise APRÈS
   l'acquisition de tous les verrous PostgreSQL (verrous de ligne et verrous
   consultatifs d'identité). Le franchissement d'une échéance PENDANT
   l'attente d'un verrou est testé sur PostgreSQL réel pour chaque chemin :
   événement (verrou de campagne, de participation, d'identité), inscription
   (verrou de campagne, d'identité), activation, création de campagne,
   répétition idempotente.
3. Répétition d'un événement dont au moins une finalité est expirée
   (totalement ou partiellement) : EventRetentionExpired, sans aucune
   écriture ni suppression (la clé reste consommée ; la purge relève du lot
   2C).
4. Version de politique d'un type inattendu : InvalidProductAnalyticsInput,
   jamais TypeError.
"""
import threading
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from core.product_analytics import campaigns as pc
from core.product_analytics import common
from core.product_analytics import events as pe
from core.product_analytics import participation as pp
from core.product_analytics.common import (
    CampaignTransitionRefused,
    EventIdempotencyCollision,
    EventNotAuthorized,
    EventRetentionExpired,
    InvalidProductAnalyticsInput,
    ParticipationRefused,
)
from tests.test_cognitive_capture import _backend_pid, _NoDB, _Statements, _wait_until_blocked_by, _writes
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_product_analytics_services import (  # noqa: F401 — fixtures engine, Sessions, clock
    POLICY,
    T0,
    USER,
    Sessions,
    _active,
    _business_kwargs,
    _campaign_kwargs,
    _committed,
    _count,
    _enroll_kwargs,
    _event_kwargs,
    _planned,
    _record,
    _rows,
    _snapshot,
    clock,
    engine,
)


def _run_while_blocked(engine, holder, waiter, action, *, while_blocked, release="rollback"):
    """Comme tests.test_cognitive_capture._run_blocked, avec une action
    exécutée pendant que `waiter` est RÉELLEMENT bloqué par `holder`
    (pg_blocking_pids) : ici, avancer l'horloge des services au-delà d'une
    échéance. Puis holder commite ou annule."""
    holder_pid, waiter_pid = _backend_pid(holder), _backend_pid(waiter)
    outcome = {}

    def work():
        try:
            outcome["result"] = action(waiter)
            waiter.commit()
        except Exception as exc:  # noqa: BLE001 — restitué au test
            outcome["error"] = exc
            waiter.rollback()

    thread = threading.Thread(target=work)
    thread.start()
    try:
        _wait_until_blocked_by(engine, waiter_pid, holder_pid)
        assert outcome == {}
        while_blocked()
        getattr(holder, release)()
    finally:
        if thread.is_alive() and holder.in_transaction():
            holder.rollback()
        thread.join(timeout=10)
    assert not thread.is_alive()
    return outcome.get("result"), outcome.get("error")


def _campaign_with_window(Sessions, clock, *, close_days, window_days, user=USER):
    campaign_id = _active(Sessions, clock, close_by_at=T0 + timedelta(days=close_days))
    _committed(Sessions, lambda s: pp.enroll_participant(
        s, campaign_id=campaign_id, user_id=user, window_start_at=T0, window_end_at=T0 + timedelta(days=window_days),
        authorization_recorded_at=T0, notice_version="v0-notice-1"))
    return campaign_id


def _event_at(occurred, **overrides):
    return _event_kwargs(occurred_at=occurred, resolved_at=occurred, **overrides)


# --------------------------------------------------------------------------
# 1. Politique de retard : aucune fenêtre de grâce
# --------------------------------------------------------------------------

def test_late_policy_is_declared_without_grace_window():
    assert pe.LATE_EVENT_GRACE == timedelta(0)


@pytest.mark.parametrize("after", [timedelta(0), timedelta(microseconds=1), timedelta(days=3)])
def test_pg_campaign_left_active_after_its_deadline_refuses_backdated_events(engine, Sessions, clock, after):
    campaign_id = _campaign_with_window(Sessions, clock, close_days=10, window_days=10)
    clock.now = T0 + timedelta(days=10) + after
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(EventNotAuthorized):
            pe.record_event(session, **_event_at(T0 + timedelta(days=5)))
        session.commit()
    assert _snapshot(engine) == before
    # La campagne est restée techniquement active : seule l'échéance refuse.
    assert _rows(engine, "product_campaigns")[0]["status"] == "active"
    assert _rows(engine, "product_campaigns")[0]["id"] == campaign_id


def test_pg_event_recorded_just_before_the_deadline_is_accepted(engine, Sessions, clock):
    _campaign_with_window(Sessions, clock, close_days=10, window_days=10)
    clock.now = T0 + timedelta(days=10) - timedelta(microseconds=1)
    assert _record(Sessions, **_event_at(T0 + timedelta(days=5)))[1] is True


@pytest.mark.parametrize("after", [timedelta(0), timedelta(seconds=1)])
def test_pg_ended_participation_window_refuses_backdated_events(engine, Sessions, clock, after):
    _campaign_with_window(Sessions, clock, close_days=60, window_days=7)
    clock.now = T0 + timedelta(days=7) + after
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(EventNotAuthorized):
            pe.record_event(session, **_event_at(T0 + timedelta(days=3)))
    assert _snapshot(engine) == before


def test_pg_delayed_event_while_window_and_campaign_are_open_is_accepted(engine, Sessions, clock):
    """Aucune borne de retard propre n'est imposée tant que la fenêtre et la
    campagne sont ouvertes (point ouvert d'arbitrage, voir rapport)."""
    _campaign_with_window(Sessions, clock, close_days=60, window_days=7)
    clock.now = T0 + timedelta(days=7) - timedelta(microseconds=1)
    assert _record(Sessions, **_event_at(T0 + timedelta(hours=1)))[1] is True


def test_pg_window_start_is_inclusive(engine, Sessions, clock):
    _campaign_with_window(Sessions, clock, close_days=60, window_days=7)
    clock.advance(hours=1)
    assert _record(Sessions, **_event_at(T0))[1] is True


# --------------------------------------------------------------------------
# 2. Horloge fraîche après l'attente des verrous
# --------------------------------------------------------------------------

def test_pg_event_waiting_for_a_campaign_lock_decides_with_a_fresh_clock(engine, Sessions, clock):
    campaign_id = _campaign_with_window(Sessions, clock, close_days=10, window_days=10)
    clock.now = T0 + timedelta(days=10) - timedelta(seconds=1)
    with Sessions() as holder, Sessions() as waiter:
        pc.close_campaign(holder, campaign_id=campaign_id)  # FOR UPDATE, puis annulée
        _, error = _run_while_blocked(
            engine, holder, waiter, lambda s: pe.record_event(s, **_event_at(T0 + timedelta(days=5))),
            while_blocked=lambda: clock.advance(seconds=2))
    assert isinstance(error, EventNotAuthorized)
    assert _rows(engine, "product_campaigns")[0]["status"] == "active"
    assert _count(engine, "product_activity_events") == 0


def test_pg_event_waiting_for_a_participation_lock_decides_with_a_fresh_clock(engine, Sessions, clock):
    campaign_id = _campaign_with_window(Sessions, clock, close_days=60, window_days=7)
    clock.now = T0 + timedelta(days=7) - timedelta(seconds=1)
    with Sessions() as holder, Sessions() as waiter:
        pp.withdraw_participant(holder, campaign_id=campaign_id, user_id=USER)  # FOR UPDATE, puis annulé
        _, error = _run_while_blocked(
            engine, holder, waiter, lambda s: pe.record_event(s, **_event_at(T0 + timedelta(days=3))),
            while_blocked=lambda: clock.advance(seconds=2))
    assert isinstance(error, EventNotAuthorized)
    assert _rows(engine, "product_campaign_participants")[0]["participation_status"] == "enrolled"
    assert _count(engine, "product_activity_events") == 0


def _identity_cases(clock):
    occurred = T0 + timedelta(days=5)
    visit = uuid.uuid4()
    return {
        "idempotency_key": (lambda key: _event_at(occurred, idempotency_key=key), True),
        "session_start": (lambda key: _event_at(occurred, idempotency_key=key, event_type="session_started",
                                                surface="app", source="server_visit", visit_id=visit), False),
        "source_operation": (lambda key, op=uuid.uuid4(): _business_kwargs(
            occurred_at=occurred, resolved_at=occurred, delivered_at=occurred, idempotency_key=key,
            source_operation_id=op), False),
    }


@pytest.mark.parametrize("identity", ["idempotency_key", "session_start", "source_operation"])
def test_pg_event_waiting_for_an_identity_lock_decides_with_a_fresh_clock(engine, Sessions, clock, identity):
    """Le perdant d'une course sur une identité (clé d'idempotence,
    session_started de la visite, opération canonique) attend le gagnant ;
    l'échéance est franchie pendant l'attente, le gagnant annule : le perdant
    doit refuser, et non insérer avec la décision prise avant l'attente."""
    _campaign_with_window(Sessions, clock, close_days=10, window_days=10)
    clock.now = T0 + timedelta(days=10) - timedelta(seconds=1)
    build, same_key = _identity_cases(clock)[identity]
    first_key = uuid.uuid4()
    second_key = first_key if same_key else uuid.uuid4()
    with Sessions() as holder, Sessions() as waiter:
        pe.record_event(holder, **build(first_key))
        _, error = _run_while_blocked(engine, holder, waiter, lambda s: pe.record_event(s, **build(second_key)),
                                      while_blocked=lambda: clock.advance(seconds=2))
    assert isinstance(error, EventNotAuthorized)
    assert _count(engine, "product_activity_events") == 0


def test_pg_enrollment_waiting_for_a_campaign_lock_decides_with_a_fresh_clock(engine, Sessions, clock):
    campaign_id = _planned(Sessions, close_by_at=T0 + timedelta(days=10))
    clock.now = T0 + timedelta(days=10) - timedelta(seconds=1)
    now = clock.now
    kwargs = _enroll_kwargs(campaign_id=campaign_id, window_start_at=now,
                            window_end_at=now + timedelta(milliseconds=500),
                            authorization_recorded_at=now)
    with Sessions() as holder, Sessions() as waiter:
        pc.revise_campaign_policies(holder, campaign_id=campaign_id, **POLICY)  # FOR UPDATE, puis annulée
        _, error = _run_while_blocked(engine, holder, waiter, lambda s: pp.enroll_participant(s, **kwargs),
                                      while_blocked=lambda: clock.advance(seconds=2))
    assert isinstance(error, ParticipationRefused)
    assert _count(engine, "product_campaign_participants") == 0


def test_pg_enrollment_waiting_for_an_identity_lock_decides_with_a_fresh_clock(engine, Sessions, clock):
    """Course sur (campagne, utilisateur) : la fenêtre devient rétroactive
    pendant l'attente ; le gagnant annule ; le perdant refuse."""
    campaign_id = _active(Sessions, clock)
    start = clock.now + timedelta(hours=1)
    kwargs = _enroll_kwargs(campaign_id=campaign_id, window_start_at=start, window_end_at=start + timedelta(days=7),
                            authorization_recorded_at=clock.now)
    with Sessions() as holder, Sessions() as waiter:
        pp.enroll_participant(holder, **kwargs)
        _, error = _run_while_blocked(engine, holder, waiter, lambda s: pp.enroll_participant(s, **kwargs),
                                      while_blocked=lambda: clock.advance(hours=2))
    assert isinstance(error, ParticipationRefused)
    assert _count(engine, "product_campaign_participants") == 0


def test_pg_activation_waiting_for_a_campaign_lock_decides_with_a_fresh_clock(engine, Sessions, clock):
    campaign_id = _planned(Sessions, close_by_at=T0 + timedelta(days=10))
    clock.now = T0 + timedelta(days=10) - timedelta(seconds=1)
    with Sessions() as holder, Sessions() as waiter:
        pc.revise_campaign_policies(holder, campaign_id=campaign_id, **POLICY)
        _, error = _run_while_blocked(engine, holder, waiter,
                                      lambda s: pc.activate_campaign(s, campaign_id=campaign_id),
                                      while_blocked=lambda: clock.advance(seconds=2))
    assert isinstance(error, CampaignTransitionRefused)
    assert _rows(engine, "product_campaigns")[0]["status"] == "planned"


def test_pg_campaign_creation_waiting_for_an_identity_lock_decides_with_a_fresh_clock(engine, Sessions, clock):
    kwargs = _campaign_kwargs(code="wave-0", close_by_at=T0 + timedelta(seconds=1))
    with Sessions() as holder, Sessions() as waiter:
        pc.create_campaign(holder, **kwargs)
        _, error = _run_while_blocked(engine, holder, waiter, lambda s: pc.create_campaign(s, **kwargs),
                                      while_blocked=lambda: clock.advance(seconds=2))
    assert isinstance(error, InvalidProductAnalyticsInput)
    assert _count(engine, "product_campaigns") == 0


def test_pg_idempotent_retry_waiting_for_an_identity_lock_decides_with_a_fresh_clock(engine, Sessions, clock):
    _campaign_with_window(Sessions, clock, close_days=60, window_days=14)
    occurred = T0 + timedelta(hours=1)
    clock.now = occurred + timedelta(minutes=1)
    kwargs = _event_at(occurred)
    _record(Sessions, **kwargs)
    clock.now = occurred + timedelta(days=90) - timedelta(seconds=1)
    before = _rows(engine, "product_activity_events")
    with Sessions() as holder, Sessions() as waiter:
        assert pe.record_event(holder, **kwargs)[1] is False  # répétition encore valide, verrou d'identité tenu
        _, error = _run_while_blocked(engine, holder, waiter, lambda s: pe.record_event(s, **kwargs),
                                      while_blocked=lambda: clock.advance(seconds=2), release="commit")
    assert isinstance(error, EventRetentionExpired)
    assert _rows(engine, "product_activity_events") == before


# --------------------------------------------------------------------------
# 3. Répétitions sur finalités expirées
# --------------------------------------------------------------------------

def _recorded(Sessions, clock):
    _campaign_with_window(Sessions, clock, close_days=60, window_days=14)
    occurred = T0 + timedelta(hours=1)
    clock.now = occurred + timedelta(minutes=1)
    kwargs = _event_at(occurred)
    event_id = _record(Sessions, **kwargs)[0]
    return occurred, kwargs, event_id


def test_pg_retry_just_before_expiry_still_returns_the_stored_event(engine, Sessions, clock):
    occurred, kwargs, event_id = _recorded(Sessions, clock)
    clock.now = occurred + timedelta(days=90) - timedelta(microseconds=1)
    assert _record(Sessions, **kwargs) == (event_id, False)


@pytest.mark.parametrize("after", [timedelta(0), timedelta(days=1)])
@pytest.mark.parametrize("same_data", [True, False])
def test_pg_retry_on_fully_expired_retention_is_refused_without_mutation(engine, Sessions, clock, after, same_data):
    occurred, kwargs, _ = _recorded(Sessions, clock)
    clock.now = occurred + timedelta(days=90) + after
    request = kwargs if same_data else {**kwargs, "visit_id": uuid.uuid4()}
    before = _snapshot(engine)
    with Sessions() as session, _Statements(engine) as statements:
        with pytest.raises(EventRetentionExpired) as failure:
            pe.record_event(session, **request)
        assert not isinstance(failure.value, EventIdempotencyCollision)
        session.commit()
    # Aucune écriture, aucune suppression : la clé reste consommée, la ligne
    # attend la purge du lot 2C.
    assert [w for w in _writes(statements) if not w.startswith("SELECT")] == []
    assert _snapshot(engine) == before


def test_pg_retry_on_partially_expired_retention_is_refused_without_mutation(engine, Sessions, clock, monkeypatch):
    monkeypatch.setitem(common.RETENTION_POLICIES, "v0-retention-short-v1", 30)
    long_campaign = _active(Sessions, clock)
    short_campaign = _active(Sessions, clock, retention_days=30, retention_policy_version="v0-retention-short-v1")
    for campaign_id in (long_campaign, short_campaign):
        _committed(Sessions, lambda s, c=campaign_id: pp.enroll_participant(
            s, campaign_id=c, user_id=USER, window_start_at=T0, window_end_at=T0 + timedelta(days=14),
            authorization_recorded_at=T0, notice_version="v0-notice-1"))
    occurred = T0 + timedelta(hours=1)
    clock.now = occurred + timedelta(minutes=1)
    kwargs = _event_at(occurred)
    event_id = _record(Sessions, **kwargs)[0]
    [row] = _rows(engine, "product_activity_events")
    assert (row["next_scope_expires_at"], row["purge_after_at"]) == (occurred + timedelta(days=30),
                                                                     occurred + timedelta(days=90))
    assert len(row["retention_scopes"]["scopes"]) == 2
    clock.now = occurred + timedelta(days=30) - timedelta(microseconds=1)
    assert _record(Sessions, **kwargs) == (event_id, False)
    clock.now = occurred + timedelta(days=30)
    before = _snapshot(engine)
    with Sessions() as session, _Statements(engine) as statements:
        with pytest.raises(EventRetentionExpired):
            pe.record_event(session, **kwargs)
        session.commit()
    assert _writes(statements) == []
    assert _snapshot(engine) == before  # les deux finalités restent stockées : aucune purge en 2A


# --------------------------------------------------------------------------
# 4. Versions de politique d'un type inattendu
# --------------------------------------------------------------------------

UNEXPECTED_VERSIONS = [None, 1, 1.5, True, b"v0-retention-v1", ["v0-retention-v1"], ("v0-retention-v1",),
                       {"v0-retention-v1": 90}, {"v0-retention-v1"}]


@pytest.mark.parametrize("field", ["retention_policy_version", "measurement_policy_version"])
@pytest.mark.parametrize("value", UNEXPECTED_VERSIONS, ids=lambda v: type(v).__name__)
def test_unexpected_policy_version_type_is_invalid_input_not_type_error(field, value):
    for call in (lambda: common.validate_policy(**{**POLICY, field: value}),
                 lambda: pc.create_campaign(_NoDB(), **_campaign_kwargs(**{field: value})),
                 lambda: pc.revise_campaign_policies(_NoDB(), campaign_id=uuid.uuid4(), **{**POLICY, field: value})):
        with pytest.raises(InvalidProductAnalyticsInput) as failure:
            call()
        assert not isinstance(failure.value, TypeError)


# --------------------------------------------------------------------------
# Défenses indépendantes (rendues observables)
# --------------------------------------------------------------------------

def test_pg_campaign_deadline_is_enforced_even_if_a_window_outlives_it(engine, Sessions, clock):
    """Le service impose window_end_at <= close_by_at à l'inscription, ce qui
    rend le contrôle « maintenant < close_by_at » redondant pour ses propres
    lignes. Il reste une défense indépendante : avec une participation écrite
    hors service (la base ne l'interdit pas), une campagne restée active après
    son échéance refuse toujours l'événement antidaté."""
    campaign_id = _active(Sessions, clock, close_by_at=T0 + timedelta(days=10))
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO product_campaign_participants (campaign_id, user_id, window_start_at, window_end_at, "
            "participation_status, enrolled_at, authorization_recorded_at, notice_version) "
            "VALUES (:c, :u, :s, :e, 'enrolled', :s, :s, 'v0-notice-1')"),
            {"c": campaign_id, "u": USER, "s": T0, "e": T0 + timedelta(days=30)})
    clock.now = T0 + timedelta(days=10)
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(EventNotAuthorized):
            pe.record_event(session, **_event_at(T0 + timedelta(days=5)))
    assert _snapshot(engine) == before
    clock.now = T0 + timedelta(days=10) - timedelta(microseconds=1)
    assert _record(Sessions, **_event_at(T0 + timedelta(days=5)))[1] is True


@pytest.mark.parametrize("same_data", [True, False])
def test_pg_unique_index_remains_the_final_defense_against_an_unserialized_writer(engine, Sessions, clock,
                                                                                   monkeypatch, same_data):
    """Écrivain concurrent qui ne prend PAS le verrou d'identité (simulé : le
    verrou n'est pris que dans le fil principal) : le perdant attend sur
    l'index UNIQUE, relit la ligne gagnante et applique la règle
    d'idempotence (événement existant ou collision), sans doublon."""
    real_lock = common.lock_identities
    main = threading.main_thread()
    monkeypatch.setattr(common, "lock_identities",
                        lambda db, *keys: real_lock(db, *keys) if threading.current_thread() is main else None)
    _campaign_with_window(Sessions, clock, close_days=60, window_days=14)
    clock.advance(hours=1)
    kwargs = _event_at(clock.now - timedelta(minutes=1))
    request = kwargs if same_data else {**kwargs, "visit_id": uuid.uuid4()}
    with Sessions() as holder, Sessions() as waiter:
        holder_id = pe.record_event(holder, **kwargs)[0].id

        def retry(session):
            event, created = pe.record_event(session, **request)
            return event.id, created
        result, error = _run_while_blocked(engine, holder, waiter, retry, while_blocked=lambda: None,
                                           release="commit")
    if same_data:
        assert (result, error) == ((holder_id, False), None)
    else:
        assert isinstance(error, EventIdempotencyCollision)
    assert _count(engine, "product_activity_events") == 1


def test_pg_event_backdated_before_the_participant_window_is_refused(engine, Sessions, clock):
    """Campagne active depuis T0, participant inscrit plus tard (fenêtre à
    partir de T0 + 1 j) : un événement antidaté entre l'activation et le début
    de SA fenêtre (donc avant son autorisation) est refusé ; la campagne seule
    ne suffit pas."""
    campaign_id = _active(Sessions, clock)
    clock.now = T0 + timedelta(days=1)
    start = clock.now
    _committed(Sessions, lambda s: pp.enroll_participant(
        s, campaign_id=campaign_id, user_id=USER, window_start_at=start, window_end_at=start + timedelta(days=7),
        authorization_recorded_at=start, notice_version="v0-notice-1"))
    clock.advance(hours=1)
    before = _snapshot(engine)
    for occurred in (T0 + timedelta(hours=12), start - timedelta(microseconds=1)):
        with Sessions() as session:
            with pytest.raises(EventNotAuthorized):
                pe.record_event(session, **_event_at(occurred))
    assert _snapshot(engine) == before
    assert _record(Sessions, **_event_at(start))[1] is True
