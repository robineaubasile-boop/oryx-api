"""Tests de V0-T08 Lot 2B-1 — sessions d'activité produit
(core/product_analytics/sessions.py), DORMANTES.

1. Sans base : inactivité de 30 minutes identique à la migration 0023 et
   distincte des 15 jours F2, empreinte d'environnement (jamais la clé
   brute), signatures sans aucune date, validation avant tout accès à la
   base, contrat non bloquant qui ne lève jamais, aucune mutation ORM avant
   un SAVEPOINT (audit R1).
2. PostgreSQL réel (base jetable ; sinon SKIPPÉS) : onglets multiples et
   rafraîchissement dans une même visite, appareils différents pour un même
   utilisateur, expiration à exactement 30 minutes, activité juste avant
   l'expiration, concurrence réelle (pg_blocking_pids), interaction après
   J+12 et hors observation, finalisation d'une fermeture brutale sans
   événement inventé, contraintes SQL. Aucune lecture des sessions F2.
   Audit R1 : rejeu de N'IMPORTE QUELLE clé déjà acceptée (scénario
   09:00 A / 09:05 B / 09:10 C / 09:41 rejeu de B), avant et après
   l'expiration, après finalisation, après J+12 et en concurrence, sans
   aucune écriture ; clôture volontaire avant 30 minutes (aucune activité
   inventée, J0 et autres appareils intacts, rejeu idempotent qui ne clôt
   jamais une visite ultérieure, concurrence déconnexion / interaction).

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
from tests.test_product_analytics_services import POLICY, clock  # noqa: F401 — fixture clock

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
    assert (ps.OPEN_ENVIRONMENT_UNIQUE, ps.INTERACTION_PRIMARY_KEY) == (m.SESSION_OPEN_UNIQUE,
                                                                     m.INTERACTION_PRIMARY_KEY)
    assert ps.END_REASONS == m.SESSION_END_REASONS and ps.INTERACTION_KINDS == m.INTERACTION_KINDS
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
    for function in (ps.touch_activity_session, ps.attempt_session_touch, ps.close_activity_session,
                     ps.attempt_session_close):
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
            ps.close_activity_session(_NoDB(), **{**good, **bad})
    with pytest.raises(InvalidProductAnalyticsInput):
        ps.finalize_idle_sessions(_NoDB(), campaign_id="c")


def test_non_blocking_touch_never_raises_even_with_an_unusable_database():
    for attempt_function in (ps.attempt_session_touch, ps.attempt_session_close):
        attempt = attempt_function(_NoDB(), campaign_id=uuid.uuid4(), user_id=USER, environment_key=LAPTOP,
                                   interaction_key=uuid.uuid4())
        assert (attempt.status, attempt.reason, attempt.session_id) == ("failed", "AssertionError", None)


def test_no_mutation_precedes_a_savepoint():
    """Audit R1 : Session.begin_nested() vide d'abord les changements en
    attente (quel que soit autoflush) ; une mutation ORM faite avant lui
    serait écrite HORS du SAVEPOINT. Toute affectation d'attribut des
    services de sessions et de cohorte se trouve donc soit dans une fonction
    imbriquée transmise à l'écrivain (_write / _flush_or_conflict), soit
    lexicalement dans un bloc « with db.begin_nested() »."""
    import ast

    from core.product_analytics import cohort as ch

    def offenders(node, protected=False):
        found = []
        for child in ast.iter_child_nodes(node):
            inside = protected or isinstance(child, ast.FunctionDef) or (
                isinstance(child, ast.With) and any(
                    isinstance(item.context_expr, ast.Call) and getattr(item.context_expr.func, "attr", None)
                    == "begin_nested" for item in child.items))
            if (isinstance(child, (ast.Assign, ast.AugAssign)) and not inside
                    and any(isinstance(t, ast.Attribute) for t in (
                        child.targets if isinstance(child, ast.Assign) else [child.target]))):
                found.append(child.lineno)
            found += offenders(child, inside)
        return found

    for module, writer in ((ps, "_write"), (ch, "_flush_or_conflict")):
        tree = ast.parse(open(module.__file__, encoding="utf-8").read())
        mutating = 0
        for function in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
            assert offenders(function) == [], (module.__name__, function.name)
            if any(isinstance(n, ast.FunctionDef) for n in ast.walk(function) if n is not function):
                mutating += 1
                assert [n for n in ast.walk(function) if isinstance(n, ast.Call)
                        and getattr(n.func, "id", None) == writer], (module.__name__, function.name)
        assert mutating >= 2, module.__name__


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


def _writes_of(statements):
    return [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]


def _replay(Sessions, engine, campaign_id, key, **kwargs):
    """Rejoue `key` ; vérifie qu'AUCUNE écriture n'a lieu ; renvoie la visite."""
    with Sessions() as session, _Statements(engine) as statements:
        touch = _touch(session, campaign_id, key=key, **kwargs)
        result = (touch.session.id, touch.outcome)
        session.commit()
    assert _writes_of(statements) == [], statements
    return result


def _interactions(engine):
    return _rows(engine, "product_activity_session_interactions", "recorded_at, interaction_key")


def test_pg_old_replay_before_and_after_expiry_never_creates_or_extends_a_visit(engine, Sessions, clock):
    """Scénario de l'audit : 09:00 A (ouverture), 09:05 B, 09:10 C ; rejeux
    de B à 09:20 (visite vivante) puis à 09:41 (visite expirée)."""
    campaign_id = _participant(Sessions, clock)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    start = clock.now
    visit = _committed_touch(Sessions, campaign_id, key=a)[0]
    clock.now = start + timedelta(minutes=5)
    assert _committed_touch(Sessions, campaign_id, key=b)[:2] == (visit, "continued")
    clock.now = start + timedelta(minutes=10)
    assert _committed_touch(Sessions, campaign_id, key=c)[:2] == (visit, "continued")
    before = _sessions(engine)
    assert before[0]["idle_expires_at"] == start + timedelta(minutes=40)
    for at in (timedelta(minutes=20), timedelta(minutes=41), timedelta(hours=5)):
        clock.now = start + at
        for key in (b, a, c):  # rejeu ancien, rejeu de l'ouverture, rejeu de la dernière interaction
            assert _replay(Sessions, engine, campaign_id, key) == (visit, "replayed")
        assert _sessions(engine) == before  # ni prolongée, ni finalisée, ni rouverte
    assert [r["interaction_key"] for r in _interactions(engine)] == [a, b, c]
    assert {r["session_id"] for r in _interactions(engine)} == {visit}
    # Une interaction réellement NOUVELLE après l'expiration ouvre bien une nouvelle visite.
    fresh = _committed_touch(Sessions, campaign_id)
    assert fresh[1] == "opened" and fresh[2] == visit
    clock.advance(minutes=1)  # rejeu de B après la nouvelle ouverture : toujours la visite d'origine
    assert _replay(Sessions, engine, campaign_id, b) == (visit, "replayed")
    assert _committed_touch(Sessions, campaign_id)[:2] == (fresh[0], "continued")


def test_pg_replays_after_finalization_and_across_environments(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    a, b = uuid.uuid4(), uuid.uuid4()
    visit = _committed_touch(Sessions, campaign_id, key=a)[0]
    clock.advance(minutes=3)
    _committed_touch(Sessions, campaign_id, key=b)
    clock.advance(hours=2)
    _committed(Sessions, lambda s: ps.finalize_idle_sessions(s, campaign_id=campaign_id))
    for key in (a, b):
        assert _replay(Sessions, engine, campaign_id, key) == (visit, "replayed")
    assert len(_sessions(engine)) == 1
    for key in (a, b):  # même clé dans un autre environnement : conflit, rien d'écrit
        with Sessions() as session:
            with pytest.raises(ActivitySessionConflict):
                _touch(session, campaign_id, key=key, env=PHONE)
    assert len(_sessions(engine)) == 1 and len(_interactions(engine)) == 2


def test_pg_a_replay_after_j_plus_12_creates_nothing(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    j0 = clock.now
    clock.now = j0 + TWELVE_DAYS - timedelta(minutes=5)
    key = uuid.uuid4()
    visit = _committed_touch(Sessions, campaign_id, key=key)[0]
    clock.now = j0 + TWELVE_DAYS + timedelta(minutes=10)
    assert _replay(Sessions, engine, campaign_id, key) == (visit, "replayed")
    with Sessions() as session:  # une interaction NOUVELLE reste refusée
        with pytest.raises(ActivitySessionRefused):
            _touch(session, campaign_id)
    assert len(_sessions(engine)) == 1


@pytest.mark.parametrize("release", ["commit", "rollback"])
def test_pg_concurrent_replays_of_one_key_are_accepted_once(engine, Sessions, clock, release):
    campaign_id = _participant(Sessions, clock)
    visit = _committed_touch(Sessions, campaign_id)[0]
    clock.advance(minutes=29)  # la clé K prolongerait la visite ; ses rejeux concurrents ne doivent rien ajouter
    key = uuid.uuid4()
    holder, waiter = Sessions(), Sessions()

    def replay(session):
        touch = _touch(session, campaign_id, key=key)
        return touch.session.id, touch.outcome

    try:
        assert replay(holder) == (visit, "continued")
        clock.advance(seconds=10)
        result, error = _run_blocked(engine, holder, waiter, replay, release=release)
    finally:
        holder.close()
        waiter.close()
    assert error is None
    [row] = _sessions(engine)
    if release == "commit":  # le perdant voit la clé acceptée : rejeu, aucune prolongation supplémentaire
        assert result == (visit, "replayed") and row["last_activity_at"] == clock.now - timedelta(seconds=10)
    else:  # le gagnant est annulé : la clé est acceptée UNE fois, par le perdant
        assert result == (visit, "continued") and row["last_activity_at"] == clock.now
    assert [r["interaction_key"] for r in _interactions(engine)][1:] == [key]


def test_pg_concurrent_replays_of_an_old_key_write_nothing(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    old = uuid.uuid4()
    visit = _committed_touch(Sessions, campaign_id, key=old)[0]
    clock.advance(minutes=45)
    before = (_sessions(engine), _interactions(engine))
    holder, waiter = Sessions(), Sessions()
    try:
        assert _touch(holder, campaign_id, key=old).outcome == "replayed"  # détient les verrous consultatifs
        result, error = _run_blocked(engine, holder, waiter,
                                     lambda s: (_touch(s, campaign_id, key=old).outcome))
    finally:
        holder.close()
        waiter.close()
    assert (result, error) == ("replayed", None)
    assert (_sessions(engine), _interactions(engine)) == before and before[0][0]["id"] == visit


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


def _close(session, campaign_id, *, user=USER, env=LAPTOP, key=None):
    return ps.close_activity_session(session, campaign_id=campaign_id, user_id=user, environment_key=env,
                                     interaction_key=key or uuid.uuid4())


def _committed_close(Sessions, campaign_id, **kwargs):
    def action(session):
        result = _close(session, campaign_id, **kwargs)
        return result.outcome, result.session.id if result.session is not None else None
    return _committed(Sessions, action)


def test_pg_voluntary_logout_ends_the_visit_before_thirty_minutes_without_inventing_anything(
        engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    participation_before = _rows(engine, "product_campaign_participants")
    laptop = _committed_touch(Sessions, campaign_id, env=LAPTOP)[0]
    phone = _committed_touch(Sessions, campaign_id, env=PHONE)[0]
    clock.advance(minutes=4)
    _committed_touch(Sessions, campaign_id, env=LAPTOP)
    last = clock.now
    clock.advance(minutes=6)  # 6 minutes d'inactivité seulement : bien avant l'expiration
    assert _committed_close(Sessions, campaign_id, env=LAPTOP) == ("closed", laptop)
    rows = {r["id"]: r for r in _sessions(engine)}
    assert (rows[laptop]["finalized_at"], rows[laptop]["end_reason"]) == (clock.now, "logout")
    assert rows[laptop]["last_activity_at"] == last and rows[laptop]["idle_expires_at"] == last + THIRTY_MINUTES
    assert rows[laptop]["finalized_at"] < rows[laptop]["idle_expires_at"]
    assert (rows[phone]["finalized_at"], rows[phone]["end_reason"]) == (None, None)  # autre appareil intact
    assert _rows(engine, "product_campaign_participants") == participation_before  # J0 et fenêtre inchangés
    assert _count(engine, "product_activity_events") == 0
    clock.advance(minutes=1)  # nouvelle connexion dans le même environnement : nouvelle visite
    again = _committed_touch(Sessions, campaign_id, env=LAPTOP)
    assert again[1] == "opened" and again[0] != laptop and again[2] is None


def test_pg_logout_replay_is_idempotent_and_never_closes_a_later_visit(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    first = _committed_touch(Sessions, campaign_id)[0]
    logout, orphan = uuid.uuid4(), uuid.uuid4()
    clock.advance(minutes=2)
    assert _committed_close(Sessions, campaign_id, key=logout) == ("closed", first)
    clock.advance(minutes=1)
    assert _committed_close(Sessions, campaign_id, key=orphan) == ("no_open_session", None)  # clé inscrite
    clock.advance(minutes=1)
    later = _committed_touch(Sessions, campaign_id)[0]
    before = (_sessions(engine), _interactions(engine))
    clock.advance(minutes=1)
    for key, expected in ((logout, ("replayed", first)), (orphan, ("replayed", None))):
        with Sessions() as session, _Statements(engine) as statements:
            result = _close(session, campaign_id, key=key)
            assert (result.outcome, result.session.id if result.session else None) == expected
            session.commit()
        assert _writes_of(statements) == []
    assert (_sessions(engine), _interactions(engine)) == before
    assert {r["id"]: r["finalized_at"] for r in _sessions(engine)}[later] is None  # la visite suivante reste ouverte
    # Une clé de déconnexion n'est jamais une activité, et inversement.
    with Sessions() as session:
        with pytest.raises(ActivitySessionConflict):
            _touch(session, campaign_id, key=logout)
    activity = [r for r in _interactions(engine) if r["interaction_kind"] == "activity"][0]["interaction_key"]
    with Sessions() as session:
        with pytest.raises(ActivitySessionConflict):
            _close(session, campaign_id, key=activity)


def test_pg_logout_of_an_already_expired_visit_records_inactivity(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    visit = _committed_touch(Sessions, campaign_id)[0]
    last = clock.now
    clock.now = last + THIRTY_MINUTES  # la visite est close par inactivité, pas par la déconnexion
    assert _committed_close(Sessions, campaign_id) == ("expired", visit)
    [row] = _sessions(engine)
    assert (row["end_reason"], row["finalized_at"], row["last_activity_at"]) == ("inactivity", clock.now, last)


def test_pg_logout_after_j_plus_12_or_closure_is_bookkeeping_only(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    j0 = clock.now
    clock.now = j0 + TWELVE_DAYS - timedelta(minutes=2)
    visit = _committed_touch(Sessions, campaign_id)[0]
    clock.now = j0 + TWELVE_DAYS + timedelta(minutes=5)
    _committed(Sessions, lambda s: pc.close_campaign(s, campaign_id=campaign_id))
    assert _committed_close(Sessions, campaign_id) == ("closed", visit)
    [row] = _sessions(engine)
    assert row["last_activity_at"] == j0 + TWELVE_DAYS - timedelta(minutes=2)  # aucune activité après J+12
    with Sessions() as session:
        with pytest.raises(ActivitySessionRefused) as refused:
            _close(session, campaign_id, user=OTHER_USER)
    assert refused.value.reason == "not_participant"


@pytest.mark.parametrize("first", ["interaction", "logout"])
def test_pg_concurrent_logout_and_interaction_are_serialized(engine, Sessions, clock, first):
    campaign_id = _participant(Sessions, clock)
    visit = _committed_touch(Sessions, campaign_id)[0]
    clock.advance(minutes=10)
    holder, waiter = Sessions(), Sessions()
    ops = {"interaction": lambda s: _touch(s, campaign_id).outcome, "logout": lambda s: _close(s, campaign_id).outcome}
    second = "logout" if first == "interaction" else "interaction"
    try:
        assert ops[first](holder) == ("continued" if first == "interaction" else "closed")
        held_at = clock.now
        clock.advance(seconds=30)
        result, error = _run_blocked(engine, holder, waiter, ops[second])
    finally:
        holder.close()
        waiter.close()
    assert error is None
    rows = _sessions(engine)
    original = next(r for r in rows if r["id"] == visit)
    assert (original["end_reason"], original["finalized_at"]) == ("logout", held_at if first == "logout"
                                                                  else clock.now)
    if first == "interaction":  # la déconnexion voit la prolongation validée, sans la déplacer
        assert result == "closed" and original["last_activity_at"] == held_at and len(rows) == 1
    else:  # l'interaction arrive après la déconnexion : nouvelle visite, l'ancienne reste close
        assert result == "opened" and len(rows) == 2 and original["last_activity_at"] == held_at - timedelta(
            minutes=10)


def test_pg_non_blocking_logout_reports_refusals(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    visit = _committed_touch(Sessions, campaign_id)[0]
    with Sessions() as session:
        closed = ps.attempt_session_close(session, campaign_id=campaign_id, user_id=USER, environment_key=LAPTOP,
                                          interaction_key=uuid.uuid4())
        outsider = ps.attempt_session_close(session, campaign_id=campaign_id, user_id=OTHER_USER,
                                            environment_key=LAPTOP, interaction_key=uuid.uuid4())
        session.commit()
    assert (closed.status, closed.session_id) == ("closed", visit)
    assert (outsider.status, outsider.reason) == ("refused", "not_participant")


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
    assert writes and all(s.split()[2 if s.startswith("INSERT") else 1].startswith("product_activity_session")
                          for s in writes), writes


def test_pg_sql_constraints_of_activity_sessions(engine, Sessions, clock):
    campaign_id = _participant(Sessions, clock)
    _committed_touch(Sessions, campaign_id)
    [row] = _sessions(engine)
    insert = ("INSERT INTO product_activity_sessions (id, campaign_id, user_id, environment_digest, opened_at, "
              "last_activity_at, idle_expires_at, finalized_at, end_reason) VALUES (gen_random_uuid(), :c, :u, :d, "
              ":o, :l, :i, :f, :r)")
    base = {"c": campaign_id, "u": USER, "d": ps.environment_digest(PHONE), "o": clock.now, "l": clock.now,
            "i": clock.now + THIRTY_MINUTES, "f": None, "r": None}
    with engine.connect() as conn:
        _rejects(conn, "ck_product_activity_sessions_idle_expiry", insert,
                 **{**base, "i": clock.now + timedelta(hours=8)})  # pas de durée F2 ni d'expiration libre
        _rejects(conn, "ck_product_activity_sessions_activity", insert,
                 **{**base, "l": clock.now - timedelta(minutes=1), "i": clock.now + timedelta(minutes=29)})
        _rejects(conn, "ck_product_activity_sessions_finalized", insert,
                 **{**base, "f": clock.now, "r": "inactivity"})  # inactivité avant 30 min
        _rejects(conn, "ck_product_activity_sessions_finalized", insert,
                 **{**base, "f": clock.now + THIRTY_MINUTES, "r": "logout"})  # « déconnexion » après expiration
        _rejects(conn, "ck_product_activity_sessions_end", insert, **{**base, "f": clock.now})
        _rejects(conn, "ck_product_activity_sessions_environment_digest", insert, **{**base, "d": str(PHONE)})
        _rejects(conn, "uq_product_activity_sessions_open_environment", insert, **{**base, "d": row[
            "environment_digest"]})
        _rejects(conn, "product_activity_sessions_participant_fkey", insert, **{**base, "u": OTHER_USER})
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
