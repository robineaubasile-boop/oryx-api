"""V0-T08 Lot 2C PR 4 — cycle de vie des campagnes : terminaison des
campagnes échues (parcours A), purge exceptionnelle des preuves à l'échéance
de leur campagne (parcours B) et purge définitive des places, cohortes et
campagnes (parcours C) (core/product_analytics/campaign_lifecycle.py,
frontière core/_activity_campaign_lifecycle.py), contre un VRAI PostgreSQL
(16 et 18 en CI). Chaînes réelles : tests/test_activity_campaign_lifecycle_chain.py ;
charges jusqu'à 100 000 événements : tests/test_activity_campaign_lifecycle_stress.py.

A. contrats d'entrée ; B. terminaison ; C. conservation ; D-G. purge
exceptionnelle (pending, certified, terminales, recorded) ; H. atomicité de
la preuve ; I-J. dépendances physiques et JSON ; K. cohortes et places ; L.
atomicité de la campagne ; M. concurrence ; N. transactions et COMMIT
incertain ; O. pagination, plans et budget ; P. recréation ; inventaires et
dormance.
"""
import ast
import hashlib
import inspect
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core import _activity_campaign_lifecycle as cb
from core import _activity_event_lifecycle as eb
from core import _activity_participation_lifecycle as pb
from core import _activity_proof_lifecycle as prb
from core import _activity_transactions as tx
from core.product_analytics import campaign_lifecycle as cl
from core.product_analytics import campaigns as pc
from core.product_analytics import cohort as ch
from core.product_analytics import common
from core.product_analytics import event_lifecycle as el
from core.product_analytics import finalization as fz
from core.product_analytics import participation as pp
from core.product_analytics import participation_lifecycle as pl
from core.product_analytics import proof_lifecycle as prl
from core.product_analytics import proofs
from core.product_analytics import retention
from core.product_analytics import sessions as ps
from core.product_analytics.common import InvalidProductAnalyticsInput
from tests.activity_proof_support import (
    ATTRIBUTIONS,
    EVENTS,
    LAPTOP,
    NOTICE,
    POLICY,
    REGISTRY,
    Background,
    Holder,
    certify,
    committed,
    open_origin,
    operation,
    register,
    rows,
    settle,
    touch,
    utc,
    waiting,
)
from tests.test_activity_event_finalization import (  # noqa: F401 — fixtures engine, db, clock
    MICRO,
    USER_A,
    USER_B,
    _certified,
    _wait,
    clock,
    db,
    engine,
    expire,
    finalize,
)
from tests.test_activity_event_lifecycle import _lose_commit, _record, _statements, _xid
from tests.test_activity_participation_lifecycle import R5_FORMS, _fail_on, _plan, _sql
from tests.test_cognitive_capture import _NoDB
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, _script_directory
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_product_analytics_dormancy import (
    MIGRATION_0022,
    MIGRATION_0022_SHA256,
    MIGRATION_0023,
    MIGRATION_0023_SHA256,
    MIGRATION_0024,
    MIGRATION_0024_SHA256,
)

SERVICE = REPO_ROOT / "core" / "product_analytics" / "campaign_lifecycle.py"
BOUNDARY = REPO_ROOT / "core" / "_activity_campaign_lifecycle.py"
CAMPAIGNS = "product_campaigns"
COHORTS = "product_campaign_cohorts"
SEATS = "product_cohort_seats"
PARTICIPANTS = "product_campaign_participants"
SESSIONS = "product_activity_sessions"
INTERACTIONS = "product_activity_session_interactions"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HOURS_168 = timedelta(hours=168)
HOURS_2160 = timedelta(hours=2160)
DAYS_90 = timedelta(days=90)
ENTRY_POINTS = (cb.terminate_campaign, cb.terminate_campaigns_page, cb.purge_campaign_proof,
                cb.purge_campaign_proofs_page, cb.purge_campaign, cb.purge_campaigns_page)
ANALYTICS_TABLES = (EVENTS, ATTRIBUTIONS, REGISTRY, INTERACTIONS, SESSIONS, PARTICIPANTS, SEATS, COHORTS, CAMPAIGNS)
F2_TABLES = ("users", "invitations", "auth_operations")


def _call(function, engine, item=None):
    if function is cb.terminate_campaign or function is cb.purge_campaign:
        return function(engine, campaign_id=item or uuid.uuid4())
    if function is cb.purge_campaign_proof:
        return function(engine, proof_id=item or uuid.uuid4())
    return function(engine)


# --------------------------------------------------------------------------
# A. Contrats d'entrée (sans base)
# --------------------------------------------------------------------------

def test_the_vocabularies_are_closed_and_the_three_reports_are_distinct():
    assert cb.TERMINATION_OUTCOMES == ("closed", "cancelled", "already_terminal", "not_due", "absent", "conflict",
                                       "unknown", "unconfirmed", "still_present")
    assert cb.PROOF_PURGE_OUTCOMES == ("purged", "resolved_and_purged", "not_admissible", "not_due", "absent",
                                       "conflict", "unknown")
    assert cb.CAMPAIGN_PURGE_OUTCOMES == ("purged", "blocked", "not_admissible", "not_due", "absent", "conflict",
                                          "unknown")
    assert cl.BLOCKING_REASONS == ("proof_remaining", "attribution_remaining", "participation_remaining",
                                   "session_remaining", "interaction_remaining", "event_scope_remaining")
    for reason in ("retention_incoherent", "policy_incoherent", "cohort_incoherent", "seat_incoherent",
                   "dependency_changed", "write_unconfirmed", "scan_budget_exceeded"):
        assert reason in cl.REASONS
    assert len(set(cb.OUTCOME_REASONS)) == len(cb.OUTCOME_REASONS)
    assert cl.ADMITTED_RESOLUTIONS == (("rejected", "not_authorized"), ("indeterminate", "finalization_expired"))
    assert cl.DEFAULT_PAGE_SIZE == 50 and cl.MAX_PAGE_SIZE == 200 and cl.GLOBAL_SCAN_BUDGET_SECONDS == 10.0
    assert cl.SELECTIONS == ("campaign_termination", "campaign_proof_purge", "campaign_purge")
    assert tx.__name__ == "core._activity_transactions"


SERVICE_DESIGNATIONS = ({"force": True}, {"now": datetime.now(timezone.utc)}, {"ignore_retention": True},
                        {"ignore_dependencies": True}, {"status": "closed"}, {"reason": "not_authorized"},
                        {"retention_days": 1}, {"seats": ()})


def test_the_entry_points_receive_only_bounded_and_typed_inputs_and_refuse_before_any_database_access():
    for function, field in ((cl.terminate_due_campaign, "campaign_id"), (cl.observe_campaign_termination,
                                                                          "campaign_id"),
                            (cl.purge_campaign_expired_proof, "proof_id"), (cl.observe_campaign_proof_purge,
                                                                            "proof_id"),
                            (cl.purge_campaign, "campaign_id"), (cl.observe_campaign_purge, "campaign_id")):
        assert list(inspect.signature(function).parameters) == ["db", field]
        for bad in (str(uuid.uuid4()), None, 42, uuid.uuid4().int, uuid.uuid4().bytes):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), **{field: bad})
        for designation in SERVICE_DESIGNATIONS:
            with pytest.raises(TypeError):
                function(_NoDB(), **{field: uuid.uuid4()}, **designation)
    cursors = {cl.select_due_campaign_candidates: cl.TerminationCursor(datetime.now(timezone.utc), uuid.uuid4()),
               cl.select_campaign_expired_proof_candidates: cl.CampaignProofCursor(uuid.uuid4(), uuid.uuid4()),
               cl.select_purgeable_campaign_candidates: cl.CampaignPurgeCursor(uuid.uuid4())}
    for function, own in cursors.items():
        assert list(inspect.signature(function).parameters) == ["db", "limit", "after"]
        assert function.__defaults__ is None and function.__kwdefaults__ == {"limit": 50, "after": None}
        for limit in (0, 201, -1, True, 1.0, "5", None):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), limit=limit)
        for foreign in [c for f, c in cursors.items() if f is not function] + [
                pl.ParticipationCursor(uuid.uuid4(), USER_A), pl.SessionCursor(uuid.uuid4(), datetime.now(
                    timezone.utc), uuid.uuid4()), SimpleNamespace(campaign_id=uuid.uuid4()), "cursor", uuid.uuid4()]:
            with pytest.raises(InvalidProductAnalyticsInput):  # curseurs des trois parcours incompatibles
                function(_NoDB(), after=foreign)
        for kwargs in ({"now": datetime.now(timezone.utc)}, {"offset": 3}, {"campaign_id": uuid.uuid4()}):
            with pytest.raises(TypeError):
                function(_NoDB(), **kwargs)
        assert repr(own).endswith("(<private>)")
    for forged in (cl.TerminationCursor(datetime(2026, 11, 25), uuid.uuid4()),
                   cl.TerminationCursor(datetime.now(timezone.utc), str(uuid.uuid4()))):
        with pytest.raises(InvalidProductAnalyticsInput):
            cl.select_due_campaign_candidates(_NoDB(), after=forged)
    for forged in (cl.CampaignProofCursor(uuid.uuid4(), "p"), cl.CampaignProofCursor("c", uuid.uuid4())):
        with pytest.raises(InvalidProductAnalyticsInput):
            cl.select_campaign_expired_proof_candidates(_NoDB(), after=forged)
    with pytest.raises(InvalidProductAnalyticsInput):
        cl.select_purgeable_campaign_candidates(_NoDB(), after=cl.CampaignPurgeCursor(str(uuid.uuid4())))
    for private, attempt in ((cl._observe_termination_attempt, cl._TerminationAttempt(uuid.uuid4(), (), ())),
                             (cl._observe_proof_attempt, cl._ProofAttempt(uuid.uuid4(), (), ())),
                             (cl._observe_campaign_attempt, cl._CampaignAttempt(uuid.uuid4()))):
        with pytest.raises(InvalidProductAnalyticsInput):
            private(_NoDB(), attempt=SimpleNamespace(**{k: getattr(attempt, k) for k in attempt.__slots__}))
        assert repr(attempt).endswith("(<private>)")


def test_the_boundary_refuses_every_invalid_engine_and_input_before_any_connection():
    engine = sa.create_engine("postgresql+psycopg2://u@127.0.0.1:1/oryx_never_test")
    for bad in (None, "postgresql://x", sa.create_engine("sqlite://"),
                sa.create_engine("postgresql+psycopg2://u@127.0.0.1:1/x", isolation_level="AUTOCOMMIT"),
                engine.execution_options(postgresql_readonly=True), Session(), engine.connect):
        for function in ENTRY_POINTS:
            with pytest.raises(TypeError):
                _call(function, bad)
    for function, field in ((cb.terminate_campaign, "campaign_id"), (cb.purge_campaign_proof, "proof_id"),
                            (cb.purge_campaign, "campaign_id")):
        for bad in (str(uuid.uuid4()), None, 7):
            with pytest.raises(TypeError):
                function(engine, **{field: bad})
        with pytest.raises(TypeError):
            function(engine, **{field: uuid.uuid4()}, force=True)
    for function, foreign in ((cb.terminate_campaigns_page, cl.CampaignPurgeCursor(uuid.uuid4())),
                              (cb.purge_campaign_proofs_page, cl.CampaignPurgeCursor(uuid.uuid4())),
                              (cb.purge_campaigns_page, cl.CampaignProofCursor(uuid.uuid4(), uuid.uuid4()))):
        with pytest.raises(InvalidProductAnalyticsInput):
            function(engine, after=foreign)  # avant toute connexion (port 1 : jamais joint)
        for limit in (0, 201, True):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(engine, limit=limit)


# --------------------------------------------------------------------------
# PostgreSQL réel : outils
# --------------------------------------------------------------------------

def _campaign(db, campaign_id):
    found = rows(db, f"SELECT *, xmin::text AS xmin FROM {CAMPAIGNS} WHERE id = :i", i=campaign_id)
    return found[0] if found else None


def _deadline(db, campaign_id):
    row = _campaign(db, campaign_id)
    return pl.participation_retention_deadline(SimpleNamespace(**row))


def _world(db) -> dict:
    return {table: rows(db, f"SELECT t.*, t.xmin::text AS xmin FROM {table} t ORDER BY t::text")
            for table in ANALYTICS_TABLES + F2_TABLES}


def _count(db, table, campaign_id=None):
    if campaign_id is None:
        return rows(db, f"SELECT count(*) AS n FROM {table}")[0]["n"]
    return rows(db, f"SELECT count(*) AS n FROM {table} WHERE campaign_id = :c", c=campaign_id)[0]["n"]


def _create(db, *, close_in=timedelta(days=30), code=None):
    return committed(db, lambda s: pc.create_campaign(
        s, code=code or f"wave-{uuid.uuid4().hex[:10]}", name="Vague 0", purpose="Mesurer l'usage d'Oryx.",
        close_by_at=common.utcnow() + close_in, **POLICY).id)


def _activate(db, campaign_id):
    committed(db, lambda s: pc.activate_campaign(s, campaign_id=campaign_id))


def _active(db, *, close_in=timedelta(days=30)):
    campaign_id = _create(db, close_in=close_in)
    _activate(db, campaign_id)
    return campaign_id


def _enroll(db, campaign_id, user=USER_A):
    return committed(db, lambda s: pp.enroll_at_first_eligible_login(
        s, campaign_id=campaign_id, user_id=user, authorization_recorded_at=common.utcnow(), notice_version=NOTICE))


def _generic(db, campaign_id, user=USER_A, *, window=timedelta(days=12)):
    now = common.utcnow()
    return committed(db, lambda s: pp.enroll_participant(
        s, campaign_id=campaign_id, user_id=user, window_start_at=now, window_end_at=now + window,
        authorization_recorded_at=now, notice_version=NOTICE))


def _close(db, campaign_id):
    committed(db, lambda s: pc.close_campaign(s, campaign_id=campaign_id))


def _cancel(db, campaign_id):
    committed(db, lambda s: pc.cancel_campaign(s, campaign_id=campaign_id))


def terminate(db, campaign_id):
    return cb.terminate_campaign(db, campaign_id=campaign_id)


def purge_proof(db, proof_id):
    return cb.purge_campaign_proof(db, proof_id=proof_id)


def purge_campaign(db, campaign_id):
    return cb.purge_campaign(db, campaign_id=campaign_id)


def _proof(db, proof_id):
    found = rows(db, f"SELECT *, xmin::text AS xmin FROM {REGISTRY} WHERE id = :i", i=proof_id)
    return found[0] if found else None


def _expire(db, clock, campaign_id, *, after=timedelta(0)):
    """Échéance commune de la campagne atteinte (clôture à l'instant courant si active)."""
    clock.advance(minutes=5)
    if _campaign(db, campaign_id)["status"] == "active":
        _close(db, campaign_id)
    return clock.set(_deadline(db, campaign_id) + after)


CLOSED = cb.Outcome(cb.CLOSED)
CANCELLED = cb.Outcome(cb.CANCELLED)
ALREADY = cb.Outcome(cb.ALREADY_TERMINAL)
NOT_DUE = cb.Outcome(cb.NOT_DUE)
ABSENT = cb.Outcome(cb.ABSENT)
PURGED = cb.Outcome(cb.PURGED)


# --------------------------------------------------------------------------
# B. Terminaison (parcours A)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["planned", "active"])
def test_termination_is_refused_one_microsecond_before_close_by_at_and_due_at_exact_equality(db, clock, status):
    campaign_id = _create(db) if status == "planned" else _active(db)
    row = _campaign(db, campaign_id)
    clock.set(utc(row["close_by_at"]) - MICRO)
    assert terminate(db, campaign_id) == NOT_DUE
    assert _campaign(db, campaign_id) == row  # xmin compris : aucune écriture
    clock.set(utc(row["close_by_at"]))
    assert terminate(db, campaign_id) == (CANCELLED if status == "planned" else CLOSED)
    after = _campaign(db, campaign_id)
    expected = {**row, "status": "cancelled" if status == "planned" else "closed", "closed_at": row["close_by_at"]}
    assert {k: v for k, v in after.items() if k != "xmin"} == {k: v for k, v in expected.items() if k != "xmin"}
    assert terminate(db, campaign_id) == ALREADY
    assert _campaign(db, campaign_id) == after  # jamais réécrite


