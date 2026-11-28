"""V0-T08 Lot 2C PR 3 — cycle de vie des participations, visites et
interactions : finalisation bornée des visites inactives (parcours A) et
purge atomique des participations échues (parcours B)
(core/product_analytics/participation_lifecycle.py, frontière
core/_activity_participation_lifecycle.py), contre un VRAI PostgreSQL (16 et
18 en CI). Chaîne réelle F2 -> B -> C -> PR 1 -> PR 2 -> PR 3 :
tests/test_activity_participation_lifecycle_chain.py ; charges jusqu'à
50 000 interactions : tests/test_activity_participation_lifecycle_stress.py.

A. contrats d'entrée ; B. inactivité et finalisation ; C. échéance de
conservation ; D. participations et dénominateurs ; E. dépendances
physiques et logiques ; F. atomicité ; G. concurrence ; H. propriété
transactionnelle et COMMIT incertain ; I. pagination et plans ; J.
recréation et limites historiques ; inventaires et dormance.
"""
import ast
import hashlib
import inspect
import json
import logging
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core import _activity_participation_lifecycle as lb
from core import _activity_transactions as tx
from core.models import ProductCampaign
from core.product_analytics import campaigns as pc
from core.product_analytics import cohort as ch
from core.product_analytics import common
from core.product_analytics import event_lifecycle as el
from core.product_analytics import events as pe
from core.product_analytics import finalization as fz
from core.product_analytics import participation as pp
from core.product_analytics import participation_lifecycle as pl
from core.product_analytics import proof_lifecycle as prl
from core.product_analytics import proofs
from core.product_analytics import retention
from core.product_analytics import sessions as ps
from core.product_analytics.common import (
    ActivitySessionRefused,
    EventNotAuthorized,
    InvalidProductAnalyticsInput,
    ParticipationNotFound,
    ParticipationRefused,
)
from tests.activity_proof_support import (
    ATTRIBUTIONS,
    EVENTS,
    LAPTOP,
    NOTICE,
    PHONE,
    POLICY,
    PROOF_APP,
    REGISTRY,
    Background,
    Holder,
    certify,
    close,
    committed,
    open_origin,
    open_wave,
    operation,
    register,
    rows,
    settle,
    touch,
    utc,
    waiting,
)
from tests.test_activity_event_finalization import (  # noqa: F401 — fixtures engine, db, clock
    CLEANUP,
    MICRO,
    USER_A,
    USER_B,
    _certified,
    _sleep_until,
    _wait,
    clock,
    db,
    engine,
    expire,
    finalize,
)
from tests.test_activity_event_lifecycle import _lose_commit, _record, _statements, _xid
from tests.test_cognitive_capture import _NoDB
from tests.test_f2_http_dormancy import _clean_env
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

SERVICE = REPO_ROOT / "core" / "product_analytics" / "participation_lifecycle.py"
BOUNDARY = REPO_ROOT / "core" / "_activity_participation_lifecycle.py"
CAMPAIGN_LIFECYCLE = REPO_ROOT / "core" / "product_analytics" / "campaign_lifecycle.py"  # PR 4 (nominatif)
PARTICIPANTS = "product_campaign_participants"
CAMPAIGNS = "product_campaigns"
SESSIONS = "product_activity_sessions"
INTERACTIONS = "product_activity_session_interactions"
IDLE_INDEX = "ix_product_activity_sessions_open_idle_expiry"
PARTICIPANTS_PKEY = "product_campaign_participants_pkey"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DAYS_90 = timedelta(days=90)
HOURS_2160 = timedelta(hours=2160)
THIRTY_MINUTES = timedelta(minutes=30)
ENTRY_POINTS = (lb.finalize_session, lb.finalize_sessions_page, lb.purge_participation, lb.purge_participations_page)
SERVICE_ENTRY_POINTS = (pl.participation_retention_deadline, pl.select_idle_session_candidates,
                        pl.finalize_idle_session, pl.observe_session_finalization,
                        pl.select_purgeable_participation_candidates, pl.purge_participation,
                        pl.observe_participation_purge, pl.session_lock_key, pl.participation_lock_key)
ANALYTICS_TABLES = (EVENTS, ATTRIBUTIONS, REGISTRY, INTERACTIONS, SESSIONS, PARTICIPANTS, CAMPAIGNS,
                    "product_campaign_cohorts", "product_cohort_seats", "users")


def _call(function, engine, *, campaign_id=None, user_id=USER_A, session_id=None):
    if function is lb.finalize_session:
        return function(engine, session_id=session_id or uuid.uuid4())
    if function is lb.purge_participation:
        return function(engine, campaign_id=campaign_id or uuid.uuid4(), user_id=user_id)
    return function(engine)


# --------------------------------------------------------------------------
# A. Contrats d'entrée (sans base)
# --------------------------------------------------------------------------

def test_the_vocabularies_are_closed_and_the_two_reports_are_distinct():
    assert lb.FINALIZATION_OUTCOMES == ("finalized", "already_finalized", "not_due", "not_admissible", "absent",
                                        "conflict", "unknown", "unconfirmed", "still_present")
    assert lb.PURGE_OUTCOMES == ("purged", "blocked", "not_due", "not_admissible", "absent", "conflict", "unknown")
    assert set(pl.FINALIZATION_OUTCOMES) < set(lb.FINALIZATION_OUTCOMES)
    assert pl.PURGE_OUTCOMES == lb.PURGE_OUTCOMES  # ni still_present ni unconfirmed pour une purge
    assert pl.BLOCKING_REASONS == ("proof_remaining", "attribution_remaining", "event_scope_remaining")
    assert len(set(lb.OUTCOME_REASONS)) == len(lb.OUTCOME_REASONS)
    assert pl.DEFAULT_PAGE_SIZE == 50 and pl.MAX_PAGE_SIZE == 200
    fixed = ("selection", "selected", "examined", "next_cursor", "absent_unconfirmed")
    finalization = [f for f in lb.FinalizationPageReport.__dataclass_fields__ if f not in fixed]
    purge = [f for f in lb.PurgePageReport.__dataclass_fields__ if f not in fixed + ("interactions", "sessions")]
    assert tuple(finalization) == lb.FINALIZATION_OUTCOMES and tuple(purge) == lb.PURGE_OUTCOMES
    # Trois horloges distinctes : 168 h (PR 1), 90 jours depuis occurred_at (PR 2), 90 jours depuis la fin de campagne.
    assert proofs.REGISTRY_RETENTION == timedelta(hours=168) and common.RETENTION_POLICIES == {"v0-retention-v1": 90}


def test_the_entry_points_receive_only_bounded_and_typed_inputs_and_refuse_before_any_database_access():
    for function in (pl.finalize_idle_session, pl.observe_session_finalization):
        assert list(inspect.signature(function).parameters) == ["db", "session_id"]
        for bad in (str(uuid.uuid4()), None, 42, uuid.uuid4().int):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), session_id=bad)
        for designation in ({"force": True}, {"now": datetime.now(timezone.utc)}, {"user_id": USER_A},
                            {"finalized_at": datetime.now(timezone.utc)}, {"end_reason": "logout"}):
            with pytest.raises(TypeError):
                function(_NoDB(), session_id=uuid.uuid4(), **designation)
    for function in (pl.purge_participation, pl.observe_participation_purge):
        assert list(inspect.signature(function).parameters) == ["db", "campaign_id", "user_id"]
        for campaign_id, user_id in ((str(uuid.uuid4()), USER_A), (None, USER_A), (uuid.uuid4(), ""),
                                     (uuid.uuid4(), None), (uuid.uuid4(), 7), (uuid.uuid4(), " a"),
                                     (uuid.uuid4(), "x" * 256), (uuid.uuid4(), "a\nb")):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), campaign_id=campaign_id, user_id=user_id)
        for designation in ({"force": True}, {"now": datetime.now(timezone.utc)}, {"ignore_dependencies": True},
                            {"retention_days": 1}, {"sessions": ()}, {"interactions": ()}):
            with pytest.raises(TypeError):
                function(_NoDB(), campaign_id=uuid.uuid4(), user_id=USER_A, **designation)
    session_cursor = pl.SessionCursor(uuid.uuid4(), datetime.now(timezone.utc), uuid.uuid4())
    participation_cursor = pl.ParticipationCursor(uuid.uuid4(), USER_A)
    for function, foreign in ((pl.select_idle_session_candidates, participation_cursor),
                              (pl.select_purgeable_participation_candidates, session_cursor)):
        assert list(inspect.signature(function).parameters) == ["db", "limit", "after"]
        assert function.__defaults__ is None and function.__kwdefaults__ == {"limit": 50, "after": None}
        for limit in (0, 201, -1, True, 1.0, "5", None):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), limit=limit)
        for after in (foreign, SimpleNamespace(campaign_id=uuid.uuid4(), user_id=USER_A), "cursor", uuid.uuid4()):
            with pytest.raises(InvalidProductAnalyticsInput):  # curseurs des deux parcours incompatibles
                function(_NoDB(), after=after)
        for kwargs in ({"now": datetime.now(timezone.utc)}, {"offset": 3}, {"campaign_id": uuid.uuid4()}):
            with pytest.raises(TypeError):
                function(_NoDB(), **kwargs)
    for forged in (pl.SessionCursor(uuid.uuid4(), datetime(2026, 11, 25), uuid.uuid4()),
                   pl.SessionCursor(str(uuid.uuid4()), datetime.now(timezone.utc), uuid.uuid4()),
                   pl.SessionCursor(uuid.uuid4(), datetime.now(timezone.utc), "x")):
        with pytest.raises(InvalidProductAnalyticsInput):
            pl.select_idle_session_candidates(_NoDB(), after=forged)
    for forged in (pl.ParticipationCursor(uuid.uuid4(), ""), pl.ParticipationCursor("c", USER_A)):
        with pytest.raises(InvalidProductAnalyticsInput):
            pl.select_purgeable_participation_candidates(_NoDB(), after=forged)
    for helper, name in ((pl._observe_session_attempt, "attempt"), (pl._observe_purge_attempt, "attempt")):
        with pytest.raises(InvalidProductAnalyticsInput):
            helper(_NoDB(), **{name: SimpleNamespace(campaign_id=uuid.uuid4(), user_id=USER_A, key=1)})
    for function in ENTRY_POINTS:
        with pytest.raises(TypeError):
            _call(function, _NoDB())
    public = {name for name in dir(pl) if not name.startswith("_") and inspect.isfunction(getattr(pl, name))
              and getattr(pl, name).__module__ == pl.__name__}
    assert public == {f.__name__ for f in SERVICE_ENTRY_POINTS}
    boundary_public = {name for name in dir(lb) if not name.startswith("_") and inspect.isfunction(getattr(lb, name))
                       and getattr(lb, name).__module__ == lb.__name__}
    assert boundary_public == {f.__name__ for f in ENTRY_POINTS}
    assert list(inspect.signature(lb.finalize_session).parameters) == ["engine", "session_id"]
    assert list(inspect.signature(lb.purge_participation).parameters) == ["engine", "campaign_id", "user_id"]
    for function in (lb.finalize_sessions_page, lb.purge_participations_page):
        assert list(inspect.signature(function).parameters) == ["engine", "limit", "after"]
    # Représentations masquées : aucun identifiant.
    for value in (session_cursor, participation_cursor, pl.SessionPage("x", (uuid.uuid4(),), session_cursor),
                  pl.ParticipationPage("x", ((uuid.uuid4(), USER_A),), None)):
        assert not UUID_PATTERN.search(repr(value)) and USER_A not in repr(value)


def test_the_boundary_validates_identifiers_limits_and_cursors_before_connecting(db):
    statements, connects = [], []
    probe = sa.create_engine(db.url, poolclass=sa.pool.NullPool)
    sa.event.listen(probe, "connect", lambda *a: connects.append(1))
    sa.event.listen(probe, "before_cursor_execute", lambda *a: statements.append(1))
    for bad in (str(uuid.uuid4()), None, 3):
        with pytest.raises(TypeError):
            lb.finalize_session(probe, session_id=bad)
        with pytest.raises(TypeError):
            lb.purge_participation(probe, campaign_id=bad, user_id=USER_A)
    for bad_user in (None, 3, b"user"):
        with pytest.raises(TypeError):
            lb.purge_participation(probe, campaign_id=uuid.uuid4(), user_id=bad_user)
    with pytest.raises(InvalidProductAnalyticsInput):
        lb.purge_participation(probe, campaign_id=uuid.uuid4(), user_id="")
    for limit in (0, 201, True, "1"):
        for function in (lb.finalize_sessions_page, lb.purge_participations_page):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(probe, limit=limit)
    with pytest.raises(InvalidProductAnalyticsInput):
        lb.finalize_sessions_page(probe, after=pl.ParticipationCursor(uuid.uuid4(), USER_A))
    with pytest.raises(InvalidProductAnalyticsInput):
        lb.purge_participations_page(probe, after=pl.SessionCursor(uuid.uuid4(), datetime.now(timezone.utc),
                                                                   uuid.uuid4()))
    assert statements == [] and connects == []
    probe.dispose()


# --------------------------------------------------------------------------
# C (sans base). Échéance de conservation des participations
# --------------------------------------------------------------------------

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _campaign_value(**overrides):
    values = {"status": "closed", "created_at": BASE, "activated_at": BASE + timedelta(hours=1),
              "closed_at": BASE + timedelta(days=20), "close_by_at": BASE + timedelta(days=30), **POLICY}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_the_participation_deadline_is_ninety_absolute_days_after_the_effective_end():
    deadline = pl.participation_retention_deadline
    # planned : aucune échéance (PR 4), activée ou non.
    assert deadline(_campaign_value(status="planned", activated_at=None, closed_at=None)) is None
    # active : échéance MAXIMALE depuis close_by_at (avant comme après close_by_at : pas d'horloge ici).
    assert deadline(_campaign_value(status="active", closed_at=None)) == BASE + timedelta(days=30) + HOURS_2160
    # closed : depuis closed_at (normale, anticipée, tardive = bornée par close_by_at par close_campaign).
    assert deadline(_campaign_value()) == BASE + timedelta(days=20) + HOURS_2160
    assert deadline(_campaign_value(closed_at=BASE + timedelta(hours=2))) == BASE + timedelta(hours=2) + HOURS_2160
    assert deadline(_campaign_value(closed_at=BASE + timedelta(days=30))) == BASE + timedelta(days=30) + HOURS_2160
    # cancelled : activée ou jamais activée.
    assert deadline(_campaign_value(status="cancelled")) == BASE + timedelta(days=20) + HOURS_2160
    assert deadline(_campaign_value(status="cancelled", activated_at=None, closed_at=BASE + timedelta(minutes=5))
                    ) == BASE + timedelta(minutes=5) + HOURS_2160
    # 90 jours = 2 160 heures ABSOLUES, quel que soit le fuseau des dates fournies (heure d'hiver à Paris).
    paris = ZoneInfo("Europe/Paris")
    local = _campaign_value(created_at=BASE.astimezone(paris), activated_at=(BASE + timedelta(hours=1)).astimezone(
        paris), closed_at=(BASE + timedelta(days=20)).astimezone(paris), close_by_at=(BASE + timedelta(
            days=30)).astimezone(paris))
    result = deadline(local)
    assert result == BASE + timedelta(days=20) + HOURS_2160 and result.tzinfo == timezone.utc
    assert result.astimezone(paris).hour != (BASE + timedelta(days=20)).astimezone(paris).hour  # heure murale ≠


@pytest.mark.parametrize("overrides, reason", [
    ({"retention_policy_version": "v0-retention-v9"}, "policy_incoherent"),
    ({"retention_days": 30}, "policy_incoherent"),
    ({"retention_days": True}, "policy_incoherent"),
    ({"measurement_policy_version": "v9"}, "policy_incoherent"),
    ({"status": "archived"}, "retention_incoherent"),
    ({"status": "closed", "closed_at": None}, "retention_incoherent"),
    ({"status": "cancelled", "closed_at": None}, "retention_incoherent"),
    ({"status": "closed", "activated_at": None}, "retention_incoherent"),
    ({"status": "active"}, "retention_incoherent"),  # active déjà clôturée
    ({"status": "active", "activated_at": None, "closed_at": None}, "retention_incoherent"),
    ({"status": "planned", "closed_at": None}, "retention_incoherent"),  # planned activée
    ({"status": "planned", "activated_at": None}, "retention_incoherent"),  # planned clôturée
    ({"close_by_at": BASE}, "retention_incoherent"),
    ({"activated_at": BASE - timedelta(seconds=1)}, "retention_incoherent"),
    ({"activated_at": BASE + timedelta(days=30), "closed_at": BASE + timedelta(days=30)}, "retention_incoherent"),
    ({"closed_at": BASE + timedelta(minutes=30)}, "retention_incoherent"),  # avant l'activation
    ({"closed_at": BASE + timedelta(days=31)}, "retention_incoherent"),  # au-delà de close_by_at
    ({"status": "cancelled", "activated_at": None, "closed_at": BASE - timedelta(days=1)}, "retention_incoherent"),
    ({"created_at": datetime(2026, 8, 1, 12)}, "retention_incoherent"),  # sans fuseau
    ({"closed_at": "2026-08-21"}, "retention_incoherent"),
])
def test_an_incoherent_policy_status_or_date_never_establishes_a_deadline(overrides, reason):
    with pytest.raises(pl.RetentionDeadlineIncoherent) as refused:
        pl.participation_retention_deadline(_campaign_value(**overrides))
    assert refused.value.reason == reason


