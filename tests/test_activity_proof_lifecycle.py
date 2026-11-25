"""V0-T08 Lot 2C PR 1 — cycle de vie, résolution et purge du registre
provisoire des preuves (core/product_analytics/proof_lifecycle.py, frontière
core/_activity_proof_lifecycle.py), contre un VRAI PostgreSQL (16 et 18 en
CI). Chaîne réelle F2 -> B -> C -> purge : tests/test_activity_proof_lifecycle_chain.py.

1. sans base : vocabulaires (= 0024, = bloc B, = bloc C), bornes, entrées
   fermées refusées avant tout accès, règle de purge, classement des issues
   (aucun succès fictif) ;
2. sélection : vide, un, plusieurs, limite maximale, ordre déterministe,
   pagination par curseur, lecture seule, index de purge ;
3. récupération et résolution par le bloc C : pending non expirée intacte,
   pending expirée résolue sans certification ni antidatage, certified
   admissible finalisée, certified expirée sans événement, états terminaux
   jamais ressuscités, aucune interaction ni preuve nouvelle, concurrence
   avec B et C, COMMIT incertain ;
4. purge : 168 h - 1 µs / exactement / au-delà pour chaque état terminal,
   états non terminaux, absence idempotente, échéance incohérente, horloge
   régressive, fuseaux UTC / Europe/Paris / America/New_York aux passages
   d'heure d'été et d'hiver, intégrité des données définitives ;
5. concurrence et horloge : deux purges, relecture et retry du bloc C dans
   les deux sens, certification concurrente, attente franchissant
   l'échéance (horloge PostgreSQL réelle), délai de verrou, interblocage
   réel provoqué par un écrivain hors service, aucun verrou nouveau ;
6. pannes : avant et après le DELETE, ROLLBACK, COMMIT incertain effectué ou
   non, relecture impossible, base injoignable, réexécution explicite ;
7. pages : candidat devenu non admissible ou supprimé après la sélection,
   aucune famine derrière une première ligne en échec ;
8. journaux et dormance.
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

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core import _activity_event_finalization as fb
from core import _activity_proof_lifecycle as lb
from core.product_analytics import finalization as fz
from core.product_analytics import participation as pp
from core.product_analytics import proof_lifecycle as pl
from core.product_analytics import proofs
from core.product_analytics.common import InvalidProductAnalyticsInput
from tests.activity_proof_support import (
    ATTRIBUTIONS,
    EVENTS,
    REGISTRY,
    Background,
    Holder,
    PROOF_APP,
    committed,
    open_wave,
    opened,
    operation,
    proof_row,
    register,
    rows,
    settle,
    utc,
    waiting,
)
from tests.test_activity_event_finalization import (  # noqa: F401 — fixtures engine, db, clock
    GRACE,
    HOURS_168,
    MICRO,
    USER_A,
    USER_B,
    _attributions,
    _certified,
    _events,
    _participation,
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

SERVICE = REPO_ROOT / "core" / "product_analytics" / "proof_lifecycle.py"
BOUNDARY = REPO_ROOT / "core" / "_activity_proof_lifecycle.py"
INTERACTIONS = "product_activity_session_interactions"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PURGE_INDEX = "ix_product_activity_proof_registry_purge_after_at"
TERMINAL = ("recorded", "rejected", "indeterminate")


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_the_vocabularies_are_those_of_0024_and_of_the_blocks_b_and_c():
    assert pl.RESOLVED_STATUSES == proofs.RESOLVED_STATUSES == ("recorded", "rejected", "indeterminate")
    assert pl.REGISTRY_RETENTION == proofs.REGISTRY_RETENTION == HOURS_168  # jamais « 7 jours » civils
    assert pl.FINALIZATION_GRACE == fz.FINALIZATION_GRACE == GRACE  # aucune échéance propre à ce module
    assert pl.IDENTITY_FIELDS == fz.IDENTITY_FIELDS
    assert (pl.DEFAULT_PAGE_SIZE, pl.MAX_PAGE_SIZE) == (50, 200)
    assert pl.SELECTIONS == ("certified_recovery", "pending_expiry", "resolved_purge")
    assert pl.OPERATION_CONFLICT == proofs.OPERATION_CONFLICT
    assert not set(pl.PURGE_REASONS) & set(proofs.RESOLUTION_REASONS)  # jamais écrits dans le registre
    assert lb.OUTCOMES == ("finalized", "resolved", "purged", "absent", "still_present", "not_admissible",
                           "conflict", "unknown")
    assert set(lb.OUTCOME_REASONS) == set(fz.FINALIZATION_REASONS) | set(pl.PURGE_REASONS) | {"unconfirmed_commit"}
    assert lb.TECHNICAL_UNCERTAINTY == proofs.TECHNICAL_UNCERTAINTY
    # La clé de la purge est EXACTEMENT celle du bloc B, comprise dans l'ensemble du bloc C.
    op = operation(USER_A)
    proof = SimpleNamespace(id=uuid.uuid4(), user_id=USER_A, source_operation_kind=op.operation_kind,
                            source_operation_id=op.operation_id, event_type="portfolio_used", visit_id=uuid.uuid4())
    assert pl.proof_lock_key(proof) == proofs.proof_lock_key(op) == fz.lock_keys(proof)[0]


def test_the_entry_points_receive_only_bounded_and_typed_inputs():
    for function in (pl.select_certified_candidates, pl.select_expired_pending_candidates,
                     pl.select_purgeable_candidates):
        assert list(inspect.signature(function).parameters) == ["db", "limit", "after"]
        assert inspect.signature(function).parameters["limit"].default == 50
        for bad in (0, -1, 201, 10 ** 6, True, 1.0, "50", None):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), limit=bad)  # refus avant tout accès à la base
        for bad in ("cursor", uuid.uuid4(), (datetime.now(timezone.utc), uuid.uuid4()),
                    pl.ProofCursor("other", datetime.now(timezone.utc), uuid.uuid4()),
                    pl.ProofCursor(pl.RESOLVED_PURGE if function is not pl.select_purgeable_candidates
                                   else pl.CERTIFIED_RECOVERY, datetime.now(timezone.utc), uuid.uuid4())):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), after=bad)
        selection = {pl.select_certified_candidates: pl.CERTIFIED_RECOVERY,
                     pl.select_expired_pending_candidates: pl.PENDING_EXPIRY}.get(function, pl.RESOLVED_PURGE)
        for position, proof_id in ((datetime(2026, 11, 25), uuid.uuid4()), (datetime.now(timezone.utc), "x")):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), after=pl.ProofCursor(selection, position, proof_id))
        with pytest.raises(TypeError):
            function(_NoDB(), limit=10, now=datetime.now(timezone.utc))  # aucune horloge de l'appelant
    for function in (pl.purge_resolved_proof, pl.observe_purge):
        assert list(inspect.signature(function).parameters) == ["db", "proof_id"]
        for bad in (str(uuid.uuid4()), None, 42):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(_NoDB(), proof_id=bad)
        for designation in ({"now": datetime.now(timezone.utc)}, {"user_id": USER_A}, {"force": True}):
            with pytest.raises(TypeError):
                function(_NoDB(), proof_id=uuid.uuid4(), **designation)
    for function in (lb.recover_certified_proof, lb.expire_pending_proof, lb.purge_proof):
        assert list(inspect.signature(function).parameters) == ["engine", "proof_id"]
        with pytest.raises(TypeError):
            function(None, proof_id=str(uuid.uuid4()))  # avant toute connexion
    for function in (lb.recover_certified_page, lb.expire_pending_page, lb.purge_resolved_page):
        assert list(inspect.signature(function).parameters) == ["engine", "limit", "after"]
        for bad in (0, 201, True):
            with pytest.raises(InvalidProductAnalyticsInput):
                function(None, limit=bad)  # avant toute connexion


def _stored(status, resolved_at, purge_after_at):
    return SimpleNamespace(status=status, resolved_at=resolved_at, purge_after_at=purge_after_at)


def test_the_purge_rule_is_exactly_the_0024_retention_and_never_recomputes_it():
    resolved = datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc)
    due = resolved + HOURS_168
    for status in TERMINAL:
        proof = _stored(status, resolved, due)
        assert pl.purge_problem(proof, due - MICRO) == pl.NOT_DUE
        assert pl.purge_problem(proof, due) is None  # égalité admise
        assert pl.purge_problem(proof, due + timedelta(days=400)) is None
        assert pl.purge_problem(proof, resolved - MICRO) == pl.CLOCK_INCOHERENT  # horloge régressive
        for incoherent in (resolved + timedelta(hours=167), resolved + timedelta(hours=169), resolved, None):
            assert pl.purge_problem(_stored(status, resolved, incoherent), due + timedelta(days=1)) == \
                pl.RETENTION_INCOHERENT
        assert pl.purge_problem(_stored(status, None, due), due) == pl.RETENTION_INCOHERENT
    for status in ("pending", "certified"):
        assert pl.purge_problem(_stored(status, None, None), due + timedelta(days=400)) == pl.NOT_RESOLVED
    # 168 heures absolues, quel que soit le fuseau de l'instant stocké.
    paris = timezone(timedelta(hours=2))
    assert pl.purge_problem(_stored("rejected", resolved.astimezone(paris), due), due) is None


def _decision(state, *, written, reason=None, event_id=None, resolved_at=None):
    return fz.FinalizationDecision(state, reason, written, uuid.uuid4(), event_id, resolved_at)


def test_an_outcome_is_attributed_to_this_treatment_only_for_its_own_established_write():
    event_id, at = uuid.uuid4(), datetime.now(timezone.utc)
    recorded = _decision(fz.RECORDED, written=True, event_id=event_id, resolved_at=at)
    report = fb.FinalizationReport(fz.RECORDED, None, event_id)
    assert lb._classify(report, recorded) == lb.LifecycleOutcome(lb.FINALIZED, fz.RECORDED, None)
    # Relecture idempotente d'une preuve déjà recorded : jamais comptée comme finalisée ici.
    assert lb._classify(report, _decision(fz.RECORDED, written=False, event_id=event_id)).outcome == \
        lb.NOT_ADMISSIBLE
    # Écriture flushée dont le COMMIT n'est pas établi : jamais un succès.
    assert lb._classify(fb.FinalizationReport(fb.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY), recorded).outcome == \
        lb.UNKNOWN
    assert lb._classify(fb.FinalizationReport(fz.CERTIFIED, proofs.TECHNICAL_UNCERTAINTY), recorded).outcome == \
        lb.UNKNOWN
    assert lb._classify(report, None).outcome == lb.NOT_ADMISSIBLE  # aucune décision captée : rien n'est à nous
    # Défense en profondeur : une issue établie différente de NOTRE décision (autre événement, autre motif)
    # n'est jamais attribuée à ce traitement, même si la décision a écrit.
    assert lb._classify(fb.FinalizationReport(fz.RECORDED, None, uuid.uuid4()), recorded).outcome == \
        lb.NOT_ADMISSIBLE
    rejected = _decision(fz.REJECTED, written=True, reason=proofs.NOT_AUTHORIZED, resolved_at=at)
    assert lb._classify(fb.FinalizationReport(fz.REJECTED, proofs.WINDOW_EXPIRED), rejected).outcome == \
        lb.NOT_ADMISSIBLE
    expired = _decision(fz.INDETERMINATE, written=True, reason=proofs.FINALIZATION_EXPIRED, resolved_at=at)
    assert lb._classify(fb.FinalizationReport(fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED), expired) == \
        lb.LifecycleOutcome(lb.RESOLVED, fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED)
    assert lb._classify(fb.FinalizationReport(fz.INDETERMINATE, proofs.TECHNICAL_UNCERTAINTY),
                        _decision(fz.INDETERMINATE, written=False)).outcome == lb.NOT_ADMISSIBLE
    assert lb._classify(fb.FinalizationReport(fz.CERTIFIED, proofs.OPERATION_CONFLICT),
                        _decision(fz.CERTIFIED, written=False)).outcome == lb.CONFLICT
    assert lb._classify(fb.FinalizationReport(fz.RECORDED, fz.RECORD_INCOHERENT),
                        _decision(fz.RECORDED, written=False)).outcome == lb.CONFLICT
    assert lb._classify(fb.FinalizationReport(fz.ABSENT), _decision(fz.ABSENT, written=False)).outcome == lb.ABSENT
    assert lb._classify(fb.FinalizationReport(fz.PENDING), _decision(fz.PENDING, written=False)).outcome == \
        lb.NOT_ADMISSIBLE


# --------------------------------------------------------------------------
# PostgreSQL réel : outils
# --------------------------------------------------------------------------

def _recorded(db, clock, user=USER_A, *, campaign_id=None):
    proof = _certified(db, user, campaign_id=campaign_id)
    clock.advance(seconds=1)
    assert finalize(db, proof.proof_id).state == fz.RECORDED
    return proof


def _rejected(db, clock, user=USER_A, *, campaign_id=None):
    proof = _certified(db, user, campaign_id=campaign_id)
    committed(db, lambda s: pp.withdraw_participant(s, campaign_id=proof.campaign_id, user_id=user))
    clock.advance(seconds=1)
    assert finalize(db, proof.proof_id).state == fz.REJECTED
    return proof


def _indeterminate(db, clock, user=USER_A, *, campaign_id=None):
    origin, op = opened(db, user, campaign_id=campaign_id)
    pending = register(db, origin, op)
    clock.advance(seconds=1)
    assert settle(db, origin, op, pending.proof_id).state == proofs.INDETERMINATE
    return SimpleNamespace(proof_id=pending.proof_id, campaign_id=origin.campaign_id, origin=origin, op=op)


RESOLVE = {"recorded": _recorded, "rejected": _rejected, "indeterminate": _indeterminate}


def _pending(db, user=USER_A, *, campaign_id=None):
    origin, op = opened(db, user, campaign_id=campaign_id)
    pending = register(db, origin, op)
    return SimpleNamespace(proof_id=pending.proof_id, campaign_id=origin.campaign_id, origin=origin, op=op)


def _due(db, proof_id) -> datetime:
    return utc(proof_row(db, proof_id)["purge_after_at"])


def _select(db, function, **kwargs):
    return committed(db, lambda s: function(s, **kwargs))


def _table(db, table):
    return rows(db, f"SELECT t.*, t.xmin::text AS xmin FROM {table} t ORDER BY t::text")


def _world(db, *, without=None) -> dict:
    """Toutes les données hors registre, et le registre sans la preuve
    `without` : lignes et xmin (aucune réécriture invisible)."""
    world = {table: _table(db, table) for table in (EVENTS, ATTRIBUTIONS, INTERACTIONS, "product_activity_sessions",
                                                     "product_campaign_participants", "product_campaigns", "users")}
    world[REGISTRY] = [r for r in _table(db, REGISTRY) if r["id"] != without]
    return world


def _constraints(db):
    return rows(db, "SELECT conrelid::regclass::text AS t, conname, contype, convalidated, condeferrable, "
                    "pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid::regclass::text IN "
                    f"('{REGISTRY}', '{ATTRIBUTIONS}', '{EVENTS}') ORDER BY 1, 2")


def _shift(db, proof_id, delta):
    """Écrivain hors service (scénario explicite) : décale TOUS les instants
    de la preuve d'un même `delta` (CHECK de 0024 respectés : chronologie et
    168 heures exactes conservées) pour exercer l'horloge PostgreSQL réelle."""
    with db.begin() as conn:
        shift = "make_interval(secs => :s)"  # secondes absolues (jamais des jours civils)
        assignments = ", ".join(f"{column} = {column} + {shift}"
                                for column in ("created_at", "certified_at", "resolved_at", "purge_after_at"))
        conn.execute(sa.text(f"UPDATE {REGISTRY} SET {assignments} WHERE id = :i"),
                     {"s": delta.total_seconds(), "i": proof_id})


