"""V0-T08 Lot 2C PR 2 — cycle de vie des événements d'activité produit :
expiration des finalités, suppression physique des événements, de leurs
attributions et des preuves recorded qui les référencent encore
(core/product_analytics/event_lifecycle.py, frontière
core/_activity_event_lifecycle.py, garde-fous core/_activity_transactions.py),
contre un VRAI PostgreSQL (16 et 18 en CI). Chaîne réelle F2 -> B -> C ->
purges : tests/test_activity_event_lifecycle_chain.py.

A. contrats d'entrée (refus avant tout accès à la base) ; B. temporalité
(bornes à la microseconde, horloge régressive, attente franchissant
l'échéance, fuseaux) ; C. événements historiques et certifiés ; D.
suppressions atomiques ; E. concurrence ; F. propriété transactionnelle et
COMMIT incertain ; G. intégrité ; H. pagination ; I. non-recréation ;
inventaires et dormance.
"""
import ast
import hashlib
import inspect
import json
import logging
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core import _activity_event_lifecycle as lb
from core import _activity_transactions as tx
from core.models import ProductActivityEvent, ProductActivityProof
from core.product_analytics import campaigns as pc
from core.product_analytics import common
from core.product_analytics import event_lifecycle as el
from core.product_analytics import events as pe
from core.product_analytics import finalization as fz
from core.product_analytics import participation as pp
from core.product_analytics import proof_lifecycle as pl
from core.product_analytics import proofs
from core.product_analytics import retention
from core.product_analytics.common import InvalidProductAnalyticsInput
from tests.activity_proof_support import (
    ATTRIBUTIONS,
    EVENTS,
    PROOF_APP,
    REGISTRY,
    Background,
    Clock,
    Holder,
    committed,
    open_wave,
    proof_row,
    rows,
    utc,
    waiting,
)
from tests.test_activity_event_finalization import (  # noqa: F401 — fixtures engine, db, clock
    HOURS_168,
    MICRO,
    USER_A,
    USER_B,
    _certified,
    _sleep_until,
    _wait,
    clock,
    db,
    engine,
    finalize,
)
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

SERVICE = REPO_ROOT / "core" / "product_analytics" / "event_lifecycle.py"
BOUNDARY = REPO_ROOT / "core" / "_activity_event_lifecycle.py"
TRANSACTIONS = REPO_ROOT / "core" / "_activity_transactions.py"
INTERACTIONS = "product_activity_session_interactions"
SESSIONS = "product_activity_sessions"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DAYS_90 = timedelta(days=90)
NEXT_INDEX = "ix_product_activity_events_next_scope_expires_at"
ENTRY_POINTS = (lb.expire_event, lb.expire_events_page, lb.inspect_event, lb.inspect_events_page)
SERVICE_ENTRY_POINTS = (el.select_expired_event_candidates, el.expire_event_scopes, el.observe_event_retention,
                        el.select_integrity_candidates, el.inspect_event_integrity)


def _call(function, engine, event_id=None):
    if function in (lb.expire_event, lb.inspect_event):
        return function(engine, event_id=event_id or uuid.uuid4())
    return function(engine)


# --------------------------------------------------------------------------
# A. Contrats d'entrée (sans base)
# --------------------------------------------------------------------------

def test_the_vocabularies_are_closed_and_the_two_reports_are_distinct():
    assert lb.OUTCOMES == ("pruned", "purged", "not_due", "absent", "conflict", "unknown", "unconfirmed",
                           "still_present")
    assert lb.INTEGRITY_OUTCOMES == el.INTEGRITY_OUTCOMES == ("valid", "inconsistent", "absent", "unknown")
    assert set(el.EXPIRY_OUTCOMES) < set(lb.OUTCOMES)
    assert len(set(lb.OUTCOME_REASONS)) == len(lb.OUTCOME_REASONS)
    assert el.DEFAULT_PAGE_SIZE == 50 and el.MAX_PAGE_SIZE == 200
    expiry = [f for f in lb.ExpiryPageReport.__dataclass_fields__ if f not in ("selection", "selected", "examined",
                                                                                "next_cursor")]
    integrity = [f for f in lb.IntegrityPageReport.__dataclass_fields__ if f not in ("selection", "selected",
                                                                                      "examined", "next_cursor")]
    assert tuple(expiry) == lb.OUTCOMES and tuple(integrity) == lb.INTEGRITY_OUTCOMES
    # Les identités de la preuve et de l'attribution sont celles du bloc C ; les
    # champs repris de l'événement sont ceux de la FK composite de 0024.
    assert el.IDENTITY_FIELDS == fz.IDENTITY_FIELDS
    assert set(el.EVENT_IDENTITY_FIELDS) < set(fz.IDENTITY_FIELDS)


def test_the_entry_points_receive_only_bounded_and_typed_inputs_and_refuse_before_any_database_access():
    for function in (el.expire_event_scopes, el.observe_event_retention, el.inspect_event_integrity):
        assert list(inspect.signature(function).parameters) == ["db", "event_id"]
        for bad in (str(uuid.uuid4()), None, 42, uuid.uuid4().int):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), event_id=bad)
        for designation in ({"force": True}, {"now": datetime.now(timezone.utc)}, {"user_id": USER_A},
                            {"campaign_id": uuid.uuid4()}, {"expires_at": datetime.now(timezone.utc)},
                            {"retention_scopes": {}}, {"scopes": ()}):
            with pytest.raises(TypeError):
                function(_NoDB(), event_id=uuid.uuid4(), **designation)
    for function, cursor, foreign in ((el.select_expired_event_candidates, el.ExpiryCursor(
            datetime.now(timezone.utc), uuid.uuid4()), el.IntegrityCursor(uuid.uuid4())),
            (el.select_integrity_candidates, el.IntegrityCursor(uuid.uuid4()),
             el.ExpiryCursor(datetime.now(timezone.utc), uuid.uuid4()))):
        assert list(inspect.signature(function).parameters) == ["db", "limit", "after"]
        for limit in (0, 201, -1, True, 1.0, "5", None):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), limit=limit)
        for after in (foreign, SimpleNamespace(**{"event_id": uuid.uuid4()}), "cursor", uuid.uuid4()):
            with pytest.raises(InvalidProductAnalyticsInput):  # curseurs nominaux et d'intégrité incompatibles
                function(_NoDB(), after=after)
        for kwargs in ({"now": datetime.now(timezone.utc)}, {"user_id": USER_A}, {"offset": 3}):
            with pytest.raises(TypeError):
                function(_NoDB(), **kwargs)
        assert function.__defaults__ is None and function.__kwdefaults__ == {"limit": 50, "after": None}
        del cursor
    for forged in (el.ExpiryCursor(datetime(2026, 11, 25), uuid.uuid4()), el.ExpiryCursor(
            datetime.now(timezone.utc), str(uuid.uuid4()))):
        with pytest.raises(InvalidProductAnalyticsInput):
            el.select_expired_event_candidates(_NoDB(), after=forged)
    with pytest.raises(InvalidProductAnalyticsInput):
        el.select_integrity_candidates(_NoDB(), after=el.IntegrityCursor("x"))
    with pytest.raises(InvalidProductAnalyticsInput):
        el._observe_attempt(_NoDB(), attempt=SimpleNamespace(event_id=uuid.uuid4(), keys=()))
    # Frontière : moteur exigé, identifiant exact, bornes validées avant toute connexion.
    for function in ENTRY_POINTS:
        with pytest.raises(TypeError):
            _call(function, _NoDB())
    public = {name for name in dir(el) if not name.startswith("_") and inspect.isfunction(getattr(el, name))
              and getattr(el, name).__module__ == el.__name__}
    assert public == {f.__name__ for f in SERVICE_ENTRY_POINTS}  # aucun autre point d'entrée métier
    boundary_public = {name for name in dir(lb) if not name.startswith("_") and inspect.isfunction(getattr(lb, name))
                       and getattr(lb, name).__module__ == lb.__name__}
    assert boundary_public == {f.__name__ for f in ENTRY_POINTS}
    for function in (lb.expire_event, lb.inspect_event):
        assert list(inspect.signature(function).parameters) == ["engine", "event_id"]
    for function in (lb.expire_events_page, lb.inspect_events_page):
        assert list(inspect.signature(function).parameters) == ["engine", "limit", "after"]


def test_the_boundary_validates_identifiers_limits_and_cursors_before_connecting(db):
    statements, connects = [], []
    probe = sa.create_engine(db.url, poolclass=sa.pool.NullPool)
    sa.event.listen(probe, "connect", lambda *a: connects.append(1))
    sa.event.listen(probe, "before_cursor_execute", lambda *a: statements.append(1))
    for bad in (str(uuid.uuid4()), None, 3):
        for function in (lb.expire_event, lb.inspect_event):
            with pytest.raises(TypeError):
                function(probe, event_id=bad)
    for limit in (0, 201, True, "1"):
        for function in (lb.expire_events_page, lb.inspect_events_page):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(probe, limit=limit)
    with pytest.raises(InvalidProductAnalyticsInput):
        lb.expire_events_page(probe, after=el.IntegrityCursor(uuid.uuid4()))
    with pytest.raises(InvalidProductAnalyticsInput):
        lb.inspect_events_page(probe, after=el.ExpiryCursor(datetime.now(timezone.utc), uuid.uuid4()))
    assert statements == [] and connects == []
    probe.dispose()


# --------------------------------------------------------------------------
# PostgreSQL réel : outils
# --------------------------------------------------------------------------

def _record(db, user=USER_A, *, event_type="portfolio_used", surface="portfolio", source="server_business",
            kind="portfolio_write", operation_id=None, visit_id=None, key=None, occurred_at=None):
    now = occurred_at or common.utcnow()
    business = source == "server_business"
    kwargs = {"user_id": user, "visit_id": visit_id or uuid.uuid4(), "idempotency_key": key or uuid.uuid4(),
              "event_type": event_type, "surface": surface, "executed_route": "legacy" if business else None,
              "source": source, "source_operation_kind": kind if business else None,
              "source_operation_id": (operation_id or uuid.uuid4()) if business else None, "outcome": "succeeded",
              "delivery_status": "not_applicable", "occurred_at": now, "resolved_at": now, "delivered_at": None}
    event, created = committed(db, lambda s: pe.record_event(s, **kwargs))
    assert created
    return SimpleNamespace(id=event.id, kwargs=kwargs)


def _historical(db, user=USER_A, *, campaigns=1, **kwargs):
    """Événement 2A réel (record_event), une finalité par campagne active."""
    campaign_ids = [open_wave(db, user) for _ in range(campaigns)]
    event = _record(db, user, **kwargs)
    event.campaign_ids = campaign_ids
    return event


def _certified_event(db, clock, user=USER_A, *, campaign_id=None):
    """Chaîne B -> C réelle : preuve recorded, événement certifié, attribution."""
    proof = _certified(db, user, campaign_id=campaign_id)
    clock.advance(seconds=1)
    decision = finalize(db, proof.proof_id)
    assert decision.state == fz.RECORDED
    proof.id = decision.event_id
    return proof


def _event(db, event_id):
    found = rows(db, f"SELECT *, xmin::text AS xmin FROM {EVENTS} WHERE id = :i", i=event_id)
    return found[0] if found else None


def _attribution(db, event_id):
    found = rows(db, f"SELECT *, xmin::text AS xmin FROM {ATTRIBUTIONS} WHERE event_id = :i", i=event_id)
    return found[0] if found else None


def _expiry(db, event_id, which=min):
    return which(retention.parse_instant(s["expires_at"]) for s in _event(db, event_id)["retention_scopes"]["scopes"])


def _table(db, table):
    return rows(db, f"SELECT t.*, t.xmin::text AS xmin FROM {table} t ORDER BY t::text")


ALL_TABLES = (EVENTS, ATTRIBUTIONS, REGISTRY, INTERACTIONS, SESSIONS, "product_campaign_participants",
              "product_campaigns", "users")


def _world(db, *, without=()) -> dict:
    """Toutes les lignes analytics (xmin compris), sauf celles de `without`."""
    world = {}
    for table in ALL_TABLES:
        world[table] = [r for r in _table(db, table)
                        if r.get("id") not in without and r.get("event_id") not in without]
    return world


def _rewrite_scopes(db, event_id, expirations: dict, *, keep_columns=False):
    """Écrivain HORS SERVICE (scénario explicite, simulation d'une future
    révision de finalité) : réécrit les échéances de certaines finalités ;
    document canonique et colonnes cohérents sauf keep_columns."""
    event = _event(db, event_id)
    scopes = [retention.RetentionScope(s.kind, s.campaign_id, s.policy_version,
                                       expirations.get(s.campaign_id, s.expires_at))
              for s in retention.parse_retention_scopes(event["retention_scopes"])]
    document = retention.build_retention_document(scopes)
    first, last = retention.retention_bounds(scopes)
    if keep_columns:
        first, last = event["next_scope_expires_at"], event["purge_after_at"]
    _set(db, event_id, retention_scopes=document, next_scope_expires_at=first, purge_after_at=last)
    return document


def _set(db, event_id, **values):
    """Écrivain hors service : UPDATE brut d'un événement."""
    with db.begin() as conn:
        conn.execute(sa.update(ProductActivityEvent.__table__).where(ProductActivityEvent.__table__.c.id == event_id)
                     .values(**values))


def _shorten(db, event_id, campaign_id, expires_at):
    return _rewrite_scopes(db, event_id, {campaign_id: expires_at})


def expire(db, event_id):
    return lb.expire_event(db, event_id=event_id)


def inspect_one(db, event_id):
    return lb.inspect_event(db, event_id=event_id)