# --------------------------------------------------------------------------
# PostgreSQL réel : outils
# --------------------------------------------------------------------------

def _sql(db, statement, **params):
    """Écrivain HORS SERVICE (scénario explicite de test)."""
    with db.begin() as conn:
        return conn.execute(sa.text(statement), params)


def _campaign(db, campaign_id):
    [row] = rows(db, f"SELECT *, xmin::text AS xmin FROM {CAMPAIGNS} WHERE id = :i", i=campaign_id)
    return row


def _deadline(db, campaign_id):
    row = _campaign(db, campaign_id)
    return pl.participation_retention_deadline(SimpleNamespace(**row))


def _participation(db, campaign_id, user=USER_A):
    found = rows(db, f"SELECT *, xmin::text AS xmin FROM {PARTICIPANTS} WHERE campaign_id = :c AND user_id = :u",
                 c=campaign_id, u=user)
    return found[0] if found else None


def _visit(db, session_id):
    found = rows(db, f"SELECT *, xmin::text AS xmin FROM {SESSIONS} WHERE id = :i", i=session_id)
    return found[0] if found else None


def _couple(db, campaign_id, user=USER_A) -> dict:
    """Participation, visites et interactions du couple (xmin compris)."""
    return {"participation": _participation(db, campaign_id, user),
            "sessions": rows(db, f"SELECT *, xmin::text AS xmin FROM {SESSIONS} WHERE campaign_id = :c AND "
                                 "user_id = :u ORDER BY id", c=campaign_id, u=user),
            "interactions": rows(db, f"SELECT *, xmin::text AS xmin FROM {INTERACTIONS} WHERE campaign_id = :c "
                                     "AND user_id = :u ORDER BY interaction_key", c=campaign_id, u=user)}


def _world(db, *, without=()) -> dict:
    """Toutes les lignes analytics (xmin compris), sauf les couples `without`."""
    world = {}
    for table in ANALYTICS_TABLES:
        found = rows(db, f"SELECT t.*, t.xmin::text AS xmin FROM {table} t ORDER BY t::text")
        world[table] = [r for r in found if (r.get("campaign_id"), r.get("user_id")) not in without]
    return world


def _count(db, table, campaign_id=None, user=USER_A):
    if campaign_id is None:
        return rows(db, f"SELECT count(*) AS n FROM {table}")[0]["n"]
    return rows(db, f"SELECT count(*) AS n FROM {table} WHERE campaign_id = :c AND user_id = :u", c=campaign_id,
                u=user)[0]["n"]


def _wave(db, user=USER_A, *, close_days=30):
    return open_wave(db, user, close_days=close_days)


def _enroll(db, campaign_id, user):
    return committed(db, lambda s: pp.enroll_at_first_eligible_login(
        s, campaign_id=campaign_id, user_id=user, authorization_recorded_at=common.utcnow(), notice_version=NOTICE))


def _close_campaign(db, campaign_id):
    committed(db, lambda s: pc.close_campaign(s, campaign_id=campaign_id))


def _cancel_campaign(db, campaign_id):
    committed(db, lambda s: pc.cancel_campaign(s, campaign_id=campaign_id))


def _withdraw(db, campaign_id, user=USER_A):
    committed(db, lambda s: pp.withdraw_participant(s, campaign_id=campaign_id, user_id=user))


def _closed_wave(db, clock, user=USER_A, *, visits=1, closes=0):
    """Campagne réelle : J0, `visits` visites (environnements distincts), une
    activité chacune, `closes` déconnexions sans visite, puis clôture."""
    campaign_id = _wave(db, user)
    for index in range(visits):
        touch(db, campaign_id, user, environment=uuid.UUID(int=index + 1))
        clock.advance(seconds=1)
    for _ in range(closes):
        close(db, campaign_id, user, environment=uuid.uuid4())
    clock.advance(hours=1)
    _close_campaign(db, campaign_id)
    return campaign_id


def finalize_one(db, session_id):
    return lb.finalize_session(db, session_id=session_id)


def purge(db, campaign_id, user=USER_A):
    return lb.purge_participation(db, campaign_id=campaign_id, user_id=user)


FINALIZED = lb.SessionOutcome(lb.FINALIZED)
ALREADY = lb.SessionOutcome(lb.ALREADY_FINALIZED)
NOT_DUE_A = lb.SessionOutcome(lb.NOT_DUE)
EXPIRED_A = lb.SessionOutcome(lb.NOT_ADMISSIBLE, pl.RETENTION_EXPIRED)
NOT_DUE_B = lb.PurgeOutcome(lb.NOT_DUE)
ABSENT_B = lb.PurgeOutcome(lb.ABSENT)


def _purged(interactions, sessions):
    return lb.PurgeOutcome(lb.PURGED, None, interactions, sessions)


def _blocked(reason):
    return lb.PurgeOutcome(lb.BLOCKED, reason)


# --------------------------------------------------------------------------
# B. Inactivité et finalisation (parcours A)
# --------------------------------------------------------------------------

def _open_visit(db, clock, user=USER_A, *, campaign_id=None, environment=LAPTOP):
    campaign_id = campaign_id or _wave(db, user)
    visit_id, _ = touch(db, campaign_id, user, environment=environment)
    return campaign_id, visit_id


@pytest.mark.parametrize("offset, expected", [(-MICRO, NOT_DUE_A), (timedelta(0), FINALIZED),
                                              (timedelta(hours=3), FINALIZED)])
def test_the_inactivity_bound_is_exact_and_a_late_finalization_carries_the_real_instant(db, clock, offset, expected):
    campaign_id, visit_id = _open_visit(db, clock)
    before, world = _visit(db, visit_id), _world(db)
    clock.set(utc(before["idle_expires_at"]) + offset)
    assert finalize_one(db, visit_id) == expected
    after = _visit(db, visit_id)
    if expected == NOT_DUE_A:
        assert after == before and _world(db) == world  # ligne identique, xmin compris
        return
    # Seules finalized_at (instant réel de traitement, jamais idle_expires_at) et end_reason changent.
    assert (utc(after["finalized_at"]), after["end_reason"]) == (clock.now, "inactivity")
    assert {k: v for k, v in after.items() if k not in ("finalized_at", "end_reason", "xmin")} == {
        k: v for k, v in before.items() if k not in ("finalized_at", "end_reason", "xmin")}
    changed = _world(db)
    changed[SESSIONS] = [r for r in changed[SESSIONS] if r["id"] != visit_id]
    world[SESSIONS] = [r for r in world[SESSIONS] if r["id"] != visit_id]
    assert changed == world  # aucune interaction, aucun événement, rien d'autre
    assert finalize_one(db, visit_id) == ALREADY and _visit(db, visit_id) == after


def test_a_regressive_clock_writes_nothing_and_never_invents_an_activity(db, clock):
    _, visit_id = _open_visit(db, clock)
    before = _visit(db, visit_id)
    clock.set(utc(before["last_activity_at"]) - MICRO)
    assert finalize_one(db, visit_id) == lb.SessionOutcome(lb.UNKNOWN, pl.CLOCK_INCOHERENT)
    assert _visit(db, visit_id) == before
    clock.set(utc(before["idle_expires_at"]))
    assert finalize_one(db, visit_id) == FINALIZED


def test_visits_ended_by_logout_by_reopening_or_already_finalized_are_left_untouched(db, clock):
    campaign_id, visit_id = _open_visit(db, clock)
    close(db, campaign_id, USER_A, environment=LAPTOP)
    logout = _visit(db, visit_id)
    assert logout["end_reason"] == "logout"
    clock.advance(hours=1)
    assert finalize_one(db, visit_id) == ALREADY and _visit(db, visit_id) == logout
    other, _ = touch(db, campaign_id, USER_A, environment=PHONE)
    clock.advance(minutes=31)
    reopened, _ = touch(db, campaign_id, USER_A, environment=PHONE)  # 2B-1 finalise l'ancienne et ouvre
    assert reopened != other and _visit(db, other)["end_reason"] == "inactivity"
    snapshot = _visit(db, other)
    assert finalize_one(db, other) == ALREADY and _visit(db, other) == snapshot


@pytest.mark.parametrize("change", ["withdrawal", "observation_ended", "closure", "cancellation",
                                    "active_after_close_by"])
def test_a_historically_legitimate_visit_is_finalized_after_withdrawal_observation_end_or_closure(db, clock,
                                                                                                    change):
    campaign_id, visit_id = _open_visit(db, clock)
    if change == "withdrawal":
        _withdraw(db, campaign_id)
    elif change == "observation_ended":
        clock.advance(days=13)
    elif change == "closure":
        clock.advance(minutes=1)
        _close_campaign(db, campaign_id)
    elif change == "cancellation":
        clock.advance(minutes=1)
        _cancel_campaign(db, campaign_id)
    else:
        clock.advance(days=31)  # active au-delà de close_by_at : fin effective = close_by_at
    clock.advance(hours=1)
    assert finalize_one(db, visit_id) == FINALIZED
    assert _visit(db, visit_id)["end_reason"] == "inactivity"


@pytest.mark.parametrize("offset, expected", [(-MICRO, FINALIZED), (timedelta(0), EXPIRED_A),
                                              (timedelta(days=1), EXPIRED_A)])
def test_no_finalization_is_written_from_the_participation_retention_deadline(db, clock, offset, expected):
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(minutes=5)
    _close_campaign(db, campaign_id)
    deadline = _deadline(db, campaign_id)
    assert deadline == utc(_campaign(db, campaign_id)["closed_at"]) + HOURS_2160
    before = _visit(db, visit_id)
    clock.set(deadline + offset)
    assert finalize_one(db, visit_id) == expected
    if expected == EXPIRED_A:
        assert _visit(db, visit_id) == before


def test_a_visit_of_an_expired_participation_whose_purge_is_blocked_is_never_finalized(db, clock):
    proof = _certified(db)  # preuve certified : bloque la purge
    [visit] = rows(db, f"SELECT id FROM {SESSIONS}")
    clock.advance(minutes=5)
    _close_campaign(db, proof.campaign_id)
    clock.set(_deadline(db, proof.campaign_id))
    assert purge(db, proof.campaign_id) == _blocked(pl.PROOF_REMAINING)
    before = _visit(db, visit["id"])
    assert finalize_one(db, visit["id"]) == EXPIRED_A and _visit(db, visit["id"]) == before


def test_out_of_contract_visits_are_conflicts_and_never_written(db, clock):
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(hours=1)
    before = _visit(db, visit_id)
    # Ouverte avant le J0 de la participation (écrivain hors service).
    _sql(db, f"UPDATE {PARTICIPANTS} SET window_start_at = window_start_at + interval '1 second', "
             "enrolled_at = enrolled_at + interval '1 second' WHERE campaign_id = :c", c=campaign_id)
    assert finalize_one(db, visit_id) == lb.SessionOutcome(lb.CONFLICT, pl.SESSION_INCOHERENT)
    assert _visit(db, visit_id) == before
    _sql(db, f"UPDATE {PARTICIPANTS} SET window_start_at = window_start_at - interval '1 second', "
             "enrolled_at = enrolled_at - interval '1 second' WHERE campaign_id = :c", c=campaign_id)
    # Campagne hors contrat (politique inconnue) : aucune échéance, aucune écriture.
    _sql(db, f"UPDATE {CAMPAIGNS} SET retention_policy_version = 'v0-retention-v9' WHERE id = :c", c=campaign_id)
    assert finalize_one(db, visit_id) == lb.SessionOutcome(lb.CONFLICT, pl.POLICY_INCOHERENT)
    _sql(db, f"UPDATE {CAMPAIGNS} SET retention_policy_version = 'v0-retention-v1' WHERE id = :c", c=campaign_id)
    assert finalize_one(db, visit_id) == FINALIZED


def test_a_visit_in_a_never_activated_campaign_is_out_of_contract(db, clock):
    campaign_id = committed(db, lambda s: pc.create_campaign(
        s, code=f"planned-{uuid.uuid4().hex[:8]}", name="Vague 0", purpose="Mesure.",
        close_by_at=common.utcnow() + timedelta(days=30), **POLICY).id)
    now = common.utcnow()
    committed(db, lambda s: pp.enroll_participant(s, campaign_id=campaign_id, user_id=USER_A, window_start_at=now,
                                                  window_end_at=now + timedelta(days=12),
                                                  authorization_recorded_at=now, notice_version=NOTICE))
    visit_id = uuid.uuid4()  # écrivain hors service : ps.touch refuse une campagne planned
    _sql(db, f"INSERT INTO {SESSIONS} (id, campaign_id, user_id, environment_digest, opened_at, last_activity_at, "
             "idle_expires_at) VALUES (:i, :c, :u, :d, :t, :t, :t + interval '30 minutes')", i=visit_id,
         c=campaign_id, u=USER_A, d="a" * 64, t=now)
    clock.advance(hours=1)
    assert finalize_one(db, visit_id) == lb.SessionOutcome(lb.CONFLICT, pl.SESSION_INCOHERENT)
    assert _visit(db, visit_id)["finalized_at"] is None
    assert purge(db, campaign_id) == lb.PurgeOutcome(lb.NOT_ADMISSIBLE, pl.DEADLINE_UNESTABLISHED)


def test_the_historical_idle_finalization_keeps_its_behaviour_and_still_writes_after_expiry(db, clock):
    """Caractérisation (L10, non corrigée) : finalize_idle_sessions finalise
    même après l'échéance de conservation ; le nouveau parcours refuse."""
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(minutes=5)
    _close_campaign(db, campaign_id)
    clock.set(_deadline(db, campaign_id) + timedelta(days=1))
    assert finalize_one(db, visit_id) == EXPIRED_A
    finalized = committed(db, lambda s: [v.id for v in ps.finalize_idle_sessions(s, campaign_id=campaign_id)])
    assert finalized == [visit_id] and utc(_visit(db, visit_id)["finalized_at"]) == clock.now


def test_the_lock_key_is_exactly_the_one_of_the_session_service():
    campaign_id, digest, interaction_key = uuid.uuid4(), ps.environment_digest(LAPTOP), uuid.uuid4()
    assert pl.session_lock_key(campaign_id, USER_A, digest) == proofs.session_lock_keys(
        campaign_id, USER_A, digest, interaction_key)[0]
    assert pl.participation_lock_key(campaign_id, USER_A) == common.identity_lock_key("participation", campaign_id,
                                                                                      USER_A)
    source = inspect.getsource(ps._lock)
    assert 'identity_lock_key("activity_session", campaign_id, user_id, digest)' in source
    assert 'identity_lock_key("participation", campaign_id, user_id)' in inspect.getsource(
        pp._lock_identity_and_read)


def test_the_postgresql_clock_is_read_after_a_lock_wait_that_crosses_the_inactivity_bound(db, monkeypatch):
    """Horloge PostgreSQL RÉELLE : la visite n'est pas échue à la sélection ;
    la finalisation attend la clé de la visite pendant le passage de
    l'échéance et finalise avec l'instant réel lu après l'attente."""
    monkeypatch.setattr(common, "utcnow", lambda: datetime.now(timezone.utc) - timedelta(minutes=29, seconds=59))
    campaign_id, visit_id = _open_visit(db, None)
    idle = utc(_visit(db, visit_id)["idle_expires_at"])
    assert committed(db, lambda s: pl.select_idle_session_candidates(s)).session_ids == ()
    holder = Holder(db)
    common.lock_identities(holder.db, pl.session_lock_key(campaign_id, USER_A, ps.environment_digest(LAPTOP)))
    contender = Background(lambda: finalize_one(db, visit_id))
    contender.start()
    _wait(lambda: waiting(db) == 1, "la finalisation n'attend pas la clé de la visite")
    _sleep_until(idle + timedelta(milliseconds=100))
    holder.rollback()
    assert contender.outcome() == FINALIZED
    assert utc(_visit(db, visit_id)["finalized_at"]) > idle


# --------------------------------------------------------------------------
# C. Conservation : échéance, fuseaux, états de campagne (PostgreSQL)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("zone", ["UTC", "Europe/Paris", "America/New_York"])
@pytest.mark.parametrize("closed_at", [datetime(2026, 8, 5, 12, tzinfo=timezone.utc),  # +90 j : heures d'hiver (UE, US)
                                       datetime(2026, 12, 28, 12, tzinfo=timezone.utc)])  # +90 j : heure d'été