def _real_clock_terminal(db, monkeypatch, status="rejected", *, due_in):
    """Preuve terminale dont purge_after_at = horloge PostgreSQL réelle +
    `due_in` ; l'horloge réelle est rétablie pour la purge."""
    from tests.activity_proof_support import Clock
    fake = Clock(datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5))
    monkeypatch.setattr(pl.common, "utcnow", fake)
    monkeypatch.setattr(proofs, "server_clock", fake)
    proof = RESOLVE[status](db, fake)
    monkeypatch.undo()
    now = rows(db, "SELECT clock_timestamp() AS n")[0]["n"]
    _shift(db, proof.proof_id, now + due_in - _due(db, proof.proof_id))
    return proof


# --------------------------------------------------------------------------
# 2. Sélection
# --------------------------------------------------------------------------

def test_an_empty_registry_selects_nothing(db, clock):
    for function in (pl.select_certified_candidates, pl.select_expired_pending_candidates,
                     pl.select_purgeable_candidates):
        page = _select(db, function)
        assert (page.proof_ids, page.next_cursor) == ((), None)


def test_each_selection_returns_only_its_potential_candidates_and_writes_nothing(db, clock):
    campaign = open_wave(db, USER_A)
    certified = _certified(db, campaign_id=campaign)
    clock.advance(seconds=1)
    pending = _pending(db, campaign_id=campaign)
    clock.advance(seconds=1)
    resolved = {status: RESOLVE[status](db, clock, campaign_id=campaign) for status in ("recorded", "indeterminate")}
    before = _world(db)
    assert _select(db, pl.select_certified_candidates).proof_ids == (certified.proof_id,)
    assert _select(db, pl.select_expired_pending_candidates).proof_ids == ()  # échéance non atteinte
    assert _select(db, pl.select_purgeable_candidates).proof_ids == ()  # conservation non échue
    deadline = fz.finalization_deadline(_participation(db, campaign), SimpleNamespace(
        close_by_at=rows(db, "SELECT close_by_at FROM product_campaigns WHERE id = :c", c=campaign)[0]["close_by_at"]))
    clock.set(deadline - MICRO)
    assert _select(db, pl.select_expired_pending_candidates).proof_ids == ()
    clock.set(deadline)  # même borne que finalization.finalization_deadline
    assert _select(db, pl.select_expired_pending_candidates).proof_ids == (pending.proof_id,)
    due = {status: _due(db, proof.proof_id) for status, proof in resolved.items()}
    clock.set(due["indeterminate"] - MICRO)
    assert _select(db, pl.select_purgeable_candidates).proof_ids == (resolved["recorded"].proof_id,)
    clock.set(due["indeterminate"])
    assert _select(db, pl.select_purgeable_candidates).proof_ids == (resolved["recorded"].proof_id,
                                                                     resolved["indeterminate"].proof_id)
    # La sélection, répétée, ne modifie rien (xmin compris) et ne rend jamais pending / certified à la purge.
    assert _world(db) == before
    assert _select(db, pl.select_certified_candidates).proof_ids == (certified.proof_id,)


