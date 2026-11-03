"""R1 PR 9-C1-A — verrous et concurrence réelle des primitives C1-A, contre
un VRAI PostgreSQL (16 et 18 en CI).

Méthode PR 9-A : vraies connexions distinctes (threads), points
d'interruption déterministes qui gardent la transaction et ses verrous
ouverts, attente CONSTATÉE (pg_stat_activity, pg_blocking_pids), sondes
NOWAIT pour l'état des verrous ; pg_stat_database.deadlocks relevé avant /
après chaque test et aucun refus 503 accepté là où un succès est attendu :
un délai converti en 503 n'est jamais compté comme une absence
d'interblocage. Aucune garantie générale « sans interblocage » n'est
affirmée : seuls les scénarios ci-dessous sont démontrés.

1. verrous pris AVANT le SAVEPOINT C1 conservés après une annulation
   locale ; verrous pris DANS le SAVEPOINT libérés ;
2. profil des verrous détenus entre Prepare et Apply ;
3. décision préparée contre ACK legacy et contre finalisation legacy
   (/decryptage), dans les deux ordres : attente au rang analysis_sessions,
   une seule avancée, identité de l'AnalysisSession préparée conservée ;
4. absence préparée contredite par une session commitée : refus fermé
   (jamais d'adoption du contexte B) ; création concurrente NON commitée :
   limite documentée (non résolue par C1-A).
"""
import logging
import threading
import uuid

import pytest
import sqlalchemy as sa

from core import decryptage_progress as dp
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2.runner import AUTHORIZED, UNAVAILABLE
from tests.authz_f2_pr9a_support import (  # noqa: F401 — fixtures
    LEGACY_APP,
    TICKER,
    USER_A,
    Background,
    BenchEnv,
    ack,
    bench,
    legacy,
    legacy_ack,
    legacy_turn,
    turn,
    wait_until,
)
from tests.authz_f2_pr9c1a_support import RehearsalOutcome, c1a, rehearsal  # noqa: F401 — fixture
from tests.f2_support import prepare_schema
from tests.test_authz_f2_pr9a_concurrency import LegacyPause, waited_relations, waiting_pids
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

CONV_A, CONV_B = "conv-c1a-a", "conv-c1a-b"


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def benv(pg_url, schema, c1a, caplog):  # noqa: F811
    caplog.set_level(logging.WARNING, logger="oryx.authz_f2")
    environment = BenchEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before, "interblocage PostgreSQL détecté"
    environment.dispose()


@pytest.fixture
def alice(benv):
    return benv.confirmed()


def run(benv, issued, command, **kwargs):
    return benv.runner(**kwargs).run(issued.credential(), command)


def ok(outcome):
    assert outcome.status == AUTHORIZED, outcome
    return outcome.value


def no_unavailable(caplog):
    assert "status=unavailable" not in caplog.text, caplog.text


def opened(benv, issued, conversation_key, marker="business", text=""):
    done = ok(run(benv, issued, turn(conversation_key, marker, text)))
    ok(run(benv, issued, ack(conversation_key, done.assistant_turn_id)))
    return done


class StagePause:
    """Interrompt la répétition à une étape C1-A (verrous détenus) ; `then`
    s'exécute à la reprise (par exemple une annulation locale)."""

    def __init__(self, script, stage, then=None):
        self.reached, self.release = threading.Event(), threading.Event()
        self.state = None

        def pause(state):
            self.state = state
            self.reached.set()
            assert self.release.wait(30), "pause C1-A non relâchée"
            if then is not None:
                then(state)

        script.at(stage, pause)


def probe(benv, table, row_id, mode="FOR UPDATE"):
    """Sonde NOWAIT d'une connexion tierce : 'granted' ou le SQLSTATE."""
    with benv.observer.begin() as conn:
        try:
            conn.execute(sa.text(f"SELECT 1 FROM {table} WHERE id = :i {mode} NOWAIT"), {"i": row_id})
            return "granted"
        except sa.exc.OperationalError as exc:
            return exc.orig.pgcode


