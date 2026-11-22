"""Tests de V0-T08 Lot 2B-1 — cohorte pré-J0, calendrier d'admission et J0
individuel (core/product_analytics/cohort.py), DORMANTS.

1. Sans base : registre de cohorte (cinq places) identique à la migration
   0023, échéance minimale de campagne (dernier J0 + 12 jours + 30 minutes),
   signatures (aucun user_id pour le rattachement, aucune date pour le J0),
   validation avant tout accès à la base, vocabulaires de refus fermés,
   contrat non bloquant qui ne lève jamais (même base inutilisable), aucune
   fonction de retrait / remplacement de place, aucune modification de la
   date limite d'admission.
2. PostgreSQL réel (base jetable ; sinon SKIPPÉS) : cohorte de cinq places,
   refus à quatre (et sixième place impossible, service et SQL), invitation
   ou identité réattribuée refusée, même utilisateur sur deux places refusé,
   testeur jamais inscrit conservé ; calendrier ; J0 exact de douze jours,
   connexions concurrentes, répétées, après la date limite (y compris
   franchie pendant l'attente d'un verrou), autorisation absente ou
   incohérente, campagne inactive, points d'inscription bruts refusés ;
   panne analytique sans J0 artificiel ni antidaté ; audit R1 : collision
   d'UNIQUE pendant un rattachement (autoflush désactivé et activé) sans
   écriture partielle, classée CohortConflict, transaction de l'appelant
   toujours utilisable.

Preuves F2 : sauf mention contraire, les lignes invitations / auth_operations
sont des FIXTURES SQL reproduisant la forme que le service F2 écrit (aucune
route, aucun service F2 appelé). Un test unique emploie le VRAI service F2
(core.auth_f2.service, sans HTTP, sans activation de F2 en production) pour
vérifier le contrat de données ; il ne valide AUCUN raccordement F2 de
production, qui n'existe pas encore.
"""
import ast
import hashlib
import importlib.util
import inspect
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from core.product_analytics import campaigns as pc
from core.product_analytics import cohort as ch
from core.product_analytics import common
from core.product_analytics import participation as pp
from core.product_analytics.common import (
    AdmissionRefused,
    CampaignTransitionRefused,
    CohortConflict,
    CohortNotFound,
    CohortRefused,
    InvalidProductAnalyticsInput,
    ParticipationConflict,
    ParticipationRefused,
)
from tests.test_cognitive_capture import _NoDB, _run_blocked, _Statements, _writes
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, _reset_schema, _run_alembic
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture
from tests.test_migration_0016_portfolio_idempotency import _rejects
from tests.test_product_analytics_audit_r1 import _run_while_blocked
from tests.test_product_analytics_services import POLICY, T0, clock  # noqa: F401 — fixture clock

PACKAGE = REPO_ROOT / "core" / "product_analytics"
MIGRATION = REPO_ROOT / "alembic" / "versions" / "0023_analytics_cohort_sessions.py"
TWELVE_DAYS = timedelta(days=12)
THIRTY_MINUTES = timedelta(minutes=30)
NOTICE = "v0-notice-1"
ADMISSION_CLOSES = T0 + timedelta(days=10)
CLOSE_BY = ADMISSION_CLOSES + TWELVE_DAYS + THIRTY_MINUTES  # échéance minimale exacte
ANALYTICS_TABLES = ("product_activity_session_interactions", "product_activity_sessions", "product_activity_events",
                    "product_cohort_seats", "product_campaign_participants", "product_campaign_cohorts",
                    "product_campaigns")
F2_FIXTURE_TABLES = ("auth_operations", "invitations")
BASE_USER = "v0-cohort-outsider"