def _statements(engine):
    seen = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))
    sa.event.listen(engine, "before_cursor_execute", capture)
    return seen, lambda: sa.event.remove(engine, "before_cursor_execute", capture)


def _keys(db, event_id) -> tuple:
    event = committed(db, lambda s: s.get(ProductActivityEvent, event_id))
    return tuple(sorted(el._event_lock_keys(event)))


def _hold_keys(db, event_id):
    holder = Holder(db)
    common.lock_identities(holder.db, *_keys(db, event_id))
    return holder


PURGED = lb.EventOutcome(lb.PURGED)
PRUNED = lb.EventOutcome(lb.PRUNED)
NOT_DUE = lb.EventOutcome(lb.NOT_DUE)
ABSENT = lb.EventOutcome(lb.ABSENT)


# --------------------------------------------------------------------------
# B. Temporalité et conservation
# --------------------------------------------------------------------------

def test_a_historical_event_is_purged_from_its_exact_deadline_never_a_microsecond_before(db, clock):
    event = _historical(db)
    other = _historical(db, USER_B)
    expiry = _expiry(db, event.id)
    assert expiry == utc(_event(db, event.id)["occurred_at"]) + DAYS_90  # 90 jours depuis occurred_at
    world = _world(db)
    clock.set(expiry - MICRO)
    assert expire(db, event.id) == NOT_DUE
    assert _world(db) == world  # rien écrit, xmin compris
    clock.set(expiry)  # égalité : expirée
    assert expire(db, event.id) == PURGED
    assert _event(db, event.id) is None
    assert {r["id"] for r in _table(db, EVENTS)} == {other.id}
    assert [r for r in _table(db, EVENTS)] == [r for r in world[EVENTS] if r["id"] == other.id]  # xmin intact
    assert expire(db, event.id) == ABSENT  # réexécution idempotente
    later = _historical(db)
    clock.set(_expiry(db, later.id) + timedelta(days=30))
    assert expire(db, later.id) == PURGED


def test_a_regressive_clock_never_writes(db, clock):
    campaign_id = open_wave(db, USER_A)
    clock.advance(minutes=1)
    event = _record(db, occurred_at=clock.now - timedelta(seconds=30))  # enregistré 30 s après l'occurrence
    occurred = utc(_event(db, event.id)["occurred_at"])
    _shorten(db, event.id, campaign_id, occurred + MICRO)  # finalité raccourcie, déjà échue
    row = _event(db, event.id)
    clock.set(utc(row["updated_at"]) - MICRO)  # postérieure à l'échéance, antérieure à updated_at
    assert expire(db, event.id) == lb.EventOutcome(lb.UNKNOWN, el.CLOCK_INCOHERENT)
    assert _event(db, event.id) == row
    clock.set(utc(row["updated_at"]))
    assert expire(db, event.id) == PURGED


def test_a_status_change_never_advances_a_deadline(db, clock):
    event = _historical(db, campaigns=1)
    campaign_id = event.campaign_ids[0]
    committed(db, lambda s: pp.withdraw_participant(s, campaign_id=campaign_id, user_id=USER_A))
    committed(db, lambda s: pc.close_campaign(s, campaign_id=campaign_id))
    row = _event(db, event.id)
    clock.set(_expiry(db, event.id) - MICRO)
    assert expire(db, event.id) == NOT_DUE and _event(db, event.id) == row  # withdrawn, closed : aucune avance
    cancelled = _historical(db, USER_B)
    committed(db, lambda s: pc.cancel_campaign(s, campaign_id=cancelled.campaign_ids[0]))
    assert expire(db, cancelled.id) == NOT_DUE
    clock.set(_expiry(db, event.id))
    assert expire(db, event.id) == PURGED


def _real_clock_event(db, monkeypatch, *, due_in):
    """Événement historique dont l'unique finalité échoit à l'horloge
    PostgreSQL RÉELLE + due_in (raccourcissement simulé hors service) ;
    l'horloge réelle est rétablie pour le traitement."""
    fake = Clock(datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5))
    monkeypatch.setattr(common, "utcnow", fake)
    monkeypatch.setattr(proofs, "server_clock", fake)
    event = _historical(db)
    monkeypatch.undo()
    now = rows(db, "SELECT clock_timestamp() AS n")[0]["n"]
    _shorten(db, event.id, event.campaign_ids[0], now + due_in)
    return event


def test_a_lock_wait_crossing_the_deadline_is_decided_with_the_postgresql_clock_read_after_it(db, monkeypatch):
    event = _real_clock_event(db, monkeypatch, due_in=timedelta(milliseconds=1200))
    due = _expiry(db, event.id)
    assert committed(db, lambda s: el.select_expired_event_candidates(s)).event_ids == ()
    holder = _hold_keys(db, event.id)
    contender = Background(lambda: expire(db, event.id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "l'expiration n'attend pas les clés de l'événement")
        assert datetime.now(timezone.utc) < due
        _sleep_until(due + timedelta(milliseconds=200))  # attente totale < lock_timeout (2 s)
    finally:
        holder.rollback()
    assert contender.outcome() == PURGED
    early = _real_clock_event(db, monkeypatch, due_in=timedelta(minutes=5))
    assert expire(db, early.id) == NOT_DUE


TRANSITIONS = [
    # (fuseau de session, occurred_at UTC) : 90 jours franchissant un changement d'heure.
    ("UTC", datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)),
    ("Europe/Paris", datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)),  # hiver : 25/10/2026
    ("America/New_York", datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)),  # hiver : 01/11/2026
    ("Europe/Paris", datetime(2027, 1, 5, 10, 0, tzinfo=timezone.utc)),  # été : 28/03/2027
    ("America/New_York", datetime(2027, 1, 5, 10, 0, tzinfo=timezone.utc)),  # été : 14/03/2027
]


@pytest.mark.parametrize("zone, occurred", TRANSITIONS)
def test_the_90_days_are_absolute_whatever_the_session_time_zone(db, clock, zone, occurred):
    clock.set(occurred)
    event = _historical(db)
    expiry = _expiry(db, event.id)
    assert expiry - occurred == timedelta(hours=2160)
    zoned = sa.create_engine(db.url, poolclass=sa.pool.NullPool, connect_args={
        "options": f"-c timezone={zone}", "application_name": PROOF_APP})
    try:
        with zoned.connect() as conn:
            assert conn.execute(sa.text("SHOW timezone")).scalar_one() == zone
            civil = conn.execute(sa.text("SELECT CAST(:o AS timestamptz) + interval '90 days'"),
                                 {"o": occurred}).scalar_one()
        # Contre-épreuve : les jours civils du fuseau diffèrent des 2160 heures absolues.
        assert (civil == expiry) is (zone == "UTC")
        clock.set(expiry - MICRO)
        assert committed(zoned, lambda s: el.select_expired_event_candidates(s)).event_ids == ()
        assert lb.expire_event(zoned, event_id=event.id) == NOT_DUE
        clock.set(expiry)
        assert committed(zoned, lambda s: el.select_expired_event_candidates(s)).event_ids == (event.id,)
        assert lb.expire_event(zoned, event_id=event.id) == PURGED
    finally:
        zoned.dispose()


# --------------------------------------------------------------------------
# C. Événements historiques et certifiés
# --------------------------------------------------------------------------

HISTORICAL_SHAPES = [
    {"event_type": "portfolio_used", "surface": "portfolio", "kind": "portfolio_write"},
    {"event_type": "decrypter_used", "surface": "decrypter", "kind": "assistant_delivery"},
    {"event_type": "view_opened", "surface": "app", "source": "client_signal"},
    {"event_type": "session_started", "surface": "app", "source": "server_visit"},
]


@pytest.mark.parametrize("shape", HISTORICAL_SHAPES, ids=lambda s: s["event_type"])
def test_the_keys_are_exactly_those_of_the_writers_of_each_path(db, clock, shape):
    event = _historical(db, **shape)
    row = committed(db, lambda s: s.get(ProductActivityEvent, event.id))
    request = {"event_type": row.event_type, "visit_id": row.visit_id,
               "source_operation_kind": row.source_operation_kind, "source_operation_id": row.source_operation_id}
    assert el._path(row) == el.HISTORICAL  # decrypter_used / portfolio_used 2A : jamais certifiés
    assert set(el._event_lock_keys(row)) == set(pe._identity_locks(row.user_id, request, row.idempotency_key))
    expected = {common.identity_lock_key("event_key", row.user_id, row.idempotency_key)}
    if row.event_type == "session_started":
        expected.add(common.identity_lock_key("event_session_start", row.user_id, row.visit_id))
    if row.source_operation_kind is not None:
        expected.add(common.identity_lock_key("event_source_operation", row.user_id, row.source_operation_kind,
                                              row.source_operation_id))
    assert set(el._event_lock_keys(row)) == expected
    clock.set(_expiry(db, event.id))
    assert expire(db, event.id) == PURGED


def test_the_certified_keys_are_those_of_block_c_even_once_the_proof_is_gone(db, clock):
    proof = _certified_event(db, clock)
    stored_proof = committed(db, lambda s: s.get(ProductActivityProof, proof.proof_id))
    event = committed(db, lambda s: s.get(ProductActivityEvent, proof.id))
    assert el._path(event) == el.CERTIFIED and event.idempotency_key == stored_proof.id  # pas event.id
    keys = set(el._event_lock_keys(event))
    assert keys == set(fz.lock_keys(stored_proof))
    assert pl.proof_lock_key(stored_proof) in keys  # activity_proof
    historical = pe.CERTIFIED_OPERATIONS[event.source_operation_kind][2]
    assert common.identity_lock_key("event_source_operation", event.user_id, historical,
                                    event.source_operation_id) in keys  # opération historique équivalente
    assert common.identity_lock_key("event_key", event.user_id, event.id) not in keys
    # Preuve purgée (168 h) : les mêmes clés sont reconstruites depuis l'événement seul.
    clock.set(utc(proof_row(db, proof.proof_id)["purge_after_at"]))
    assert committed(db, lambda s: pl.purge_resolved_proof(s, proof_id=proof.proof_id)).outcome == pl.PURGED
    assert set(el._event_lock_keys(committed(db, lambda s: s.get(ProductActivityEvent, proof.id)))) == keys


def test_a_historical_event_loses_only_its_expired_scopes_and_nothing_else(db, clock):
    event = _historical(db, campaigns=3)
    first, second, third = event.campaign_ids
    occurred = utc(_event(db, event.id)["occurred_at"])
    _rewrite_scopes(db, event.id, {first: occurred + timedelta(days=10), second: occurred + timedelta(days=20)})
    neighbour = _historical(db, USER_B)
    before = _event(db, event.id)
    world = _world(db, without={event.id})
    clock.set(occurred + timedelta(days=10))  # égalité : la première finalité seulement
    assert expire(db, event.id) == PRUNED
    after = _event(db, event.id)
    assert after["retention_scopes"] == {"version": "retention-scopes-v1", "scopes": [
        s for s in before["retention_scopes"]["scopes"] if s["campaign_id"] != str(first)]}
    assert (utc(after["next_scope_expires_at"]), utc(after["purge_after_at"]), utc(after["updated_at"])) == (
        occurred + timedelta(days=20), utc(before["purge_after_at"]), clock.now)  # avance, jamais d'allongement
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"retention_scopes", "next_scope_expires_at", "updated_at", "xmin"}
    assert _world(db, without={event.id}) == world  # lignes étrangères intactes, xmin compris
    assert expire(db, event.id) == NOT_DUE and _event(db, event.id) == after  # rien à retirer
    # Révision validée par la règle existante : ni finalité ajoutée, ni prolongation.
    retention.validate_scope_revision(before["retention_scopes"], after["retention_scopes"])
    clock.set(occurred + timedelta(days=20) + MICRO)
    assert expire(db, event.id) == PRUNED
    last = _event(db, event.id)
    assert [s["campaign_id"] for s in last["retention_scopes"]["scopes"]] == [str(third)]
    assert utc(last["next_scope_expires_at"]) == utc(last["purge_after_at"]) == occurred + DAYS_90
    clock.set(occurred + DAYS_90)
    assert expire(db, event.id) == PURGED and _event(db, event.id) is None
    assert _event(db, neighbour.id) is not None


def test_a_historical_event_whose_scopes_all_expired_is_purged_at_once(db, clock):
    event = _historical(db, campaigns=2)
    occurred = utc(_event(db, event.id)["occurred_at"])
    _shorten(db, event.id, event.campaign_ids[0], occurred + timedelta(days=5))
    clock.set(occurred + DAYS_90)
    assert expire(db, event.id) == PURGED and _event(db, event.id) is None


def test_a_certified_event_is_purged_with_its_attribution_once_its_proof_is_already_gone(db, clock):
    proof = _certified_event(db, clock)
    other = _certified_event(db, clock, USER_B)
    clock.set(utc(proof_row(db, proof.proof_id)["purge_after_at"]))
    assert committed(db, lambda s: pl.purge_resolved_proof(s, proof_id=proof.proof_id)).outcome == pl.PURGED
    expiry = _expiry(db, proof.id)
    world = _world(db, without={proof.id})
    clock.set(expiry - MICRO)
    assert expire(db, proof.id) == NOT_DUE
    clock.set(expiry)
    assert expire(db, proof.id) == PURGED
    assert _event(db, proof.id) is None and _attribution(db, proof.id) is None
    assert _world(db, without={proof.id}) == world
    assert _event(db, other.id) is not None and _attribution(db, other.id) is not None


