"""Tests de V0-T08 Lot 2A — inscription Vague 0 à la première connexion
éligible (participation.enroll_at_first_eligible_login), DORMANTE.

Décisions produit couvertes : J0 individuel = première connexion
authentifiée et éligible ; observation de 12 jours complets à partir du J0 ;
testeurs démarrant à des dates différentes ; connexion répétée sans
réinitialisation ; aucune prolongation de campagne ; conservation de 90
jours depuis occurred_at.

1. Sans base : signature sans aucune date de fenêtre (l'appelant ne peut pas
   fournir le J0), durée d'observation portée par la politique de mesure,
   validation avant tout accès à la base, aucun service ne modifie
   close_by_at, statut provisoire documenté de LATE_EVENT_GRACE.
2. PostgreSQL réel : connexion initiale et J0 exact, bornes [J0, J0 + 12 j)
   vérifiées avec de vrais événements, cinq démarrages décalés, J0 trop
   tardif pour close_by_at (campagne inchangée), connexion répétée,
   campagne non active, concurrence réelle sur la première connexion
   (gagnant validé, gagnant annulé, échéance franchie pendant l'attente,
   clôture concurrente), ancrage de la conservation sur occurred_at.
"""
import ast
import threading
import inspect
import uuid
from datetime import timedelta

import pytest

from core.product_analytics import campaigns as pc
from core.product_analytics import common
from core.product_analytics import events as pe
from core.product_analytics import participation as pp
from core.product_analytics.common import (
    EventNotAuthorized,
    InvalidProductAnalyticsInput,
    ParticipationConflict,
    ParticipationRefused,
)
from tests.test_cognitive_capture import _NoDB, _Statements, _writes
from tests.test_migration_0002_analysis_sessions import REPO_ROOT
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_product_analytics_audit_r1 import _run_while_blocked
from tests.test_product_analytics_services import (  # noqa: F401 — fixtures engine, Sessions, clock
    OTHER_USER,
    T0,
    USER,
    Sessions,
    _active,
    _bring_to,
    _committed,
    _count,
    _event_kwargs,
    _rows,
    _snapshot,
    clock,
    engine,
)

PACKAGE = REPO_ROOT / "core" / "product_analytics"
TWELVE_DAYS = timedelta(days=12)
NOTICE = "v0-notice-1"
TESTERS = ("pa-tester-1", "pa-tester-2", "pa-tester-3", "pa-tester-4", "pa-tester-5")


def _login(session, campaign_id, user=USER, authorized_at=T0, notice=NOTICE):
    return pp.enroll_at_first_eligible_login(session, campaign_id=campaign_id, user_id=user,
                                             authorization_recorded_at=authorized_at, notice_version=notice)


def _committed_login(Sessions, campaign_id, user=USER, **kwargs):
    with Sessions() as session:
        participation, created = _login(session, campaign_id, user, **kwargs)
        result = (participation.window_start_at, participation.window_end_at, created)
        session.commit()
        return result


def _event_at(occurred, user=USER):
    return _event_kwargs(user_id=user, occurred_at=occurred, resolved_at=occurred)


def _record(Sessions, kwargs):
    with Sessions() as session:
        event, created = pe.record_event(session, **kwargs)
        event_id = event.id
        session.commit()
        return event_id, created


def _participants(engine):
    return {row["user_id"]: row for row in _rows(engine, "product_campaign_participants", "user_id")}


@pytest.fixture
def testers(engine, Sessions):
    """Cinq testeurs Vague 0 supplémentaires (supprimés par le nettoyage de Sessions)."""
    with engine.begin() as conn:
        for tester in TESTERS:
            conn.exec_driver_sql("INSERT INTO users (id, level) VALUES (%s, 'debutant')", (tester,))
    return TESTERS


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_first_login_signature_accepts_no_window_date():
    parameters = inspect.signature(pp.enroll_at_first_eligible_login).parameters
    assert list(parameters) == ["db", "campaign_id", "user_id", "authorization_recorded_at", "notice_version"]
    for name, parameter in parameters.items():
        if name != "db":
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is inspect.Parameter.empty
    for forbidden in ("window_start_at", "window_end_at", "j0", "enrolled_at", "now"):
        with pytest.raises(TypeError):
            pp.enroll_at_first_eligible_login(_NoDB(), campaign_id=uuid.uuid4(), user_id=USER,
                                              authorization_recorded_at=T0, notice_version=NOTICE,
                                              **{forbidden: T0})