def _m0023():
    spec = importlib.util.spec_from_file_location("m0023_cohort", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_cohort_registry_matches_migration_0023():
    m = _m0023()
    assert common.COHORT_POLICIES == {"v0-cohort-v1": common.CohortPolicy(seat_count=5)}
    assert common.COHORT_POLICIES[common.WAVE_0_COHORT_POLICY_VERSION].seat_count == m.WAVE_0_SEAT_COUNT == 5
    assert ("ck_product_campaign_cohorts_seat_count", "seat_count = 5") in m.COHORT_CHECKS
    assert ("ck_product_cohort_seats_seat_number", "seat_number BETWEEN 1 AND 5") in m.SEAT_CHECKS
    assert ch.INVITATION_ACTIVATION == m.BINDING_OPERATION_KIND and ch.COMMITTED == m.BINDING_OPERATION_STATUS
    assert (ch.SEAT_NUMBER_UNIQUE, ch.SEAT_INVITATION_UNIQUE, ch.SEAT_USER_UNIQUE) == (
        m.SEAT_NUMBER_UNIQUE, m.SEAT_INVITATION_UNIQUE, m.SEAT_USER_UNIQUE)


def test_campaign_end_covers_last_j0_twelve_days_and_thirty_minutes():
    assert common.SESSION_FINALIZATION_GRACE == common.ACTIVITY_SESSION_INACTIVITY == THIRTY_MINUTES
    assert common.required_close_by_at(ADMISSION_CLOSES, "v0-measurement-v1") == CLOSE_BY
    assert common.required_close_by_at(ADMISSION_CLOSES, "unknown-policy") is None


def test_signatures_are_keyword_only_without_defaults_and_never_take_a_browser_identity():
    for function in (ch.define_cohort, ch.add_cohort_seat, ch.activate_cohort_campaign,
                     ch.bind_seat_to_activated_invitation, ch.record_seat_authorization,
                     ch.admit_at_first_eligible_login, ch.attempt_first_login_admission):
        for name, parameter in inspect.signature(function).parameters.items():
            if name != "db":
                assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (function.__name__, name)
                assert parameter.default is inspect.Parameter.empty, (function.__name__, name)
    # Le rattachement ne reçoit AUCUN user_id : l'utilisateur vient de la preuve F2.
    assert list(inspect.signature(ch.bind_seat_to_activated_invitation).parameters) == ["db", "campaign_id",
                                                                                       "seat_id"]
    # Le J0 ne reçoit aucune date : horloge serveur seulement.
    for function in (ch.admit_at_first_eligible_login, ch.attempt_first_login_admission):
        assert list(inspect.signature(function).parameters) == ["db", "campaign_id", "user_id"]


@pytest.mark.parametrize("call", [
    lambda: ch.define_cohort(_NoDB(), campaign_id="x", cohort_policy_version="v0-cohort-v1",
                             admission_closes_at=ADMISSION_CLOSES),
    lambda: ch.define_cohort(_NoDB(), campaign_id=uuid.uuid4(), cohort_policy_version="v0-cohort-v2",
                             admission_closes_at=ADMISSION_CLOSES),
    lambda: ch.define_cohort(_NoDB(), campaign_id=uuid.uuid4(), cohort_policy_version=["v0-cohort-v1"],
                             admission_closes_at=ADMISSION_CLOSES),
    lambda: ch.define_cohort(_NoDB(), campaign_id=uuid.uuid4(), cohort_policy_version="v0-cohort-v1",
                             admission_closes_at=ADMISSION_CLOSES.replace(tzinfo=None)),
    lambda: ch.add_cohort_seat(_NoDB(), campaign_id=uuid.uuid4(), invitation_id="token"),
    lambda: ch.activate_cohort_campaign(_NoDB(), campaign_id=str(uuid.uuid4())),
    lambda: ch.bind_seat_to_activated_invitation(_NoDB(), campaign_id=uuid.uuid4(), seat_id=1),
    lambda: ch.record_seat_authorization(_NoDB(), campaign_id=uuid.uuid4(), user_id="", notice_version=NOTICE),
    lambda: ch.record_seat_authorization(_NoDB(), campaign_id=uuid.uuid4(), user_id="u", notice_version="Notice V0"),
    lambda: ch.admit_at_first_eligible_login(_NoDB(), campaign_id=uuid.uuid4(), user_id=None),
])
def test_services_validate_before_any_database_access(call):
    with pytest.raises(InvalidProductAnalyticsInput):
        call()


def test_non_blocking_attempt_never_raises_even_with_an_unusable_database():
    """Base inutilisable (le moindre accès lève) : issue 'failed', aucune
    exception propagée à la requête métier."""
    attempt = ch.attempt_first_login_admission(_NoDB(), campaign_id=uuid.uuid4(), user_id="u")
    assert (attempt.status, attempt.reason, attempt.j0) == ("failed", "AssertionError", None)
    invalid = ch.attempt_first_login_admission(_NoDB(), campaign_id="x", user_id="u")
    assert (invalid.status, invalid.reason) == ("conflict", "InvalidProductAnalyticsInput")
    assert set(ch.ATTEMPT_STATUSES) == {"admitted", "already_admitted", "not_eligible", "conflict", "failed"}


def _raised_reasons(path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {kw.value.value for node in ast.walk(tree) if isinstance(node, ast.Call)
            for kw in node.keywords if kw.arg == "reason" and isinstance(kw.value, ast.Constant)}


def test_refusal_reasons_are_closed_vocabularies():
    assert _raised_reasons(PACKAGE / "cohort.py") <= set(common.COHORT_REFUSAL_REASONS) | set(
        common.ADMISSION_REFUSAL_REASONS)
    assert set(common.ADMISSION_REFUSAL_REASONS) <= _raised_reasons(PACKAGE / "cohort.py")
    assert _raised_reasons(PACKAGE / "sessions.py") == set(common.SESSION_REFUSAL_REASONS)


def test_no_seat_removal_replacement_and_immutable_admission_deadline():
    """Cohorte fixe : aucune suppression (ni delete ORM, ni DELETE SQL),
    aucune réaffectation de invitation_id / seat_number / campaign_id, et
    aucune affectation de admission_closes_at hors de la création."""
    source = (PACKAGE / "cohort.py").read_text(encoding="utf-8")
    assert ".delete(" not in source and "DELETE" not in source.upper().replace("DELETED", "")
    tree = ast.parse(source)
    targets = [target for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
               for target in (node.targets if isinstance(node, ast.Assign) else [node.target])]
    assigned = {t.attr for t in targets if isinstance(t, ast.Attribute)}
    assert not assigned & {"admission_closes_at", "invitation_id", "seat_number", "campaign_id", "seat_count",
                           "cohort_policy_version", "close_by_at"}
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert not [f for f in functions if any(w in f for w in ("remove", "delete", "replace", "reassign", "unbind"))]


# --------------------------------------------------------------------------
# 2. PostgreSQL réel
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine(pg_url):  # noqa: F811
    eng = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    _reset_schema(eng)
    _run_alembic(pg_url, "upgrade", "head")
    with eng.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant')"), {"u": BASE_USER})
    yield eng
    eng.dispose()


@pytest.fixture
def Sessions(engine):
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)  # = core.db.SessionLocal
    yield factory
    with engine.begin() as conn:
        for table in ANALYTICS_TABLES + F2_FIXTURE_TABLES:
            conn.execute(sa.text(f"DELETE FROM {table}"))
        conn.execute(sa.text("DELETE FROM users WHERE id <> :u"), {"u": BASE_USER})


def _rows(engine, table, order="1"):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(f"SELECT * FROM {table} ORDER BY {order}")).mappings()]


def _count(engine, table, where="TRUE", **params):
    with engine.connect() as conn:
        return conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {where}"), params).scalar_one()


def _snapshot(engine):
    return {table: _rows(engine, table) for table in ANALYTICS_TABLES + F2_FIXTURE_TABLES + ("users",)}


def _committed(Sessions, action):
    with Sessions() as session:
        result = action(session)
        session.commit()
        return result


# --- Fixtures F2 (SQL) : forme exacte des lignes du service F2 --------------

def _invitation(engine, *, status="pending", expires_at=None) -> uuid.UUID:
    invitation_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO invitations (id, token_hash, hash_key_version, cohort, status, created_at, expires_at, "
            "revoked_at) VALUES (:id, :h, 'v1', 'wave-0', :s, :c, :e, :r)"),
            {"id": invitation_id, "h": hashlib.sha256(invitation_id.bytes).hexdigest(), "s": status,
             "c": T0 - timedelta(days=1), "e": expires_at or T0 + timedelta(days=30),
             "r": T0 - timedelta(hours=1) if status == "revoked" else None})
    return invitation_id


def _activate(engine, invitation_id, *, at=None, user_id=None, status="committed") -> str:
    """Activation F2 d'une invitation : profil créé, invitation acceptée,
    opération 'invitation_activation' committed (comme
    F2AuthService.complete_invitation_activation)."""
    at = at or T0 - timedelta(minutes=5)
    user_id = user_id or f"v0-tester-{uuid.uuid4().hex[:12]}"
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant') ON CONFLICT DO NOTHING"),
                     {"u": user_id})
        committed = status == "committed"
        conn.execute(sa.text(
            "INSERT INTO auth_operations (id, kind, status, invitation_id, user_id, operation_schema_version, "
            "created_at, updated_at, expires_at, committed_at) VALUES (:id, 'invitation_activation', :s, :i, :u, "
            "'v1', :c, :at, :x, :ca)"),
            {"id": uuid.uuid4(), "s": status, "i": invitation_id, "u": user_id if committed else None,
             "c": at - timedelta(minutes=1), "at": at, "x": at + timedelta(minutes=10),
             "ca": at if committed else None})
        if committed:
            conn.execute(sa.text("UPDATE invitations SET status = 'accepted', accepted_at = :at WHERE id = :i"),
                         {"at": at, "i": invitation_id})
    return user_id