def _other_proofs(db, clock, user=USER_A, *, campaign_id):
    """Preuves pending, certified, rejected, indeterminate de la même campagne
    (opérations distinctes) : jamais supprimées par ce mécanisme."""
    from tests.test_activity_proof_lifecycle import _indeterminate, _pending, _rejected
    pending = _pending(db, user, campaign_id=campaign_id)
    certified = _certified(db, user, campaign_id=campaign_id)
    indeterminate = _indeterminate(db, clock, user, campaign_id=campaign_id)
    rejected = _rejected(db, clock, USER_B)
    return [p.proof_id for p in (pending, certified, indeterminate, rejected)]


def test_a_shortened_certified_event_is_purged_with_its_attribution_and_its_still_recorded_proof(db, clock):
    proof = _certified_event(db, clock)
    others = _other_proofs(db, clock, campaign_id=proof.campaign_id)
    recorded = proof_row(db, proof.proof_id)
    occurred = utc(_event(db, proof.id)["occurred_at"])
    _shorten(db, proof.id, proof.campaign_id, occurred + timedelta(hours=1))  # raccourcie : < 168 h de la preuve
    clock.set(occurred + timedelta(hours=1))
    assert clock.now < utc(recorded["purge_after_at"])  # la preuve est encore dans ses 168 heures
    world = _world(db, without={proof.id, proof.proof_id})
    assert expire(db, proof.id) == PURGED
    assert (_event(db, proof.id), _attribution(db, proof.id), proof_row(db, proof.proof_id)) == (None, None, None)
    assert _world(db, without={proof.id, proof.proof_id}) == world
    assert all(proof_row(db, other) is not None for other in others)  # aucun autre état de preuve touché


def test_block_c_rereads_a_shortened_certified_event_as_incoherent_without_any_repair(db, clock):
    """Limite L8 (documentée) : la relecture historique du bloc C exige la
    finalité nominale ; un raccourcissement (future révision) la rend
    record_incoherent. Le bloc C n'est pas modifié ; la PR 2 applique
    l'échéance raccourcie."""
    proof = _certified_event(db, clock)
    occurred = utc(_event(db, proof.id)["occurred_at"])
    _shorten(db, proof.id, proof.campaign_id, occurred + timedelta(days=30))
    observed = committed(db, lambda s: fz.observe_proof(s, proof_id=proof.proof_id))
    assert (observed.state, observed.reason) == (fz.RECORDED, fz.RECORD_INCOHERENT)
    assert inspect_one(db, proof.id) == lb.EventOutcome(lb.VALID)  # structurellement valide pour la PR 2
    clock.set(occurred + timedelta(days=30))
    assert expire(db, proof.id) == PURGED


def _second_scope(db, event_id, other):
    """Écrivain hors service : ajoute à un événement la finalité d'une autre
    campagne active de l'utilisateur (`other`, ouverte avant l'événement :
    sa fenêtre couvre occurred_at)."""
    event = _event(db, event_id)
    occurred = utc(event["occurred_at"])
    scopes = list(retention.parse_retention_scopes(event["retention_scopes"])) + [
        retention.RetentionScope("campaign", other, "v0-retention-v1", occurred + DAYS_90)]
    first, last = retention.retention_bounds(scopes)
    _set(db, event_id, retention_scopes=retention.build_retention_document(scopes), next_scope_expires_at=first,
         purge_after_at=last)
    return other


def _insert_event(db, **overrides):
    """Écrivain hors service : événement inséré sans passer par les services."""
    now = common.utcnow()
    campaign_id = overrides.pop("campaign_id")
    values = {"id": uuid.uuid4(), "user_id": USER_A, "visit_id": uuid.uuid4(), "idempotency_key": uuid.uuid4(),
              "event_type": "portfolio_used", "surface": "portfolio", "executed_route": "f2",
              "source": "server_business", "source_operation_kind": "portfolio_position_create",
              "source_operation_id": uuid.uuid4(), "outcome": "succeeded", "delivery_status": "not_applicable",
              "occurred_at": now, "recorded_at": now, "updated_at": now, "resolved_at": now, "delivered_at": None,
              "schema_version": "product-activity-event-v1"}
    values.update(overrides)
    scope = retention.RetentionScope("campaign", campaign_id, "v0-retention-v1", values["occurred_at"] + DAYS_90)
    values.setdefault("retention_scopes", retention.build_retention_document([scope]))
    values.setdefault("next_scope_expires_at", scope.expires_at)
    values.setdefault("purge_after_at", scope.expires_at)
    with db.begin() as conn:
        conn.execute(sa.insert(ProductActivityEvent.__table__).values(**values))
    return values["id"]


def test_out_of_contract_certified_events_are_never_touched_and_are_reported(db, clock):
    other = open_wave(db, USER_A)
    proof = _certified_event(db, clock)
    _second_scope(db, proof.id, other)  # certifié à deux finalités, rattachées
    orphan = _insert_event(db, campaign_id=proof.campaign_id)  # nature canonique sans attribution
    shape = _insert_event(db, campaign_id=proof.campaign_id, executed_route="legacy")  # forme non certifiée
    clock.set(utc(_event(db, proof.id)["purge_after_at"]) + timedelta(days=1))
    world = _world(db)
    for event_id in (proof.id, orphan, shape):
        assert expire(db, event_id) == lb.EventOutcome(lb.CONFLICT, el.CLASSIFICATION_INCOHERENT)
        assert inspect_one(db, event_id) == lb.EventOutcome(lb.INCONSISTENT, el.CLASSIFICATION_INCOHERENT)
    assert _world(db) == world  # aucune écriture, aucune réparation
    # Défense en profondeur : même si le contrôle de contrat laissait passer un
    # certifié à deux finalités dont une seule est échue, aucun retrait partiel.
    occurred = utc(_event(db, proof.id)["occurred_at"])
    _shorten(db, proof.id, proof.campaign_id, occurred + timedelta(days=1))
    clock.set(occurred + timedelta(days=1))
    world = _world(db)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(el, "_contract_problem", lambda db, locked: None)
        assert expire(db, proof.id) == lb.EventOutcome(lb.CONFLICT, el.CLASSIFICATION_INCOHERENT)
    assert _world(db) == world


def test_a_historical_event_with_an_attribution_is_out_of_contract(db, clock):
    """Impossible sous les FK et CHECK de 0024 : simulé dans UNE transaction de
    test annulée (contraintes retirées), au niveau du service."""
    proof = _certified_event(db, clock)
    historical = _historical(db)
    attribution = _attribution(db, proof.id)
    clock.set(_expiry(db, historical.id))  # avant les verrous ACCESS EXCLUSIVE de la transaction de test
    with Session(bind=db, autoflush=False) as s:
        for constraint in ("product_activity_event_attributions_event_fkey",
                           "ck_product_activity_event_attributions_operation",
                           "product_activity_proof_registry_attribution_fkey"):
            table = REGISTRY if "registry" in constraint else ATTRIBUTIONS
            s.execute(sa.text(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}"))
        s.execute(sa.text(f"UPDATE {ATTRIBUTIONS} SET event_id = :h, source_operation_kind = 'portfolio_write' "
                          "WHERE event_id = :e"), {"h": historical.id, "e": attribution["event_id"]})
        assert el.expire_event_scopes(s, event_id=historical.id) == el.EventDecision(
            el.CONFLICT, el.CLASSIFICATION_INCOHERENT)
        assert el.inspect_event_integrity(s, event_id=historical.id) == el.IntegrityDecision(
            el.INCONSISTENT, el.CLASSIFICATION_INCOHERENT)
        # L'événement certifié a perdu son attribution : hors contrat lui aussi.
        assert el.inspect_event_integrity(s, event_id=proof.id).outcome == el.INCONSISTENT
        s.rollback()
    assert _attribution(db, proof.id) == attribution


def test_a_residual_proof_that_does_not_match_is_never_deleted(db, clock):
    proof = _certified_event(db, clock)
    occurred = utc(_event(db, proof.id)["occurred_at"])
    _shorten(db, proof.id, proof.campaign_id, occurred + timedelta(hours=1))
    with db.begin() as conn:  # hors service : résolution de la preuve décalée (CHECK 168 h respecté)
        conn.execute(sa.text(f"UPDATE {REGISTRY} SET resolved_at = resolved_at + interval '1 second', "
                             "purge_after_at = purge_after_at + interval '1 second' WHERE id = :i"),
                     {"i": proof.proof_id})
    clock.set(occurred + timedelta(hours=1))
    world = _world(db)
    assert expire(db, proof.id) == lb.EventOutcome(lb.CONFLICT, el.PROOF_INCOHERENT)
    assert inspect_one(db, proof.id) == lb.EventOutcome(lb.INCONSISTENT, el.PROOF_INCOHERENT)
    assert _world(db) == world


# --------------------------------------------------------------------------
# D. Suppressions atomiques
# --------------------------------------------------------------------------

def _shortened_certified(db, clock):
    proof = _certified_event(db, clock)
    occurred = utc(_event(db, proof.id)["occurred_at"])
    _shorten(db, proof.id, proof.campaign_id, occurred + timedelta(hours=1))
    clock.set(occurred + timedelta(hours=1))
    return proof


def _fail_after(engine, prefix):
    """Panne APRÈS l'exécution réelle de l'instruction (SQL exécuté)."""
    seen = []

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(prefix) and not seen:
            seen.append(prefix)
            raise RuntimeError("panne injectée")
    sa.event.listen(engine, "after_cursor_execute", fail)
    return seen, lambda: sa.event.remove(engine, "after_cursor_execute", fail)


@pytest.mark.parametrize("prefix", [f"DELETE FROM {REGISTRY}", f"DELETE FROM {ATTRIBUTIONS}",
                                    f"DELETE FROM {EVENTS}", "RELEASE SAVEPOINT"])
def test_a_failure_after_any_delete_leaves_no_partial_state_and_the_rerun_is_clean(db, clock, prefix):
    proof = _shortened_certified(db, clock)
    world = _world(db)
    seen, remove = _fail_after(db, prefix)
    try:
        assert expire(db, proof.id) == lb.EventOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)
    finally:
        remove()
    assert seen == [prefix] and _world(db) == world  # tout ou rien
    assert expire(db, proof.id) == PURGED


@pytest.mark.parametrize("prefix", [f"UPDATE {EVENTS}", "RELEASE SAVEPOINT"])
def test_a_failure_during_a_partial_removal_writes_nothing(db, clock, prefix):
    event = _historical(db, campaigns=2)
    occurred = utc(_event(db, event.id)["occurred_at"])
    _shorten(db, event.id, event.campaign_ids[0], occurred + timedelta(days=1))
    clock.set(occurred + timedelta(days=1))
    world = _world(db)
    seen, remove = _fail_after(db, prefix)
    try:
        assert expire(db, event.id).outcome == lb.UNKNOWN
    finally:
        remove()
    assert seen == [prefix] and _world(db) == world
    assert expire(db, event.id) == PRUNED


@pytest.mark.parametrize("table", [REGISTRY, ATTRIBUTIONS, EVENTS])
def test_an_unexpected_row_count_rolls_the_whole_savepoint_back(db, clock, table):
    proof = _shortened_certified(db, clock)
    world = _world(db)
    real = el._exactly_one

    def miscount(session, statement):
        if statement.table.name == table:
            session.execute(statement.execution_options(synchronize_session=False))  # SQL réellement exécuté
            raise el._Unconfirmed()  # nombre de lignes inattendu
        return real(session, statement)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(el, "_exactly_one", miscount)
        assert expire(db, proof.id) == lb.EventOutcome(lb.CONFLICT, el.WRITE_UNCONFIRMED)
    assert _world(db) == world
    assert expire(db, proof.id) == PURGED


def test_a_conditional_write_that_matches_nothing_is_a_conflict(db, clock):
    event = _historical(db, campaigns=2)
    occurred = utc(_event(db, event.id)["occurred_at"])
    _shorten(db, event.id, event.campaign_ids[0], occurred + timedelta(days=1))
    clock.set(occurred + timedelta(days=1))
    world = _world(db)
    real = el._same_event
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(el, "_same_event", lambda event: real(event) + (ProductActivityEvent.id != event.id,))
        assert expire(db, event.id) == lb.EventOutcome(lb.CONFLICT, el.WRITE_UNCONFIRMED)  # UPDATE : 0 ligne
        clock.set(occurred + DAYS_90)
        assert expire(db, event.id) == lb.EventOutcome(lb.CONFLICT, el.WRITE_UNCONFIRMED)  # DELETE : 0 ligne
    assert _world(db) == world


def test_the_foreign_keys_are_unchanged_and_the_delete_order_is_theirs(db, clock):
    constraints = rows(db, "SELECT conrelid::regclass::text AS t, conname, contype, confdeltype, condeferrable, "
                           "pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conrelid::regclass::text IN "
                           f"('{REGISTRY}', '{ATTRIBUTIONS}', '{EVENTS}') ORDER BY 1, 2")
    assert {(c["t"], c["confdeltype"], c["condeferrable"]) for c in constraints if c["contype"] == "f"} == {
        (EVENTS, "a", False), (ATTRIBUTIONS, "a", False), (REGISTRY, "a", False)}  # NO ACTION, ni CASCADE ni SET NULL
    proof = _shortened_certified(db, clock)
    seen, remove = _statements(db)
    try:
        assert expire(db, proof.id) == PURGED
    finally:
        remove()
    deletes = [s.split(" WHERE")[0] for s in seen if s.startswith("DELETE")]
    assert deletes == [f"DELETE FROM {REGISTRY}", f"DELETE FROM {ATTRIBUTIONS}", f"DELETE FROM {EVENTS}"]
    assert [s for s in seen if "SAVEPOINT" in s][:1] and len([s for s in seen if s.startswith("SAVEPOINT")]) == 1
    assert not [s for s in seen if s.startswith(("UPDATE", "INSERT"))]
    assert rows(db, "SELECT conrelid::regclass::text AS t, conname, contype, confdeltype, condeferrable, "
                    "pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conrelid::regclass::text IN "
                    f"('{REGISTRY}', '{ATTRIBUTIONS}', '{EVENTS}') ORDER BY 1, 2") == constraints