def test_observation_window_is_carried_by_the_measurement_policy():
    assert common.OBSERVATION_WINDOWS == {"v0-measurement-v1": timedelta(days=12)}
    assert set(common.OBSERVATION_WINDOWS) == set(common.MEASUREMENT_POLICIES)
    assert common.OBSERVATION_WINDOWS[common.WAVE_0_POLICY.measurement_policy_version] == TWELVE_DAYS
    assert common.WAVE_0_POLICY.retention_days == 90


@pytest.mark.parametrize("overrides", [
    {"campaign_id": "wave-0"}, {"user_id": ""}, {"user_id": None}, {"authorization_recorded_at": None},
    {"authorization_recorded_at": T0.replace(tzinfo=None)}, {"notice_version": None},
    {"notice_version": "Notice V0"}, {"notice_version": ["v0-notice-1"]},
])
def test_first_login_validates_before_any_database_access(overrides):
    kwargs = {"campaign_id": uuid.uuid4(), "user_id": USER, "authorization_recorded_at": T0,
              "notice_version": NOTICE, **overrides}
    with pytest.raises(InvalidProductAnalyticsInput):
        pp.enroll_at_first_eligible_login(_NoDB(), **kwargs)


def test_no_service_ever_changes_close_by_at():
    """close_by_at n'est posée qu'à la création : aucune affectation
    d'attribut close_by_at dans le paquet (donc aucune prolongation)."""
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        targets = [target for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                   for target in (node.targets if isinstance(node, ast.Assign) else [node.target])]
        assert not [t for t in targets if isinstance(t, ast.Attribute) and t.attr == "close_by_at"], path.name


def test_late_event_grace_is_documented_as_a_provisional_restriction():
    assert pe.LATE_EVENT_GRACE == timedelta(0)
    doc = " ".join(pe.__doc__.split()).lower()
    assert "restriction provisoire du service dormant, pas le contrat produit final" in doc
    assert "relève du lot 2b" in doc
    assert "aucune donnée survenue après window_end_at n'entre dans les métriques" in doc


# --------------------------------------------------------------------------
# 2. PostgreSQL réel
# --------------------------------------------------------------------------

def test_pg_first_login_creates_an_exact_twelve_day_window_from_server_j0(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)  # close_by_at = T0 + 60 j
    campaign_before = _rows(engine, "product_campaigns")
    clock.advance(hours=1, microseconds=7)
    j0 = clock.now
    with Sessions() as session:
        participation, created = _login(session, campaign_id)
        assert created is True and participation.window_start_at == j0
        assert _count(engine, "product_campaign_participants") == 0  # rien n'est commité par le service
        session.commit()
    [row] = _rows(engine, "product_campaign_participants")
    assert row == {"campaign_id": campaign_id, "user_id": USER, "window_start_at": j0,
                   "window_end_at": j0 + TWELVE_DAYS, "participation_status": "enrolled", "enrolled_at": j0,
                   "authorization_recorded_at": T0, "notice_version": NOTICE}
    assert row["window_end_at"] - row["window_start_at"] == timedelta(days=12)
    assert _rows(engine, "product_campaigns") == campaign_before
    assert _count(engine, "product_activity_events") == 0


def test_pg_window_bounds_are_enforced_on_real_events(engine, Sessions, clock):
    """Chaque borne isolée : [J0, J0 + 12 j) sur occurred_at, et
    enregistrement avant l'échéance (LATE_EVENT_GRACE = 0, provisoire)."""
    campaign_id = _active(Sessions, clock)
    clock.advance(hours=1)
    j0 = clock.now
    _committed_login(Sessions, campaign_id)
    end = j0 + TWELVE_DAYS
    last = end - timedelta(microseconds=1)
    assert _record(Sessions, _event_at(j0))[1] is True  # borne J0 incluse, enregistrée au J0
    clock.now = j0 + timedelta(hours=1)
    with Sessions() as session:  # antérieur au J0, fenêtre ouverte : refusé
        with pytest.raises(EventNotAuthorized):
            pe.record_event(session, **_event_at(j0 - timedelta(microseconds=1)))
    clock.now = last
    assert _record(Sessions, _event_at(last))[1] is True  # dernière µs, enregistrée à temps
    refused = ((last, end),                       # survenu dans la fenêtre, remis à l'échéance
               (end, end + timedelta(hours=1)))   # survenu à l'échéance : exclu
    for occurred, recorded in refused:
        clock.now = recorded
        with Sessions() as session:
            with pytest.raises(EventNotAuthorized):
                pe.record_event(session, **_event_at(occurred))
    assert _count(engine, "product_activity_events") == 2