@pytest.mark.parametrize("status", ["planned", "active"])
def test_a_late_termination_never_moves_the_effective_end_and_only_two_columns_change(db, clock, status):
    campaign_id = _create(db) if status == "planned" else _active(db)
    participant = _generic(db, campaign_id) if status == "planned" else _enroll(db, campaign_id)
    del participant
    world = _world(db)
    row = _campaign(db, campaign_id)
    close_by = utc(row["close_by_at"])
    clock.set(close_by + timedelta(days=15))  # exécution réelle à J+45 pour une échéance J+30
    assert terminate(db, campaign_id).outcome in (cb.CLOSED, cb.CANCELLED)
    after = _campaign(db, campaign_id)
    assert utc(after["closed_at"]) == close_by  # jamais l'instant de traitement
    assert _deadline(db, campaign_id) == close_by + HOURS_2160  # J+120, jamais J+135
    changed = {k for k in row if k != "xmin" and row[k] != after[k]}
    assert changed == {"status", "closed_at"}
    new_world = _world(db)
    for table in ANALYTICS_TABLES + F2_TABLES:
        if table != CAMPAIGNS:
            assert new_world[table] == world[table], table  # aucun descendant modifié


def test_termination_matrix_absent_incoherent_and_clock_behind_a_stored_instant(db, clock):
    assert terminate(db, uuid.uuid4()) == ABSENT
    campaign_id = _create(db)
    clock.set(utc(_campaign(db, campaign_id)["close_by_at"]) + timedelta(days=1))
    for statement, reason in (
            (f"UPDATE {CAMPAIGNS} SET retention_policy_version = 'v9' WHERE id = :i", "policy_incoherent"),
            (f"UPDATE {CAMPAIGNS} SET retention_days = 30 WHERE id = :i", "policy_incoherent"),
            (f"UPDATE {CAMPAIGNS} SET measurement_policy_version = 'v9' WHERE id = :i", "policy_incoherent"),
            (f"UPDATE {CAMPAIGNS} SET status = 'cancelled' WHERE id = :i", "retention_incoherent"),
            (f"UPDATE {CAMPAIGNS} SET activated_at = created_at WHERE id = :i", "retention_incoherent")):
        original = _campaign(db, campaign_id)
        _sql(db, statement, i=campaign_id)
        forged = _campaign(db, campaign_id)
        assert terminate(db, campaign_id) == cb.Outcome(cb.CONFLICT, reason), statement
        assert _campaign(db, campaign_id) == forged
        _sql(db, f"UPDATE {CAMPAIGNS} SET status = :s, activated_at = :a, retention_policy_version = :r, "
                 "retention_days = :d, measurement_policy_version = :m WHERE id = :i", s=original["status"],
             a=original["activated_at"], r=original["retention_policy_version"], d=original["retention_days"],
             m=original["measurement_policy_version"], i=campaign_id)
    # Instant historique postérieur à l'horloge (création future) : unknown, jamais une décision.
    clock.set(utc(_campaign(db, campaign_id)["created_at"]) - timedelta(seconds=1))
    assert terminate(db, campaign_id) == cb.Outcome(cb.UNKNOWN, cl.CLOCK_INCOHERENT)
    # Campagne déjà terminale mais incohérente : conflict avant already_terminal.
    active = _active(db)
    clock.set(utc(_campaign(db, active)["close_by_at"]) + timedelta(days=1))
    with pytest.raises(sa.exc.IntegrityError, match="ck_product_campaigns_closed"):
        _sql(db, f"UPDATE {CAMPAIGNS} SET status = 'closed', closed_at = NULL WHERE id = :i", i=active)
    _sql(db, f"UPDATE {CAMPAIGNS} SET status = 'cancelled', closed_at = NULL WHERE id = :i", i=active)  # admis
    forged = _campaign(db, active)
    assert terminate(db, active) == cb.Outcome(cb.CONFLICT, cl.RETENTION_INCOHERENT)
    assert _campaign(db, active) == forged


def test_a_cancelled_or_closed_campaign_keeps_its_historical_closed_at(db, clock):
    cancelled = _create(db)
    clock.advance(hours=2)
    _cancel(db, cancelled)  # jamais activée : légitime
    closed = _active(db)
    clock.advance(hours=3)
    _close(db, closed)
    before = {c: _campaign(db, c) for c in (cancelled, closed)}
    clock.advance(days=60)
    assert terminate(db, cancelled) == ALREADY and terminate(db, closed) == ALREADY
    assert {c: _campaign(db, c) for c in (cancelled, closed)} == before


def test_a_regressive_clock_writes_nothing_then_termination_is_clean(db, clock):
    campaign_id = _active(db)
    row = _campaign(db, campaign_id)
    clock.set(utc(row["activated_at"]) - MICRO)  # horloge antérieure à l'activation stockée
    assert terminate(db, campaign_id) == cb.Outcome(cb.UNKNOWN, cl.CLOCK_INCOHERENT)
    assert _campaign(db, campaign_id) == row
    clock.set(utc(row["close_by_at"]))
    assert terminate(db, campaign_id) == CLOSED


def test_the_postgresql_clock_is_read_after_a_lock_wait_that_crosses_close_by_at(db):
    """Horloge réelle : la campagne est verrouillée par une transaction tierce
    jusqu'après close_by_at ; la terminaison, en attente, décide APRÈS."""
    now = datetime.now(timezone.utc)
    campaign_id = committed(db, lambda s: pc.create_campaign(
        s, code=f"wave-{uuid.uuid4().hex[:10]}", name="Vague 0", purpose="Mesurer.",
        close_by_at=now + timedelta(seconds=1), **POLICY).id)
    holder = Holder(db, lambda s: s.execute(sa.text(f"SELECT 1 FROM {CAMPAIGNS} WHERE id = :i FOR SHARE"),
                                            {"i": campaign_id}))
    contender = Background(lambda: terminate(db, campaign_id))
    contender.start()
    _wait(lambda: waiting(db) >= 1, "terminaison en attente du verrou")
    while datetime.now(timezone.utc) < now + timedelta(seconds=1, milliseconds=200):
        pass
    holder.commit()
    assert contender.outcome() == CANCELLED  # lock_timeout 2 s : l'attente reste inférieure


# --------------------------------------------------------------------------
# C. Conservation : 90 jours absolus depuis la fin effective
# --------------------------------------------------------------------------

@pytest.mark.parametrize("zone", ["UTC", "Europe/Paris", "America/New_York"])
def test_the_common_deadline_is_2160_absolute_hours_whatever_the_session_time_zone(db, clock, zone):
    # Clôture le 15 janvier 2027 : les 90 jours franchissent le passage à l'heure d'été (Paris, New York).
    campaign_id = _active(db, close_in=timedelta(days=400))
    clock.set(datetime(2027, 1, 15, 10, tzinfo=timezone.utc))
    _close(db, campaign_id)
    closed_at = utc(_campaign(db, campaign_id)["closed_at"])
    deadline = _deadline(db, campaign_id)
    assert deadline == closed_at + HOURS_2160 and deadline - closed_at == DAYS_90
    with db.connect() as conn:
        conn.execute(sa.text(f"SET TIME ZONE '{zone}'"))
        sql_deadline = conn.execute(sa.select(pl._potential_deadline()).where(
            pc.ProductCampaign.id == campaign_id)).scalar_one()
    assert utc(sql_deadline) == deadline  # secondes absolues, jamais des jours civils
    local = ZoneInfo(zone)
    if zone != "UTC":  # heure murale différente : 2 160 heures absolues, jamais 90 jours civils
        assert deadline.astimezone(local).hour != closed_at.astimezone(local).hour
    for instant, expected in ((deadline - MICRO, cb.NOT_DUE), (deadline, cb.PURGED)):
        clock.set(instant)
        assert purge_campaign(db, campaign_id).outcome == expected


def test_early_and_late_closures_and_never_activated_cancellations_use_their_effective_end(db, clock):
    early = _active(db)
    clock.advance(hours=2)
    _close(db, early)
    late = _active(db, close_in=timedelta(hours=1))
    clock.advance(hours=5)
    _close(db, late)  # campaigns._closing_time : bornée par close_by_at
    never = _create(db)
    clock.advance(minutes=1)
    _cancel(db, never)
    for campaign_id in (early, late, never):
        row = _campaign(db, campaign_id)
        assert _deadline(db, campaign_id) == min(utc(row["closed_at"]), utc(row["close_by_at"])) + HOURS_2160
    assert utc(_campaign(db, late)["closed_at"]) == utc(_campaign(db, late)["close_by_at"])


def test_a_planned_campaign_has_no_deadline_and_an_active_one_its_maximal_deadline(db, clock):
    planned = _create(db)
    _generic(db, planned)  # inscription générique d'une campagne planned
    active = _active(db, close_in=timedelta(days=10))
    clock.set(utc(_campaign(db, planned)["close_by_at"]) + timedelta(days=365))
    assert purge_campaign(db, planned) == cb.Outcome(cb.NOT_ADMISSIBLE, cl.DEADLINE_UNESTABLISHED)
    assert purge_campaign(db, active) == cb.Outcome(cb.NOT_ADMISSIBLE, cl.CAMPAIGN_NOT_TERMINAL)
    assert _deadline(db, active) == utc(_campaign(db, active)["close_by_at"]) + HOURS_2160
    assert _deadline(db, planned) is None  # aucune date de substitution


# --------------------------------------------------------------------------
# D-G. Purge exceptionnelle des preuves (parcours B) : outils
# --------------------------------------------------------------------------

def _pending(db, *, campaign_id=None, user=USER_A):
    from tests.activity_proof_support import open_wave
    campaign_id = campaign_id or open_wave(db, user)
    op = operation(user)
    origin = open_origin(db, op, campaign_id=campaign_id)
    pending = register(db, origin, op)
    assert pending.state == proofs.PENDING
    return SimpleNamespace(proof_id=pending.proof_id, campaign_id=campaign_id, origin=origin, op=op)


def _in_state(db, clock, state):
    if state in ("certified", "recorded"):
        proof = _certified(db)
        if state == "recorded":
            clock.advance(seconds=1)
            assert finalize(db, proof.proof_id).state == fz.RECORDED
        return proof
    proof = _pending(db)
    if state == "indeterminate":
        assert settle(db, proof.origin, proof.op, proof.proof_id).state == proofs.INDETERMINATE
    elif state == "rejected":
        clock.advance(days=13)  # certification après J+12 : rejected / window_expired
        assert certify(db, proof.origin, proof.op, proof.proof_id).state == proofs.REJECTED
    return proof


def _definitive(db):
    return rows(db, f"SELECT e.*, e.xmin::text AS xmin FROM {EVENTS} e ORDER BY id"), rows(
        db, f"SELECT a.*, a.xmin::text AS xmin FROM {ATTRIBUTIONS} a ORDER BY event_id")


def _parents(db, campaign_id):
    """Participation, visites, interactions et campagne (xmin compris)."""
    return {table: rows(db, f"SELECT t.*, t.xmin::text AS xmin FROM {table} t WHERE "
                            f"{'id' if table == CAMPAIGNS else 'campaign_id'} = :c ORDER BY t::text", c=campaign_id)
            for table in (CAMPAIGNS, PARTICIPANTS, SESSIONS, INTERACTIONS)}


def _capture_resolution(db, monkeypatch):
    """Valeurs RÉELLEMENT écrites par la résolution, lues dans la transaction
    avant le DELETE (aucun journal persistant)."""
    seen = []
    real = cl._db_row

    def spy(session, table, proof_id):
        row = real(session, table, proof_id)
        seen.append(dict(row) if row is not None else None)
        return row
    monkeypatch.setattr(cl, "_db_row", spy)
    return seen


def _long_window(db, clock, *, user=USER_A, close_by=timedelta(days=200), window=timedelta(days=180)):
    """Campagne GÉNÉRIQUE à longue fenêtre (vrais services) : création,
    activation, inscription explicite [J0, J0 + 180 j), interaction et preuve
    pending légitimes."""
    campaign_id = _active(db, close_in=close_by)
    _generic(db, campaign_id, user, window=window)
    clock.advance(minutes=1)
    return _pending(db, campaign_id=campaign_id, user=user)


# --------------------------------------------------------------------------
# D. Preuves pending (correction B1)
# --------------------------------------------------------------------------

def test_a_pending_proof_before_the_campaign_deadline_is_not_due_and_untouched(db, clock):
    proof = _pending(db)
    deadline = _expire(db, clock, proof.campaign_id)
    clock.set(deadline - MICRO)
    before = _proof(db, proof.proof_id)
    assert purge_proof(db, proof.proof_id) == NOT_DUE
    assert _proof(db, proof.proof_id) == before


def test_a_pending_proof_whose_finalization_deadline_is_reached_is_resolved_indeterminate_then_purged(
        db, clock, monkeypatch):
    proof = _pending(db)
    deadline = _expire(db, clock, proof.campaign_id, after=timedelta(seconds=3))
    world, parents = _definitive(db), _parents(db, proof.campaign_id)
    seen = _capture_resolution(db, monkeypatch)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.RESOLVED_AND_PURGED, None,
                                                         ("indeterminate", "finalization_expired"))
    [written] = seen
    assert (written["status"], written["resolution_reason"], written["certified_at"]) == (
        "indeterminate", "finalization_expired", None)
    assert utc(written["resolved_at"]) == deadline  # instant réel (échéance + 3 s), jamais antidaté
    assert written["purge_after_at"] - written["resolved_at"] == HOURS_168
    assert _proof(db, proof.proof_id) is None
    assert _definitive(db) == world and _parents(db, proof.campaign_id) == parents  # aucun autre objet


@pytest.mark.parametrize("ending", ["closed", "cancelled"])
def test_b1_an_early_closure_leaves_the_historical_pending_resolution_refused_and_the_exceptional_path_rejects(
        db, clock, monkeypatch, ending):
    """Contre-épreuve B1 : clôture anticipée à J+10, conservation échue à
    J+100, fin contractuelle d'observation à J+180."""
    proof = _long_window(db, clock)
    j0 = clock.now
    clock.set(j0 + timedelta(days=10))
    (_close if ending == "closed" else _cancel)(db, proof.campaign_id)
    deadline = _deadline(db, proof.campaign_id)
    assert deadline == utc(_campaign(db, proof.campaign_id)["closed_at"]) + HOURS_2160
    clock.set(j0 + timedelta(days=100, hours=1))
    assert clock.now >= deadline
    # Bloc C historique : aucune résolution finalization_expired avant J+180 (inchangé).
    assert expire(db, proof.proof_id).state == proofs.PENDING
    assert _proof(db, proof.proof_id)["status"] == "pending"
    # PR 3 : bloquée par la preuve résiduelle.
    assert pb.purge_participation(db, campaign_id=proof.campaign_id, user_id=USER_A) == pb.PurgeOutcome(
        pb.BLOCKED, pl.PROOF_REMAINING)
    seen = _capture_resolution(db, monkeypatch)
    definitive = _definitive(db)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.RESOLVED_AND_PURGED, None, ("rejected", "not_authorized"))
    [written] = seen
    assert (written["status"], written["resolution_reason"], utc(written["resolved_at"]),
            written["purge_after_at"] - written["resolved_at"], written["certified_at"]) == (
        "rejected", "not_authorized", clock.now, HOURS_168, None)
    assert _proof(db, proof.proof_id) is None and _definitive(db) == definitive  # aucun événement créé
    assert pb.purge_participation(db, campaign_id=proof.campaign_id, user_id=USER_A).outcome == pb.PURGED
    assert purge_campaign(db, proof.campaign_id) == PURGED
    assert _campaign(db, proof.campaign_id) is None


def test_b1_never_applies_before_the_campaign_deadline_nor_through_the_historical_path(db, clock):
    proof = _long_window(db, clock)
    j0 = clock.now
    clock.set(j0 + timedelta(days=10))
    _close(db, proof.campaign_id)
    clock.set(_deadline(db, proof.campaign_id) - MICRO)
    before = _proof(db, proof.proof_id)
    assert purge_proof(db, proof.proof_id) == NOT_DUE
    assert expire(db, proof.proof_id).state == proofs.PENDING
    assert _proof(db, proof.proof_id) == before


def test_a_regressive_clock_resolves_nothing_and_a_planned_campaign_is_not_admissible(db, clock):
    proof = _pending(db)
    _expire(db, clock, proof.campaign_id)
    before = _proof(db, proof.proof_id)
    clock.set(utc(before["created_at"]) - MICRO)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.UNKNOWN, cl.CLOCK_INCOHERENT)
    assert _proof(db, proof.proof_id) == before
    _sql(db, f"UPDATE {CAMPAIGNS} SET status = 'planned', activated_at = NULL, closed_at = NULL WHERE id = :i",
         i=proof.campaign_id)  # admis par les CHECK de 0022
    clock.advance(days=400)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.NOT_ADMISSIBLE, cl.DEADLINE_UNESTABLISHED)
    assert _proof(db, proof.proof_id) == before


# --------------------------------------------------------------------------
# E. Preuves certified (correction B2 : bloc C sans création d'événement)
# --------------------------------------------------------------------------