def start(benv, issued, command):
    worker = Background(lambda: run(benv, issued, command))
    worker.start()
    return worker


# --------------------------------------------------------------------------
# 1-2. Verrous autour du SAVEPOINT C1
# --------------------------------------------------------------------------

@pytest.mark.parametrize("locked_before", [True, False])
def test_locks_before_the_savepoint_survive_a_local_rollback_locks_inside_do_not(benv, alice, c1a, locked_before):
    opened(benv, alice, CONV_A)
    [session] = benv.sessions()
    [event] = benv.events()

    def lock_session_first(state):
        if locked_before:  # verrou pris AVANT le SAVEPOINT C1, dans la transaction du runner
            state.db.bind.execute(sa.text("SELECT 1 FROM analysis_sessions WHERE id = :s FOR UPDATE"),
                                  {"s": session["id"]})

    def rollback(state):
        raise bridge._LocalRollback(bridge.CONTEXT_CHANGED)

    c1a.at("before_savepoint", lock_session_first)
    inside = StagePause(c1a, "prepared", then=rollback)
    after = StagePause(c1a, "after_savepoint")
    worker = start(benv, alice, rehearsal(CONV_A, "moat", "Réponse business."))
    try:
        assert inside.reached.wait(15)
        # Entre Prepare et Apply : session et ancre verrouillées par la préparation.
        assert probe(benv, "analysis_sessions", session["id"]) == "55P03"
        assert probe(benv, "cognitive_events", event["id"]) == "55P03"
        assert probe(benv, "users", USER_A, "FOR KEY SHARE") == "granted"
        assert probe(benv, "users", USER_A, "FOR NO KEY UPDATE") == "55P03"
    finally:
        inside.release.set()
    try:
        assert after.reached.wait(15)
        assert after.state.scope.rolled_back
        # Après ROLLBACK TO SAVEPOINT : l'event (verrouillé DANS le SAVEPOINT)
        # est libéré ; la session ne reste verrouillée que si elle l'était AVANT.
        assert probe(benv, "cognitive_events", event["id"]) == "granted"
        assert probe(benv, "analysis_sessions", session["id"]) == ("55P03" if locked_before else "granted")
        assert probe(benv, "users", USER_A, "FOR NO KEY UPDATE") == "55P03"  # verrou du runner, avant tout
    finally:
        after.release.set()
    outcome = ok(worker.outcome())
    assert type(outcome) is RehearsalOutcome and outcome.savepoint == "rolled_back"
    assert [e["user_work_snapshot"] for e in benv.events()] == [[]]
    assert benv.sessions()[0]["current_step"] == "business"


# --------------------------------------------------------------------------
# 3. Décision préparée contre ACK et /decryptage historiques
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first", ["f2", "legacy"])
def test_prepared_turn_against_legacy_ack(benv, alice, c1a, legacy, monkeypatch, caplog, first):
    opened(benv, alice, CONV_A)                                   # A ancrée à l'event business
    other = ok(run(benv, alice, turn(CONV_B, "business")))         # B : livraison en attente d'ACK
    [session] = benv.sessions()
    contribution = rehearsal(CONV_A, "moat", "Ma réponse business.")
    if first == "f2":
        pause = StagePause(c1a, "prepared")
        holder = start(benv, alice, contribution)
        assert pause.reached.wait(15)
        contender = Background(lambda: legacy_ack(benv, CONV_B, other.assistant_turn_id))
        contender.start()
        wait_until(lambda: benv.waiting(LEGACY_APP) == 1, message="l'ACK historique n'attend pas")
        assert waited_relations(benv, waiting_pids(benv, LEGACY_APP)[0]["pid"]) == {"analysis_sessions"}
        pause.release.set()
        done = ok(holder.outcome())
        contender.outcome()
    else:
        hold = LegacyPause(monkeypatch, "capture_delivered_response")
        holder = Background(lambda: legacy_ack(benv, CONV_B, other.assistant_turn_id))
        holder.start()
        assert hold.reached.wait(15)
        contender = start(benv, alice, contribution)
        wait_until(lambda: benv.waiting() == 1, message="la préparation n'attend pas")
        assert waited_relations(benv, waiting_pids(benv)[0]["pid"]) == {"analysis_sessions"}
        hold.release.set()
        holder.outcome()
        done = ok(contender.outcome())
    assert done.savepoint == "released"
    assert (done.applied.input_action, done.applied.product_progress_action) == ("contribution_appended", "forward")
    assert done.context.analysis_session_id == done.applied.analysis_session_id == session["id"]
    events = benv.events()
    assert [e["status"] for e in events] == ["open"] and len(events[0]["user_work_snapshot"]) == 1
    acks = {lk["assistant_delivery_id"]: lk["response_action"] for lk in benv.links()}
    assert acks[other.assistant_turn_id] == "continued_event"
    assert benv.sessions()[0]["current_step"] == "moat"
    no_unavailable(caplog)