# --------------------------------------------------------------------------
# E. Concurrence (PostgreSQL réel)
# --------------------------------------------------------------------------

def _race(db, event_id, contenders=2):
    """Plusieurs expirations simultanées du même événement, libérées ensemble."""
    holder = _hold_keys(db, event_id)
    actors = [Background(lambda: expire(db, event_id)) for _ in range(contenders)]
    for actor in actors:
        actor.start()
    try:
        _wait(lambda: waiting(db) == contenders, "les expirations n'attendent pas les clés")
    finally:
        holder.rollback()
    return sorted(actor.outcome().outcome for actor in actors)


def test_two_simultaneous_purges_delete_once_and_the_loser_claims_nothing(db, clock):
    proof = _shortened_certified(db, clock)
    assert _race(db, proof.id) == [lb.ABSENT, lb.PURGED]
    event = _historical(db)
    clock.set(_expiry(db, event.id))
    assert _race(db, event.id, contenders=3) == [lb.ABSENT, lb.ABSENT, lb.PURGED]


def test_two_simultaneous_partial_removals_remove_once(db, clock):
    event = _historical(db, campaigns=2)
    occurred = utc(_event(db, event.id)["occurred_at"])
    _shorten(db, event.id, event.campaign_ids[0], occurred + timedelta(days=1))
    clock.set(occurred + timedelta(days=1))
    assert _race(db, event.id) == [lb.NOT_DUE, lb.PRUNED]
    assert len(_event(db, event.id)["retention_scopes"]["scopes"]) == 1


def _in_holder(db, action):
    """Transaction tierce NON committée qui a exécuté `action` (verrous tenus)."""
    return Holder(db, action=action)


def _contend(db, holder, action, *, commit=True, waits=True):
    contender = Background(action)
    contender.start()
    try:
        if waits:
            _wait(lambda: waiting(db) == 1, "le concurrent n'attend pas")
    finally:
        holder.commit() if commit else holder.rollback()
    return contender.outcome()


def test_a_partial_removal_and_a_purge_are_serialized_both_ways(db, clock):
    event = _historical(db, campaigns=2)
    occurred = utc(_event(db, event.id)["occurred_at"])
    _shorten(db, event.id, event.campaign_ids[0], occurred + timedelta(days=1))
    clock.set(occurred + timedelta(days=1))
    holder = _in_holder(db, lambda s: el.expire_event_scopes(s, event_id=event.id))
    assert holder.value.outcome == el.PRUNED
    clock.set(occurred + DAYS_90)
    assert _contend(db, holder, lambda: expire(db, event.id)) == PURGED  # relit le document révisé
    other = _historical(db, campaigns=2)
    occurred = utc(_event(db, other.id)["occurred_at"])
    _shorten(db, other.id, other.campaign_ids[0], occurred + timedelta(days=1))
    clock.set(occurred + DAYS_90)
    holder = _in_holder(db, lambda s: el.expire_event_scopes(s, event_id=other.id))
    assert holder.value.outcome == el.PURGED
    clock.set(occurred + timedelta(days=1))
    assert _contend(db, holder, lambda: expire(db, other.id)) == ABSENT


@pytest.mark.parametrize("block_c", ["observe", "finalize_retry"])
def test_a_purge_and_a_block_c_reread_are_serialized_both_ways(db, clock, block_c):
    service = fz.observe_proof if block_c == "observe" else fz.finalize_certified_proof
    proof = _shortened_certified(db, clock)
    holder = _in_holder(db, lambda s: service(s, proof_id=proof.proof_id))
    assert holder.value.state == fz.RECORDED
    assert _contend(db, holder, lambda: expire(db, proof.id)) == PURGED
    other = _shortened_certified(db, clock)
    holder = _in_holder(db, lambda s: el.expire_event_scopes(s, event_id=other.id))
    assert holder.value.outcome == el.PURGED
    reread = _contend(db, holder, lambda: committed(db, lambda s: service(s, proof_id=other.proof_id)))
    assert reread == fz.FinalizationDecision(fz.ABSENT)  # rien n'est recréé
    assert _event(db, other.id) is None and rows(db, f"SELECT count(*) AS n FROM {EVENTS}")[0]["n"] == 0


def test_a_purge_and_the_nominal_proof_purge_are_serialized_both_ways(db, clock):
    proof = _certified_event(db, clock)
    _shorten(db, proof.id, proof.campaign_id, utc(_event(db, proof.id)["occurred_at"]) + timedelta(days=8))
    clock.set(utc(proof_row(db, proof.proof_id)["purge_after_at"]) + timedelta(days=1))  # les deux échues
    holder = _in_holder(db, lambda s: pl.purge_resolved_proof(s, proof_id=proof.proof_id))
    assert holder.value.outcome == pl.PURGED
    assert _contend(db, holder, lambda: expire(db, proof.id)) == PURGED  # preuve disparue : attribution + événement
    other = _certified_event(db, clock)
    _shorten(db, other.id, other.campaign_id, utc(_event(db, other.id)["occurred_at"]) + timedelta(days=8))
    clock.set(utc(proof_row(db, other.proof_id)["purge_after_at"]) + timedelta(days=1))
    holder = _in_holder(db, lambda s: el.expire_event_scopes(s, event_id=other.id))
    assert holder.value.outcome == el.PURGED
    assert _contend(db, holder, lambda: committed(db, lambda s: pl.purge_resolved_proof(
        s, proof_id=other.proof_id))) == pl.PurgeDecision(pl.ABSENT)


def test_a_purge_and_a_block_b_transition_on_the_same_proof_key_are_serialized_both_ways(db, clock):
    from tests.activity_proof_support import settle
    proof = _shortened_certified(db, clock)
    holder = _in_holder(db, lambda s: proofs.settle(s, origin=proof.origin, operation=proof.op,
                                                     proof_id=proof.proof_id))
    assert holder.value.state == proofs.RECORDED and not holder.value.written
    assert _contend(db, holder, lambda: expire(db, proof.id)) == PURGED
    other = _shortened_certified(db, clock)
    holder = _in_holder(db, lambda s: el.expire_event_scopes(s, event_id=other.id))
    decision = _contend(db, holder, lambda: settle(db, other.origin, other.op, other.proof_id))
    assert (decision.state, decision.reason, decision.written) == (proofs.ABSENT, proofs.TECHNICAL_UNCERTAINTY,
                                                                   False)
    # Une certification B d'une AUTRE opération du même utilisateur n'a aucun contact.
    third = _shortened_certified(db, clock)
    from tests.activity_proof_support import opened, register
    origin, op = opened(db, USER_A, campaign_id=third.campaign_id)
    pending = register(db, origin, op)
    holder = _in_holder(db, lambda s: proofs.certify_pending(s, origin=origin, operation=op,
                                                              proof_id=pending.proof_id))
    try:
        assert expire(db, third.id) == PURGED  # jamais en attente
    finally:
        holder.commit()
    assert proof_row(db, pending.proof_id)["status"] == proofs.CERTIFIED


def test_a_purge_and_a_historical_2a_write_are_serialized_and_nothing_is_recreated(db, clock):
    event = _historical(db)
    clock.set(_expiry(db, event.id))

    def repeat(s):
        try:
            pe.record_event(s, **event.kwargs)
        except common.EventRetentionExpired:
            return "expired"  # répétition d'un événement échu : refus sans écriture, clés tenues
    holder = _in_holder(db, repeat)
    assert holder.value == "expired"
    assert _contend(db, holder, lambda: expire(db, event.id)) == PURGED
    other = _historical(db, USER_B)
    clock.set(_expiry(db, other.id))
    holder = _in_holder(db, lambda s: el.expire_event_scopes(s, event_id=other.id))

    def replay():
        try:
            committed(db, lambda s: pe.record_event(s, **other.kwargs))
        except common.EventNotAuthorized:
            return "refused"
    assert _contend(db, holder, replay) == "refused"  # la clé libérée n'ouvre aucune recréation
    assert _event(db, other.id) is None and rows(db, f"SELECT count(*) AS n FROM {EVENTS}")[0]["n"] == 0


@pytest.mark.parametrize("transition", ["withdraw", "close"])
def test_a_withdrawal_or_a_campaign_closure_and_a_purge_are_serialized_both_ways(db, clock, transition):
    def act(campaign_id, user):
        if transition == "withdraw":
            return lambda s: pp.withdraw_participant(s, campaign_id=campaign_id, user_id=user)
        return lambda s: pc.close_campaign(s, campaign_id=campaign_id)
    event = _historical(db)
    clock.set(_expiry(db, event.id) - timedelta(days=60))  # clôture et retrait encore possibles
    _shorten(db, event.id, event.campaign_ids[0], clock.now)
    holder = _in_holder(db, act(event.campaign_ids[0], USER_A))
    assert _contend(db, holder, lambda: expire(db, event.id)) == PURGED  # aucune échéance avancée par le statut
    other = _historical(db, USER_B)
    _shorten(db, other.id, other.campaign_ids[0], clock.now)
    holder = _in_holder(db, lambda s: el.expire_event_scopes(s, event_id=other.id))
    assert holder.value.outcome == el.PURGED
    _contend(db, holder, lambda: committed(db, act(other.campaign_ids[0], USER_B)))


def _hook_once(monkeypatch, target, name, action):
    real, done = getattr(target, name), []

    def hooked(*args, **kwargs):
        if not done:
            done.append(1)
            action()
        return real(*args, **kwargs)
    monkeypatch.setattr(target, name, hooked)
    return done


def test_a_proof_that_disappears_between_the_indicative_read_and_the_locks_is_not_a_conflict(db, clock, monkeypatch):
    proof = _shortened_certified(db, clock)

    def outside_delete():  # écrivain hors service, entre L0 et l'acquisition des clés
        with db.begin() as conn:
            conn.execute(sa.text(f"DELETE FROM {REGISTRY} WHERE id = :i"), {"i": proof.proof_id})
    done = _hook_once(monkeypatch, common, "lock_identities", outside_delete)
    assert expire(db, proof.id) == PURGED and done == [1]
    assert (_event(db, proof.id), _attribution(db, proof.id)) == (None, None)


def _restore_proof(db, row):
    values = {k: v for k, v in row.items() if k != "xmin"}
    with db.begin() as conn:
        conn.execute(sa.insert(ProductActivityProof.__table__).values(**values))


def _after_purged_proof(db, clock):
    """Événement certifié raccourci dont la preuve a déjà été purgée ; la ligne
    de preuve est rendue pour qu'un écrivain hors service la réinsère."""
    proof = _certified_event(db, clock)
    row = proof_row(db, proof.proof_id)
    clock.set(utc(row["purge_after_at"]))
    assert committed(db, lambda s: pl.purge_resolved_proof(s, proof_id=proof.proof_id)).outcome == pl.PURGED
    _shorten(db, proof.id, proof.campaign_id, clock.now)
    return proof, row