# --- Campagnes à cohorte -----------------------------------------------------

def _planned(Sessions, *, close_by_at=CLOSE_BY, admission_closes_at=ADMISSION_CLOSES, define=True):
    def action(session):
        campaign = pc.create_campaign(session, code=f"wave-0-{uuid.uuid4().hex[:8]}", name="Vague 0",
                                      purpose="Mesurer l'usage des surfaces Oryx pendant la Vague 0.",
                                      close_by_at=close_by_at, **POLICY)
        if define:
            ch.define_cohort(session, campaign_id=campaign.id, cohort_policy_version="v0-cohort-v1",
                             admission_closes_at=admission_closes_at)
        return campaign.id
    return _committed(Sessions, action)


def _seat(Sessions, campaign_id, invitation_id):
    return _committed(Sessions, lambda s: ch.add_cohort_seat(s, campaign_id=campaign_id,
                                                             invitation_id=invitation_id)[0].id)


def _full_cohort(engine, Sessions, clock, *, seats=5, **kwargs):
    campaign_id = _planned(Sessions, **kwargs)
    invitations = [_invitation(engine) for _ in range(seats)]
    seat_ids = [_seat(Sessions, campaign_id, invitation) for invitation in invitations]
    return campaign_id, invitations, seat_ids


def _activate_campaign(Sessions, campaign_id):
    return _committed(Sessions, lambda s: ch.activate_cohort_campaign(s, campaign_id=campaign_id).status)


def _bind(Sessions, campaign_id, seat_id):
    return _committed(Sessions, lambda s: ch.bind_seat_to_activated_invitation(s, campaign_id=campaign_id,
                                                                               seat_id=seat_id))[1]


def _authorize(Sessions, campaign_id, user_id, notice=NOTICE):
    return _committed(Sessions, lambda s: ch.record_seat_authorization(s, campaign_id=campaign_id, user_id=user_id,
                                                                       notice_version=notice))[1]


def _tester(engine, Sessions, clock, campaign_id, invitation_id, seat_id, *, authorize=True):
    """Invitation activée (F2), place rattachée, autorisation enregistrée."""
    user_id = _activate(engine, invitation_id, at=clock.now - timedelta(minutes=5))
    _bind(Sessions, campaign_id, seat_id)
    if authorize:
        _authorize(Sessions, campaign_id, user_id)
    return user_id


def _ready(engine, Sessions, clock, *, testers=1, **kwargs):
    """Campagne active à cinq places ; les `testers` premiers sont autorisés."""
    campaign_id, invitations, seats = _full_cohort(engine, Sessions, clock, **kwargs)
    _activate_campaign(Sessions, campaign_id)
    clock.advance(minutes=10)
    users = [_tester(engine, Sessions, clock, campaign_id, invitations[i], seats[i]) for i in range(testers)]
    return campaign_id, users, invitations, seats


def _admit(session, campaign_id, user_id):
    return ch.admit_at_first_eligible_login(session, campaign_id=campaign_id, user_id=user_id)


def _committed_admit(Sessions, campaign_id, user_id):
    def action(session):
        admission = _admit(session, campaign_id, user_id)
        return (admission.participation.window_start_at, admission.participation.window_end_at, admission.created,
                admission.j0_uncertain)
    return _committed(Sessions, action)


# --- A. Cohorte pré-J0 --------------------------------------------------------

def test_pg_exactly_five_seats_activate_without_touching_f2(engine, Sessions, clock):
    campaign_id, invitations, seat_ids = _full_cohort(engine, Sessions, clock)
    f2_before = {t: _rows(engine, t) for t in F2_FIXTURE_TABLES}
    assert _activate_campaign(Sessions, campaign_id) == "active"
    seats = _rows(engine, "product_cohort_seats", "seat_number")
    assert [s["seat_number"] for s in seats] == [1, 2, 3, 4, 5]
    assert [s["invitation_id"] for s in seats] == invitations and [s["id"] for s in seats] == seat_ids
    assert len({s["id"] for s in seats}) == 5 and all(isinstance(s["id"], uuid.UUID) for s in seats)
    assert all(s["campaign_id"] == campaign_id and s["user_id"] is None for s in seats)
    [cohort] = _rows(engine, "product_campaign_cohorts")
    assert (cohort["campaign_id"], cohort["seat_count"], cohort["admission_closes_at"]) == (campaign_id, 5,
                                                                                            ADMISSION_CLOSES)
    assert _count(engine, "product_campaign_participants") == 0  # activation ≠ J0
    assert {t: _rows(engine, t) for t in F2_FIXTURE_TABLES} == f2_before  # F2 jamais écrit


@pytest.mark.parametrize("seats", [0, 4])
def test_pg_incomplete_cohort_refuses_activation_without_write(engine, Sessions, clock, seats):
    campaign_id, _, _ = _full_cohort(engine, Sessions, clock, seats=seats)
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(CohortRefused) as refused:
            ch.activate_cohort_campaign(session, campaign_id=campaign_id)
    assert refused.value.reason == "cohort_incomplete"
    assert _snapshot(engine) == before and _rows(engine, "product_campaigns")[0]["status"] == "planned"


def test_pg_a_sixth_seat_is_impossible_in_service_and_in_sql(engine, Sessions, clock):
    campaign_id, _, _ = _full_cohort(engine, Sessions, clock)
    extra = _invitation(engine)
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(CohortRefused) as refused:
            ch.add_cohort_seat(session, campaign_id=campaign_id, invitation_id=extra)
    assert refused.value.reason == "cohort_full" and _snapshot(engine) == before
    with engine.connect() as conn:
        insert = ("INSERT INTO product_cohort_seats (id, campaign_id, seat_number, invitation_id, created_at) "
                  "VALUES (gen_random_uuid(), :c, :n, :i, :t)")
        _rejects(conn, "ck_product_cohort_seats_seat_number", insert, c=campaign_id, n=6, i=extra, t=T0)
        _rejects(conn, "uq_product_cohort_seats_campaign_seat_number", insert, c=campaign_id, n=5, i=extra, t=T0)
        conn.rollback()
    assert _activate_campaign(Sessions, campaign_id) == "active"  # toujours exactement cinq