def test_a_certified_proof_of_a_terminal_campaign_is_rejected_not_authorized_then_purged(db, clock, monkeypatch):
    proof = _certified(db)
    certified_at = _proof(db, proof.proof_id)["certified_at"]
    _expire(db, clock, proof.campaign_id)
    definitive, parents = _definitive(db), _parents(db, proof.campaign_id)
    seen = _capture_resolution(db, monkeypatch)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.RESOLVED_AND_PURGED, None, ("rejected", "not_authorized"))
    assert seen[0]["certified_at"] == certified_at  # certified_at conservé
    assert _proof(db, proof.proof_id) is None
    assert _definitive(db) == definitive and _parents(db, proof.campaign_id) == parents


def test_a_certified_proof_of_an_active_expired_campaign_follows_the_historical_reason_order(db, clock, monkeypatch):
    enrolled = _certified(db)
    withdrawn = _certified(db, USER_B)
    committed(db, lambda s: pp.withdraw_participant(s, campaign_id=withdrawn.campaign_id, user_id=USER_B))
    for proof in (enrolled, withdrawn):
        clock.set(max(clock.now, utc(_campaign(db, proof.campaign_id)["close_by_at"]) + HOURS_2160))
    assert {_campaign(db, p.campaign_id)["status"] for p in (enrolled, withdrawn)} == {"active"}
    seen = _capture_resolution(db, monkeypatch)
    assert purge_proof(db, enrolled.proof_id) == cb.Outcome(cb.RESOLVED_AND_PURGED, None,
                                                            ("indeterminate", "finalization_expired"))
    assert purge_proof(db, withdrawn.proof_id) == cb.Outcome(cb.RESOLVED_AND_PURGED, None,
                                                             ("rejected", "not_authorized"))  # retrait d'abord
    assert [w["status"] for w in seen] == ["indeterminate", "rejected"]
    assert _count(db, EVENTS) == 0 and _count(db, ATTRIBUTIONS) == 0  # aucune création


@pytest.mark.parametrize("forgery", ["interaction_after_proof", "certified_after_window", "foreign_environment",
                                     "unattested_nature", "interaction_outside_visit"])
def test_a_certified_proof_whose_integrity_is_not_established_is_never_resolved_nor_deleted(db, clock, forgery):
    proof = _certified(db)
    row = _proof(db, proof.proof_id)
    if forgery == "interaction_after_proof":
        _sql(db, f"UPDATE {INTERACTIONS} SET recorded_at = :t WHERE interaction_key = :k",
             t=row["created_at"] + MICRO, k=row["interaction_key"])
    elif forgery == "certified_after_window":
        [participation] = rows(db, f"SELECT * FROM {PARTICIPANTS} WHERE campaign_id = :c", c=proof.campaign_id)
        _sql(db, f"UPDATE {REGISTRY} SET certified_at = :t WHERE id = :i", t=participation["window_end_at"],
             i=proof.proof_id)  # certification à la borne exacte de la fenêtre : jamais émise par le bloc B
    elif forgery == "foreign_environment":
        _sql(db, f"UPDATE {SESSIONS} SET last_activity_at = opened_at, idle_expires_at = opened_at + "
                 "interval '30 minutes' WHERE id = :v", v=row["visit_id"])
        _sql(db, f"UPDATE {INTERACTIONS} SET recorded_at = recorded_at + interval '1 second' "
                 "WHERE interaction_key = :k", k=row["interaction_key"])
    elif forgery == "unattested_nature":
        with pytest.raises(sa.exc.IntegrityError):  # nature hors couple fermé : refusée par 0024
            _sql(db, f"UPDATE {REGISTRY} SET event_type = 'coach_used' WHERE id = :i", i=proof.proof_id)
        _sql(db, f"UPDATE {REGISTRY} SET event_type = 'coach_used', surface = 'coach', "
                 "source_operation_kind = 'assistant_delivery' WHERE id = :i", i=proof.proof_id)
    elif forgery == "interaction_outside_visit":
        _sql(db, f"UPDATE {SESSIONS} SET opened_at = opened_at + interval '1 second', last_activity_at = "
                 "last_activity_at + interval '1 second', idle_expires_at = idle_expires_at + interval '1 second' "
                 "WHERE id = :v", v=row["visit_id"])  # interaction antérieure à l'ouverture de sa visite
    _expire(db, clock, proof.campaign_id)
    before = _proof(db, proof.proof_id)
    outcome = purge_proof(db, proof.proof_id)
    assert outcome.outcome == cb.CONFLICT and outcome.reason in (cl.PROOF_INCOHERENT, cl.PARTICIPATION_INCOHERENT,
                                                                 cl.SESSION_INCOHERENT), (forgery, outcome)
    assert _proof(db, proof.proof_id) == before  # jamais résolue puis supprimée
    assert _count(db, EVENTS) == 0


def test_the_exceptional_path_never_reaches_an_event_writer(monkeypatch, db, clock):
    proof = _certified(db)
    _expire(db, clock, proof.campaign_id)
    from core.product_analytics import events as pe

    def forbidden(*args, **kwargs):
        raise AssertionError("chemin de création d'événement atteint")
    for module, name in ((fz, "finalize_certified_proof"), (fz, "_record"), (pe, "_stage_certified_event"),
                         (pe, "record_event"), (fz, "resolve_expired_pending_proof"), (fz, "_lock")):
        monkeypatch.setattr(module, name, forbidden)
    assert purge_proof(db, proof.proof_id).outcome == cb.RESOLVED_AND_PURGED
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "finalization"}
    assert used == {"RECORD_INCOHERENT", "lock_keys", "finalization_deadline", "_certification_problem", "_resolve",
                    "_Locked", "_identity", "IDENTITY_FIELDS", "_accepted_during_observation"}


# --------------------------------------------------------------------------
# F. Preuves terminales rejected et indeterminate
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["rejected", "indeterminate"])
def test_a_terminal_proof_is_deleted_without_a_second_resolution(db, clock, state):
    proof = _in_state(db, clock, state)
    before = _proof(db, proof.proof_id)
    seen, remove = _statements(db)
    try:
        _expire(db, clock, proof.campaign_id)
        assert purge_proof(db, proof.proof_id) == PURGED
    finally:
        remove()
    assert not [s for s in seen if s.startswith(f"UPDATE {REGISTRY}")]  # aucune nouvelle résolution
    assert _proof(db, proof.proof_id) is None
    del before


def test_an_extremely_late_resolution_is_purged_at_once_when_the_campaign_is_expired(db, clock):
    proof = _pending(db)
    deadline = _expire(db, clock, proof.campaign_id)
    clock.set(deadline - timedelta(days=1))
    assert settle(db, proof.origin, proof.op, proof.proof_id).state == proofs.INDETERMINATE  # tardive
    row = _proof(db, proof.proof_id)
    clock.set(deadline + timedelta(minutes=1))
    assert clock.now < utc(row["purge_after_at"])  # 168 heures non écoulées
    assert committed(db, lambda s: prl.purge_resolved_proof(s, proof_id=proof.proof_id)).reason == prl.NOT_DUE
    assert purge_proof(db, proof.proof_id) == PURGED  # échéance parente : aucune attente supplémentaire
    assert _proof(db, proof.proof_id) is None


def test_a_terminal_proof_with_an_incompatible_reason_or_a_clock_before_its_resolution_is_kept(db, clock):
    late = _pending(db)
    deadline = _expire(db, clock, late.campaign_id)
    clock.set(deadline + timedelta(hours=2))
    assert settle(db, late.origin, late.op, late.proof_id).state == proofs.INDETERMINATE
    clock.set(deadline + timedelta(hours=1))  # échéance atteinte, horloge antérieure à resolved_at
    before = _proof(db, late.proof_id)
    assert purge_proof(db, late.proof_id) == cb.Outcome(cb.UNKNOWN, cl.CLOCK_INCOHERENT)
    clock.set(deadline + timedelta(hours=3))
    _sql(db, f"UPDATE {REGISTRY} SET resolution_reason = 'window_expired' WHERE id = :i", i=late.proof_id)
    assert purge_proof(db, late.proof_id) == cb.Outcome(cb.CONFLICT, cl.PROOF_INCOHERENT)  # motif de rejected
    assert {k: v for k, v in _proof(db, late.proof_id).items() if k not in ("xmin", "resolution_reason")} == {
        k: v for k, v in before.items() if k not in ("xmin", "resolution_reason")}
    with pytest.raises(sa.exc.IntegrityError, match="ck_product_activity_proof_registry_purge_after"):
        _sql(db, f"UPDATE {REGISTRY} SET purge_after_at = purge_after_at + interval '1 hour' WHERE id = :i",
             i=late.proof_id)  # échéance individuelle incohérente : refusée par 0024


# --------------------------------------------------------------------------
# G. Preuves recorded (correction B3)
# --------------------------------------------------------------------------

def _recorded(db, clock, kind="portfolio_position_create"):
    proof = _certified(db, kind=kind)
    clock.advance(seconds=1)
    report = finalize(db, proof.proof_id)
    assert report.state == fz.RECORDED
    proof.event_id = report.event_id
    return proof


def _expired_recorded(db, clock, kind="portfolio_position_create"):
    proof = _recorded(db, clock, kind)
    _expire(db, clock, proof.campaign_id)
    [event] = rows(db, f"SELECT * FROM {EVENTS} WHERE id = :i", i=proof.event_id)
    assert utc(event["next_scope_expires_at"]) <= clock.now  # événement déjà échu (90 j depuis occurred_at)
    return proof


def test_a_coherent_recorded_proof_of_an_expired_event_is_purged_and_the_event_is_left_to_pr2(db, clock):
    proof = _expired_recorded(db, clock)
    definitive = _definitive(db)
    assert committed(db, lambda s: el.inspect_event_integrity(s, event_id=proof.event_id)) == el.IntegrityDecision(
        el.VALID)
    assert purge_proof(db, proof.proof_id) == PURGED
    assert _proof(db, proof.proof_id) is None and _definitive(db) == definitive  # événement et attribution intacts
    # Non-régression PR 2 : l'événement reste valide puis est purgé par elle seule.
    assert committed(db, lambda s: el.inspect_event_integrity(s, event_id=proof.event_id)) == el.IntegrityDecision(
        el.VALID)
    assert eb.expire_event(db, event_id=proof.event_id) == eb.EventOutcome(eb.PURGED)
    assert _count(db, EVENTS) == 0 and _count(db, ATTRIBUTIONS) == 0


def test_a_legitimately_shortened_scope_keeps_the_recorded_proof_purgeable(db, clock):
    proof = _expired_recorded(db, clock)
    [event] = rows(db, f"SELECT * FROM {EVENTS} WHERE id = :i", i=proof.event_id)
    document = event["retention_scopes"]
    shortened = utc(event["next_scope_expires_at"]) - timedelta(days=10)
    document["scopes"][0]["expires_at"] = retention.format_instant(shortened)
    _sql(db, f"UPDATE {EVENTS} SET retention_scopes = CAST(:d AS jsonb), next_scope_expires_at = :t, "
             "purge_after_at = :t WHERE id = :i", d=json.dumps(document), t=shortened, i=proof.event_id)
    assert purge_proof(db, proof.proof_id) == PURGED


RECORDED_FORGERIES = {
    "recorded_and_updated_plus_5_minutes": (
        f"UPDATE {EVENTS} SET recorded_at = recorded_at + interval '5 minutes', "
        "updated_at = updated_at + interval '5 minutes' WHERE id = :e"),
    "delivery_status": f"UPDATE {EVENTS} SET delivery_status = 'unconfirmed' WHERE id = :e",
    "delivered_at": f"UPDATE {EVENTS} SET delivered_at = delivered_at + interval '1 second' WHERE id = :e",
    "attribution_certified_at": (f"UPDATE {ATTRIBUTIONS} SET certified_at = certified_at - interval '1 second' "
                                 "WHERE event_id = :e"),
    "event_resolved_at": f"UPDATE {EVENTS} SET resolved_at = resolved_at + interval '1 second' WHERE id = :e",
    "route": f"UPDATE {EVENTS} SET executed_route = 'legacy' WHERE id = :e",
    "outcome": f"UPDATE {EVENTS} SET outcome = 'indeterminate' WHERE id = :e",
    "source": f"UPDATE {EVENTS} SET source = 'client_signal' WHERE id = :e",
    "occurred_at": f"UPDATE {EVENTS} SET occurred_at = occurred_at - interval '1 second' WHERE id = :e",
    "updated_at_only": f"UPDATE {EVENTS} SET updated_at = updated_at + interval '1 second' WHERE id = :e",
}


@pytest.mark.parametrize("forgery", sorted(RECORDED_FORGERIES))
def test_a_canonical_divergence_of_the_recorded_event_forbids_the_exceptional_deletion(db, clock, forgery):
    proof = _expired_recorded(db, clock, kind="decryptage_delivery_ack")
    _sql(db, RECORDED_FORGERIES[forgery], e=proof.event_id)
    before = _proof(db, proof.proof_id)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.CONFLICT, cl.RECORD_INCOHERENT), forgery
    assert _proof(db, proof.proof_id) == before


def test_the_five_minute_shift_is_detected_only_through_proof_resolved_at(db, clock):
    """Contre-épreuve B3 : recorded_at et updated_at décalés de cinq minutes
    restent dans les bornes générales ; seule la comparaison avec
    proof.resolved_at révèle la divergence."""
    proof = _expired_recorded(db, clock)
    _sql(db, RECORDED_FORGERIES["recorded_and_updated_plus_5_minutes"], e=proof.event_id)
    [event] = rows(db, f"SELECT * FROM {EVENTS} WHERE id = :i", i=proof.event_id)
    row = _proof(db, proof.proof_id)
    assert event["updated_at"] == event["recorded_at"] and event["recorded_at"] - row["resolved_at"] == timedelta(
        minutes=5)
    assert committed(db, lambda s: el.inspect_event_integrity(s, event_id=proof.event_id)) == el.IntegrityDecision(
        el.INCONSISTENT, el.PROOF_INCOHERENT)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.CONFLICT, cl.RECORD_INCOHERENT)


@pytest.mark.parametrize("document", ["two_scopes", "other_campaign"])
def test_a_recorded_event_with_several_scopes_or_another_campaign_is_refused(db, clock, document):
    proof = _expired_recorded(db, clock)
    other = _active(db)
    _enroll(db, other)
    [event] = rows(db, f"SELECT * FROM {EVENTS} WHERE id = :i", i=proof.event_id)
    scope = event["retention_scopes"]["scopes"][0]
    foreign = {**scope, "campaign_id": str(other)}
    scopes = sorted([scope, foreign], key=lambda s: (s["expires_at"], s["campaign_id"])) if document == "two_scopes" \
        else [foreign]
    _sql(db, f"UPDATE {EVENTS} SET retention_scopes = CAST(:d AS jsonb) WHERE id = :i",
         d=json.dumps({"version": "retention-scopes-v1", "scopes": scopes}), i=proof.event_id)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.CONFLICT, cl.RECORD_INCOHERENT)
    assert _proof(db, proof.proof_id) is not None


# --------------------------------------------------------------------------
# H. Atomicité de la purge exceptionnelle
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", [f"UPDATE {REGISTRY}", f"DELETE FROM {REGISTRY}", "RELEASE SAVEPOINT",
                                    "final_clock"])
def test_a_failure_after_the_resolution_the_delete_or_at_the_release_leaves_the_initial_state(
        db, clock, monkeypatch, prefix):
    proof = _certified(db)
    _expire(db, clock, proof.campaign_id)
    before, world = _proof(db, proof.proof_id), _world(db)
    if prefix == "final_clock":  # panne de l'horloge finale, lue après le DELETE dans le SAVEPOINT
        instants = iter([clock.now, None])

        def failing(session):
            value = next(instants)
            if value is None:
                raise RuntimeError("horloge finale indisponible")
            return value
        monkeypatch.setattr(proofs, "server_clock", failing)
        remove = lambda: None  # noqa: E731
    else:
        remove = _fail_on(db, prefix)
    try:
        outcome = purge_proof(db, proof.proof_id)
    finally:
        remove()
    assert outcome == cb.Outcome(cb.UNKNOWN, cb.TECHNICAL_UNCERTAINTY)  # aucun succès fictif
    assert _proof(db, proof.proof_id) == before and _world(db) == world  # aucun état terminal intermédiaire


def test_an_unexpected_row_count_or_resolution_rolls_back_the_whole_savepoint(db, clock, monkeypatch):
    proof = _certified(db)
    _expire(db, clock, proof.campaign_id)
    before = _proof(db, proof.proof_id)
    real = cl._affected
    monkeypatch.setattr(cl, "_affected", lambda session, statement, expected: real(session, statement, expected + 1))
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.CONFLICT, cl.WRITE_UNCONFIRMED)
    assert _proof(db, proof.proof_id) == before
    monkeypatch.setattr(cl, "_affected", real)
    monkeypatch.setattr(cl, "_db_row", lambda session, table, proof_id: None)  # résolution non vérifiée
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.CONFLICT, cl.WRITE_UNCONFIRMED)
    assert _proof(db, proof.proof_id) == before
    monkeypatch.undo()