def _clone(db, proof_id, count):
    """Écrivain hors service (volume) : `count` copies terminales d'une preuve
    réelle, nouvelles identités canoniques, mêmes rattachements (FK et CHECK
    de 0024 respectés)."""
    columns = [c["column_name"] for c in rows(db, "SELECT column_name FROM information_schema.columns WHERE "
                                                  "table_name = :t ORDER BY ordinal_position", t=REGISTRY)]
    values = ", ".join("gen_random_uuid()" if c in ("id", "source_operation_id") else c for c in columns)
    with db.begin() as conn:
        conn.execute(sa.text(f"INSERT INTO {REGISTRY} ({', '.join(columns)}) SELECT {values} FROM {REGISTRY}, "
                             "generate_series(1, :n) WHERE id = :i"), {"n": count, "i": proof_id})


def test_the_maximum_page_the_deterministic_order_and_the_cursor_pagination(db, clock):
    seed = _rejected(db, clock)
    _clone(db, seed.proof_id, 204)  # 205 preuves de même purge_after_at : départage par identifiant
    clock.set(_due(db, seed.proof_id))
    expected = [r["id"] for r in rows(db, f"SELECT id FROM {REGISTRY} ORDER BY purge_after_at, id")]
    assert len(expected) == 205 and expected == sorted(expected)
    first = _select(db, pl.select_purgeable_candidates, limit=pl.MAX_PAGE_SIZE)
    assert list(first.proof_ids) == expected[:200] and first.next_cursor is not None
    assert repr(first.next_cursor) == "ProofCursor(resolved_purge, <private>)"
    second = _select(db, pl.select_purgeable_candidates, limit=pl.MAX_PAGE_SIZE, after=first.next_cursor)
    assert (list(second.proof_ids), second.next_cursor) == (expected[200:], None)
    # Pages de 7 : chaque candidat exactement une fois, dans l'ordre.
    seen, cursor = [], None
    while True:
        page = _select(db, pl.select_purgeable_candidates, limit=7, after=cursor)
        seen += page.proof_ids
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert seen == expected
    # Ordre identique d'une lecture à l'autre ; une page exactement pleine annonce une suite (vide).
    assert _select(db, pl.select_purgeable_candidates, limit=5).proof_ids == tuple(expected[:5])
    last = _select(db, pl.select_purgeable_candidates, limit=5, after=pl.ProofCursor(
        pl.RESOLVED_PURGE, clock.now, expected[199]))
    assert last.proof_ids == tuple(expected[200:]) and last.next_cursor is not None
    tail = _select(db, pl.select_purgeable_candidates, limit=5, after=last.next_cursor)
    assert (tail.proof_ids, tail.next_cursor) == ((), None)


def test_the_purge_selection_is_served_by_the_0024_partial_index(db, clock):
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.exec_driver_sql("SET enable_seqscan = off")  # table vide : rendre le choix d'index observable
        for after in (None, pl.ProofCursor(pl.RESOLVED_PURGE, now, uuid.uuid4())):
            sql = str(pl._purge_query(now, 50, after).compile(dialect=conn.dialect,
                                                               compile_kwargs={"literal_binds": True}))
            plan = " ".join(r[0] for r in conn.exec_driver_sql("EXPLAIN " + sql.replace("%", "%%")))
            # Parcours d'index (simple ou bitmap selon les statistiques), borné par l'horloge.
            assert re.search(rf"(Index Scan using|Bitmap Index Scan on) {PURGE_INDEX}", plan), plan
            assert re.search(r"Index Cond: \(+purge_after_at <=", plan) and "Seq Scan" not in plan, plan


# --------------------------------------------------------------------------
# 3. Récupération et résolution par le bloc C
# --------------------------------------------------------------------------

def test_a_pending_proof_before_its_deadline_is_never_selected_nor_touched(db, clock):
    pending = _pending(db)
    before = _world(db)
    end = _participation(db, pending.campaign_id).window_end_at
    for instant in (end - MICRO, end, end + GRACE - MICRO):
        clock.set(instant)
        report = lb.expire_pending_page(db)
        assert (report.selected, report.examined, report.next_cursor) == (True, 0, None)
        assert lb.expire_pending_proof(db, proof_id=pending.proof_id) == lb.LifecycleOutcome(
            lb.NOT_ADMISSIBLE, fz.PENDING, None)  # jamais déclarée incertaine arbitrairement
        assert lb.recover_certified_proof(db, proof_id=pending.proof_id).outcome == lb.NOT_ADMISSIBLE
    assert _world(db) == before


def test_an_expired_pending_proof_is_resolved_late_without_certification_and_never_antedated(db, clock):
    pending = _pending(db)
    deadline = _participation(db, pending.campaign_id).window_end_at + GRACE
    interactions = _table(db, INTERACTIONS)
    resolved_at = clock.set(deadline + timedelta(hours=2))  # traitement tardif
    report = lb.expire_pending_page(db)
    assert (report.examined, report.resolved, report.next_cursor) == (1, 1, None)
    assert report.counters() == {**dict.fromkeys(lb.OUTCOMES, 0), "resolved": 1}
    row = proof_row(db, pending.proof_id)
    assert (row["status"], row["resolution_reason"], row["certified_at"], row["recorded_event_id"]) == (
        "indeterminate", "finalization_expired", None, None)
    assert utc(row["resolved_at"]) == resolved_at != deadline  # instant réel de résolution, jamais l'échéance
    assert row["purge_after_at"] - row["resolved_at"] == HOURS_168
    assert (_events(db), _attributions(db), _table(db, INTERACTIONS)) == ([], [], interactions)
    clock.advance(hours=1)
    again = lb.expire_pending_page(db)
    assert (again.examined, again.resolved) == (0, 0)  # résolue : plus jamais sélectionnée
    for function in (lb.expire_pending_proof, lb.recover_certified_proof):
        assert function(db, proof_id=pending.proof_id) == lb.LifecycleOutcome(
            lb.NOT_ADMISSIBLE, fz.INDETERMINATE, proofs.FINALIZATION_EXPIRED)
    assert proof_row(db, pending.proof_id) == row