@pytest.mark.parametrize("first", ["f2", "legacy"])
def test_prepared_turn_against_legacy_finalization(benv, alice, c1a, legacy, monkeypatch, caplog, first):
    opened(benv, alice, CONV_A)
    anchor_b = legacy_turn(benv, legacy, CONV_B, "business")
    legacy_ack(benv, CONV_B, anchor_b["assistant_turn_id"])  # B ancrée au même event (continued_event)
    [session] = benv.sessions()
    contribution = rehearsal(CONV_A, "moat", "Réponse business A.")
    if first == "f2":
        pause = StagePause(c1a, "prepared")
        holder = start(benv, alice, contribution)
        assert pause.reached.wait(15)
        contender = Background(lambda: legacy_turn(benv, legacy, CONV_B, "moat", "Réponse business B."))
        contender.start()
        wait_until(lambda: benv.waiting(LEGACY_APP) == 1, message="le /decryptage historique n'attend pas")
        assert waited_relations(benv, waiting_pids(benv, LEGACY_APP)[0]["pid"]) == {"analysis_sessions"}
        pause.release.set()
        f2, historical = ok(holder.outcome()), contender.outcome()
        expected = ("forward", "stale_contribution_step")
    else:
        hold = LegacyPause(monkeypatch, "apply_construction_these_progress")
        holder = Background(lambda: legacy_turn(benv, legacy, CONV_B, "moat", "Réponse business B."))
        holder.start()
        assert hold.reached.wait(15)
        contender = start(benv, alice, contribution)
        wait_until(lambda: benv.waiting() == 1, message="la préparation n'attend pas")
        assert waited_relations(benv, waiting_pids(benv)[0]["pid"]) == {"analysis_sessions"}
        hold.release.set()
        historical, f2 = holder.outcome(), ok(contender.outcome())
        expected = ("stale_contribution_step", "forward")
    # La préparation qui a ATTENDU relit l'état commité sous verrou (étape
    # moat) et l'applique tel quel : jamais l'état d'avant l'attente.
    assert f2.context.analysis_session_id == f2.applied.analysis_session_id == session["id"]
    assert f2.context.current_step == ("business" if first == "f2" else "moat")
    [current] = benv.sessions()
    assert current["current_step"] == "moat" and current["status"] == "in_progress"
    progress = {lk["assistant_delivery_id"]: lk["product_progress_action"] for lk in benv.links()}
    assert (progress[f2.assistant_turn_id], progress[uuid.UUID(historical["assistant_turn_id"])]) == expected
    [event] = benv.events()
    assert event["status"] == "open" and len(event["user_work_snapshot"]) == 2
    assert [s["step"] for s in benv.rows("SELECT step FROM user_statements ORDER BY id")] == ["business", "business"]
    no_unavailable(caplog)


# --------------------------------------------------------------------------
# 4. Absence préparée et création concurrente d'AnalysisSession
# --------------------------------------------------------------------------