def test_pg_five_testers_starting_on_different_days_each_get_twelve_full_days(engine, Sessions, clock, testers):
    offsets = (timedelta(hours=1), timedelta(days=1), timedelta(days=3, hours=5), timedelta(days=7),
               timedelta(days=10))
    campaign_id = _active(Sessions, clock, close_by_at=T0 + timedelta(days=22))  # dernier J0 admis : T0 + 10 j
    j0s = {}
    for tester, offset in zip(testers, offsets):
        clock.now = T0 + offset
        j0s[tester] = clock.now
        assert _committed_login(Sessions, campaign_id, tester) == (clock.now, clock.now + TWELVE_DAYS, True)
    rows = _participants(engine)
    for tester in testers:
        assert (rows[tester]["window_start_at"], rows[tester]["window_end_at"]) == (j0s[tester],
                                                                                    j0s[tester] + TWELVE_DAYS)
    # La dernière microseconde de chaque fenêtre est acceptée, la suivante refusée,
    # indépendamment des fenêtres des autres testeurs.
    for tester in sorted(testers, key=j0s.get):
        last = j0s[tester] + TWELVE_DAYS - timedelta(microseconds=1)
        clock.now = last
        assert _record(Sessions, _event_at(last, tester))[1] is True
    for tester in testers:
        clock.now = j0s[tester] + TWELVE_DAYS
        with Sessions() as session:
            with pytest.raises(EventNotAuthorized):
                pe.record_event(session, **_event_at(clock.now - timedelta(seconds=1), tester))
    assert _count(engine, "product_activity_events") == 5


@pytest.mark.parametrize("late", [timedelta(microseconds=1), timedelta(days=1)])
def test_pg_first_login_too_close_to_close_by_at_is_refused_without_extending_the_campaign(engine, Sessions, clock,
                                                                                             late):
    campaign_id = _active(Sessions, clock, close_by_at=T0 + timedelta(days=22))
    clock.now = T0 + timedelta(days=10) + late
    before = _snapshot(engine)
    with Sessions() as session, _Statements(engine) as statements:
        with pytest.raises(ParticipationRefused):
            _login(session, campaign_id)
        session.commit()
    assert [w for w in _writes(statements) if not w.startswith(("SAVEPOINT", "RELEASE"))] == []
    assert _snapshot(engine) == before  # aucune participation, close_by_at inchangée
    # À la limite exacte, la fenêtre complète tient : J0 + 12 j == close_by_at.
    clock.now = T0 + timedelta(days=10)
    assert _committed_login(Sessions, campaign_id)[1] == T0 + timedelta(days=22)