def test_an_admissible_certified_proof_is_finalized_by_the_real_block_c(db, clock):
    proof = _certified(db)
    interactions, registry = _table(db, INTERACTIONS), len(_table(db, REGISTRY))
    clock.advance(seconds=1)
    report = lb.recover_certified_page(db)
    assert (report.examined, report.finalized, report.next_cursor) == (1, 1, None)
    row = proof_row(db, proof.proof_id)
    [event], [attribution] = _events(db), _attributions(db)
    assert (row["status"], row["recorded_event_id"], event["idempotency_key"], attribution["event_id"]) == (
        "recorded", event["id"], proof.proof_id, event["id"])
    assert attribution["certified_at"] == row["certified_at"] == event["resolved_at"]
    assert utc(row["resolved_at"]) == clock.now == utc(event["recorded_at"])
    # Aucune interaction, aucune preuve nouvelle ; la reprise suivante ne rend plus rien.
    assert _table(db, INTERACTIONS) == interactions and len(_table(db, REGISTRY)) == registry
    assert lb.recover_certified_page(db).examined == 0
    assert lb.recover_certified_proof(db, proof_id=proof.proof_id) == lb.LifecycleOutcome(
        lb.NOT_ADMISSIBLE, fz.RECORDED, None)  # recorded jamais réenregistrée
    assert (len(_events(db)), len(_attributions(db))) == (1, 1)


def test_an_expired_certified_proof_never_produces_an_event_and_is_not_retried_forever(db, clock):
    proof = _certified(db)
    deadline = _participation(db, proof.campaign_id).window_end_at + GRACE
    clock.set(deadline)
    report = lb.recover_certified_page(db)
    assert (report.examined, report.resolved, report.finalized) == (1, 1, 0)
    row = proof_row(db, proof.proof_id)
    assert (row["status"], row["resolution_reason"], utc(row["resolved_at"])) == (
        "indeterminate", "finalization_expired", deadline)
    assert row["certified_at"] is not None and (_events(db), _attributions(db)) == ([], [])
    clock.advance(days=1)
    assert lb.recover_certified_page(db).examined == 0  # terminale : plus jamais reprise
    assert proof_row(db, proof.proof_id) == row


@pytest.mark.parametrize("status", TERMINAL)
def test_terminal_proofs_are_never_selected_for_recovery_nor_resurrected(db, clock, status):
    proof = RESOLVE[status](db, clock)
    before = _world(db)
    clock.advance(days=20)  # au-delà de toute échéance de finalisation
    for page in (lb.recover_certified_page, lb.expire_pending_page):
        assert page(db).examined == 0
    for function in (lb.recover_certified_proof, lb.expire_pending_proof):
        outcome = function(db, proof_id=proof.proof_id)
        assert (outcome.outcome, outcome.state) == (lb.NOT_ADMISSIBLE, status)
    assert _world(db) == before


