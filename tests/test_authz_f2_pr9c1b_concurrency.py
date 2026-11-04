"""R1 PR 9-C1-B — concurrence réelle de la finalisation, contre un VRAI
PostgreSQL (16 et 18 en CI).

Méthode PR 9-A : vraies connexions distinctes (threads), points
d'interruption déterministes qui gardent la transaction et ses verrous
ouverts, attente CONSTATÉE dans pg_stat_activity / pg_blocking_pids,
pg_stat_database.deadlocks relevé avant / après chaque test, aucun refus 503
accepté là où un succès est attendu. Un délai n'est jamais compté comme une
absence d'interblocage ; aucune garantie générale n'est affirmée au-delà des
scénarios ci-dessous.

1. deux finalisations concurrentes du même candidat, et de deux candidats du
   même bail : une seule livraison, la seconde reconnue (AlreadyFinalized),
   aucune substitution ;
2. livraison legacy gagnante PENDANT le claim (SAVEPOINT du service imbriqué
   dans le SAVEPOINT C1) : collision prouvée, blocage, jamais d'adoption ;
3. finalisation contre ACK historique (deux ordres) ;
4. finalisation contre /decryptage historique (deux ordres) : la
   finalisation qui attend relit le contexte sous verrous et refuse un
   contexte B (FinalizationContextChanged), jamais appliqué silencieusement.
"""
import logging
import threading
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core import assistant_delivery as ad
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import decryptage_finalization as dfin
from core.authz_f2.runner import AUTHORIZED
from tests.authz_f2_pr9a_support import LEGACY_APP, USER_A, Background, legacy_ack, legacy_turn, wait_until
from tests.authz_f2_pr9b1_support import CONV, legacy_route  # noqa: F401 — fixture
from tests.authz_f2_pr9b2_support import FakeClaude, FakeClock
from tests.authz_f2_pr9c1b_support import FinalizationEnv, next_turn, schema  # noqa: F401 — fixture
from tests.test_authz_f2_pr9a_concurrency import LegacyPause, waited_relations, waiting_pids
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture


@pytest.fixture
def env(pg_url, schema, caplog):  # noqa: F811
    caplog.set_level(logging.WARNING, logger="oryx.authz_f2")
    environment = FinalizationEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before, "interblocage PostgreSQL détecté"
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


def no_unavailable(caplog):
    assert "status=unavailable" not in caplog.text, caplog.text


class RunnerPause:
    """Point d'interruption du runner (verrous et écritures non commités détenus)."""

    def __init__(self, point):
        self.point = point
        self.reached, self.release = threading.Event(), threading.Event()

    def __call__(self, point):
        if point == self.point:
            self.reached.set()
            assert self.release.wait(30), "pause non relâchée"


class BridgePause:
    """Interrompt la finalisation APRÈS Prepare (session et ancre verrouillées
    dans le SAVEPOINT C1)."""

    def __init__(self, monkeypatch):
        self.reached, self.release = threading.Event(), threading.Event()
        original = bridge._prepare_turn

        def paused(*args, **kwargs):
            prepared = original(*args, **kwargs)
            self.reached.set()
            assert self.release.wait(30), "pause du pont non relâchée"
            return prepared

        monkeypatch.setattr(bridge, "_prepare_turn", paused)


def reply_for(step):
    return f"Étape {step}.\n<!--ORYX_STEP:{step}-->"


def opened(env, issued, conv=CONV):
    """Tour F2 business finalisé + ACK historique : conversation ancrée."""
    _, _, candidate = env.generate(issued, next_turn(conv), text=reply_for("business"))
    done = env.finalize(issued, candidate)
    assert done.status == AUTHORIZED, done
    legacy_ack(env, conv, done.value.assistant_delivery_id)
    return done.value


def contribution(env, issued, conv=CONV, text="Réponse business A.", step="moat"):
    _, lease, candidate = env.generate(issued, next_turn(conv, question=text, context="h"), text=reply_for(step))
    return lease, candidate


# --------------------------------------------------------------------------
# 1. Deux finalisations concurrentes
# --------------------------------------------------------------------------