def test_a_dependency_that_appears_after_the_reread_aborts_the_treatment(db, clock):
    proof, row = _after_purged_proof(db, clock)
    fired = []

    def inject(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().startswith("SELECT") and "FROM product_campaign_participants" in \
                statement and "FOR SHARE" in statement:
            fired.append(1)
            _restore_proof(db, row)  # écrivain hors service, après L1, avant les verrous de lignes
    sa.event.listen(db, "before_cursor_execute", inject)
    seen, remove = _statements(db)
    try:
        outcome = expire(db, proof.id)
    finally:
        sa.event.remove(db, "before_cursor_execute", inject)
        remove()
    assert fired == [1] and outcome == lb.EventOutcome(lb.CONFLICT, el.DEPENDENCY_CHANGED)
    # Abandon décidé par la relecture L2, AVANT toute écriture (la FK n'est que la défense finale).
    assert not [s for s in seen if s.startswith(("DELETE", "UPDATE", "SAVEPOINT"))]
    world = _world(db)
    assert proof_row(db, proof.proof_id) is not None and _event(db, proof.id) is not None
    assert _world(db) == world
    assert expire(db, proof.id) == PURGED  # réexécution : la dépendance est vue dès L0, supprimée avec l'événement
    assert proof_row(db, proof.proof_id) is None


def test_a_key_that_was_not_acquired_aborts_without_any_late_advisory_lock(db, clock, monkeypatch):
    event = _historical(db)
    clock.set(_expiry(db, event.id))
    real, calls = el._required_keys, []

    def extra(*args):
        calls.append(1)
        keys = real(*args)
        return keys | {12345} if len(calls) == 2 else keys  # L1 exige une clé non détenue
    monkeypatch.setattr(el, "_required_keys", extra)
    seen, remove = _statements(db)
    try:
        assert expire(db, event.id) == lb.EventOutcome(lb.CONFLICT, el.DEPENDENCY_CHANGED)
    finally:
        remove()
    locks = [i for i, s in enumerate(seen) if "pg_advisory_xact_lock" in s]
    assert len(locks) == len(_keys(db, event.id)) and locks == list(range(locks[0], locks[0] + len(locks)))
    assert not [s for s in seen[locks[-1]:] if "FOR SHARE" in s or "FOR UPDATE" in s]  # abandon avant tout verrou
    assert _event(db, event.id) is not None


def test_the_lock_protocol_is_one_sorted_advisory_batch_then_rows_in_rank_order(db, clock, monkeypatch):
    other = open_wave(db, USER_A)
    proof = _shortened_certified(db, clock)
    spy = []
    real = common.lock_identities
    monkeypatch.setattr(common, "lock_identities", lambda s, *keys: (spy.append(keys), real(s, *keys))[1])
    seen, remove = _statements(db)
    try:
        assert expire(db, proof.id) == PURGED
    finally:
        remove()
    assert len(spy) == 1 and set(spy[0]) == set(_keys_from(proof)) and len(spy[0]) == 4  # UN appel, 4 clés
    assert pl.proof_lock_key(SimpleNamespace(user_id=USER_A, source_operation_kind=proof.op.operation_kind,
                                             source_operation_id=proof.op.operation_id)) in spy[0]
    locks = [s for s in seen if "pg_advisory_xact_lock" in s]
    assert len(locks) == 4
    ranks = [("pg_advisory_xact_lock", None), ("FROM product_campaign_participants", "FOR SHARE"),
             ("FROM product_campaigns", "FOR SHARE"), (f"FROM {INTERACTIONS}", "FOR SHARE"),
             (f"FROM {REGISTRY}", "FOR UPDATE"),
             (f"FROM {ATTRIBUTIONS}", "FOR UPDATE"), (f"FROM {EVENTS}", "FOR UPDATE")]
    positions = []
    for marker, mode in ranks:
        found = [i for i, s in enumerate(seen) if marker in s and (mode is None or s.endswith(mode))]
        assert found, marker
        positions.append((found[0], found[-1]))
    assert all(positions[i][1] < positions[i + 1][0] for i in range(len(positions) - 1)), positions
    assert not [s for s in seen if "product_activity_sessions" in s or "users" in s.split(" WHERE")[0]]
    del other


def _keys_from(proof):
    return {*fz.lock_keys(SimpleNamespace(id=proof.proof_id, user_id=USER_A, event_type="portfolio_used",
                                          visit_id=proof.origin.visit_id, source_operation_kind=proof.op.operation_kind,
                                          source_operation_id=proof.op.operation_id))}


def test_an_outside_sql_writer_changing_the_document_is_seen_after_the_row_lock(db, clock):
    other = open_wave(db, USER_A)
    committed(db, lambda s: pp.withdraw_participant(s, campaign_id=other, user_id=USER_A))  # hors de l'événement
    event = _historical(db)
    clock.set(_expiry(db, event.id))
    occurred = utc(_event(db, event.id)["occurred_at"])
    scopes = list(retention.parse_retention_scopes(_event(db, event.id)["retention_scopes"])) + [
        retention.RetentionScope("campaign", other, "v0-retention-v1", occurred + DAYS_90)]
    first, last = retention.retention_bounds(scopes)
    holder = Holder(db, sql=f"UPDATE {EVENTS} SET retention_scopes = CAST(:d AS jsonb), next_scope_expires_at = :f, "
                            "purge_after_at = :l WHERE id = :i",
                    d=json.dumps(retention.build_retention_document(scopes)), f=first, l=last, i=event.id)
    outcome = _contend(db, holder, lambda: expire(db, event.id))  # L1 : ancien document ; L2 : nouveau
    assert outcome == lb.EventOutcome(lb.CONFLICT, el.DEPENDENCY_CHANGED)  # campagne non verrouillée à son rang
    assert len(_event(db, event.id)["retention_scopes"]["scopes"]) == 2


def test_the_implicit_foreign_key_locks_of_an_outside_writer_are_respected_both_ways(db, clock):
    proof, row = _after_purged_proof(db, clock)
    values = {k: v for k, v in row.items() if k != "xmin"}
    insert = sa.insert(ProductActivityProof.__table__).values(**values)
    # 1. Écrivain hors service : preuve référençante NON committée (FOR KEY SHARE implicite sur l'attribution).
    writer = Holder(db)
    writer.db.execute(insert)
    seen, remove = _statements(db)
    try:
        assert _contend(db, writer, lambda: expire(db, proof.id)) == lb.EventOutcome(lb.CONFLICT,
                                                                                      el.DEPENDENCY_CHANGED)
    finally:
        remove()
    assert not [s for s in seen if s.startswith(("DELETE", "SAVEPOINT"))]  # vue en L2 : aucune écriture tentée
    with db.begin() as conn:
        conn.execute(sa.text(f"DELETE FROM {REGISTRY} WHERE id = :i"), {"i": proof.proof_id})
    writer = Holder(db)
    writer.db.execute(insert)
    assert _contend(db, writer, lambda: expire(db, proof.id), commit=False) == PURGED  # annulé : suppression
    # 2. Inverse : la purge tient l'attribution ; l'INSERT référençant attend puis viole la FK.
    other, other_row = _after_purged_proof(db, clock)
    holder = _in_holder(db, lambda s: el.expire_event_scopes(s, event_id=other.id))
    assert holder.value.outcome == el.PURGED

    def outside_insert():
        try:
            with db.begin() as conn:
                conn.execute(sa.insert(ProductActivityProof.__table__).values(
                    **{k: v for k, v in other_row.items() if k != "xmin"}))
        except sa.exc.IntegrityError as exc:
            return exc.orig.diag.constraint_name
    assert _contend(db, holder, outside_insert) == "product_activity_proof_registry_attribution_fkey"
    assert proof_row(db, other.proof_id) is None and _event(db, other.id) is None


def test_a_lock_timeout_writes_nothing_and_the_explicit_rerun_is_clean(db, clock):
    event = _historical(db)
    clock.set(_expiry(db, event.id))
    row = _event(db, event.id)
    holder = _hold_keys(db, event.id)
    try:
        outcome = expire(db, event.id)  # lock_timeout 2 s de la frontière
    finally:
        holder.rollback()
    assert outcome == lb.EventOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY) and _event(db, event.id) == row
    row_holder = Holder(db, sql=f"SELECT 1 FROM {EVENTS} WHERE id = :i FOR UPDATE", i=event.id)
    try:
        assert expire(db, event.id).outcome == lb.UNKNOWN  # attente de la ligne : idem
    finally:
        row_holder.rollback()
    assert expire(db, event.id) == PURGED


def _deadlock_count(engine):
    return rows(engine, "SELECT deadlocks AS n FROM pg_stat_database WHERE datname = current_database()")[0]["n"]


def _row_lock_waits(engine) -> int:
    return rows(engine, "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = :a AND "
                        "wait_event_type = 'Lock' AND wait_event IN ('transactionid', 'tuple')", a=PROOF_APP)[0]["n"]


def test_a_real_deadlock_caused_by_an_outside_writer_is_detected_and_nothing_is_wrongly_claimed(engine, monkeypatch):
    """Écrivain hors service : LIGNE d'événement puis clé consultative (ordre
    inverse) ; PostgreSQL détecte l'interblocage ; unknown sans écriture, ou
    purged si l'écrivain est la victime ; jamais un succès fictif."""
    from tests.test_activity_event_finalization import CLEANUP
    try:
        event = _real_clock_event(engine, monkeypatch, due_in=-timedelta(seconds=1))
        key = _keys(engine, event.id)[0]
        before = _deadlock_count(engine)
        writer = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            writer.execute(sa.text(f"SELECT 1 FROM {EVENTS} WHERE id = :i FOR UPDATE"), {"i": event.id})
            contender = Background(lambda: expire(engine, event.id))
            contender.start()
            _wait(lambda: _row_lock_waits(engine) == 1, "l'expiration n'attend pas la ligne")
            try:
                writer.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})  # cycle
                writer.commit()
            except sa.exc.OperationalError:
                writer.rollback()
            outcome = contender.outcome()
        finally:
            writer.close()
        assert _deadlock_count(engine) == before + 1
        present = _event(engine, event.id) is not None
        assert (outcome.outcome, present) in ((lb.UNKNOWN, True), (lb.PURGED, False)), (outcome, present)
    finally:
        with engine.begin() as conn:
            for table in CLEANUP:
                conn.execute(sa.text(f"DELETE FROM {table}"))


# --------------------------------------------------------------------------
# F. Propriété transactionnelle et COMMIT incertain
# --------------------------------------------------------------------------

def _xid(caller):
    return caller.execute(sa.text("SELECT txid_current()")).scalar_one()


def _due_event(db, clock):
    event = _historical(db)
    clock.set(_expiry(db, event.id))
    return event, _event(db, event.id)


@pytest.mark.parametrize("kind", ["connection", "session"])
def test_every_entry_point_refuses_a_caller_connection_or_session_without_touching_its_transaction(db, clock, kind):
    event, row = _due_event(db, clock)
    world = _world(db)
    with (db.connect() if kind == "connection" else Session(bind=db)) as caller:
        for open_transaction in (False, True):
            xid = _xid(caller) if open_transaction else None
            for function in ENTRY_POINTS:
                seen, remove = _statements(db)
                try:
                    with pytest.raises(TypeError):
                        _call(function, caller, event.id)
                finally:
                    remove()
                assert seen == [], function.__name__  # aucune instruction, pas même BEGIN
                assert caller.in_transaction() is open_transaction, function.__name__
            if open_transaction:
                assert _xid(caller) == xid  # même transaction, ni committée ni annulée
        caller.rollback()
    assert _event(db, event.id) == row and _world(db) == world
    assert expire(db, event.id) == PURGED  # le moteur : la frontière possède sa transaction


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
    connections = []
    for engine in refused.values():
        if isinstance(engine, sa.Engine):
            sa.event.listen(engine, "connect", lambda *a: connections.append(a))
            sa.event.listen(engine, "before_cursor_execute", lambda *a: connections.append(a))
    for engine in refused.values():
        for function in ENTRY_POINTS:
            with pytest.raises(TypeError):
                _call(function, engine)
    assert connections == []
    for engine in (db, db.execution_options(isolation_level="READ COMMITTED")):
        assert tx._require_engine(engine) is engine
    assert lb._require_engine is tx._require_engine and lb._owned is tx._owned  # garde-fous communs, inchangés


def test_a_driver_level_autocommit_is_detected_in_every_transaction_and_nothing_is_done(db, clock):
    event, row = _due_event(db, clock)
    world = _world(db)
    autocommit = sa.create_engine(db.url, poolclass=sa.pool.NullPool)
    sa.event.listen(autocommit, "connect", lambda dbapi_connection, record: setattr(
        dbapi_connection, "autocommit", True))
    assert tx._require_engine(autocommit) is autocommit  # invisible à l'examen statique
    seen, remove = _statements(autocommit)
    try:
        for function in (lb.expire_event, lb.inspect_event):
            assert function(autocommit, event_id=event.id) == lb.EventOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)
        for page in (lb.expire_events_page, lb.inspect_events_page):
            report = page(autocommit)
            assert (report.selected, report.examined, report.next_cursor) == (False, 0, None), page.__name__
    finally:
        remove()
    work = [s for s in seen if EVENTS in s or "pg_advisory" in s or "SAVEPOINT" in s or s.startswith(
        ("DELETE", "UPDATE", "INSERT"))]
    assert work == []  # ni lecture, ni verrou, ni écriture hors d'un bloc de transaction
    assert _world(db) == world and _event(db, event.id) == row
    autocommit.dispose()
    assert expire(db, event.id) == PURGED