def test_ninety_absolute_days_whatever_the_session_time_zone_and_the_daylight_saving_change(db, clock, zone,
                                                                                            closed_at):
    clock.set(closed_at - timedelta(days=2))
    campaign_id = _closed_wave(db, clock)
    _sql(db, f"UPDATE {CAMPAIGNS} SET closed_at = :t WHERE id = :c", t=closed_at, c=campaign_id)
    deadline = _deadline(db, campaign_id)
    assert deadline == closed_at + HOURS_2160
    zoned = sa.create_engine(db.url, poolclass=sa.pool.NullPool,
                             connect_args={"application_name": PROOF_APP, "options": f"-c TimeZone={zone}"})
    try:
        with zoned.connect() as conn:
            civil = conn.execute(sa.text("SELECT closed_at + interval '90 days' FROM product_campaigns WHERE id = :c"),
                                 {"c": campaign_id}).scalar_one()
            sql_deadline = conn.execute(sa.select(pl._potential_deadline()).where(
                ProductCampaign.id == campaign_id)).scalar_one()
        assert utc(sql_deadline) == deadline  # échéance SQL indicative : mêmes secondes absolues
        assert (utc(civil) != deadline) is (zone != "UTC")  # contre-épreuve : jours civils du fuseau
        clock.set(deadline - MICRO)
        assert committed(zoned, lambda s: pl.select_purgeable_participation_candidates(s)).participations == ()
        assert lb.purge_participation(zoned, campaign_id=campaign_id, user_id=USER_A) == NOT_DUE_B
        clock.set(deadline)
        assert committed(zoned, lambda s: pl.select_purgeable_participation_candidates(s)).participations == (
            (campaign_id, USER_A),)
        assert lb.purge_participation(zoned, campaign_id=campaign_id, user_id=USER_A) == _purged(1, 1)
    finally:
        zoned.dispose()


@pytest.mark.parametrize("scenario", ["active_before_close_by", "active_after_close_by", "closed_normally",
                                      "closed_early", "closed_late", "cancelled_activated", "cancelled_planned"])
def test_each_campaign_state_derives_its_deadline_from_its_effective_end_only(db, clock, scenario):
    start = clock.now
    if scenario == "cancelled_planned":
        campaign_id = committed(db, lambda s: pc.create_campaign(
            s, code=f"c-{uuid.uuid4().hex[:8]}", name="Vague 0", purpose="Mesure.",
            close_by_at=start + timedelta(days=30), **POLICY).id)
        committed(db, lambda s: pp.enroll_participant(
            s, campaign_id=campaign_id, user_id=USER_A, window_start_at=start, window_end_at=start + timedelta(days=12),
            authorization_recorded_at=start, notice_version=NOTICE))
        clock.advance(hours=2)
        _cancel_campaign(db, campaign_id)
        end = start + timedelta(hours=2)
    else:
        campaign_id = _wave(db)
        touch(db, campaign_id, USER_A)
        close_by = utc(_campaign(db, campaign_id)["close_by_at"])
        end = close_by
        if scenario == "closed_normally":
            clock.set(close_by - timedelta(hours=1))
            _close_campaign(db, campaign_id)
            end = clock.now
        elif scenario == "closed_early":
            clock.advance(days=2)
            _close_campaign(db, campaign_id)
            end = clock.now
        elif scenario == "closed_late":
            clock.set(close_by + timedelta(days=10))  # retard de traitement : jamais une prolongation
            _close_campaign(db, campaign_id)
        elif scenario == "cancelled_activated":
            clock.advance(days=3)
            _cancel_campaign(db, campaign_id)
            end = clock.now
        elif scenario == "active_after_close_by":
            clock.set(close_by + timedelta(days=5))
    assert _deadline(db, campaign_id) == end + HOURS_2160
    if scenario == "active_before_close_by":
        clock.advance(days=1)
        assert committed(db, lambda s: pl.select_purgeable_participation_candidates(s)).participations == ()
        assert purge(db, campaign_id) == NOT_DUE_B  # jamais purgeable avant close_by_at (et 90 jours)
    clock.set(end + HOURS_2160 - MICRO)
    assert purge(db, campaign_id) == NOT_DUE_B and _participation(db, campaign_id) is not None
    clock.set(end + HOURS_2160)
    assert purge(db, campaign_id).outcome == lb.PURGED and _participation(db, campaign_id) is None


def test_a_planned_campaign_with_participants_is_never_selected_nor_purged(db, clock):
    now = clock.now
    campaign_id = committed(db, lambda s: pc.create_campaign(
        s, code=f"p-{uuid.uuid4().hex[:8]}", name="Vague 0", purpose="Mesure.", close_by_at=now + timedelta(days=30),
        **POLICY).id)
    committed(db, lambda s: pp.enroll_participant(s, campaign_id=campaign_id, user_id=USER_A, window_start_at=now,
                                                  window_end_at=now + timedelta(days=12),
                                                  authorization_recorded_at=now, notice_version=NOTICE))
    clock.advance(days=400)
    before = _couple(db, campaign_id)
    assert committed(db, lambda s: pl.select_purgeable_participation_candidates(s)).participations == ()
    assert purge(db, campaign_id) == lb.PurgeOutcome(lb.NOT_ADMISSIBLE, pl.DEADLINE_UNESTABLISHED)
    assert _couple(db, campaign_id) == before  # PR 4


ANOMALIES = {
    "cancelled_without_closed_at": ("UPDATE {t} SET status = 'cancelled', closed_at = NULL", "retention_incoherent"),
    "active_with_closed_at": ("UPDATE {t} SET closed_at = activated_at", "retention_incoherent"),
    "unknown_retention_policy": ("UPDATE {t} SET retention_policy_version = 'v9'", "policy_incoherent"),
    "retention_days_mismatch": ("UPDATE {t} SET retention_days = 30000", "policy_incoherent"),
    "unknown_measurement_policy": ("UPDATE {t} SET measurement_policy_version = 'v9'", "policy_incoherent"),
    "close_by_at_equals_creation": ("UPDATE {t} SET close_by_at = created_at, activated_at = created_at",
                                    "retention_incoherent"),
    "activated_at_close_by": ("UPDATE {t} SET status = 'closed', activated_at = close_by_at, closed_at = close_by_at",
                              "retention_incoherent"),
    "cancelled_never_activated_closed_before_creation": (
        "UPDATE {t} SET status = 'cancelled', activated_at = NULL, closed_at = created_at - interval '1 day'",
        "retention_incoherent"),
    "planned_already_activated": ("UPDATE {t} SET status = 'planned'", "retention_incoherent"),
}


@pytest.mark.parametrize("anomaly", sorted(ANOMALIES))
def test_an_incoherent_campaign_is_selected_immediately_and_reported_as_a_conflict(db, clock, anomaly):
    """Toute incohérence admise par les CHECK de 0022 et refusée par
    l'échéance est rendue par la sélection, quelle que soit l'horloge : elle
    n'est jamais rendue invisible."""
    statement, reason = ANOMALIES[anomaly]
    campaign_id, visit_id = _open_visit(db, clock)
    healthy = _wave(db, USER_B)  # campagne saine, non échue : jamais sélectionnée
    _sql(db, statement.format(t=CAMPAIGNS) + " WHERE id = :c", c=campaign_id)
    clock.advance(hours=1)
    before = _couple(db, campaign_id)
    page = committed(db, lambda s: pl.select_purgeable_participation_candidates(s))
    assert page.participations == ((campaign_id, USER_A),)
    assert purge(db, campaign_id) == lb.PurgeOutcome(lb.CONFLICT, reason)
    assert finalize_one(db, visit_id).outcome == lb.CONFLICT
    assert _couple(db, campaign_id) == before and _participation(db, healthy, USER_B) is not None


def test_the_selection_covers_every_campaign_anomaly_that_the_deadline_refuses():
    """Couverture : chaque branche de refus de participation_retention_deadline
    a une clause correspondante dans la sélection (comparaison des sources)."""
    selection = inspect.getsource(pl._campaign_anomaly)
    for clause in ("known_policy", "measurement_policy_version.in_(MEASUREMENT_POLICIES)",
                   "status.in_(CAMPAIGN_STATUSES)", "close_by_at <= c.created_at", "activated_at >= c.close_by_at",
                   "activated_at < c.created_at", "closed_at < c.created_at", "closed_at > c.close_by_at",
                   "closed_at < c.activated_at", "c.status == PLANNED", "c.status == ACTIVE", "c.status == CLOSED",
                   "c.status == CANCELLED"):
        assert clause in selection, clause


# --------------------------------------------------------------------------
# D. Participations et dénominateurs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["enrolled", "withdrawn", "completed"])
@pytest.mark.parametrize("activity", ["none", "visits"])
def test_every_participant_is_kept_until_the_deadline_whatever_its_status_or_activity(db, clock, status, activity):
    campaign_id = _closed_wave(db, clock, visits=2 if activity == "visits" else 0, closes=1 if activity == "visits"
                               else 0)
    if status == "withdrawn":
        _withdraw(db, campaign_id)
    elif status == "completed":
        _sql(db, f"UPDATE {PARTICIPANTS} SET participation_status = 'completed' WHERE campaign_id = :c",
             c=campaign_id)  # aucun service n'écrit completed (L12) : écrivain hors service
    other = _wave(db, USER_B)
    deadline = _deadline(db, campaign_id)
    world = _world(db)
    for instant in (clock.now + timedelta(days=1), deadline - timedelta(days=1), deadline - MICRO):
        clock.set(instant)
        assert purge(db, campaign_id) == NOT_DUE_B
        assert lb.purge_participations_page(db).examined == 0
        assert _world(db) == world  # préservation intégrale du dénominateur (xmin compris)
    clock.set(deadline)
    expected = _purged(3, 2) if activity == "visits" else _purged(0, 0)
    assert purge(db, campaign_id) == expected
    assert _world(db) == {table: [r for r in found if (r.get("campaign_id"), r.get("user_id")) != (
        campaign_id, USER_A)] for table, found in world.items()}
    assert _participation(db, other, USER_B) is not None


def test_before_the_deadline_nothing_is_locked_beyond_the_campaign_and_dependencies_are_irrelevant(db, clock):
    """Contrôle préliminaire (rang 4) : avant l'échéance, not_due même avec une
    preuve résiduelle ; ni dépendance consultée, ni visite ni interaction
    verrouillée."""
    proof = _certified(db)
    clock.advance(minutes=5)
    _close_campaign(db, proof.campaign_id)
    clock.set(_deadline(db, proof.campaign_id) - MICRO)
    seen, remove = _statements(db)
    try:
        assert purge(db, proof.campaign_id) == NOT_DUE_B
    finally:
        remove()
    assert not [s for s in seen if REGISTRY in s or ATTRIBUTIONS in s or EVENTS in s]
    assert not [s for s in seen if "FOR UPDATE" in s and (SESSIONS in s or INTERACTIONS in s)]


# --------------------------------------------------------------------------
# E. Dépendances physiques et logiques
# --------------------------------------------------------------------------

def _expire_campaign(db, clock, campaign_id):
    clock.advance(minutes=5)
    if _campaign(db, campaign_id)["status"] == "active":
        _close_campaign(db, campaign_id)
    clock.set(_deadline(db, campaign_id))


def _proof_in_state(db, clock, state):
    if state in ("certified", "recorded"):
        proof = _certified(db)
        if state == "recorded":
            clock.advance(seconds=1)
            assert finalize(db, proof.proof_id).state == fz.RECORDED
        return proof
    op = operation(USER_A)
    origin = open_origin(db, op, campaign_id=_wave(db))
    pending = register(db, origin, op)
    proof = SimpleNamespace(proof_id=pending.proof_id, campaign_id=origin.campaign_id, origin=origin, op=op)
    if state == "indeterminate":
        assert settle(db, origin, op, pending.proof_id).state == proofs.INDETERMINATE
    elif state == "rejected":
        clock.advance(days=13)  # certification après J+12 : rejected / window_expired
        assert certify(db, origin, op, pending.proof_id).state == proofs.REJECTED
    return proof


@pytest.mark.parametrize("state", ["pending", "certified", "recorded", "rejected", "indeterminate"])
def test_any_residual_proof_blocks_the_purge_and_is_never_resolved_nor_deleted(db, clock, state):
    proof = _proof_in_state(db, clock, state)
    _expire_campaign(db, clock, proof.campaign_id)
    registry_before, world = rows(db, f"SELECT *, xmin::text AS xmin FROM {REGISTRY}"), _world(db)
    assert purge(db, proof.campaign_id) == _blocked(pl.PROOF_REMAINING)
    page = lb.purge_participations_page(db)
    assert (page.examined, page.blocked, page.purged) == (1, 1, 0)
    assert rows(db, f"SELECT *, xmin::text AS xmin FROM {REGISTRY}") == registry_before and _world(db) == world


def test_a_late_proof_resolution_never_moves_the_participation_deadline(db, clock):
    proof = _proof_in_state(db, clock, "pending")
    _expire_campaign(db, clock, proof.campaign_id)
    deadline = _deadline(db, proof.campaign_id)
    clock.advance(days=3)
    assert expire(db, proof.proof_id).state == proofs.INDETERMINATE  # résolution TARDIVE (instant réel)
    assert _deadline(db, proof.campaign_id) == deadline
    assert purge(db, proof.campaign_id) == _blocked(pl.PROOF_REMAINING)  # 168 h : arbitrage PR 4 / PR 5
    clock.advance(hours=168)
    assert committed(db, lambda s: prl.purge_resolved_proof(s, proof_id=proof.proof_id)).outcome == prl.PURGED
    assert purge(db, proof.campaign_id).outcome == lb.PURGED


def test_a_residual_attribution_blocks_the_purge_after_the_proof_purge(db, clock):
    proof = _proof_in_state(db, clock, "recorded")
    clock.advance(hours=169)
    assert committed(db, lambda s: prl.purge_resolved_proof(s, proof_id=proof.proof_id)).outcome == prl.PURGED
    _expire_campaign(db, clock, proof.campaign_id)
    # L'événement certifié est conservé 90 jours depuis occurred_at : il est échu avant la participation.
    assert purge(db, proof.campaign_id) == _blocked(pl.ATTRIBUTION_REMAINING)
    [event] = rows(db, f"SELECT id FROM {EVENTS}")
    from core import _activity_event_lifecycle as eb
    assert eb.expire_event(db, event_id=event["id"]) == eb.EventOutcome(eb.PURGED)  # PR 2 d'abord
    assert purge(db, proof.campaign_id).outcome == lb.PURGED


def _two_campaigns(db, clock):
    """USER_A inscrit à A puis à B ; A est clôturée, B reste active."""
    a = _wave(db)
    visit_a, _ = touch(db, a, USER_A)
    b = _wave(db)
    return a, b, visit_a


def _historical_event(db, *, visit_id=None):
    return _record(db, USER_A, event_type="coach_used", surface="coach", source="client_signal", visit_id=visit_id)


def _scopes_of(db, event_id):
    [row] = rows(db, f"SELECT retention_scopes FROM {EVENTS} WHERE id = :i", i=event_id)
    return {s.campaign_id for s in retention.parse_retention_scopes(row["retention_scopes"])}


def test_a_logical_scope_of_the_campaign_blocks_whether_single_multiple_or_already_expired(db, clock):
    a, b, _ = _two_campaigns(db, clock)
    event = _historical_event(db)  # finalités A et B
    assert _scopes_of(db, event.id) == {a, b}
    _expire_campaign(db, clock, a)
    clock.advance(days=1)  # finalité A échue (occurred_at + 90 j), toujours présente : bloquante
    assert purge(db, a) == _blocked(pl.EVENT_SCOPE_REMAINING)
    # Seule la PR 2 traite les finalités (ici toutes échues : l'événement est supprimé par elle).
    from core import _activity_event_lifecycle as eb
    assert eb.expire_event(db, event_id=event.id) == eb.EventOutcome(eb.PURGED)
    assert purge(db, a).outcome == lb.PURGED


def test_an_event_kept_only_for_another_campaign_keeps_its_historical_visit_and_never_blocks(db, clock):
    a, b, visit_a = _two_campaigns(db, clock)
    _withdraw(db, a)  # l'événement suivant n'est rattaché qu'à B
    event = _historical_event(db, visit_id=visit_a)  # visit_id historique d'une visite de A
    assert _scopes_of(db, event.id) == {b}
    _expire_campaign(db, clock, a)
    before = rows(db, f"SELECT *, xmin::text AS xmin FROM {EVENTS} WHERE id = :i", i=event.id)
    assert purge(db, a) == _purged(1, 1)
    assert rows(db, f"SELECT *, xmin::text AS xmin FROM {EVENTS} WHERE id = :i", i=event.id) == before
    assert _visit(db, visit_a) is None  # le visit_id historique ne prolonge rien
    # Non-régression du contrôle d'intégrité de la PR 2 : l'événement de B reste valide.
    assert committed(db, lambda s: el.inspect_event_integrity(s, event_id=event.id)) == el.IntegrityDecision(
        el.VALID)


def _forge_document(db, event_id, document):
    _sql(db, f"UPDATE {EVENTS} SET retention_scopes = CAST(:d AS jsonb) WHERE id = :i", i=event_id,
         d=json.dumps(document))


def _b_only_event(db, clock):
    a, b, _ = _two_campaigns(db, clock)
    _withdraw(db, a)
    event = _historical_event(db)
    [row] = rows(db, f"SELECT retention_scopes FROM {EVENTS} WHERE id = :i", i=event.id)
    return a, b, event, row["retention_scopes"]