def test_two_concurrent_finalizations_produce_one_delivery(env, alice, caplog):
    _, lease, candidate = env.generate(alice, next_turn())
    pause = RunnerPause("executed")
    holder = Background(lambda: env.finalize(alice, candidate, hook=pause))
    holder.start()
    assert pause.reached.wait(15)
    contender = Background(lambda: env.finalize(alice, candidate))
    contender.start()
    wait_until(lambda: env.waiting() == 1, message="la seconde finalisation n'attend pas")
    assert waited_relations(env, waiting_pids(env)[0]["pid"]) == {"users"}  # sérialisées au rang users
    pause.release.set()
    first, second = holder.outcome(), contender.outcome()
    assert type(first.value) is dfin.DecryptageFinalized
    assert second.value == dfin.AlreadyFinalized(lease.reservation_id, first.value.assistant_delivery_id)
    assert len(env.delivery_rows()) == 1 and len(env.links()) == 1 and len(env.sessions()) == 1
    no_unavailable(caplog)


def test_two_candidates_of_one_lease_never_substitute_each_other(env, alice, caplog):
    command, reserved = env.reserve(alice, next_turn())
    clock = FakeClock()
    first = env.orchestrator(clock).generate(alice.credential(), command, reserved).candidate
    clock2 = FakeClock()
    second = env.orchestrator(clock2, claude=FakeClaude(clock2, text="Autre texte.\n<!--ORYX_STEP:business-->")
                              ).generate(alice.credential(), command, reserved).candidate
    pause = RunnerPause("executed")
    holder = Background(lambda: env.finalize(alice, first, hook=pause))
    holder.start()
    assert pause.reached.wait(15)
    contender = Background(lambda: env.finalize(alice, second))
    contender.start()
    wait_until(lambda: env.waiting() == 1)
    pause.release.set()
    done, other = holder.outcome(), contender.outcome()
    assert type(other.value) is dfin.AlreadyFinalized
    [delivery] = env.delivery_rows()
    assert "Autre texte" not in delivery["payload"] and delivery["id"] == done.value.assistant_delivery_id
    no_unavailable(caplog)


# --------------------------------------------------------------------------
# 2. Livraison legacy gagnante pendant le claim
# --------------------------------------------------------------------------

@pytest.mark.parametrize("same_fingerprint", [False, True], ids=["different_sha", "identical_sha"])
def test_a_legacy_winner_during_the_claim_is_a_proven_collision(env, alice, caplog, same_fingerprint):
    _, lease, candidate = env.generate(alice, next_turn())
    fingerprint = bridge._delivery_request_fingerprint(candidate) if same_fingerprint else "e" * 64
    holder = Session(env.legacy_engine)
    _, created = ad.claim_delivery(
        holder, user_id=USER_A, conversation_key=candidate.turn.conversation_key, surface=ad.DECRYPTAGE_SURFACE,
        source_user_turn_id=candidate.turn.client_turn_id, delivery_ordinal=1, request_fingerprint=fingerprint,
        visible_content_fingerprint=ad.visible_content_fingerprint("Réponse historique."),
        response_payload={"analysis": "Réponse historique."}, private_metadata={},
        delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION)
    assert created  # non commitée : le claim F2 attendra l'index unique
    log = []
    sa.event.listen(env.engine, "before_cursor_execute", lambda c, cur, st, *a: log.append(" ".join(st.split())))
    worker = Background(lambda: env.finalize(alice, candidate))
    worker.start()
    try:
        wait_until(lambda: env.waiting() == 1, message="le claim F2 n'attend pas l'index unique")
        holder.commit()
    finally:
        holder.close()
    outcome = worker.outcome()
    assert outcome.status == AUTHORIZED and outcome.value == dfin.LegacyCollisionBlocked(lease.reservation_id)
    control = [s for s in log if s.split()[0] in ("SAVEPOINT", "RELEASE", "ROLLBACK")]
    outer = control[0].split()[-1]
    rolled = [s.split()[-1] for s in control if s.startswith("ROLLBACK TO SAVEPOINT")]
    assert len(rolled) == 2 and rolled[0] != outer and rolled[1] == outer  # service, puis SAVEPOINT C1
    row = env.reservation(lease.reservation_id)
    assert (row["state"], row["blocked_reason"], row["assistant_delivery_id"]) == ("blocked", "legacy_collision", None)
    [delivery] = env.delivery_rows()  # la seule livraison est la legacy, jamais adoptée
    assert delivery["request_fingerprint"] == fingerprint
    assert env.links() == [] and env.sessions() == []
    no_unavailable(caplog)


