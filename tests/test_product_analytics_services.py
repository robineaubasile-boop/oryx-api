"""Tests de V0-T08 Lot 2A : services métier DORMANTS de l'analytics produit
(core/product_analytics/).

1. Sans base (toujours exécutés) : vocabulaires identiques à la migration
   0022, registre des politiques (Vague 0 = 90 jours, aucune valeur par
   défaut), table des transitions, validation stricte AVANT tout accès à la
   base (session factice qui échoue au moindre accès), cohérence des
   événements, absence de champ libre, document retention-scopes-v1 et
   règle de révision (aucune prolongation, aucune finalité rétroactive),
   aucune transaction possédée par les services.
2. PostgreSQL réel (base jetable, ORYX_TEST_DATABASE_URL ; sinon SKIPPÉS,
   rien n'est simulé avec SQLite) : transitions, immutabilité, inscription,
   autorisation fermée des événements, idempotence et collisions, unicités
   métier, concurrence réelle (deux connexions, attente observée par
   pg_blocking_pids, aucun sleep arbitraire), rollback transactionnel,
   conservation des participants sans événement.

L'horloge des services (common.utcnow) est remplacée par une horloge
déterministe ; aucune campagne, participation ni événement n'est écrit
ailleurs que dans la base jetable du test.
"""
import ast
import importlib.util
import inspect
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from core.models import ProductActivityEvent
from core.product_analytics import campaigns as pc
from core.product_analytics import common
from core.product_analytics import events as pe
from core.product_analytics import participation as pp
from core.product_analytics import retention as pr
from core.product_analytics.common import (
    CampaignCodeConflict,
    CampaignNotFound,
    CampaignPolicyLocked,
    CampaignTransitionRefused,
    EventIdempotencyCollision,
    EventNotAuthorized,
    EventUniquenessConflict,
    InvalidProductAnalyticsInput,
    InvalidRetentionScopes,
    ParticipationConflict,
    ParticipationNotFound,
    ParticipationRefused,
    ProductAnalyticsError,
    ProductAnalyticsUserNotFound,
    RetentionRevisionRefused,
    UnsupportedProductEventSchema,
)
from tests.test_cognitive_capture import _NoDB, _run_blocked, _Statements, _writes
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, _reset_schema, _run_alembic
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

PACKAGE = REPO_ROOT / "core" / "product_analytics"
MIGRATION = REPO_ROOT / "alembic" / "versions" / "0022_product_analytics_v1.py"
USER, OTHER_USER = "pa-user-1", "pa-user-2"
T0 = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
POLICY = {"retention_days": 90, "retention_policy_version": "v0-retention-v1",
          "measurement_policy_version": "v0-measurement-v1"}
TABLES = ("product_activity_events", "product_campaign_participants", "product_campaigns")


