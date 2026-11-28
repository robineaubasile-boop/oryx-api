"""V0-T08 Lot 2C PR 4 — charges du contrôle GLOBAL des finalités logiques
(purge définitive des campagnes), PostgreSQL réel (16 et 18 en CI).

Volumes de 100 à 100 000 événements répartis entre plusieurs utilisateurs et
campagnes (charges de test, jamais des limites métier). Mesures (imprimées,
ligne « stress ») : durée totale, détention du verrou de campagne, documents
examinés, instructions, pic mémoire Python, attente d'une transaction
concurrente, comportement du budget de 10 secondes. Aucune suppression sur
la base d'une lecture partielle ; aucune migration, aucune recherche
approximative.
"""
import json
import threading
import time
import tracemalloc
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core import _activity_campaign_lifecycle as cb
from core.product_analytics import campaign_lifecycle as cl
from core.product_analytics import campaigns as pc
from core.product_analytics import common, proofs
from core.product_analytics import participation_lifecycle as pl
from tests.activity_proof_support import EVENTS, POLICY, committed, rows
from tests.test_activity_event_finalization import USER_A, USER_B, clock, db, engine  # noqa: F401 — fixtures
from tests.test_activity_event_lifecycle import _statements
from tests.test_activity_participation_lifecycle import _plan, _sql
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

VOLUMES = [100, 1000, 10000, 50000, 100000]
FIRST = uuid.UUID("00000000-0000-4000-8000-000000000000")
LAST = uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")


def _expired_empty_campaign(db, clock):
    campaign_id = committed(db, lambda s: pc.create_campaign(
        s, code=f"wave-{uuid.uuid4().hex[:10]}", name="Vague 0", purpose="Mesurer.",
        close_by_at=clock.now + timedelta(days=30), **POLICY).id)
    clock.advance(minutes=1)
    committed(db, lambda s: pc.cancel_campaign(s, campaign_id=campaign_id))
    return campaign_id


def _scope(campaign_id, expires):
    return {"kind": "campaign", "campaign_id": str(campaign_id), "policy_version": "v0-retention-v1",
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}


def _insert_events(db, count, campaigns, now):
    """`count` événements 2A canoniques répartis entre USER_A / USER_B et les
    campagnes `campaigns` (documents canoniques d'AUTRES campagnes)."""
    expires = now + timedelta(days=80)
    documents = [json.dumps({"version": "retention-scopes-v1", "scopes": [_scope(c, expires)]}) for c in campaigns]
    _sql(db, f"""
        INSERT INTO {EVENTS} (id, user_id, visit_id, idempotency_key, event_type, surface, executed_route, source,
                              source_operation_kind, source_operation_id, outcome, delivery_status, occurred_at,
                              recorded_at, updated_at, resolved_at, delivered_at, retention_scopes,
                              next_scope_expires_at, purge_after_at, schema_version)
        SELECT gen_random_uuid(), CASE WHEN g % 2 = 0 THEN :a ELSE :b END, gen_random_uuid(), gen_random_uuid(),
               'coach_used', 'coach', NULL, 'client_signal', NULL, NULL, 'succeeded', 'not_applicable', :t, :t, :t,
               :t, NULL, CAST((CAST(:docs AS text[]))[1 + g % :n] AS jsonb), :x, :x, 'product-activity-event-v1'
          FROM generate_series(1, :count) g""",
         a=USER_A, b=USER_B, t=now - timedelta(days=10), x=expires, docs=documents, n=len(documents), count=count)
    _sql(db, f"ANALYZE {EVENTS}")


def _event(db, event_id, document, now):
    _sql(db, f"""
        INSERT INTO {EVENTS} (id, user_id, visit_id, idempotency_key, event_type, surface, source, outcome,
                              delivery_status, occurred_at, recorded_at, updated_at, retention_scopes,
                              next_scope_expires_at, purge_after_at, schema_version)
        VALUES (:i, :u, gen_random_uuid(), gen_random_uuid(), 'coach_used', 'coach', 'client_signal', 'succeeded',
                'not_applicable', :t, :t, :t, CAST(:d AS jsonb), :x, :x, 'product-activity-event-v1')""",
         i=event_id, u=USER_B, t=now - timedelta(days=10), x=now + timedelta(days=80), d=json.dumps(document))