@pytest.mark.parametrize("form, expected", [
    ("illegible_item", "conflict"), ("illegible_missing_id", "conflict"), ("illegible_bad_uuid", "conflict"),
    ("non_canonical_b_only", "purged"), ("non_canonical_names_a", "conflict"),
    ("independent_b_anomaly", "purged"),
])
def test_only_pertinent_json_anomalies_prevent_the_purge(db, clock, form, expected):
    a, b, event, document = _b_only_event(db, clock)
    [scope] = document["scopes"]
    forged = {
        "illegible_item": {"version": "retention-scopes-v1", "scopes": ["opaque"]},
        "illegible_missing_id": {"version": "retention-scopes-v1", "scopes": [{"kind": "campaign"}]},
        "illegible_bad_uuid": {"version": "retention-scopes-v1", "scopes": [{**scope, "campaign_id": "not-a-uuid"}]},
        "non_canonical_b_only": {"version": "retention-scopes-v1", "scopes": [
            {**scope, "campaign_id": scope["campaign_id"].upper()}]},
        "non_canonical_names_a": {"version": "retention-scopes-v1", "scopes": [
            scope, {**scope, "campaign_id": str(a).upper()}]},
        # Anomalie d'échéance propre à B (au-delà de l'autorisation) : document canonique interprétable.
        "independent_b_anomaly": {"version": "retention-scopes-v1", "scopes": [
            {**scope, "expires_at": "2099-01-01T00:00:00.000000Z"}]},
    }[form]
    _forge_document(db, event.id, forged)
    _expire_campaign(db, clock, a)
    before = rows(db, f"SELECT *, xmin::text AS xmin FROM {EVENTS}")
    outcome = purge(db, a)
    if expected == "conflict":
        assert outcome == lb.PurgeOutcome(lb.CONFLICT, pl.RETENTION_INCOHERENT)
        assert _participation(db, a) is not None
    else:
        assert outcome.outcome == lb.PURGED
    assert rows(db, f"SELECT *, xmin::text AS xmin FROM {EVENTS}") == before  # jamais modifié ni supprimé


def test_events_of_other_users_never_block_and_the_scan_is_progressive(db, clock):
    a = _closed_wave(db, clock)
    b = _wave(db, USER_B)
    events = [_record(db, USER_B, event_type="coach_used", surface="coach", source="client_signal")
              for _ in range(3)]
    assert all(_scopes_of(db, e.id) == {b} for e in events)
    clock.set(_deadline(db, a))
    seen, remove = _statements(db)
    try:
        assert purge(db, a).outcome == lb.PURGED
    finally:
        remove()
    scans = [s for s in seen if EVENTS in s]
    assert scans and all("WHERE product_activity_events.user_id =" in s for s in scans)  # même utilisateur seulement
    assert pl.EVENT_SCAN_BATCH == 500
    source = inspect.getsource(pl._event_scope_problem)
    # V0-T08 Lot 2C PR 4 (nominatif) : classification R5 bis extraite vers
    # retention.campaign_reference, qui applique d'abord parse_retention_scopes.
    assert "yield_per=EVENT_SCAN_BATCH" in source and "retention.campaign_reference" in source
    assert "parse_retention_scopes(document)" in inspect.getsource(retention.campaign_reference)


# --------------------------------------------------------------------------
# F. Atomicité
# --------------------------------------------------------------------------

def _rich(db, clock):
    """A : deux visites (dont une finalisée par déconnexion), une ouverte,
    trois environnements, deux déconnexions sans visite ; B (même campagne)
    et A dans une autre campagne : témoins."""
    campaign_id = _wave(db)
    _enroll(db, campaign_id, USER_B)
    touch(db, campaign_id, USER_A, environment=LAPTOP)
    touch(db, campaign_id, USER_A, environment=LAPTOP)
    close(db, campaign_id, USER_A, environment=LAPTOP)
    touch(db, campaign_id, USER_A, environment=PHONE)
    close(db, campaign_id, USER_A, environment=uuid.uuid4())
    close(db, campaign_id, USER_A, environment=uuid.uuid4())
    touch(db, campaign_id, USER_B, environment=LAPTOP)
    other = _wave(db)
    touch(db, other, USER_A)
    _expire_campaign(db, clock, campaign_id)
    return campaign_id, other


def test_a_full_participation_is_deleted_atomically_with_its_open_and_closed_visits(db, clock):
    campaign_id, other = _rich(db, clock)
    couple = _couple(db, campaign_id)
    assert len(couple["sessions"]) == 2 and len(couple["interactions"]) == 6
    assert [s["finalized_at"] is None for s in couple["sessions"]].count(True) == 1  # une visite encore ouverte
    assert len([i for i in couple["interactions"] if i["session_id"] is None]) == 2
    world = _world(db, without={(campaign_id, USER_A)})
    seen, remove = _statements(db)
    try:
        assert purge(db, campaign_id) == _purged(6, 2)
    finally:
        remove()
    assert _couple(db, campaign_id) == {"participation": None, "sessions": [], "interactions": []}
    assert _world(db) == world  # B, l'autre campagne, la campagne elle-même : xmin compris
    writes = [s.split(" WHERE")[0] for s in seen if s.startswith(("SAVEPOINT", "RELEASE", "DELETE", "UPDATE",
                                                                   "INSERT", "ROLLBACK TO"))]
    assert [w.split(" ")[0] + (" " + w.split(" ")[2] if w.startswith("DELETE") else "") for w in writes] == [
        "SAVEPOINT", f"DELETE {INTERACTIONS}", f"DELETE {SESSIONS}", f"DELETE {PARTICIPANTS}", "RELEASE"]
    locks = [s for s in seen if "FOR UPDATE" in s or "FOR SHARE" in s or "pg_advisory" in s]
    assert [("advisory" if "pg_advisory" in s else re.findall(r"FROM (\w+)", s)[0]) for s in locks] == [
        "advisory", PARTICIPANTS, CAMPAIGNS, SESSIONS, INTERACTIONS]  # ordre imposé, une seule clé
    assert "ORDER BY product_activity_sessions.id FOR UPDATE" in locks[3]
    assert "ORDER BY product_activity_session_interactions.interaction_key FOR UPDATE" in locks[4]
    assert not [s for s in seen if REGISTRY in s and "FOR" in s] and not [s for s in seen if ATTRIBUTIONS in s
                                                                          and "FOR" in s]


def _fail_on(engine, prefix):
    def boom(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(prefix):
            raise RuntimeError("panne injectée")
    sa.event.listen(engine, "after_cursor_execute", boom)
    return lambda: sa.event.remove(engine, "after_cursor_execute", boom)


@pytest.mark.parametrize("prefix", [f"DELETE FROM {INTERACTIONS}", f"DELETE FROM {SESSIONS}",
                                    f"DELETE FROM {PARTICIPANTS}", "RELEASE SAVEPOINT"])
def test_a_failure_after_any_delete_or_at_the_savepoint_release_leaves_nothing(db, clock, prefix):
    campaign_id, _ = _rich(db, clock)
    world = _world(db)
    remove = _fail_on(db, prefix)
    try:
        assert purge(db, campaign_id) == lb.PurgeOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)
    finally:
        remove()
    assert _world(db) == world
    assert purge(db, campaign_id) == _purged(6, 2)


@pytest.mark.parametrize("which", ["interactions", "sessions", "participation"])
def test_an_unexpected_affected_row_count_cancels_the_whole_savepoint(db, clock, monkeypatch, which):
    campaign_id, _ = _rich(db, clock)
    world = _world(db)
    real_lock, real_same = pl._lock_descendants, pl._same_participation
    if which == "participation":
        monkeypatch.setattr(pl, "_same_participation", lambda p: real_same(p) + (sa.false(),))
    else:
        target = pl.ProductActivitySessionInteraction if which == "interactions" else pl.ProductActivitySession
        monkeypatch.setattr(pl, "_lock_descendants", lambda db_, model, *a: real_lock(db_, model, *a) + (
            1 if model is target else 0))
    assert purge(db, campaign_id) == lb.PurgeOutcome(lb.CONFLICT, pl.WRITE_UNCONFIRMED)
    assert _world(db) == world
    monkeypatch.setattr(pl, "_lock_descendants", real_lock)
    monkeypatch.setattr(pl, "_same_participation", real_same)
    assert purge(db, campaign_id) == _purged(6, 2)


def test_a_foreign_key_violation_inside_the_savepoint_is_a_conflict_and_nothing_is_deleted(db, clock, monkeypatch):
    proof = _proof_in_state(db, clock, "pending")
    _expire_campaign(db, clock, proof.campaign_id)
    world = _world(db)
    monkeypatch.setattr(pl, "_dependency_problem", lambda *a: None)  # contrôle neutralisé : la FK reste la défense
    assert purge(db, proof.campaign_id) == lb.PurgeOutcome(lb.CONFLICT, pl.DEPENDENCY_CHANGED)
    assert _world(db) == world


def test_a_participation_without_descendants_is_purged_alone(db, clock):
    campaign_id = _closed_wave(db, clock, visits=0)
    _enroll_b = _wave(db, USER_B)
    clock.set(_deadline(db, campaign_id))
    world = _world(db, without={(campaign_id, USER_A)})
    assert purge(db, campaign_id) == _purged(0, 0)
    assert _world(db) == world and _participation(db, _enroll_b, USER_B) is not None
    assert purge(db, campaign_id) == ABSENT_B


def test_the_finalization_writes_exactly_one_conditional_update_in_one_savepoint(db, clock):
    _, visit_id = _open_visit(db, clock)
    clock.advance(hours=1)
    seen, remove = _statements(db)
    try:
        assert finalize_one(db, visit_id) == FINALIZED
    finally:
        remove()
    writes = [s for s in seen if s.startswith(("SAVEPOINT", "RELEASE", "DELETE", "UPDATE", "INSERT"))]
    assert [w.split(" ")[0] for w in writes] == ["SAVEPOINT", "UPDATE", "RELEASE"]
    assert writes[1].startswith(f"UPDATE {SESSIONS} SET finalized_at=") and "end_reason=" in writes[1]
    assert "finalized_at IS NULL" in writes[1] and "idle_expires_at <=" in writes[1]
    locks = [s for s in seen if "FOR UPDATE" in s or "FOR SHARE" in s or "pg_advisory" in s]
    assert [("advisory" if "pg_advisory" in s else s.split("FROM ")[1].split(" ")[0] + (
        " share" if "FOR SHARE" in s else " update")) for s in locks] == [
        "advisory", f"{PARTICIPANTS} share", f"{CAMPAIGNS} share", f"{SESSIONS} update"]


# --------------------------------------------------------------------------
# G. Concurrence (PostgreSQL réel)
# --------------------------------------------------------------------------

def _expired_couple(db, clock, *, visits=1):
    campaign_id = _closed_wave(db, clock, visits=visits)
    clock.set(_deadline(db, campaign_id))
    return campaign_id


def _hold(db, action):
    """Transaction tierce qui exécute `action` et garde ses verrous."""
    return Holder(db, action)


def _waits_then(db, holder, contender_fn, *, commit=True, count=1, message="attente attendue"):
    contender = Background(contender_fn)
    contender.start()
    _wait(lambda: waiting(db) >= count, message)
    holder.commit() if commit else holder.rollback()
    return contender.outcome()


def _visit_key(campaign_id, user=USER_A, environment=LAPTOP):
    return pl.session_lock_key(campaign_id, user, ps.environment_digest(environment))


def test_two_simultaneous_finalizations_write_once(db, clock):
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(hours=1)
    holder = Holder(db)
    common.lock_identities(holder.db, _visit_key(campaign_id))
    first, second = Background(lambda: finalize_one(db, visit_id)), Background(lambda: finalize_one(db, visit_id))
    first.start(), second.start()
    _wait(lambda: waiting(db) == 2, "les deux finalisations n'attendent pas la clé")
    holder.rollback()
    assert sorted([first.outcome().outcome, second.outcome().outcome]) == ["already_finalized", "finalized"]


def test_two_simultaneous_purges_delete_once(db, clock):
    campaign_id = _expired_couple(db, clock)
    holder = Holder(db)
    common.lock_identities(holder.db, pl.participation_lock_key(campaign_id, USER_A))
    first, second = Background(lambda: purge(db, campaign_id)), Background(lambda: purge(db, campaign_id))
    first.start(), second.start()
    _wait(lambda: waiting(db) == 2, "les deux purges n'attendent pas la clé")
    holder.rollback()
    assert sorted([first.outcome().outcome, second.outcome().outcome]) == ["absent", "purged"]


@pytest.mark.parametrize("first", ["purge", "finalization"])
def test_a_finalization_and_a_purge_are_serialized_in_both_orders(db, clock, first):
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(minutes=5)
    _close_campaign(db, campaign_id)
    deadline = _deadline(db, campaign_id)
    if first == "purge":
        clock.set(deadline)
        holder = _hold(db, lambda s: pl.purge_participation(s, campaign_id=campaign_id, user_id=USER_A))
        assert holder.value.outcome == pl.PURGED
        assert _waits_then(db, holder, lambda: finalize_one(db, visit_id)) == lb.SessionOutcome(lb.ABSENT)
    else:
        clock.set(deadline - timedelta(seconds=1))
        holder = _hold(db, lambda s: pl.finalize_idle_session(s, session_id=visit_id))
        assert holder.value.outcome == pl.FINALIZED
        clock.set(deadline)
        assert _waits_then(db, holder, lambda: purge(db, campaign_id)) == _purged(1, 1)


@pytest.mark.parametrize("first", ["touch", "finalization"])
def test_a_new_activity_and_a_finalization_are_serialized_by_the_visit_key(db, clock, first):
    campaign_id, visit_id = _open_visit(db, clock)
    if first == "touch":
        clock.advance(minutes=29)
        holder = _hold(db, lambda s: ps.touch_activity_session(s, campaign_id=campaign_id, user_id=USER_A,
                                                               environment_key=LAPTOP, interaction_key=uuid.uuid4()))
        clock.advance(minutes=2)  # l'ancienne échéance est franchie ; la prolongation la repousse
        assert _waits_then(db, holder, lambda: finalize_one(db, visit_id)) == NOT_DUE_A
    else:
        clock.advance(minutes=31)
        holder = _hold(db, lambda s: pl.finalize_idle_session(s, session_id=visit_id))
        outcome = _waits_then(db, holder, lambda: committed(db, lambda s: ps.touch_activity_session(
            s, campaign_id=campaign_id, user_id=USER_A, environment_key=LAPTOP, interaction_key=uuid.uuid4()).outcome))
        assert outcome == ps.OPENED and _visit(db, visit_id)["end_reason"] == "inactivity"


@pytest.mark.parametrize("contender", ["logout", "historical"])
def test_a_concurrent_logout_or_historical_finalization_is_seen_after_the_wait(db, clock, contender):
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(minutes=31)
    if contender == "logout":
        holder = _hold(db, lambda s: ps.close_activity_session(s, campaign_id=campaign_id, user_id=USER_A,
                                                               environment_key=LAPTOP, interaction_key=uuid.uuid4()))
    else:
        holder = _hold(db, lambda s: ps.finalize_idle_sessions(s, campaign_id=campaign_id))
    assert _waits_then(db, holder, lambda: finalize_one(db, visit_id)) == ALREADY


@pytest.mark.parametrize("first", ["admission", "purge"])
def test_a_repeated_admission_and_a_purge_are_serialized_by_the_participation_key(db, clock, first):
    campaign_id = _expired_couple(db, clock)
    [participation] = rows(db, f"SELECT * FROM {PARTICIPANTS} WHERE campaign_id = :c", c=campaign_id)

    def login(s):
        return pp.enroll_at_first_eligible_login(s, campaign_id=campaign_id, user_id=USER_A,
                                                 authorization_recorded_at=participation["authorization_recorded_at"],
                                                 notice_version=NOTICE)
    if first == "admission":
        holder = _hold(db, login)
        assert holder.value[1] is False  # restitution historique (L10)
        assert _waits_then(db, holder, lambda: purge(db, campaign_id)) == _purged(1, 1)
    else:
        holder = _hold(db, lambda s: pl.purge_participation(s, campaign_id=campaign_id, user_id=USER_A))

        def relogin():
            try:
                return committed(db, login)
            except ParticipationRefused as refused:
                return refused
        assert isinstance(_waits_then(db, holder, relogin), ParticipationRefused)  # jamais recréée
    assert _participation(db, campaign_id) is None


@pytest.mark.parametrize("first", ["withdrawal", "purge"])
def test_a_withdrawal_and_a_purge_are_serialized_on_the_participation_row(db, clock, first):
    campaign_id = _expired_couple(db, clock)
    if first == "withdrawal":
        holder = _hold(db, lambda s: pp.withdraw_participant(s, campaign_id=campaign_id, user_id=USER_A))
        assert _waits_then(db, holder, lambda: purge(db, campaign_id)) == _purged(1, 1)  # statut sans effet
    else:
        holder = _hold(db, lambda s: pl.purge_participation(s, campaign_id=campaign_id, user_id=USER_A))

        def withdraw():
            try:
                return _withdraw(db, campaign_id)
            except ParticipationNotFound as missing:
                return missing
        assert isinstance(_waits_then(db, holder, withdraw), ParticipationNotFound)