# --------------------------------------------------------------------------
# 3. Finalisation contre ACK historique
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first", ["f2", "legacy"])
def test_finalization_against_a_legacy_ack(env, alice, monkeypatch, caplog, first):
    opened(env, alice)                                                   # A ancrée à l'event business
    _, _, other = env.generate(alice, next_turn("conv-c1b-b"), text=reply_for("business"))
    pending = env.finalize(alice, other).value                           # B : livraison en attente d'ACK
    lease, candidate = contribution(env, alice)
    if first == "f2":
        pause = BridgePause(monkeypatch)
        holder = Background(lambda: env.finalize(alice, candidate))
        holder.start()
        assert pause.reached.wait(15)
        contender = Background(lambda: legacy_ack(env, "conv-c1b-b", pending.assistant_delivery_id))
        contender.start()
        wait_until(lambda: env.waiting(LEGACY_APP) == 1, message="l'ACK historique n'attend pas")
        assert waited_relations(env, waiting_pids(env, LEGACY_APP)[0]["pid"]) == {"analysis_sessions"}
        pause.release.set()
        done = holder.outcome()
        contender.outcome()
    else:
        hold = LegacyPause(monkeypatch, "capture_delivered_response")
        holder = Background(lambda: legacy_ack(env, "conv-c1b-b", pending.assistant_delivery_id))
        holder.start()
        assert hold.reached.wait(15)
        contender = Background(lambda: env.finalize(alice, candidate))
        contender.start()
        wait_until(lambda: env.waiting() == 1, message="la finalisation n'attend pas")
        assert waited_relations(env, waiting_pids(env)[0]["pid"]) == {"analysis_sessions"}
        hold.release.set()
        holder.outcome()
        done = contender.outcome()
    assert done.status == AUTHORIZED and type(done.value) is dfin.DecryptageFinalized  # ACK hors snapshot B2
    actions = {lk["assistant_delivery_id"]: (lk["input_action"], lk["product_progress_action"], lk["response_action"])
               for lk in env.links()}
    assert actions[done.value.assistant_delivery_id][:2] == ("contribution_appended", "forward")
    assert actions[pending.assistant_delivery_id][2] == "continued_event"
    assert env.sessions()[0]["current_step"] == "moat"
    no_unavailable(caplog)


# --------------------------------------------------------------------------
# 4. Finalisation contre /decryptage historique
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first", ["f2", "legacy"])
def test_finalization_against_a_legacy_decryptage(env, alice, legacy_route, monkeypatch, caplog, first):
    opened(env, alice)
    anchor_b = legacy_turn(env, legacy_route, "conv-c1b-b", "business")
    legacy_ack(env, "conv-c1b-b", anchor_b["assistant_turn_id"])  # B ancrée au même event
    lease, candidate = contribution(env, alice)
    if first == "f2":
        pause = BridgePause(monkeypatch)
        holder = Background(lambda: env.finalize(alice, candidate))
        holder.start()
        assert pause.reached.wait(15)
        contender = Background(lambda: legacy_turn(env, legacy_route, "conv-c1b-b", "moat", "Réponse business B."))
        contender.start()
        wait_until(lambda: env.waiting(LEGACY_APP) == 1, message="le /decryptage historique n'attend pas")
        assert waited_relations(env, waiting_pids(env, LEGACY_APP)[0]["pid"]) == {"analysis_sessions"}
        pause.release.set()
        done, historical = holder.outcome(), contender.outcome()
        assert type(done.value) is dfin.DecryptageFinalized
        progress = {lk["assistant_delivery_id"]: lk["product_progress_action"] for lk in env.links()}
        assert (progress[done.value.assistant_delivery_id],
                progress[uuid.UUID(historical["assistant_turn_id"])]) == ("forward", "stale_contribution_step")
    else:
        hold = LegacyPause(monkeypatch, "apply_construction_these_progress")
        holder = Background(lambda: legacy_turn(env, legacy_route, "conv-c1b-b", "moat", "Réponse business B."))
        holder.start()
        assert hold.reached.wait(15)
        contender = Background(lambda: env.finalize(alice, candidate))
        contender.start()
        wait_until(lambda: env.waiting() == 1, message="la finalisation n'attend pas")
        assert waited_relations(env, waiting_pids(env)[0]["pid"]) == {"analysis_sessions"}
        before = env.full_snapshot()
        hold.release.set()
        holder.outcome()
        done = contender.outcome()
        # Contexte B (étape avancée, contribution ajoutée) relu sous verrous :
        # jamais appliqué silencieusement, rien de la finalisation n'est écrit.
        assert done.status == AUTHORIZED and done.value == dfin.FinalizationContextChanged(lease.reservation_id)
        assert env.reservation(lease.reservation_id)["state"] == "reserved"
        assert len(env.delivery_rows()) == len(before["assistant_deliveries"]) + 1  # la seule nouvelle : legacy
    [session] = env.sessions()
    assert (session["current_step"], session["status"]) == ("moat", "in_progress")  # une seule avancée
    no_unavailable(caplog)