def test_a_final_clock_behind_the_decision_rolls_back_the_resolution_and_the_delete(db, clock, monkeypatch):
    proof = _certified(db)
    _expire(db, clock, proof.campaign_id)
    before = _proof(db, proof.proof_id)
    instants = iter([clock.now, clock.now - MICRO])
    monkeypatch.setattr(proofs, "server_clock", lambda session: next(instants))
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.UNKNOWN, cl.CLOCK_INCOHERENT)
    assert _proof(db, proof.proof_id) == before


# --------------------------------------------------------------------------
# I-L. Purge définitive des campagnes (parcours C) : outils
# --------------------------------------------------------------------------

@pytest.fixture
def f2(engine):
    """Lignes F2 créées par les scénarios de cohorte (forme exacte des lignes
    du service F2, comme tests/test_product_analytics_cohort.py), supprimées
    après le test (lignes analytics d'abord, dans l'ordre des FK)."""
    from tests.test_activity_event_finalization import CLEANUP
    created = []
    yield created
    with engine.begin() as conn:
        for table in CLEANUP:
            conn.execute(sa.text(f"DELETE FROM {table}"))
        conn.execute(sa.text("DELETE FROM auth_operations WHERE invitation_id = ANY(:i)"),
                     {"i": [i for kind, i in created if kind == "invitation"]})
        conn.execute(sa.text("DELETE FROM invitations WHERE id = ANY(:i)"),
                     {"i": [i for kind, i in created if kind == "invitation"]})
        for kind, value in created:
            if kind == "user":
                conn.execute(sa.text(f"DELETE FROM {PARTICIPANTS} WHERE user_id = :u"), {"u": value})
                conn.execute(sa.text("DELETE FROM users WHERE id = :u"), {"u": value})


def _invitation(db, f2, *, status="pending"):
    invitation_id = uuid.uuid4()
    now = common.utcnow()
    with db.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO invitations (id, token_hash, hash_key_version, cohort, status, created_at, expires_at, "
            "revoked_at) VALUES (:id, :h, 'v1', 'wave-0', :s, :c, :e, :r)"),
            {"id": invitation_id, "h": hashlib.sha256(invitation_id.bytes).hexdigest(), "s": status,
             "c": now - timedelta(days=1), "e": now + timedelta(days=30),
             "r": now - timedelta(hours=1) if status == "revoked" else None})
    f2.append(("invitation", invitation_id))
    return invitation_id


def _activation(db, f2, invitation_id):
    """Activation F2 committed de l'invitation (comme
    F2AuthService.complete_invitation_activation)."""
    at = common.utcnow() - timedelta(minutes=5)
    user_id = f"v0-p4-tester-{uuid.uuid4().hex[:12]}"
    with db.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant')"), {"u": user_id})
        conn.execute(sa.text(
            "INSERT INTO auth_operations (id, kind, status, invitation_id, user_id, operation_schema_version, "
            "created_at, updated_at, expires_at, committed_at) VALUES (:id, 'invitation_activation', 'committed', "
            ":i, :u, 'v1', :c, :at, :x, :at)"),
            {"id": uuid.uuid4(), "i": invitation_id, "u": user_id, "c": at - timedelta(minutes=1), "at": at,
             "x": at + timedelta(minutes=10)})
        conn.execute(sa.text("UPDATE invitations SET status = 'accepted', accepted_at = :at WHERE id = :i"),
                     {"at": at, "i": invitation_id})
    f2.append(("user", user_id))
    return user_id


def _cohort_campaign(db, f2, *, seats=5, admission=timedelta(days=10), close_in=timedelta(days=30)):
    def action(s):
        campaign = pc.create_campaign(s, code=f"wave-{uuid.uuid4().hex[:10]}", name="Vague 0", purpose="Mesurer.",
                                      close_by_at=common.utcnow() + close_in, **POLICY)
        ch.define_cohort(s, campaign_id=campaign.id, cohort_policy_version="v0-cohort-v1",
                         admission_closes_at=common.utcnow() + admission)
        return campaign.id
    campaign_id = committed(db, action)
    invitations = [_invitation(db, f2) for _ in range(seats)]
    seat_ids = [committed(db, lambda s, i=i: ch.add_cohort_seat(s, campaign_id=campaign_id, invitation_id=i)[0].id)
                for i in invitations]
    return SimpleNamespace(id=campaign_id, invitations=invitations, seats=seat_ids, users={})


def _bind(db, f2, cohort, index, *, authorize=True):
    user_id = _activation(db, f2, cohort.invitations[index])
    committed(db, lambda s: ch.bind_seat_to_activated_invitation(s, campaign_id=cohort.id,
                                                                 seat_id=cohort.seats[index]))
    if authorize:
        committed(db, lambda s: ch.record_seat_authorization(s, campaign_id=cohort.id, user_id=user_id,
                                                             notice_version=NOTICE))
    cohort.users[index] = user_id
    return user_id


def _activate_cohort(db, cohort):
    committed(db, lambda s: ch.activate_cohort_campaign(s, campaign_id=cohort.id))


def _f2_world(db):
    return {table: rows(db, f"SELECT t.*, t.xmin::text AS xmin FROM {table} t ORDER BY t::text")
            for table in F2_TABLES}


def _expired_terminal(db, clock, campaign_id):
    """Campagne terminée (clôture si active, annulation si planned) puis
    échéance commune atteinte."""
    clock.advance(minutes=5)
    status = _campaign(db, campaign_id)["status"]
    if status == "active":
        _close(db, campaign_id)
    elif status == "planned":
        _cancel(db, campaign_id)
    return clock.set(_deadline(db, campaign_id))


def _purged(seats=0, cohorts=0):
    return cb.Outcome(cb.PURGED, None, None, seats, cohorts)


def _gone(db, campaign_id):
    return (_campaign(db, campaign_id), _count(db, COHORTS, campaign_id), _count(db, SEATS, campaign_id)) == (None, 0, 0)


# --------------------------------------------------------------------------
# I. Dépendances physiques (constatées, jamais résolues)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["pending", "certified", "recorded", "rejected", "indeterminate"])
def test_any_residual_proof_blocks_the_campaign_purge_and_is_never_touched(db, clock, state):
    proof = _in_state(db, clock, state)
    _expired_terminal(db, clock, proof.campaign_id)
    world = _world(db)
    seen, remove = _statements(db)
    try:
        assert purge_campaign(db, proof.campaign_id) == cb.Outcome(cb.BLOCKED, cl.PROOF_REMAINING)
    finally:
        remove()
    assert _world(db) == world
    assert not [s for s in seen if "FOR UPDATE" in s and CAMPAIGNS not in s]  # aucune dépendance verrouillée
    assert not [s for s in seen if "pg_advisory" in s or s.startswith(("UPDATE", "DELETE"))]


def test_each_physical_dependency_has_its_own_blocking_reason(db, clock):
    proof = _expired_recorded(db, clock)
    campaign_id = proof.campaign_id
    assert purge_campaign(db, campaign_id) == cb.Outcome(cb.BLOCKED, cl.PROOF_REMAINING)
    assert purge_proof(db, proof.proof_id) == PURGED  # PR 4 B
    assert purge_campaign(db, campaign_id) == cb.Outcome(cb.BLOCKED, cl.ATTRIBUTION_REMAINING)
    assert eb.expire_event(db, event_id=proof.event_id) == eb.EventOutcome(eb.PURGED)  # PR 2
    assert purge_campaign(db, campaign_id) == cb.Outcome(cb.BLOCKED, cl.SESSION_REMAINING)
    _sql(db, f"DELETE FROM {INTERACTIONS} WHERE session_id IS NOT NULL AND campaign_id = :c", c=campaign_id)
    _sql(db, f"DELETE FROM {SESSIONS} WHERE campaign_id = :c", c=campaign_id)  # écrivain hors service (test)
    _sql(db, f"INSERT INTO {INTERACTIONS} (campaign_id, user_id, interaction_key, interaction_kind, "
             "environment_digest, session_id, recorded_at) SELECT campaign_id, user_id, gen_random_uuid(), 'close', "
             f"repeat('c', 64), NULL, enrolled_at FROM {PARTICIPANTS} WHERE campaign_id = :c", c=campaign_id)
    assert purge_campaign(db, campaign_id) == cb.Outcome(cb.BLOCKED, cl.INTERACTION_REMAINING)  # clôture sans visite
    _sql(db, f"DELETE FROM {INTERACTIONS} WHERE campaign_id = :c", c=campaign_id)
    assert purge_campaign(db, campaign_id) == cb.Outcome(cb.BLOCKED, cl.PARTICIPATION_REMAINING)
    assert pb.purge_participation(db, campaign_id=campaign_id, user_id=USER_A).outcome == pb.PURGED  # PR 3
    assert purge_campaign(db, campaign_id) == _purged()
    assert _gone(db, campaign_id)


# --------------------------------------------------------------------------
# J. Dépendances logiques globales (R5 bis, tous les utilisateurs)
# --------------------------------------------------------------------------

def _empty_expired(db, clock):
    """Campagne annulée sans participation, échéance atteinte."""
    campaign_id = _create(db)
    clock.advance(minutes=1)
    _cancel(db, campaign_id)
    return campaign_id


def _outsider_event(db, clock):
    """Événement 2A réel de USER_B, rattaché à SA campagne B seulement
    (USER_B n'a jamais participé à la campagne traitée)."""
    b = _active(db)
    _enroll(db, b, USER_B)
    event = _record(db, USER_B, event_type="coach_used", surface="coach", source="client_signal")
    [row] = rows(db, f"SELECT retention_scopes FROM {EVENTS} WHERE id = :i", i=event.id)
    return b, event, row["retention_scopes"]


def _forge(db, event_id, document):
    _sql(db, f"UPDATE {EVENTS} SET retention_scopes = CAST(:d AS jsonb) WHERE id = :i", i=event_id,
         d=json.dumps(document))


@pytest.mark.parametrize("form", sorted(R5_FORMS))
def test_r5_bis_is_applied_globally_to_events_of_users_outside_the_campaign(db, clock, form):
    a = _empty_expired(db, clock)
    _, event, document = _outsider_event(db, clock)
    [scope] = document["scopes"]
    expression, expected = R5_FORMS[form]
    item = eval(expression.format(scope="scope").replace("{{", "{").replace("}}", "}"),  # noqa: S307 — test
                {"scope": scope, "A": str(a), "B": scope["campaign_id"]})
    _forge(db, event.id, {"version": "retention-scopes-v1", "scopes": [item]})
    clock.set(_deadline(db, a))
    before = rows(db, f"SELECT *, xmin::text AS xmin FROM {EVENTS}")
    outcome = purge_campaign(db, a)
    if expected == "purged":
        assert outcome == _purged(), (form, outcome)
        assert _gone(db, a)
    else:
        assert outcome == cb.Outcome(cb.CONFLICT, cl.RETENTION_INCOHERENT), (form, outcome)
        assert _campaign(db, a) is not None
    assert rows(db, f"SELECT *, xmin::text AS xmin FROM {EVENTS}") == before  # jamais réparé ni supprimé


@pytest.mark.parametrize("scopes, expected", [("a", "blocked"), ("a_and_b", "blocked"), ("b", "purged")])
def test_a_canonical_scope_of_the_campaign_held_by_an_outsider_blocks(db, clock, scopes, expected):
    a = _empty_expired(db, clock)
    _, event, document = _outsider_event(db, clock)
    [scope] = document["scopes"]
    own = {**scope, "campaign_id": str(a)}
    items = {"a": [own], "a_and_b": sorted([own, scope], key=lambda s: (s["expires_at"], s["campaign_id"])),
             "b": [scope]}[scopes]
    _forge(db, event.id, {"version": "retention-scopes-v1", "scopes": items})
    clock.set(_deadline(db, a))
    outcome = purge_campaign(db, a)
    if expected == "blocked":
        assert outcome == cb.Outcome(cb.BLOCKED, cl.EVENT_SCOPE_REMAINING)
    else:
        assert outcome == _purged()


def test_an_incoherent_document_prevails_over_a_mere_residual_scope_whatever_the_order(db, clock):
    a = _empty_expired(db, clock)
    _, first, document = _outsider_event(db, clock)
    _, second, _ = _outsider_event(db, clock)
    [scope] = document["scopes"]
    _forge(db, first.id, {"version": "retention-scopes-v1", "scopes": [{**scope, "campaign_id": str(a)}]})
    _forge(db, second.id, {"version": "retention-scopes-v1", "scopes": [{**scope, "kind": "user"}]})
    clock.set(_deadline(db, a))
    assert purge_campaign(db, a) == cb.Outcome(cb.CONFLICT, cl.RETENTION_INCOHERENT)


def test_an_independent_anomaly_of_another_campaign_never_blocks_and_pr3_classification_is_shared(db, clock):
    a = _empty_expired(db, clock)
    b, event, document = _outsider_event(db, clock)
    [scope] = document["scopes"]
    for anomaly in ({**scope, "policy_version": "v9"}, {**scope, "expires_at": "demain"}):
        _forge(db, event.id, {"version": "retention-scopes-v1", "scopes": [anomaly]})
        for target in (a, b):
            assert retention.campaign_reference({"version": "retention-scopes-v1", "scopes": [anomaly]}, target) == (
                retention.REFERENCE_ABSENT if target == a else retention.REFERENCE_INCOHERENT)
    clock.set(_deadline(db, a))
    assert purge_campaign(db, a) == _purged()


def test_the_r5_bis_extraction_is_exactly_the_pr3_classification():
    """Équivalence de la classification extraite (retention.campaign_reference)
    et de la lecture d'origine de la PR 3, sur toutes les formes couvertes par
    la PR #257 et sur les formes d'enveloppe."""
    a, b = uuid.uuid4(), uuid.uuid4()
    scope = {"kind": "campaign", "campaign_id": str(b), "policy_version": "v0-retention-v1",
             "expires_at": "2027-01-01T00:00:00.000000Z"}

    def original(document, campaign_id):  # code de participation_lifecycle avant l'extraction (PR #257)
        try:
            scopes = retention.parse_retention_scopes(document)
        except pl._STRUCTURE_ERRORS:
            named = retention.lenient_campaigns(document)
            return "incoherent" if named is None or campaign_id in named else "absent"
        return "present" if any(s.campaign_id == campaign_id for s in scopes) else "absent"
    documents = []
    for expression, _ in R5_FORMS.values():
        item = eval(expression.format(scope="scope").replace("{{", "{").replace("}}", "}"),  # noqa: S307 — test
                    {"scope": scope, "A": str(a), "B": str(b)})
        documents.append({"version": "retention-scopes-v1", "scopes": [item]})
    documents += [{"version": "retention-scopes-v1", "scopes": [{**scope, "campaign_id": str(a)}]}, None, [], "x",
                  {"version": "retention-scopes-v2", "scopes": [scope]}, {"version": "retention-scopes-v1"},
                  {"version": "retention-scopes-v1", "scopes": []}, {"version": "retention-scopes-v1",
                                                                       "scopes": {"0": scope}}]
    for document in documents:
        for campaign_id in (a, b):
            assert retention.campaign_reference(document, campaign_id) == original(document, campaign_id), document
    assert pl._lenient_campaigns is retention.lenient_campaigns
    # Documents altérés aléatoirement (graine fixe) : même classification que la formulation d'origine.
    import random
    rng = random.Random(20261128)
    values = {"kind": ["campaign", "Campaign", "user", 3, None, ["campaign"]],
              "campaign_id": [str(a), str(b), str(a).upper(), str(b).replace("-", ""), "{" + str(a) + "}",
                              "urn:uuid:" + str(b), 7, None, "", str(a)[:-1]],
              "policy_version": ["v0-retention-v1", "v9", 1, None],
              "expires_at": ["2027-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.000000Z", "demain", 0, None,
                             "2027-02-30T00:00:00.000000Z"]}
    for _ in range(4000):
        items = []
        for _ in range(rng.choice([1, 1, 2, 3])):
            item = {key: rng.choice(choices) for key, choices in values.items()}
            if rng.random() < 0.1:
                item.pop(rng.choice(list(item)))
            if rng.random() < 0.05:
                item["extra"] = str(a)
            items.append(item)
        if rng.random() < 0.5:
            items.sort(key=lambda i: (str(i.get("expires_at")), str(i.get("campaign_id"))))
        document = {"version": rng.choice(["retention-scopes-v1"] * 8 + ["retention-scopes-v2", 1]),
                    "scopes": items if rng.random() < 0.95 else {"0": items}}
        for campaign_id in (a, b):
            assert retention.campaign_reference(document, campaign_id) == original(document, campaign_id), document