def test_pg_repeated_login_never_resets_the_window(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    clock.advance(hours=1)
    j0 = clock.now
    _committed_login(Sessions, campaign_id)
    before = _snapshot(engine)
    for later in (timedelta(minutes=31), timedelta(days=3), timedelta(days=12), timedelta(days=13)):
        clock.now = j0 + later
        with Sessions() as session, _Statements(engine) as statements:
            participation, created = _login(session, campaign_id)
            assert (participation.window_start_at, participation.window_end_at, created) == (
                j0, j0 + TWELVE_DAYS, False)
            session.commit()
        assert _writes(statements) == []
        assert _snapshot(engine) == before


def test_pg_repeated_login_with_another_authorization_trace_or_after_withdrawal_is_refused(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    clock.advance(hours=1)
    _committed_login(Sessions, campaign_id)
    clock.advance(days=1)
    before = _snapshot(engine)
    for kwargs in ({"authorized_at": T0 + timedelta(minutes=1)}, {"notice": "v0-notice-2"}):
        with Sessions() as session:
            with pytest.raises(ParticipationConflict):
                _login(session, campaign_id, **kwargs)
    assert _snapshot(engine) == before
    _committed(Sessions, lambda s: pp.withdraw_participant(s, campaign_id=campaign_id, user_id=USER))
    withdrawn = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(ParticipationConflict):  # aucune réinscription, aucune nouvelle fenêtre
            _login(session, campaign_id)
    assert _snapshot(engine) == withdrawn


def test_pg_login_on_an_explicitly_enrolled_participation_keeps_its_window(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    _committed(Sessions, lambda s: pp.enroll_participant(
        s, campaign_id=campaign_id, user_id=USER, window_start_at=T0, window_end_at=T0 + timedelta(days=5),
        authorization_recorded_at=T0, notice_version=NOTICE))
    clock.advance(days=2)
    assert _committed_login(Sessions, campaign_id) == (T0, T0 + timedelta(days=5), False)


@pytest.mark.parametrize("status", ["planned", "closed", "cancelled"])
def test_pg_first_login_requires_an_active_campaign(engine, Sessions, clock, status):
    campaign_id = _bring_to(Sessions, clock, status)
    clock.advance(hours=1)
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(ParticipationRefused):
            _login(session, campaign_id)
    assert _snapshot(engine) == before


def test_pg_first_login_refuses_a_future_authorization_trace_and_an_unknown_user(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    clock.advance(hours=1)
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(ParticipationRefused):
            _login(session, campaign_id, authorized_at=clock.now + timedelta(microseconds=1))
        with pytest.raises(common.ProductAnalyticsUserNotFound):
            _login(session, campaign_id, user="ghost")
        with pytest.raises(common.CampaignNotFound):
            _login(session, uuid.uuid4())
    assert _snapshot(engine) == before
    # Trace enregistrée exactement au J0 : admise.
    assert _committed_login(Sessions, campaign_id, authorized_at=clock.now)[2] is True


def test_pg_first_login_refuses_a_measurement_policy_without_observation_window(engine, Sessions, clock,
                                                                                monkeypatch):
    monkeypatch.setattr(common, "MEASUREMENT_POLICIES", (*common.MEASUREMENT_POLICIES, "v0-measurement-x"))
    campaign_id = _active(Sessions, clock, measurement_policy_version="v0-measurement-x")
    clock.advance(hours=1)
    with Sessions() as session:
        with pytest.raises(ParticipationRefused):
            _login(session, campaign_id)
    assert _count(engine, "product_campaign_participants") == 0


# --- Concurrence réelle sur la première connexion ---------------------------

@pytest.mark.parametrize("release", ["commit", "rollback"])
def test_pg_concurrent_first_logins_share_one_window(engine, Sessions, clock, release):
    """Deux premières connexions concurrentes du même testeur : la seconde
    attend le verrou d'identité ; l'horloge avance pendant l'attente.
    Gagnant validé : la seconde renvoie SA fenêtre (même J0). Gagnant
    annulé : la seconde crée la fenêtre avec un J0 lu APRÈS l'attente."""
    campaign_id = _active(Sessions, clock)
    clock.advance(hours=1)
    first_j0 = clock.now
    with Sessions() as holder, Sessions() as waiter:
        assert _login(holder, campaign_id)[0].window_start_at == first_j0

        def second(session):
            participation, created = _login(session, campaign_id)
            return participation.window_start_at, participation.window_end_at, created
        result, error = _run_while_blocked(engine, holder, waiter, second,
                                           while_blocked=lambda: clock.advance(seconds=5), release=release)
    assert error is None
    if release == "commit":
        assert result == (first_j0, first_j0 + TWELVE_DAYS, False)
    else:
        assert result == (first_j0 + timedelta(seconds=5), first_j0 + timedelta(seconds=5) + TWELVE_DAYS, True)
    [row] = _rows(engine, "product_campaign_participants")
    assert (row["window_start_at"], row["window_end_at"]) == result[:2]


def test_pg_first_login_waiting_for_a_campaign_lock_reads_j0_after_the_wait(engine, Sessions, clock):
    """La campagne est verrouillée (FOR UPDATE d'une clôture finalement
    annulée) ; l'horloge avance pendant l'attente : le J0 est l'instant lu
    APRÈS l'obtention du verrou, jamais celui d'avant l'attente."""
    campaign_id = _active(Sessions, clock)
    clock.advance(hours=1)
    before_wait = clock.now
    with Sessions() as holder, Sessions() as waiter:
        pc.close_campaign(holder, campaign_id=campaign_id)

        def login(session):
            participation, created = _login(session, campaign_id)
            return participation.window_start_at, participation.window_end_at, created
        result, error = _run_while_blocked(engine, holder, waiter, login,
                                           while_blocked=lambda: clock.advance(seconds=7), release="rollback")
    j0 = before_wait + timedelta(seconds=7)
    assert (result, error) == ((j0, j0 + TWELVE_DAYS, True), None)
    assert _rows(engine, "product_campaigns")[0]["status"] == "active"


def test_pg_concurrent_first_login_refused_when_the_wait_makes_twelve_days_impossible(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock, close_by_at=T0 + timedelta(days=13))
    clock.now = T0 + timedelta(days=1) - timedelta(seconds=2)  # J0 + 12 j tient encore (2 s de marge)
    with Sessions() as holder, Sessions() as waiter:
        _login(holder, campaign_id)
        _, error = _run_while_blocked(engine, holder, waiter, lambda s: _login(s, campaign_id),
                                      while_blocked=lambda: clock.advance(seconds=5), release="rollback")
    assert isinstance(error, ParticipationRefused)
    assert _count(engine, "product_campaign_participants") == 0


def test_pg_first_login_waiting_for_a_concurrent_closing_is_refused(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    clock.advance(hours=1)
    with Sessions() as holder, Sessions() as waiter:
        pc.close_campaign(holder, campaign_id=campaign_id)  # FOR UPDATE, puis validée
        _, error = _run_while_blocked(engine, holder, waiter, lambda s: _login(s, campaign_id),
                                      while_blocked=lambda: None, release="commit")
    assert isinstance(error, ParticipationRefused)
    assert _count(engine, "product_campaign_participants") == 0


def test_pg_concurrent_first_logins_of_different_testers_do_not_block_each_other(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    clock.advance(hours=1)
    with Sessions() as first, Sessions() as second:
        _login(first, campaign_id, USER)
        _login(second, campaign_id, OTHER_USER)  # verrous d'identité distincts, campagne en partage
        first.commit()
        second.commit()
    assert set(_participants(engine)) == {USER, OTHER_USER}


# --- Conservation : 90 jours depuis occurred_at (décision figée) ------------

def test_pg_retention_is_anchored_on_occurred_at_even_when_recorded_late(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    clock.advance(hours=1)
    j0 = clock.now
    _committed_login(Sessions, campaign_id)
    occurred = j0 + timedelta(hours=2)
    clock.now = j0 + timedelta(days=4)  # remis plus tard, fenêtre encore ouverte
    _record(Sessions, _event_at(occurred))
    [row] = _rows(engine, "product_activity_events")
    assert row["recorded_at"] == clock.now
    assert row["next_scope_expires_at"] == row["purge_after_at"] == occurred + timedelta(days=90)
    assert row["retention_scopes"]["scopes"][0]["expires_at"] == (occurred + timedelta(days=90)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


# --- Dormance -----------------------------------------------------------------

def test_first_login_service_is_referenced_by_no_application_module():
    offenders = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        parts = path.relative_to(REPO_ROOT).parts
        if parts[0] in ("tests", ".git") or "node_modules" in parts or path.parent == PACKAGE:
            continue
        if "enroll_at_first_eligible_login" in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []
    for directory in ("web", "web-v2"):
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if path.is_file() and path.suffix in (".html", ".js", ".mjs", ".css", ".json"):
                assert "first_eligible_login" not in path.read_text(encoding="utf-8", errors="ignore"), path


@pytest.mark.parametrize("method", ["first_login", "explicit"])
def test_pg_primary_key_remains_the_final_defense_for_enrollments(engine, Sessions, clock, monkeypatch, method):
    """Écrivain concurrent qui ne prend PAS le verrou d'identité (simulé : le
    verrou n'est pris que dans le fil principal) : le perdant attend sur la
    clé primaire, relit la participation gagnante et la renvoie telle quelle
    (created=False), sans doublon ni seconde fenêtre."""
    real_lock = common.lock_identities
    main = threading.main_thread()
    monkeypatch.setattr(common, "lock_identities",
                        lambda db, *keys: real_lock(db, *keys) if threading.current_thread() is main else None)
    campaign_id = _active(Sessions, clock)
    clock.advance(hours=1)
    start = clock.now

    def enroll(session):
        if method == "first_login":
            participation, created = _login(session, campaign_id)
        else:
            participation, created = pp.enroll_participant(
                session, campaign_id=campaign_id, user_id=USER, window_start_at=start,
                window_end_at=start + TWELVE_DAYS, authorization_recorded_at=T0, notice_version=NOTICE)
        return participation.window_start_at, participation.window_end_at, created
    with Sessions() as holder, Sessions() as waiter:
        enroll(holder)
        result, error = _run_while_blocked(engine, holder, waiter, enroll,
                                           while_blocked=lambda: clock.advance(seconds=3), release="commit")
    assert (result, error) == ((start, start + TWELVE_DAYS, False), None)
    assert _count(engine, "product_campaign_participants") == 1
