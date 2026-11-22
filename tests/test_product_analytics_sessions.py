"""Tests de V0-T08 Lot 2B-1 — sessions d'activité produit
(core/product_analytics/sessions.py), DORMANTES.

1. Sans base : inactivité de 30 minutes identique à la migration 0023 et
   distincte des 15 jours F2, empreinte d'environnement (jamais la clé
   brute), signatures sans aucune date, validation avant tout accès à la
   base, contrat non bloquant qui ne lève jamais.
2. PostgreSQL réel (base jetable ; sinon SKIPPÉS) : onglets multiples et
   rafraîchissement dans une même visite, appareils différents pour un même
   utilisateur, expiration à exactement 30 minutes, activité juste avant
   l'expiration, rejeu (ouverture et dernière interaction), concurrence
   réelle (pg_blocking_pids), interaction après J+12 et hors observation,
   finalisation d'une fermeture brutale sans événement inventé, contraintes
   SQL. Aucune lecture des sessions F2.

Participations créées par le point d'inscription 2A (campagnes sans
cohorte) : les sessions ne dépendent que de la participation ; le parcours
complet cohorte -> J0 -> session est couvert par un test dédié.
"""
import inspect
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from core.product_analytics import campaigns as pc
from core.product_analytics import common
from core.product_analytics import participation as pp
from core.product_analytics import sessions as ps
from core.product_analytics.common import ActivitySessionConflict, ActivitySessionRefused, InvalidProductAnalyticsInput
from tests.test_cognitive_capture import _NoDB, _run_blocked, _Statements
from tests.test_migration_0002_analysis_sessions import _reset_schema, _run_alembic
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0016_portfolio_idempotency import _rejects
from tests.test_product_analytics_cohort import (
    ANALYTICS_TABLES,
    F2_FIXTURE_TABLES,
    NOTICE,
    _admit,
    _committed,
    _count,
    _m0023,
    _ready,
    _rows,
)
from tests.test_product_analytics_services import POLICY, T0, clock  # noqa: F401 — fixture clock

USER, OTHER_USER = "pa-session-user-1", "pa-session-user-2"
TWELVE_DAYS = timedelta(days=12)
THIRTY_MINUTES = timedelta(minutes=30)
LAPTOP, PHONE = uuid.UUID("0b8f0e8e-0000-4000-8000-000000000001"), uuid.UUID("0b8f0e8e-0000-4000-8000-000000000002")


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_inactivity_is_thirty_minutes_and_unrelated_to_f2_credentials():
    m = _m0023()
    assert common.ACTIVITY_SESSION_INACTIVITY == THIRTY_MINUTES and m.SESSION_INACTIVITY == "30 minutes"
    assert ("ck_product_activity_sessions_idle_expiry",
            "idle_expires_at = last_activity_at + interval '30 minutes'") in m.SESSION_CHECKS
    assert (ps.OPENING_KEY_UNIQUE, ps.OPEN_ENVIRONMENT_UNIQUE) == (m.SESSION_OPENING_UNIQUE, m.SESSION_OPEN_UNIQUE)
    code = open(ps.__file__, encoding="utf-8").read().replace(ps.__doc__, "")  # hors docstring
    for marker in ("oryx_sessions", "OryxSession", "absolute_expires_at", "auth_f2", "f2_runtime", "AuthOperation"):
        assert marker not in code, marker


def test_environment_digest_is_stable_and_never_the_raw_key():
    digest = ps.environment_digest(LAPTOP)
    assert digest == ps.environment_digest(uuid.UUID(str(LAPTOP))) and len(digest) == 64
    assert digest != ps.environment_digest(PHONE)
    assert str(LAPTOP) not in digest and LAPTOP.hex not in digest
    with pytest.raises(InvalidProductAnalyticsInput):
        ps.environment_digest(str(LAPTOP))