def test_a_recovery_waits_for_a_concurrent_certification_and_finalizes_it(db):
    origin, op = opened(db, USER_A)
    pending = register(db, origin, op)
    holder = Holder(db, lambda s: proofs.certify_pending(s, origin=origin, operation=op, proof_id=pending.proof_id))
    contender = Background(lambda: lb.recover_certified_proof(db, proof_id=pending.proof_id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la récupération n'attend pas la certification")
    finally:
        holder.commit()
    assert holder.value.state == proofs.CERTIFIED
    assert contender.outcome() == lb.LifecycleOutcome(lb.FINALIZED, fz.RECORDED, None)
    assert (len(_events(db)), len(_attributions(db))) == (1, 1)


def test_a_recovery_page_concurrent_with_a_finalization_claims_nothing(db):
    proof = _certified(db)
    holder = Holder(db, lambda s: fz.finalize_certified_proof(s, proof_id=proof.proof_id))
    contender = Background(lambda: lb.recover_certified_page(db))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la récupération n'attend pas la finalisation")
    finally:
        holder.commit()
    report = contender.outcome()
    assert holder.value.written and holder.value.state == fz.RECORDED
    assert (report.examined, report.finalized, report.not_admissible) == (1, 0, 1)  # jamais attribuée à la reprise
    assert (len(_events(db)), len(_attributions(db))) == (1, 1)


def test_an_expiry_page_concurrent_with_a_pending_resolution_claims_nothing(db, clock):
    pending = _pending(db)
    clock.set(_participation(db, pending.campaign_id).window_end_at + GRACE)
    holder = Holder(db, lambda s: fz.resolve_expired_pending_proof(s, proof_id=pending.proof_id))
    contender = Background(lambda: lb.expire_pending_page(db))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la résolution n'attend pas la résolution concurrente")
    finally:
        holder.commit()
    report = contender.outcome()
    assert holder.value.written and (report.examined, report.resolved, report.not_admissible) == (1, 0, 1)
    assert proof_row(db, pending.proof_id)["status"] == "indeterminate"


def test_a_concurrent_certification_after_the_deadline_is_a_rejection_seen_by_the_expiry(db, clock):
    pending = _pending(db)
    clock.set(_participation(db, pending.campaign_id).window_end_at + GRACE)
    holder = Holder(db, lambda s: proofs.certify_pending(s, origin=pending.origin, operation=pending.op,
                                                         proof_id=pending.proof_id))
    contender = Background(lambda: lb.expire_pending_proof(db, proof_id=pending.proof_id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la résolution n'attend pas la certification")
    finally:
        holder.commit()
    assert (holder.value.state, holder.value.reason) == (proofs.REJECTED, proofs.WINDOW_EXPIRED)
    assert contender.outcome() == lb.LifecycleOutcome(lb.NOT_ADMISSIBLE, fz.REJECTED, proofs.WINDOW_EXPIRED)


def _lose_commit(engine, statement_prefix, *, really_commit):
    seen = []

    def mark(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(statement_prefix):
            conn.info["target"] = True

    def lose(conn):
        if conn.info.pop("target", False) and not seen:
            seen.append("lost")
            if really_commit:
                conn.connection.dbapi_connection.commit()
            raise OSError("confirmation du COMMIT analytics perdue")
    sa.event.listen(engine, "before_cursor_execute", mark)
    sa.event.listen(engine, "commit", lose)
    return seen, lambda: (sa.event.remove(engine, "commit", lose),
                          sa.event.remove(engine, "before_cursor_execute", mark))


@pytest.mark.parametrize("really_commit", [True, False])
def test_an_uncertain_recovery_commit_is_established_by_block_c_and_never_assumed(db, clock, really_commit):
    proof = _certified(db)
    clock.advance(seconds=1)
    seen, remove = _lose_commit(db, f"INSERT INTO {EVENTS}", really_commit=really_commit)
    try:
        report = lb.recover_certified_page(db)
    finally:
        remove()
    assert seen == ["lost"]
    if really_commit:
        assert (report.finalized, report.unknown) == (1, 0)  # COMMIT vérifié par la relecture du bloc C
        assert proof_row(db, proof.proof_id)["status"] == "recorded"
    else:
        assert (report.finalized, report.unknown) == (0, 1)  # aucun succès annoncé
        assert proof_row(db, proof.proof_id)["status"] == "certified" and _events(db) == []
        # Réexécution EXPLICITE (aucun retry automatique), sans F2 ni certification nouvelle.
        assert lb.recover_certified_proof(db, proof_id=proof.proof_id).outcome == lb.FINALIZED
    assert (len(_events(db)), len(_attributions(db))) == (1, 1)


# --------------------------------------------------------------------------
# 4. Purge
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", TERMINAL)
def test_a_terminal_proof_is_purged_from_exactly_168_hours_never_a_microsecond_before(db, clock, status):
    proof = RESOLVE[status](db, clock)
    later = RESOLVE[status](db, clock, USER_B)
    due, row = _due(db, proof.proof_id), proof_row(db, proof.proof_id)
    assert due - utc(row["resolved_at"]) == HOURS_168
    clock.set(due - MICRO)
    assert lb.purge_proof(db, proof_id=proof.proof_id) == lb.LifecycleOutcome(lb.NOT_ADMISSIBLE, status, pl.NOT_DUE)
    assert proof_row(db, proof.proof_id) == row  # rien modifié (xmin compris)
    clock.set(due)
    assert lb.purge_proof(db, proof_id=proof.proof_id) == lb.LifecycleOutcome(lb.PURGED, fz.ABSENT, None)
    assert proof_row(db, proof.proof_id) is None
    clock.set(_due(db, later.proof_id) + timedelta(days=30))
    assert lb.purge_proof(db, proof_id=later.proof_id).outcome == lb.PURGED
    # Idempotent : une preuve absente n'est jamais attribuée à ce traitement.
    for proof_id in (proof.proof_id, later.proof_id, uuid.uuid4()):
        assert lb.purge_proof(db, proof_id=proof_id) == lb.LifecycleOutcome(lb.ABSENT, fz.ABSENT, None)
    assert rows(db, f"SELECT id FROM {REGISTRY}") == []


def test_pending_and_certified_proofs_are_never_purged_directly(db, clock):
    campaign = open_wave(db, USER_A)
    pending, certified = _pending(db, campaign_id=campaign), _certified(db, campaign_id=campaign)
    before = _world(db)
    clock.advance(days=400)
    for proof, state in ((pending, "pending"), (certified, "certified")):
        assert committed(db, lambda s, p=proof: pl.purge_resolved_proof(s, proof_id=p.proof_id)) == pl.PurgeDecision(
            pl.NOT_PURGEABLE, pl.NOT_RESOLVED, False, state)
        assert lb.purge_proof(db, proof_id=proof.proof_id) == lb.LifecycleOutcome(lb.NOT_ADMISSIBLE, state,
                                                                                    pl.NOT_RESOLVED)
    report = lb.purge_resolved_page(db)
    assert (report.examined, report.purged) == (0, 0)
    assert _world(db) == before


INCOHERENCES = [
    ("resolved_at + interval '167 hours'", None),
    ("resolved_at + interval '169 hours'", None),
    # Sept jours CIVILS à Paris à travers le passage à l'heure d'hiver : 169 heures absolues.
    ("(resolved_at AT TIME ZONE 'Europe/Paris' + interval '7 days') AT TIME ZONE 'Europe/Paris'",
     datetime(2026, 10, 24, 23, tzinfo=timezone.utc)),
]


@pytest.mark.parametrize("expression, resolved_at", INCOHERENCES)
def test_an_incoherent_retention_deadline_is_refused_even_when_reached(db, clock, expression, resolved_at):
    """La règle de 0024 est imposée par un CHECK ; pour exercer la défense du
    service, la contrainte est retirée DANS une transaction de test, la ligne
    altérée, la purge appelée dans cette même transaction, puis tout est
    annulé (DDL compris)."""
    if resolved_at is not None:
        clock.set(resolved_at - timedelta(seconds=1))
    proof = _rejected(db, clock)
    before = proof_row(db, proof.proof_id)
    clock.set(_due(db, proof.proof_id) + timedelta(days=2))
    with Session(bind=db, autoflush=False, expire_on_commit=False) as session:
        session.execute(sa.text(f"ALTER TABLE {REGISTRY} DROP CONSTRAINT "
                                "ck_product_activity_proof_registry_purge_after"))
        session.execute(sa.text(f"UPDATE {REGISTRY} SET purge_after_at = {expression} WHERE id = :i"),
                        {"i": proof.proof_id})
        altered = session.execute(sa.text(f"SELECT purge_after_at - resolved_at AS d FROM {REGISTRY} WHERE id = :i"),
                                  {"i": proof.proof_id}).scalar_one()
        decision = pl.purge_resolved_proof(session, proof_id=proof.proof_id)
        session.rollback()
    assert altered != HOURS_168
    assert decision == pl.PurgeDecision(pl.NOT_PURGEABLE, pl.RETENTION_INCOHERENT, False, "rejected")
    assert proof_row(db, proof.proof_id) == before  # annulé, contrainte rétablie
    assert any(c["conname"] == "ck_product_activity_proof_registry_purge_after" for c in _constraints(db))


def test_a_regressive_clock_never_purges(db, clock):
    proof = _indeterminate(db, clock)
    row = proof_row(db, proof.proof_id)
    clock.set(utc(row["resolved_at"]) - timedelta(seconds=1))
    assert lb.purge_proof(db, proof_id=proof.proof_id) == lb.LifecycleOutcome(lb.NOT_ADMISSIBLE, "indeterminate",
                                                                                pl.CLOCK_INCOHERENT)
    assert proof_row(db, proof.proof_id) == row


TRANSITIONS = [  # (fuseau de session, résolution juste avant un passage d'heure, sens)
    ("UTC", datetime(2026, 10, 24, 23, tzinfo=timezone.utc), "hiver"),
    ("Europe/Paris", datetime(2026, 10, 24, 23, tzinfo=timezone.utc), "hiver"),  # 25/10/2026 03:00 -> 02:00
    ("Europe/Paris", datetime(2027, 3, 27, 23, tzinfo=timezone.utc), "été"),  # 28/03/2027 02:00 -> 03:00
    ("America/New_York", datetime(2026, 10, 31, 23, tzinfo=timezone.utc), "hiver"),  # 01/11/2026
    ("America/New_York", datetime(2027, 3, 13, 23, tzinfo=timezone.utc), "été"),  # 14/03/2027
]


@pytest.mark.parametrize("zone, resolved_at, change", TRANSITIONS)
def test_the_168_hours_are_absolute_across_daylight_saving_changes(db, clock, zone, resolved_at, change):
    clock.set(resolved_at - timedelta(seconds=1))
    proof = _rejected(db, clock)
    assert utc(proof_row(db, proof.proof_id)["resolved_at"]) == resolved_at
    due = resolved_at + HOURS_168
    engine = sa.create_engine(db.url, poolclass=sa.pool.NullPool, connect_args={"options": f"-c TimeZone={zone}"})
    try:
        with engine.connect() as conn:
            local = conn.execute(sa.text(
                f"SELECT current_setting('TimeZone') AS tz, purge_after_at - resolved_at AS absolute, "
                f"(purge_after_at AT TIME ZONE :z) - (resolved_at AT TIME ZONE :z) AS wall "
                f"FROM {REGISTRY} WHERE id = :i"), {"z": zone, "i": proof.proof_id}).mappings().one()
        assert (local["tz"], local["absolute"]) == (zone, HOURS_168)
        purge_after, resolved = sa.literal_column("purge_after_at"), sa.literal_column("resolved_at")
        with engine.connect() as conn:
            guard = conn.execute(sa.select(purge_after == resolved + pl.absolute_interval(HOURS_168),
                                           purge_after == resolved + sa.bindparam("naive", HOURS_168))
                                 .select_from(sa.table(REGISTRY)).where(sa.literal_column("id") == proof.proof_id),
                                 {"naive": HOURS_168}).one()
        # Garde SQL du DELETE : secondes absolues. Contre-épreuve : un timedelta lié tel quel devient
        # « 7 days » (psycopg2), ajoutés en jours civils du fuseau de session — faux aux changements d'heure.
        assert guard[0] is True and guard[1] is (zone == "UTC")
        # Heure murale hors UTC : 167 h (passage à l'heure d'hiver) ou 169 h (heure d'été) — jamais 7 jours civils.
        expected_wall = HOURS_168 if zone == "UTC" else HOURS_168 + timedelta(hours=-1 if change == "hiver" else 1)
        assert local["wall"] == expected_wall
        for instant, expected in ((due - MICRO, pl.NOT_PURGEABLE), (due, pl.PURGED)):
            clock.set(instant)
            with Session(bind=engine, autoflush=False, expire_on_commit=False) as session:
                decision = pl.purge_resolved_proof(session, proof_id=proof.proof_id)
                session.commit()
            assert decision.outcome == expected
    finally:
        engine.dispose()
    assert proof_row(db, proof.proof_id) is None


def test_purging_a_recorded_proof_leaves_the_definitive_data_exactly_intact(db, clock):
    proof = _recorded(db, clock)
    other = _recorded(db, clock, USER_B)  # autre preuve, non échue
    world, constraints = _world(db, without=proof.proof_id), _constraints(db)
    [event] = [e for e in _events(db) if e["idempotency_key"] == proof.proof_id]
    clock.set(_due(db, proof.proof_id))
    assert _due(db, other.proof_id) > clock.now
    report = lb.purge_resolved_page(db)
    assert (report.examined, report.purged) == (1, 1)
    assert proof_row(db, proof.proof_id) is None
    # Événements, attributions (xmin compris), autres preuves, visites, interactions, participations,
    # campagnes, utilisateurs : identiques ; contraintes inchangées.
    assert _world(db) == world and _constraints(db) == constraints
    # L'attribution ne référence pas le registre : aucune FK attribution / événement -> registre.
    assert rows(db, "SELECT conname FROM pg_constraint WHERE confrelid = CAST(:r AS regclass)", r=REGISTRY) == []
    # L'événement et son attribution restent exploitables jusqu'à leur propre échéance (90 jours).
    [joined] = rows(db, f"SELECT e.next_scope_expires_at, e.purge_after_at, a.certified_at FROM {EVENTS} e "
                        f"JOIN {ATTRIBUTIONS} a ON a.event_id = e.id WHERE e.id = :i", i=event["id"])
    assert utc(joined["purge_after_at"]) == utc(event["occurred_at"]) + timedelta(days=90) > clock.now
    assert joined["certified_at"] == event["resolved_at"]
    # Rien n'est recréé : ni preuve, ni événement (bloc C : absente), ni attribution.
    assert finalize(db, proof.proof_id) == fz.FinalizationDecision(fz.ABSENT)
    assert lb.recover_certified_proof(db, proof_id=proof.proof_id).outcome == lb.ABSENT
    clock.set(utc(event["purge_after_at"]) - MICRO)
    assert lb.purge_resolved_page(db).purged == 1  # l'autre preuve, échue depuis : le registre seul
    assert _world(db)[EVENTS] == world[EVENTS] and _world(db)[ATTRIBUTIONS] == world[ATTRIBUTIONS]
    assert rows(db, f"SELECT id FROM {REGISTRY}") == []


# --------------------------------------------------------------------------
# 5. Concurrence et horloge
# --------------------------------------------------------------------------

def _contended(db, holder_action, contender_action):
    holder = Holder(db, holder_action)
    contender = Background(contender_action)
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "le concurrent n'attend pas")
    finally:
        holder.commit()
    return holder.value, contender.outcome()


def test_two_concurrent_purges_delete_once_and_the_loser_claims_nothing(db, clock):
    proof = _rejected(db, clock)
    clock.set(_due(db, proof.proof_id))
    won, lost = _contended(db, lambda s: pl.purge_resolved_proof(s, proof_id=proof.proof_id),
                           lambda: lb.purge_proof(db, proof_id=proof.proof_id))
    assert won == pl.PurgeDecision(pl.PURGED, None, True, None)
    assert lost == lb.LifecycleOutcome(lb.ABSENT, fz.ABSENT, None)
    assert proof_row(db, proof.proof_id) is None


@pytest.mark.parametrize("block_c", ["observe", "finalize_retry"])
def test_a_purge_and_a_block_c_reread_are_serialized_both_ways(db, clock, block_c):
    service = fz.observe_proof if block_c == "observe" else fz.finalize_certified_proof
    first, second = _recorded(db, clock), _recorded(db, clock, USER_B)
    events_before = (_events(db), _attributions(db))
    clock.set(max(_due(db, first.proof_id), _due(db, second.proof_id)))
    # Relecture (ou retry) du bloc C en cours, puis purge : elle attend, puis supprime.
    reread, purged = _contended(db, lambda s: service(s, proof_id=first.proof_id),
                                lambda: lb.purge_proof(db, proof_id=first.proof_id))
    assert (reread.state, reread.reason, reread.written) == (fz.RECORDED, fz.RETENTION_EXPIRED, False)
    assert purged.outcome == lb.PURGED
    # Purge en cours, puis relecture (ou retry) du bloc C : elle attend, puis constate l'absence.
    deleted, after = _contended(db, lambda s: pl.purge_resolved_proof(s, proof_id=second.proof_id),
                                lambda: committed(db, lambda s: service(s, proof_id=second.proof_id)))
    assert deleted.outcome == pl.PURGED and after == fz.FinalizationDecision(fz.ABSENT)
    assert (_events(db), _attributions(db)) == events_before  # rien supprimé ni recréé côté définitif
    assert rows(db, f"SELECT id FROM {REGISTRY}") == []


def test_a_purge_waits_for_concurrent_block_b_transitions_and_never_purges_too_early(db):
    pending = _pending(db)
    holder = Holder(db, lambda s: proofs.settle(s, origin=pending.origin, operation=pending.op,
                                                proof_id=pending.proof_id))
    contender = Background(lambda: lb.purge_proof(db, proof_id=pending.proof_id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la purge n'attend pas le bloc B")
    finally:
        holder.commit()
    # settle a résolu la preuve à l'instant de sa décision : purge non échue (168 h comptées depuis).
    assert holder.value.state == proofs.INDETERMINATE
    assert contender.outcome() == lb.LifecycleOutcome(lb.NOT_ADMISSIBLE, "indeterminate", pl.NOT_DUE)
    # Certification en cours : la purge l'attend, puis refuse la preuve certified.
    pending = _pending(db, USER_B)
    certified, refused = _contended(db, lambda s: proofs.certify_pending(s, origin=pending.origin,
                                                                         operation=pending.op,
                                                                         proof_id=pending.proof_id),
                                    lambda: lb.purge_proof(db, proof_id=pending.proof_id))
    assert certified.state == proofs.CERTIFIED and certified.written
    assert refused == lb.LifecycleOutcome(lb.NOT_ADMISSIBLE, "certified", pl.NOT_RESOLVED)


def _hold_proof_key(db, proof_id):
    row = proof_row(db, proof_id)
    key = pl.proof_lock_key(SimpleNamespace(**row))
    return Holder(db, sql="SELECT pg_advisory_xact_lock(:k)", k=key)


def test_a_lock_wait_crossing_the_deadline_is_decided_with_the_postgresql_clock_read_after_it(db, monkeypatch):
    """Horloge PostgreSQL RÉELLE : la purge démarre avant l'échéance (la
    sélection ne rend pas encore la preuve), attend le verrou canonique,
    l'échéance passe pendant l'attente : la décision prise APRÈS le verrou
    supprime."""
    proof = _real_clock_terminal(db, monkeypatch, due_in=timedelta(milliseconds=1200))
    due = _due(db, proof.proof_id)
    assert committed(db, lambda s: pl.select_purgeable_candidates(s)).proof_ids == ()
    holder = _hold_proof_key(db, proof.proof_id)
    contender = Background(lambda: lb.purge_proof(db, proof_id=proof.proof_id))
    contender.start()
    try:
        _wait(lambda: waiting(db) == 1, "la purge n'attend pas le verrou canonique")
        assert datetime.now(timezone.utc) < due
        _sleep_until(due + timedelta(milliseconds=200))  # attente totale < lock_timeout (2 s)
    finally:
        holder.rollback()
    assert contender.outcome() == lb.LifecycleOutcome(lb.PURGED, fz.ABSENT, None)
    # Sans attente, avant l'échéance réelle : conservée.
    early = _real_clock_terminal(db, monkeypatch, "recorded", due_in=timedelta(minutes=5))
    assert lb.purge_proof(db, proof_id=early.proof_id) == lb.LifecycleOutcome(lb.NOT_ADMISSIBLE, "recorded",
                                                                                pl.NOT_DUE)


def test_a_lock_timeout_purges_nothing_and_the_explicit_rerun_is_clean(db, clock):
    proof = _rejected(db, clock)
    clock.set(_due(db, proof.proof_id))
    row = proof_row(db, proof.proof_id)
    holder = _hold_proof_key(db, proof.proof_id)
    try:
        outcome = lb.purge_proof(db, proof_id=proof.proof_id)  # lock_timeout 2 s de la frontière
    finally:
        holder.rollback()
    assert outcome == lb.LifecycleOutcome(lb.UNKNOWN, lb.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    assert proof_row(db, proof.proof_id) == row
    assert lb.purge_proof(db, proof_id=proof.proof_id).outcome == lb.PURGED


def _deadlock_count(engine):
    return rows(engine, "SELECT deadlocks AS n FROM pg_stat_database WHERE datname = current_database()")[0]["n"]


def _row_lock_waits(engine) -> int:
    return rows(engine, "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = :a AND "
                        "wait_event_type = 'Lock' AND wait_event IN ('transactionid', 'tuple')", a=PROOF_APP)[0]["n"]


def test_a_real_deadlock_caused_by_an_outside_writer_is_detected_and_purges_nothing_wrongly(engine, monkeypatch):
    """Écrivain hors service qui prend la LIGNE de preuve puis la clé
    canonique (ordre inverse de tous les services) : PostgreSQL détecte
    l'interblocage ; l'issue de la purge est UNKNOWN (rien supprimé) ou
    PURGED (l'autre transaction est la victime), jamais un succès fictif."""
    try:
        proof = _real_clock_terminal(engine, monkeypatch, due_in=-timedelta(seconds=1))
        key = pl.proof_lock_key(SimpleNamespace(**proof_row(engine, proof.proof_id)))
        before = _deadlock_count(engine)
        writer = Session(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            writer.execute(sa.text(f"SELECT 1 FROM {REGISTRY} WHERE id = :i FOR UPDATE"), {"i": proof.proof_id})
            contender = Background(lambda: lb.purge_proof(engine, proof_id=proof.proof_id))
            contender.start()  # clé obtenue, puis attente de la ligne tenue par l'écrivain
            _wait(lambda: _row_lock_waits(engine) == 1, "la purge n'attend pas la ligne")
            try:
                writer.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})  # cycle
                writer.commit()
            except sa.exc.OperationalError:  # l'écrivain est la victime
                writer.rollback()
            outcome = contender.outcome()
        finally:
            writer.close()
        assert _deadlock_count(engine) == before + 1  # interblocage réellement détecté par PostgreSQL
        present = proof_row(engine, proof.proof_id) is not None
        assert (outcome.outcome, present) in ((lb.UNKNOWN, True), (lb.PURGED, False)), (outcome, present)
    finally:
        with engine.begin() as conn:
            for table in (REGISTRY, ATTRIBUTIONS, EVENTS, INTERACTIONS, "product_activity_sessions",
                          "product_cohort_seats", "product_campaign_participants", "product_campaign_cohorts",
                          "product_campaigns"):
                conn.execute(sa.text(f"DELETE FROM {table}"))


def test_the_purge_takes_one_advisory_key_then_the_proof_row_and_nothing_else(db, monkeypatch):
    proof = _real_clock_terminal(db, monkeypatch, due_in=-timedelta(seconds=1))
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))
    sa.event.listen(db, "before_cursor_execute", capture)
    try:
        assert lb.purge_proof(db, proof_id=proof.proof_id).outcome == lb.PURGED
    finally:
        sa.event.remove(db, "before_cursor_execute", capture)
    work = [s for s in statements if not s.startswith("SET ")]
    assert len([s for s in statements if s.startswith("SET ")]) == 3  # garde-fous de la frontière
    shapes = [("advisory" if "pg_advisory_xact_lock" in s else "clock" if "clock_timestamp" in s else
               "savepoint" if s.startswith(("SAVEPOINT", "RELEASE SAVEPOINT")) else
               "delete" if s.startswith(f"DELETE FROM {REGISTRY}") else
               "proof_for_update" if s.startswith("SELECT") and s.endswith("FOR UPDATE") and REGISTRY in s else
               "proof_read" if s.startswith("SELECT") and REGISTRY in s else s) for s in work]
    assert shapes == ["proof_read", "advisory", "proof_for_update", "clock", "savepoint", "delete", "savepoint"]
    assert [s for s in work if s.startswith("SELECT pg_advisory_xact_lock")].__len__() == 1
    for other in ("product_campaign", "product_activity_sessions", INTERACTIONS, EVENTS, ATTRIBUTIONS, "users",
                  "FOR SHARE", "FOR KEY SHARE", "SKIP LOCKED", "NOWAIT"):
        assert not any(other in s for s in work), other


# --------------------------------------------------------------------------
# 6. Pannes et COMMIT incertain
# --------------------------------------------------------------------------

def _due_rejected(db, clock):
    proof = _rejected(db, clock)
    clock.set(_due(db, proof.proof_id))
    return proof, proof_row(db, proof.proof_id)


@pytest.mark.parametrize("moment", ["before_delete", "after_delete"])
def test_a_failure_around_the_delete_purges_nothing_and_claims_nothing(db, clock, moment):
    proof, row = _due_rejected(db, clock)
    event = "before_cursor_execute" if moment == "before_delete" else "after_cursor_execute"

    def fail(conn, cursor, statement, *args):
        if statement.startswith(f"DELETE FROM {REGISTRY}"):
            raise RuntimeError("panne injectée")
    sa.event.listen(db, event, fail)
    try:
        outcome = lb.purge_proof(db, proof_id=proof.proof_id)
        page = lb.purge_resolved_page(db)
    finally:
        sa.event.remove(db, event, fail)
    assert outcome == lb.LifecycleOutcome(lb.UNKNOWN, lb.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)
    assert (page.examined, page.purged, page.unknown) == (1, 0, 1)  # aucun compteur de succès fictif
    assert proof_row(db, proof.proof_id) == row
    assert lb.purge_proof(db, proof_id=proof.proof_id).outcome == lb.PURGED  # réexécution explicite


def test_a_flushed_deletion_rolled_back_by_the_caller_is_not_durable(db, clock):
    proof, row = _due_rejected(db, clock)
    with Session(bind=db, autoflush=False, expire_on_commit=False) as session:
        decision = pl.purge_resolved_proof(session, proof_id=proof.proof_id)
        assert decision == pl.PurgeDecision(pl.PURGED, None, True, None)  # flush seulement
        assert proof_row(db, proof.proof_id) == row  # invisible hors de la transaction
        session.rollback()
    assert proof_row(db, proof.proof_id) == row
    # Le service ne committe jamais : une session fermée sans COMMIT ne supprime rien.
    with Session(bind=db, autoflush=False, expire_on_commit=False) as session:
        assert pl.purge_resolved_proof(session, proof_id=proof.proof_id).written
    assert proof_row(db, proof.proof_id) == row


@pytest.mark.parametrize("really_commit", [True, False])
def test_an_uncertain_purge_commit_is_never_attributed_to_this_treatment(db, clock, really_commit, caplog):
    caplog.set_level(logging.INFO, logger="oryx.activity_proof_lifecycle")
    proof, row = _due_rejected(db, clock)
    seen, remove = _lose_commit(db, f"DELETE FROM {REGISTRY}", really_commit=really_commit)
    try:
        page = lb.purge_resolved_page(db)
    finally:
        remove()
    assert seen == ["lost"] and (page.examined, page.purged) == (1, 0)
    if really_commit:
        assert (page.absent, page.still_present) == (1, 0)  # absente, sans suppression attribuée
        assert proof_row(db, proof.proof_id) is None
        assert lb.purge_proof(db, proof_id=proof.proof_id) == lb.LifecycleOutcome(lb.ABSENT, fz.ABSENT, None)
    else:
        assert (page.absent, page.still_present) == (0, 1)
        assert proof_row(db, proof.proof_id) == row  # jamais recréée ni modifiée
        assert lb.purge_proof(db, proof_id=proof.proof_id).outcome == lb.PURGED  # réexécution explicite
    _no_identifier_logged(caplog)


def test_an_uncertain_purge_commit_that_cannot_be_reread_is_unknown(db, clock, monkeypatch):
    proof, _ = _due_rejected(db, clock)

    def unreadable(session, *, proof_id):
        raise OSError("relecture impossible")
    monkeypatch.setattr(pl, "observe_purge", unreadable)
    seen, remove = _lose_commit(db, f"DELETE FROM {REGISTRY}", really_commit=True)
    try:
        outcome = lb.purge_proof(db, proof_id=proof.proof_id)
    finally:
        remove()
    assert seen == ["lost"] and outcome == lb.LifecycleOutcome(lb.UNKNOWN, lb.UNKNOWN, proofs.TECHNICAL_UNCERTAINTY)


def test_an_unreachable_database_is_unknown_and_never_raises(db, clock):
    down = sa.create_engine(db.url.set(port=1), poolclass=sa.pool.NullPool)
    proof_id = uuid.uuid4()
    for function in (lb.purge_proof, lb.recover_certified_proof, lb.expire_pending_proof):
        assert function(down, proof_id=proof_id).outcome == lb.UNKNOWN
    for page in (lb.purge_resolved_page, lb.recover_certified_page, lb.expire_pending_page):
        report = page(down)
        assert (report.selected, report.examined, report.next_cursor) == (False, 0, None)


# --------------------------------------------------------------------------
# 7. Pages : décisions reprises sous verrou, aucune famine
# --------------------------------------------------------------------------

def _after_selection(monkeypatch, name, action):
    original = getattr(pl, name)

    def selected(session, **kwargs):
        page = original(session, **kwargs)
        action(page)
        return page
    monkeypatch.setattr(pl, name, selected)


def test_a_candidate_no_longer_admissible_after_the_selection_is_not_purged(db, clock, monkeypatch):
    proof, row = _due_rejected(db, clock)
    _after_selection(monkeypatch, "select_purgeable_candidates", lambda page: clock.set(clock.now - MICRO))
    page = lb.purge_resolved_page(db)
    assert (page.examined, page.not_admissible, page.purged) == (1, 1, 0)
    assert proof_row(db, proof.proof_id) == row


def test_a_candidate_deleted_between_the_selection_and_its_treatment_is_absent(db, clock, monkeypatch):
    first, second = _rejected(db, clock), _indeterminate(db, clock)
    clock.set(_due(db, second.proof_id))

    def delete_first(page):
        assert page.proof_ids == (first.proof_id, second.proof_id)
        assert committed(db, lambda s: pl.purge_resolved_proof(s, proof_id=first.proof_id)).outcome == pl.PURGED
    _after_selection(monkeypatch, "select_purgeable_candidates", delete_first)
    page = lb.purge_resolved_page(db)
    assert (page.examined, page.absent, page.purged) == (2, 1, 1)


def test_a_failing_first_candidate_never_starves_the_following_ones(db, clock):
    candidates = [RESOLVE[TERMINAL[i % 3]](db, clock) for i in range(4)]  # purge_after_at croissants
    clock.set(_due(db, candidates[-1].proof_id))
    first = candidates[0]
    holder = _hold_proof_key(db, first.proof_id)  # la première ligne reste indisponible (délai de verrou)
    try:
        # Pages d'un candidat : le curseur dépasse la ligne en échec, qui n'est pas rendue deux fois.
        one = lb.purge_resolved_page(db, limit=1)
        assert (one.examined, one.unknown, one.next_cursor is not None) == (1, 1, True)
        two = lb.purge_resolved_page(db, limit=1, after=one.next_cursor)
        assert (two.examined, two.purged) == (1, 1) and proof_row(db, candidates[1].proof_id) is None
        # Page complète : l'échec de la première ligne n'empêche pas les suivantes.
        rest = lb.purge_resolved_page(db)
        assert (rest.examined, rest.unknown, rest.purged, rest.next_cursor) == (3, 1, 2, None)
        assert proof_row(db, first.proof_id) is not None
    finally:
        holder.rollback()
    assert lb.purge_resolved_page(db).counters() == {**dict.fromkeys(lb.OUTCOMES, 0), "purged": 1}  # réexécution
    assert rows(db, f"SELECT id FROM {REGISTRY}") == []


# --------------------------------------------------------------------------
# 8. Journaux et dormance
# --------------------------------------------------------------------------

def _no_identifier_logged(caplog):
    records = [r for r in caplog.records if r.name == "oryx.activity_proof_lifecycle"]
    assert records
    for record in records:
        message = record.getMessage()
        assert USER_A not in message and USER_B not in message and not UUID_PATTERN.search(message), message
        assert re.fullmatch(r"activity_proof_(purge outcome=[a-z_]+ reason=([a-z_]+|None)|lifecycle selection="
                            r"[a-z_]+ selected=(True|False) examined=\d+( [a-z_]+=\d+){8})", message), message


def test_the_reports_and_their_representations_carry_no_identifier(db, clock, caplog):
    caplog.set_level(logging.INFO, logger="oryx.activity_proof_lifecycle")
    seed = _rejected(db, clock)
    _clone(db, seed.proof_id, 3)
    clock.set(_due(db, seed.proof_id))
    report = lb.purge_resolved_page(db, limit=2)
    assert (report.examined, report.purged, report.next_cursor is not None) == (2, 2, True)
    for text in (repr(report), repr(report.next_cursor), repr(lb.LifecycleOutcome(lb.PURGED, fz.ABSENT)),
                 repr(committed(db, lambda s: pl.select_purgeable_candidates(s)))):
        assert not UUID_PATTERN.search(text) and USER_A not in text, text
    _no_identifier_logged(caplog)


def _imports(path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.add(node.module)
            if node.module in ("core", "core.product_analytics"):
                found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


def test_the_service_and_the_boundary_import_exactly_their_side_and_know_no_definitive_table():
    assert _imports(SERVICE) == {"uuid", "dataclasses", "datetime", "sqlalchemy", "core.models",
                                 "core.product_analytics", "core.product_analytics.common",
                                 "core.product_analytics.finalization", "core.product_analytics.proofs",
                                 "core.product_analytics.common"}
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    assert {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "core.models"
            for a in n.names} == {"ProductActivityProof", "ProductCampaign", "ProductCampaignParticipant"}
    assert _imports(BOUNDARY) == {"logging", "uuid", "dataclasses", "core", "core._activity_event_finalization",
                                  "core.product_analytics", "core.product_analytics.finalization",
                                  "core.product_analytics.proof_lifecycle"}
    service, boundary = SERVICE.read_text(encoding="utf-8"), BOUNDARY.read_text(encoding="utf-8")
    for source in (service, boundary):
        for forbidden in ("authz_f2", "auth_f2", "f2_runtime", "_activity_proof_bridge", "oryx_sessions",
                          "OryxSession", "_run_attested", "os.environ", "getenv", "Thread", "asyncio", "schedule",
                          "socket", "requests", "httpx", "APIRouter", "FastAPI", "import_module", "__import__",
                          "record_event", "_stage_certified_event", "certify_pending", "register_pending",
                          "open_origin", "ProductActivityEvent", "product_activity_events",
                          "product_activity_event_attributions", "sessions", "INSERT", "UPDATE ", "TRUNCATE",
                          "CASCADE", "session_replication_role", "DISABLE TRIGGER", "while True", "sleep", "print("):
            assert forbidden not in source, forbidden
    # Le service ne possède jamais la transaction ; une seule écriture, dans UN SAVEPOINT.
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"commit", "rollback", "close", "begin", "add", "merge", "flush"}
    assert [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "begin_nested"].__len__() == 1
    assert [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "delete"].__len__() == 1
    # La frontière ne contient aucun SQL et ne réécrit pas le bloc C : elle réutilise sa transaction.
    assert "sqlalchemy" not in boundary and "sa.text" not in boundary and "DELETE" not in boundary
    assert "finalization_boundary._run(" in boundary and "finalization_boundary._transaction(" in boundary


def test_nothing_calls_the_lifecycle_outside_the_tests_and_the_documentation():
    offenders = []
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
        if any(marker in source for marker in ("_activity_proof_lifecycle", "proof_lifecycle", "purge_resolved",
                                               "purge_proof", "recover_certified", "expire_pending_page",
                                               "select_purgeable", "select_certified_candidates",
                                               "select_expired_pending")):
            offenders.append(str(relative))
    assert offenders == []
    # Alembic : tête et empreintes inchangées ; aucune migration nouvelle.
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
PRIVATE = ("core._activity_proof_lifecycle", "core._activity_event_finalization", "core._activity_proof_bridge")
loaded = sorted(n for n in sys.modules if n in PRIVATE or n.startswith("core.product_analytics"))
print(json.dumps({"loaded": loaded, "threads": sorted(t.name for t in threading.enumerate())}))
'''


def test_the_real_api_never_loads_the_lifecycle_whatever_the_environment():
    baseline = None
    for environment in ({}, {"ORYX_PRODUCT_ANALYTICS": "1", "ORYX_V0_T08_ENABLED": "1", "ORYX_PROOF_PURGE": "1",
                             "ORYX_PROOF_LIFECYCLE": "1", "ORYX_LOT_2C": "1", "ORYX_F2_ENABLED": "1"}):
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                             env=_clean_env(environment), timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["loaded"] == []
        baseline = baseline or result["threads"]
        assert result["threads"] == baseline  # aucun thread nouveau, quelle que soit la variable