def _lose_commit(engine, statement_prefix, *, really_commit, between=None):
    seen = []

    def mark(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(statement_prefix):
            conn.info["target"] = True

    def lose(conn):
        if conn.info.pop("target", False) and not seen:
            seen.append("lost")
            if really_commit:
                conn.connection.dbapi_connection.commit()
            elif between is not None:
                conn.connection.dbapi_connection.rollback()  # rien de committé par ce traitement
            if between is not None:
                between()
            raise OSError("confirmation du COMMIT analytics perdue")
    sa.event.listen(engine, "before_cursor_execute", mark)
    sa.event.listen(engine, "commit", lose)
    return seen, lambda: (sa.event.remove(engine, "commit", lose),
                          sa.event.remove(engine, "before_cursor_execute", mark))


def _partial(db, clock):
    event = _historical(db, campaigns=2)
    occurred = utc(_event(db, event.id)["occurred_at"])
    _shorten(db, event.id, event.campaign_ids[0], occurred + timedelta(days=1))
    clock.set(occurred + timedelta(days=1))
    return event


@pytest.mark.parametrize("really_commit", [True, False])
def test_an_uncertain_purge_commit_is_never_counted_as_a_purge(db, clock, really_commit):
    proof = _shortened_certified(db, clock)
    world = _world(db)
    seen, remove = _lose_commit(db, f"DELETE FROM {EVENTS}", really_commit=really_commit)
    try:
        report = lb.expire_events_page(db)
    finally:
        remove()
    assert seen == ["lost"] and report.purged == 0 and report.examined == 1
    if really_commit:
        assert report.absent == 1 and _event(db, proof.id) is None  # constaté absent, jamais attribué
        assert expire(db, proof.id) == ABSENT
    else:
        assert report.still_present == 1 and _world(db) == world
        assert expire(db, proof.id) == PURGED  # réexécution explicite sûre


@pytest.mark.parametrize("really_commit", [True, False])
def test_an_uncertain_partial_removal_commit_is_never_counted_as_a_removal(db, clock, really_commit):
    event = _partial(db, clock)
    before = _event(db, event.id)
    seen, remove = _lose_commit(db, f"UPDATE {EVENTS}", really_commit=really_commit)
    try:
        outcome = expire(db, event.id)
    finally:
        remove()
    assert seen == ["lost"]
    if really_commit:
        assert outcome == lb.EventOutcome(lb.UNCONFIRMED, lb.UNCONFIRMED_COMMIT)  # état révisé observé, non compté
        assert len(_event(db, event.id)["retention_scopes"]["scopes"]) == 1
    else:
        assert outcome == lb.EventOutcome(lb.STILL_PRESENT, lb.TECHNICAL_UNCERTAINTY) and _event(db, event.id) == before
        assert expire(db, event.id) == PRUNED


def test_an_identical_concurrent_removal_after_an_uncertain_commit_is_not_claimed(db, clock):
    """Le COMMIT de ce traitement n'a PAS lieu ; une autre exécution, identique,
    retire la même finalité avant la relecture : l'état attendu est observé,
    mais il n'appartient pas à ce traitement : unconfirmed, jamais pruned."""
    event = _partial(db, clock)
    concurrent = []
    seen, remove = _lose_commit(db, f"UPDATE {EVENTS}", really_commit=False,
                                between=lambda: concurrent.append(committed(
                                    db, lambda s: el.expire_event_scopes(s, event_id=event.id)).outcome))
    try:
        report = lb.expire_events_page(db)
    finally:
        remove()
    assert seen == ["lost"] and concurrent == [el.PRUNED]
    assert (report.examined, report.pruned, report.unconfirmed) == (1, 0, 1)


def test_the_reread_of_an_uncertain_commit_waits_for_the_identities_of_the_attempt(db, clock, monkeypatch):
    """La relecture reprend les clés de l'opération TENTÉE (en mémoire) : bien
    que l'événement ait disparu, une transaction encore en cours sur ces
    identités est ATTENDUE avant toute conclusion."""
    event, _ = _due_event(db, clock)
    keys = _keys(db, event.id)
    captured, waited = [], []
    real = el._observe

    def spy(session, event_id, attempt_keys):
        captured.append(tuple(attempt_keys))
        return real(session, event_id, attempt_keys)
    monkeypatch.setattr(el, "_observe", spy)

    def in_flight():  # après le COMMIT réel : une transaction tient encore ces identités
        holder = _hold_keys_by(db, keys)

        def release():
            _wait(lambda: waiting(db) == 1, "la relecture n'attend pas les identités de l'opération")
            waited.append(1)
            holder.rollback()
        Background(release).start()
    seen, remove = _lose_commit(db, f"DELETE FROM {EVENTS}", really_commit=True, between=in_flight)
    try:
        assert expire(db, event.id) == lb.EventOutcome(lb.ABSENT, lb.UNCONFIRMED_COMMIT)
    finally:
        remove()
    assert seen == ["lost"] and captured == [keys] and waited == [1]


def _hold_keys_by(db, keys):
    holder = Holder(db)
    common.lock_identities(holder.db, *keys)
    return holder


def test_an_unreadable_reread_and_a_reread_without_transaction_are_unknown(db, clock):
    event, _ = _due_event(db, clock)
    outside = []
    engine = sa.create_engine(db.url, poolclass=sa.pool.NullPool, connect_args={"application_name": PROOF_APP})
    seen, remove = _lose_commit(engine, f"DELETE FROM {EVENTS}", really_commit=True)

    def connect(dbapi_connection, record):
        if seen:
            dbapi_connection.autocommit = True  # seule la connexion de RELECTURE est en autocommit

    def capture(conn, cursor, statement, parameters, context, executemany):
        if conn.connection.dbapi_connection.autocommit:
            outside.append(" ".join(statement.split()))
    sa.event.listen(engine, "connect", connect)
    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        report = lb.expire_events_page(engine)
    finally:
        remove()
        engine.dispose()
    assert seen == ["lost"] and (report.examined, report.purged, report.absent, report.unknown) == (1, 0, 0, 1)
    assert [s for s in outside if not s.startswith("SET ")] == []  # aucune lecture ni verrou hors bloc
    assert _event(db, event.id) is None  # la suppression a réellement eu lieu, sans être annoncée


def test_every_transaction_of_a_valid_engine_is_an_effective_postgresql_transaction(db, clock, monkeypatch):
    statuses = []
    real = tx._require_effective_transaction

    def spy(session):
        statuses.append(session.connection().connection.dbapi_connection.info.transaction_status)
        return real(session)
    monkeypatch.setattr(tx, "_require_effective_transaction", spy)
    event, _ = _due_event(db, clock)
    report = lb.expire_events_page(db)
    assert (report.examined, report.purged) == (1, 1) and statuses == [tx._INTRANS] * 2  # sélection + traitement
    statuses.clear()
    other, _ = _due_event(db, clock)
    report = lb.inspect_events_page(db)
    assert (report.examined, report.valid) == (1, 1) and statuses == [tx._INTRANS] * 2  # sélection + inspection
    statuses.clear()
    seen, remove = _lose_commit(db, f"DELETE FROM {EVENTS}", really_commit=False)
    try:
        assert expire(db, other.id).outcome == lb.STILL_PRESENT
    finally:
        remove()
    assert seen == ["lost"] and statuses == [tx._INTRANS] * 2  # écriture + relecture, chacune la sienne


def test_a_failure_before_commit_and_an_unreachable_database_are_unknown(db, clock):
    event, row = _due_event(db, clock)
    unreachable = sa.create_engine(db.url.set(port=1), poolclass=sa.pool.NullPool)
    assert lb.expire_event(unreachable, event_id=event.id) == lb.EventOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)
    report = lb.expire_events_page(unreachable)
    assert (report.selected, report.examined, report.next_cursor) == (False, 0, None)
    assert lb.inspect_events_page(unreachable).selected is False
    assert _event(db, event.id) == row


# --------------------------------------------------------------------------
# G. Contrôle d'intégrité indépendant
# --------------------------------------------------------------------------

def _anomalies(db, clock):
    """Une anomalie par motif, écrites hors service (CHECK de 0022 respectés)."""
    found = {}
    event = _historical(db)
    document = _event(db, event.id)["retention_scopes"]
    item = dict(document["scopes"][0])
    _set(db, event.id, retention_scopes={"version": document["version"], "scopes": [{**item, "kind": "user"}]})
    found["structure"] = (event.id, el.RETENTION_INCOHERENT)
    event = _historical(db)
    _set(db, event.id, next_scope_expires_at=_event(db, event.id)["next_scope_expires_at"] - timedelta(days=1))
    found["columns"] = (event.id, el.RETENTION_INCOHERENT)
    event = _historical(db)
    occurred = utc(_event(db, event.id)["occurred_at"])
    _rewrite_scopes(db, event.id, {event.campaign_ids[0]: occurred + timedelta(seconds=1)}, keep_columns=True)
    found["invisible"] = (event.id, el.RETENTION_INCOHERENT)  # document échu, colonnes de sélection futures
    event = _historical(db)
    _rewrite_scopes(db, event.id, {event.campaign_ids[0]: occurred + DAYS_90 + timedelta(days=1)})
    found["beyond_policy"] = (event.id, el.ATTACHMENT_INCOHERENT)
    event = _historical(db)
    foreign = open_wave(db, USER_B)
    scope = retention.RetentionScope("campaign", foreign, "v0-retention-v1", occurred + DAYS_90)
    _set(db, event.id, retention_scopes=retention.build_retention_document([scope]))
    found["not_attached"] = (event.id, el.ATTACHMENT_INCOHERENT)
    event = _historical(db)
    _set(db, event.id, schema_version="product-activity-event-v0")
    found["schema"] = (event.id, el.SCHEMA_UNSUPPORTED)
    return found


def test_the_integrity_check_reports_every_anomaly_including_those_invisible_to_the_selection(db, clock):
    anomalies = _anomalies(db, clock)
    valid = _historical(db)
    expired = _historical(db)
    clock.advance(seconds=1)
    invisible = anomalies["invisible"][0]
    assert committed(db, lambda s: el.select_expired_event_candidates(s)).event_ids == ()  # rien d'échu en colonnes
    world = _world(db)
    seen, remove = _statements(db)
    try:
        independent = lb.inspect_events_page(db)  # parcours sans filtre temporel : TOUS les événements
        assert (independent.examined, independent.inconsistent, independent.valid) == (
            len(anomalies) + 2, len(anomalies), 2)
        assert invisible in committed(db, lambda s: el.select_integrity_candidates(s)).event_ids
        for name, (event_id, reason) in anomalies.items():
            assert inspect_one(db, event_id) == lb.EventOutcome(lb.INCONSISTENT, reason), name
        assert inspect_one(db, valid.id) == lb.EventOutcome(lb.VALID)
        clock.set(_expiry(db, expired.id) + timedelta(days=1))
        assert inspect_one(db, expired.id) == lb.EventOutcome(lb.VALID)  # échu n'est pas incohérent
        assert inspect_one(db, uuid.uuid4()) == lb.EventOutcome(lb.ABSENT)
        report = lb.inspect_events_page(db)
    finally:
        remove()
    assert (report.examined, report.valid, report.inconsistent, report.absent, report.unknown) == (
        len(anomalies) + 2, 2, len(anomalies), 0, 0)
    assert not [s for s in seen if s.startswith(("INSERT", "UPDATE", "DELETE", "SAVEPOINT"))]  # aucune écriture
    assert _world(db) == world  # xmin compris : aucune réparation
    # La sélection nominale traite ce qu'elle voit ; elle ne répare rien.
    page = lb.expire_events_page(db)
    assert (page.examined, page.purged, page.conflict) == (len(anomalies) + 2, 2, len(anomalies))
    assert all(_event(db, event_id) is not None for event_id, _ in anomalies.values())


def test_an_isolated_integrity_failure_never_starves_the_following_candidates(db, clock):
    events = sorted([_historical(db).id for _ in range(3)])
    holder = Holder(db, sql=f"SELECT 1 FROM {EVENTS} WHERE id = :i FOR UPDATE", i=events[0])
    try:
        report = lb.inspect_events_page(db)  # le premier attend la ligne : délai de verrou
    finally:
        holder.rollback()
    assert (report.examined, report.unknown, report.valid) == (3, 1, 2)
    first = lb.inspect_events_page(db, limit=1)
    assert first.next_cursor is not None and first.next_cursor.event_id == events[0]
    second = lb.inspect_events_page(db, limit=2, after=first.next_cursor)
    assert (second.examined, second.next_cursor.event_id) == (2, events[2])
    assert lb.inspect_events_page(db, limit=2, after=second.next_cursor).examined == 0


# --------------------------------------------------------------------------
# H. Pagination
# --------------------------------------------------------------------------

def test_empty_pages_and_the_maximum_page_in_a_deterministic_order(db, clock):
    assert committed(db, lambda s: el.select_expired_event_candidates(s)) .event_ids == ()
    empty = lb.expire_events_page(db)
    assert (empty.selected, empty.examined, empty.next_cursor) == (True, 0, None)
    seed = _historical(db)
    expiry = _expiry(db, seed.id)
    template = _event(db, seed.id)
    ids = [seed.id]
    with db.begin() as conn:  # 204 clones de même échéance (écrivain de test)
        for _ in range(204):
            values = {k: v for k, v in template.items() if k != "xmin"}
            values.update(id=uuid.uuid4(), idempotency_key=uuid.uuid4(), source_operation_id=uuid.uuid4())
            conn.execute(sa.insert(ProductActivityEvent.__table__).values(**values))
            ids.append(values["id"])
    clock.set(expiry)
    full = committed(db, lambda s: el.select_expired_event_candidates(s, limit=200))
    assert full.event_ids == tuple(sorted(ids)[:200]) and full.next_cursor.event_id == sorted(ids)[199]
    rest = committed(db, lambda s: el.select_expired_event_candidates(s, limit=200, after=full.next_cursor))
    assert rest.event_ids == tuple(sorted(ids)[200:]) and rest.next_cursor is None
    integrity = committed(db, lambda s: el.select_integrity_candidates(s, limit=200))
    assert integrity.event_ids == tuple(sorted(ids)[:200])
    page = lb.expire_events_page(db, limit=200)
    assert (page.examined, page.purged, page.next_cursor.event_id) == (200, 200, sorted(ids)[199])
    assert lb.expire_events_page(db, limit=200, after=page.next_cursor).purged == 5
    assert rows(db, f"SELECT count(*) AS n FROM {EVENTS}")[0]["n"] == 0


def test_the_nominal_selection_is_served_by_the_existing_next_expiry_index(db, clock):
    event = _historical(db)
    clock.set(_expiry(db, event.id))
    cursor = el.ExpiryCursor(_expiry(db, event.id) - timedelta(days=1), uuid.uuid4())
    for after in (None, cursor):
        statement = el._expiry_query(clock.now, 50, after)
        compiled = statement.compile(dialect=db.dialect, compile_kwargs={"literal_binds": True})
        with db.connect() as conn:
            conn.execute(sa.text("SET enable_seqscan = off"))
            plan = "\n".join(r[0] for r in conn.execute(sa.text(f"EXPLAIN {compiled}")))
        assert NEXT_INDEX in plan and "Index Cond" in plan, plan
        if after is not None:
            assert re.search(r"Index Cond: .*next_scope_expires_at >=", plan), plan  # borne du curseur dans l'index


def test_each_selected_candidate_is_reread_and_handled_once_whatever_happened_since(db, clock):
    pruned = _partial(db, clock)  # retrait partiel : next_scope_expires_at avance après traitement
    gone, ineligible = _historical(db), _historical(db)
    for event in (gone, ineligible):
        _shorten(db, event.id, event.campaign_ids[0], clock.now - timedelta(seconds=1))
    page = committed(db, lambda s: el.select_expired_event_candidates(s, limit=3))
    snapshot = {r["id"]: r["next_scope_expires_at"] for r in _table(db, EVENTS)}
    assert set(page.event_ids) == {pruned.id, gone.id, ineligible.id}
    last = page.event_ids[-1]
    assert (page.next_cursor.event_id, page.next_cursor.position) == (last, snapshot[last])  # photographie
    with db.begin() as conn:  # entre la sélection et le traitement : disparu, puis devenu non admissible
        conn.execute(sa.text(f"DELETE FROM {EVENTS} WHERE id = :i"), {"i": gone.id})
    _shorten(db, ineligible.id, ineligible.campaign_ids[0], clock.now + timedelta(days=1))
    outcomes = {event_id: expire(db, event_id) for event_id in page.event_ids}
    assert outcomes == {pruned.id: PRUNED, gone.id: ABSENT, ineligible.id: NOT_DUE}
    assert lb.expire_events_page(db).examined == 0  # aucun ne reste éligible : rien n'est rendu deux fois