def test_pg_cohort_campaign_cannot_be_activated_by_the_generic_path(engine, Sessions, clock):
    campaign_id, _, _ = _full_cohort(engine, Sessions, clock)
    with Sessions() as session:
        with pytest.raises(CampaignTransitionRefused, match="activate_cohort_campaign"):
            pc.activate_campaign(session, campaign_id=campaign_id)
    plain = _planned(Sessions, define=False)
    with Sessions() as session:
        with pytest.raises(CohortRefused) as refused:
            ch.activate_cohort_campaign(session, campaign_id=plain)
    assert refused.value.reason == "cohort_missing"


def test_pg_seats_are_frozen_once_the_campaign_is_active(engine, Sessions, clock):
    campaign_id, invitations, _ = _full_cohort(engine, Sessions, clock, seats=5)
    _activate_campaign(Sessions, campaign_id)
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(CohortRefused) as refused:
            ch.add_cohort_seat(session, campaign_id=campaign_id, invitation_id=_invitation(engine))
        assert refused.value.reason == "campaign_not_planned"
    with Sessions() as session:  # date limite immuable : autre valeur refusée, même valeur idempotente
        with pytest.raises(CohortConflict):
            ch.define_cohort(session, campaign_id=campaign_id, cohort_policy_version="v0-cohort-v1",
                             admission_closes_at=ADMISSION_CLOSES + timedelta(days=1))
        assert ch.define_cohort(session, campaign_id=campaign_id, cohort_policy_version="v0-cohort-v1",
                                admission_closes_at=ADMISSION_CLOSES)[1] is False
        assert ch.add_cohort_seat(session, campaign_id=campaign_id, invitation_id=invitations[0])[1] is False
    assert {k: v for k, v in _snapshot(engine).items() if k != "invitations"} == {
        k: v for k, v in before.items() if k != "invitations"}


def test_pg_an_invitation_occupies_one_seat_and_a_bound_seat_is_never_reassigned(engine, Sessions, clock):
    campaign_id, invitations, seats = _full_cohort(engine, Sessions, clock)
    # Même invitation : la place existante est renvoyée, aucune nouvelle place.
    with Sessions() as session:
        again, created = ch.add_cohort_seat(session, campaign_id=campaign_id, invitation_id=invitations[0])
        assert (again.id, created) == (seats[0], False)
    _activate_campaign(Sessions, campaign_id)
    user = _activate(engine, invitations[0])
    assert _bind(Sessions, campaign_id, seats[0]) is True
    assert _bind(Sessions, campaign_id, seats[0]) is False  # répétition : idempotente, aucune réattribution
    with engine.connect() as conn:
        other = _activate(engine, invitations[1])
        _rejects(conn, "uq_product_cohort_seats_campaign_invitation",
                 "UPDATE product_cohort_seats SET invitation_id = :i WHERE id = :s", i=invitations[0], s=seats[1])
        # Réattribution forcée à un autre utilisateur : la preuve F2 composite l'interdit.
        _rejects(conn, "product_cohort_seats_binding_user_fkey",
                 "UPDATE product_cohort_seats SET user_id = :u WHERE id = :s", u=other, s=seats[0])
        conn.rollback()
    assert _rows(engine, "product_cohort_seats", "seat_number")[0]["user_id"] == user


def test_pg_the_same_identity_cannot_occupy_two_seats(engine, Sessions, clock):
    campaign_id, invitations, seats = _full_cohort(engine, Sessions, clock)
    _activate_campaign(Sessions, campaign_id)
    user = _activate(engine, invitations[0])
    _activate(engine, invitations[1], user_id=user)  # deux invitations activées par le même profil
    _bind(Sessions, campaign_id, seats[0])
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(CohortConflict, match="identité déjà rattachée"):
            ch.bind_seat_to_activated_invitation(session, campaign_id=campaign_id, seat_id=seats[1])
    assert _snapshot(engine) == before
    proof = _rows(engine, "auth_operations")[1]
    with engine.connect() as conn:
        _rejects(conn, "uq_product_cohort_seats_campaign_user",
                 "UPDATE product_cohort_seats SET user_id = :u, binding_operation_id = :o, "
                 "binding_operation_kind = 'invitation_activation', binding_operation_status = 'committed', "
                 "binding_committed_at = :c, bound_at = :b WHERE id = :s",
                 u=user, o=proof["id"], c=proof["committed_at"], b=clock.now, s=seats[1])
        conn.rollback()


def test_pg_binding_rests_on_a_committed_f2_activation_only(engine, Sessions, clock):
    campaign_id, invitations, seats = _full_cohort(engine, Sessions, clock)
    _activate_campaign(Sessions, campaign_id)
    _activate(engine, invitations[1], status="pending")  # opération F2 non committée
    for seat in seats[:2]:
        with Sessions() as session:
            with pytest.raises(CohortRefused) as refused:
                ch.bind_seat_to_activated_invitation(session, campaign_id=campaign_id, seat_id=seat)
        assert refused.value.reason == "invitation_not_activated"
    assert _count(engine, "product_cohort_seats", "user_id IS NOT NULL") == 0
    # En SQL : un user_id sans opération F2 committed de CETTE invitation est refusé.
    other_invitation = _invitation(engine)
    foreign_user = _activate(engine, other_invitation)
    foreign_proof = [r for r in _rows(engine, "auth_operations") if r["invitation_id"] == other_invitation][0]
    with engine.connect() as conn:
        bind = ("UPDATE product_cohort_seats SET user_id = :u, binding_operation_id = :o, "
                "binding_operation_kind = 'invitation_activation', binding_operation_status = 'committed', "
                "binding_committed_at = :c, bound_at = :b WHERE id = :s")
        _rejects(conn, "product_cohort_seats_binding_invitation_fkey", bind, u=foreign_user,
                 o=foreign_proof["id"], c=foreign_proof["committed_at"], b=clock.now, s=seats[2])
        _rejects(conn, "ck_product_cohort_seats_binding",
                 "UPDATE product_cohort_seats SET user_id = :u WHERE id = :s", u=foreign_user, s=seats[2])
        conn.rollback()