# --------------------------------------------------------------------------
# K. Cohortes et places : invariants universels, jamais l'activation
# --------------------------------------------------------------------------

def test_a_campaign_without_cohort_or_with_an_empty_or_incomplete_cohort_is_purged(db, clock, f2):
    plain = _empty_expired(db, clock)
    empty = _cohort_campaign(db, f2, seats=0)
    partial = _cohort_campaign(db, f2, seats=2)
    _bind(db, f2, partial, 0, authorize=False)  # place rattachée sans autorisation
    f2_before = None
    for cohort in (empty, partial):
        _expired_terminal(db, clock, cohort.id)  # annulées avant activation : cohortes incomplètes légitimes
    clock.set(max(_deadline(db, c) for c in (plain, empty.id, partial.id)))
    f2_before = _f2_world(db)
    assert purge_campaign(db, plain) == _purged()
    assert purge_campaign(db, empty.id) == _purged(0, 1)
    assert purge_campaign(db, partial.id) == _purged(2, 1)
    assert all(_gone(db, c) for c in (plain, empty.id, partial.id))
    assert _f2_world(db) == f2_before  # invitations, opérations et utilisateurs intacts (xmin)


def test_an_activated_cohort_with_unbound_unauthorized_and_unadmitted_seats_is_purged(db, clock, f2):
    cohort = _cohort_campaign(db, f2)
    _activate_cohort(db, cohort)
    clock.advance(minutes=10)
    _bind(db, f2, cohort, 0)  # autorisée, admise puis purgée par la PR 3
    _bind(db, f2, cohort, 1)  # autorisée, jamais admise
    _bind(db, f2, cohort, 2, authorize=False)  # rattachée sans autorisation
    committed(db, lambda s: ch.admit_at_first_eligible_login(s, campaign_id=cohort.id, user_id=cohort.users[0]))
    _sql(db, "UPDATE invitations SET status = 'revoked', revoked_at = :t WHERE id = :i", t=clock.now,
         i=cohort.invitations[3])  # révoquée après la sélection
    _sql(db, "UPDATE invitations SET expires_at = :t WHERE id = :i", t=clock.now, i=cohort.invitations[4])  # expirée
    _expired_terminal(db, clock, cohort.id)
    assert purge_campaign(db, cohort.id) == cb.Outcome(cb.BLOCKED, cl.PARTICIPATION_REMAINING)
    assert pb.purge_participation(db, campaign_id=cohort.id, user_id=cohort.users[0]).outcome == pb.PURGED
    f2_before = _f2_world(db)
    assert purge_campaign(db, cohort.id) == _purged(5, 1)
    assert _gone(db, cohort.id) and _f2_world(db) == f2_before


def test_historically_permitted_late_writes_never_block_nor_extend_the_purge(db, clock, f2):
    """Place ajoutée après close_by_at sur une campagne planned, terminée
    ensuite par la PR 4 (closed_at = close_by_at < created_at de la place) ;
    marque tardive d'échec d'admission (L10)."""
    late = _cohort_campaign(db, f2, seats=1, admission=timedelta(hours=1), close_in=timedelta(days=13))
    clock.set(utc(_campaign(db, late.id)["close_by_at"]) + timedelta(days=1))
    extra = _invitation(db, f2)
    committed(db, lambda s: ch.add_cohort_seat(s, campaign_id=late.id, invitation_id=extra))  # historiquement permis
    assert terminate(db, late.id) == CANCELLED
    seat_created = max(utc(r["created_at"]) for r in rows(db, f"SELECT created_at FROM {SEATS} WHERE campaign_id = :c",
                                                          c=late.id))
    assert seat_created > utc(_campaign(db, late.id)["closed_at"])
    marked = _cohort_campaign(db, f2, seats=5)
    _activate_cohort(db, marked)
    clock.advance(minutes=10)
    user = _bind(db, f2, marked, 0)
    _expired_terminal(db, clock, marked.id)
    committed(db, lambda s: ch._mark_admission_failure(s, marked.id, user))  # après clôture et échéance
    assert rows(db, f"SELECT admission_failure_first_at FROM {SEATS} WHERE user_id = :u", u=user)[0][
        "admission_failure_first_at"] is not None
    deadline_late = _deadline(db, late.id)
    assert deadline_late == utc(_campaign(db, late.id)["close_by_at"]) + HOURS_2160  # jamais prolongée
    clock.set(max(deadline_late, _deadline(db, marked.id)))
    assert purge_campaign(db, late.id) == _purged(2, 1)
    assert purge_campaign(db, marked.id) == _purged(5, 1)


SEAT_FORGERIES = {
    "number_gap": (f"UPDATE {SEATS} SET seat_number = 5 WHERE campaign_id = :c AND seat_number = 2", "seat_incoherent"),
    "created_before_cohort": (f"UPDATE {SEATS} SET created_at = created_at - interval '1 day' WHERE campaign_id = :c "
                              "AND seat_number = 2", "seat_incoherent"),
    "bound_at_deadline": (f"UPDATE {SEATS} s SET bound_at = c.admission_closes_at, authorization_recorded_at = "
                          f"c.admission_closes_at FROM {COHORTS} c WHERE c.campaign_id = s.campaign_id AND "
                          "s.campaign_id = :c AND s.seat_number = 1", "seat_incoherent"),
    "notice": (f"UPDATE {SEATS} SET notice_version = 'Notice libre!' WHERE campaign_id = :c AND seat_number = 1",
               "seat_incoherent"),
    "cohort_policy": (f"UPDATE {COHORTS} SET cohort_policy_version = 'v9' WHERE campaign_id = :c",
                      "cohort_incoherent"),
    "cohort_after_close_by": (f"UPDATE {COHORTS} c SET admission_closes_at = p.close_by_at + interval '1 second' "
                              f"FROM {CAMPAIGNS} p WHERE p.id = c.campaign_id AND c.campaign_id = :c",
                              "cohort_incoherent"),
}


@pytest.mark.parametrize("forgery", sorted(SEAT_FORGERIES))
def test_a_broken_universal_invariant_of_the_cohort_or_its_seats_is_a_conflict(db, clock, f2, forgery):
    cohort = _cohort_campaign(db, f2, seats=2)
    _bind(db, f2, cohort, 0)
    statement, reason = SEAT_FORGERIES[forgery]
    _sql(db, statement, c=cohort.id)
    _expired_terminal(db, clock, cohort.id)
    world = _world(db)
    assert purge_campaign(db, cohort.id) == cb.Outcome(cb.CONFLICT, reason)
    assert _world(db) == world


def test_an_activated_cohort_that_became_incomplete_is_reported_and_kept(db, clock, f2):
    cohort = _cohort_campaign(db, f2)
    _activate_cohort(db, cohort)
    _sql(db, f"DELETE FROM {SEATS} WHERE campaign_id = :c AND seat_number = 5", c=cohort.id)  # hors service
    _expired_terminal(db, clock, cohort.id)
    assert purge_campaign(db, cohort.id) == cb.Outcome(cb.CONFLICT, cl.COHORT_INCOHERENT)
    assert _count(db, SEATS, cohort.id) == 4


def test_a_seat_instant_after_the_clock_is_unknown_and_never_corrected(db, clock, f2):
    cohort = _cohort_campaign(db, f2, seats=1)
    deadline = _expired_terminal(db, clock, cohort.id)
    _sql(db, f"UPDATE {SEATS} SET created_at = :t WHERE campaign_id = :c", t=deadline + timedelta(days=1), c=cohort.id)
    world = _world(db)
    assert purge_campaign(db, cohort.id) == cb.Outcome(cb.UNKNOWN, cl.CLOCK_INCOHERENT)
    assert _world(db) == world


# --------------------------------------------------------------------------
# L. Atomicité de la purge de campagne
# --------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", [f"DELETE FROM {SEATS}", f"DELETE FROM {COHORTS}", f"DELETE FROM {CAMPAIGNS}",
                                    "RELEASE SAVEPOINT"])
def test_a_failure_after_any_delete_or_at_the_release_leaves_nothing_deleted(db, clock, f2, prefix):
    cohort = _cohort_campaign(db, f2, seats=3)
    other = _cohort_campaign(db, f2, seats=1)
    _expired_terminal(db, clock, cohort.id)
    world = _world(db)
    remove = _fail_on(db, prefix)
    try:
        assert purge_campaign(db, cohort.id) == cb.Outcome(cb.UNKNOWN, cb.TECHNICAL_UNCERTAINTY)
    finally:
        remove()
    assert _world(db) == world  # xmin compris : rollback complet, autres campagnes intactes
    assert purge_campaign(db, cohort.id) == _purged(3, 1)
    assert _campaign(db, other.id) is not None and _count(db, SEATS, other.id) == 1


def test_an_unexpected_row_count_or_a_foreign_key_violation_rolls_back_everything(db, clock, f2, monkeypatch):
    cohort = _cohort_campaign(db, f2, seats=2)
    _expired_terminal(db, clock, cohort.id)
    world = _world(db)
    real = cl._affected
    calls = []

    def shifted(session, statement, expected):
        calls.append(expected)
        return real(session, statement, expected + (1 if len(calls) == 2 else 0))  # cohorte
    monkeypatch.setattr(cl, "_affected", shifted)
    assert purge_campaign(db, cohort.id) == cb.Outcome(cb.CONFLICT, cl.WRITE_UNCONFIRMED)
    assert _world(db) == world
    monkeypatch.setattr(cl, "_affected", real)
    # Référence physique apparue sans être vue par la relecture finale (contrôle neutralisé) : la FK refuse.
    _sql(db, f"INSERT INTO {PARTICIPANTS} (campaign_id, user_id, window_start_at, window_end_at, participation_status, "
             "enrolled_at, authorization_recorded_at, notice_version) SELECT id, :u, created_at, close_by_at, "
             f"'enrolled', created_at, created_at, 'v0-notice-1' FROM {CAMPAIGNS} WHERE id = :c", u=USER_A, c=cohort.id)
    real_dep = cl._dependency_problem
    monkeypatch.setattr(cl, "_dependency_problem", lambda session, campaign_id, budget: (None, 0))
    world = _world(db)
    assert purge_campaign(db, cohort.id) == cb.Outcome(cb.CONFLICT, cl.DEPENDENCY_CHANGED)
    assert _world(db) == world
    monkeypatch.setattr(cl, "_dependency_problem", real_dep)
    assert purge_campaign(db, cohort.id) == cb.Outcome(cb.BLOCKED, cl.PARTICIPATION_REMAINING)


# --------------------------------------------------------------------------
# M. Concurrence (PostgreSQL réel, deux ordres de passage)
# --------------------------------------------------------------------------

def _behind(db, holder, contender_fn, *, commit=True, count=1, message="attente attendue"):
    """`contender_fn` démarre pendant que `holder` garde ses verrous ; il doit
    attendre, puis décider après le COMMIT (ou le ROLLBACK) du détenteur."""
    contender = Background(contender_fn)
    contender.start()
    try:
        _wait(lambda: waiting(db) >= count, message)
    except BaseException:
        holder.rollback()  # jamais un détenteur oublié (le nettoyage attendrait ses verrous)
        contender.join(30)
        raise
    holder.commit() if commit else holder.rollback()
    return contender.outcome()


def _refusal(action):
    try:
        action()
    except common.ProductAnalyticsError as exc:
        return type(exc).__name__, getattr(exc, "reason", None)
    return None


def test_two_concurrent_terminations_write_once(db, clock):
    campaign_id = _active(db)
    clock.set(utc(_campaign(db, campaign_id)["close_by_at"]))
    holder = Holder(db, lambda s: cl.terminate_due_campaign(s, campaign_id=campaign_id))
    assert holder.value.outcome == cl.CLOSED_OUTCOME
    assert _behind(db, holder, lambda: terminate(db, campaign_id)) == ALREADY
    holder = Holder(db, lambda s: cl.terminate_due_campaign(s, campaign_id=campaign_id))
    assert holder.value.outcome == cl.ALREADY_TERMINAL
    holder.rollback()


@pytest.mark.parametrize("transition", ["cancel", "close"])
def test_a_historical_transition_and_the_termination_are_serialized_in_both_orders(db, clock, transition):
    campaign_id = _active(db)
    clock.set(utc(_campaign(db, campaign_id)["close_by_at"]) + timedelta(days=1))
    writer = pc.cancel_campaign if transition == "cancel" else pc.close_campaign
    holder = Holder(db, lambda s: writer(s, campaign_id=campaign_id))
    assert _behind(db, holder, lambda: terminate(db, campaign_id)) == ALREADY
    other = _active(db)
    clock.set(max(clock.now, utc(_campaign(db, other)["close_by_at"])))
    holder = Holder(db, lambda s: cl.terminate_due_campaign(s, campaign_id=other))
    refused = _behind(db, holder, lambda: _refusal(lambda: committed(db, lambda s: writer(s, campaign_id=other))))
    assert refused == ("CampaignTransitionRefused", None)  # aucune réouverture, closed_at historique conservé
    assert utc(_campaign(db, other)["closed_at"]) == utc(_campaign(db, other)["close_by_at"])


def test_activation_policy_revision_and_seat_addition_are_serialized_with_the_termination(db, clock, f2):
    planned = _create(db)
    clock.set(utc(_campaign(db, planned)["close_by_at"]) - timedelta(seconds=1))
    holder = Holder(db, lambda s: pc.activate_campaign(s, campaign_id=planned))
    clock.advance(seconds=2)  # l'échéance est franchie pendant l'attente
    assert _behind(db, holder, lambda: terminate(db, planned)) == CLOSED  # campagne active à l'échéance
    revised = _create(db)
    clock.set(utc(_campaign(db, revised)["close_by_at"]))
    holder = Holder(db, lambda s: pc.revise_campaign_policies(s, campaign_id=revised, **POLICY))
    assert _behind(db, holder, lambda: terminate(db, revised)) == CANCELLED
    cohort = _cohort_campaign(db, f2, seats=1)
    clock.set(utc(_campaign(db, cohort.id)["close_by_at"]))
    invitation = _invitation(db, f2)
    holder = Holder(db, lambda s: cl.terminate_due_campaign(s, campaign_id=cohort.id))
    refused = _behind(db, holder, lambda: _refusal(lambda: committed(
        db, lambda s: ch.add_cohort_seat(s, campaign_id=cohort.id, invitation_id=invitation))))
    assert refused == ("CohortRefused", "campaign_not_planned")
    assert _refusal(lambda: committed(db, lambda s: pc.revise_campaign_policies(
        s, campaign_id=cohort.id, **POLICY))) == ("CampaignPolicyLocked", None)


def test_an_admission_and_the_termination_are_serialized_in_both_orders(db, clock, f2):
    cohort = _cohort_campaign(db, f2)
    _activate_cohort(db, cohort)
    clock.advance(minutes=10)
    first, second = _bind(db, f2, cohort, 0), _bind(db, f2, cohort, 1)
    holder = Holder(db, lambda s: ch.admit_at_first_eligible_login(s, campaign_id=cohort.id, user_id=first))
    clock.set(utc(_campaign(db, cohort.id)["close_by_at"]))
    assert _behind(db, holder, lambda: terminate(db, cohort.id)) == CLOSED
    assert _count(db, PARTICIPANTS, cohort.id) == 1
    other = _cohort_campaign(db, f2)
    _activate_cohort(db, other)
    holder = Holder(db, lambda s: cl.terminate_due_campaign(s, campaign_id=other.id))
    clock.set(max(clock.now, utc(_campaign(db, other.id)["close_by_at"])))
    holder.rollback()
    del second


def test_two_exceptional_purges_of_the_same_proof_delete_once(db, clock):
    proof = _certified(db)
    _expire(db, clock, proof.campaign_id)
    holder = Holder(db, lambda s: cl.purge_campaign_expired_proof(s, proof_id=proof.proof_id))
    assert holder.value.outcome == cl.RESOLVED_AND_PURGED
    assert _behind(db, holder, lambda: purge_proof(db, proof.proof_id)) == ABSENT
    holder_rolled = None
    del holder_rolled


def test_the_pr1_nominal_purge_and_the_exceptional_purge_are_serialized_in_both_orders(db, clock):
    for first in ("pr1", "pr4"):
        proof = _in_state(db, clock, "indeterminate")
        _expire(db, clock, proof.campaign_id)
        clock.advance(hours=200)  # 168 heures écoulées aussi
        if first == "pr1":
            holder = Holder(db, lambda s: prl.purge_resolved_proof(s, proof_id=proof.proof_id))
            assert holder.value.outcome == prl.PURGED
            assert _behind(db, holder, lambda: purge_proof(db, proof.proof_id)) == ABSENT
        else:
            holder = Holder(db, lambda s: cl.purge_campaign_expired_proof(s, proof_id=proof.proof_id))
            assert holder.value.outcome == cl.PURGED
            assert _behind(db, holder, lambda: prb.purge_proof(db, proof_id=proof.proof_id)).outcome == prb.ABSENT