def test_the_page_cursor_is_the_snapshot_never_the_state_after_treatment(db, clock):
    event = _partial(db, clock)
    snapshot = utc(_event(db, event.id)["next_scope_expires_at"])
    report = lb.expire_events_page(db, limit=1)
    assert (report.pruned, utc(report.next_cursor.position), report.next_cursor.event_id) == (1, snapshot, event.id)
    assert utc(_event(db, event.id)["next_scope_expires_at"]) > snapshot  # l'état a avancé, pas le curseur


def test_a_failure_on_the_first_candidate_never_starves_the_following_ones(db, clock):
    events = [_historical(db) for _ in range(3)]
    clock.set(max(_expiry(db, e.id) for e in events))
    first = committed(db, lambda s: el.select_expired_event_candidates(s)).event_ids[0]
    holder = Holder(db, sql=f"SELECT 1 FROM {EVENTS} WHERE id = :i FOR UPDATE", i=first)
    try:
        report = lb.expire_events_page(db)
    finally:
        holder.rollback()
    assert (report.examined, report.unknown, report.purged) == (3, 1, 2)
    assert lb.expire_events_page(db).purged == 1  # la passe suivante le reprend


# --------------------------------------------------------------------------
# I. Non-recréation
# --------------------------------------------------------------------------

def test_after_a_nominal_purge_the_exact_replay_recreates_nothing(db, clock):
    event = _historical(db)
    proof = _certified_event(db, clock, USER_B)
    clock.set(utc(proof_row(db, proof.proof_id)["purge_after_at"]))
    assert committed(db, lambda s: pl.purge_resolved_proof(s, proof_id=proof.proof_id)).outcome == pl.PURGED
    clock.set(max(_expiry(db, event.id), _expiry(db, proof.id)))
    assert lb.expire_events_page(db).purged == 2
    with pytest.raises(common.EventNotAuthorized):  # 2A : fenêtre et campagne closes depuis longtemps
        committed(db, lambda s: pe.record_event(s, **event.kwargs))
    assert committed(db, lambda s: fz.finalize_certified_proof(s, proof_id=proof.proof_id)) == \
        fz.FinalizationDecision(fz.ABSENT)  # bloc C : la preuve n'existe plus
    from tests.activity_proof_support import register
    assert register(db, proof.origin, proof.op).state == proofs.ABSENT  # bloc B : hors observation
    assert rows(db, f"SELECT count(*) AS n FROM {EVENTS}")[0]["n"] == 0
    assert rows(db, f"SELECT count(*) AS n FROM {REGISTRY}")[0]["n"] == 0


def test_the_recreation_risk_after_an_early_shortening_in_an_open_window_is_real_and_not_masked(db, clock):
    """Limite L1 (documentée, NON résolue ici) : un raccourcissement anticipé
    pendant une fenêtre encore ouverte, puis la suppression, laissent le rejeu
    exact recréer l'événement avec la conservation nominale. Aucun tombstone,
    aucune prolongation, aucune révocation n'est introduit pour le masquer."""
    event = _historical(db)
    occurred = utc(_event(db, event.id)["occurred_at"])
    _shorten(db, event.id, event.campaign_ids[0], occurred + timedelta(hours=1))
    clock.set(occurred + timedelta(hours=1))
    assert expire(db, event.id) == PURGED
    recreated, created = committed(db, lambda s: pe.record_event(s, **event.kwargs))
    assert created and recreated.id != event.id  # recréé : le risque est caractérisé
    assert utc(recreated.purge_after_at) == occurred + DAYS_90  # avec la conservation NOMINALE
    assert rows(db, "SELECT count(*) AS n FROM pg_tables WHERE tablename LIKE '%tombstone%'")[0]["n"] == 0


# --------------------------------------------------------------------------
# Inventaires et dormance
# --------------------------------------------------------------------------

