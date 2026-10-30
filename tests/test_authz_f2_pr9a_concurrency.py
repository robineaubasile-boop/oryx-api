"""R1 PR 9-A — verrous et concurrence réelle du plan DECRYPTAGE_MUTATION et
du pont ORM privé, contre un VRAI PostgreSQL (16 et 18 en CI).

Vraies connexions distinctes (threads, une connexion par acteur),
synchronisation déterministe : points d'interruption (Pause) qui gardent
la transaction et ses verrous ouverts, attente CONSTATÉE dans
pg_stat_activity (wait_event_type = 'Lock') et pg_blocking_pids(), jamais
un sleep arbitraire. pg_stat_database.deadlocks est relevé avant / après
chaque test (aucun interblocage toléré) et aucun refus 503 n'est accepté
là où un succès est attendu : un délai d'attente converti en 503 n'est
jamais compté comme une absence d'interblocage.

Matrice (V2 A-02 / A-03, V2.2-01) : deux mutations Décrypter du même
utilisateur ; deux utilisateurs ; FOR NO KEY UPDATE et FK ; profil F2 et
portefeuille F2 contre Décrypter F2 (deux ordres) ; révocation pendant
l'attente users, pendant l'attente préparatoire (session, génération) et
après l'autorisation ; ACK F2 contre ACK historique (deux ordres) ;
finalisation F2 contre ACK historique et contre /decryptage historique
(deux ordres) ; deux conversations sur la même analyse ; event fermé
pendant l'attente ; livraison étrangère verrouillée par un tiers.
Profils de verrous MESURÉS (sondes NOWAIT) et classification des échecs
PostgreSQL (40P01, 55P03, 57014, 23505).
"""
import logging
import threading
import uuid

import pytest
import sqlalchemy as sa

import api
from core import assistant_delivery as ad
from core.authz_f2 import commands as c
from core.authz_f2.runner import AUTHORIZED, NOT_FOUND, SESSION_INVALID, UNAVAILABLE
from tests.authz_f2_pr9a_support import (  # noqa: F401 — fixtures
    BOB_EMAIL,
    LEGACY_APP,
    TICKER,
    USER_A,
    USER_B,
    Background,
    BenchEnv,
    Pause,
    ack,
    bench,
    legacy,
    legacy_ack,
    legacy_turn,
    turn,
    wait_until,
)
from tests.authz_f2_support import create_command
from tests.f2_support import APP_NAME, prepare_schema, scalar
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

CONV_A, CONV_B = "conv-c-a", "conv-c-b"


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def benv(pg_url, schema, bench, caplog):  # noqa: F811
    caplog.set_level(logging.WARNING, logger="oryx.authz_f2")
    environment = BenchEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before, "interblocage PostgreSQL détecté"
    environment.dispose()


@pytest.fixture
def alice(benv):
    return benv.confirmed()


@pytest.fixture
def bob(benv):
    return benv.confirmed(BOB_EMAIL, USER_B)


def run(benv, issued, command, **kwargs):
    return benv.runner(**kwargs).run(issued.credential(), command)


def ok(outcome):
    assert outcome.status == AUTHORIZED, outcome
    return outcome.value


def no_unavailable(caplog):
    assert "status=unavailable" not in caplog.text, caplog.text