def test_the_block_c_and_the_exceptional_purge_are_serialized_and_a_prior_transition_is_reevaluated(db, clock):
    proof = _certified(db)
    clock.set(utc(_campaign(db, proof.campaign_id)["close_by_at"]) + HOURS_2160)  # campagne active échue
    holder = Holder(db, lambda s: fz.finalize_certified_proof(s, proof_id=proof.proof_id))
    assert holder.value.state == fz.INDETERMINATE  # bloc C : finalization_expired
    assert _behind(db, holder, lambda: purge_proof(db, proof.proof_id)) == PURGED  # transition vue en L1
    pending = _pending(db)
    clock.set(max(clock.now, utc(_campaign(db, pending.campaign_id)["close_by_at"]) + HOURS_2160))
    holder = Holder(db, lambda s: cl.purge_campaign_expired_proof(s, proof_id=pending.proof_id))
    assert holder.value.outcome == cl.RESOLVED_AND_PURGED
    assert _behind(db, holder, lambda: expire(db, pending.proof_id)).state == fz.ABSENT


def test_the_pr2_event_purge_and_the_exceptional_purge_are_serialized_in_both_orders(db, clock):
    proof = _expired_recorded(db, clock)
    holder = Holder(db, lambda s: el.expire_event_scopes(s, event_id=proof.event_id))
    assert holder.value.outcome == el.PURGED
    assert _behind(db, holder, lambda: purge_proof(db, proof.proof_id)) == ABSENT
    other = _expired_recorded(db, clock)
    holder = Holder(db, lambda s: cl.purge_campaign_expired_proof(s, proof_id=other.proof_id))
    assert holder.value.outcome == cl.PURGED
    assert _behind(db, holder, lambda: eb.expire_event(db, event_id=other.event_id)) == eb.EventOutcome(eb.PURGED)
    assert _count(db, EVENTS) == 0 and _count(db, REGISTRY) == 0


def test_the_pr3_participation_purge_waits_for_the_exceptional_purge_then_proceeds(db, clock):
    proof = _pending(db)
    _expire(db, clock, proof.campaign_id)
    holder = Holder(db, lambda s: cl.purge_campaign_expired_proof(s, proof_id=proof.proof_id))
    assert holder.value.outcome == cl.RESOLVED_AND_PURGED
    outcome = _behind(db, holder, lambda: pb.purge_participation(db, campaign_id=proof.campaign_id, user_id=USER_A))
    assert outcome.outcome == pb.PURGED  # aucune preuve après l'attente
    blocked = _pending(db)
    _expire(db, clock, blocked.campaign_id)
    holder = Holder(db, lambda s: pl.purge_participation(s, campaign_id=blocked.campaign_id, user_id=USER_A))
    assert holder.value.outcome == pl.BLOCKED
    holder.commit()
    assert purge_proof(db, blocked.proof_id).outcome == cb.RESOLVED_AND_PURGED


def test_a_visit_finalization_and_a_termination_never_deadlock_with_the_exceptional_purge(db, clock):
    proof = _pending(db)
    clock.set(utc(_campaign(db, proof.campaign_id)["close_by_at"]) + HOURS_2160)  # active, échue
    holder = Holder(db, lambda s: cl.purge_campaign_expired_proof(s, proof_id=proof.proof_id))
    assert holder.value.outcome == cl.RESOLVED_AND_PURGED
    assert _behind(db, holder, lambda: terminate(db, proof.campaign_id)) == CLOSED  # campagne FOR SHARE tenue
    visit = rows(db, f"SELECT id FROM {SESSIONS} WHERE campaign_id = :c", c=proof.campaign_id)[0]["id"]
    assert pb.finalize_session(db, session_id=visit).outcome == pb.NOT_ADMISSIBLE  # conservation échue
    other = _pending(db)
    clock.set(max(clock.now, utc(_campaign(db, other.campaign_id)["close_by_at"]) + HOURS_2160))
    holder = Holder(db, lambda s: cl.terminate_due_campaign(s, campaign_id=other.campaign_id))
    assert _behind(db, holder, lambda: purge_proof(db, other.proof_id)).outcome == cb.RESOLVED_AND_PURGED


def test_the_campaign_purge_and_the_exceptional_purge_never_wait_on_each_other_cyclically(db, clock):
    proof = _pending(db)
    _expire(db, clock, proof.campaign_id)
    holder = Holder(db, lambda s: cl.purge_campaign_expired_proof(s, proof_id=proof.proof_id))
    assert _behind(db, holder, lambda: purge_campaign(db, proof.campaign_id)) == cb.Outcome(
        cb.BLOCKED, cl.SESSION_REMAINING)  # la preuve a disparu ; la participation reste (PR 3)
    other = _pending(db)
    _expire(db, clock, other.campaign_id)
    holder = Holder(db, lambda s: cl.purge_campaign(s, campaign_id=other.campaign_id))
    assert holder.value.reason == cl.PROOF_REMAINING
    assert _behind(db, holder, lambda: purge_proof(db, other.proof_id)).outcome == cb.RESOLVED_AND_PURGED


def _identity_scenario(db, clock):
    proof = _certified(db)
    _expire(db, clock, proof.campaign_id)
    return proof


def test_an_identity_changed_between_l0_and_l1_is_a_conflict_never_an_absence(db, clock, monkeypatch):
    proof = _identity_scenario(db, clock)
    held = cl._required_keys(committed(db, lambda s: cl._proof_state(s, proof.proof_id)))  # clés de L0
    real, calls = cl._proof_state, []

    def changing(session, proof_id):
        calls.append(1)
        if len(calls) == 2:  # après L0 et les clés : écrivain hors service
            _sql(db, f"UPDATE {REGISTRY} SET source_operation_id = :o WHERE id = :i", o=uuid.uuid4(), i=proof_id)
        return real(session, proof_id)
    monkeypatch.setattr(cl, "_proof_state", changing)
    seen, remove = _statements(db)
    try:
        assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.CONFLICT, cl.IDENTITY_CHANGED)
    finally:
        remove()
    advisory = [i for i, s in enumerate(seen) if "pg_advisory" in s]
    assert len(advisory) == len(held) > 1  # un seul appel trié (une instruction par clé de L0), aucune clé tardive
    assert not [s for s in seen if " FOR " in s]  # refus en L1, avant tout verrou de ligne
    assert _proof(db, proof.proof_id) is not None


def test_a_dependency_changed_after_l1_or_a_proof_gone_during_the_locks(db, clock, monkeypatch):
    proof = _identity_scenario(db, clock)
    real = pl._participation

    def after_l1(session, campaign_id, user_id, *, mode=None):
        _sql(db, f"UPDATE {REGISTRY} SET created_at = created_at - interval '1 microsecond' WHERE id = :i",
             i=proof.proof_id)
        return real(session, campaign_id, user_id, mode=mode)
    monkeypatch.setattr(pl, "_participation", after_l1)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.CONFLICT, cl.DEPENDENCY_CHANGED)
    monkeypatch.setattr(pl, "_participation", real)
    other = _identity_scenario(db, clock)

    def gone(session, campaign_id, user_id, *, mode=None):
        _sql(db, f"DELETE FROM {REGISTRY} WHERE id = :i", i=other.proof_id)
        return real(session, campaign_id, user_id, mode=mode)
    monkeypatch.setattr(pl, "_participation", gone)
    assert purge_proof(db, other.proof_id) == ABSENT


def test_a_recorded_proof_needing_a_key_not_held_is_refused(db, clock, monkeypatch):
    proof = _expired_recorded(db, clock)
    real, calls = cl._required_keys, []

    def growing(state):
        calls.append(1)
        keys = real(state)
        return keys | {12345} if len(calls) == 2 else keys  # L1 : nouvelle clé nécessaire
    monkeypatch.setattr(cl, "_required_keys", growing)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.CONFLICT, cl.DEPENDENCY_CHANGED)
    monkeypatch.setattr(cl, "_required_keys", real)
    expected = set(fz.lock_keys(SimpleNamespace(**{k: v for k, v in _proof(db, proof.proof_id).items()
                                                   if k != "xmin"})))
    event = committed(db, lambda s: s.get(el.ProductActivityEvent, proof.event_id))
    assert cl._required_keys(committed(db, lambda s: cl._proof_state(s, proof.proof_id))) == expected | set(
        el._event_lock_keys(event))  # union exacte des conventions du bloc C et de la PR 2


@pytest.mark.parametrize("order", ["mark_first", "purge_first", "timeout"])
def test_mark_admission_failure_and_the_campaign_purge_in_every_order(db, clock, f2, order):
    cohort = _cohort_campaign(db, f2)
    _activate_cohort(db, cohort)
    clock.advance(minutes=10)
    user = _bind(db, f2, cohort, 0)
    _expired_terminal(db, clock, cohort.id)
    if order == "mark_first":
        holder = Holder(db, lambda s: ch._mark_admission_failure(s, cohort.id, user))
        assert _behind(db, holder, lambda: purge_campaign(db, cohort.id)) == _purged(5, 1)  # état effectif relu
    elif order == "purge_first":
        holder = Holder(db, lambda s: cl.purge_campaign(s, campaign_id=cohort.id))
        assert holder.value.outcome == cl.PURGED
        marker = Background(lambda: committed(db, lambda s: ch._mark_admission_failure(s, cohort.id, user)))
        marker.start()
        _wait(lambda: waiting(db) >= 1, "la marque attend la place")
        holder.commit()
        marker.outcome()
        assert _gone(db, cohort.id) and _count(db, SEATS) == 0  # constate l'absence, ne recrée rien
    else:
        holder = Holder(db, lambda s: s.execute(sa.text(f"SELECT 1 FROM {SEATS} WHERE user_id = :u FOR UPDATE"),
                                                {"u": user}))
        world = _world(db)
        try:
            assert purge_campaign(db, cohort.id) == cb.Outcome(cb.UNKNOWN, cb.TECHNICAL_UNCERTAINTY)  # 2 s
        finally:
            holder.rollback()
        assert _world(db) == world  # aucune suppression partielle
        assert purge_campaign(db, cohort.id) == _purged(5, 1)


def test_implicit_foreign_keys_an_outside_insert_waits_for_the_campaign_purge_then_fails(db, clock):
    campaign_id = _empty_expired(db, clock)
    clock.set(_deadline(db, campaign_id))
    holder = Holder(db, lambda s: cl.purge_campaign(s, campaign_id=campaign_id))
    if holder.value.outcome != cl.PURGED:
        holder.rollback()
        raise AssertionError(holder.value)

    def outside():
        try:
            _sql(db, f"INSERT INTO {COHORTS} (campaign_id, cohort_policy_version, seat_count, admission_closes_at, "
                     "created_at) VALUES (:c, 'v0-cohort-v1', 5, now() + interval '1 day', now())", c=campaign_id)
        except sa.exc.IntegrityError as exc:
            return "fk" if "product_campaign_cohorts_campaign_id_fkey" in str(exc) else str(exc)
        return "inserted"
    assert _behind(db, holder, outside) == "fk"  # FOR KEY SHARE de l'INSERT attend le verrou exclusif
    assert _gone(db, campaign_id)


def test_a_new_campaign_with_the_same_code_is_a_distinct_identity_and_old_uuids_stay_refused(db, clock):
    code = f"wave-{uuid.uuid4().hex[:10]}"
    old = _create(db, code=code)
    _cancel(db, old)
    clock.set(_deadline(db, old))
    holder = Holder(db, lambda s: cl.purge_campaign(s, campaign_id=old))
    assert _refusal(lambda: committed(db, lambda s: pc.create_campaign(
        s, code=code, name="Vague 1", purpose="Nouvelle.", close_by_at=clock.now + timedelta(days=30), **POLICY))) == (
        "CampaignCodeConflict", None)  # aucune clé de code prise par la purge : refus historique, pas d'attente
    holder.commit()
    new = _create(db, code=code)
    assert new != old and _campaign(db, new)["code"] == code
    for replay in (lambda s: pc.activate_campaign(s, campaign_id=old), lambda s: pc.cancel_campaign(s, campaign_id=old),
                   lambda s: pc.close_campaign(s, campaign_id=old),
                   lambda s: pc.revise_campaign_policies(s, campaign_id=old, **POLICY),
                   lambda s: ch.define_cohort(s, campaign_id=old, cohort_policy_version="v0-cohort-v1",
                                              admission_closes_at=clock.now + timedelta(days=1))):
        assert _refusal(lambda: committed(db, replay)) == ("CampaignNotFound", None)
    assert _campaign(db, old) is None and _campaign(db, new)["status"] == "planned"


def test_an_outside_sql_writer_after_the_last_global_read_leaves_an_orphan_reference_l11(db, clock, monkeypatch):
    """Limite L11 étendue à la campagne, reproduite : un écrivain SQL hors
    service (sans le verrou de campagne) écrit une référence à A après la
    seconde lecture globale ; la purge réussit et la référence devient
    orpheline. La double lecture ne l'élimine pas."""
    a = _empty_expired(db, clock)
    _, event, document = _outsider_event(db, clock)
    clock.set(_deadline(db, a))
    real, calls = cl._event_scope_scan, []

    def then_write(session, campaign_id, budget):
        result = real(session, campaign_id, budget)
        calls.append(1)
        if len(calls) == 2:
            [scope] = document["scopes"]
            _forge(db, event.id, {"version": "retention-scopes-v1", "scopes": [{**scope, "campaign_id": str(a)}]})
        return result
    monkeypatch.setattr(cl, "_event_scope_scan", then_write)
    assert purge_campaign(db, a) == _purged()
    [row] = rows(db, f"SELECT retention_scopes FROM {EVENTS} WHERE id = :i", i=event.id)
    assert retention.campaign_reference(row["retention_scopes"], a) == retention.REFERENCE_PRESENT  # orpheline
    assert _campaign(db, a) is None


def test_a_reference_written_between_the_two_global_reads_is_a_dependency_change(db, clock, monkeypatch):
    a = _empty_expired(db, clock)
    _, event, document = _outsider_event(db, clock)
    clock.set(_deadline(db, a))
    real, calls = cl._event_scope_scan, []

    def then_write(session, campaign_id, budget):
        result = real(session, campaign_id, budget)
        calls.append(1)
        if len(calls) == 1:
            [scope] = document["scopes"]
            _forge(db, event.id, {"version": "retention-scopes-v1", "scopes": [{**scope, "campaign_id": str(a)}]})
        return result
    monkeypatch.setattr(cl, "_event_scope_scan", then_write)
    assert purge_campaign(db, a) == cb.Outcome(cb.CONFLICT, cl.DEPENDENCY_CHANGED)
    assert _campaign(db, a) is not None


def test_a_real_deadlock_provoked_by_an_outside_writer_is_detected_and_nothing_is_claimed(engine, monkeypatch, f2):
    """Écrivain hors service : PLACE puis CAMPAGNE (ordre inverse de la
    purge) ; PostgreSQL détecte l'interblocage ; jamais un succès fictif."""
    from tests.test_activity_event_finalization import CLEANUP, Clock
    fake = Clock(datetime.now(timezone.utc) - timedelta(minutes=5))
    monkeypatch.setattr(common, "utcnow", fake)
    monkeypatch.setattr(proofs, "server_clock", fake)
    try:
        cohort = _cohort_campaign(engine, f2, seats=1)
        _expired_terminal(engine, fake, cohort.id)
        before = rows(engine, "SELECT deadlocks AS n FROM pg_stat_database WHERE datname = current_database()")[0]["n"]
        writer = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            writer.execute(sa.text(f"SELECT 1 FROM {SEATS} WHERE campaign_id = :c FOR UPDATE"), {"c": cohort.id})
            contender = Background(lambda: purge_campaign(engine, cohort.id))
            contender.start()
            _wait(lambda: waiting(engine) >= 1, "la purge n'attend pas la place")
            try:
                writer.execute(sa.text(f"SELECT 1 FROM {CAMPAIGNS} WHERE id = :c FOR SHARE"), {"c": cohort.id})
                writer.commit()
            except sa.exc.OperationalError:
                writer.rollback()
            outcome = contender.outcome()
        finally:
            writer.close()
        after = rows(engine, "SELECT deadlocks AS n FROM pg_stat_database WHERE datname = current_database()")[0]["n"]
        assert after == before + 1
        present = _campaign(engine, cohort.id) is not None
        assert (outcome.outcome, present) in ((cb.UNKNOWN, True), (cb.PURGED, False)), (outcome, present)
    finally:
        with engine.begin() as conn:
            for table in CLEANUP:
                conn.execute(sa.text(f"DELETE FROM {table}"))