@pytest.mark.parametrize("first", ["closure", "purge"])
def test_a_campaign_closure_and_a_purge_are_serialized_on_the_campaign_row(db, clock, first):
    campaign_id = _wave(db)
    touch(db, campaign_id, USER_A)
    clock.set(utc(_campaign(db, campaign_id)["close_by_at"]) + HOURS_2160)  # restée active : fin = close_by_at
    if first == "closure":
        holder = _hold(db, lambda s: pc.close_campaign(s, campaign_id=campaign_id))
        assert _waits_then(db, holder, lambda: purge(db, campaign_id)) == _purged(1, 1)
    else:
        holder = _hold(db, lambda s: pl.purge_participation(s, campaign_id=campaign_id, user_id=USER_A))
        _waits_then(db, holder, lambda: _close_campaign(db, campaign_id))
    assert _campaign(db, campaign_id)["status"] == "closed" and _participation(db, campaign_id) is None


@pytest.mark.parametrize("first", ["record_event", "purge"])
def test_a_2a_event_writer_and_a_purge_are_serialized_on_the_participation(db, clock, first):
    a, b, _ = _two_campaigns(db, clock)
    occurred = clock.now  # dans la fenêtre de A et de B
    clock.advance(minutes=5)
    _close_campaign(db, a)
    clock.set(_deadline(db, a) - timedelta(days=2))
    b_late = _wave(db)  # campagne active à l'instant de l'écriture
    clock.set(_deadline(db, a))
    kwargs = {"user_id": USER_A, "visit_id": uuid.uuid4(), "idempotency_key": uuid.uuid4(),
              "event_type": "coach_used", "surface": "coach", "executed_route": None, "source": "client_signal",
              "source_operation_kind": None, "source_operation_id": None, "outcome": "succeeded",
              "delivery_status": "not_applicable", "occurred_at": occurred, "resolved_at": occurred,
              "delivered_at": None}
    del b, b_late

    def write(s):
        try:
            return pe.record_event(s, **kwargs)
        except EventNotAuthorized as refused:
            return refused
    if first == "record_event":
        holder = _hold(db, write)  # verrouille la participation A en partage (fenêtre couvrant occurred_at)
        assert isinstance(holder.value, EventNotAuthorized)  # A close, B expirée pour cet instant
        assert _waits_then(db, holder, lambda: purge(db, a)) == _purged(1, 1)
    else:
        holder = _hold(db, lambda s: pl.purge_participation(s, campaign_id=a, user_id=USER_A))
        assert isinstance(_waits_then(db, holder, lambda: committed(db, write)), EventNotAuthorized)
    assert _count(db, EVENTS) == 0


def test_a_block_b_registration_is_serialized_with_the_purge_and_then_blocks_it(db, clock):
    op = operation(USER_A)
    origin = open_origin(db, op, campaign_id=_wave(db))
    _expire_campaign(db, clock, origin.campaign_id)
    holder = _hold(db, lambda s: proofs.register_pending(s, origin=origin, operation=op))
    assert holder.value.written is False  # hors observation : B n'écrit rien (participation verrouillée en partage)
    assert _waits_then(db, holder, lambda: purge(db, origin.campaign_id)) == _purged(1, 1)
    assert _count(db, REGISTRY) == 0
    # Inverse : la purge tient la participation ; B attend, puis ne trouve plus rien à rattacher.
    other_op = operation(USER_A)
    other = open_origin(db, other_op, campaign_id=_wave(db))
    _expire_campaign(db, clock, other.campaign_id)
    holder = _hold(db, lambda s: pl.purge_participation(s, campaign_id=other.campaign_id, user_id=USER_A))
    late = _waits_then(db, holder, lambda: register(db, other, other_op))
    assert late.written is False and _count(db, REGISTRY) == 0


def test_a_block_c_finalization_is_serialized_with_the_purge(db, clock):
    proof = _certified(db)
    _expire_campaign(db, clock, proof.campaign_id)
    holder = _hold(db, lambda s: fz.finalize_certified_proof(s, proof_id=proof.proof_id))
    assert _waits_then(db, holder, lambda: purge(db, proof.campaign_id)) == _blocked(pl.PROOF_REMAINING)


@pytest.mark.parametrize("commit", [True, False])
def test_a_pr1_purge_in_progress_never_makes_the_participation_purge_wait(db, clock, commit):
    proof = _proof_in_state(db, clock, "indeterminate")
    _expire_campaign(db, clock, proof.campaign_id)
    holder = _hold(db, lambda s: prl.purge_resolved_proof(s, proof_id=proof.proof_id))
    assert holder.value.outcome == prl.PURGED
    assert purge(db, proof.campaign_id) == _blocked(pl.PROOF_REMAINING)  # aucune attente : état committé constaté
    holder.commit() if commit else holder.rollback()
    expected = _purged(1, 1) if commit else _blocked(pl.PROOF_REMAINING)
    assert purge(db, proof.campaign_id) == expected


def test_a_pr2_purge_holding_the_participation_makes_the_purge_wait_then_succeed(db, clock):
    from core import _activity_event_lifecycle as eb
    a = _wave(db)
    touch(db, a, USER_A)
    event = _historical_event(db)
    _expire_campaign(db, clock, a)
    holder = _hold(db, lambda s: el.expire_event_scopes(s, event_id=event.id))
    assert holder.value.outcome == el.PURGED
    assert _waits_then(db, holder, lambda: purge(db, a)) == _purged(1, 1)
    assert eb.expire_event(db, event_id=event.id) == eb.EventOutcome(eb.ABSENT)


def test_a_pr2_reread_in_progress_never_contacts_the_purge(db, clock):
    a, b, visit_a = _two_campaigns(db, clock)
    _withdraw(db, a)
    event = _historical_event(db, visit_id=visit_a)
    _expire_campaign(db, clock, a)
    holder = _hold(db, lambda s: el.observe_event_retention(s, event_id=event.id))
    assert holder.value.outcome == el.PRESENT
    try:
        assert purge(db, a) == _purged(1, 1)  # sans attente : la relecture PR 2 ne prend que des clés d'événement
    finally:
        holder.rollback()
    del b


def test_an_implicit_foreign_key_lock_of_an_outside_insert_is_waited_for_in_both_outcomes(db, clock):
    campaign_id = _expired_couple(db, clock)
    [session] = rows(db, f"SELECT * FROM {SESSIONS} WHERE campaign_id = :c", c=campaign_id)
    insert = (f"INSERT INTO {INTERACTIONS} (campaign_id, user_id, interaction_key, interaction_kind, "
              "environment_digest, session_id, recorded_at) VALUES (:c, :u, :k, 'activity', :d, :s, :t)")
    params = {"c": campaign_id, "u": USER_A, "d": session["environment_digest"], "s": session["id"],
              "t": session["last_activity_at"]}
    for commit, expected in ((False, _purged(1, 1)),):
        holder = Holder(db, sql=insert, k=uuid.uuid4(), **params)  # FOR KEY SHARE implicite sur la participation
        assert _waits_then(db, holder, lambda: purge(db, campaign_id), commit=commit) == expected
    other = _expired_couple(db, clock)
    [session] = rows(db, f"SELECT * FROM {SESSIONS} WHERE campaign_id = :c", c=other)
    params.update(c=other, d=session["environment_digest"], s=session["id"])
    holder = Holder(db, sql=insert, k=uuid.uuid4(), **params)
    assert _waits_then(db, holder, lambda: purge(db, other), commit=True) == _purged(2, 1)  # comptée sous verrou
    # Inverse : la purge tient la participation ; l'INSERT attend puis viole la FK.
    third = _expired_couple(db, clock)
    [session] = rows(db, f"SELECT * FROM {SESSIONS} WHERE campaign_id = :c", c=third)
    params.update(c=third, d=session["environment_digest"], s=session["id"])
    holder = _hold(db, lambda s: pl.purge_participation(s, campaign_id=third, user_id=USER_A))

    def outside():
        try:
            _sql(db, insert, k=uuid.uuid4(), **params)
        except sa.exc.IntegrityError as exc:
            return exc
    assert isinstance(_waits_then(db, holder, outside), sa.exc.IntegrityError)


def test_an_outside_json_writer_is_detected_before_the_final_reread_but_not_after_it(db, clock, monkeypatch):
    """Limite L11 reproduite, toutes contraintes en place : un écrivain SQL
    hors service qui ne prend pas le verrou de participation peut rattacher
    un événement à la participation après la relecture finale ; il survit à
    la purge. Avant la relecture finale : conflict / dependency_changed."""
    a, b, _ = _two_campaigns(db, clock)
    _withdraw(db, a)
    event = _historical_event(db)
    [row] = rows(db, f"SELECT retention_scopes FROM {EVENTS} WHERE id = :i", i=event.id)
    [scope] = row["retention_scopes"]["scopes"]
    _expire_campaign(db, clock, a)
    attach = {"version": "retention-scopes-v1", "scopes": sorted(
        [scope, {**scope, "campaign_id": str(a)}], key=lambda s: (s["expires_at"], s["campaign_id"]))}
    real = pl._lock_descendants
    calls = []

    def outside_writer_between(db_, model, *args):
        calls.append(model)
        if model is pl.ProductActivitySessionInteraction and len(calls) == 2:
            _forge_document(db, event.id, attach)  # COMMIT d'un écrivain hors service, sans aucun verrou
        return real(db_, model, *args)
    monkeypatch.setattr(pl, "_lock_descendants", outside_writer_between)
    assert purge(db, a) == lb.PurgeOutcome(lb.CONFLICT, pl.DEPENDENCY_CHANGED)
    assert _participation(db, a) is not None
    monkeypatch.setattr(pl, "_lock_descendants", real)
    _forge_document(db, event.id, row["retention_scopes"])
    remove = []

    def after_final_reread(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SAVEPOINT") and not remove:
            remove.append(1)
            _forge_document(db, event.id, attach)
    sa.event.listen(db, "before_cursor_execute", after_final_reread)
    try:
        assert purge(db, a) == _purged(1, 1)
    finally:
        sa.event.remove(db, "before_cursor_execute", after_final_reread)
    assert _participation(db, a) is None and _scopes_of(db, event.id) == {a, b}  # référence JSON orpheline
    del b


def _row_lock_waits(engine) -> int:
    return rows(engine, "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = :a AND "
                        "wait_event_type = 'Lock' AND wait_event IN ('transactionid', 'tuple')", a=PROOF_APP)[0]["n"]


def test_a_real_deadlock_caused_by_an_outside_writer_is_detected_and_nothing_is_wrongly_claimed(engine, monkeypatch):
    """Écrivain hors service : VISITE puis PARTICIPATION (ordre inverse) ;
    PostgreSQL détecte l'interblocage ; unknown sans écriture, ou purged si
    l'écrivain est la victime ; jamais un succès fictif."""
    from tests.test_activity_event_finalization import Clock
    clock = Clock(datetime.now(timezone.utc) - timedelta(minutes=5))
    monkeypatch.setattr(common, "utcnow", clock)
    monkeypatch.setattr(proofs, "server_clock", clock)
    try:
        campaign_id = _expired_couple(engine, clock)
        [session] = rows(engine, f"SELECT id FROM {SESSIONS} WHERE campaign_id = :c", c=campaign_id)
        before = rows(engine, "SELECT deadlocks AS n FROM pg_stat_database WHERE datname = current_database()")[0]["n"]
        writer = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            writer.execute(sa.text(f"SELECT 1 FROM {SESSIONS} WHERE id = :i FOR UPDATE"), {"i": session["id"]})
            contender = Background(lambda: purge(engine, campaign_id))
            contender.start()
            _wait(lambda: _row_lock_waits(engine) == 1, "la purge n'attend pas la visite")
            try:
                writer.execute(sa.text(f"SELECT 1 FROM {PARTICIPANTS} WHERE campaign_id = :c FOR UPDATE"),
                               {"c": campaign_id})
                writer.commit()
            except sa.exc.OperationalError:
                writer.rollback()
            outcome = contender.outcome()
        finally:
            writer.close()
        after = rows(engine, "SELECT deadlocks AS n FROM pg_stat_database WHERE datname = current_database()")[0]["n"]
        assert after == before + 1
        present = _participation(engine, campaign_id) is not None
        assert (outcome.outcome, present) in ((lb.UNKNOWN, True), (lb.PURGED, False)), (outcome, present)
    finally:
        with engine.begin() as conn:
            for table in CLEANUP:
                conn.execute(sa.text(f"DELETE FROM {table}"))


def test_a_lock_timeout_is_unknown_and_the_explicit_rerun_is_clean(db, clock):
    campaign_id = _expired_couple(db, clock)
    world = _world(db)
    holder = Holder(db)
    common.lock_identities(holder.db, pl.participation_lock_key(campaign_id, USER_A))
    try:
        assert purge(db, campaign_id) == lb.PurgeOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)  # 2 s
    finally:
        holder.rollback()
    assert _world(db) == world and purge(db, campaign_id) == _purged(1, 1)


# --------------------------------------------------------------------------
# H. Propriété transactionnelle et COMMIT incertain
# --------------------------------------------------------------------------

def _fixtures_for_entry_points(db, clock):
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(minutes=5)
    _close_campaign(db, campaign_id)
    clock.set(_deadline(db, campaign_id) - timedelta(minutes=1))
    return campaign_id, visit_id


@pytest.mark.parametrize("kind", ["connection", "session"])
def test_every_entry_point_refuses_a_caller_connection_or_session_without_touching_its_transaction(db, clock, kind):
    campaign_id, visit_id = _fixtures_for_entry_points(db, clock)
    world = _world(db)
    with (db.connect() if kind == "connection" else Session(bind=db)) as caller:
        for open_transaction in (False, True):
            xid = _xid(caller) if open_transaction else None
            for function in ENTRY_POINTS:
                seen, remove = _statements(db)
                try:
                    with pytest.raises(TypeError):
                        _call(function, caller, campaign_id=campaign_id, session_id=visit_id)
                finally:
                    remove()
                assert seen == [], function.__name__  # aucune instruction, pas même BEGIN
                assert caller.in_transaction() is open_transaction, function.__name__
            if open_transaction:
                assert _xid(caller) == xid
        caller.rollback()
    assert _world(db) == world
    assert finalize_one(db, visit_id) == FINALIZED  # le moteur : la frontière possède sa transaction


def test_only_an_engine_able_to_open_its_own_postgresql_transaction_is_accepted(db):
    url = db.url
    refused = {
        "none": None, "url_text": url.render_as_string(hide_password=False), "url": url, "no_db": _NoDB(),
        "sqlite": sa.create_engine("sqlite://"),
        "autocommit_engine": sa.create_engine(url, poolclass=sa.pool.NullPool, isolation_level="AUTOCOMMIT"),
        "autocommit_option": sa.create_engine(url, poolclass=sa.pool.NullPool).execution_options(
            isolation_level="AUTOCOMMIT"),
        "read_only": sa.create_engine(url, poolclass=sa.pool.NullPool).execution_options(postgresql_readonly=True),
    }
    activity = []
    for engine in refused.values():
        if isinstance(engine, sa.Engine):
            sa.event.listen(engine, "connect", lambda *a: activity.append(a))
            sa.event.listen(engine, "before_cursor_execute", lambda *a: activity.append(a))
    for engine in refused.values():
        for function in ENTRY_POINTS:
            with pytest.raises(TypeError):
                _call(function, engine)
    assert activity == []
    assert lb._require_engine is tx._require_engine and lb._owned is tx._owned  # garde-fous communs, inchangés


def test_a_driver_level_autocommit_is_detected_in_every_transaction_and_nothing_is_done(db, clock):
    campaign_id, visit_id = _fixtures_for_entry_points(db, clock)
    clock.set(_deadline(db, campaign_id))
    world = _world(db)
    autocommit = sa.create_engine(db.url, poolclass=sa.pool.NullPool)
    sa.event.listen(autocommit, "connect", lambda dbapi_connection, record: setattr(
        dbapi_connection, "autocommit", True))
    seen, remove = _statements(autocommit)
    try:
        assert lb.finalize_session(autocommit, session_id=visit_id) == lb.SessionOutcome(
            lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)
        assert lb.purge_participation(autocommit, campaign_id=campaign_id, user_id=USER_A) == lb.PurgeOutcome(
            lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)
        for page in (lb.finalize_sessions_page, lb.purge_participations_page):
            report = page(autocommit)
            assert (report.selected, report.examined, report.next_cursor) == (False, 0, None), page.__name__
    finally:
        remove()
        autocommit.dispose()
    work = [s for s in seen if not s.startswith("SET ") and "transaction_status" not in s]
    assert work == []  # ni lecture, ni verrou, ni écriture hors d'un bloc de transaction
    assert _world(db) == world and purge(db, campaign_id) == _purged(1, 1)