def test_signatures_take_no_date_and_validate_before_any_database_access():
    for function in (ps.touch_activity_session, ps.attempt_session_touch):
        assert list(inspect.signature(function).parameters) == ["db", "campaign_id", "user_id", "environment_key",
                                                                 "interaction_key"]
        for name, parameter in inspect.signature(function).parameters.items():
            if name != "db":
                assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is inspect.Parameter.empty
    assert list(inspect.signature(ps.finalize_idle_sessions).parameters) == ["db", "campaign_id"]
    good = {"campaign_id": uuid.uuid4(), "user_id": USER, "environment_key": LAPTOP, "interaction_key": uuid.uuid4()}
    for bad in ({"campaign_id": "c"}, {"user_id": ""}, {"environment_key": "laptop"}, {"interaction_key": 1}):
        with pytest.raises(InvalidProductAnalyticsInput):
            ps.touch_activity_session(_NoDB(), **{**good, **bad})
    with pytest.raises(InvalidProductAnalyticsInput):
        ps.finalize_idle_sessions(_NoDB(), campaign_id="c")


def test_non_blocking_touch_never_raises_even_with_an_unusable_database():
    attempt = ps.attempt_session_touch(_NoDB(), campaign_id=uuid.uuid4(), user_id=USER, environment_key=LAPTOP,
                                       interaction_key=uuid.uuid4())
    assert (attempt.status, attempt.reason, attempt.session_id) == ("failed", "AssertionError", None)


# --------------------------------------------------------------------------
# 2. PostgreSQL réel
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine(pg_url):  # noqa: F811
    eng = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    _reset_schema(eng)
    _run_alembic(pg_url, "upgrade", "head")
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:a, 'debutant'), (:b, 'debutant')"),
                     {"a": USER, "b": OTHER_USER})
    yield eng
    eng.dispose()


@pytest.fixture
def Sessions(engine):
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)  # = core.db.SessionLocal
    yield factory
    with engine.begin() as conn:
        for table in ANALYTICS_TABLES + F2_FIXTURE_TABLES:
            conn.execute(sa.text(f"DELETE FROM {table}"))
        conn.execute(sa.text("DELETE FROM users WHERE id NOT IN (:a, :b)"), {"a": USER, "b": OTHER_USER})


def _participant(Sessions, clock, user=USER, *, close_days=30):
    """Campagne active sans cohorte et J0 de `user` à maintenant."""
    def action(session):
        campaign = pc.create_campaign(session, code=f"wave-0-{uuid.uuid4().hex[:8]}", name="Vague 0",
                                      purpose="Mesurer l'usage des surfaces Oryx.",
                                      close_by_at=clock.now + timedelta(days=close_days), **POLICY)
        pc.activate_campaign(session, campaign_id=campaign.id)
        pp.enroll_at_first_eligible_login(session, campaign_id=campaign.id, user_id=user,
                                          authorization_recorded_at=clock.now, notice_version=NOTICE)
        return campaign.id
    return _committed(Sessions, action)


def _touch(session, campaign_id, *, user=USER, env=LAPTOP, key=None):
    return ps.touch_activity_session(session, campaign_id=campaign_id, user_id=user, environment_key=env,
                                     interaction_key=key or uuid.uuid4())


def _committed_touch(Sessions, campaign_id, **kwargs):
    def action(session):
        touch = _touch(session, campaign_id, **kwargs)
        return touch.session.id, touch.outcome, touch.finalized.id if touch.finalized else None
    return _committed(Sessions, action)


def _sessions(engine):
    return _rows(engine, "product_activity_sessions", "opened_at, id")