# --------------------------------------------------------------------------
# N. Propriété transactionnelle et COMMIT incertain
# --------------------------------------------------------------------------

def _due_everything(db, clock, f2):
    """Une campagne à terminer, une preuve à purger, une campagne à purger."""
    to_terminate = _active(db, close_in=timedelta(days=400))
    proof = _certified(db)
    _expire(db, clock, proof.campaign_id)
    to_purge = _cohort_campaign(db, f2, seats=1)
    _expired_terminal(db, clock, to_purge.id)
    clock.set(max(clock.now, _deadline(db, proof.campaign_id), utc(_campaign(db, to_terminate)["close_by_at"])))
    return to_terminate, proof, to_purge.id


@pytest.mark.parametrize("kind", ["connection", "session"])
def test_every_entry_point_refuses_a_caller_connection_or_session_without_touching_its_transaction(
        db, clock, f2, kind):
    to_terminate, proof, to_purge = _due_everything(db, clock, f2)
    world = _world(db)
    items = {cb.terminate_campaign: to_terminate, cb.purge_campaign_proof: proof.proof_id, cb.purge_campaign: to_purge}
    with (db.connect() if kind == "connection" else Session(bind=db)) as caller:
        for open_transaction in (False, True):
            xid = _xid(caller) if open_transaction else None
            for function in ENTRY_POINTS:
                seen, remove = _statements(db)
                try:
                    with pytest.raises(TypeError):
                        _call(function, caller, items.get(function))
                finally:
                    remove()
                assert seen == [], function.__name__  # aucune instruction, pas même BEGIN
                assert caller.in_transaction() is open_transaction, function.__name__
            if open_transaction:
                assert _xid(caller) == xid
        caller.rollback()
    assert _world(db) == world
    assert terminate(db, to_terminate) == CLOSED and purge_proof(db, proof.proof_id).outcome == cb.RESOLVED_AND_PURGED
    assert purge_campaign(db, to_purge) == _purged(1, 1)


def test_only_an_engine_able_to_open_its_own_postgresql_transaction_is_accepted(db):
    url = db.url
    refused = [None, url.render_as_string(hide_password=False), url, _NoDB(), sa.create_engine("sqlite://"),
               sa.create_engine(url, poolclass=sa.pool.NullPool, isolation_level="AUTOCOMMIT"),
               sa.create_engine(url, poolclass=sa.pool.NullPool).execution_options(isolation_level="AUTOCOMMIT"),
               sa.create_engine(url, poolclass=sa.pool.NullPool).execution_options(postgresql_readonly=True)]
    activity = []
    for engine in refused:
        if isinstance(engine, sa.Engine):
            sa.event.listen(engine, "connect", lambda *a: activity.append(a))
            sa.event.listen(engine, "before_cursor_execute", lambda *a: activity.append(a))
    for engine in refused:
        for function in ENTRY_POINTS:
            with pytest.raises(TypeError):
                _call(function, engine)
    assert activity == []
    assert cb._require_engine is tx._require_engine and cb._owned is tx._owned  # garde-fous communs, inchangés


def test_a_driver_level_autocommit_is_detected_in_every_transaction_and_nothing_is_done(db, clock, f2):
    to_terminate, proof, to_purge = _due_everything(db, clock, f2)
    world = _world(db)
    autocommit = sa.create_engine(db.url, poolclass=sa.pool.NullPool)
    sa.event.listen(autocommit, "connect", lambda dbapi_connection, record: setattr(
        dbapi_connection, "autocommit", True))
    seen, remove = _statements(autocommit)
    unknown = cb.Outcome(cb.UNKNOWN, cb.TECHNICAL_UNCERTAINTY)
    try:
        assert cb.terminate_campaign(autocommit, campaign_id=to_terminate) == unknown
        assert cb.purge_campaign_proof(autocommit, proof_id=proof.proof_id) == unknown
        assert cb.purge_campaign(autocommit, campaign_id=to_purge) == unknown
        for page in (cb.terminate_campaigns_page, cb.purge_campaign_proofs_page, cb.purge_campaigns_page):
            report = page(autocommit)
            assert (report.selected, report.examined, report.next_cursor) == (False, 0, None), page.__name__
    finally:
        remove()
        autocommit.dispose()
    work = [s for s in seen if not s.startswith("SET ") and "transaction_status" not in s]
    assert work == []  # ni lecture, ni verrou, ni écriture hors d'un bloc de transaction
    assert _world(db) == world


def _spy_statuses(monkeypatch):
    statuses = []
    real = tx._require_effective_transaction

    def spy(session):
        statuses.append(session.connection().connection.dbapi_connection.info.transaction_status)
        return real(session)
    monkeypatch.setattr(tx, "_require_effective_transaction", spy)
    return statuses


def test_every_transaction_of_a_valid_engine_is_an_effective_postgresql_transaction(db, clock, f2, monkeypatch):
    to_terminate, proof, to_purge = _due_everything(db, clock, f2)
    statuses = _spy_statuses(monkeypatch)
    for page in (cb.terminate_campaigns_page, cb.purge_campaign_proofs_page, cb.purge_campaigns_page):
        statuses.clear()
        report = page(db)
        assert report.selected and report.examined >= 1
        assert statuses == [tx._INTRANS] * (1 + report.examined), page.__name__  # sélection + un par candidat
    other = _active(db, close_in=timedelta(days=1))
    clock.set(max(clock.now, utc(_campaign(db, other)["close_by_at"])))
    statuses.clear()
    seen, remove = _lose_commit(db, f"UPDATE {CAMPAIGNS}", really_commit=False)
    try:
        assert terminate(db, other) == cb.Outcome(cb.STILL_PRESENT, cb.TECHNICAL_UNCERTAINTY)
    finally:
        remove()
    assert seen == ["lost"] and statuses == [tx._INTRANS] * 2  # écriture + relecture, chacune la sienne
    del to_terminate, proof, to_purge


@pytest.mark.parametrize("path", ["termination", "proof", "campaign"])
def test_a_relecture_alone_running_in_autocommit_is_unknown(db, clock, f2, monkeypatch, path):
    """Contrôle propre à la relecture : seule la transaction de relecture perd
    son bloc effectif ; jamais une issue de succès ni d'absence."""
    to_terminate, proof, to_purge = _due_everything(db, clock, f2)
    real = tx._require_effective_transaction
    calls = []

    def second_fails(session):
        calls.append(1)
        if len(calls) == 2:
            raise tx._NotOwned()
        return real(session)
    monkeypatch.setattr(tx, "_require_effective_transaction", second_fails)
    prefix, action = {"termination": (f"UPDATE {CAMPAIGNS}", lambda: terminate(db, to_terminate)),
                      "proof": (f"DELETE FROM {REGISTRY}", lambda: purge_proof(db, proof.proof_id)),
                      "campaign": (f"DELETE FROM {CAMPAIGNS}", lambda: purge_campaign(db, to_purge))}[path]
    seen, remove = _lose_commit(db, prefix, really_commit=True)
    try:
        assert action() == cb.Outcome(cb.UNKNOWN, cb.TECHNICAL_UNCERTAINTY)
    finally:
        remove()
    assert seen == ["lost"] and len(calls) == 2


@pytest.mark.parametrize("really_commit", [True, False])
@pytest.mark.parametrize("status", ["planned", "active"])
def test_an_uncertain_termination_commit_is_never_counted_as_a_termination(db, clock, really_commit, status):
    campaign_id = _create(db) if status == "planned" else _active(db)
    clock.set(utc(_campaign(db, campaign_id)["close_by_at"]))
    before = _campaign(db, campaign_id)
    seen, remove = _lose_commit(db, f"UPDATE {CAMPAIGNS}", really_commit=really_commit)
    try:
        report = cb.terminate_campaigns_page(db)
    finally:
        remove()
    assert seen == ["lost"] and report.examined == 1 and report.closed == report.cancelled == 0
    if really_commit:
        assert report.unconfirmed == 1 and _campaign(db, campaign_id)["status"] in ("closed", "cancelled")
        assert terminate(db, campaign_id) == ALREADY
    else:
        assert report.still_present == 1 and _campaign(db, campaign_id) == before
        assert terminate(db, campaign_id).outcome in (cb.CLOSED, cb.CANCELLED)  # réexécution explicite sûre


@pytest.mark.parametrize("column", ["name", "purpose", "code", "closed_at"])
def test_an_altered_campaign_snapshot_after_an_uncertain_termination_is_unknown(db, clock, column):
    campaign_id = _active(db)
    clock.set(utc(_campaign(db, campaign_id)["close_by_at"]))
    alter = {"name": f"UPDATE {CAMPAIGNS} SET name = 'Autre' WHERE id = :i",
             "purpose": f"UPDATE {CAMPAIGNS} SET purpose = 'Autre.' WHERE id = :i",
             "code": f"UPDATE {CAMPAIGNS} SET code = code || '-x' WHERE id = :i",
             "closed_at": f"UPDATE {CAMPAIGNS} SET closed_at = closed_at - interval '1 second' WHERE id = :i"}[column]
    seen, remove = _lose_commit(db, f"UPDATE {CAMPAIGNS}", really_commit=True, between=lambda: _sql(
        db, alter, i=campaign_id))
    try:
        assert terminate(db, campaign_id) == cb.Outcome(cb.UNKNOWN, cb.TECHNICAL_UNCERTAINTY)
    finally:
        remove()
    assert seen == ["lost"]


def test_an_uncertain_termination_of_a_vanished_campaign_is_absent_unconfirmed(db, clock):
    campaign_id = _create(db)
    clock.set(utc(_campaign(db, campaign_id)["close_by_at"]))
    seen, remove = _lose_commit(db, f"UPDATE {CAMPAIGNS}", really_commit=True, between=lambda: _sql(
        db, f"DELETE FROM {CAMPAIGNS} WHERE id = :i", i=campaign_id))
    try:
        assert terminate(db, campaign_id) == cb.Outcome(cb.ABSENT, cb.UNCONFIRMED_COMMIT)
    finally:
        remove()


@pytest.mark.parametrize("really_commit", [True, False])
def test_an_uncertain_proof_purge_is_never_counted_as_a_purge(db, clock, really_commit):
    proof = _certified(db)
    _expire(db, clock, proof.campaign_id)
    before = _proof(db, proof.proof_id)
    seen, remove = _lose_commit(db, f"DELETE FROM {REGISTRY}", really_commit=really_commit)
    try:
        outcome = purge_proof(db, proof.proof_id)
    finally:
        remove()
    assert seen == ["lost"]
    if really_commit:
        assert outcome == cb.Outcome(cb.ABSENT, cb.UNCONFIRMED_COMMIT) and _proof(db, proof.proof_id) is None
    else:
        assert outcome == cb.Outcome(cb.UNKNOWN, cb.TECHNICAL_UNCERTAINTY)  # jamais still_present
        assert _proof(db, proof.proof_id) == before  # aucune résolution intermédiaire durable
        assert purge_proof(db, proof.proof_id).outcome == cb.RESOLVED_AND_PURGED


def test_an_uncertain_purge_of_a_recorded_proof_does_not_require_the_event_absence(db, clock):
    proof = _expired_recorded(db, clock)
    seen, remove = _lose_commit(db, f"DELETE FROM {REGISTRY}", really_commit=True)
    try:
        assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.ABSENT, cb.UNCONFIRMED_COMMIT)
    finally:
        remove()
    assert _count(db, EVENTS) == 1 and _count(db, ATTRIBUTIONS) == 1  # responsabilité de la PR 2


def test_a_proof_of_the_same_canonical_identity_under_another_uuid_makes_the_reread_unknown(db, clock):
    proof = _pending(db)
    _expire(db, clock, proof.campaign_id)
    row = _proof(db, proof.proof_id)
    columns = [c for c in row if c not in ("xmin", "id")]

    def recreate():
        values = {c: row[c] for c in columns}
        _sql(db, f"INSERT INTO {REGISTRY} (id, {', '.join(columns)}) VALUES (:id, "
                 f"{', '.join(':' + c for c in columns)})", id=uuid.uuid4(), **values)
    seen, remove = _lose_commit(db, f"DELETE FROM {REGISTRY}", really_commit=True, between=recreate)
    try:
        assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.UNKNOWN, cb.TECHNICAL_UNCERTAINTY)
    finally:
        remove()
    assert seen == ["lost"]


@pytest.mark.parametrize("really_commit", [True, False])
def test_an_uncertain_campaign_purge_is_never_counted_as_a_purge(db, clock, f2, really_commit):
    cohort = _cohort_campaign(db, f2, seats=2)
    _expired_terminal(db, clock, cohort.id)
    world = _world(db)
    seen, remove = _lose_commit(db, f"DELETE FROM {CAMPAIGNS}", really_commit=really_commit)
    try:
        report = cb.purge_campaigns_page(db)
    finally:
        remove()
    assert seen == ["lost"] and report.purged == 0 and (report.seats, report.cohorts) == (0, 0)
    if really_commit:
        assert (report.absent, report.absent_unconfirmed) == (1, 1) and _gone(db, cohort.id)
    else:
        assert report.unknown == 1 and _world(db) == world


@pytest.mark.parametrize("residue", ["scope", "ambiguous", "interrupted"])
def test_an_absent_campaign_with_a_residual_scope_an_ambiguous_document_or_an_interrupted_scan_is_unknown(
        db, clock, monkeypatch, residue):
    a = _empty_expired(db, clock)
    _, event, document = _outsider_event(db, clock)
    clock.set(_deadline(db, a))
    [scope] = document["scopes"]

    def between():
        if residue == "scope":
            _forge(db, event.id, {"version": "retention-scopes-v1", "scopes": [{**scope, "campaign_id": str(a)}]})
        elif residue == "ambiguous":
            _forge(db, event.id, {"version": "retention-scopes-v1", "scopes": [{**scope, "kind": 3}]})
        else:
            ticks = iter([0.0] + [100.0] * 1000)  # budget propre de la relecture, dépassé pendant la lecture
            monkeypatch.setattr(cl, "_monotonic", lambda: next(ticks))
    seen, remove = _lose_commit(db, f"DELETE FROM {CAMPAIGNS}", really_commit=True, between=between)
    try:
        assert purge_campaign(db, a) == cb.Outcome(cb.UNKNOWN, cb.TECHNICAL_UNCERTAINTY)
    finally:
        remove()
    assert seen == ["lost"] and _campaign(db, a) is None


# --------------------------------------------------------------------------
# O. Pagination, plans et budget
# --------------------------------------------------------------------------

def test_pages_are_strict_bounded_and_their_cursor_is_the_selection_snapshot(db, clock):
    close_by = clock.now + timedelta(days=3)
    campaigns_ = [committed(db, lambda s: pc.create_campaign(
        s, code=f"wave-{uuid.uuid4().hex[:10]}", name="V", purpose="M.", close_by_at=close_by, **POLICY).id)
        for _ in range(5)]  # échéances identiques
    clock.set(close_by)
    selection = committed(db, lambda s: cl.select_due_campaign_candidates(s, limit=2))
    report = cb.terminate_campaigns_page(db, limit=2)
    assert report.next_cursor == selection.next_cursor and report.examined == 2 and report.cancelled == 2
    second = cb.terminate_campaigns_page(db, limit=2, after=report.next_cursor)
    third = cb.terminate_campaigns_page(db, limit=2, after=second.next_cursor)
    assert (second.examined, third.examined, third.next_cursor) == (2, 1, None)
    assert {_campaign(db, c)["status"] for c in campaigns_} == {"cancelled"}
    assert cb.terminate_campaigns_page(db).examined == 0  # page vide : selected, rien
    empty = cb.terminate_campaigns_page(db)
    assert empty.selected and empty.next_cursor is None
    for statement in (cl._termination_query(clock.now, 50, None), cl._proof_query(clock.now, 50, None),
                      cl._purge_query(clock.now, 50, None)):
        assert "OFFSET" not in str(statement) and " FOR " not in str(statement)


def test_an_incoherent_campaign_is_selected_by_every_path_and_never_hidden(db, clock):
    proof = _pending(db)
    _sql(db, f"UPDATE {CAMPAIGNS} SET retention_policy_version = 'v9' WHERE id = :i", i=proof.campaign_id)
    assert proof.campaign_id in committed(db, lambda s: cl.select_due_campaign_candidates(s)).campaign_ids
    assert proof.proof_id in committed(db, lambda s: cl.select_campaign_expired_proof_candidates(s)).proof_ids
    assert proof.campaign_id in committed(db, lambda s: cl.select_purgeable_campaign_candidates(s)).campaign_ids
    assert terminate(db, proof.campaign_id) == cb.Outcome(cb.CONFLICT, cl.POLICY_INCOHERENT)
    assert purge_proof(db, proof.proof_id) == cb.Outcome(cb.CONFLICT, cl.POLICY_INCOHERENT)
    assert purge_campaign(db, proof.campaign_id) == cb.Outcome(cb.CONFLICT, cl.POLICY_INCOHERENT)