def _m0022():
    spec = importlib.util.spec_from_file_location("m0022_lot2a", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    """Horloge déterministe partagée par les services pendant un test."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock(T0)
    monkeypatch.setattr(common, "utcnow", fake)
    return fake


def _campaign_kwargs(**overrides):
    kwargs = {"code": f"wave-0-{uuid.uuid4().hex[:8]}", "name": "Vague 0",
              "purpose": "Mesurer l'usage des surfaces Oryx pendant la Vague 0.",
              "close_by_at": T0 + timedelta(days=60), **POLICY}
    kwargs.update(overrides)
    return kwargs


def _event_kwargs(**overrides):
    occurred = overrides.get("occurred_at", T0 + timedelta(hours=1))
    kwargs = {"user_id": USER, "visit_id": uuid.uuid4(), "idempotency_key": uuid.uuid4(),
              "event_type": "education_used", "surface": "education", "executed_route": None,
              "source": "client_signal", "source_operation_kind": None, "source_operation_id": None,
              "outcome": "succeeded", "delivery_status": "not_applicable", "occurred_at": occurred,
              "resolved_at": occurred, "delivered_at": None}
    kwargs.update(overrides)
    return kwargs


def _business_kwargs(**overrides):
    occurred = overrides.get("occurred_at", T0 + timedelta(hours=1))
    kwargs = _event_kwargs(event_type="decrypter_used", surface="decrypter", source="server_business",
                           source_operation_kind="assistant_delivery", source_operation_id=uuid.uuid4(),
                           executed_route="legacy", outcome="succeeded", delivery_status="confirmed",
                           occurred_at=occurred, resolved_at=occurred, delivered_at=occurred)
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_vocabularies_are_exactly_those_of_migration_0022():
    m = _m0022()
    assert common.CAMPAIGN_STATUSES == m.CAMPAIGN_STATUSES
    assert common.PARTICIPATION_STATUSES == m.PARTICIPATION_STATUSES
    assert common.EVENT_TYPES == m.EVENT_TYPES
    assert common.SOURCES == m.SOURCES
    assert common.OUTCOMES == m.OUTCOMES
    assert common.DELIVERY_STATUSES == m.DELIVERY_STATUSES
    assert pr.RETENTION_SCOPES_VERSION == m.RETENTION_SCOPES_VERSION
    assert set(common.EVENT_TYPE_RULES) == set(m.EVENT_TYPES)
    assert pe.IDEMPOTENCY_UNIQUE == m.EVENT_IDEMPOTENCY_UNIQUE
    assert pe.SESSION_START_UNIQUE == m.EVENT_SESSION_START_UNIQUE
    assert pe.SOURCE_OPERATION_UNIQUE == m.EVENT_SOURCE_OPERATION_UNIQUE
    assert pc.CAMPAIGN_CODE_UNIQUE == m.CAMPAIGN_CODE_UNIQUE
    # Longueurs des vocabulaires du lot 2A compatibles avec les colonnes.
    assert max(map(len, common.SURFACES)) <= 32 and max(map(len, common.EXECUTED_ROUTES)) <= 32
    assert max(map(len, common.SOURCE_OPERATION_KINDS)) <= 64
    assert {r.operation_kind for r in common.EVENT_TYPE_RULES.values()} - {None} == set(
        common.SOURCE_OPERATION_KINDS)
    assert {r.surface for r in common.EVENT_TYPE_RULES.values()} - {None} <= set(common.SURFACES)


def test_wave_0_policy_is_90_days_and_never_implicit():
    assert common.WAVE_0_POLICY.retention_days == 90
    assert common.RETENTION_POLICIES == {common.WAVE_0_POLICY.retention_policy_version: 90}
    assert common.MEASUREMENT_POLICIES == (common.WAVE_0_POLICY.measurement_policy_version,)
    # Aucun paramètre par défaut : chaque politique est transmise explicitement.
    for function in (pc.create_campaign, pc.revise_campaign_policies, pp.enroll_participant, pe.record_event):
        for name, parameter in inspect.signature(function).parameters.items():
            if name != "db":
                assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (function.__name__, name)
                assert parameter.default is inspect.Parameter.empty, (function.__name__, name)
    for bad in ({"retention_days": 30}, {"retention_days": 91}, {"retention_days": True},
                {"retention_days": 90.0}, {"retention_policy_version": "v0-retention-v2"},
                {"measurement_policy_version": "other"}):
        with pytest.raises(InvalidProductAnalyticsInput):
            common.validate_policy(**{**POLICY, **bad})


def test_transition_table_is_closed_and_terminal_states_never_reopen():
    assert pc.TRANSITIONS == {("planned", "active"), ("active", "closed"), ("planned", "cancelled"),
                              ("active", "cancelled")}
    assert not [t for t in pc.TRANSITIONS if t[0] in common.TERMINAL_CAMPAIGN_STATUSES]


def test_exception_hierarchy():
    for name in ("InvalidProductAnalyticsInput", "ProductAnalyticsUserNotFound", "CampaignNotFound",
                 "CampaignCodeConflict", "CampaignTransitionRefused", "CampaignPolicyLocked",
                 "ParticipationNotFound", "ParticipationRefused", "ParticipationConflict", "EventNotAuthorized",
                 "EventIdempotencyCollision", "EventUniquenessConflict", "UnsupportedProductEventSchema",
                 "InvalidRetentionScopes", "RetentionRevisionRefused"):
        assert issubclass(getattr(common, name), ProductAnalyticsError)
    assert issubclass(InvalidProductAnalyticsInput, ValueError)


@pytest.mark.parametrize("overrides", [
    {"code": "Wave 0"}, {"code": "-wave"}, {"code": "a" * 65}, {"code": ""}, {"name": ""}, {"name": " x"},
    {"name": "x" * 161}, {"purpose": ""}, {"purpose": "x" * 2001}, {"close_by_at": datetime(2027, 1, 1)},
    {"close_by_at": "2027-01-01"}, {"retention_days": 30}, {"retention_policy_version": "unknown"},
])
def test_create_campaign_validates_before_any_database_access(overrides):
    with pytest.raises(InvalidProductAnalyticsInput):
        pc.create_campaign(_NoDB(), **_campaign_kwargs(**overrides))


def test_campaign_transitions_validate_identifiers_before_any_database_access():
    for function in (pc.activate_campaign, pc.close_campaign, pc.cancel_campaign):
        with pytest.raises(InvalidProductAnalyticsInput):
            function(_NoDB(), campaign_id=str(uuid.uuid4()))
    with pytest.raises(InvalidProductAnalyticsInput):
        pc.revise_campaign_policies(_NoDB(), campaign_id=uuid.uuid4(), **{**POLICY, "retention_days": 30})


def _enroll_kwargs(**overrides):
    kwargs = {"campaign_id": uuid.uuid4(), "user_id": USER, "window_start_at": T0,
              "window_end_at": T0 + timedelta(days=14), "authorization_recorded_at": T0,
              "notice_version": "v0-notice-1"}
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize("overrides", [
    {"campaign_id": "x"}, {"user_id": ""}, {"user_id": " u"}, {"user_id": None}, {"user_id": "u\n"},
    {"window_end_at": T0}, {"window_end_at": T0 - timedelta(seconds=1)},
    {"window_start_at": datetime(2026, 12, 1)},
    # Autorisation technique exigée, jamais déduite : absente ou postérieure
    # au début de la fenêtre.
    {"authorization_recorded_at": None}, {"notice_version": None}, {"notice_version": ""},
    {"notice_version": "Notice V0"}, {"authorization_recorded_at": T0 + timedelta(seconds=1)},
])
def test_enroll_validates_before_any_database_access(overrides):
    with pytest.raises(InvalidProductAnalyticsInput):
        pp.enroll_participant(_NoDB(), **_enroll_kwargs(**overrides))


@pytest.mark.parametrize("kwargs", [
    _event_kwargs(event_type="session_started", surface="app", source="server_visit"),
    _event_kwargs(event_type="view_opened", surface="portfolio"),
    _event_kwargs(event_type="view_opened", surface="app", source="server_visit"),
    _event_kwargs(event_type="profile_viewed", surface="profile"),
    _event_kwargs(event_type="education_used", surface="education", source="server_visit"),
    _event_kwargs(event_type="coach_used", surface="coach"),
    _event_kwargs(event_type="decrypter_used", surface="decrypter"),
    _event_kwargs(event_type="portfolio_used", surface="portfolio"),
    _business_kwargs(),
    _business_kwargs(outcome="in_progress", resolved_at=None, delivery_status="pending", delivered_at=None),
    _business_kwargs(outcome="failed", delivery_status="unconfirmed", delivered_at=None, executed_route="f2"),
    _business_kwargs(event_type="portfolio_used", surface="portfolio", source_operation_kind="portfolio_write",
                     delivery_status="not_applicable", delivered_at=None, executed_route=None),
], ids=lambda kwargs: f"{kwargs['event_type']}-{kwargs['source']}-{kwargs['outcome']}-{kwargs['delivery_status']}")
def test_every_legitimate_event_shape_passes_validation(kwargs):
    request = pe._validate(**kwargs)
    assert set(request) == set(pe.REQUEST_FIELDS)


REJECTED_EVENTS = {
    "unknown_event_type": _event_kwargs(event_type="page_viewed"),
    "unknown_source": _event_kwargs(source="browser"),
    "unknown_outcome": _event_kwargs(outcome="ok"),
    "unknown_delivery": _event_kwargs(delivery_status="unknown"),
    "free_text_surface": _event_kwargs(surface="Que vaut LVMH ?"),
    "ticker_as_surface": _event_kwargs(event_type="view_opened", surface="mc_pa"),
    "url_as_route": _business_kwargs(executed_route="/api/user/u/portfolio"),
    "surface_mismatch": _event_kwargs(event_type="coach_used", surface="education"),
    "session_from_client": _event_kwargs(event_type="session_started", surface="app"),
    "session_on_other_surface": _event_kwargs(event_type="session_started", surface="education", source="server_visit"),
    "session_failed": _event_kwargs(event_type="session_started", surface="app", source="server_visit",
                                    outcome="failed"),
    "business_for_education": _business_kwargs(event_type="education_used", surface="education"),
    "business_without_operation": _business_kwargs(source_operation_kind=None, source_operation_id=None),
    "business_wrong_operation_kind": _business_kwargs(source_operation_kind="portfolio_write"),
    "business_operation_id_not_uuid": _business_kwargs(source_operation_id="op-1"),
    "half_operation_pair": _event_kwargs(source_operation_kind="assistant_delivery"),
    "operation_on_client_signal": _event_kwargs(event_type="decrypter_used", surface="decrypter",
                                                source_operation_kind="assistant_delivery",
                                                source_operation_id=uuid.uuid4()),
    "route_on_client_signal": _event_kwargs(executed_route="legacy"),
    "in_progress_client": _event_kwargs(outcome="in_progress", resolved_at=None),
    "terminal_without_resolved_at": _event_kwargs(resolved_at=None),
    "in_progress_with_resolved_at": _business_kwargs(outcome="in_progress"),
    "delivery_on_client": _event_kwargs(delivery_status="pending"),
    "confirmed_without_delivered_at": _business_kwargs(delivered_at=None),
    "pending_with_delivered_at": _business_kwargs(delivery_status="pending"),
    "resolved_before_occurred": _event_kwargs(resolved_at=T0),
    "delivered_before_occurred": _business_kwargs(delivered_at=T0),
    "naive_occurred_at": _event_kwargs(occurred_at=datetime(2026, 12, 1, 10), resolved_at=datetime(2026, 12, 1, 10)),
    "string_occurred_at": _event_kwargs(occurred_at="2026-12-01T10:00:00Z"),
    "visit_id_string": _event_kwargs(visit_id="11111111-2222-4333-8444-555555555555"),
    "idempotency_key_string": _event_kwargs(idempotency_key="k-1"),
    "empty_user": _event_kwargs(user_id=""),
}


@pytest.mark.parametrize("case", sorted(REJECTED_EVENTS))
def test_invalid_events_are_refused_before_any_database_access(case):
    kwargs = REJECTED_EVENTS[case]
    with pytest.raises(InvalidProductAnalyticsInput) as failure:
        pe.record_event(_NoDB(), **kwargs)
    # Aucun écho de valeur libre dans le message.
    for field in ("surface", "executed_route", "user_id"):
        value = kwargs.get(field)
        if isinstance(value, str) and value and value not in common.SURFACES + common.EXECUTED_ROUTES:
            assert value not in str(failure.value)


def test_no_free_field_can_be_attached_to_an_event():
    """Paramètres nommés exacts : aucun texte, prompt, réponse, montant ni
    donnée pédagogique ne peut être transmis."""
    assert set(inspect.signature(pe.record_event).parameters) == {"db", "user_id", "idempotency_key",
                                                                  *pe.REQUEST_FIELDS}
    for extra in ({"question": "Que vaut LVMH ?"}, {"payload": {"ticker": "MC.PA"}}, {"competency_code": "c1"},
                  {"amount": 100.0}, {"retention_scopes": {}}, {"recorded_at": T0}, {"schema_version": "x"}):
        with pytest.raises(TypeError):
            pe.record_event(_NoDB(), **_event_kwargs(), **extra)
    columns = {c.name for c in ProductActivityEvent.__table__.columns}
    assert columns == {"id", "user_id", "idempotency_key", "recorded_at", "updated_at", "retention_scopes",
                       "next_scope_expires_at", "purge_after_at", "schema_version", *pe.REQUEST_FIELDS}


def _scope_item(campaign_id=None, expires_at="2027-03-01T10:00:00.000000Z", **overrides):
    item = {"kind": "campaign", "campaign_id": str(campaign_id or uuid.uuid4()),
            "policy_version": "v0-retention-v1", "expires_at": expires_at}
    item.update(overrides)
    return item


def test_retention_document_round_trip_and_bounds():
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    first = T0 + timedelta(days=90)
    scopes = [pr.RetentionScope("campaign", b, "v0-retention-v1", first + timedelta(days=1)),
              pr.RetentionScope("campaign", a, "v0-retention-v1", first)]
    document = pr.build_retention_document(scopes)
    assert document == {"version": "retention-scopes-v1", "scopes": [
        _scope_item(a, "2027-03-01T09:00:00.000000Z"), _scope_item(b, "2027-03-02T09:00:00.000000Z")]}
    parsed = pr.parse_retention_scopes(document)
    assert [s.campaign_id for s in parsed] == [a, b]
    assert pr.retention_bounds(parsed) == (first, first + timedelta(days=1))
    assert pr.validate_retention_columns(document, next_scope_expires_at=first,
                                         purge_after_at=first + timedelta(days=1)) == parsed
    for next_at, purge_at in ((first, first), (first + timedelta(days=1), first + timedelta(days=1)),
                              (first - timedelta(seconds=1), first + timedelta(days=1)),
                              (first, first + timedelta(days=2))):
        with pytest.raises(InvalidRetentionScopes):
            pr.validate_retention_columns(document, next_scope_expires_at=next_at, purge_after_at=purge_at)


INVALID_DOCUMENTS = {
    "not_object": [],
    "extra_key": {"version": "retention-scopes-v1", "scopes": [_scope_item()], "note": "x"},
    "missing_scopes": {"version": "retention-scopes-v1"},
    "other_version": {"version": "retention-scopes-v2", "scopes": [_scope_item()]},
    "version_in_list": {"version": ["retention-scopes-v1"], "scopes": [_scope_item()]},
    "empty_scopes": {"version": "retention-scopes-v1", "scopes": []},
    "scopes_object": {"version": "retention-scopes-v1", "scopes": {"a": _scope_item()}},
    "unknown_kind": {"version": "retention-scopes-v1", "scopes": [_scope_item(kind="analytics")]},
    "extra_scope_key": {"version": "retention-scopes-v1", "scopes": [{**_scope_item(), "ip": "1.2.3.4"}]},
    "missing_scope_key": {"version": "retention-scopes-v1", "scopes": [
        {k: v for k, v in _scope_item().items() if k != "policy_version"}]},
    "uppercase_uuid": {"version": "retention-scopes-v1", "scopes": [
        _scope_item(campaign_id="AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE")]},
    "not_uuid": {"version": "retention-scopes-v1", "scopes": [_scope_item(campaign_id="wave-0")]},
    "unknown_policy": {"version": "retention-scopes-v1", "scopes": [_scope_item(policy_version="forever")]},
    "naive_instant": {"version": "retention-scopes-v1", "scopes": [_scope_item(expires_at="2027-03-01T09:00:00")]},
    "offset_instant": {"version": "retention-scopes-v1", "scopes": [
        _scope_item(expires_at="2027-03-01T09:00:00.000000+00:00")]},
    "impossible_instant": {"version": "retention-scopes-v1", "scopes": [
        _scope_item(expires_at="2027-02-30T09:00:00.000000Z")]},
    "numeric_instant": {"version": "retention-scopes-v1", "scopes": [_scope_item(expires_at=1700000000)]},
    "duplicate_campaign": {"version": "retention-scopes-v1", "scopes": [
        _scope_item(uuid.UUID(int=3)), _scope_item(uuid.UUID(int=3), "2027-03-02T10:00:00.000000Z")]},
    "unsorted": {"version": "retention-scopes-v1", "scopes": [
        _scope_item(uuid.UUID(int=4), "2027-03-02T10:00:00.000000Z"), _scope_item(uuid.UUID(int=5))]},
}


@pytest.mark.parametrize("case", sorted(INVALID_DOCUMENTS))
def test_invalid_retention_documents_are_refused(case):
    with pytest.raises(InvalidRetentionScopes):
        pr.parse_retention_scopes(INVALID_DOCUMENTS[case])


def test_retention_revision_only_shortens_or_removes():
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    previous = {"version": "retention-scopes-v1", "scopes": [
        _scope_item(a, "2027-03-01T09:00:00.000000Z"), _scope_item(b, "2027-03-02T09:00:00.000000Z")]}

    def doc(*items):
        return {"version": "retention-scopes-v1", "scopes": list(items)}

    # Identique, raccourcie, ou finalité retirée : admis.
    assert len(pr.validate_scope_revision(previous, previous)) == 2
    pr.validate_scope_revision(previous, doc(_scope_item(a, "2027-02-01T09:00:00.000000Z"),
                                             _scope_item(b, "2027-03-02T09:00:00.000000Z")))
    assert [s.campaign_id for s in pr.validate_scope_revision(
        previous, doc(_scope_item(b, "2027-03-02T09:00:00.000000Z")))] == [b]
    # Prolongation rétroactive.
    with pytest.raises(RetentionRevisionRefused):
        pr.validate_scope_revision(previous, doc(_scope_item(a, "2027-03-01T09:00:00.000001Z"),
                                                 _scope_item(b, "2027-03-02T09:00:00.000000Z")))
    # Nouvelle finalité ajoutée rétroactivement (même échéance plus courte).
    with pytest.raises(RetentionRevisionRefused):
        pr.validate_scope_revision(previous, doc(_scope_item(uuid.UUID(int=9), "2027-01-01T09:00:00.000000Z"),
                                                 _scope_item(a, "2027-03-01T09:00:00.000000Z")))
    # Finalité dont la politique change.
    previous_one = doc(_scope_item(a, "2027-03-01T09:00:00.000000Z"))
    original = dict(common.RETENTION_POLICIES)
    common.RETENTION_POLICIES["v0-retention-v0"] = 90
    try:
        with pytest.raises(RetentionRevisionRefused):
            pr.validate_scope_revision(previous_one, doc(_scope_item(
                a, "2027-03-01T09:00:00.000000Z", policy_version="v0-retention-v0")))
    finally:
        common.RETENTION_POLICIES.clear()
        common.RETENTION_POLICIES.update(original)
    # Un document révisé vide n'est pas une révision (suppression : lot 2C).
    with pytest.raises(InvalidRetentionScopes):
        pr.validate_scope_revision(previous, doc())


def _calls(path, names) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in names]


def test_services_never_own_the_transaction_and_never_delete():
    for path in sorted(PACKAGE.glob("*.py")):
        assert _calls(path, {"commit", "rollback", "close", "delete", "begin", "execute_raw"}) == [], path.name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                    and node.module and node.module.startswith("sqlalchemy") for alias in node.names}
        assert imported <= {"select", "IntegrityError"}, path.name  # ni delete, ni update, ni SQL brut (text)
    # Un SAVEPOINT par INSERT, rien d'autre.
    for name, expected in (("campaigns.py", 1), ("participation.py", 1), ("events.py", 1)):
        assert _calls(PACKAGE / name, {"begin_nested"}) == ["begin_nested"] * expected


def test_no_service_infers_legal_consent_from_technical_fields():
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))} | {
            a.arg for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) for a in n.args.kwonlyargs}
        assert not [i for i in identifiers if "consent" in i.lower()], path.name


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
        for table in TABLES:
            conn.execute(sa.text(f"DELETE FROM {table}"))
        conn.execute(sa.text("DELETE FROM users WHERE id NOT IN (:a, :b)"), {"a": USER, "b": OTHER_USER})


def _rows(engine, table, order="1"):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.text(f"SELECT * FROM {table} ORDER BY {order}")).mappings()]


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def _snapshot(engine):
    return {table: _rows(engine, table) for table in (*TABLES, "users")}


def _committed(Sessions, action):
    with Sessions() as session:
        result = action(session)
        session.commit()
        return result


def _planned(Sessions, **overrides):
    return _committed(Sessions, lambda s: pc.create_campaign(s, **_campaign_kwargs(**overrides)).id)


def _active(Sessions, clock, **overrides):
    campaign_id = _planned(Sessions, **overrides)
    _committed(Sessions, lambda s: pc.activate_campaign(s, campaign_id=campaign_id))
    return campaign_id


def _enrolled(Sessions, clock, campaign_id, user=USER, days=14):
    return _committed(Sessions, lambda s: pp.enroll_participant(
        s, campaign_id=campaign_id, user_id=user, window_start_at=clock.now,
        window_end_at=clock.now + timedelta(days=days), authorization_recorded_at=clock.now,
        notice_version="v0-notice-1")[0].campaign_id)


def _ready(Sessions, clock, user=USER, **overrides):
    """Campagne active + participation de `user` sur [T0, T0 + 14 j)."""
    campaign_id = _active(Sessions, clock, **overrides)
    _enrolled(Sessions, clock, campaign_id, user)
    clock.advance(hours=2)
    return campaign_id


def _record(Sessions, **kwargs):
    with Sessions() as session:
        event, created = pe.record_event(session, **kwargs)
        session.commit()
        return event.id, created


# --- Campagnes : création ----------------------------------------------------

def test_pg_create_campaign_is_planned_explicit_and_creates_nothing_else(engine, Sessions, clock):
    with Sessions() as session:
        campaign_id = pc.create_campaign(session, **_campaign_kwargs(code="wave-0")).id
        assert _count(engine, "product_campaigns") == 0  # rien n'est commité par le service
        session.commit()
    [row] = _rows(engine, "product_campaigns")
    assert row == {"id": campaign_id, "code": "wave-0", "name": "Vague 0",
                   "purpose": "Mesurer l'usage des surfaces Oryx pendant la Vague 0.", "status": "planned",
                   "created_at": T0, "activated_at": None, "closed_at": None, "close_by_at": T0 + timedelta(days=60),
                   "retention_days": 90, "retention_policy_version": "v0-retention-v1",
                   "measurement_policy_version": "v0-measurement-v1"}
    assert _count(engine, "product_campaign_participants") == _count(engine, "product_activity_events") == 0


def test_pg_duplicate_code_is_refused_without_write(engine, Sessions, clock):
    _planned(Sessions, code="wave-0")
    before = _snapshot(engine)
    with Sessions() as session, _Statements(engine) as statements:
        with pytest.raises(CampaignCodeConflict):
            pc.create_campaign(session, **_campaign_kwargs(code="wave-0"))
        session.commit()
    assert _writes(statements) == [] and _snapshot(engine) == before


def test_pg_concurrent_duplicate_code_waits_then_conflicts(engine, Sessions, clock):
    with Sessions() as first, Sessions() as second:
        pc.create_campaign(first, **_campaign_kwargs(code="wave-0"))
        _, error = _run_blocked(engine, first, second,
                                lambda s: pc.create_campaign(s, **_campaign_kwargs(code="wave-0", name="Autre")))
    assert isinstance(error, CampaignCodeConflict)
    assert [r["name"] for r in _rows(engine, "product_campaigns")] == ["Vague 0"]


def test_pg_close_by_at_must_follow_creation(engine, Sessions, clock):
    for close_by in (T0, T0 - timedelta(days=1)):
        with Sessions() as session:
            with pytest.raises(InvalidProductAnalyticsInput):
                pc.create_campaign(session, **_campaign_kwargs(close_by_at=close_by))
    assert _count(engine, "product_campaigns") == 0


# --- Campagnes : transitions -------------------------------------------------

def test_pg_valid_transitions_planned_active_closed(engine, Sessions, clock):
    campaign_id = _planned(Sessions)
    clock.advance(days=1)
    _committed(Sessions, lambda s: pc.activate_campaign(s, campaign_id=campaign_id))
    clock.advance(days=10)
    _committed(Sessions, lambda s: pc.close_campaign(s, campaign_id=campaign_id))
    [row] = _rows(engine, "product_campaigns")
    assert (row["status"], row["activated_at"], row["closed_at"]) == (
        "closed", T0 + timedelta(days=1), T0 + timedelta(days=11))


@pytest.mark.parametrize("activate_first", [False, True])
def test_pg_cancellation_from_planned_or_active(engine, Sessions, clock, activate_first):
    campaign_id = _active(Sessions, clock) if activate_first else _planned(Sessions)
    clock.advance(days=3)
    _committed(Sessions, lambda s: pc.cancel_campaign(s, campaign_id=campaign_id))
    [row] = _rows(engine, "product_campaigns")
    assert (row["status"], row["activated_at"], row["closed_at"]) == (
        "cancelled", T0 if activate_first else None, T0 + timedelta(days=3))


INVALID_TRANSITIONS = [
    ("planned", "close"), ("active", "activate"), ("closed", "activate"), ("closed", "close"),
    ("closed", "cancel"), ("cancelled", "activate"), ("cancelled", "close"), ("cancelled", "cancel"),
]


def _bring_to(Sessions, clock, status):
    campaign_id = _planned(Sessions)
    steps = {"planned": [], "active": ["activate"], "closed": ["activate", "close"], "cancelled": ["cancel"]}[status]
    for step in steps:
        _committed(Sessions, lambda s: getattr(pc, f"{step}_campaign")(s, campaign_id=campaign_id))
    return campaign_id


@pytest.mark.parametrize("status,action", INVALID_TRANSITIONS)
def test_pg_invalid_transitions_and_reopening_are_refused_without_write(engine, Sessions, clock, status, action):
    campaign_id = _bring_to(Sessions, clock, status)
    clock.advance(days=1)
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(CampaignTransitionRefused):
            getattr(pc, f"{action}_campaign")(session, campaign_id=campaign_id)
        session.commit()
    assert _snapshot(engine) == before


def test_pg_unknown_campaign(engine, Sessions, clock):
    with Sessions() as session:
        for function in (pc.activate_campaign, pc.close_campaign, pc.cancel_campaign):
            with pytest.raises(CampaignNotFound):
                function(session, campaign_id=uuid.uuid4())
        with pytest.raises(CampaignNotFound):
            pc.revise_campaign_policies(session, campaign_id=uuid.uuid4(), **POLICY)


def test_pg_activation_after_deadline_is_refused_and_late_closing_stops_at_deadline(engine, Sessions, clock):
    late = _planned(Sessions, close_by_at=T0 + timedelta(days=5))
    running = _active(Sessions, clock, close_by_at=T0 + timedelta(days=5))
    clock.advance(days=5)
    with Sessions() as session:
        with pytest.raises(CampaignTransitionRefused):
            pc.activate_campaign(session, campaign_id=late)
    clock.advance(days=1)
    _committed(Sessions, lambda s: pc.close_campaign(s, campaign_id=running))
    rows = {r["id"]: r for r in _rows(engine, "product_campaigns")}
    assert rows[late]["status"] == "planned"
    assert (rows[running]["status"], rows[running]["closed_at"]) == ("closed", T0 + timedelta(days=5))


def test_pg_concurrent_activations_are_serialized(engine, Sessions, clock):
    campaign_id = _planned(Sessions)
    with Sessions() as first, Sessions() as second:
        pc.activate_campaign(first, campaign_id=campaign_id)
        _, error = _run_blocked(engine, first, second, lambda s: pc.activate_campaign(s, campaign_id=campaign_id))
    assert isinstance(error, CampaignTransitionRefused)
    [row] = _rows(engine, "product_campaigns")
    assert (row["status"], row["activated_at"]) == ("active", T0)


def test_pg_concurrent_close_and_cancel_have_a_single_winner(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    clock.advance(days=1)
    with Sessions() as first, Sessions() as second:
        pc.close_campaign(first, campaign_id=campaign_id)
        _, error = _run_blocked(engine, first, second, lambda s: pc.cancel_campaign(s, campaign_id=campaign_id))
    assert isinstance(error, CampaignTransitionRefused)
    assert _rows(engine, "product_campaigns")[0]["status"] == "closed"


# --- Campagnes : immutabilité des politiques ---------------------------------

def test_pg_policies_are_revisable_while_planned(engine, Sessions, clock, monkeypatch):
    campaign_id = _planned(Sessions)
    monkeypatch.setitem(common.RETENTION_POLICIES, "v0-retention-v0", 30)
    _committed(Sessions, lambda s: pc.revise_campaign_policies(
        s, campaign_id=campaign_id, retention_days=30, retention_policy_version="v0-retention-v0",
        measurement_policy_version="v0-measurement-v1"))
    [row] = _rows(engine, "product_campaigns")
    assert (row["retention_days"], row["retention_policy_version"]) == (30, "v0-retention-v0")


@pytest.mark.parametrize("status", ["active", "closed", "cancelled"])
def test_pg_policies_are_immutable_after_activation(engine, Sessions, clock, monkeypatch, status):
    campaign_id = _bring_to(Sessions, clock, status)
    monkeypatch.setitem(common.RETENTION_POLICIES, "v0-retention-v0", 30)
    before = _snapshot(engine)
    for policy in (POLICY, {**POLICY, "retention_days": 30, "retention_policy_version": "v0-retention-v0"}):
        with Sessions() as session:
            with pytest.raises(CampaignPolicyLocked):
                pc.revise_campaign_policies(session, campaign_id=campaign_id, **policy)
            session.commit()
    assert _snapshot(engine) == before


def test_pg_policy_revision_waits_for_a_concurrent_activation(engine, Sessions, clock):
    campaign_id = _planned(Sessions)
    with Sessions() as first, Sessions() as second:
        pc.activate_campaign(first, campaign_id=campaign_id)
        _, error = _run_blocked(engine, first, second,
                                lambda s: pc.revise_campaign_policies(s, campaign_id=campaign_id, **POLICY))
    assert isinstance(error, CampaignPolicyLocked)


# --- Participation -----------------------------------------------------------

@pytest.mark.parametrize("status", ["planned", "active"])
def test_pg_enrollment_in_planned_or_active_campaign(engine, Sessions, clock, status):
    campaign_id = _bring_to(Sessions, clock, status)
    clock.advance(hours=1)
    start = clock.now + timedelta(hours=1)
    kwargs = _enroll_kwargs(campaign_id=campaign_id, window_start_at=start, window_end_at=start + timedelta(days=7),
                            authorization_recorded_at=clock.now)
    participation, created = _committed(Sessions, lambda s: pp.enroll_participant(s, **kwargs))
    assert created is True
    [row] = _rows(engine, "product_campaign_participants")
    assert row == {"campaign_id": campaign_id, "user_id": USER, "window_start_at": start,
                   "window_end_at": start + timedelta(days=7), "participation_status": "enrolled",
                   "enrolled_at": clock.now, "authorization_recorded_at": clock.now, "notice_version": "v0-notice-1"}
    # Inscription : aucune activation implicite, aucun événement.
    assert _rows(engine, "product_campaigns")[0]["status"] == status
    assert _count(engine, "product_activity_events") == 0


def test_pg_enrollment_retry_is_idempotent_and_conflicting_retry_refused(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    kwargs = _enroll_kwargs(campaign_id=campaign_id)
    _committed(Sessions, lambda s: pp.enroll_participant(s, **kwargs))
    before = _snapshot(engine)
    clock.advance(minutes=5)
    with Sessions() as session, _Statements(engine) as statements:
        participation, created = pp.enroll_participant(session, **kwargs)
        assert created is False and participation.enrolled_at == T0
        session.commit()
    assert _writes(statements) == []
    for overrides in ({"window_end_at": T0 + timedelta(days=15)}, {"notice_version": "v0-notice-2"},
                      {"window_start_at": T0 + timedelta(minutes=5),
                       "authorization_recorded_at": T0 + timedelta(minutes=5)}):
        with Sessions() as session:
            with pytest.raises(ParticipationConflict):
                pp.enroll_participant(session, **{**kwargs, **overrides})
    assert _snapshot(engine) == before


def test_pg_concurrent_enrollment_inserts_once(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    kwargs = _enroll_kwargs(campaign_id=campaign_id)
    with Sessions() as first, Sessions() as second:
        pp.enroll_participant(first, **kwargs)
        result, error = _run_blocked(engine, first, second, lambda s: pp.enroll_participant(s, **kwargs)[1])
    assert (result, error) == (False, None)
    assert _count(engine, "product_campaign_participants") == 1


def test_pg_unknown_user_is_never_created(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock)
    with Sessions() as session, _Statements(engine) as statements:
        with pytest.raises(ProductAnalyticsUserNotFound):
            pp.enroll_participant(session, **_enroll_kwargs(campaign_id=campaign_id, user_id="ghost"))
        with pytest.raises(ProductAnalyticsUserNotFound):
            pe.record_event(session, **_event_kwargs(user_id="ghost"))
        session.commit()
    assert _writes(statements) == []
    assert _count(engine, "users") == 2 and _count(engine, "product_campaign_participants") == 0


@pytest.mark.parametrize("status", ["closed", "cancelled"])
def test_pg_enrollment_in_terminal_campaign_is_refused(engine, Sessions, clock, status):
    campaign_id = _bring_to(Sessions, clock, status)
    with Sessions() as session:
        with pytest.raises(ParticipationRefused):
            pp.enroll_participant(session, **_enroll_kwargs(campaign_id=campaign_id))
    with Sessions() as session:
        with pytest.raises(CampaignNotFound):
            pp.enroll_participant(session, **_enroll_kwargs(campaign_id=uuid.uuid4()))
    assert _count(engine, "product_campaign_participants") == 0


@pytest.mark.parametrize("case", ["retroactive", "beyond_deadline", "future_authorization", "after_deadline"])
def test_pg_unauthorized_enrollment_is_refused(engine, Sessions, clock, case):
    campaign_id = _active(Sessions, clock, close_by_at=T0 + timedelta(days=30))
    clock.advance(days=1)
    now = clock.now
    kwargs = {
        "retroactive": _enroll_kwargs(window_start_at=now - timedelta(seconds=1),
                                      authorization_recorded_at=now - timedelta(days=1)),
        "beyond_deadline": _enroll_kwargs(window_start_at=now, authorization_recorded_at=now,
                                          window_end_at=T0 + timedelta(days=30, seconds=1)),
        "future_authorization": _enroll_kwargs(window_start_at=now + timedelta(hours=2),
                                               authorization_recorded_at=now + timedelta(hours=1)),
        "after_deadline": _enroll_kwargs(window_start_at=T0 + timedelta(days=31),
                                         window_end_at=T0 + timedelta(days=32),
                                         authorization_recorded_at=T0 + timedelta(days=31)),
    }[case]
    if case == "after_deadline":
        clock.advance(days=31)
    with Sessions() as session:
        with pytest.raises(ParticipationRefused):
            pp.enroll_participant(session, **{**kwargs, "campaign_id": campaign_id})
    assert _count(engine, "product_campaign_participants") == 0


def test_pg_withdrawal_keeps_the_participant(engine, Sessions, clock):
    campaign_id = _ready(Sessions, clock)
    _committed(Sessions, lambda s: pp.withdraw_participant(s, campaign_id=campaign_id, user_id=USER))
    _committed(Sessions, lambda s: pp.withdraw_participant(s, campaign_id=campaign_id, user_id=USER))
    [row] = _rows(engine, "product_campaign_participants")
    assert row["participation_status"] == "withdrawn"
    with Sessions() as session:
        with pytest.raises(ParticipationNotFound):
            pp.withdraw_participant(session, campaign_id=campaign_id, user_id=OTHER_USER)
        # Aucune réinscription silencieuse après un retrait.
        with pytest.raises(ParticipationConflict):
            pp.enroll_participant(session, **_enroll_kwargs(campaign_id=campaign_id))
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE product_campaign_participants SET participation_status = 'completed'"))
    with Sessions() as session:
        with pytest.raises(ParticipationRefused):
            pp.withdraw_participant(session, campaign_id=campaign_id, user_id=USER)


@pytest.mark.parametrize("ending", ["close", "cancel"])
def test_pg_participants_without_events_are_kept_for_denominators(engine, Sessions, clock, ending):
    campaign_id = _ready(Sessions, clock)
    _enrolled(Sessions, clock, campaign_id, OTHER_USER)
    clock.advance(hours=1)
    _record(Sessions, **_event_kwargs(occurred_at=clock.now, resolved_at=clock.now))
    clock.advance(days=20)
    _committed(Sessions, lambda s: getattr(pc, f"{ending}_campaign")(s, campaign_id=campaign_id))
    rows = _rows(engine, "product_campaign_participants", "user_id")
    assert [(r["user_id"], r["participation_status"]) for r in rows] == [(USER, "enrolled"), (OTHER_USER, "enrolled")]
    assert _count(engine, "product_activity_events") == 1


# --- Événements : enregistrement et autorisation ----------------------------

def test_pg_record_event_persists_exact_fields_and_retention(engine, Sessions, clock):
    campaign_id = _ready(Sessions, clock)
    occurred = clock.now - timedelta(minutes=30)
    kwargs = _event_kwargs(occurred_at=occurred, resolved_at=occurred)
    with Sessions() as session:
        event, created = pe.record_event(session, **kwargs)
        event_id = event.id
        assert created is True and _count(engine, "product_activity_events") == 0
        session.commit()
    [row] = _rows(engine, "product_activity_events")
    expires = occurred + timedelta(days=90)
    assert row == {"id": event_id, "user_id": USER, "visit_id": kwargs["visit_id"],
                   "idempotency_key": kwargs["idempotency_key"], "event_type": "education_used",
                   "surface": "education", "executed_route": None, "source": "client_signal",
                   "source_operation_kind": None, "source_operation_id": None, "outcome": "succeeded",
                   "delivery_status": "not_applicable", "occurred_at": occurred, "recorded_at": clock.now,
                   "updated_at": clock.now, "resolved_at": occurred, "delivered_at": None,
                   "retention_scopes": {"version": "retention-scopes-v1", "scopes": [
                       {"kind": "campaign", "campaign_id": str(campaign_id), "policy_version": "v0-retention-v1",
                        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}]},
                   "next_scope_expires_at": expires, "purge_after_at": expires,
                   "schema_version": "product-activity-event-v1"}
    with Sessions() as session:
        scopes = pr.validate_retention_columns(row["retention_scopes"], next_scope_expires_at=expires,
                                               purge_after_at=expires)
        pr.verify_scopes_against_campaigns(session, scopes, user_id=USER, occurred_at=occurred)


def test_pg_business_event_with_canonical_operation(engine, Sessions, clock):
    _ready(Sessions, clock)
    kwargs = _business_kwargs(occurred_at=clock.now, resolved_at=clock.now, delivered_at=clock.now)
    _record(Sessions, **kwargs)
    [row] = _rows(engine, "product_activity_events")
    assert (row["source"], row["source_operation_kind"], row["source_operation_id"], row["executed_route"],
            row["delivery_status"], row["delivered_at"]) == (
        "server_business", "assistant_delivery", kwargs["source_operation_id"], "legacy", "confirmed", clock.now)


def _expect_not_authorized(engine, Sessions, **kwargs):
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(EventNotAuthorized):
            pe.record_event(session, **_event_kwargs(**kwargs))
        session.commit()
    assert _snapshot(engine) == before


def test_pg_event_without_participation_is_refused(engine, Sessions, clock):
    _ready(Sessions, clock, user=OTHER_USER)
    _expect_not_authorized(engine, Sessions, occurred_at=clock.now, resolved_at=clock.now)


@pytest.mark.parametrize("status", ["withdrawn", "completed"])
def test_pg_event_of_an_inactive_participation_is_refused(engine, Sessions, clock, status):
    _ready(Sessions, clock)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE product_campaign_participants SET participation_status = :s"), {"s": status})
    _expect_not_authorized(engine, Sessions, occurred_at=clock.now, resolved_at=clock.now)


@pytest.mark.parametrize("case", ["after_window", "before_window", "window_end_exclusive"])
def test_pg_event_outside_the_observation_window_is_refused(engine, Sessions, clock, case):
    _ready(Sessions, clock)  # fenêtre [T0, T0 + 14 j)
    occurred = {"after_window": T0 + timedelta(days=15), "before_window": T0 - timedelta(seconds=1),
                "window_end_exclusive": T0 + timedelta(days=14)}[case]
    clock.now = max(clock.now, occurred + timedelta(seconds=1))
    _expect_not_authorized(engine, Sessions, occurred_at=occurred, resolved_at=occurred)
    # La borne de début est incluse.
    _record(Sessions, **_event_kwargs(occurred_at=T0, resolved_at=T0))


@pytest.mark.parametrize("case", ["planned", "closed", "cancelled", "before_activation"])
def test_pg_event_of_a_campaign_that_is_not_collecting_is_refused(engine, Sessions, clock, case):
    campaign_id = _planned(Sessions)
    _enrolled(Sessions, clock, campaign_id)  # fenêtre [T0, T0 + 14 j), campagne encore planned
    clock.advance(days=1)
    if case != "planned":
        _committed(Sessions, lambda s: pc.activate_campaign(s, campaign_id=campaign_id))
    if case in ("closed", "cancelled"):
        clock.advance(hours=1)
        action = {"closed": "close", "cancelled": "cancel"}[case]
        _committed(Sessions, lambda s: getattr(pc, f"{action}_campaign")(s, campaign_id=campaign_id))
    occurred = T0 + timedelta(hours=1) if case == "before_activation" else clock.now - timedelta(minutes=10)
    _expect_not_authorized(engine, Sessions, occurred_at=occurred, resolved_at=occurred)


def test_pg_event_already_past_its_retention_is_refused(engine, Sessions, clock):
    campaign_id = _active(Sessions, clock, close_by_at=T0 + timedelta(days=200))
    _committed(Sessions, lambda s: pp.enroll_participant(
        s, campaign_id=campaign_id, user_id=USER, window_start_at=T0, window_end_at=T0 + timedelta(days=150),
        authorization_recorded_at=T0, notice_version="v0-notice-1"))
    clock.advance(days=95)
    old = T0 + timedelta(days=4)  # échéance T0 + 94 j, dépassée
    _expect_not_authorized(engine, Sessions, occurred_at=old, resolved_at=old)
    recent = T0 + timedelta(days=6)
    _record(Sessions, **_event_kwargs(occurred_at=recent, resolved_at=recent))


def test_pg_future_occurrence_is_refused(engine, Sessions, clock):
    _ready(Sessions, clock)
    future = clock.now + timedelta(seconds=1)
    with Sessions() as session:
        with pytest.raises(InvalidProductAnalyticsInput):
            pe.record_event(session, **_event_kwargs(occurred_at=future, resolved_at=future))
    assert _count(engine, "product_activity_events") == 0


def test_pg_two_active_campaigns_give_two_sorted_scopes(engine, Sessions, clock):
    first = _active(Sessions, clock)
    second = _active(Sessions, clock)
    other = _active(Sessions, clock)
    for campaign_id in (first, second):
        _enrolled(Sessions, clock, campaign_id)
    _enrolled(Sessions, clock, other, OTHER_USER)
    clock.advance(hours=1)
    _record(Sessions, **_event_kwargs(occurred_at=clock.now, resolved_at=clock.now))
    [row] = _rows(engine, "product_activity_events")
    assert [s["campaign_id"] for s in row["retention_scopes"]["scopes"]] == sorted([str(first), str(second)])
    assert row["next_scope_expires_at"] == row["purge_after_at"] == clock.now + timedelta(days=90)


# --- Événements : idempotence -------------------------------------------------

def test_pg_identical_retry_returns_the_stored_event_without_any_write(engine, Sessions, clock):
    campaign_id = _ready(Sessions, clock)
    kwargs = _event_kwargs(occurred_at=clock.now, resolved_at=clock.now)
    event_id, _ = _record(Sessions, **kwargs)
    before = _snapshot(engine)
    clock.advance(minutes=3)
    # Entre-temps : nouvelle campagne et retrait de la première ; la
    # répétition n'ajoute aucune finalité et ne réévalue rien.
    second = _active(Sessions, clock)
    _enrolled(Sessions, clock, second)
    _committed(Sessions, lambda s: pp.withdraw_participant(s, campaign_id=campaign_id, user_id=USER))
    snapshot = _rows(engine, "product_activity_events")
    with Sessions() as session, _Statements(engine) as statements:
        event, created = pe.record_event(session, **kwargs)
        assert (event.id, created) == (event_id, False)
        session.commit()
    assert _writes(statements) == []
    assert _rows(engine, "product_activity_events") == snapshot == before["product_activity_events"]


@pytest.mark.parametrize("field", pe.REQUEST_FIELDS)
def test_pg_key_reuse_with_incompatible_data_is_a_collision(engine, Sessions, clock, field):
    _ready(Sessions, clock)
    occurred = clock.now - timedelta(minutes=10)
    kwargs = _business_kwargs(occurred_at=occurred, resolved_at=occurred, delivered_at=occurred)
    _record(Sessions, **kwargs)
    changed = {
        "visit_id": {"visit_id": uuid.uuid4()},
        "event_type": {"event_type": "portfolio_used", "surface": "portfolio", "source_operation_kind": "portfolio_write"},
        "surface": {"event_type": "portfolio_used", "surface": "portfolio", "source_operation_kind": "portfolio_write"},
        "executed_route": {"executed_route": "f2"},
        "source": {"source": "client_signal", "source_operation_kind": None, "source_operation_id": None,
                   "executed_route": None, "delivery_status": "not_applicable", "delivered_at": None},
        "source_operation_kind": {"event_type": "portfolio_used", "surface": "portfolio",
                                  "source_operation_kind": "portfolio_write"},
        "source_operation_id": {"source_operation_id": uuid.uuid4()},
        "outcome": {"outcome": "failed"},
        "delivery_status": {"delivery_status": "unconfirmed", "delivered_at": None},
        "occurred_at": {"occurred_at": occurred - timedelta(microseconds=1)},
        "resolved_at": {"resolved_at": occurred + timedelta(minutes=1)},
        "delivered_at": {"delivered_at": occurred + timedelta(minutes=1)},
    }[field]
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(EventIdempotencyCollision):
            pe.record_event(session, **{**kwargs, **changed})
        session.commit()
    assert _snapshot(engine) == before


def test_pg_same_key_for_another_user_is_independent(engine, Sessions, clock):
    _ready(Sessions, clock)
    campaign_id = _rows(engine, "product_campaigns")[0]["id"]
    clock.advance(minutes=1)
    _committed(Sessions, lambda s: pp.enroll_participant(
        s, campaign_id=campaign_id, user_id=OTHER_USER, window_start_at=clock.now,
        window_end_at=clock.now + timedelta(days=7), authorization_recorded_at=clock.now, notice_version="n1"))
    clock.advance(minutes=1)
    kwargs = _event_kwargs(occurred_at=clock.now, resolved_at=clock.now)
    assert _record(Sessions, **kwargs)[1] is True
    assert _record(Sessions, **{**kwargs, "user_id": OTHER_USER})[1] is True
    assert _count(engine, "product_activity_events") == 2


def test_pg_stored_event_with_invalid_retention_or_schema_fails_closed(engine, Sessions, clock):
    _ready(Sessions, clock)
    kwargs = _event_kwargs(occurred_at=clock.now, resolved_at=clock.now)
    _record(Sessions, **kwargs)
    extended = clock.now + timedelta(days=91)
    tamperings = [
        ("UPDATE product_activity_events SET next_scope_expires_at = next_scope_expires_at - interval '1 day'",
         InvalidRetentionScopes),
        ("UPDATE product_activity_events SET retention_scopes = jsonb_set(retention_scopes, '{scopes,0,expires_at}', "
         f"'\"{extended.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}\"'), next_scope_expires_at = :e, purge_after_at = :e",
         InvalidRetentionScopes),
        ("UPDATE product_activity_events SET retention_scopes = jsonb_set(retention_scopes, '{scopes,0,campaign_id}', "
         f"'\"{uuid.uuid4()}\"')", InvalidRetentionScopes),
        ("UPDATE product_activity_events SET schema_version = 'product-activity-event-v0'",
         UnsupportedProductEventSchema),
    ]
    with engine.connect() as conn:
        original = dict(conn.execute(sa.text(
            "SELECT retention_scopes, next_scope_expires_at, purge_after_at, schema_version "
            "FROM product_activity_events")).mappings().one())
    for statement, error in tamperings:
        with engine.begin() as conn:
            conn.execute(sa.text(statement), {"e": extended})
        with Sessions() as session:
            with pytest.raises(error):
                pe.record_event(session, **kwargs)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE product_activity_events SET retention_scopes = CAST(:retention_scopes AS jsonb), "
                "next_scope_expires_at = :next_scope_expires_at, purge_after_at = :purge_after_at, "
                "schema_version = :schema_version"), {**original, "retention_scopes": json.dumps(original["retention_scopes"])})
    assert _record(Sessions, **kwargs)[1] is False


# --- Événements : unicités métier --------------------------------------------

def test_pg_single_session_started_per_user_and_visit(engine, Sessions, clock):
    _ready(Sessions, clock)
    _ready(Sessions, clock, user=OTHER_USER)
    visit = uuid.uuid4()
    session_kwargs = {"event_type": "session_started", "surface": "app", "source": "server_visit", "visit_id": visit,
                      "occurred_at": clock.now, "resolved_at": clock.now}
    _record(Sessions, **_event_kwargs(**session_kwargs))
    before = _snapshot(engine)
    with Sessions() as session:
        with pytest.raises(EventUniquenessConflict) as failure:
            pe.record_event(session, **_event_kwargs(**session_kwargs))
        assert failure.value.reason == "session_start"
    assert _snapshot(engine) == before
    # Autre visite, autre utilisateur, autre type dans la même visite : admis.
    _record(Sessions, **_event_kwargs(**{**session_kwargs, "visit_id": uuid.uuid4()}))
    _record(Sessions, **_event_kwargs(**{**session_kwargs, "user_id": OTHER_USER}))
    _record(Sessions, **_event_kwargs(visit_id=visit, occurred_at=clock.now, resolved_at=clock.now))
    assert _count(engine, "product_activity_events") == 4


def test_pg_canonical_operation_is_unique_per_owner(engine, Sessions, clock):
    _ready(Sessions, clock)
    _ready(Sessions, clock, user=OTHER_USER)
    operation = uuid.uuid4()
    kwargs = _business_kwargs(source_operation_id=operation, occurred_at=clock.now, resolved_at=clock.now,
                              delivered_at=clock.now)
    _record(Sessions, **kwargs)
    with Sessions() as session:
        with pytest.raises(EventUniquenessConflict) as failure:
            pe.record_event(session, **{**kwargs, "idempotency_key": uuid.uuid4(), "visit_id": uuid.uuid4()})
        assert failure.value.reason == "source_operation"
    _record(Sessions, **{**kwargs, "user_id": OTHER_USER, "idempotency_key": uuid.uuid4()})
    _record(Sessions, **_business_kwargs(event_type="portfolio_used", surface="portfolio",
                                         source_operation_kind="portfolio_write", source_operation_id=operation,
                                         occurred_at=clock.now, resolved_at=clock.now, delivered_at=clock.now))
    assert _count(engine, "product_activity_events") == 3


# --- Concurrence réelle -------------------------------------------------------

def test_pg_concurrent_identical_events_insert_once(engine, Sessions, clock):
    _ready(Sessions, clock)
    kwargs = _event_kwargs(occurred_at=clock.now, resolved_at=clock.now)
    with Sessions() as first, Sessions() as second:
        event_id = pe.record_event(first, **kwargs)[0].id

        def retry(session):
            event, created = pe.record_event(session, **kwargs)
            return event.id, created
        result, error = _run_blocked(engine, first, second, retry)
    assert (result, error) == ((event_id, False), None)
    assert _count(engine, "product_activity_events") == 1


def test_pg_concurrent_same_key_with_other_data_is_a_collision(engine, Sessions, clock):
    _ready(Sessions, clock)
    kwargs = _event_kwargs(occurred_at=clock.now, resolved_at=clock.now)
    with Sessions() as first, Sessions() as second:
        pe.record_event(first, **kwargs)
        _, error = _run_blocked(engine, first, second,
                                lambda s: pe.record_event(s, **{**kwargs, "visit_id": uuid.uuid4()}))
    assert isinstance(error, EventIdempotencyCollision)
    assert _count(engine, "product_activity_events") == 1


def test_pg_concurrent_insert_after_rollback_inserts_once(engine, Sessions, clock):
    _ready(Sessions, clock)
    kwargs = _event_kwargs(occurred_at=clock.now, resolved_at=clock.now)
    with Sessions() as first, Sessions() as second:
        pe.record_event(first, **kwargs)
        result, error = _run_blocked(engine, first, second, lambda s: pe.record_event(s, **kwargs),
                                     release="rollback")
    assert error is None and result[1] is True
    assert _count(engine, "product_activity_events") == 1


@pytest.mark.parametrize("kind", ["session_start", "source_operation"])
def test_pg_concurrent_business_uniqueness_writes_once(engine, Sessions, clock, kind):
    _ready(Sessions, clock)
    if kind == "session_start":
        kwargs = _event_kwargs(event_type="session_started", surface="app", source="server_visit",
                               occurred_at=clock.now, resolved_at=clock.now)
    else:
        kwargs = _business_kwargs(occurred_at=clock.now, resolved_at=clock.now, delivered_at=clock.now)
    with Sessions() as first, Sessions() as second:
        pe.record_event(first, **kwargs)
        _, error = _run_blocked(engine, first, second, lambda s: pe.record_event(
            s, **{**kwargs, "idempotency_key": uuid.uuid4()}))
    assert isinstance(error, EventUniquenessConflict) and error.reason == kind
    assert _count(engine, "product_activity_events") == 1


def test_pg_closing_waits_for_an_event_being_recorded(engine, Sessions, clock):
    campaign_id = _ready(Sessions, clock)
    with Sessions() as recorder, Sessions() as closer:
        pe.record_event(recorder, **_event_kwargs(occurred_at=clock.now, resolved_at=clock.now))
        result, error = _run_blocked(engine, recorder, closer,
                                     lambda s: pc.close_campaign(s, campaign_id=campaign_id).status)
    assert (result, error) == ("closed", None)
    assert _count(engine, "product_activity_events") == 1


@pytest.mark.parametrize("action", ["close", "cancel", "withdraw"])
def test_pg_event_waits_for_a_concurrent_closing_or_withdrawal_then_is_refused(engine, Sessions, clock, action):
    campaign_id = _ready(Sessions, clock)
    with Sessions() as holder, Sessions() as recorder:
        if action == "withdraw":
            pp.withdraw_participant(holder, campaign_id=campaign_id, user_id=USER)
        else:
            getattr(pc, f"{action}_campaign")(holder, campaign_id=campaign_id)
        _, error = _run_blocked(engine, holder, recorder, lambda s: pe.record_event(
            s, **_event_kwargs(occurred_at=clock.now, resolved_at=clock.now)))
    assert isinstance(error, EventNotAuthorized)
    assert _count(engine, "product_activity_events") == 0


# --- Atomicité et rollback ------------------------------------------------------

def test_pg_caller_rollback_discards_everything(engine, Sessions, clock):
    with Sessions() as session:
        campaign = pc.create_campaign(session, **_campaign_kwargs())
        pc.activate_campaign(session, campaign_id=campaign.id)
        pp.enroll_participant(session, **_enroll_kwargs(campaign_id=campaign.id))
        clock.advance(hours=1)
        pe.record_event(session, **_event_kwargs(occurred_at=clock.now, resolved_at=clock.now))
        session.rollback()
    assert all(_count(engine, table) == 0 for table in TABLES)


def test_pg_a_refused_event_leaves_the_caller_transaction_usable(engine, Sessions, clock):
    campaign_id = _ready(Sessions, clock)
    kwargs = _event_kwargs(event_type="session_started", surface="app", source="server_visit",
                           occurred_at=clock.now, resolved_at=clock.now)
    _record(Sessions, **kwargs)
    with Sessions() as session:
        pp.enroll_participant(
            session, campaign_id=campaign_id, user_id=OTHER_USER, window_start_at=clock.now,
            window_end_at=clock.now + timedelta(days=1), authorization_recorded_at=clock.now, notice_version="n1")
        with pytest.raises(EventUniquenessConflict):
            pe.record_event(session, **{**kwargs, "idempotency_key": uuid.uuid4()})
        with pytest.raises(EventIdempotencyCollision):
            pe.record_event(session, **{**kwargs, "visit_id": uuid.uuid4()})
        session.commit()
    assert _count(engine, "product_campaign_participants") == 2
    assert _count(engine, "product_activity_events") == 1


def test_pg_database_failure_rolls_back_the_savepoint_only(engine, Sessions, clock, monkeypatch):
    """Échec PostgreSQL réel (CHECK ck_product_activity_events_retention_window)
    provoqué en inversant les échéances calculées : l'IntegrityError est
    propagée, le SAVEPOINT est annulé, aucune ligne partielle, et la
    transaction de l'appelant reste utilisable."""
    campaign_id = _ready(Sessions, clock)
    real_bounds = pr.retention_bounds
    monkeypatch.setattr(pr, "retention_bounds",
                        lambda scopes: (real_bounds(scopes)[0] + timedelta(days=1), real_bounds(scopes)[1]))
    with Sessions() as session:
        pp.enroll_participant(session, campaign_id=campaign_id, user_id=OTHER_USER, window_start_at=clock.now,
                              window_end_at=clock.now + timedelta(days=1), authorization_recorded_at=clock.now,
                              notice_version="n1")
        with pytest.raises(IntegrityError) as failure:
            pe.record_event(session, **_event_kwargs(occurred_at=clock.now, resolved_at=clock.now))
        assert failure.value.orig.diag.constraint_name == "ck_product_activity_events_retention_window"
        assert session.in_transaction()
        session.commit()
    assert _count(engine, "product_campaign_participants") == 2
    assert _count(engine, "product_activity_events") == 0


def test_pg_services_write_only_their_own_tables(engine, Sessions, clock):
    tables_before = _snapshot(engine)["users"]
    with Sessions() as session, _Statements(engine) as statements:
        campaign = pc.create_campaign(session, **_campaign_kwargs())
        pc.activate_campaign(session, campaign_id=campaign.id)
        pp.enroll_participant(session, **_enroll_kwargs(campaign_id=campaign.id))
        clock.advance(hours=1)
        pe.record_event(session, **_event_kwargs(occurred_at=clock.now, resolved_at=clock.now))
        pp.withdraw_participant(session, campaign_id=campaign.id, user_id=USER)
        pc.close_campaign(session, campaign_id=campaign.id)
        session.commit()
    writes = [w for w in _writes(statements) if not w.startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))]
    assert writes and all(w.split()[0] in ("INSERT", "UPDATE") for w in writes)
    assert {w.split()[2] if w.startswith("INSERT") else w.split()[1] for w in writes} <= set(TABLES)
    assert _snapshot(engine)["users"] == tables_before