def test_every_transaction_of_a_valid_engine_is_an_effective_postgresql_transaction(db, clock, monkeypatch):
    statuses = []
    real = tx._require_effective_transaction

    def spy(session):
        statuses.append(session.connection().connection.dbapi_connection.info.transaction_status)
        return real(session)
    monkeypatch.setattr(tx, "_require_effective_transaction", spy)
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(hours=1)
    assert lb.finalize_sessions_page(db).finalized == 1 and statuses == [tx._INTRANS] * 2  # sélection + traitement
    statuses.clear()
    _close_campaign(db, campaign_id)
    clock.set(_deadline(db, campaign_id))
    assert lb.purge_participations_page(db).purged == 1 and statuses == [tx._INTRANS] * 2
    statuses.clear()
    other, other_visit = _open_visit(db, clock)
    clock.advance(hours=1)
    seen, remove = _lose_commit(db, f"UPDATE {SESSIONS}", really_commit=False)
    try:
        assert finalize_one(db, other_visit).outcome == lb.STILL_PRESENT
    finally:
        remove()
    assert seen == ["lost"] and statuses == [tx._INTRANS] * 2  # écriture + relecture, chacune la sienne
    statuses.clear()
    _close_campaign(db, other)
    clock.set(_deadline(db, other))
    seen, remove = _lose_commit(db, f"DELETE FROM {PARTICIPANTS}", really_commit=True)
    try:
        assert purge(db, other) == lb.PurgeOutcome(lb.ABSENT, lb.UNCONFIRMED_COMMIT)
    finally:
        remove()
    assert seen == ["lost"] and statuses == [tx._INTRANS] * 2


@pytest.mark.parametrize("really_commit", [True, False])
def test_an_uncertain_finalization_commit_is_never_counted_as_a_finalization(db, clock, really_commit):
    _, visit_id = _open_visit(db, clock)
    clock.advance(hours=1)
    before = _visit(db, visit_id)
    seen, remove = _lose_commit(db, f"UPDATE {SESSIONS}", really_commit=really_commit)
    try:
        report = lb.finalize_sessions_page(db)
    finally:
        remove()
    assert seen == ["lost"] and report.finalized == 0 and report.examined == 1
    if really_commit:
        assert report.unconfirmed == 1 and _visit(db, visit_id)["end_reason"] == "inactivity"
        assert finalize_one(db, visit_id) == ALREADY
    else:
        assert report.still_present == 1 and _visit(db, visit_id) == before
        assert finalize_one(db, visit_id) == FINALIZED  # réexécution explicite sûre


@pytest.mark.parametrize("really_commit", [True, False])
@pytest.mark.parametrize("column", ["opened_at", "last_activity_at", "environment_digest", "finalized_at"])
def test_an_altered_visit_snapshot_after_an_uncertain_finalization_is_unknown(db, clock, really_commit, column):
    """Photographie COMPLÈTE : toute colonne altérée entre le COMMIT incertain
    et la relecture rend unknown (jamais unconfirmed ni still_present)."""
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(hours=1)
    alter = {
        "opened_at": f"UPDATE {SESSIONS} SET opened_at = opened_at - interval '1 microsecond' WHERE id = :i",
        "last_activity_at": f"UPDATE {SESSIONS} SET last_activity_at = last_activity_at - interval '1 second', "
                            "idle_expires_at = idle_expires_at - interval '1 second', opened_at = LEAST(opened_at, "
                            "last_activity_at - interval '1 second') WHERE id = :i",
        "environment_digest": f"DELETE FROM {INTERACTIONS} WHERE session_id = :i; UPDATE {SESSIONS} SET "
                              "environment_digest = repeat('b', 64) WHERE id = :i",
        "finalized_at": f"UPDATE {SESSIONS} SET finalized_at = idle_expires_at + interval '1 second', "
                        "end_reason = 'inactivity' WHERE id = :i",
    }[column]
    seen, remove = _lose_commit(db, f"UPDATE {SESSIONS}", really_commit=really_commit,
                                between=lambda: _sql(db, alter, i=visit_id))
    try:
        outcome = finalize_one(db, visit_id)
    finally:
        remove()
    assert seen == ["lost"] and outcome == lb.SessionOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)
    del campaign_id


def test_the_finalization_reread_takes_the_key_of_the_attempt_and_waits_for_it(db, clock, monkeypatch):
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(hours=1)
    key = _visit_key(campaign_id)
    captured, waited = [], []
    real = pl._session_observation

    def spy(session, session_id, campaign, user, digest, attempt_key):
        captured.append(attempt_key)
        return real(session, session_id, campaign, user, digest, attempt_key)
    monkeypatch.setattr(pl, "_session_observation", spy)

    def in_flight():
        holder = Holder(db)
        common.lock_identities(holder.db, key)

        def release():
            _wait(lambda: waiting(db) == 1, "la relecture n'attend pas la clé de l'opération")
            waited.append(1)
            holder.rollback()
        Background(release).start()
    seen, remove = _lose_commit(db, f"UPDATE {SESSIONS}", really_commit=True, between=in_flight)
    try:
        assert finalize_one(db, visit_id) == lb.SessionOutcome(lb.UNCONFIRMED, lb.UNCONFIRMED_COMMIT)
    finally:
        remove()
    assert seen == ["lost"] and captured == [key] and waited == [1]


def test_an_absent_visit_after_an_uncertain_finalization_is_absent_unconfirmed(db, clock):
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(hours=1)

    def disappear():
        _sql(db, f"DELETE FROM {INTERACTIONS} WHERE campaign_id = :c; DELETE FROM {SESSIONS} WHERE id = :i",
             c=campaign_id, i=visit_id)
    seen, remove = _lose_commit(db, f"UPDATE {SESSIONS}", really_commit=True, between=disappear)
    try:
        report = lb.finalize_sessions_page(db)
    finally:
        remove()
    assert (report.absent, report.absent_unconfirmed, report.finalized) == (1, 1, 0)


@pytest.mark.parametrize("really_commit", [True, False])
def test_an_uncertain_purge_commit_is_never_counted_as_a_purge(db, clock, really_commit):
    campaign_id, _ = _rich(db, clock)
    before = _couple(db, campaign_id)
    seen, remove = _lose_commit(db, f"DELETE FROM {INTERACTIONS}", really_commit=really_commit)
    try:
        report = lb.purge_participations_page(db)
    finally:
        remove()
    # USER_B (même campagne) est purgé normalement : seuls SES descendants sont comptés.
    assert seen == ["lost"] and report.examined == 2 and report.purged == 1
    assert (report.interactions, report.sessions) == (1, 1)
    if really_commit:
        assert (report.absent, report.absent_unconfirmed) == (1, 1)  # absence constatée, jamais attribuée
        assert purge(db, campaign_id) == ABSENT_B
    else:
        assert report.unknown == 1 and _couple(db, campaign_id) == before  # jamais still_present pour une purge
        assert purge(db, campaign_id) == _purged(6, 2)


@pytest.mark.parametrize("change", ["swapped_descendant", "residual_event_scope", "residual_session"])
def test_an_uncertain_purge_reread_never_concludes_from_counters_or_partial_absence(db, clock, change):
    campaign_id, _ = _rich(db, clock)
    [session] = rows(db, f"SELECT * FROM {SESSIONS} WHERE campaign_id = :c AND user_id = :u LIMIT 1",
                     c=campaign_id, u=USER_A)

    def between():
        if change == "swapped_descendant":  # même nombre d'interactions, autres lignes
            _sql(db, f"DELETE FROM {INTERACTIONS} WHERE campaign_id = :c AND user_id = :u AND session_id IS NULL "
                     f"AND interaction_key = (SELECT min(interaction_key) FROM {INTERACTIONS} WHERE campaign_id = :c "
                     "AND user_id = :u AND session_id IS NULL); "
                     f"INSERT INTO {INTERACTIONS} (campaign_id, user_id, interaction_key, interaction_kind, "
                     "environment_digest, session_id, recorded_at) VALUES (:c, :u, :k, 'close', :d, NULL, :t)",
                 c=campaign_id, u=USER_A, k=uuid.uuid4(), d=session["environment_digest"],
                 t=session["last_activity_at"])
        elif change == "residual_event_scope":
            event = _record(db, USER_B, event_type="coach_used", surface="coach", source="client_signal")
            _sql(db, f"UPDATE {EVENTS} SET user_id = :u WHERE id = :i", u=USER_A, i=event.id)
            [row] = rows(db, f"SELECT retention_scopes FROM {EVENTS} WHERE id = :i", i=event.id)
            [scope] = row["retention_scopes"]["scopes"]
            _forge_document(db, event.id, {"version": "retention-scopes-v1", "scopes": [
                {**scope, "campaign_id": str(campaign_id)}]})
    really = change != "swapped_descendant"
    if change == "residual_session":
        prefix = f"DELETE FROM {SESSIONS}"  # COMMIT incertain avant : descendants encore présents
        really = False
    else:
        prefix = f"DELETE FROM {INTERACTIONS}"
    if change == "residual_event_scope":
        _wave(db, USER_B)  # B inscrit ailleurs : l'événement forgé vient d'un vrai écrivain
    seen, remove = _lose_commit(db, prefix, really_commit=really, between=between)
    try:
        outcome = purge(db, campaign_id)
    finally:
        remove()
    assert seen == ["lost"] and outcome == lb.PurgeOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)


def test_an_unreadable_reread_and_a_reread_without_transaction_are_unknown(db, clock):
    campaign_id = _expired_couple(db, clock)
    outside = []
    engine = sa.create_engine(db.url, poolclass=sa.pool.NullPool, connect_args={"application_name": PROOF_APP})
    seen, remove = _lose_commit(engine, f"DELETE FROM {INTERACTIONS}", really_commit=True)

    def connect(dbapi_connection, record):
        if seen:
            dbapi_connection.autocommit = True  # seule la connexion de RELECTURE est en autocommit

    def capture(conn, cursor, statement, parameters, context, executemany):
        if conn.connection.dbapi_connection.autocommit:
            outside.append(" ".join(statement.split()))
    sa.event.listen(engine, "connect", connect)
    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        report = lb.purge_participations_page(engine)
    finally:
        remove()
        engine.dispose()
    assert seen == ["lost"] and (report.examined, report.purged, report.absent, report.unknown) == (1, 0, 0, 1)
    assert [s for s in outside if not s.startswith("SET ")] == []
    assert _participation(db, campaign_id) is None  # la suppression a réellement eu lieu, sans être annoncée


def test_a_finalization_reread_without_transaction_is_unknown(db, clock):
    _, visit_id = _open_visit(db, clock)
    clock.advance(hours=1)
    engine = sa.create_engine(db.url, poolclass=sa.pool.NullPool, connect_args={"application_name": PROOF_APP})
    seen, remove = _lose_commit(engine, f"UPDATE {SESSIONS}", really_commit=True)
    sa.event.listen(engine, "connect", lambda dbapi_connection, record: seen and setattr(
        dbapi_connection, "autocommit", True))
    try:
        assert lb.finalize_session(engine, session_id=visit_id) == lb.SessionOutcome(lb.UNKNOWN,
                                                                                     lb.TECHNICAL_UNCERTAINTY)
    finally:
        remove()
        engine.dispose()


def test_a_failure_before_commit_and_an_unreachable_database_are_unknown(db, clock):
    campaign_id, visit_id = _fixtures_for_entry_points(db, clock)
    world = _world(db)
    unreachable = sa.create_engine(db.url.set(port=1), poolclass=sa.pool.NullPool)
    assert lb.finalize_session(unreachable, session_id=visit_id).outcome == lb.UNKNOWN
    assert lb.purge_participation(unreachable, campaign_id=campaign_id, user_id=USER_A).outcome == lb.UNKNOWN
    for page in (lb.finalize_sessions_page, lb.purge_participations_page):
        report = page(unreachable)
        assert (report.selected, report.examined, report.next_cursor) == (False, 0, None)  # aucun avancement
    assert _world(db) == world


# --------------------------------------------------------------------------
# I. Pagination, plans SQL
# --------------------------------------------------------------------------

def _forge_users(db, count, prefix="v0-p3-user"):
    users = [f"{prefix}-{index:04d}" for index in range(count)]
    _sql(db, "INSERT INTO users (id, level) SELECT unnest(CAST(:u AS text[])), 'debutant' ON CONFLICT DO NOTHING",
         u=users)
    return users


def _forge_participants(db, campaign_id, users, start):
    _sql(db, f"INSERT INTO {PARTICIPANTS} (campaign_id, user_id, window_start_at, window_end_at, "
             "participation_status, enrolled_at, authorization_recorded_at, notice_version) "
             "SELECT :c, unnest(CAST(:u AS text[])), :s, :s + interval '12 days', 'enrolled', :s, :s, 'v0-notice-1'",
         c=campaign_id, u=users, s=start)


def _forge_open_visits(db, campaign_id, users, last_activity):
    _sql(db, f"INSERT INTO {SESSIONS} (id, campaign_id, user_id, environment_digest, opened_at, last_activity_at, "
             "idle_expires_at) SELECT gen_random_uuid(), :c, u, encode(sha256(convert_to(u, 'UTF8')), 'hex'), :t, :t, "
             ":t + interval '30 minutes' FROM unnest(CAST(:u AS text[])) AS u", c=campaign_id, u=users,
         t=last_activity)


def _drop_forged_users(db):
    with db.begin() as conn:
        for table in CLEANUP:
            conn.execute(sa.text(f"DELETE FROM {table}"))
        conn.execute(sa.text("DELETE FROM users WHERE id LIKE 'v0-p3-%'"))


def test_empty_pages_are_selected_and_distinguished_from_a_failed_selection(db, clock):
    for page in (lb.finalize_sessions_page, lb.purge_participations_page):
        report = page(db)
        assert (report.selected, report.examined, report.next_cursor) == (True, 0, None)


def test_pages_of_idle_visits_follow_the_index_order_across_campaigns_with_equal_deadlines(db, clock):
    try:
        users = _forge_users(db, 205)
        first, second = _wave(db), _wave(db)
        _forge_participants(db, first, users[:100], clock.now)
        _forge_participants(db, second, users[100:], clock.now)
        _forge_open_visits(db, first, users[:100], clock.now)
        _forge_open_visits(db, second, users[100:], clock.now)  # même idle_expires_at partout
        clock.advance(hours=1)
        expected = [r["id"] for r in rows(db, f"SELECT id FROM {SESSIONS} WHERE finalized_at IS NULL ORDER BY "
                                              "campaign_id, idle_expires_at, id")]
        full = committed(db, lambda s: pl.select_idle_session_candidates(s, limit=200))
        assert list(full.session_ids) == expected[:200] and full.next_cursor.session_id == expected[199]
        rest = committed(db, lambda s: pl.select_idle_session_candidates(s, limit=200, after=full.next_cursor))
        assert list(rest.session_ids) == expected[200:] and rest.next_cursor is None
        page = lb.finalize_sessions_page(db, limit=200)
        assert (page.examined, page.finalized) == (200, 200)
        assert page.next_cursor == full.next_cursor  # photographie de sélection, jamais recalculée
        assert lb.finalize_sessions_page(db, limit=200, after=page.next_cursor).finalized == 5
        assert lb.finalize_sessions_page(db).examined == 0
    finally:
        _drop_forged_users(db)


def test_pages_of_participations_follow_the_primary_key_and_the_cursor_of_the_selection(db, clock):
    try:
        users = _forge_users(db, 205)
        campaign_id = _wave(db)
        _forge_participants(db, campaign_id, users, clock.now)
        clock.advance(hours=1)
        _close_campaign(db, campaign_id)
        clock.set(_deadline(db, campaign_id))
        expected = [(r["campaign_id"], r["user_id"]) for r in rows(
            db, f"SELECT campaign_id, user_id FROM {PARTICIPANTS} ORDER BY campaign_id, user_id")]
        full = committed(db, lambda s: pl.select_purgeable_participation_candidates(s, limit=200))
        assert list(full.participations) == expected[:200]
        rest = committed(db, lambda s: pl.select_purgeable_participation_candidates(s, limit=200,
                                                                                    after=full.next_cursor))
        assert list(rest.participations) == expected[200:] and rest.next_cursor is None  # curseur strict
        page = lb.purge_participations_page(db, limit=200)
        assert (page.examined, page.purged) == (200, 200) and page.next_cursor == full.next_cursor
        assert (page.next_cursor.campaign_id, page.next_cursor.user_id) == expected[199]
        second = lb.purge_participations_page(db, limit=200, after=page.next_cursor)
        assert (second.examined, second.purged, second.next_cursor) == (6, 6, None)  # 205 + J0 de USER_A
    finally:
        _drop_forged_users(db)


def test_each_selected_candidate_is_reread_and_a_failure_never_stops_the_following_ones(db, clock):
    gone, ineligible, held, healthy = (_closed_wave(db, clock) for _ in range(4))
    clock.set(max(_deadline(db, c) for c in (gone, ineligible, held, healthy)))
    page = committed(db, lambda s: pl.select_purgeable_participation_candidates(s))
    assert set(page.participations) == {(c, USER_A) for c in (gone, ineligible, held, healthy)}
    _sql(db, f"DELETE FROM {INTERACTIONS} WHERE campaign_id = :c; DELETE FROM {SESSIONS} WHERE campaign_id = :c; "
             f"DELETE FROM {PARTICIPANTS} WHERE campaign_id = :c", c=gone)
    _sql(db, f"UPDATE {CAMPAIGNS} SET retention_days = 91 WHERE id = :c", c=ineligible)  # devenue hors contrat
    holder = Holder(db)
    common.lock_identities(holder.db, pl.participation_lock_key(held, USER_A))
    try:
        outcomes = {c: purge(db, c).outcome for c, _ in page.participations}
    finally:
        holder.rollback()
    assert outcomes == {gone: lb.ABSENT, ineligible: lb.CONFLICT, held: lb.UNKNOWN, healthy: lb.PURGED}