def _measured(db, campaign_id):
    """Purge par la frontière, avec ses mesures."""
    seen, remove = _statements(db)
    tracemalloc.start()
    started = time.monotonic()
    try:
        outcome = cb.purge_campaign(db, campaign_id=campaign_id)
    finally:
        elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        remove()
    fetches = len([s for s in seen if s.startswith("FETCH")]) or len([s for s in seen if "retention_scopes" in s])
    return outcome, {"seconds": round(elapsed, 3), "statements": len(seen), "scan_statements": fetches,
                     "python_peak_mb": round(peak / 1e6, 2)}


def _report(label, volume, outcome, measures, **extra):
    print(f"stress {label} volume={volume} outcome={outcome.outcome} reason={outcome.reason} "
          + " ".join(f"{k}={v}" for k, v in {**measures, **extra}.items()))


@pytest.mark.parametrize("volume", VOLUMES)
def test_an_exhaustive_global_scan_without_reference_purges_within_the_budget(db, clock, volume):
    a = _expired_empty_campaign(db, clock)
    others = [uuid.uuid4() for _ in range(5)]
    _insert_events(db, volume, others, clock.now)
    clock.set(pl.participation_retention_deadline(committed(db, lambda s: s.get(pc.ProductCampaign, a))))
    scanned = {}
    real = cl._event_scope_scan

    def counting(session, campaign_id, budget):
        result = real(session, campaign_id, budget)
        scanned.setdefault("reads", []).append(result[1])
        return result
    cl._event_scope_scan = counting
    try:
        outcome, measures = _measured(db, a)
    finally:
        cl._event_scope_scan = real
    _report("no_reference", volume, outcome, measures, documents=sum(scanned["reads"]), reads=len(scanned["reads"]))
    if outcome.outcome == cb.UNKNOWN:  # capacité dépassée : documentée, jamais une suppression partielle
        assert outcome.reason == cb.TECHNICAL_UNCERTAINTY or outcome.reason == cl.SCAN_BUDGET_EXCEEDED
        assert committed(db, lambda s: s.get(pc.ProductCampaign, a)) is not None
    else:
        assert outcome == cb.Outcome(cb.PURGED)
        assert scanned["reads"] == [volume, volume]  # deux lectures EXHAUSTIVES
    assert measures["python_peak_mb"] < 64  # lecture progressive : aucune ligne ORM en mémoire


@pytest.mark.parametrize("position", ["first", "last"])
@pytest.mark.parametrize("volume", [10000, 100000])
def test_a_blocking_scope_at_the_start_or_the_end_of_the_scan_is_found(db, clock, volume, position):
    a = _expired_empty_campaign(db, clock)
    _insert_events(db, volume, [uuid.uuid4() for _ in range(5)], clock.now)
    _event(db, FIRST if position == "first" else LAST,
           {"version": "retention-scopes-v1", "scopes": [_scope(a, clock.now + timedelta(days=80))]}, clock.now)
    clock.set(pl.participation_retention_deadline(committed(db, lambda s: s.get(pc.ProductCampaign, a))))
    outcome, measures = _measured(db, a)
    _report(f"blocking_{position}", volume, outcome, measures)
    assert outcome in (cb.Outcome(cb.BLOCKED, cl.EVENT_SCOPE_REMAINING),
                       cb.Outcome(cb.UNKNOWN, cl.SCAN_BUDGET_EXCEEDED))
    assert outcome.outcome == cb.BLOCKED or volume == 100000
    assert committed(db, lambda s: s.get(pc.ProductCampaign, a)) is not None


@pytest.mark.parametrize("form", ["ambiguous", "non_canonical_a", "non_canonical_b", "outsider_a"])
def test_special_documents_among_50000_events(db, clock, form):
    a = _expired_empty_campaign(db, clock)
    b = uuid.uuid4()
    _insert_events(db, 50000, [uuid.uuid4() for _ in range(5)], clock.now)
    scope = _scope(b, clock.now + timedelta(days=80))
    document = {"ambiguous": {**scope, "kind": "user"},
                "non_canonical_a": {**scope, "campaign_id": str(a).upper()},
                "non_canonical_b": {**scope, "campaign_id": str(b).replace("-", "")},
                "outsider_a": {**scope, "campaign_id": str(a)}}[form]
    _event(db, uuid.UUID(int=uuid.uuid4().int), {"version": "retention-scopes-v1", "scopes": [document]}, clock.now)
    clock.set(pl.participation_retention_deadline(committed(db, lambda s: s.get(pc.ProductCampaign, a))))
    outcome, measures = _measured(db, a)
    _report(form, 50000, outcome, measures)
    expected = {"ambiguous": cb.Outcome(cb.CONFLICT, cl.RETENTION_INCOHERENT),
                "non_canonical_a": cb.Outcome(cb.CONFLICT, cl.RETENTION_INCOHERENT),
                "non_canonical_b": cb.Outcome(cb.PURGED),
                "outsider_a": cb.Outcome(cb.BLOCKED, cl.EVENT_SCOPE_REMAINING)}[form]
    assert outcome == expected