def test_pg_authorization_is_a_distinct_explicit_trace(engine, Sessions, clock):
    campaign_id, invitations, seats = _full_cohort(engine, Sessions, clock)
    _activate_campaign(Sessions, campaign_id)
    clock.advance(minutes=10)
    user = _tester(engine, Sessions, clock, campaign_id, invitations[0], seats[0], authorize=False)
    seat = _rows(engine, "product_cohort_seats", "seat_number")[0]
    assert (seat["authorization_recorded_at"], seat["notice_version"]) == (None, None)  # rattachement ≠ autorisation
    with Sessions() as session:  # connexion d'un titulaire non autorisé : aucun J0
        with pytest.raises(AdmissionRefused) as refused:
            _admit(session, campaign_id, user)
    assert refused.value.reason == "authorization_missing"
    for intruder in (BASE_USER, "v0-unknown-user"):
        with Sessions() as session:
            with pytest.raises(CohortRefused) as refused:
                ch.record_seat_authorization(session, campaign_id=campaign_id, user_id=intruder,
                                             notice_version=NOTICE)
        assert refused.value.reason == "seat_not_bound"
    clock.advance(minutes=1)
    assert _authorize(Sessions, campaign_id, user) is True
    assert _authorize(Sessions, campaign_id, user) is False  # répétition identique
    with Sessions() as session:
        with pytest.raises(CohortConflict):
            ch.record_seat_authorization(session, campaign_id=campaign_id, user_id=user, notice_version="v0-notice-2")
    seat = _rows(engine, "product_cohort_seats", "seat_number")[0]
    assert seat["authorization_recorded_at"] == clock.now > seat["bound_at"] and seat["notice_version"] == NOTICE
    with engine.connect() as conn:  # en SQL : aucune autorisation sans rattachement
        _rejects(conn, "ck_product_cohort_seats_authorization",
                 "UPDATE product_cohort_seats SET authorization_recorded_at = :t, notice_version = 'n' "
                 "WHERE id = :s", t=clock.now, s=seats[1])
        conn.rollback()


def test_pg_revoked_or_expired_invitations_make_the_cohort_incoherent(engine, Sessions, clock):
    revoked = _invitation(engine, status="revoked")
    campaign_id = _planned(Sessions)
    with Sessions() as session:
        with pytest.raises(CohortRefused) as refused:
            ch.add_cohort_seat(session, campaign_id=campaign_id, invitation_id=revoked)
    assert refused.value.reason == "invitation_unavailable"
    short = _invitation(engine, expires_at=T0 + timedelta(days=1))
    for invitation in [short] + [_invitation(engine) for _ in range(4)]:
        _seat(Sessions, campaign_id, invitation)
    clock.advance(days=2)  # l'invitation de la place 1 expire sans activation
    with Sessions() as session:
        with pytest.raises(CohortRefused) as refused:
            ch.activate_cohort_campaign(session, campaign_id=campaign_id)
    assert refused.value.reason == "cohort_incoherent" and "invitation" in str(refused.value)


def test_pg_testers_who_never_log_in_keep_their_seats(engine, Sessions, clock):
    campaign_id, users, _, _ = _ready(engine, Sessions, clock, testers=3)
    _committed_admit(Sessions, campaign_id, users[0])
    _committed_admit(Sessions, campaign_id, users[1])
    clock.now = CLOSE_BY - timedelta(minutes=1)
    _committed(Sessions, lambda s: pc.close_campaign(s, campaign_id=campaign_id))
    seats = _rows(engine, "product_cohort_seats", "seat_number")
    assert len(seats) == 5 and [s["seat_number"] for s in seats] == [1, 2, 3, 4, 5]
    assert [s["user_id"] for s in seats][:3] == users and [s["user_id"] for s in seats][3:] == [None, None]
    assert {p["user_id"] for p in _rows(engine, "product_campaign_participants")} == set(users[:2])


# --- B. Calendrier d'admission ----------------------------------------------

def test_pg_cohort_definition_requires_a_compatible_calendar(engine, Sessions, clock):
    campaign_id = _planned(Sessions, define=False, close_by_at=CLOSE_BY)
    define = lambda s, at: ch.define_cohort(s, campaign_id=campaign_id, cohort_policy_version="v0-cohort-v1",  # noqa
                                            admission_closes_at=at)
    for at, reason in ((T0, "admission_closed"), (T0 - timedelta(days=1), "admission_closed"),
                       (ADMISSION_CLOSES + timedelta(microseconds=1), "calendar_incompatible")):
        with Sessions() as session:
            with pytest.raises(CohortRefused) as refused:
                define(session, at)
        assert refused.value.reason == reason
    assert _count(engine, "product_campaign_cohorts") == 0
    cohort, created = _committed(Sessions, lambda s: define(s, ADMISSION_CLOSES))  # égalité exacte admise
    assert created is True
    [row] = _rows(engine, "product_campaign_cohorts")
    assert row["admission_closes_at"] + TWELVE_DAYS + THIRTY_MINUTES == _rows(engine, "product_campaigns")[0][
        "close_by_at"]


def test_pg_cohort_cannot_be_defined_over_existing_participations_or_outside_planned(engine, Sessions, clock):
    campaign_id = _planned(Sessions, define=False)
    _committed(Sessions, lambda s: pp.enroll_participant(
        s, campaign_id=campaign_id, user_id=BASE_USER, window_start_at=T0, window_end_at=T0 + TWELVE_DAYS,
        authorization_recorded_at=T0, notice_version=NOTICE))
    with Sessions() as session:
        with pytest.raises(CohortRefused) as refused:
            ch.define_cohort(session, campaign_id=campaign_id, cohort_policy_version="v0-cohort-v1",
                             admission_closes_at=ADMISSION_CLOSES)
    assert refused.value.reason == "participation_exists"
    plain = _planned(Sessions, define=False)
    _committed(Sessions, lambda s: pc.activate_campaign(s, campaign_id=plain))
    with Sessions() as session:
        with pytest.raises(CohortRefused) as refused:
            ch.define_cohort(session, campaign_id=plain, cohort_policy_version="v0-cohort-v1",
                             admission_closes_at=ADMISSION_CLOSES)
    assert refused.value.reason == "campaign_not_planned"
    with Sessions() as session:
        with pytest.raises(CohortNotFound):
            ch.add_cohort_seat(session, campaign_id=plain, invitation_id=_invitation(engine))


def test_pg_activation_is_refused_once_the_admission_deadline_is_reached(engine, Sessions, clock):
    campaign_id, _, _ = _full_cohort(engine, Sessions, clock)
    clock.now = ADMISSION_CLOSES
    with Sessions() as session:
        with pytest.raises(CohortRefused) as refused:
            ch.activate_cohort_campaign(session, campaign_id=campaign_id)
    assert refused.value.reason == "admission_closed"
    assert _rows(engine, "product_campaigns")[0]["status"] == "planned"


# --- C. J0 individuel -------------------------------------------------------

