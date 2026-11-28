"""V0-T08 Lot 2C PR 3 — maîtrise des volumes de la purge atomique d'une
participation (core/product_analytics/participation_lifecycle.py), contre un
VRAI PostgreSQL (16 et 18 en CI).

Charges de TEST (jamais des limites métier) : 0, 100, 1 000, 10 000 et
50 000 interactions, réparties sur plusieurs visites, dont des interactions
de clôture sans visite. Les lignes de charge sont écrites par un écrivain
de test ensembliste (generate_series), dans les formes exactes du service
2B-1 (CHECK et FK de 0023 en place). Mesures : durée de chaque instruction,
nombre d'instructions (indépendant du volume : traitement ensembliste),
mémoire Python, attentes de verrou, plan exécuté des DELETE (déclencheurs de
FK compris).

Caractérisation de capacité (limite L14, docs/v0_t08_2c_pr3.md) : le
contrôle de la FK product_activity_session_interactions -> visites, à la
suppression de chaque visite, n'a pas d'index de soutien sur session_id ;
son coût croît avec visites x interactions du couple. Une participation à
très nombreuses visites dépasse le statement_timeout de 5 s : la purge est
unknown, rien n'est supprimé (atomicité préservée), jamais fractionnée,
jamais un timeout modifié.
"""
import re
import threading
import time
import tracemalloc
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core import _activity_participation_lifecycle as lb
from tests.activity_proof_support import PROOF_APP, rows
from tests.test_activity_event_finalization import USER_A, USER_B, clock, db, engine  # noqa: F401 — fixtures
from tests.test_activity_participation_lifecycle import (
    INTERACTIONS,
    PARTICIPANTS,
    SESSIONS,
    _closed_wave,
    _couple,
    _deadline,
    _sql,
    _wave,
    _world,
)
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

# (interactions, visites, clôtures sans visite) : répartitions représentatives.
VOLUMES = [(0, 0, 0), (100, 2, 10), (1_000, 20, 100), (10_000, 100, 1_000), (50_000, 500, 5_000)]


def _forge_load(db, campaign_id, user, *, interactions, visits, closes):
    """Écrivain de TEST ensembliste : visites finalisées puis interactions
    'activity' réparties sur elles, et clôtures sans visite."""
    [participation] = rows(db, f"SELECT window_start_at FROM {PARTICIPANTS} WHERE campaign_id = :c AND user_id = :u",
                           c=campaign_id, u=user)
    t = participation["window_start_at"]
    if visits:
        _sql(db, f"INSERT INTO {SESSIONS} (id, campaign_id, user_id, environment_digest, opened_at, last_activity_at, "
                 "idle_expires_at, finalized_at, end_reason) SELECT gen_random_uuid(), :c, :u, "
                 "encode(sha256(convert_to('visit-' || g, 'UTF8')), 'hex'), :t, :t, :t + interval '30 minutes', "
                 ":t + interval '31 minutes', 'inactivity' FROM generate_series(1, :v) g", c=campaign_id, u=user, t=t,
             v=visits)
        per_visit = (interactions - closes) // visits
        _sql(db, f"INSERT INTO {INTERACTIONS} (campaign_id, user_id, interaction_key, interaction_kind, "
                 "environment_digest, session_id, recorded_at) SELECT :c, :u, gen_random_uuid(), 'activity', "
                 f"s.environment_digest, s.id, :t FROM {SESSIONS} s, generate_series(1, :p) g "
                 "WHERE s.campaign_id = :c AND s.user_id = :u", c=campaign_id, u=user, t=t, p=per_visit)
    if closes:
        _sql(db, f"INSERT INTO {INTERACTIONS} (campaign_id, user_id, interaction_key, interaction_kind, "
                 "environment_digest, session_id, recorded_at) SELECT :c, :u, gen_random_uuid(), 'close', "
                 "encode(sha256(convert_to('close-' || g, 'UTF8')), 'hex'), NULL, :t FROM generate_series(1, :k) g",
             c=campaign_id, u=user, t=t, k=closes)
    _sql(db, f"ANALYZE {INTERACTIONS}; ANALYZE {SESSIONS}")


def _timed_statements(engine):
    timings, starts = [], {}

    def before(conn, cursor, statement, parameters, context, executemany):
        starts[id(cursor)] = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, executemany):
        timings.append((" ".join(statement.split())[:60], time.perf_counter() - starts.pop(id(cursor), 0.0)))
    sa.event.listen(engine, "before_cursor_execute", before)
    sa.event.listen(engine, "after_cursor_execute", after)
    return timings, lambda: (sa.event.remove(engine, "before_cursor_execute", before),
                             sa.event.remove(engine, "after_cursor_execute", after))


class _LockSampler(threading.Thread):
    """Échantillonne les attentes de verrou des sessions de test."""

    def __init__(self, engine):
        super().__init__(daemon=True)  # moteur distinct : ses lectures ne sont jamais comptées
        self.engine = sa.create_engine(engine.url, poolclass=sa.pool.NullPool)
        self.stop, self.max_waiting = threading.Event(), 0

    def run(self):
        while not self.stop.is_set():
            waiting = rows(self.engine, "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = :a "
                                        "AND wait_event_type = 'Lock'", a=PROOF_APP)[0]["n"]
            self.max_waiting = max(self.max_waiting, waiting)
            self.stop.wait(0.05)
        self.engine.dispose()