def test_an_individual_failure_never_blocks_the_following_candidates_and_a_failed_selection_advances_nothing(
        db, clock, monkeypatch):
    gone, held, healthy = (_create(db) for _ in range(3))
    clock.set(utc(_campaign(db, healthy)["close_by_at"]))
    real = cl.select_due_campaign_candidates

    def then_delete(session, **kwargs):
        page = real(session, **kwargs)
        _sql(db, f"DELETE FROM {CAMPAIGNS} WHERE id = :i", i=gone)  # disparue après la sélection
        return page
    monkeypatch.setattr(cl, "select_due_campaign_candidates", then_delete)
    holder = Holder(db, lambda s: s.execute(sa.text(f"SELECT 1 FROM {CAMPAIGNS} WHERE id = :i FOR UPDATE"),
                                            {"i": held}))
    try:
        report = cb.terminate_campaigns_page(db)
    finally:
        holder.rollback()
    assert (report.examined, report.absent, report.unknown, report.cancelled) == (3, 1, 1, 1)
    monkeypatch.setattr(cl, "select_due_campaign_candidates", lambda session, **kwargs: 1 / 0)
    failed = cb.terminate_campaigns_page(db)
    assert (failed.selected, failed.examined, failed.next_cursor) == (False, 0, None)


def test_the_selection_plans_use_existing_indexes_on_representative_data(db, clock):
    for _ in range(3):
        _pending(db)
    _sql(db, "ANALYZE product_activity_proof_registry; ANALYZE product_campaigns")
    cursor = cl.CampaignProofCursor(uuid.uuid4(), uuid.uuid4())
    for after in (None, cursor):
        plan = _plan(db, cl._proof_query(clock.now, 50, after), seqscan=False)
        # Index existants commençant par campaign_id (campaign_status ou visit) : ordre du curseur, tri
        # incrémental sur id, filtre de campagne par jointure sur sa clé primaire (L14 : aucun index nouveau).
        assert re.search(r"Index (Only )?Scan using ix_product_activity_proof_registry_(campaign_status|visit)",
                         plan), plan
        assert "Seq Scan on product_activity_proof_registry" not in plan and "product_campaigns_pkey" in plan, plan
    plan = _plan(db, cl._purge_query(clock.now, 50, cl.CampaignPurgeCursor(uuid.uuid4())), seqscan=False)
    assert "product_campaigns_pkey" in plan, plan


def test_the_budget_is_monotonic_cumulative_and_interrupts_without_mutation(db, clock, monkeypatch):
    a = _empty_expired(db, clock)
    for _ in range(3):
        _outsider_event(db, clock)
    clock.set(_deadline(db, a))
    world = _world(db)
    ticks = {"now": 0.0}
    monkeypatch.setattr(cl, "_monotonic", lambda: ticks["now"])
    real = retention.campaign_reference

    def slow(document, campaign_id):
        ticks["now"] += 1.8  # chaque document « coûte » 1,8 s monotones
        return real(document, campaign_id)
    monkeypatch.setattr(retention, "campaign_reference", slow)
    # 3 documents par lecture : 5,4 s puis 10,8 s cumulées : la SECONDE lecture dépasse (jamais réinitialisé).
    assert purge_campaign(db, a) == cb.Outcome(cb.UNKNOWN, cl.SCAN_BUDGET_EXCEEDED)
    assert _world(db) == world
    ticks["now"] = 0.0
    monkeypatch.setattr(retention, "campaign_reference", real)
    assert purge_campaign(db, a) == _purged()  # sans dépassement : purge exhaustive
    assert inspect.signature(cb.purge_campaign).parameters.keys() == {"engine", "campaign_id"}


def test_the_budget_is_checked_after_lock_waits_and_before_the_delete(db, clock, monkeypatch, f2):
    cohort = _cohort_campaign(db, f2, seats=1)
    _expired_terminal(db, clock, cohort.id)
    world = _world(db)
    ticks = {"now": 0.0}
    monkeypatch.setattr(cl, "_monotonic", lambda: ticks["now"])
    real = pl._lock_mode

    def slow_lock(statement, mode):
        if mode == "update" and "product_cohort_seats" in str(statement):
            ticks["now"] += 11.0  # attente du verrou des places
        return real(statement, mode)
    monkeypatch.setattr(pl, "_lock_mode", slow_lock)
    assert purge_campaign(db, cohort.id) == cb.Outcome(cb.UNKNOWN, cl.SCAN_BUDGET_EXCEEDED)
    assert _world(db) == world


def test_the_server_side_cursor_is_closed_on_early_returns(db, clock, monkeypatch):
    a = _empty_expired(db, clock)
    _, event, document = _outsider_event(db, clock)
    [scope] = document["scopes"]
    _forge(db, event.id, {"version": "retention-scopes-v1", "scopes": [{**scope, "kind": "user"}]})
    clock.set(_deadline(db, a))
    with Session(bind=db, autoflush=False) as session:
        assert cl.purge_campaign(session, campaign_id=a).reason == cl.RETENTION_INCOHERENT  # retour anticipé
        assert session.execute(sa.text("SELECT count(*) FROM pg_cursors")).scalar_one() == 0  # curseur refermé
        session.rollback()


# --------------------------------------------------------------------------
# P. Prévention de la recréation
# --------------------------------------------------------------------------

def test_after_the_purge_the_historical_paths_refuse_every_replay_without_writing(db, clock, f2):
    cohort = _cohort_campaign(db, f2)
    _activate_cohort(db, cohort)
    clock.advance(minutes=10)
    user = _bind(db, f2, cohort, 0)
    committed(db, lambda s: ch.admit_at_first_eligible_login(s, campaign_id=cohort.id, user_id=user))
    visit, key = touch(db, cohort.id, user)
    _expired_terminal(db, clock, cohort.id)
    assert pb.purge_participation(db, campaign_id=cohort.id, user_id=user).outcome == pb.PURGED
    invitations = rows(db, "SELECT *, xmin::text AS xmin FROM invitations WHERE id = ANY(:i)", i=cohort.invitations)
    assert purge_campaign(db, cohort.id) == _purged(5, 1)
    world = _world(db)
    now = common.utcnow()
    replays = {
        "activation": lambda s: ch.activate_cohort_campaign(s, campaign_id=cohort.id),
        "closure": lambda s: pc.close_campaign(s, campaign_id=cohort.id),
        "cancellation": lambda s: pc.cancel_campaign(s, campaign_id=cohort.id),
        "policy": lambda s: pc.revise_campaign_policies(s, campaign_id=cohort.id, **POLICY),
        "cohort": lambda s: ch.define_cohort(s, campaign_id=cohort.id, cohort_policy_version="v0-cohort-v1",
                                             admission_closes_at=now + timedelta(days=1)),
        "seat": lambda s: ch.add_cohort_seat(s, campaign_id=cohort.id, invitation_id=cohort.invitations[0]),
        "binding": lambda s: ch.bind_seat_to_activated_invitation(s, campaign_id=cohort.id, seat_id=cohort.seats[0]),
        "authorization": lambda s: ch.record_seat_authorization(s, campaign_id=cohort.id, user_id=user,
                                                                notice_version=NOTICE),
        "admission": lambda s: ch.admit_at_first_eligible_login(s, campaign_id=cohort.id, user_id=user),
        "generic": lambda s: pp.enroll_participant(s, campaign_id=cohort.id, user_id=user, window_start_at=now,
                                                   window_end_at=now + timedelta(days=1),
                                                   authorization_recorded_at=now, notice_version=NOTICE),
        "first_login": lambda s: pp.enroll_at_first_eligible_login(s, campaign_id=cohort.id, user_id=user,
                                                                   authorization_recorded_at=now,
                                                                   notice_version=NOTICE),
        "visit": lambda s: ps.touch_activity_session(s, campaign_id=cohort.id, user_id=user, environment_key=LAPTOP,
                                                     interaction_key=key),
        "close_visit": lambda s: ps.close_activity_session(s, campaign_id=cohort.id, user_id=user,
                                                           environment_key=LAPTOP, interaction_key=uuid.uuid4()),
        "proof_origin": lambda s: proofs.open_origin(s, campaign_id=cohort.id, environment_key=LAPTOP,
                                                     operation=operation(user)),
        "event": lambda s: _event_writer(s, user, now),
    }
    for name, replay in replays.items():
        refused = _refusal(lambda: committed(db, replay))
        assert refused is not None, name
    attempt = ch.attempt_first_login_admission  # contrat non bloquant : aucune place, aucune écriture
    assert committed(db, lambda s: attempt(s, campaign_id=cohort.id, user_id=user)).status == ch.NOT_ELIGIBLE
    assert _world(db) == world  # rien de recréé
    assert rows(db, "SELECT *, xmin::text AS xmin FROM invitations WHERE id = ANY(:i)", i=cohort.invitations) == \
        invitations  # invitations F2 préservées
    del visit


def _event_writer(session, user, now):
    from core.product_analytics import events as pe
    return pe.record_event(session, user_id=user, visit_id=uuid.uuid4(), idempotency_key=uuid.uuid4(),
                           event_type="coach_used", surface="coach", executed_route=None, source="client_signal",
                           source_operation_kind=None, source_operation_id=None, outcome="succeeded",
                           delivery_status="not_applicable", occurred_at=now, resolved_at=now, delivered_at=None)


# --------------------------------------------------------------------------
# Journaux, inventaires et dormance
# --------------------------------------------------------------------------

def test_the_logs_carry_only_closed_outcomes_reasons_and_counters(db, clock, f2, caplog):
    caplog.set_level(logging.INFO, logger="oryx.activity_campaign_lifecycle")
    to_terminate, proof, to_purge = _due_everything(db, clock, f2)
    code = _campaign(db, to_purge)["code"]
    cb.terminate_campaigns_page(db)
    cb.purge_campaign_proofs_page(db)
    cb.purge_campaigns_page(db)
    terminate(db, to_terminate)
    messages = [r.getMessage() for r in caplog.records if r.name == "oryx.activity_campaign_lifecycle"]
    assert messages
    for message in messages:
        assert not UUID_PATTERN.search(message) and USER_A not in message and code not in message, message
        assert "retention-scopes" not in message and "Cursor" not in message and "Vague" not in message, message
        assert re.fullmatch(r"activity_campaign_[a-z_]+( [a-z_]+=[A-Za-z0-9_]+)+", message), message
    del proof


FORBIDDEN = ("authz_f2", "auth_f2", "f2_runtime", "_activity_proof_bridge", "oryx_sessions", "OryxSession",
             "_run_attested", "os.environ", "getenv", "Thread", "asyncio", "schedule", "socket", "requests", "httpx",
             "APIRouter", "FastAPI", "import_module", "__import__", "record_event", "_stage_certified_event",
             "finalize_certified_proof", "resolve_expired_pending_proof", "finalization._record", "finalization._lock(",
             "certify_pending", "register_pending", "open_origin", "finalize_idle_sessions", "purge_resolved_proof",
             "expire_event_scopes", "purge_participation", "withdraw_participant", "close_campaign", "cancel_campaign",
             "TRUNCATE", "CASCADE", "SET NULL", "session_replication_role", "DISABLE TRIGGER", "while True", "sleep",
             "print(", "tombstone", "portfolio_positions", "invitations", "auth_operations", "pedagog", "cognitive",
             "COMPLETED", "INSERT", "pg_advisory_lock(", "LOCK TABLE", "campaign_code")


def _imports(path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            if node.module in ("core", "core.product_analytics"):
                found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def test_the_modules_import_exactly_their_side_and_reach_nothing_else():
    assert _imports(SERVICE) == {"time", "uuid", "dataclasses", "datetime", "types", "sqlalchemy", "sqlalchemy.exc",
                                 "core.models", "core.product_analytics", "core.product_analytics.common",
                                 "core.product_analytics.event_lifecycle", "core.product_analytics.finalization",
                                 "core.product_analytics.participation_lifecycle", "core.product_analytics.proofs",
                                 "core.product_analytics.retention"}
    assert _imports(BOUNDARY) == {"logging", "uuid", "dataclasses", "core", "core._activity_event_finalization",
                                  "core._activity_transactions", "core.product_analytics",
                                  "core.product_analytics.campaign_lifecycle"}
    for path in (SERVICE, BOUNDARY):
        source = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN:
            assert forbidden not in source, (path.name, forbidden)
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    session_calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                     and n.func.value.id == "db"}
    assert session_calls == {"execute", "begin_nested", "expunge"}  # ni commit, ni rollback, ni add, ni flush
    deletes = [[ast.unparse(a) for a in n.args] for n in sorted(
        (n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
         and n.func.id == "delete"), key=lambda n: (n.lineno, n.col_offset))]  # ordre du source
    assert deletes == [["table"], ["seats_table"], ["cohorts_table"], ["campaigns_table"]]  # preuve ; places, cohorte,
    updates = [[ast.unparse(a) for a in n.args] for n in ast.walk(tree) if isinstance(n, ast.Call)  # campagne
               and isinstance(n.func, ast.Name) and n.func.id == "update"]
    assert updates == [["table"]]  # terminaison seulement
    assert len([n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "begin_nested"]) == 3  # UN SAVEPOINT par chemin d'écriture
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"ProductActivityEvent_", "Invitation", "AuthOperation", "User"}
    assert {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "retention"} == {"parse_retention_scopes", "campaign_reference", "REFERENCE_INCOHERENT",
                                              "REFERENCE_PRESENT"}
    assert {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "event_lifecycle"} == {"_required_keys", "_activity_proof_key", "_path", "CERTIFIED",
                                                    "_Locked", "_contract_problem"}
    boundary = BOUNDARY.read_text(encoding="utf-8")
    assert "import sqlalchemy" not in boundary and "sa." not in boundary and "text(" not in boundary
    assert "SELECT" not in boundary and "DELETE " not in boundary and "UPDATE " not in boundary
    assert "execute(" not in boundary and "while " not in boundary and "for _ in" not in boundary
    assert "finalization_boundary._transaction(" in boundary and "finalization_boundary._run(" not in boundary
    assert boundary.count("_owned(") == 3  # sélection, candidat, relecture (helpers partagés des trois parcours)
    assert "retry" not in boundary.lower().replace("aucun retry automatique", "")
    assert {a.attr for a in ast.walk(ast.parse(boundary)) if isinstance(a, ast.Attribute)
            and isinstance(a.value, ast.Name) and a.value.id == "finalization_boundary"} == {
        "_transaction", "_COMMITTED", "_FAILED"}


def test_nothing_calls_the_campaign_lifecycle_outside_the_tests_and_the_documentation():
    offenders = []
    markers = ("campaign_lifecycle", "_activity_campaign_lifecycle", "terminate_due_campaign", "terminate_campaign",
               "purge_campaign_expired_proof", "select_purgeable_campaign_candidates", "purge_campaigns_page")
    for path in sorted(REPO_ROOT.rglob("*")):
        if (not path.is_file() or path in (SERVICE, BOUNDARY) or ".git" in path.parts or "__pycache__" in path.parts
                or "node_modules" in path.parts or path.suffix not in (".py", ".js", ".html", ".ts", ".json", ".sh",
                                                                        ".yml", ".yaml", ".toml", ".ini", ".cfg",
                                                                        ".txt", ".mjs")):
            continue
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in ("tests", "docs"):
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in source for marker in markers):
            offenders.append(str(relative))
    assert offenders == []
    script = _script_directory()
    assert script.get_heads() == ["0024_product_activity_proofs"]
    assert len(list((REPO_ROOT / "alembic" / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))) == 24
    for path, digest in ((MIGRATION_0022, MIGRATION_0022_SHA256), (MIGRATION_0023, MIGRATION_0023_SHA256),
                         (MIGRATION_0024, MIGRATION_0024_SHA256)):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


PROBE = r'''
import json, socket, sys
socket.socket.connect = lambda self, address: (_ for _ in ()).throw(OSError("réseau interdit"))
import api
loaded = sorted(n for n in sys.modules if "campaign_lifecycle" in n or n.endswith("_activity_campaign_lifecycle"))
print(json.dumps({"loaded": loaded}))
'''


def test_the_real_api_never_loads_the_campaign_lifecycle_whatever_the_environment():
    import subprocess
    import sys
    from tests.test_f2_http_dormancy import _clean_env
    for environment in ({}, {"ORYX_PRODUCT_ANALYTICS": "1", "ORYX_V0_T08_ENABLED": "1", "ORYX_CAMPAIGN_LIFECYCLE": "1",
                             "ORYX_F2_ENABLED": "1"}):
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                             env=_clean_env(environment), timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        assert json.loads(out.stdout.strip().splitlines()[-1])["loaded"] == []