def _plan(db, statement, *, seqscan=True):
    compiled = statement.compile(dialect=db.dialect, compile_kwargs={"literal_binds": True})
    with db.connect() as conn:
        if not seqscan:
            conn.execute(sa.text("SET enable_seqscan = off"))
        return "\n".join(r[0] for r in conn.execute(sa.text(f"EXPLAIN {compiled}")))


def test_the_selection_plans_use_the_existing_indexes_on_representative_data(db, clock):
    try:
        users = _forge_users(db, 400)
        campaigns = [_wave(db) for _ in range(4)]
        for index, campaign_id in enumerate(campaigns):
            chunk = users[index * 100:(index + 1) * 100]
            _forge_participants(db, campaign_id, chunk, clock.now)
            _forge_open_visits(db, campaign_id, chunk, clock.now - timedelta(hours=index))
        # Visites finalisées (hors de l'index partiel) en grand nombre.
        _sql(db, f"INSERT INTO {SESSIONS} (id, campaign_id, user_id, environment_digest, opened_at, last_activity_at, "
                 "idle_expires_at, finalized_at, end_reason) SELECT gen_random_uuid(), p.campaign_id, p.user_id, "
                 "encode(sha256(convert_to(p.user_id || g, 'UTF8')), 'hex'), :t, :t, :t + interval '30 minutes', "
                 f":t + interval '31 minutes', 'inactivity' FROM {PARTICIPANTS} p, generate_series(1, 20) g",
             t=clock.now - timedelta(days=1))
        _sql(db, "ANALYZE product_activity_sessions; ANALYZE product_campaign_participants; ANALYZE product_campaigns")
        clock.advance(minutes=150)  # une partie seulement des visites ouvertes est échue
        cursor = pl.SessionCursor(campaigns[0], clock.now - timedelta(hours=3), uuid.uuid4())
        for after in (None, cursor):
            plan = _plan(db, pl._idle_query(clock.now, 50, after))
            assert IDLE_INDEX in plan and "Index Cond" in plan and "Seq Scan" not in plan, plan
            if after is not None:
                assert re.search(r"Index Cond: .*ROW\(campaign_id, idle_expires_at\) >=", plan), plan
        for after in (None, pl.ParticipationCursor(campaigns[1], users[150])):
            plan = _plan(db, pl._purge_query(clock.now, 50, after), seqscan=False)
            assert PARTICIPANTS_PKEY in plan, plan
            if after is not None:
                assert re.search(r"Index Cond: \(ROW\(campaign_id, \(?user_id\)?(::text)?\) >", plan), plan
        assert "OFFSET" not in str(pl._purge_query(clock.now, 50, None)) + str(pl._idle_query(clock.now, 50, None))
    finally:
        _drop_forged_users(db)


# --------------------------------------------------------------------------
# J. Recréation et limites historiques
# --------------------------------------------------------------------------

def _refused(action, *errors):
    try:
        action()
    except errors as exc:
        return exc
    raise AssertionError("aucun refus")


def test_no_compliant_path_recreates_a_purged_participation(db, clock):
    proof = _certified(db)
    clock.advance(seconds=1)
    assert finalize(db, proof.proof_id).state == fz.RECORDED
    campaign_id = proof.campaign_id
    [participation] = rows(db, f"SELECT * FROM {PARTICIPANTS} WHERE campaign_id = :c", c=campaign_id)
    [interaction] = rows(db, f"SELECT * FROM {INTERACTIONS} WHERE campaign_id = :c", c=campaign_id)
    _expire_campaign(db, clock, campaign_id)
    from core import _activity_event_lifecycle as eb
    from core import _activity_proof_lifecycle as pb
    assert pb.purge_resolved_page(db).purged == 1
    assert eb.expire_events_page(db).purged == 1
    assert purge(db, campaign_id) == _purged(1, 1)
    world = _world(db)
    now = common.utcnow()
    _refused(lambda: committed(db, lambda s: pp.enroll_participant(
        s, campaign_id=campaign_id, user_id=USER_A, window_start_at=now, window_end_at=now + timedelta(days=1),
        authorization_recorded_at=now, notice_version=NOTICE)), ParticipationRefused)
    _refused(lambda: committed(db, lambda s: pp.first_eligible_login(
        s, campaign_id=campaign_id, user_id=USER_A, authorization_recorded_at=participation[
            "authorization_recorded_at"], notice_version=NOTICE, via_cohort=False)), ParticipationRefused)
    for operation_function in (ps.touch_activity_session, ps.close_activity_session):
        refused = _refused(lambda: committed(db, lambda s: operation_function(
            s, campaign_id=campaign_id, user_id=USER_A, environment_key=LAPTOP,
            interaction_key=interaction["interaction_key"])), ActivitySessionRefused)
        assert refused.reason == "not_participant"
    # Preuve depuis l'ancienne interaction : aucune ligne.
    assert register(db, proof.origin, proof.op).state == proofs.ABSENT
    # Événement attribué à la participation disparue : refusé.
    _refused(lambda: _record(db, USER_A, event_type="coach_used", surface="coach", source="client_signal",
                             occurred_at=utc(participation["window_start_at"]) + timedelta(hours=1)),
             EventNotAuthorized)
    assert _world(db) == world


def test_a_remaining_cohort_seat_never_readmits_into_the_expired_campaign(db, clock, monkeypatch):
    from tests import test_product_analytics_cohort as tc
    from tests.test_product_analytics_services import T0
    from sqlalchemy.orm import sessionmaker
    factory = sessionmaker(bind=db, autocommit=False, autoflush=False)
    clock.set(T0)
    try:
        campaign_id, users, _, _ = tc._ready(db, factory, clock, testers=1)
        [user] = users
        admitted = tc._committed_admit(factory, campaign_id, user)
        assert admitted[2] is True
        clock.advance(hours=1)
        _close_campaign(db, campaign_id)
        clock.set(_deadline(db, campaign_id))
        assert purge(db, campaign_id, user) == _purged(0, 0)
        seats = rows(db, "SELECT *, xmin::text AS xmin FROM product_cohort_seats ORDER BY id")
        cohort = rows(db, "SELECT *, xmin::text AS xmin FROM product_campaign_cohorts")
        refused = _refused(lambda: tc._committed_admit(factory, campaign_id, user), common.AdmissionRefused)
        assert refused.reason == "campaign_not_active"
        attempt = committed(db, lambda s: ch.attempt_first_login_admission(s, campaign_id=campaign_id, user_id=user))
        assert (attempt.status, attempt.reason) == ("not_eligible", "campaign_not_active")
        assert _participation(db, campaign_id, user) is None
        assert rows(db, "SELECT *, xmin::text AS xmin FROM product_cohort_seats ORDER BY id") == seats  # inchangées
        assert rows(db, "SELECT *, xmin::text AS xmin FROM product_campaign_cohorts") == cohort
        # Effet résiduel caractérisé (L10, non corrigé) : une panne technique de l'admission après la purge
        # marque encore la place (admission_failure_first_at), faute de participation.
        def broken(*args, **kwargs):
            raise RuntimeError("panne technique injectée")
        monkeypatch.setattr(ch, "admit_at_first_eligible_login", broken)
        failed = committed(db, lambda s: ch.attempt_first_login_admission(s, campaign_id=campaign_id, user_id=user))
        assert failed.status == "failed"
        [seat] = rows(db, "SELECT admission_failure_first_at FROM product_cohort_seats WHERE user_id = :u", u=user)
        assert seat["admission_failure_first_at"] is not None and _participation(db, campaign_id, user) is None
    finally:
        with db.begin() as conn:
            for table in CLEANUP:
                conn.execute(sa.text(f"DELETE FROM {table}"))
            for table in ("auth_operations", "invitations"):
                conn.execute(sa.text(f"DELETE FROM {table}"))
            conn.execute(sa.text("DELETE FROM users WHERE id LIKE 'v0-tester-%'"))


def test_historical_writers_still_accept_a_close_and_restitute_after_expiry_when_the_purge_is_blocked(db, clock):
    """Contre-exemple historique (L10, caractérisé, NON corrigé ici) :
    campagne clôturée, conservation échue, purge bloquée par une preuve ;
    close_activity_session inscrit encore une nouvelle interaction 'close'
    (sans visite) ; le rejeu d'une visite et la restitution du J0 répondent
    encore. Le parcours A, lui, refuse toute écriture."""
    proof = _certified(db)
    campaign_id = proof.campaign_id
    [visit] = rows(db, f"SELECT * FROM {SESSIONS} WHERE campaign_id = :c", c=campaign_id)
    _expire_campaign(db, clock, campaign_id)
    assert purge(db, campaign_id) == _blocked(pl.PROOF_REMAINING)
    before = _count(db, INTERACTIONS, campaign_id)
    closing = committed(db, lambda s: ps.close_activity_session(
        s, campaign_id=campaign_id, user_id=USER_A, environment_key=uuid.uuid4(), interaction_key=uuid.uuid4()))
    assert closing.outcome == ps.NO_OPEN_SESSION
    assert _count(db, INTERACTIONS, campaign_id) == before + 1  # écriture après l'échéance de conservation
    replay = committed(db, lambda s: ps.touch_activity_session(
        s, campaign_id=campaign_id, user_id=USER_A, environment_key=LAPTOP,
        interaction_key=proof.origin.interaction_key))
    assert replay.outcome == ps.REPLAYED and replay.session.id == visit["id"]  # rejeu historique restitué
    [participation] = rows(db, f"SELECT * FROM {PARTICIPANTS} WHERE campaign_id = :c", c=campaign_id)
    restituted, created = committed(db, lambda s: pp.enroll_at_first_eligible_login(
        s, campaign_id=campaign_id, user_id=USER_A, authorization_recorded_at=participation[
            "authorization_recorded_at"], notice_version=NOTICE))
    assert created is False and utc(restituted.window_start_at) == utc(participation["window_start_at"])
    assert finalize_one(db, visit["id"]) == EXPIRED_A  # nouveau parcours : aucune mutation


def test_the_python_clock_of_the_historical_services_may_diverge_from_postgresql(db, clock, monkeypatch):
    """Limite caractérisée : les services historiques décident avec
    common.utcnow, la PR 3 avec l'horloge PostgreSQL. Une horloge Python en
    retard laisse close_activity_session écrire alors que PostgreSQL a
    franchi l'échéance de conservation."""
    campaign_id = _expired_couple(db, clock)
    deadline = _deadline(db, campaign_id)
    monkeypatch.setattr(common, "utcnow", lambda: deadline - timedelta(days=1))
    committed(db, lambda s: ps.close_activity_session(s, campaign_id=campaign_id, user_id=USER_A,
                                                      environment_key=uuid.uuid4(), interaction_key=uuid.uuid4()))
    assert proofs.server_clock(None) >= deadline and purge(db, campaign_id) == _purged(2, 1)


# --------------------------------------------------------------------------
# Rapports, journaux, inventaires et dormance
# --------------------------------------------------------------------------

def _no_identifier_logged(caplog):
    for record in caplog.records:
        message = record.getMessage()
        assert not UUID_PATTERN.search(message) and USER_A not in message and USER_B not in message, message
        assert "retention_scopes" not in message and "cursor" not in message.lower(), message


def test_reports_and_logs_carry_only_closed_vocabularies_and_counters(db, clock, caplog):
    caplog.set_level(logging.INFO, logger="oryx.activity_participation_lifecycle")
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(hours=1)
    report = lb.finalize_sessions_page(db)
    assert report.examined == sum(report.counters().values()) == 1
    _close_campaign(db, campaign_id)
    clock.set(_deadline(db, campaign_id))
    purge_report = lb.purge_participations_page(db)
    assert purge_report.examined == sum(purge_report.counters().values()) == purge_report.purged == 1
    assert (purge_report.interactions, purge_report.sessions) == (1, 1)
    for value in (report, purge_report, lb.finalize_session(db, session_id=visit_id), purge(db, campaign_id)):
        assert not UUID_PATTERN.search(repr(value)) and USER_A not in repr(value)
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("activity_session_lifecycle selection=session_finalization selected=True examined=1 "
                            "finalized=1") for m in messages), messages
    assert any(m.startswith("activity_participation_lifecycle selection=participation_purge selected=True "
                            "examined=1 purged=1") for m in messages), messages
    assert "activity_session_finalization outcome=absent reason=None" in messages
    _no_identifier_logged(caplog)