def waiting_pids(benv, application_name=APP_NAME):
    return benv.rows("SELECT pid, pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                     "WHERE application_name = :a AND wait_event_type = 'Lock'", a=application_name)


def waited_relations(benv, pid):
    """Relations des verrous de LIGNE (tuple) détenus par un acteur qui
    attend une ligne : la table de la ligne attendue."""
    return {r["rel"] for r in benv.rows("SELECT relation::regclass::text AS rel FROM pg_locks "
                                        "WHERE pid = :p AND locktype = 'tuple'", p=pid)}


def held_by(benv, holder_app, waiter_app):
    """L'acteur `waiter_app` attend un verrou de l'acteur `holder_app`."""
    holders = {r["pid"] for r in benv.rows("SELECT pid FROM pg_stat_activity WHERE application_name = :a "
                                           "AND state LIKE 'idle in transaction%'", a=holder_app)}
    waiters = waiting_pids(benv, waiter_app)
    return len(waiters) == 1 and set(waiters[0]["blockers"]) & holders


def start_paused(benv, issued, command, point="executed"):
    pause = Pause(point)
    worker = Background(lambda: run(benv, issued, command, hook=pause))
    worker.start()
    assert pause.reached.wait(15), "point d'interruption non atteint"
    return pause, worker


def opened(benv, issued, conversation_key, marker="business", text=""):
    """Tour F2 + ACK : la conversation est ancrée à l'event de `marker`."""
    done = ok(run(benv, issued, turn(conversation_key, marker, text)))
    ok(run(benv, issued, ack(conversation_key, done.assistant_turn_id)))
    return done


class LegacyPause:
    """Interrompt un appel historique APRÈS la fonction de service visée
    (verrous de la transaction historique détenus) — patch du seul nom
    importé par api.py, jamais du service lui-même (le pont F2 n'est pas
    touché)."""

    def __init__(self, monkeypatch, name):
        self.reached, self.release = threading.Event(), threading.Event()
        original = getattr(api, name)

        def paused(*args, **kwargs):
            result = original(*args, **kwargs)
            self.reached.set()
            assert self.release.wait(30), "pause historique non relâchée"
            return result

        monkeypatch.setattr(api, name, paused)


# --------------------------------------------------------------------------
# Plan DECRYPTAGE_MUTATION : users FOR NO KEY UPDATE
# --------------------------------------------------------------------------

def test_two_decryptage_mutations_of_one_user_are_serialized_on_users(benv, alice, caplog):
    second = benv.confirmed("alice2@example.com", USER_A)
    pause, holder = start_paused(benv, alice, turn(CONV_A, "business"))
    contender = Background(lambda: run(benv, second, turn(CONV_B, "business")))
    contender.start()
    wait_until(lambda: benv.waiting() == 1, message="la seconde mutation n'attend pas")
    [waiter] = waiting_pids(benv)
    assert waited_relations(benv, waiter["pid"]) == {"users"}  # attente au rang users, avant tout verrou métier
    assert benv.hook_calls == ["prevalidated"]  # le second n'a pas atteint ses verrous de décision
    pause.release.set()
    first, other = ok(holder.outcome()), ok(contender.outcome())
    assert first.created and other.created and first.analysis_session_id == other.analysis_session_id
    no_unavailable(caplog)


def test_two_users_are_never_serialized_by_the_plan(benv, alice, bob, caplog):
    pause, holder = start_paused(benv, alice, turn(CONV_A, "business"))
    try:
        assert ok(run(benv, bob, turn(CONV_B, "business"))).created  # aboutit pendant la pause d'Alice
        assert benv.waiting() == 0
    finally:
        pause.release.set()
    ok(holder.outcome())
    no_unavailable(caplog)


def test_no_key_update_is_compatible_with_foreign_key_checks(benv, alice):
    pause, holder = start_paused(benv, alice, turn(CONV_A, "business"))
    try:
        probes = {}
        for mode in ("FOR KEY SHARE", "FOR SHARE", "FOR NO KEY UPDATE", "FOR UPDATE"):
            with benv.observer.begin() as conn:
                try:
                    conn.execute(sa.text(f"SELECT id FROM users WHERE id = :u {mode} NOWAIT"), {"u": USER_A})
                    probes[mode] = "granted"
                except sa.exc.OperationalError as exc:
                    probes[mode] = exc.orig.pgcode
        assert probes == {"FOR KEY SHARE": "granted", "FOR SHARE": "55P03", "FOR NO KEY UPDATE": "55P03",
                          "FOR UPDATE": "55P03"}
        # Une écriture historique avec FK vers users n'attend pas.
        with benv.observer.begin() as conn:
            conn.execute(sa.text("SET LOCAL lock_timeout = '500ms'"))
            conn.execute(sa.text("INSERT INTO portfolio_positions (user_id, ticker, quantity) VALUES (:u, 'X', 1)"),
                         {"u": USER_A})
    finally:
        pause.release.set()
    ok(holder.outcome())


@pytest.mark.parametrize("first, second", [("decryptage", "profile"), ("profile", "decryptage"),
                                           ("decryptage", "portfolio"), ("portfolio", "decryptage")])
def test_profile_and_portfolio_mutations_wait_coherently(benv, alice, caplog, first, second):
    commands = {"decryptage": turn(CONV_A, "business"), "profile": c.UpdateProfileLevel("avance", 0),
                "portfolio": create_command()}
    pause, holder = start_paused(benv, alice, commands[first])
    contender = Background(lambda: run(benv, alice, commands[second]))
    contender.start()
    wait_until(lambda: benv.waiting() == 1, message="aucune attente au rang users")
    assert waited_relations(benv, waiting_pids(benv)[0]["pid"]) == {"users"}
    pause.release.set()
    ok(holder.outcome())
    ok(contender.outcome())
    no_unavailable(caplog)


# --------------------------------------------------------------------------
# Révocation et T_authorization
# --------------------------------------------------------------------------

def test_revocation_committed_while_waiting_on_users_is_refused(benv, alice):
    before = benv.snapshot()
    blocker = benv.observer.connect()
    tx = blocker.begin()
    blocker.execute(sa.text("SELECT id FROM users WHERE id = :u FOR UPDATE"), {"u": USER_A})
    worker = Background(lambda: run(benv, alice, turn(CONV_A, "business")))
    worker.start()
    try:
        wait_until(lambda: benv.waiting() == 1)
        assert benv.service.revoke_session(alice.session_id, "security")  # n'attend pas users
    finally:
        tx.rollback()
        blocker.close()
    assert worker.outcome().status == SESSION_INVALID
    assert benv.snapshot() == before


@pytest.mark.parametrize("rank", ["oryx_sessions", "oryx_session_generations"])
def test_revocation_committed_during_the_preparatory_wait_is_refused(benv, alice, rank):
    """DECRYPTAGE_MUTATION ne déclare aucun verrou métier préparatoire :
    l'attente préparatoire est celle des rangs session et génération,
    AVANT T_authorization. Une révocation committée pendant cette attente
    est vue (FOR SHARE relit la dernière version) : refus, rien n'est écrit."""
    before = benv.snapshot()
    blocker = benv.observer.connect()
    tx = blocker.begin()
    target = alice.session_id if rank == "oryx_sessions" else alice.generation_id
    blocker.execute(sa.text(f"UPDATE {rank} SET status = 'revoked', revoked_at = clock_timestamp(), "
                            "revocation_reason = 'security' WHERE id = :i"), {"i": target})
    worker = Background(lambda: run(benv, alice, turn(CONV_A, "business")))
    worker.start()
    try:
        wait_until(lambda: benv.waiting() == 1)
        [waiter] = waiting_pids(benv)
        assert benv.rows("SELECT relation::regclass::text AS rel FROM pg_locks WHERE pid = :p AND locktype = 'tuple'",
                         p=waiter["pid"]) == [{"rel": rank}]
        assert "authorized" not in benv.hook_calls
        tx.commit()
    finally:
        if tx.is_active:
            tx.rollback()
        blocker.close()
    assert worker.outcome().status == SESSION_INVALID
    assert benv.snapshot() == before


def test_expiry_during_the_wait_is_judged_at_t_authorization(benv, alice):
    before = benv.snapshot()
    blocker = benv.observer.connect()
    tx = blocker.begin()
    blocker.execute(sa.text("SELECT id FROM users WHERE id = :u FOR UPDATE"), {"u": USER_A})
    worker = Background(lambda: run(benv, alice, turn(CONV_A, "business")))
    worker.start()
    try:
        wait_until(lambda: benv.waiting() == 1)
        benv.expire_at(alice.session_id, "clock_timestamp()")  # échue avant T_authorization
    finally:
        tx.rollback()
        blocker.close()
    assert worker.outcome().status == SESSION_INVALID
    assert benv.snapshot() == before


def test_revocation_after_authorization_waits_for_the_commit(benv, alice, caplog):
    pause, holder = start_paused(benv, alice, turn(CONV_A, "business"), point="authorized")
    revoker = Background(lambda: benv.service.revoke_session(alice.session_id, "logout"))
    revoker.start()
    wait_until(lambda: benv.waiting() == 1, message="la révocation n'attend pas la transaction autorisée")
    pause.release.set()
    assert ok(holder.outcome()).created  # autorisée à T_authorization : commitée
    assert revoker.outcome() is True
    assert len(benv.deliveries()) == 1
    assert run(benv, alice, turn(CONV_A, "moat", "x")).status == SESSION_INVALID
    no_unavailable(caplog)


# --------------------------------------------------------------------------
# ACK F2 simulé contre ACK historique réel
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first", ["f2", "legacy"])
def test_f2_ack_against_legacy_ack_captures_once(benv, alice, legacy, monkeypatch, caplog, first):
    pending = ok(run(benv, alice, turn(CONV_A, "business")))
    turn_id = pending.assistant_turn_id
    if first == "f2":
        pause, holder = start_paused(benv, alice, ack(CONV_A, turn_id))
        contender = Background(lambda: legacy_ack(benv, CONV_A, turn_id))
        contender.start()
        wait_until(lambda: benv.waiting(LEGACY_APP) == 1, message="l'ACK historique n'attend pas")
        assert waited_relations(benv, waiting_pids(benv, LEGACY_APP)[0]["pid"]) == {"assistant_deliveries"}
        pause.release.set()
        assert ok(holder.outcome()).response_action == "opened_event"
        assert contender.outcome()["delivery_status"] == "delivered"
    else:
        hold = LegacyPause(monkeypatch, "acknowledge_delivery")
        holder = Background(lambda: legacy_ack(benv, CONV_A, turn_id))
        holder.start()
        assert hold.reached.wait(15)
        contender = Background(lambda: run(benv, alice, ack(CONV_A, turn_id)))
        contender.start()
        wait_until(lambda: benv.waiting() == 1, message="l'ACK F2 n'attend pas")
        assert waited_relations(benv, waiting_pids(benv)[0]["pid"]) == {"assistant_deliveries"}
        hold.release.set()
        assert holder.outcome()["delivery_status"] == "delivered"
        assert ok(contender.outcome()).response_action == "opened_event"  # rejeu idempotent
    [event] = benv.events()
    [link] = benv.links()
    assert event["status"] == "open" and link["capture_state"] == "captured" and link["response_action"] == "opened_event"
    no_unavailable(caplog)


# --------------------------------------------------------------------------
# Finalisation F2 simulée contre ACK et /decryptage historiques
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first", ["f2", "legacy"])
def test_f2_finalization_against_legacy_ack(benv, alice, legacy, monkeypatch, caplog, first):
    opened(benv, alice, CONV_A)                                    # A ancrée à l'event business
    other = ok(run(benv, alice, turn(CONV_B, "business")))          # B : livraison en attente d'ACK
    contribution = turn(CONV_A, "moat", "Ma réponse business.")
    if first == "f2":
        pause, holder = start_paused(benv, alice, contribution)
        contender = Background(lambda: legacy_ack(benv, CONV_B, other.assistant_turn_id))
        contender.start()
        wait_until(lambda: benv.waiting(LEGACY_APP) == 1)
        assert waited_relations(benv, waiting_pids(benv, LEGACY_APP)[0]["pid"]) == {"analysis_sessions"}
        pause.release.set()
        done = ok(holder.outcome())
        contender.outcome()
    else:
        hold = LegacyPause(monkeypatch, "capture_delivered_response")
        holder = Background(lambda: legacy_ack(benv, CONV_B, other.assistant_turn_id))
        holder.start()
        assert hold.reached.wait(15)
        contender = Background(lambda: run(benv, alice, contribution))
        contender.start()
        wait_until(lambda: benv.waiting() == 1)
        assert waited_relations(benv, waiting_pids(benv)[0]["pid"]) == {"analysis_sessions"}
        hold.release.set()
        holder.outcome()
        done = ok(contender.outcome())
    assert (done.input_action, done.product_progress_action) == ("contribution_appended", "forward")
    events = benv.events()
    assert [e["status"] for e in events] == ["open"] and len(events[0]["user_work_snapshot"]) == 1
    acks = {lk["assistant_delivery_id"]: lk["response_action"] for lk in benv.links()}
    assert acks[other.assistant_turn_id] == "continued_event"
    assert benv.sessions()[0]["current_step"] == "moat"
    no_unavailable(caplog)


@pytest.mark.parametrize("first", ["f2", "legacy"])
def test_f2_finalization_against_legacy_decryptage_preserves_integrity(benv, alice, legacy, monkeypatch, caplog,
                                                                      first):
    opened(benv, alice, CONV_A)
    anchor_b = legacy_turn(benv, legacy, CONV_B, "business")
    legacy_ack(benv, CONV_B, anchor_b["assistant_turn_id"])  # B ancrée au même event (continued_event)
    contribution = turn(CONV_A, "moat", "Réponse business A.")
    if first == "f2":
        pause, holder = start_paused(benv, alice, contribution)
        contender = Background(lambda: legacy_turn(benv, legacy, CONV_B, "moat", "Réponse business B."))
        contender.start()
        wait_until(lambda: benv.waiting(LEGACY_APP) == 1)
        assert waited_relations(benv, waiting_pids(benv, LEGACY_APP)[0]["pid"]) == {"analysis_sessions"}
        pause.release.set()
        f2, historical = ok(holder.outcome()), contender.outcome()
        expected = ("forward", "stale_contribution_step")
    else:
        hold = LegacyPause(monkeypatch, "apply_construction_these_progress")
        holder = Background(lambda: legacy_turn(benv, legacy, CONV_B, "moat", "Réponse business B."))
        holder.start()
        assert hold.reached.wait(15)
        contender = Background(lambda: run(benv, alice, contribution))
        contender.start()
        wait_until(lambda: benv.waiting() == 1)
        assert waited_relations(benv, waiting_pids(benv)[0]["pid"]) == {"analysis_sessions"}
        hold.release.set()
        historical, f2 = holder.outcome(), ok(contender.outcome())
        expected = ("stale_contribution_step", "forward")
    assert historical["delivery_status"] == "pending"
    # Une seule avancée, jamais deux ; les deux contributions répondent à
    # business (relu sous verrou), l'event reste unique et ouvert.
    [session] = benv.sessions()
    assert session["current_step"] == "moat" and session["status"] == "in_progress"
    progress = {lk["assistant_delivery_id"]: lk["product_progress_action"] for lk in benv.links()}
    assert (progress[f2.assistant_turn_id], progress[uuid.UUID(historical["assistant_turn_id"])]) == expected
    [event] = benv.events()
    assert event["status"] == "open" and len(event["user_work_snapshot"]) == 2
    statements = benv.rows("SELECT step FROM user_statements")
    assert [s["step"] for s in statements] == ["business", "business"]
    no_unavailable(caplog)


def test_event_closed_during_the_wait_is_reread_never_reattached(benv, alice, legacy, monkeypatch, caplog):
    opened(benv, alice, CONV_A)
    opened(benv, alice, CONV_B)                                   # B ancrée au même event (continued)
    advance = ok(run(benv, alice, turn(CONV_A, "moat", "Réponse business A.")))
    hold = LegacyPause(monkeypatch, "capture_delivered_response")  # transition : ferme business sous verrou
    closer = Background(lambda: legacy_ack(benv, CONV_A, advance.assistant_turn_id))
    closer.start()
    assert hold.reached.wait(15)
    before = benv.snapshot()
    contender = Background(lambda: run(benv, alice, turn(CONV_B, "moat", "Réponse business B.")))
    contender.start()
    wait_until(lambda: benv.waiting() == 1)
    hold.release.set()
    closer.outcome()
    # Relu après l'attente : ancre fermée, même ticker -> stale, rien n'est
    # écrit, jamais de rattachement au nouvel event moat.
    assert contender.outcome().status == UNAVAILABLE
    assert "error=StaleConversationContext" in caplog.text
    after = benv.snapshot()
    for table in ("decryptage_cognitive_links", "assistant_deliveries", "user_statements"):
        assert len(after[table]) == len(before[table]), table
    [moat] = [e for e in benv.events() if e["status"] == "open"]
    assert moat["user_work_snapshot"] == []


# --------------------------------------------------------------------------
# Livraison étrangère verrouillée par un tiers (V2.2-01)
# --------------------------------------------------------------------------

def test_foreign_delivery_locked_by_a_third_party_is_refused_without_waiting(benv, alice, bob):
    theirs = ok(run(benv, bob, turn(CONV_B, "business")))
    blocker = benv.observer.connect()
    tx = blocker.begin()
    blocker.execute(sa.text("SELECT id FROM assistant_deliveries WHERE id = :d FOR UPDATE"),
                    {"d": theirs.assistant_turn_id})
    try:
        for conversation_key in (CONV_B, CONV_A):
            observed = []
            worker = Background(lambda key=conversation_key: run(benv, alice, ack(key, theirs.assistant_turn_id)))
            worker.start()
            while not worker.finished.is_set():
                observed.append(benv.waiting())
            outcome = worker.outcome(timeout=4)  # < lock_timeout (5 s) : aucune attente possible
            assert (outcome.status, outcome.value) == (NOT_FOUND, None)
            assert set(observed) <= {0}
        # Contre-épreuve : la primitive historique (UUID d'abord) attend le
        # verrou exclusif d'une ligne étrangère avant de vérifier le
        # propriétaire.
        with benv.Sessions() as db:
            db.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(sa.exc.OperationalError) as waited:
                ad.lock_delivery_for_ack(db, assistant_turn_id=theirs.assistant_turn_id, user_id=USER_A,
                                         conversation_key=CONV_B)
            assert waited.value.orig.pgcode == "55P03"
            db.rollback()
        with benv.Sessions() as db:
            db.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(ad.AssistantDeliveryNotFound):
                ad.lock_owned_delivery(db, assistant_turn_id=theirs.assistant_turn_id, user_id=USER_A,
                                       conversation_key=CONV_B)
            db.rollback()
    finally:
        tx.rollback()
        blocker.close()


# --------------------------------------------------------------------------
# Profils de verrous MESURÉS (sondes NOWAIT d'une connexion tierce)
# --------------------------------------------------------------------------

MODES = ("FOR KEY SHARE", "FOR SHARE", "FOR NO KEY UPDATE", "FOR UPDATE")


def held_mode(benv, table, key, value):
    """Mode de verrou de ligne déduit des conflits NOWAIT (55P03) : le plus
    faible mode incompatible désigne le mode détenu."""
    granted = []
    for mode in MODES:
        with benv.observer.begin() as conn:
            try:
                conn.execute(sa.text(f"SELECT 1 FROM {table} WHERE {key} = :v {mode} NOWAIT"), {"v": value})
                granted.append(mode)
            except sa.exc.OperationalError as exc:
                assert exc.orig.pgcode == "55P03"
    return {(): "FOR UPDATE", ("FOR KEY SHARE",): "FOR NO KEY UPDATE",
            ("FOR KEY SHARE", "FOR SHARE"): "FOR SHARE",
            ("FOR KEY SHARE", "FOR SHARE", "FOR NO KEY UPDATE"): "FOR KEY SHARE"}.get(tuple(granted), "aucun")


def test_measured_lock_profile_of_a_contribution_turn_and_of_its_ack(benv, alice):
    opened(benv, alice, CONV_A)
    [anchor] = benv.events()
    [session] = benv.sessions()
    pause, holder = start_paused(benv, alice, turn(CONV_A, "moat", "Ma réponse business."))
    try:
        turn_profile = {
            "users": held_mode(benv, "users", "id", USER_A),
            "oryx_sessions": held_mode(benv, "oryx_sessions", "id", alice.session_id),
            "oryx_session_generations": held_mode(benv, "oryx_session_generations", "id", alice.generation_id),
            "conversation_identities": held_mode(benv, "conversation_identities", "conversation_key", CONV_A),
            "analysis_sessions": held_mode(benv, "analysis_sessions", "id", session["id"]),
            "cognitive_events (ancre)": held_mode(benv, "cognitive_events", "id", anchor["id"]),
        }
    finally:
        pause.release.set()
    done = ok(holder.outcome())
    assert turn_profile == {"users": "FOR NO KEY UPDATE", "oryx_sessions": "FOR SHARE",
                            "oryx_session_generations": "FOR SHARE",
                            # verrou IMPLICITE de la FK assistant_deliveries.conversation_key
                            "conversation_identities": "FOR KEY SHARE",
                            "analysis_sessions": "FOR UPDATE", "cognitive_events (ancre)": "FOR UPDATE"}
    pause, holder = start_paused(benv, alice, ack(CONV_A, done.assistant_turn_id))
    try:
        ack_profile = {
            "users": held_mode(benv, "users", "id", USER_A),
            "assistant_deliveries": held_mode(benv, "assistant_deliveries", "id", done.assistant_turn_id),
            "decryptage_cognitive_links": held_mode(benv, "decryptage_cognitive_links", "assistant_delivery_id",
                                                    done.assistant_turn_id),
            "analysis_sessions": held_mode(benv, "analysis_sessions", "id", session["id"]),
            "cognitive_events (fermé)": held_mode(benv, "cognitive_events", "id", anchor["id"]),
        }
    finally:
        pause.release.set()
    assert ok(holder.outcome()).response_action == "transitioned_event"
    assert ack_profile == {"users": "FOR NO KEY UPDATE", "assistant_deliveries": "FOR UPDATE",
                           "decryptage_cognitive_links": "FOR UPDATE", "analysis_sessions": "FOR UPDATE",
                           "cognitive_events (fermé)": "FOR UPDATE"}


# --------------------------------------------------------------------------
# Classification des échecs PostgreSQL (jamais un succès)
# --------------------------------------------------------------------------

def classify(exc) -> str:
    code = getattr(getattr(exc, "orig", None), "pgcode", None)
    message = str(getattr(exc, "orig", exc))
    if code == "40P01":
        return "deadlock_detected"
    if code == "55P03":
        return "lock_timeout" if "lock timeout" in message else "lock_not_available"
    if code == "57014":
        return "statement_timeout" if "statement timeout" in message else "query_canceled"
    if code == "23505":
        return "unique_violation"
    return "unexpected"


def test_postgresql_failures_are_classified(benv, alice):
    blocker = benv.observer.connect()
    tx = blocker.begin()
    blocker.execute(sa.text("SELECT id FROM users WHERE id = :u FOR UPDATE"), {"u": USER_A})
    found = {}
    try:
        for name, sql in (("nowait", "SELECT id FROM users WHERE id = :u FOR UPDATE NOWAIT"),
                          ("lock", "SET LOCAL lock_timeout = '200ms'; SELECT id FROM users WHERE id = :u FOR UPDATE"),
                          ("statement", "SET LOCAL statement_timeout = '200ms'; SELECT pg_sleep(2), :u")):
            with benv.engine.begin() as conn:
                try:
                    conn.execute(sa.text(sql), {"u": USER_A})
                except sa.exc.DBAPIError as exc:
                    found[name] = classify(exc)
        with benv.engine.begin() as conn:
            try:
                conn.execute(sa.text("INSERT INTO users (id, level) VALUES (:u, 'debutant')"), {"u": USER_A})
            except sa.exc.DBAPIError as exc:
                found["unique"] = classify(exc)
    finally:
        tx.rollback()
        blocker.close()
    assert found == {"nowait": "lock_not_available", "lock": "lock_timeout", "statement": "statement_timeout",
                     "unique": "unique_violation"}


def test_lock_timeout_in_phase_b_is_unavailable_never_a_success(benv, alice, caplog):
    """Un délai de verrou (5 s, policy F2) rend 503 SANS rien écrire ; il
    n'est jamais compté comme une absence d'interblocage (les autres tests
    exigent AUTHORIZED et un compteur deadlocks inchangé)."""
    before = benv.snapshot()
    blocker = benv.observer.connect()
    tx = blocker.begin()
    blocker.execute(sa.text("SELECT id FROM users WHERE id = :u FOR UPDATE"), {"u": USER_A})
    try:
        outcome = run(benv, alice, turn(CONV_A, "business"))
    finally:
        tx.rollback()
        blocker.close()
    assert (outcome.status, outcome.value) == (UNAVAILABLE, None)
    assert "status=unavailable error=OperationalError" in caplog.text
    assert benv.snapshot() == before


def test_real_deadlock_is_detected_and_classified(pg_url, schema):  # noqa: F811
    """Contrôle du classificateur sur un VRAI interblocage, construit hors
    de F2 (deux connexions brutes, ordre inverse sur deux lignes users) ;
    PostgreSQL en annule une (40P01). Les tests F2 ci-dessus exigent, eux,
    un compteur inchangé."""
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, level) VALUES ('dl-1', 'debutant'), ('dl-2', 'debutant') "
                                 "ON CONFLICT DO NOTHING"))
        before = scalar(engine, "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
        first, second = engine.connect(), engine.connect()
        t1, t2 = first.begin(), second.begin()
        first.execute(sa.text("UPDATE users SET level = 'avance' WHERE id = 'dl-1'"))
        second.execute(sa.text("UPDATE users SET level = 'avance' WHERE id = 'dl-2'"))
        errors = []

        def cross(conn, target):
            try:
                conn.execute(sa.text("UPDATE users SET level = 'debutant' WHERE id = :t"), {"t": target})
            except sa.exc.DBAPIError as exc:
                errors.append(classify(exc))

        worker = threading.Thread(target=cross, args=(first, "dl-2"))
        worker.start()
        wait_until(lambda: scalar(engine, "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                          "AND datname = current_database()") == 1)
        cross(second, "dl-1")
        worker.join(10)
        t1.rollback()
        t2.rollback()
        first.close()
        second.close()
        assert errors == ["deadlock_detected"]
        wait_until(lambda: scalar(engine, "SELECT deadlocks FROM pg_stat_database "
                                          "WHERE datname = current_database()") == before + 1)
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM users WHERE id IN ('dl-1', 'dl-2')"))
        engine.dispose()


def test_concurrent_mixed_load_without_deadlock(benv, alice, bob, legacy, caplog):
    """Charge mixte (deux utilisateurs, F2 et historique, tours et ACK) :
    tout aboutit, aucun interblocage (compteur relevé par la fixture)."""
    opened(benv, alice, CONV_A)
    opened(benv, bob, CONV_B)
    pending = [ok(run(benv, alice, turn(f"{CONV_A}-{i}", "business"))) for i in range(3)]
    actors = [Background(lambda i=i: run(benv, alice, ack(f"{CONV_A}-{i}", pending[i].assistant_turn_id)))
              for i in range(3)]
    actors += [Background(lambda: run(benv, bob, turn(CONV_B, "moat", "Réponse B."))),
               Background(lambda: legacy_turn(benv, legacy, "conv-legacy", "business")),
               Background(lambda: run(benv, alice, c.UpdateProfileLevel("avance", 0)))]
    for actor in actors:
        actor.start()
    results = [actor.outcome() for actor in actors]
    for result in results[:4] + results[5:]:
        assert result.status == AUTHORIZED, result
    assert results[4]["delivery_status"] == "pending"
    no_unavailable(caplog)
    assert TICKER == "MC.PA"