def test_an_interrupted_scan_and_two_reads_exceeding_the_budget_never_delete(db, clock, monkeypatch):
    a = _expired_empty_campaign(db, clock)
    _insert_events(db, 10000, [uuid.uuid4()], clock.now)
    clock.set(pl.participation_retention_deadline(committed(db, lambda s: s.get(pc.ProductCampaign, a))))
    # Budget réduit en test seulement (jamais exposé aux points d'entrée) : interruption au milieu de la lecture.
    monkeypatch.setattr(cl, "GLOBAL_SCAN_BUDGET_SECONDS", 0.05)
    outcome, measures = _measured(db, a)
    _report("interrupted", 10000, outcome, measures, budget=0.05)
    assert outcome == cb.Outcome(cb.UNKNOWN, cl.SCAN_BUDGET_EXCEEDED)
    assert committed(db, lambda s: s.get(pc.ProductCampaign, a)) is not None
    # Deux lectures cumulées : chacune sous le budget, leur somme au-delà (horloge monotone virtuelle :
    # chaque document « coûte » 10 / 15 000 s ; 10 000 documents par lecture = 6,7 s).
    monkeypatch.setattr(cl, "GLOBAL_SCAN_BUDGET_SECONDS", 10.0)
    virtual = {"now": 0.0, "documents": 0}
    monkeypatch.setattr(cl, "_monotonic", lambda: virtual["now"])
    real = cl.retention.campaign_reference

    def costly(document, campaign_id):
        virtual["now"] += 10.0 / 15000
        virtual["documents"] += 1
        return real(document, campaign_id)
    monkeypatch.setattr(cl.retention, "campaign_reference", costly)
    outcome = cb.purge_campaign(db, campaign_id=a)
    _report("two_reads", 10000, outcome, {"virtual_seconds": round(virtual["now"], 3)},
            documents=virtual["documents"])
    assert outcome == cb.Outcome(cb.UNKNOWN, cl.SCAN_BUDGET_EXCEEDED)
    assert 10000 < virtual["documents"] < 20000  # première lecture complète, seconde interrompue
    assert committed(db, lambda s: s.get(pc.ProductCampaign, a)) is not None


def test_the_campaign_lock_hold_and_the_wait_of_a_concurrent_transaction_are_measured(db, clock):
    a = _expired_empty_campaign(db, clock)
    _insert_events(db, 50000, [uuid.uuid4() for _ in range(5)], clock.now)
    clock.set(pl.participation_retention_deadline(committed(db, lambda s: s.get(pc.ProductCampaign, a))))
    waited = {}
    started = threading.Event()

    def contender():
        started.wait(10)
        time.sleep(0.05)  # la purge détient déjà le verrou exclusif
        with Session(bind=db) as session:
            begin = time.monotonic()
            found = session.execute(sa.text("SELECT 1 FROM product_campaigns WHERE id = :i FOR SHARE"),
                                    {"i": a}).first()
            waited["seconds"] = time.monotonic() - begin
            waited["found"] = found
            session.rollback()
    thread = threading.Thread(target=contender, daemon=True)
    thread.start()
    real = cl._dependency_problem

    def signal(session, campaign_id, budget):
        started.set()
        return real(session, campaign_id, budget)
    cl._dependency_problem = signal
    try:
        outcome, measures = _measured(db, a)
    finally:
        cl._dependency_problem = real
    thread.join(30)
    _report("lock_hold", 50000, outcome, measures, contender_wait_seconds=round(waited["seconds"], 3))
    assert outcome == cb.Outcome(cb.PURGED) and waited["found"] is None  # attente, puis la campagne a disparu
    assert waited["seconds"] <= measures["seconds"]


def test_the_global_scan_plan_reads_the_events_in_primary_key_order(db, clock):
    _insert_events(db, 10000, [uuid.uuid4()], clock.now)
    statement = (sa.select(cl.ProductActivityEvent.retention_scopes).order_by(cl.ProductActivityEvent.id))
    plan = _plan(db, statement, seqscan=False)
    print("stress plan " + " | ".join(line.strip() for line in plan.splitlines()))
    assert "product_activity_events_pkey" in plan