def _imports(path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.add(node.module)
            if node.module in ("core", "core.product_analytics"):
                found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


FORBIDDEN = ("authz_f2", "auth_f2", "f2_runtime", "_activity_proof_bridge", "oryx_sessions", "OryxSession",
             "_run_attested", "os.environ", "getenv", "Thread", "asyncio", "schedule", "socket", "requests", "httpx",
             "APIRouter", "FastAPI", "import_module", "__import__", "_stage_certified_event", "_certified_conflict",
             "certify_pending", "register_pending", "open_origin", "finalize_certified_proof",
             "resolve_expired_pending_proof", "TRUNCATE", "CASCADE", "SET NULL",
             "session_replication_role", "DISABLE TRIGGER", "while True", "sleep", "print(", "tombstone",
             "portfolio_positions", "pedagog", "cognitive")


def test_the_modules_import_exactly_their_side_and_reach_nothing_else():
    assert _imports(SERVICE) == {"uuid", "dataclasses", "datetime", "sqlalchemy", "sqlalchemy.exc", "core.models",
                                 "core.product_analytics", "core.product_analytics.common",
                                 "core.product_analytics.events", "core.product_analytics.finalization",
                                 "core.product_analytics.proofs", "core.product_analytics.retention"}
    assert _imports(BOUNDARY) == {"logging", "uuid", "dataclasses", "core", "core._activity_event_finalization",
                                  "core._activity_transactions", "core.product_analytics",
                                  "core.product_analytics.event_lifecycle"}
    assert _imports(TRANSACTIONS) == {"sqlalchemy.engine"}
    for path in (SERVICE, BOUNDARY, TRANSACTIONS):
        source = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN:
            assert forbidden not in source, (path.name, forbidden)
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    session_calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                     and n.func.value.id == "db"}
    assert session_calls == {"execute", "begin_nested", "expunge"}  # ni commit, ni rollback, ni add, ni flush
    # Aucun écrivain ni aucune primitive d'un autre lot n'est appelé (purge nominale de la PR 1 comprise).
    attributes = {n.attr for path in (SERVICE, BOUNDARY)
                  for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
    assert not attributes & {"record_event", "purge_resolved_proof", "observe_purge", "observe_proof", "_run",
                             "finalize_in_transaction", "expire_pending_in_transaction", "touch_activity_session",
                             "withdraw_participant", "close_campaign", "cancel_campaign"}
    assert {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "events"} == {"CERTIFIED_OPERATIONS", "CERTIFIED_OUTCOME", "CERTIFIED_ROUTE",
                                           "REQUEST_FIELDS", "_certified_request", "_identity_locks"}
    assert {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "finalization"} == {"IDENTITY_FIELDS", "lock_keys", "finalization_deadline",
                                                 "_accepted_during_observation"}
    assert len([n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "begin_nested"]) == 2  # UN SAVEPOINT par chemin d'écriture
    boundary = BOUNDARY.read_text(encoding="utf-8")
    assert "import sqlalchemy" not in boundary and "sa." not in boundary and "text(" not in boundary
    assert "SELECT" not in boundary and "DELETE " not in boundary and "UPDATE " not in boundary
    assert "execute(" not in boundary and "while " not in boundary and "for _ in" not in boundary
    assert "finalization_boundary._transaction(" in boundary and "finalization_boundary._run(" not in boundary
    assert "_owned(" in boundary and "_observe_attempt" in boundary
    # Aucune boucle multi-pages ni nouveau thread ; un seul retry n'existe nulle part.
    assert "retry" not in boundary.lower().replace("aucun retry automatique", "")


def test_nothing_calls_the_event_lifecycle_outside_the_tests_and_the_documentation():
    offenders = []
    markers = ("event_lifecycle", "_activity_event_lifecycle", "_activity_transactions", "expire_event",
               "inspect_event", "expire_events_page", "inspect_events_page", "select_expired_event_candidates",
               "select_integrity_candidates")
    # V0-T08 2C PR 3 (nominatif) : la frontière du cycle de vie des participations
    # importe les garde-fous de core/_activity_transactions.py (vérifié ci-dessous).
    participation_boundary = REPO_ROOT / "core" / "_activity_participation_lifecycle.py"
    allowed = {SERVICE, BOUNDARY, TRANSACTIONS, REPO_ROOT / "core" / "_activity_proof_lifecycle.py",
               participation_boundary}
    for path in sorted(REPO_ROOT.rglob("*")):
        if (not path.is_file() or path in allowed or ".git" in path.parts or "__pycache__" in path.parts
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
    participation_source = participation_boundary.read_text(encoding="utf-8")
    assert [m for m in markers if m in participation_source] == ["_activity_transactions"]
    assert "from core._activity_transactions import _owned, _require_engine" in participation_source
    # La frontière du registre (PR 1) n'importe des garde-fous que les noms extraits.
    proof_boundary = (REPO_ROOT / "core" / "_activity_proof_lifecycle.py").read_text(encoding="utf-8")
    assert "event_lifecycle" not in proof_boundary and "_activity_transactions import (" in proof_boundary
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
PRIVATE = ("core._activity_event_lifecycle", "core._activity_transactions", "core._activity_proof_lifecycle",
           "core._activity_event_finalization", "core._activity_proof_bridge")
loaded = sorted(n for n in sys.modules if n in PRIVATE or n.startswith("core.product_analytics"))
print(json.dumps({"loaded": loaded, "threads": sorted(t.name for t in threading.enumerate())}))
'''


def test_the_real_api_never_loads_the_event_lifecycle_whatever_the_environment():
    baseline = None
    for environment in ({}, {"ORYX_PRODUCT_ANALYTICS": "1", "ORYX_V0_T08_ENABLED": "1", "ORYX_EVENT_PURGE": "1",
                             "ORYX_EVENT_LIFECYCLE": "1", "ORYX_LOT_2C": "1", "ORYX_F2_ENABLED": "1"}):
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                             env=_clean_env(environment), timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["loaded"] == []
        baseline = baseline or result["threads"]
        assert result["threads"] == baseline


def _no_identifier_logged(caplog):
    for record in caplog.records:
        message = record.getMessage()
        assert not UUID_PATTERN.search(message) and USER_A not in message and USER_B not in message, message
        assert "retention" not in message and "campaign" not in message and "cursor" not in message.lower(), message


def test_reports_and_logs_carry_only_closed_vocabularies_and_counters(db, clock, caplog):
    caplog.set_level(logging.INFO, logger="oryx.activity_event_lifecycle")
    events = [_historical(db) for _ in range(3)]
    clock.set(max(_expiry(db, e.id) for e in events))
    report = lb.expire_events_page(db, limit=2)
    assert (report.examined, report.purged, report.next_cursor is not None) == (2, 2, True)
    assert report.examined == sum(report.counters().values())
    integrity = lb.inspect_events_page(db)
    assert integrity.examined == sum(integrity.counters().values()) == 1
    for text in (repr(report), repr(report.next_cursor), repr(integrity), repr(lb.EventOutcome(lb.PURGED)),
                 repr(committed(db, lambda s: el.select_expired_event_candidates(s))),
                 repr(committed(db, lambda s: el.observe_event_retention(s, event_id=events[2].id))),
                 repr(committed(db, lambda s: el.expire_event_scopes(s, event_id=events[2].id))),
                 repr(el.IntegrityCursor(uuid.uuid4()))):
        assert not UUID_PATTERN.search(text) and USER_A not in text and "retention" not in text, text
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("activity_event_lifecycle selection=event_expiry selected=True examined=2 pruned=0 "
                            "purged=2 ") for m in messages), messages
    assert "activity_event_expiry outcome=purged reason=None" in messages
    _no_identifier_logged(caplog)


def test_concurrent_threads_are_never_started_by_the_lifecycle(db, clock):
    before = {t.ident for t in threading.enumerate()}
    event, _ = _due_event(db, clock)
    lb.expire_events_page(db)
    lb.inspect_events_page(db)
    assert {t.ident for t in threading.enumerate()} <= before | {threading.get_ident()}
    assert _event(db, event.id) is None


# --------------------------------------------------------------------------
# R. Correctif post-audit (HEAD audité bab86fe) : contre-épreuves B1 et B2
# --------------------------------------------------------------------------

def _certified_of(db, clock, kind, *, purged):
    """Événement certifié réel du bloc C (Décrypter ou Portfolio), preuve
    présente ou déjà purgée par la purge nominale de la PR 1."""
    from tests.activity_proof_support import ACK, CREATE
    proof = _certified(db, USER_A, ACK if kind == "decrypter" else CREATE)
    clock.advance(seconds=1)
    decision = finalize(db, proof.proof_id)
    assert decision.state == fz.RECORDED
    proof.id = decision.event_id
    if purged:
        clock.set(utc(proof_row(db, proof.proof_id)["purge_after_at"]))
        assert committed(db, lambda s: pl.purge_resolved_proof(s, proof_id=proof.proof_id)).outcome == pl.PURGED
    return proof


def _tamper(db, proof, case):
    """Écrivain HORS SERVICE, toutes contraintes PostgreSQL en place."""
    event = _event(db, proof.id)
    if case == "delivery":
        if event["event_type"] == "decrypter_used":
            _set(db, proof.id, delivery_status="unconfirmed", delivered_at=None)
        else:
            _set(db, proof.id, delivery_status="confirmed", delivered_at=event["occurred_at"])
    elif case == "delivered_at":
        delivered = event["occurred_at"] + timedelta(seconds=1)
        _set(db, proof.id, delivery_status="confirmed", delivered_at=delivered)
    elif case == "updated_at":
        _set(db, proof.id, updated_at=event["updated_at"] + timedelta(seconds=1))
    elif case == "recorded_at":  # finalisation déplacée à l'échéance du bloc C (jamais admissible)
        deadline = fz.finalization_deadline(*_authorization(db, proof.campaign_id))
        _set(db, proof.id, recorded_at=deadline, updated_at=deadline)
    elif case == "origin":  # interaction d'origine : recorded_at ≠ occurred_at de l'événement
        with db.begin() as conn:
            conn.execute(sa.text(f"UPDATE {INTERACTIONS} SET recorded_at = recorded_at - interval '1 microsecond' "
                                 "WHERE interaction_key = :k"), {"k": proof.origin.interaction_key})
    else:
        raise AssertionError(case)


def _authorization(db, campaign_id, user=USER_A):
    from core.models import ProductCampaign, ProductCampaignParticipant
    return committed(db, lambda s: (s.get(ProductCampaignParticipant, (campaign_id, user)),
                                    s.get(ProductCampaign, campaign_id)))


B1_CASES = ["delivery", "delivered_at", "updated_at", "recorded_at", "origin"]
B1_REASONS = {"delivery": el.CERTIFIED_INCOHERENT, "delivered_at": el.CERTIFIED_INCOHERENT,
              "updated_at": el.CERTIFIED_INCOHERENT, "recorded_at": el.CERTIFIED_INCOHERENT,
              "origin": el.ORIGIN_INCOHERENT}


@pytest.mark.parametrize("purged", [False, True], ids=["proof_present", "proof_purged"])
@pytest.mark.parametrize("kind", ["decrypter", "portfolio"])
@pytest.mark.parametrize("case", B1_CASES)
def test_b1_a_tampered_certified_event_is_never_valid_nor_purged(db, clock, case, kind, purged):
    proof = _certified_of(db, clock, kind, purged=purged)
    assert inspect_one(db, proof.id) == lb.EventOutcome(lb.VALID)  # canonique avant l'altération
    _tamper(db, proof, case)
    if not purged:  # le bloC C, lui, refuse déjà de relire cet état comme le sien
        observed = committed(db, lambda s: fz.observe_proof(s, proof_id=proof.proof_id))
        assert (observed.state, observed.reason) == (fz.RECORDED, fz.RECORD_INCOHERENT)
    integrity = inspect_one(db, proof.id)
    assert integrity == lb.EventOutcome(lb.INCONSISTENT, B1_REASONS[case]), integrity
    clock.set(_expiry(db, proof.id) + timedelta(days=1))
    world = _world(db)
    outcome = expire(db, proof.id)
    assert outcome.outcome == lb.CONFLICT and outcome.reason == integrity.reason, outcome
    assert _world(db) == world  # aucune mutation, aucune réparation


@pytest.mark.parametrize("kind", ["decrypter", "portfolio"])
def test_b1_a_legitimately_shortened_certified_event_stays_valid_and_purgeable(db, clock, kind):
    for purged in (False, True):
        proof = _certified_of(db, clock, kind, purged=purged)
        occurred = utc(_event(db, proof.id)["occurred_at"])
        _shorten(db, proof.id, proof.campaign_id, occurred + timedelta(days=8))
        assert inspect_one(db, proof.id) == lb.EventOutcome(lb.VALID)
        clock.set(occurred + timedelta(days=8))
        assert expire(db, proof.id) == PURGED
        assert (_event(db, proof.id), _attribution(db, proof.id), proof_row(db, proof.proof_id)) == (None, None, None)


def _outside_set(db, event_id, **values):
    """Écrivain hors service sur une connexion indépendante (autocommit)."""
    with db.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT").execute(
            sa.update(ProductActivityEvent.__table__).where(ProductActivityEvent.__table__.c.id == event_id)
            .values(**values))


def test_b2_an_uncommitted_removal_whose_event_was_altered_before_the_reread_is_unknown(db, clock):
    event = _partial(db, clock)
    seen, remove = _lose_commit(db, f"UPDATE {EVENTS}", really_commit=False,
                                between=lambda: _outside_set(db, event.id, outcome="failed"))
    try:
        outcome = expire(db, event.id)
    finally:
        remove()
    assert seen == ["lost"] and outcome == lb.EventOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)


def test_b2_a_committed_removal_whose_revised_state_was_altered_is_unknown(db, clock):
    event = _partial(db, clock)
    seen, remove = _lose_commit(db, f"UPDATE {EVENTS}", really_commit=True,
                                between=lambda: _outside_set(db, event.id, delivery_status="pending"))
    try:
        outcome = expire(db, event.id)
    finally:
        remove()
    assert seen == ["lost"] and outcome == lb.EventOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)


def test_b2_an_uncommitted_purge_whose_certified_event_was_altered_is_unknown(db, clock):
    proof = _certified_of(db, clock, "decrypter", purged=True)
    clock.set(_expiry(db, proof.id))
    seen, remove = _lose_commit(db, f"DELETE FROM {EVENTS}", really_commit=False,
                                between=lambda: _outside_set(db, proof.id, delivery_status="unconfirmed",
                                                             delivered_at=None))
    try:
        outcome = expire(db, proof.id)
    finally:
        remove()
    assert seen == ["lost"] and outcome == lb.EventOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)


def test_b2_a_change_between_the_reads_of_the_reread_is_unknown_never_a_composite_state(db, clock):
    """Lecture composite : une preuve référençante est insérée hors service
    APRÈS la lecture de la liste des preuves par la relecture et AVANT la fin
    de ses lectures ; la photographie assemblée serait égale à l'état initial
    (sans preuve) alors que l'état réel a changé : jamais still_present."""
    proof, row = _after_purged_proof(db, clock)
    fired = []
    seen, remove = _lose_commit(db, f"DELETE FROM {EVENTS}", really_commit=False)

    def inject(conn, cursor, statement, parameters, context, executemany):
        if seen and not fired and f"FROM {ATTRIBUTIONS}" in statement and statement.rstrip().endswith("FOR SHARE"):
            fired.append(1)
            _restore_proof(db, row)  # référence l'attribution (FOR KEY SHARE, compatible avec FOR SHARE)
    sa.event.listen(db, "before_cursor_execute", inject)
    try:
        outcome = expire(db, proof.id)
    finally:
        sa.event.remove(db, "before_cursor_execute", inject)
        remove()
    assert seen == ["lost"] and fired == [1]
    assert outcome == lb.EventOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)
    assert proof_row(db, proof.proof_id) is not None and _event(db, proof.id) is not None


def test_an_attribution_switched_to_another_origin_between_the_reads_is_never_validated_against_the_old_one(
        db, clock):
    """L'attribution relue en L1 désigne l'interaction verrouillée au rang 6 ;
    un écrivain hors service la rattache ensuite à une AUTRE interaction
    'activity' de la même visite (FK respectées) avant son verrou : abandon,
    jamais une validation contre l'ancienne origine."""
    from tests.activity_proof_support import touch
    proof = _certified_event(db, clock)
    clock.advance(seconds=5)
    visit, other_key = touch(db, proof.campaign_id, USER_A)  # même environnement, < 30 min : même visite
    assert visit == proof.origin.visit_id
    clock.set(utc(proof_row(db, proof.proof_id)["purge_after_at"]))
    assert committed(db, lambda s: pl.purge_resolved_proof(s, proof_id=proof.proof_id)).outcome == pl.PURGED
    _shorten(db, proof.id, proof.campaign_id, clock.now)
    fired = []

    def inject(conn, cursor, statement, parameters, context, executemany):
        if not fired and f"FROM {INTERACTIONS}" in statement and statement.rstrip().endswith("FOR SHARE"):
            fired.append(1)
            _outside(db, f"UPDATE {ATTRIBUTIONS} SET interaction_key = :k WHERE event_id = :e", k=other_key,
                     e=proof.id)
    sa.event.listen(db, "before_cursor_execute", inject)
    try:
        outcome = expire(db, proof.id)
    finally:
        sa.event.remove(db, "before_cursor_execute", inject)
    assert fired == [1] and outcome == lb.EventOutcome(lb.CONFLICT, el.DEPENDENCY_CHANGED)
    assert _event(db, proof.id) is not None and _attribution(db, proof.id)["interaction_key"] == other_key
    assert inspect_one(db, proof.id) == lb.EventOutcome(lb.INCONSISTENT, el.ORIGIN_INCOHERENT)  # puis vu tel quel


def test_b1_residual_limit_a_finalization_instant_shifted_within_the_block_c_bounds_after_the_proof_purge(db, clock):
    """Limite résiduelle (documentée) : l'instant de finalisation n'a d'autre
    référence canonique que la preuve (resolved_at). Preuve présente : tout
    décalage est détecté ; preuve purgée : seul un décalage qui sort des bornes
    du bloc C l'est (test B1 « recorded_at ») ; un décalage qui y reste n'est
    plus distinguable de l'original."""
    present = _certified_of(db, clock, "portfolio", purged=False)
    purged = _certified_of(db, clock, "portfolio", purged=True)
    for proof in (present, purged):
        shifted = _event(db, proof.id)["recorded_at"] + timedelta(seconds=1)
        _set(db, proof.id, recorded_at=shifted, updated_at=shifted)
    assert inspect_one(db, present.id) == lb.EventOutcome(lb.INCONSISTENT, el.PROOF_INCOHERENT)
    assert inspect_one(db, purged.id) == lb.EventOutcome(lb.VALID)  # limite caractérisée, non masquée


def _outside(db, sql, **params):
    with db.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT").execute(sa.text(sql), params)


def test_b2_an_uncommitted_purge_whose_residual_proof_or_origin_was_altered_is_unknown(db, clock):
    for target in ("proof", "interaction", "attribution"):
        proof = _shortened_certified(db, clock)
        if target == "proof":
            alter = lambda: _outside(db, f"UPDATE {REGISTRY} SET resolved_at = resolved_at + interval '1 second', "  # noqa: E731
                                         "purge_after_at = purge_after_at + interval '1 second' WHERE id = :i",
                                     i=proof.proof_id)
        elif target == "interaction":
            alter = lambda: _outside(db, f"UPDATE {INTERACTIONS} SET recorded_at = recorded_at - "  # noqa: E731
                                         "interval '1 microsecond' WHERE interaction_key = :k",
                                     k=proof.origin.interaction_key)
        else:
            alter = lambda: _outside(db, f"UPDATE {ATTRIBUTIONS} SET certified_at = certified_at + "  # noqa: E731
                                         "interval '1 microsecond' WHERE event_id = :e", e=proof.id)
        seen, remove = _lose_commit(db, f"DELETE FROM {EVENTS}", really_commit=False, between=alter)
        try:
            outcome = expire(db, proof.id)
        finally:
            remove()
        assert seen == ["lost"] and outcome == lb.EventOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY), target


def test_b2_the_reread_takes_only_the_attempted_keys_then_shared_row_locks_in_rank_order(db, clock):
    proof = _shortened_certified(db, clock)
    keys = _keys(db, proof.id)
    seen, remove = _lose_commit(db, f"DELETE FROM {EVENTS}", really_commit=False)
    statements, stop = _statements(db)
    try:
        assert expire(db, proof.id) == lb.EventOutcome(lb.STILL_PRESENT, lb.TECHNICAL_UNCERTAINTY)
    finally:
        remove()
        stop()
    reread = statements[[i for i, s in enumerate(statements) if s.startswith("DELETE FROM " + EVENTS)][0]:]
    reread = reread[[i for i, s in enumerate(reread) if "pg_advisory_xact_lock" in s][0]:]
    locks = [s for s in reread if "pg_advisory_xact_lock" in s]
    assert len(locks) == len(keys) == 4  # les clés de l'opération tentée, aucune autre
    assert not [s for s in reread if s.startswith(("DELETE", "UPDATE", "INSERT", "SAVEPOINT"))]
    assert not [s for s in reread if s.endswith("FOR UPDATE")]
    shared = [s.split(" FROM ")[1].split(" ")[0] for s in reread if s.endswith("FOR SHARE")]
    assert shared == [INTERACTIONS, REGISTRY, ATTRIBUTIONS, EVENTS]  # rangs 6 à 9, en partage
    last_lock = max(i for i, s in enumerate(reread) if "pg_advisory_xact_lock" in s)
    assert last_lock < min(i for i, s in enumerate(reread) if s.endswith("FOR SHARE"))  # aucune clé tardive


def test_b2_the_nominal_reread_outcomes_are_preserved(db, clock):
    """Contre-épreuve : sans altération, les issues nominales restent celles
    attendues (still_present, unconfirmed, absent)."""
    for really_commit, expected in ((False, lb.STILL_PRESENT), (True, lb.UNCONFIRMED)):
        event = _partial(db, clock)
        seen, remove = _lose_commit(db, f"UPDATE {EVENTS}", really_commit=really_commit)
        try:
            assert expire(db, event.id).outcome == expected
        finally:
            remove()
    proof = _certified_of(db, clock, "decrypter", purged=False)
    occurred = utc(_event(db, proof.id)["occurred_at"])
    _shorten(db, proof.id, proof.campaign_id, occurred + timedelta(hours=1))
    clock.set(occurred + timedelta(hours=1))
    seen, remove = _lose_commit(db, f"DELETE FROM {EVENTS}", really_commit=True)
    try:
        assert expire(db, proof.id) == lb.EventOutcome(lb.ABSENT, lb.UNCONFIRMED_COMMIT)
    finally:
        remove()