def test_prepared_absence_contradicted_by_a_committed_session_is_refused(benv, alice, c1a, legacy, caplog):
    pause = StagePause(c1a, "prepared")
    worker = start(benv, alice, rehearsal(CONV_A, "business"))
    try:
        assert pause.reached.wait(15)
        assert pause.state.prepared.context.analysis_session_id is None  # absence préparée
        # Aucune ligne à verrouiller : le /decryptage historique crée et commite
        # sa session sans attendre.
        created = legacy_turn(benv, legacy, CONV_B, "business")
        assert created["delivery_status"] == "pending"
    finally:
        pause.release.set()
    before_sessions = benv.sessions()
    assert worker.outcome().status == UNAVAILABLE  # jamais d'adoption de la session B
    assert "error=PreparedAnalysisSessionMismatch" in caplog.text
    assert benv.sessions() == before_sessions and len(before_sessions) == 1
    assert [d["conversation_key"] for d in benv.deliveries()] == [CONV_B]


def test_known_limitation_uncommitted_concurrent_session_creation_is_not_detected(benv, alice, c1a, legacy,
                                                                                  monkeypatch, caplog):
    """LIMITE DOCUMENTÉE, NON RÉSOLUE PAR C1-A : quand l'AnalysisSession est
    initialement absente, aucune ligne n'est verrouillable ; une création
    concurrente encore NON commitée au moment d'Apply est invisible (READ
    COMMITTED, aucune contrainte d'unicité) : deux sessions in_progress
    peuvent coexister, exactement comme entre deux /decryptage historiques.
    Ce test fige le comportement actuel pour qu'une correction future soit
    explicite (writers legacy non coordonnés avec F2)."""
    hold = LegacyPause(monkeypatch, "apply_construction_these_progress")
    holder = Background(lambda: legacy_turn(benv, legacy, CONV_B, "business"))
    holder.start()
    assert hold.reached.wait(15)  # session historique créée, NON commitée
    try:
        done = ok(run(benv, alice, rehearsal(CONV_A, "business")))
    finally:
        hold.release.set()
    holder.outcome()
    assert done.applied.product_progress_action == "new"
    sessions = benv.sessions()
    assert [s["status"] for s in sessions] == ["in_progress", "in_progress"]  # anomalie connue
    no_unavailable(caplog)


def test_prepared_session_identity_survives_a_concurrent_new_attempt(benv, alice, c1a, legacy, caplog):
    """Session préparée verrouillée : un /decryptage historique d'une autre
    conversation sur le même ticker attend la session (jamais une seconde
    tentative), puis voit l'état commité par la répétition."""
    opened(benv, alice, CONV_A)
    [session] = benv.sessions()
    pause = StagePause(c1a, "prepared")
    worker = start(benv, alice, rehearsal(CONV_A, "moat", "Réponse business."))
    try:
        assert pause.reached.wait(15)
        contender = Background(lambda: legacy_turn(benv, legacy, CONV_B, "business"))
        contender.start()
        wait_until(lambda: benv.waiting(LEGACY_APP) == 1)
        assert waited_relations(benv, waiting_pids(benv, LEGACY_APP)[0]["pid"]) == {"analysis_sessions"}
    finally:
        pause.release.set()
    done = ok(worker.outcome())
    historical = contender.outcome()
    assert done.applied.analysis_session_id == session["id"]
    assert [s["id"] for s in benv.sessions()] == [session["id"]]  # aucune seconde tentative
    progress = {lk["assistant_delivery_id"]: (lk["input_action"], lk["product_progress_action"])
                for lk in benv.links()}
    assert progress[done.assistant_turn_id] == ("contribution_appended", "forward")
    assert progress[uuid.UUID(historical["assistant_turn_id"])] == ("no_open_event", "retrograde_marker")
    no_unavailable(caplog)


def test_progress_variant_never_reads_the_active_session(benv, alice, c1a, monkeypatch):
    """Garde statique complémentaire, exécutée : pendant toute la
    répétition, find_active_analysis_session est inatteignable."""
    opened(benv, alice, CONV_A)
    monkeypatch.setattr(dp, "find_active_analysis_session",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("relecture de la session active")))
    done = ok(run(benv, alice, rehearsal(CONV_A, "moat", "Réponse business.")))
    assert done.applied.product_progress_action == "forward"