FORBIDDEN = ("authz_f2", "auth_f2", "f2_runtime", "_activity_proof_bridge", "oryx_sessions", "OryxSession",
             "_run_attested", "os.environ", "getenv", "Thread", "asyncio", "schedule", "socket", "requests", "httpx",
             "APIRouter", "FastAPI", "import_module", "__import__", "record_event", "_stage_certified_event",
             "certify_pending", "register_pending", "open_origin", "finalize_idle_sessions", "purge_resolved_proof",
             "expire_event_scopes",
             "withdraw_participant", "close_campaign", "cancel_campaign", "TRUNCATE", "CASCADE", "SET NULL",
             "session_replication_role", "DISABLE TRIGGER", "while True", "sleep", "print(", "tombstone",
             "portfolio_positions", "invitations", "auth_operations", "product_cohort_seats",
             "ProductCohortSeat", "ProductCampaignCohort", "pedagog", "cognitive", "COMPLETED", "INSERT")


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
    assert _imports(SERVICE) == {"uuid", "dataclasses", "datetime", "sqlalchemy", "sqlalchemy.exc", "core.models",
                                 "core.product_analytics", "core.product_analytics.common",
                                 "core.product_analytics.proofs", "core.product_analytics.retention",
                                 "core.product_analytics.sessions"}
    assert _imports(BOUNDARY) == {"logging", "uuid", "dataclasses", "core", "core._activity_event_finalization",
                                  "core._activity_transactions", "core.product_analytics",
                                  "core.product_analytics.participation_lifecycle"}
    for path in (SERVICE, BOUNDARY):
        source = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN:
            assert forbidden not in source, (path.name, forbidden)
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    session_calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                     and n.func.value.id == "db"}
    assert session_calls == {"execute", "begin_nested", "expunge"}  # ni commit, ni rollback, ni add, ni flush
    attributes = {n.attr for path in (SERVICE, BOUNDARY)
                  for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
    assert not attributes & {"touch_activity_session", "close_activity_session", "finalize_idle_sessions",
                             "record_event", "merge", "flush", "commit", "rollback"}  # aucun écrivain d'un autre lot
    assert "session_ended" not in {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    assert {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "proofs"} == {"server_clock"}
    assert {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "sessions"} == {"INACTIVITY", "LOGOUT", "END_REASONS"}
    assert {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "retention"} == {"campaign_reference", "lenient_campaigns", "REFERENCE_INCOHERENT",
                                              "REFERENCE_PRESENT"}  # R5 bis extrait (PR 4, nominatif)
    assert len([n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "begin_nested"]) == 2  # UN SAVEPOINT par chemin d'écriture
    deletes = [[ast.unparse(a) for a in n.args] for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "delete"]
    assert deletes == [["i"], ["v"], ["ProductCampaignParticipant"]]
    updates = [[ast.unparse(a) for a in n.args] for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "update"]
    assert updates == [["table"]]
    boundary = BOUNDARY.read_text(encoding="utf-8")
    assert "import sqlalchemy" not in boundary and "sa." not in boundary and "text(" not in boundary
    assert "SELECT" not in boundary and "DELETE " not in boundary and "UPDATE " not in boundary
    assert "execute(" not in boundary and "while " not in boundary and "for _ in" not in boundary
    assert "finalization_boundary._transaction(" in boundary and "finalization_boundary._run(" not in boundary
    assert boundary.count("_owned(") == 5  # sélection, finalisation, relecture, purge, relecture
    assert "retry" not in boundary.lower().replace("aucun retry automatique", "")


def test_nothing_calls_the_participation_lifecycle_outside_the_tests_and_the_documentation():
    offenders = []
    markers = ("participation_lifecycle", "_activity_participation_lifecycle", "finalize_idle_session(",
               "finalize_sessions_page", "purge_participation", "select_idle_session_candidates",
               "select_purgeable_participation_candidates", "participation_retention_deadline")
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
        if path == CAMPAIGN_LIFECYCLE:
            # V0-T08 Lot 2C PR 4 (nominatif) : le service dormant du cycle de vie des campagnes réutilise
            # l'échéance commune et les contrôles de la PR 3 (import exact, aucun point d'entrée appelé).
            assert "from core.product_analytics import participation_lifecycle as pl" in source
            assert not {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)
                        and isinstance(n.value, ast.Name) and n.value.id == "pl"} & {
                "finalize_idle_session", "purge_participation", "select_idle_session_candidates",
                "select_purgeable_participation_candidates", "observe_session_finalization",
                "observe_participation_purge"}
            continue
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
import json, socket, sys, threading
socket.socket.connect = lambda self, address: (_ for _ in ()).throw(OSError("réseau interdit"))
import api
PRIVATE = ("core._activity_participation_lifecycle", "core._activity_event_lifecycle", "core._activity_transactions",
           "core._activity_proof_lifecycle", "core._activity_event_finalization", "core._activity_proof_bridge")
loaded = sorted(n for n in sys.modules if n in PRIVATE or n.startswith("core.product_analytics"))
print(json.dumps({"loaded": loaded, "threads": sorted(t.name for t in threading.enumerate())}))
'''


def test_the_real_api_never_loads_the_participation_lifecycle_whatever_the_environment():
    baseline = None
    for environment in ({}, {"ORYX_PRODUCT_ANALYTICS": "1", "ORYX_V0_T08_ENABLED": "1", "ORYX_PARTICIPATION_PURGE": "1",
                             "ORYX_SESSION_FINALIZATION": "1", "ORYX_LOT_2C": "1", "ORYX_F2_ENABLED": "1"}):
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                             env=_clean_env(environment), timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["loaded"] == []
        baseline = baseline or result["threads"]
        assert result["threads"] == baseline


# --------------------------------------------------------------------------
# R1. Correctif post-audit (HEAD audité 2cf7425) : contre-épreuves
# --------------------------------------------------------------------------

def _closed_or_cancelled(db, campaign_id, status):
    (_close_campaign if status == "closed" else _cancel_campaign)(db, campaign_id)
    return utc(_campaign(db, campaign_id)["closed_at"])


@pytest.mark.parametrize("status", ["closed", "cancelled"])
def test_r1_b1_a_closure_stored_after_the_decision_clock_is_never_finalized(db, clock, status):
    """Contre-épreuve B1-1 (reproduite sur 2cf7425 : finalized) : activité à
    9 h, échéance d'inactivité à 9 h 30, horloge PostgreSQL à 10 h, campagne
    closed / cancelled dont closed_at stocké vaut 11 h."""
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(minutes=60)
    _sql(db, f"UPDATE {CAMPAIGNS} SET status = :s, closed_at = :t WHERE id = :c", s=status,
         t=clock.now + timedelta(hours=1), c=campaign_id)  # admis par les CHECK de 0022
    before = _visit(db, visit_id)
    assert finalize_one(db, visit_id) == lb.SessionOutcome(lb.UNKNOWN, pl.CLOCK_INCOHERENT)
    assert _visit(db, visit_id) == before  # aucune écriture, aucun antidatage


@pytest.mark.parametrize("status", ["closed", "cancelled"])
def test_r1_b1_a_regressive_clock_behind_the_closure_writes_nothing(db, clock, status):
    """Données cohérentes, horloge PostgreSQL revenue en deçà de closed_at
    (mais au-delà de l'échéance d'inactivité) : unknown, puis finalisation
    normale une fois l'horloge revenue."""
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(hours=2)
    closed_at = _closed_or_cancelled(db, campaign_id, status)
    before = _visit(db, visit_id)
    clock.set(closed_at - MICRO)  # régression : idle_expires_at < horloge < closed_at
    assert utc(before["idle_expires_at"]) < clock.now
    assert finalize_one(db, visit_id) == lb.SessionOutcome(lb.UNKNOWN, pl.CLOCK_INCOHERENT)
    assert _visit(db, visit_id) == before
    # Parcours B : chronologie stockée postérieure à l'horloge, constatée sans écriture (jamais not_due).
    assert purge(db, campaign_id) == lb.PurgeOutcome(lb.UNKNOWN, pl.CLOCK_INCOHERENT)
    clock.set(closed_at + timedelta(hours=1))
    assert purge(db, campaign_id) == NOT_DUE_B
    assert finalize_one(db, visit_id) == FINALIZED
    assert utc(_visit(db, visit_id)["finalized_at"]) == clock.now  # jamais imposée à closed_at


@pytest.mark.parametrize("status", ["closed", "cancelled"])
@pytest.mark.parametrize("offset, expected", [(timedelta(minutes=5), "conflict"), (MICRO, "conflict"),
                                              (timedelta(0), "finalized"), (-timedelta(minutes=5), "finalized")])
def test_r1_b1_an_activity_after_the_effective_closure_is_out_of_contract(db, clock, status, offset, expected):
    """Contre-épreuve B1-2 (reproduite sur 2cf7425 : finalized) : clôture
    anticipée, last_activity_at postérieur à closed_at (mais avant
    window_end_at et close_by_at), idle_expires_at conforme, horloge après
    l'échéance d'inactivité. L'égalité est admise (touch et la clôture sont
    sérialisés sur la campagne) ; la FINALISATION après la clôture est
    légitime."""
    campaign_id, visit_id = _open_visit(db, clock)
    clock.advance(minutes=10)
    closed_at = _closed_or_cancelled(db, campaign_id, status)
    _sql(db, f"UPDATE {SESSIONS} SET last_activity_at = :t, idle_expires_at = :t + interval '30 minutes' "
             "WHERE id = :i", t=closed_at + offset, i=visit_id)
    clock.set(closed_at + timedelta(hours=1))
    before = _visit(db, visit_id)
    # Couverture : la sélection nominale rend la visite (ouverte, inactive).
    assert committed(db, lambda s: pl.select_idle_session_candidates(s)).session_ids == (visit_id,)
    report = lb.finalize_sessions_page(db)
    if expected == "conflict":
        assert (report.examined, report.conflict, report.finalized) == (1, 1, 0)
        assert finalize_one(db, visit_id) == lb.SessionOutcome(lb.CONFLICT, pl.SESSION_INCOHERENT)
        assert _visit(db, visit_id) == before
    else:
        assert (report.examined, report.finalized) == (1, 1)
        assert utc(_visit(db, visit_id)["finalized_at"]) == clock.now


def test_r1_b1_the_deadline_stays_pure_and_the_clock_is_checked_only_in_the_decision_paths():
    source = inspect.getsource(pl.participation_retention_deadline)
    assert "server_clock" not in source and "utcnow" not in source and "db" not in inspect.signature(
        pl.participation_retention_deadline).parameters
    future = _campaign_value(closed_at=BASE + timedelta(days=29))  # aucune horloge : échéance établie
    assert pl.participation_retention_deadline(future) == BASE + timedelta(days=29) + HOURS_2160
    for function in (pl.finalize_idle_session, pl.purge_participation):
        assert "_clock_problem(" in inspect.getsource(function)


PARTICIPATION_ANOMALIES = {
    # Contre-épreuve principale B2 (reproduite sur 2cf7425 : purged) : fenêtre altérée à J+200.
    "window_beyond_close_by": "window_end_at = window_start_at + interval '200 days'",
    "authorization_missing": "authorization_recorded_at = NULL, notice_version = NULL",
    "notice_missing": "notice_version = NULL",
    "notice_malformed": "notice_version = 'NOT A VERSION'",
    "authorization_after_window_start": "authorization_recorded_at = window_start_at + interval '1 day'",
    "authorization_after_enrollment": "authorization_recorded_at = enrolled_at + interval '1 microsecond'",
    "enrolled_after_window_start": "enrolled_at = window_start_at + interval '1 second'",
    "enrolled_before_campaign_creation": "enrolled_at = enrolled_at - interval '400 days', "
                                         "authorization_recorded_at = authorization_recorded_at - interval '400 days'",
    "enrolled_after_closure": "enrolled_at = enrolled_at + interval '2 days', "
                              "window_start_at = window_start_at + interval '2 days', "
                              "window_end_at = window_end_at + interval '2 days'",
}


@pytest.mark.parametrize("anomaly", sorted(PARTICIPATION_ANOMALIES))
def test_r1_b2_a_participation_outside_its_universal_invariants_is_never_deleted(db, clock, anomaly):
    campaign_id = _wave(db)
    touch(db, campaign_id, USER_A)
    clock.advance(days=1)
    _close_campaign(db, campaign_id)  # clôture anticipée (J+1 ; close_by_at à J+30)
    _sql(db, f"UPDATE {PARTICIPANTS} SET {PARTICIPATION_ANOMALIES[anomaly]} WHERE campaign_id = :c",
         c=campaign_id)  # admis par les CHECK de 0022
    deadline = _deadline(db, campaign_id)
    clock.set(deadline)
    before = _couple(db, campaign_id)
    page = committed(db, lambda s: pl.select_purgeable_participation_candidates(s))
    assert page.participations == ((campaign_id, USER_A),)  # couverture : sélectionnée à l'échéance
    report = lb.purge_participations_page(db)
    assert (report.examined, report.conflict, report.purged) == (1, 1, 0)
    assert purge(db, campaign_id) == lb.PurgeOutcome(lb.CONFLICT, pl.PARTICIPATION_INCOHERENT)
    assert _couple(db, campaign_id) == before and _deadline(db, campaign_id) == deadline  # aucune autre échéance
    [visit] = before["sessions"]
    clock.set(utc(visit["idle_expires_at"]) + timedelta(seconds=1))
    finalization = finalize_one(db, visit["id"])  # la visite peut elle-même devenir hors fenêtre
    assert finalization.outcome == lb.CONFLICT and finalization.reason in (pl.PARTICIPATION_INCOHERENT,
                                                                           pl.SESSION_INCOHERENT)
    assert _visit(db, visit["id"]) == visit


@pytest.mark.parametrize("status", ["enrolled", "withdrawn", "completed"])
def test_r1_b2_generic_enrollments_and_windows_beyond_an_early_closure_remain_purgeable(db, clock, status):
    """Contrats des inscriptions GÉNÉRIQUES (enroll_participant) : fenêtre
    courte, début différé, J0 ≠ inscription, inscription avant l'activation,
    fenêtre dépassant la clôture anticipée ; aucune discrimination de statut."""
    now = clock.now
    campaign_id = committed(db, lambda s: pc.create_campaign(
        s, code=f"g-{uuid.uuid4().hex[:8]}", name="Vague 0", purpose="Mesure.", close_by_at=now + timedelta(days=30),
        **POLICY).id)
    clock.advance(minutes=1)
    committed(db, lambda s: pp.enroll_participant(
        s, campaign_id=campaign_id, user_id=USER_A, window_start_at=now + timedelta(hours=3),
        window_end_at=now + timedelta(days=3), authorization_recorded_at=now, notice_version=NOTICE))
    clock.advance(minutes=1)
    committed(db, lambda s: pc.activate_campaign(s, campaign_id=campaign_id))  # après l'inscription générique
    clock.advance(hours=2)
    _close_campaign(db, campaign_id)  # avant même le début de la fenêtre contractuelle
    if status != "enrolled":
        _sql(db, f"UPDATE {PARTICIPANTS} SET participation_status = :s WHERE campaign_id = :c", s=status,
             c=campaign_id)
    participation = _participation(db, campaign_id)
    assert utc(participation["window_end_at"]) > utc(_campaign(db, campaign_id)["closed_at"])
    assert participation["window_end_at"] - participation["window_start_at"] != timedelta(days=12)
    clock.set(_deadline(db, campaign_id))
    assert purge(db, campaign_id) == _purged(0, 0)


def test_r1_b2_the_wave_0_first_login_contract_is_not_imposed_on_generic_enrollments():
    source = inspect.getsource(pl._participation_problem)
    assert "OBSERVATION_WINDOWS" not in source and "timedelta" not in source and "activated_at" not in source
    assert "participation_status ==" not in source and "ENROLLED" not in source


R5_FORMS = {
    # (élément forgé, issue) ; « B » : campagne B de l'utilisateur, « A » : campagne traitée.
    "canonical_b": ("{scope}", "purged"),
    "uppercase_b": ('{{**scope, "campaign_id": B.upper()}}', "purged"),
    "hex32_b": ('{{**scope, "campaign_id": B.replace("-", "")}}', "purged"),
    "unknown_policy_value_b": ('{{**scope, "policy_version": "v9"}}', "purged"),
    "malformed_expiry_b": ('{{**scope, "expires_at": "demain"}}', "purged"),
    "uppercase_a": ('{{**scope, "campaign_id": A.upper()}}', "conflict"),
    "hex32_a": ('{{**scope, "campaign_id": A.replace("-", "")}}', "conflict"),
    "braces_b": ('{{**scope, "campaign_id": "{{" + B + "}}"}}', "conflict"),
    "urn_b": ('{{**scope, "campaign_id": "urn:uuid:" + B}}', "conflict"),
    "padded_b": ('{{**scope, "campaign_id": " " + B}}', "conflict"),
    "numeric_id": ('{{**scope, "campaign_id": 7}}', "conflict"),
    "missing_id": ('{{k: v for k, v in scope.items() if k != "campaign_id"}}', "conflict"),
    "missing_kind": ('{{k: v for k, v in scope.items() if k != "kind"}}', "conflict"),
    "unknown_kind": ('{{**scope, "kind": "user"}}', "conflict"),
    "numeric_kind": ('{{**scope, "kind": 3}}', "conflict"),
    "extra_target_a": ('{{**scope, "target": A}}', "conflict"),
    "extra_list_a": ('{{**scope, "campaign_ids": [A]}}', "conflict"),
    "missing_policy_key": ('{{k: v for k, v in scope.items() if k != "policy_version"}}', "conflict"),
    "null_item": ("None", "conflict"),
    "nested_document": ('{{"scopes": [scope]}}', "conflict"),
}


@pytest.mark.parametrize("form", sorted(R5_FORMS))
def test_r1_r5_the_lenient_reading_only_excludes_the_campaign_when_structure_and_nature_are_identifiable(
        db, clock, form):
    a, b, event, document = _b_only_event(db, clock)
    [scope] = document["scopes"]
    expression, expected = R5_FORMS[form]
    item = eval(expression.format(scope="scope").replace("{{", "{").replace("}}", "}"),  # noqa: S307 — test
                {"scope": scope, "A": str(a), "B": scope["campaign_id"]})
    _forge_document(db, event.id, {"version": "retention-scopes-v1", "scopes": [item]})  # admis par 0022
    _expire_campaign(db, clock, a)
    before = rows(db, f"SELECT *, xmin::text AS xmin FROM {EVENTS}")
    outcome = purge(db, a)
    if expected == "purged":
        assert outcome.outcome == lb.PURGED, outcome
    else:
        assert outcome == lb.PurgeOutcome(lb.CONFLICT, pl.RETENTION_INCOHERENT)
        assert _participation(db, a) is not None
    assert rows(db, f"SELECT *, xmin::text AS xmin FROM {EVENTS}") == before  # jamais réparé ni supprimé
    # Non-régression PR 2 : la lecture tolérante ne déclare jamais un document valide.
    integrity = committed(db, lambda s: el.inspect_event_integrity(s, event_id=event.id))
    assert integrity == (el.IntegrityDecision(el.VALID) if form == "canonical_b"
                         else el.IntegrityDecision(el.INCONSISTENT, el.RETENTION_INCOHERENT)), (form, integrity)
    del b


@pytest.mark.parametrize("document", [
    {"version": "retention-scopes-v1", "scopes": [], "extra": 1},
    {"version": "retention-scopes-v2", "scopes": [{}]},
    {"version": "retention-scopes-v1", "scopes": []},
    {"version": "retention-scopes-v1", "scopes": {"0": {}}},
    ["retention-scopes-v1"],
])
def test_r1_r5_the_outer_document_forms_are_refused_by_the_0022_check(db, clock, document):
    """Seules les formes des ÉLÉMENTS sont admises par la base : l'enveloppe est
    imposée par ck_product_activity_events_retention_scopes."""
    _, _, event, _ = _b_only_event(db, clock)
    with pytest.raises(sa.exc.IntegrityError, match="ck_product_activity_events_retention_scopes"):
        _forge_document(db, event.id, document)


def test_r1_the_reread_refuses_an_identity_that_would_need_another_key(db, clock, monkeypatch):
    """Relecture complémentaire : environment_digest modifié entre la lecture
    indicative et le verrou (visite sans interaction, écrivain hors service) :
    unstable, jamais present ; aucune clé supplémentaire."""
    campaign_id = _wave(db)
    visit_id = uuid.uuid4()
    _sql(db, f"INSERT INTO {SESSIONS} (id, campaign_id, user_id, environment_digest, opened_at, last_activity_at, "
             "idle_expires_at) VALUES (:i, :c, :u, :d, :t, :t, :t + interval '30 minutes')", i=visit_id,
         c=campaign_id, u=USER_A, d="a" * 64, t=clock.now)
    real, calls = pl._visit, []

    def changing(db_, session_id, *, mode=None):
        calls.append(mode)
        if len(calls) == 2:  # après la lecture indicative et la clé
            _sql(db, f"UPDATE {SESSIONS} SET environment_digest = :d WHERE id = :i", d="b" * 64, i=visit_id)
        return real(db_, session_id, mode=mode)
    monkeypatch.setattr(pl, "_visit", changing)
    seen, remove = _statements(db)
    try:
        assert committed(db, lambda s: pl.observe_session_finalization(s, session_id=visit_id)).outcome == pl.UNSTABLE
    finally:
        remove()
    assert len([s for s in seen if "pg_advisory" in s]) == 1  # aucune clé tardive
    monkeypatch.setattr(pl, "_visit", real)
    # Relecture d'une tentative : clé et empreinte de l'opération tentée, jamais celles de la ligne relue.
    visit = committed(db, lambda s: s.get(pl.ProductActivitySession, visit_id))
    attempt = pl._SessionAttempt(visit_id, campaign_id, USER_A, pl.session_lock_key(campaign_id, USER_A, "a" * 64),
                                 pl._row(visit), pl._row(visit))
    assert committed(db, lambda s: pl._observe_session_attempt(s, attempt=attempt)).outcome == pl.UNSTABLE