def test_pg_first_admission_creates_an_exact_twelve_day_window_from_server_j0(engine, Sessions, clock):
    campaign_id, [user], _, _ = _ready(engine, Sessions, clock)
    seat = _rows(engine, "product_cohort_seats", "seat_number")[0]
    clock.advance(hours=3, microseconds=11)
    j0 = clock.now
    with Sessions() as session:
        admission = _admit(session, campaign_id, user)
        assert admission.created is True and admission.j0_uncertain is False
        assert _count(engine, "product_campaign_participants") == 0  # rien n'est commité par le service
        session.commit()
    [row] = _rows(engine, "product_campaign_participants")
    assert row == {"campaign_id": campaign_id, "user_id": user, "window_start_at": j0,
                   "window_end_at": j0 + TWELVE_DAYS, "participation_status": "enrolled", "enrolled_at": j0,
                   "authorization_recorded_at": seat["authorization_recorded_at"], "notice_version": NOTICE}
    assert row["window_end_at"] - row["window_start_at"] == timedelta(days=12)
    assert row["window_end_at"] + THIRTY_MINUTES <= _rows(engine, "product_campaigns")[0]["close_by_at"]
    assert row["authorization_recorded_at"] <= j0  # aucun J0 rétroactif


def test_pg_repeated_login_never_moves_j0_nor_recreates_a_window(engine, Sessions, clock):
    campaign_id, [user], _, _ = _ready(engine, Sessions, clock)
    j0 = clock.now
    first = _committed_admit(Sessions, campaign_id, user)
    before = _snapshot(engine)
    for later in (timedelta(hours=1), timedelta(days=11), timedelta(days=13)):  # y compris après la date limite
        clock.now = j0 + later
        with Sessions() as session, _Statements(engine) as statements:
            admission = _admit(session, campaign_id, user)
            assert (admission.participation.window_start_at, admission.created) == (j0, False)
            session.commit()
        assert not [w for w in _writes(statements) if w.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert _snapshot(engine) == before and first[:3] == (j0, j0 + TWELVE_DAYS, True)


@pytest.mark.parametrize("release", ["commit", "rollback"])
def test_pg_concurrent_first_logins_keep_a_single_j0(engine, Sessions, clock, release):
    campaign_id, [user], _, _ = _ready(engine, Sessions, clock)
    j0 = clock.now
    holder, waiter = Sessions(), Sessions()
    try:
        assert _admit(holder, campaign_id, user).created is True
        clock.advance(minutes=2)  # le perdant décide avec une horloge lue après l'attente
        result, error = _run_blocked(engine, holder, waiter, lambda s: _admit(s, campaign_id, user),
                                     release=release)
    finally:
        holder.close()
        waiter.close()
    assert error is None
    [row] = _rows(engine, "product_campaign_participants")
    if release == "commit":
        assert result.created is False and row["window_start_at"] == j0
    else:  # le gagnant est annulé : le perdant établit SON J0, jamais celui du gagnant (aucun antidatage)
        assert result.created is True and row["window_start_at"] == j0 + timedelta(minutes=2)
    assert row["window_end_at"] - row["window_start_at"] == TWELVE_DAYS


def test_pg_login_at_or_after_the_admission_deadline_creates_no_j0(engine, Sessions, clock):
    campaign_id, users, _, _ = _ready(engine, Sessions, clock, testers=2)
    clock.now = ADMISSION_CLOSES - timedelta(microseconds=1)
    assert _committed_admit(Sessions, campaign_id, users[0])[2] is True  # dernière µs : admis
    clock.now = ADMISSION_CLOSES
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(AdmissionRefused) as refused:
            _admit(session, campaign_id, users[1])
    assert refused.value.reason == "admission_closed" and _snapshot(engine) == before


def test_pg_deadline_crossed_while_waiting_for_the_identity_lock_is_refused(engine, Sessions, clock):
    campaign_id, [user], _, _ = _ready(engine, Sessions, clock)
    clock.now = ADMISSION_CLOSES - timedelta(seconds=1)
    holder, waiter = Sessions(), Sessions()
    try:
        _admit(holder, campaign_id, user)

        def cross_deadline():
            clock.now = ADMISSION_CLOSES

        _, error = _run_while_blocked(engine, holder, waiter, lambda s: _admit(s, campaign_id, user),
                                      while_blocked=cross_deadline, release="rollback")
    finally:
        holder.close()
        waiter.close()
    assert isinstance(error, AdmissionRefused) and error.reason == "admission_closed"
    assert _count(engine, "product_campaign_participants") == 0


def test_pg_a_j0_written_at_the_deadline_is_rolled_back(engine, Sessions, clock, monkeypatch):
    """Défense finale : si l'horloge lue par le corps réutilisé atteint la
    date limite (après le contrôle préalable), le SAVEPOINT est annulé."""
    campaign_id, [user], _, _ = _ready(engine, Sessions, clock)
    clock.now = ADMISSION_CLOSES - timedelta(microseconds=1)
    original = pp.first_eligible_login

    def late(db, **kwargs):
        clock.now = ADMISSION_CLOSES
        return original(db, **kwargs)

    monkeypatch.setattr(pp, "first_eligible_login", late)
    with Sessions() as session:
        with pytest.raises(AdmissionRefused) as refused:
            _admit(session, campaign_id, user)
        assert refused.value.reason == "admission_closed"
        session.commit()  # la transaction de l'appelant reste utilisable
    assert _count(engine, "product_campaign_participants") == 0


def test_pg_outsiders_missing_or_inconsistent_authorization_are_refused(engine, Sessions, clock):
    campaign_id, users, invitations, seats = _ready(engine, Sessions, clock, testers=1)
    bound_only = _tester(engine, Sessions, clock, campaign_id, invitations[1], seats[1], authorize=False)
    for user, reason in ((BASE_USER, "not_in_cohort"), (bound_only, "authorization_missing")):
        with Sessions() as session:
            with pytest.raises(AdmissionRefused) as refused:
                _admit(session, campaign_id, user)
        assert refused.value.reason == reason
    plain = _planned(Sessions, define=False)
    _committed(Sessions, lambda s: pc.activate_campaign(s, campaign_id=plain))
    with Sessions() as session:
        with pytest.raises(AdmissionRefused) as refused:
            _admit(session, plain, users[0])
    assert refused.value.reason == "not_a_cohort_campaign"
    # Points d'inscription bruts : refusés sur une campagne à cohorte (aucun contournement).
    with Sessions() as session:
        with pytest.raises(ParticipationRefused, match="cohorte"):
            pp.enroll_at_first_eligible_login(session, campaign_id=campaign_id, user_id=users[0],
                                              authorization_recorded_at=T0, notice_version=NOTICE)
    with Sessions() as session:
        with pytest.raises(ParticipationRefused, match="cohorte"):
            pp.enroll_participant(session, campaign_id=campaign_id, user_id=users[0], window_start_at=clock.now,
                                  window_end_at=clock.now + TWELVE_DAYS, authorization_recorded_at=T0,
                                  notice_version=NOTICE)
    assert _count(engine, "product_campaign_participants") == 0


@pytest.mark.parametrize("forgery", ["short_window", "before_authorization", "other_notice"])
def test_pg_a_contradictory_prior_participation_is_refused(engine, Sessions, clock, forgery):
    campaign_id, [user], _, _ = _ready(engine, Sessions, clock)
    seat = _rows(engine, "product_cohort_seats", "seat_number")[0]
    start = {"before_authorization": seat["authorization_recorded_at"] - timedelta(seconds=1)}.get(forgery,
                                                                                                   clock.now)
    end = start + (TWELVE_DAYS - timedelta(days=2) if forgery == "short_window" else TWELVE_DAYS)
    with engine.begin() as conn:  # écrivain hors service (forgé)
        conn.execute(sa.text(
            "INSERT INTO product_campaign_participants VALUES (:c, :u, :s, :e, 'enrolled', :s, :a, :n)"),
            {"c": campaign_id, "u": user, "s": start, "e": end, "a": seat["authorization_recorded_at"],
             "n": "v0-notice-x" if forgery == "other_notice" else NOTICE})
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(ParticipationConflict):
            _admit(session, campaign_id, user)
    assert _snapshot(engine) == before


@pytest.mark.parametrize("status", ["planned", "closed", "cancelled"])
def test_pg_inactive_campaign_creates_no_j0(engine, Sessions, clock, status):
    campaign_id, invitations, seats = _full_cohort(engine, Sessions, clock)
    if status != "planned":
        _activate_campaign(Sessions, campaign_id)
    clock.advance(minutes=10)
    user = _tester(engine, Sessions, clock, campaign_id, invitations[0], seats[0])
    ending = {"closed": pc.close_campaign, "cancelled": pc.cancel_campaign}.get(status)
    if ending is not None:
        _committed(Sessions, lambda s: ending(s, campaign_id=campaign_id))
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(AdmissionRefused) as refused:
            _admit(session, campaign_id, user)
    assert refused.value.reason == "campaign_not_active" and _snapshot(engine) == before


def test_pg_repeated_login_after_closure_returns_the_historical_j0(engine, Sessions, clock):
    campaign_id, [user], _, _ = _ready(engine, Sessions, clock)
    j0 = clock.now
    _committed_admit(Sessions, campaign_id, user)
    clock.now = j0 + timedelta(days=12, minutes=40)
    _committed(Sessions, lambda s: pc.close_campaign(s, campaign_id=campaign_id))
    assert _committed_admit(Sessions, campaign_id, user)[:3] == (j0, j0 + TWELVE_DAYS, False)


# --- Politique de panne -----------------------------------------------------

def test_pg_analytics_failure_never_creates_nor_backdates_j0(engine, Sessions, clock, monkeypatch):
    campaign_id, [user], _, _ = _ready(engine, Sessions, clock)
    failed_at = clock.now
    original = pp._insert

    def broken(db, participation, on_existing):
        db.execute(sa.text("SELECT 1 / 0"))  # panne SQL réelle dans le SAVEPOINT
        return original(db, participation, on_existing)

    monkeypatch.setattr(pp, "_insert", broken)
    with Sessions() as session:
        attempt = ch.attempt_first_login_admission(session, campaign_id=campaign_id, user_id=user)
        assert (attempt.status, attempt.reason, attempt.j0) == ("failed", "DataError", None)
        # La requête métier continue : sa transaction reste utilisable et commitable.
        session.execute(sa.text("INSERT INTO users (id, level) VALUES ('v0-business-write', 'debutant')"))
        session.commit()
    assert _count(engine, "users", "id = 'v0-business-write'") == 1
    assert _count(engine, "product_campaign_participants") == 0  # aucun J0 artificiel
    assert _rows(engine, "product_cohort_seats", "seat_number")[0]["admission_failure_first_at"] == failed_at

    monkeypatch.setattr(pp, "_insert", original)
    clock.advance(hours=2)
    recovered = clock.now
    with Sessions() as session:
        attempt = ch.attempt_first_login_admission(session, campaign_id=campaign_id, user_id=user)
        session.commit()
    assert (attempt.status, attempt.j0, attempt.j0_uncertain) == ("admitted", recovered, True)
    [row] = _rows(engine, "product_campaign_participants")
    assert row["window_start_at"] == recovered != failed_at  # jamais antidaté à la panne
    assert row["window_end_at"] == recovered + TWELVE_DAYS
    with Sessions() as session:  # la marque d'incertitude ne bouge plus
        again = ch.attempt_first_login_admission(session, campaign_id=campaign_id, user_id=user)
    assert (again.status, again.j0, again.j0_uncertain) == ("already_admitted", recovered, True)
    assert _rows(engine, "product_cohort_seats", "seat_number")[0]["admission_failure_first_at"] == failed_at


def test_pg_non_blocking_attempt_reports_refusals_without_raising(engine, Sessions, clock):
    campaign_id, [user], _, _ = _ready(engine, Sessions, clock)
    with Sessions() as session, _Statements(engine) as statements:
        outsider = ch.attempt_first_login_admission(session, campaign_id=campaign_id, user_id=BASE_USER)
    assert (outsider.status, outsider.reason) == ("not_eligible", "not_in_cohort")
    assert not [s for s in statements if "pg_advisory" in s or "FOR UPDATE" in s or "FOR SHARE" in s]
    clock.now = ADMISSION_CLOSES
    with Sessions() as session:
        late = ch.attempt_first_login_admission(session, campaign_id=campaign_id, user_id=user)
        session.commit()
    assert (late.status, late.reason) == ("not_eligible", "admission_closed")
    assert _count(engine, "product_campaign_participants") == 0
    assert _rows(engine, "product_cohort_seats", "seat_number")[0]["admission_failure_first_at"] is None


def test_pg_cohort_services_write_only_analytics_tables(engine, Sessions, clock):
    campaign_id, invitations, seats = _full_cohort(engine, Sessions, clock)
    _activate(engine, invitations[0], at=T0)
    with Sessions() as session, _Statements(engine) as statements:
        ch.activate_cohort_campaign(session, campaign_id=campaign_id)
        clock.advance(minutes=10)
        ch.bind_seat_to_activated_invitation(session, campaign_id=campaign_id, seat_id=seats[0])
        user = _rows(engine, "auth_operations")[0]["user_id"]
        ch.record_seat_authorization(session, campaign_id=campaign_id, user_id=user, notice_version=NOTICE)
        _admit(session, campaign_id, user)
        session.commit()
    writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert writes and all(s.split()[2 if s.startswith(("INSERT", "DELETE")) else 1].startswith("product_")
                          for s in writes), writes
    assert not [s for s in statements if "invitations" in s and "FOR " in s]  # F2 : lecture sans verrou


def test_pg_binding_follows_the_real_f2_invitation_activation(engine, Sessions, clock, pg_url):  # noqa: F811
    """Contrat de DONNÉES avec le vrai service F2 (core.auth_f2.service, sans
    route HTTP, sans activation de F2) : l'utilisateur rattaché est celui que
    F2 a créé à l'activation. Ne valide aucun raccordement de production."""
    from tests.f2_support import build_service, service_engine

    f2_engine = service_engine(pg_url)
    try:
        svc, _, _ = build_service(f2_engine, engine)
        created = svc.create_invitation(cohort="wave-0")
        begin = svc.begin_invitation_activation(token=created.token)
        activated = svc.complete_invitation_activation(operation_id=begin.operation_id, candidate=begin.candidate)
        assert activated.status == "invitation_activated"
    finally:
        f2_engine.dispose()
    real_now = datetime.now(timezone.utc)
    clock.now = max(T0, real_now + timedelta(minutes=1))
    admission_closes = clock.now + timedelta(days=10)
    campaign_id = _planned(Sessions, close_by_at=admission_closes + TWELVE_DAYS + THIRTY_MINUTES,
                           admission_closes_at=admission_closes)
    seat_ids = [_seat(Sessions, campaign_id, created.invitation_id)]
    seat_ids += [_seat(Sessions, campaign_id, _invitation(engine, expires_at=clock.now + timedelta(days=30)))
                 for _ in range(4)]
    _activate_campaign(Sessions, campaign_id)
    assert _bind(Sessions, campaign_id, seat_ids[0]) is True
    seat = _rows(engine, "product_cohort_seats", "seat_number")[0]
    assert seat["user_id"] == activated.session.user_id
    assert seat["binding_operation_id"] == begin.operation_id
    _authorize(Sessions, campaign_id, activated.session.user_id)
    assert _committed_admit(Sessions, campaign_id, activated.session.user_id)[2] is True
    with engine.begin() as conn:  # nettoyage des tables F2 réelles écrites par ce test
        conn.execute(sa.text("DELETE FROM product_campaign_participants"))
        conn.execute(sa.text("DELETE FROM product_cohort_seats"))
        conn.execute(sa.text("TRUNCATE auth_operation_results, oryx_session_generations, oryx_sessions, "
                             "auth_abuse_events, auth_abuse_subjects, auth_abuse_key_state, auth_preauth_candidates, "
                             "auth_operations, invitations CASCADE"))


# --- Contrôle transactionnel (audit R1) : SAVEPOINT et autoflush ------------

@pytest.mark.parametrize("autoflush", [False, True])  # False = core.db.SessionLocal
def test_pg_binding_collision_writes_nothing_and_keeps_the_caller_transaction(engine, Sessions, clock, monkeypatch,
                                                                             autoflush):
    """Collision réelle d'UNIQUE (campaign_id, user_id) pendant un
    rattachement : un écrivain hors service, non sérialisé, a occupé
    l'identité après le contrôle préalable (simulé en masquant ce contrôle).
    Les mutations ayant lieu DANS le SAVEPOINT, seule la place visée est
    restaurée : aucune écriture partielle, CohortConflict, et la transaction
    de l'appelant reste utilisable (écriture métier commitée ensuite)."""
    campaign_id, invitations, seats = _full_cohort(engine, Sessions, clock)
    _activate_campaign(Sessions, campaign_id)
    user = _activate(engine, invitations[0])
    _activate(engine, invitations[1], user_id=user)  # deux invitations activées par le même profil
    _bind(Sessions, campaign_id, seats[1])  # l'identité occupe déjà la place 2
    monkeypatch.setattr(ch, "_seat_of_user", lambda db, campaign, user_id: None)  # contrôle préalable contourné
    before = _snapshot(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=autoflush)
    with factory() as session, _Statements(engine) as statements:
        session.execute(sa.text("INSERT INTO users (id, level) VALUES ('v0-before-bind', 'debutant')"))
        with pytest.raises(CohortConflict, match="uq_product_cohort_seats_campaign_user"):
            ch.bind_seat_to_activated_invitation(session, campaign_id=campaign_id, seat_id=seats[0])
        seat = session.get(ch.ProductCohortSeat, seats[0])
        assert (seat.user_id, seat.bound_at, seat.binding_operation_id) == (None, None, None)  # ORM restauré
        session.execute(sa.text("INSERT INTO users (id, level) VALUES ('v0-after-bind', 'debutant')"))
        session.commit()
    order = [s.split()[0] for s in statements if s.split()[0] in ("SAVEPOINT", "UPDATE", "ROLLBACK", "RELEASE")]
    assert order[order.index("UPDATE") - 1] == "SAVEPOINT"  # l'UPDATE a lieu DANS le SAVEPOINT
    assert "ROLLBACK" in order[order.index("UPDATE"):]
    after = _snapshot(engine)
    assert {r["id"] for r in after["users"]} - {r["id"] for r in before["users"]} == {"v0-before-bind",
                                                                                     "v0-after-bind"}
    assert {k: v for k, v in after.items() if k != "users"} == {k: v for k, v in before.items() if k != "users"}


def test_pg_a_refused_binding_or_authorization_leaves_the_caller_transaction_usable(engine, Sessions, clock):
    campaign_id, invitations, seats = _full_cohort(engine, Sessions, clock)
    _activate_campaign(Sessions, campaign_id)
    user = _activate(engine, invitations[0])
    _bind(Sessions, campaign_id, seats[0])
    with Sessions() as session:
        with pytest.raises(CohortRefused):  # invitation non activée
            ch.bind_seat_to_activated_invitation(session, campaign_id=campaign_id, seat_id=seats[1])
        with pytest.raises(CohortRefused):  # aucune place pour cet utilisateur
            ch.record_seat_authorization(session, campaign_id=campaign_id, user_id=BASE_USER, notice_version=NOTICE)
        assert ch.record_seat_authorization(session, campaign_id=campaign_id, user_id=user,
                                            notice_version=NOTICE)[1] is True
        session.commit()
    assert _rows(engine, "product_cohort_seats", "seat_number")[0]["notice_version"] == NOTICE