def test_pg_several_tabs_and_refreshes_share_one_visit(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    opened = clock.now
    first = _committed_touch(Sessions, campaign_id)
    assert first[1] == "opened"
    for minutes, outcome in ((1, "continued"), (2, "continued"), (20, "continued")):  # onglets, rafraîchissements
        clock.advance(minutes=minutes)
        assert _committed_touch(Sessions, campaign_id) == (first[0], outcome, None)
    [row] = _sessions(engine)
    assert row["id"] == first[0] and row["opened_at"] == opened
    assert row["last_activity_at"] == clock.now and row["idle_expires_at"] == clock.now + THIRTY_MINUTES
    assert row["finalized_at"] is None and row["environment_digest"] == ps.environment_digest(LAPTOP)
    assert str(LAPTOP) not in str(row)  # la clé brute n'est jamais stockée


def test_pg_different_devices_have_distinct_visits_but_one_user(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    users_before = _count(engine, "users")
    laptop = _committed_touch(Sessions, campaign_id, env=LAPTOP)
    phone = _committed_touch(Sessions, campaign_id, env=PHONE)
    assert laptop[1] == phone[1] == "opened" and laptop[0] != phone[0]
    rows = _sessions(engine)
    assert {r["user_id"] for r in rows} == {USER} and len(rows) == 2
    assert _count(engine, "users") == users_before  # aucun utilisateur créé
    assert _count(engine, "product_campaign_participants") == 1  # une seule participation, un seul J0
    clock.advance(minutes=5)
    assert _committed_touch(Sessions, campaign_id, env=LAPTOP)[:2] == (laptop[0], "continued")


def test_pg_expiry_at_exactly_thirty_minutes_finalizes_without_inventing_activity(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    first = _committed_touch(Sessions, campaign_id)
    last = clock.now
    clock.now = last + THIRTY_MINUTES  # borne exclue : la visite est close
    second = _committed_touch(Sessions, campaign_id)
    assert second[1] == "opened" and second[2] == first[0] and second[0] != first[0]
    old, new = _sessions(engine)
    assert old["id"] == first[0] and old["last_activity_at"] == last and old["finalized_at"] == clock.now
    assert old["idle_expires_at"] == last + THIRTY_MINUTES
    assert new["opened_at"] == clock.now and new["finalized_at"] is None
    assert _count(engine, "product_activity_events") == 0  # aucun événement inventé


def test_pg_activity_just_before_expiry_extends_the_visit(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    first = _committed_touch(Sessions, campaign_id)
    clock.now += THIRTY_MINUTES - timedelta(microseconds=1)
    assert _committed_touch(Sessions, campaign_id) == (first[0], "continued", None)
    clock.now += THIRTY_MINUTES - timedelta(microseconds=1)
    assert _committed_touch(Sessions, campaign_id) == (first[0], "continued", None)
    [row] = _sessions(engine)
    assert row["last_activity_at"] == clock.now and row["idle_expires_at"] == clock.now + THIRTY_MINUTES


def test_pg_replays_are_idempotent(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    opening, later = uuid.uuid4(), uuid.uuid4()
    first = _committed_touch(Sessions, campaign_id, key=opening)
    clock.advance(minutes=3)
    _committed_touch(Sessions, campaign_id, key=later)
    before = _sessions(engine)
    clock.advance(minutes=1)
    for key in (opening, later):  # rejeu de l'ouverture, rejeu de la dernière interaction
        with Sessions() as session, _Statements(engine) as statements:
            touch = _touch(session, campaign_id, key=key)
            assert (touch.session.id, touch.outcome) == (first[0], "replayed")
            session.commit()
        assert not [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert _sessions(engine) == before
    clock.advance(hours=2)  # après finalisation : le rejeu de l'ouverture n'ouvre aucune nouvelle visite
    _committed(Sessions, lambda s: ps.finalize_idle_sessions(s, campaign_id=campaign_id))
    assert _committed_touch(Sessions, campaign_id, key=opening)[:2] == (first[0], "replayed")
    assert len(_sessions(engine)) == 1
    with Sessions() as session:  # même clé d'ouverture dans un autre environnement : conflit
        with pytest.raises(ActivitySessionConflict):
            _touch(session, campaign_id, key=opening, env=PHONE)


@pytest.mark.parametrize("release", ["commit", "rollback"])
def test_pg_concurrent_tabs_of_one_environment_open_a_single_visit(engine, Sessions, clock, release):
    campaign_id = _participant(Sessions, clock)
    holder, waiter = Sessions(), Sessions()

    def second_tab(session):
        touch = _touch(session, campaign_id)
        return touch.session.id, touch.outcome

    try:
        held = _touch(holder, campaign_id).session.id
        result, error = _run_blocked(engine, holder, waiter, second_tab, release=release)
    finally:
        holder.close()
        waiter.close()
    assert error is None
    rows = _sessions(engine)
    assert len(rows) == 1
    if release == "commit":
        assert result == (held, "continued")
    else:
        assert result[1] == "opened" and rows[0]["id"] == result[0] != held


def test_pg_concurrent_finalization_never_closes_a_visit_being_extended(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    first = _committed_touch(Sessions, campaign_id)
    clock.advance(minutes=29)
    holder, waiter = Sessions(), Sessions()
    try:
        _touch(holder, campaign_id)  # prolongation en cours (verrou de la session)
        clock.advance(minutes=2)  # l'ancienne échéance est dépassée, la nouvelle non
        result, error = _run_blocked(engine, holder, waiter,
                                     lambda s: [x.id for x in ps.finalize_idle_sessions(s, campaign_id=campaign_id)])
    finally:
        holder.close()
        waiter.close()
    assert error is None and result == []
    [row] = _sessions(engine)
    assert row["id"] == first[0] and row["finalized_at"] is None


def test_pg_no_session_after_j_plus_12_nor_outside_observation(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    j0 = clock.now
    clock.now = j0 + TWELVE_DAYS - timedelta(minutes=1)
    last = _committed_touch(Sessions, campaign_id)
    before = _sessions(engine)
    for at in (j0 + TWELVE_DAYS, j0 + TWELVE_DAYS + timedelta(minutes=10), j0 + TWELVE_DAYS + timedelta(hours=1)):
        clock.now = at
        with Sessions() as session:
            with pytest.raises(ActivitySessionRefused) as refused:
                _touch(session, campaign_id)
        assert refused.value.reason == "observation_ended"
        with Sessions() as session:
            with pytest.raises(ActivitySessionRefused):
                _touch(session, campaign_id, env=PHONE)
    assert _sessions(engine) == before  # ni prolongée, ni rouverte
    [row] = before
    assert row["id"] == last[0] and row["last_activity_at"] < j0 + TWELVE_DAYS
    # Fermeture brutale juste avant J+12 : finalisée pendant les 30 minutes de finalisation.
    clock.now = j0 + TWELVE_DAYS + timedelta(minutes=29)
    assert _committed(Sessions, lambda s: [x.id for x in ps.finalize_idle_sessions(s, campaign_id=campaign_id)]) == [
        last[0]]
    assert _rows(engine, "product_campaigns")[0]["close_by_at"] >= clock.now  # couvert par la campagne


@pytest.mark.parametrize("case", ["not_participant", "participation_inactive", "campaign_not_active"])
def test_pg_interactions_outside_an_active_participation_are_refused(engine, Sessions, clock, case):
    campaign_id = _participant(Sessions, clock)
    user = USER
    if case == "not_participant":
        user = OTHER_USER
    elif case == "participation_inactive":
        _committed(Sessions, lambda s: pp.withdraw_participant(s, campaign_id=campaign_id, user_id=USER))
    else:
        _committed(Sessions, lambda s: pc.close_campaign(s, campaign_id=campaign_id))
    with Sessions() as session:
        with pytest.raises(ActivitySessionRefused) as refused:
            _touch(session, campaign_id, user=user)
    assert refused.value.reason == case and _count(engine, "product_activity_sessions") == 0
    with Sessions() as session:
        attempt = ps.attempt_session_touch(session, campaign_id=campaign_id, user_id=user, environment_key=LAPTOP,
                                           interaction_key=uuid.uuid4())
    assert (attempt.status, attempt.reason, attempt.session_id) == ("refused", case, None)


def test_pg_brutal_browser_close_is_finalized_without_any_event(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    other = _participant(Sessions, clock, user=OTHER_USER)
    laptop = _committed_touch(Sessions, campaign_id, env=LAPTOP)
    clock.advance(minutes=10)
    _committed_touch(Sessions, campaign_id, env=LAPTOP)
    last = clock.now
    clock.advance(minutes=25)
    phone = _committed_touch(Sessions, campaign_id, env=PHONE)
    _committed_touch(Sessions, other, user=OTHER_USER)
    clock.advance(minutes=10)  # laptop : inactif depuis 35 min ; phone : 10 min
    finalized = _committed(Sessions, lambda s: [x.id for x in ps.finalize_idle_sessions(s, campaign_id=campaign_id)])
    assert finalized == [laptop[0]]
    rows = {r["id"]: r for r in _sessions(engine)}
    assert rows[laptop[0]]["finalized_at"] == clock.now and rows[laptop[0]]["last_activity_at"] == last
    assert rows[phone[0]]["finalized_at"] is None
    assert [r["finalized_at"] for r in rows.values() if r["campaign_id"] == other] == [None]  # autre campagne
    assert _count(engine, "product_activity_events") == 0
    assert _committed(Sessions, lambda s: ps.finalize_idle_sessions(s, campaign_id=campaign_id)) == []  # idempotent
    _committed(Sessions, lambda s: pc.close_campaign(s, campaign_id=campaign_id))
    clock.advance(hours=1)  # après la clôture, la finalisation reste possible
    assert _committed(Sessions, lambda s: [x.id for x in ps.finalize_idle_sessions(s, campaign_id=campaign_id)]) == [
        phone[0]]


def test_pg_sessions_never_read_f2_sessions(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    with Sessions() as session, _Statements(engine) as statements:
        _touch(session, campaign_id)
        clock.advance(hours=1)
        _touch(session, campaign_id)
        ps.finalize_idle_sessions(session, campaign_id=campaign_id)
        session.commit()
    assert statements and not [s for s in statements if "oryx_session" in s or "auth_" in s]
    writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert writes and all("product_activity_sessions" in s.split()[1:3] for s in writes), writes


def test_pg_sql_constraints_of_activity_sessions(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    _committed_touch(Sessions, campaign_id)
    [row] = _sessions(engine)
    insert = ("INSERT INTO product_activity_sessions (id, campaign_id, user_id, environment_digest, "
              "opening_interaction_key, last_interaction_key, opened_at, last_activity_at, idle_expires_at, "
              "finalized_at) VALUES (gen_random_uuid(), :c, :u, :d, gen_random_uuid(), gen_random_uuid(), :o, :l, "
              ":i, :f)")
    base = {"c": campaign_id, "u": USER, "d": ps.environment_digest(PHONE), "o": clock.now, "l": clock.now,
            "i": clock.now + THIRTY_MINUTES, "f": None}
    with engine.connect() as conn:
        _rejects(conn, "ck_product_activity_sessions_idle_expiry", insert,
                 **{**base, "i": clock.now + timedelta(hours=8)})  # pas de durée F2 ni d'expiration libre
        _rejects(conn, "ck_product_activity_sessions_activity", insert,
                 **{**base, "l": clock.now - timedelta(minutes=1), "i": clock.now + timedelta(minutes=29)})
        _rejects(conn, "ck_product_activity_sessions_finalized", insert, **{**base, "f": clock.now})
        _rejects(conn, "ck_product_activity_sessions_environment_digest", insert, **{**base, "d": str(PHONE)})
        _rejects(conn, "uq_product_activity_sessions_open_environment", insert, **{**base, "d": row[
            "environment_digest"]})
        _rejects(conn, "product_activity_sessions_participant_fkey", insert, **{**base, "u": OTHER_USER})
        _rejects(conn, "uq_product_activity_sessions_opening_key", insert.replace(
            "gen_random_uuid(), gen_random_uuid(), :o", ":k, gen_random_uuid(), :o"),
            **{**base, "k": row["opening_interaction_key"], "f": clock.now + THIRTY_MINUTES})
        conn.rollback()


def test_pg_full_path_cohort_admission_then_activity_session(engine, Sessions, clock):
    """Parcours interne complet (sans route) : place rattachée par preuve F2
    (fixture), autorisation, J0, puis visite ; refus avant le J0."""
    campaign_id, [user], _, _ = _ready(engine, Sessions, clock)
    with Sessions() as session:
        with pytest.raises(ActivitySessionRefused) as refused:
            _touch(session, campaign_id, user=user)
    assert refused.value.reason == "not_participant"
    j0 = clock.now
    _committed(Sessions, lambda s: _admit(s, campaign_id, user))
    visit = _committed_touch(Sessions, campaign_id, user=user)
    assert visit[1] == "opened" and _sessions(engine)[0]["opened_at"] == j0