def _measured_purge(db, campaign_id, user=USER_A):
    timings, remove = _timed_statements(db)
    sampler = _LockSampler(db)
    sampler.start()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        outcome = lb.purge_participation(db, campaign_id=campaign_id, user_id=user)
    finally:
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        sampler.stop.set()
        sampler.join()
        remove()
    return outcome, elapsed, peak, timings, sampler.max_waiting


@pytest.mark.parametrize("interactions, visits, closes", VOLUMES)
def test_the_purge_is_set_based_and_atomic_up_to_fifty_thousand_interactions(db, clock, interactions, visits,
                                                                            closes, capsys):
    campaign_id = _closed_wave(db, clock, visits=0)
    witness = _wave(db, USER_B)  # autre participation, autre campagne : intacte
    _forge_load(db, campaign_id, USER_A, interactions=interactions, visits=visits, closes=closes)
    clock.set(_deadline(db, campaign_id))
    world = _world(db, without={(campaign_id, USER_A)})
    outcome, elapsed, peak, timings, waits = _measured_purge(db, campaign_id)
    assert outcome == lb.PurgeOutcome(lb.PURGED, None, interactions, visits)
    assert _couple(db, campaign_id) == {"participation": None, "sessions": [], "interactions": []}
    assert _world(db) == world and witness is not None
    # Ensembliste : nombre d'instructions indépendant du volume ; aucune ligne chargée en mémoire Python.
    assert len(timings) == 21, [t[0] for t in timings]
    assert peak < 5_000_000, peak
    assert waits == 0
    slowest = max(timings, key=lambda t: t[1])
    with capsys.disabled():
        print(f"\n[stress] interactions={interactions} visites={visits} clotures={closes} "
              f"duree={elapsed:.3f}s instructions={len(timings)} memoire_pic={peak / 1e6:.2f}Mo "
              f"attentes_verrou={waits} plus_lente={slowest[0][:40]!r}:{slowest[1]:.3f}s")


def test_the_executed_plans_of_the_deletes_show_the_foreign_key_triggers(db, clock, capsys):
    """Plan exécuté (EXPLAIN ANALYZE, transaction annulée) des trois DELETE
    du parcours B sur 10 000 interactions : coût dominant = déclencheurs de
    FK ; l'index de clé primaire sert les interactions ; aucune instruction
    hors du couple."""
    campaign_id = _closed_wave(db, clock, visits=0)
    _forge_load(db, campaign_id, USER_A, interactions=10_000, visits=100, closes=1_000)
    plans = {}
    with Session(bind=db) as session:
        for table in (INTERACTIONS, SESSIONS, PARTICIPANTS):
            plans[table] = "\n".join(r[0] for r in session.execute(sa.text(
                f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) DELETE FROM {table} WHERE campaign_id = :c "
                "AND user_id = :u"), {"c": campaign_id, "u": USER_A}))
        session.rollback()
    for table in (INTERACTIONS, SESSIONS, PARTICIPANTS):
        assert f"Delete on {table}" in plans[table]
    for constraint in ("product_activity_proof_registry_interaction_fkey",
                       "product_activity_event_attributions_interaction_fkey"):
        assert re.search(rf"Trigger for constraint {constraint}: calls=\d+", plans[INTERACTIONS]), constraint
    assert "Trigger for constraint product_activity_session_interactions_session_fkey: calls=100" in plans[SESSIONS]
    assert "Seq Scan on product_activity_sessions" in plans[SESSIONS]  # aucun index (campaign_id, user_id) : L14
    assert "Trigger for constraint product_activity_sessions_participant_fkey: calls=1" in plans[PARTICIPANTS]
    assert len(_couple(db, campaign_id)["interactions"]) == 10_000  # plans exécutés puis annulés
    with capsys.disabled():
        for table, plan in plans.items():
            print(f"\n[plan] DELETE {table}\n{plan}")


def test_a_capacity_overflow_is_unknown_and_leaves_no_partial_state(db, clock, capsys):
    """L14 : 50 000 interactions sur 25 000 visites. Le contrôle de FK des
    visites, sans index de soutien sur session_id, dépasse le
    statement_timeout de 5 s : unknown, AUCUNE suppression durable, aucune
    purge fractionnée ; la réexécution échoue de même (prérequis bloquant
    avant activation pour de tels volumes : index de soutien, migration hors
    périmètre)."""
    campaign_id = _closed_wave(db, clock, visits=0)
    _forge_load(db, campaign_id, USER_A, interactions=50_000, visits=25_000, closes=0)
    clock.set(_deadline(db, campaign_id))
    before = _couple(db, campaign_id)
    outcome, elapsed, peak, timings, waits = _measured_purge(db, campaign_id)
    assert outcome == lb.PurgeOutcome(lb.UNKNOWN, lb.TECHNICAL_UNCERTAINTY)
    assert _couple(db, campaign_id) == before  # xmin compris : aucun état partiel durable
    assert elapsed >= 5.0 and waits == 0
    with capsys.disabled():
        print(f"\n[stress] capacite interactions=50000 visites=25000 issue={outcome.outcome} duree={elapsed:.3f}s "
              f"memoire_pic={peak / 1e6:.2f}Mo")
