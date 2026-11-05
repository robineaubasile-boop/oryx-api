"""R1 PR 9-C1-B — finalisation atomique Décrypter F2, contre un VRAI
PostgreSQL (16 et 18 en CI), sous le VRAI runner et le registre de
PRODUCTION (vingt commandes depuis R1 PR 9-C2), avec des candidats produits par le VRAI
orchestrateur B2 (transports simulés).

Chaque état durable est observé par une connexion indépendante ; « état
inchangé » = empreinte complète des tables Décrypter, réservations et xmin
compris (aucune écriture durable).

1. finalisation nominale : une livraison, une capture, une progression,
   réservation finalized, UPDATE B1 APRÈS RELEASE, deux COMMIT par appel ;
   analyse complète jusqu'à la thèse ; reconnaissance terminale ;
2. atomicité : UPDATE B1 refusé après RELEASE (rollback GLOBAL de toutes
   les écritures ORM), bail expiré après Prepare, erreurs à chaque phase,
   COMMIT refusé ou incertain, aucune réexécution ;
3. revalidation B2 complète (niveau, révision, faits, NULL, contributions,
   thèse, méthode, ancre, session, étape, sessions multiples, ancre stale,
   hors bornes, non représentable) et refus techniques distincts ;
4. AlreadyFinalized : aucune écriture, aucun contenu, aucune substitution ;
5. collisions legacy prouvées, jamais d'adoption ;
6. parité de la méthode B2 / C1-B.
"""
import dataclasses
import json
from datetime import datetime
import time
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core import assistant_delivery as ad
from core import decryptage_cognitive_runtime as dcr
from core import decryptage_progress as dp
from core.authz_f2 import _adapters
from core.authz_f2 import commands as c
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import decryptage_candidate as dcand
from core.authz_f2 import decryptage_context as dc
from core.authz_f2 import decryptage_finalization as dfin
from core.authz_f2 import decryptage_method_selection as dms
from core.authz_f2 import decryptage_reservation as dr
from core.authz_f2.runner import AUTHORIZED, IDEMPOTENCY_CONFLICT, NOT_FOUND, REVISION_CONFLICT, UNAVAILABLE
from tests.authz_f2_pr9a_support import BOB_EMAIL, USER_A, USER_B, legacy_ack, legacy_turn
from tests.authz_f2_pr9b1_support import CONV, decryptage_keyring, legacy_route  # noqa: F401 — fixture
from tests.authz_f2_pr9c1b_support import FakeClaude, FakeClock, FinalizationEnv, next_turn, schema  # noqa: F401
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

STEPS = ("business", "moat", "chiffres", "valorisation", "risques", "swot_final")


@pytest.fixture
def env(pg_url, schema):  # noqa: F811
    environment = FinalizationEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before
    environment.dispose()


@pytest.fixture
def alice(env):
    return env.confirmed()


@pytest.fixture
def bob(env):
    return env.confirmed(BOB_EMAIL, USER_B)


def finalized(env, issued, candidate):
    outcome = env.finalize(issued, candidate)
    assert outcome.status == AUTHORIZED, outcome
    assert type(outcome.value) is dfin.DecryptageFinalized, outcome.value
    return outcome.value


def reply_for(step):
    return f"Étape {step}.\n<!--ORYX_STEP:{step}-->"


def f2_turn(env, issued, step, text="", conv=CONV, ack=True):
    """Tour F2 complet : réservation, génération, finalisation, ACK historique."""
    command = next_turn(conv, question=text, context="h" if text else "")
    _, _, candidate = env.generate(issued, command, text=reply_for(step))
    done = finalized(env, issued, candidate)
    if ack:
        legacy_ack(env, conv, done.assistant_delivery_id)
    return candidate, done


def started(env, issued, conv=CONV):
    """Analyse ouverte (business), conversation ancrée à son event."""
    f2_turn(env, issued, "business", conv=conv)
    [session] = env.sessions()
    return session


def pending_contribution(env, issued, step="moat", text="Ils vendent du luxe.", conv=CONV):
    """Candidat d'une contribution GÉNÉRÉ mais pas encore finalisé."""
    _, lease, candidate = env.generate(issued, next_turn(conv, question=text, context="h"), text=reply_for(step))
    return lease, candidate


def link_of(env, delivery_id):
    [link] = env.rows("SELECT * FROM decryptage_cognitive_links WHERE assistant_delivery_id = :d", d=delivery_id)
    return link


def statements(env):
    return env.rows("SELECT step, statement_text, analysis_session_id FROM user_statements ORDER BY id")


def _statements_log(engine):
    log = []
    sa.event.listen(engine, "before_cursor_execute", lambda conn, cur, st, *a: log.append(" ".join(st.split())))
    return log


def _patched(monkeypatch, module, name, wrapper_factory):
    original = getattr(module, name)
    monkeypatch.setattr(module, name, wrapper_factory(original))
    return original


# --------------------------------------------------------------------------
# 1. Finalisation nominale
# --------------------------------------------------------------------------

def test_nominal_finalization_is_one_atomic_transaction(env, alice):
    command, lease, candidate = env.generate(alice, next_turn())
    reserved = env.reservation(lease.reservation_id)
    commits = []
    sa.event.listen(env.engine, "commit", lambda conn: commits.append(1))
    log = _statements_log(env.engine)
    done = finalized(env, alice, candidate)
    assert len(commits) == 2  # phase A (lecture) et phase B : aucun COMMIT intermédiaire
    # Ordre : verrou B1, SAVEPOINT C1 ... RELEASE, horloge de décision, UPDATE final.
    lock = next(i for i, s in enumerate(log) if s.startswith("SELECT id, user_id") and "FOR UPDATE" in s
                and "decryptage_turn_reservations" in s)
    savepoint = next(i for i, s in enumerate(log) if s.startswith("SAVEPOINT"))
    name = log[savepoint].split()[-1]
    release = log.index(f"RELEASE SAVEPOINT {name}")
    projection = next(i for i, s in enumerate(log) if s.startswith("WITH o AS"))
    final = next(i for i, s in enumerate(log) if s.startswith("WITH decision AS MATERIALIZED")
                 and "UPDATE decryptage_turn_reservations SET state = 'finalized'" in s)
    assert lock < savepoint < projection < release < final
    # Correctif d'audit P1 : l'instant de décision est capturé PAR l'UPDATE
    # (aucune lecture d'horloge distincte après RELEASE).
    assert log[final].startswith("WITH decision AS MATERIALIZED (SELECT clock_timestamp() AS at) UPDATE ")
    assert "SELECT clock_timestamp()" not in log[release:final]
    assert not [s for s in log if s.startswith(("ROLLBACK", "COMMIT"))]
    # Effets : une livraison F2, son lien V2, une session neuve, les faits du snapshot financier.
    [delivery] = env.delivery_rows()
    assert delivery["id"] == done.assistant_delivery_id and delivery["status"] == "pending"
    assert delivery["request_fingerprint"] == bridge._delivery_request_fingerprint(candidate)
    assert json.loads(delivery["payload"]) == candidate.payload.as_dict()
    link = link_of(env, delivery["id"])
    assert (link["input_action"], link["product_progress_action"], link["capture_state"]) == (
        "no_open_event", "new", "awaiting_delivery")
    [session] = env.sessions()
    assert delivery["analysis_session_id"] == session["id"] and session["current_step"] == "business"
    facts = {r["fact_type"]: r["fact_value"] for r in env.rows("SELECT fact_type, fact_value FROM analysis_facts")}
    assert facts == {k: v for k, v in dfin.progress_facts(candidate.market).items() if v is not None}
    row = env.reservation(lease.reservation_id)
    assert (row["state"], row["assistant_delivery_id"], row["resolved_ticker"], row["lease_token_hash"],
            row["lease_expires_at"], row["blocked_reason"]) == ("finalized", done.assistant_delivery_id, "MC.PA",
                                                                None, None, None)
    assert row["finalized_at"] == row["updated_at"]
    for column in ("conversation_key", "source_user_turn_id", "fingerprint_version", "fingerprint_key_version",
                   "request_fingerprint", "lease_epoch", "attempts_used", "created_at"):
        assert row[column] == reserved[column], column  # intention et compteurs jamais réécrits
    # L'ACK historique (même runtime V2) ouvre l'event normalement.
    assert legacy_ack(env, CONV, done.assistant_delivery_id)["delivery_status"] == "delivered"
    assert [e["status"] for e in env.events()] == ["open"]


def test_full_analysis_reaches_the_thesis_through_f2(env, alice):
    started(env, alice)
    for previous, step in zip(STEPS, STEPS[1:]):
        _, done = f2_turn(env, alice, step, f"Ma réponse {previous}.")
        assert link_of(env, done.assistant_delivery_id)["product_progress_action"] == "forward"
    [session] = env.sessions()
    assert (session["status"], session["current_step"]) == ("completed", "swot_final")
    assert [s["step"] for s in statements(env)] == list(STEPS[:-1])
    [thesis] = env.rows("SELECT thesis_text, analysis_session_id FROM investment_theses")
    assert thesis == {"thesis_text": "Ma réponse risques.", "analysis_session_id": session["id"]}


def test_progress_rules_are_the_historical_ones(env, alice):
    """Aucune règle dupliquée : forward sans contribution, stale, rétrograde
    produits par le moteur historique sur la session préparée."""
    started(env, alice)
    _, done = f2_turn(env, alice, "moat")  # Claude seul : jamais d'avancée
    assert link_of(env, done.assistant_delivery_id)["product_progress_action"] == "forward_without_contribution"
    assert env.sessions()[0]["current_step"] == "business"
    _, done = f2_turn(env, alice, "moat", "Réponse business.")  # vraie contribution : avancée
    assert link_of(env, done.assistant_delivery_id)["product_progress_action"] == "forward"
    _, done = f2_turn(env, alice, "business", "Réponse moat.")  # marqueur rétrograde : session intacte
    assert link_of(env, done.assistant_delivery_id)["product_progress_action"] == "retrograde_marker"
    assert [s["step"] for s in statements(env)] == ["business", "moat"]
    assert env.sessions()[0]["current_step"] == "moat"


# --------------------------------------------------------------------------
# 2. Atomicité
# --------------------------------------------------------------------------

class ExpiresBeforeTheWrite:
    """Régression de l'audit P1 (PR #233) sur les instructions RÉELLES.

    Juste avant l'instruction d'écriture visée (UPDATE final ou blocage),
    DANS la transaction du runner : (1) un instant est lu (`before`) et le bail
    est encore valide à cet instant ; (2) pause RÉELLE jusqu'à ce que l'horloge
    PostgreSQL dépasse l'échéance enregistrée (constaté par une connexion
    tierce, aucune date injectée) ; (3) contre-épreuve : la condition
    `lease_expires_at > before` (un instant lu AVANT la pause, ancienne forme)
    accepterait encore la ligne, `lease_expires_at > clock_timestamp()` non ;
    (4) l'instruction RÉELLE est exécutée telle quelle. Les instructions
    exécutées et leurs paramètres sont relevés."""

    def __init__(self, monkeypatch, env, statement_name):
        self.statement = getattr(_adapters, statement_name)
        self.env, self.before, self.stale_accepts, self.fresh_accepts = env, None, None, None
        self.executed = []
        original = _adapters._first

        def first(conn, sql, **params):
            if sql == self.statement:
                probe = sa.text("SELECT clock_timestamp() AS now, lease_expires_at FROM decryptage_turn_reservations "
                                "WHERE id = :i")
                now, expires = conn.execute(probe, {"i": params["id"]}).one()
                assert now < expires, "le bail devait être encore valide avant la pause"
                self.before = now
                deadline = time.monotonic() + 20
                while not self._expired(expires):
                    assert time.monotonic() < deadline, "échéance jamais atteinte"
                    time.sleep(0.05)
                check = "SELECT count(*) FROM decryptage_turn_reservations WHERE id = :i AND lease_expires_at > "
                self.stale_accepts = conn.execute(sa.text(check + ":b"), {"i": params["id"], "b": now}).scalar_one()
                self.fresh_accepts = conn.execute(sa.text(check + "clock_timestamp()"),
                                                  {"i": params["id"]}).scalar_one()
            return original(conn, sql, **params)

        monkeypatch.setattr(_adapters, "_first", first)
        sa.event.listen(env.engine, "before_cursor_execute",
                        lambda c, cur, st, parameters, *a: self.executed.append((" ".join(st.split()), parameters)))

    def _expired(self, expires) -> bool:
        with self.env.observer.connect() as conn:
            return conn.execute(sa.text("SELECT clock_timestamp() > :e"), {"e": expires}).scalar_one()

    def assert_refused_by_the_real_statement(self):
        assert self.before is not None, "instruction visée jamais atteinte"
        assert (self.stale_accepts, self.fresh_accepts) == (1, 0)  # la fenêtre existait bien, l'UPDATE la ferme
        writes = [(st, params) for st, params in self.executed if st.startswith("WITH decision AS MATERIALIZED")]
        assert len(writes) == 1, writes
        statement, params = writes[0]
        assert statement.startswith("WITH decision AS MATERIALIZED (SELECT clock_timestamp() AS at) UPDATE "
                                    "decryptage_turn_reservations SET ")
        assert "lease_expires_at > decision.at" in statement and statement.count("clock_timestamp()") == 1
        assert not [v for v in params.values() if isinstance(v, datetime)]  # aucun instant transmis par le code
        assert "at" not in params


def _lease_expiring_soon(env, reservation_id, seconds=3):
    env.force(reservation_id, f"lease_expires_at = clock_timestamp() + interval '{seconds} seconds'")


def test_a_lease_expiring_after_a_pre_read_instant_never_finalizes(env, alice, monkeypatch):
    """reserved -> finalized : un instant lu AVANT la pause n'autorise plus
    l'UPDATE exécuté APRÈS l'expiration réelle ; ROLLBACK GLOBAL de toutes
    les écritures du SAVEPOINT libéré ; B1 inchangée (état, epoch, compteur,
    jeton)."""
    started(env, alice)
    lease, candidate = pending_contribution(env, alice)
    _lease_expiring_soon(env, lease.reservation_id)
    reserved = env.reservation(lease.reservation_id)
    before = env.full_snapshot()
    window = ExpiresBeforeTheWrite(monkeypatch, env, "_FINALIZE_RESERVATION")
    log = _statements_log(env.engine)
    outcome = env.finalize(alice, candidate)
    assert (outcome.status, outcome.value) == (REVISION_CONFLICT, None)  # jamais un objet LeaseStale committé
    window.assert_refused_by_the_real_statement()
    # Le SAVEPOINT avait été LIBÉRÉ : livraison, lien, contribution, UserStatement,
    # progression écrits... puis annulés par le ROLLBACK de la transaction externe.
    assert any(s.startswith("RELEASE SAVEPOINT") for s in log)
    assert any(s.startswith("INSERT INTO user_statements") for s in log)
    assert any(s.startswith("INSERT INTO assistant_deliveries") for s in log)
    assert env.full_snapshot() == before  # aucune livraison, capture ni progression durable
    row = env.reservation(lease.reservation_id)
    assert {k: row[k] for k in ("state", "lease_epoch", "attempts_used", "lease_token_hash", "lease_expires_at",
                                "assistant_delivery_id", "finalized_at", "resolved_ticker", "xmin")} == {
        k: reserved[k] for k in ("state", "lease_epoch", "attempts_used", "lease_token_hash", "lease_expires_at",
                                 "assistant_delivery_id", "finalized_at", "resolved_ticker", "xmin")}
    assert row["state"] == "reserved"


def test_the_same_pause_without_expiry_still_finalizes(env, alice, monkeypatch):
    """Contrôle : même instrumentation, bail encore valide au moment de
    l'écriture -> finalisation normale (la régression ne refuse pas tout)."""
    started(env, alice)
    lease, candidate = pending_contribution(env, alice)
    executed = []
    sa.event.listen(env.engine, "before_cursor_execute",
                    lambda c, cur, st, parameters, *a: executed.append(" ".join(st.split())))
    done = finalized(env, alice, candidate)
    [write] = [st for st in executed if st.startswith("WITH decision AS MATERIALIZED")]
    assert "state = 'finalized'" in write
    row = env.reservation(lease.reservation_id)
    assert row["state"] == "finalized" and row["finalized_at"] == row["updated_at"]
    assert row["assistant_delivery_id"] == done.assistant_delivery_id


def test_final_update_matching_no_row_is_a_global_rollback(env, alice, monkeypatch):
    lease_cmd = next_turn()
    _, lease, candidate = env.generate(alice, lease_cmd)
    before = env.full_snapshot()
    monkeypatch.setattr(_adapters, "_FINALIZE_RESERVATION",
                        _adapters._FINALIZE_RESERVATION.replace("WHERE id = :id", "WHERE FALSE AND id = :id"))
    assert env.finalize(alice, candidate).status == REVISION_CONFLICT
    assert env.full_snapshot() == before


def test_lease_expired_after_prepare_is_lease_stale_and_a_global_rollback(env, alice, monkeypatch):
    started(env, alice)
    lease, candidate = pending_contribution(env, alice)
    env.force(lease.reservation_id, "lease_expires_at = clock_timestamp() + interval '1500 milliseconds'")
    before = env.full_snapshot()

    def slow(original):
        def prepare(*args, **kwargs):
            prepared = original(*args, **kwargs)
            time.sleep(2.0)  # le bail échoit pendant le SAVEPOINT, avant la relecture B2
            return prepared
        return prepare

    _patched(monkeypatch, bridge, "_prepare_turn", slow)
    assert env.finalize(alice, candidate).status == REVISION_CONFLICT
    assert env.full_snapshot() == before


def _failing(exc_type=RuntimeError):
    def factory(original):
        def fail(*args, **kwargs):
            raise exc_type("échec injecté")
        return fail
    return factory


@pytest.mark.parametrize("module, name", [
    (ad, "claim_delivery"),
    (dcr, "prepare_locked_cognitive_turn"),
    (_adapters, "_preparation_outcome"),
    (dcr, "apply_prepared_cognitive_turn"),
    (dp, "apply_prepared_construction_these_progress"),
    (ad, "bind_locked_analysis_session"),
    (dcr, "record_turn_progress"),
], ids=["claim", "prepare", "projection", "apply", "progress", "bind", "record"])
def test_an_error_in_any_phase_writes_nothing(env, alice, monkeypatch, caplog, module, name):
    started(env, alice)
    lease, candidate = pending_contribution(env, alice)
    before = env.full_snapshot()
    _patched(monkeypatch, module, name, _failing())
    assert env.finalize(alice, candidate).status == UNAVAILABLE
    assert "error=RuntimeError" in caplog.text
    assert env.full_snapshot() == before  # réservation toujours reserved, même xmin


@pytest.mark.parametrize("exc", [
    dcr.AmbiguousOpenDecryptageEvents("deux events"),
    dcr.CognitiveLinkInvariantError("invariant"),
    dcr.PreparedCognitiveTurnUnusable("décision"),
    dp.PreparedAnalysisSessionMismatch("session"),
    type("StaleSubclass", (dcr.StaleConversationContext,), {})(uuid.uuid4()),
    type("AmbiguousSubclass", (dcr.AmbiguousActiveAnalysisSessions,), {})("sous-classe"),
], ids=["ambiguous_events", "link_invariant", "decision", "session_mismatch", "stale_subclass", "ambiguous_subclass"])
def test_technical_cognitive_errors_are_never_a_business_result(env, alice, monkeypatch, exc):
    started(env, alice)
    lease, candidate = pending_contribution(env, alice)
    before = env.full_snapshot()

    def raise_it(original):
        def fail(*args, **kwargs):
            raise exc
        return fail

    target = (dp, "apply_prepared_construction_these_progress") if isinstance(
        exc, dp.PreparedAnalysisSessionMismatch) else (dcr, "prepare_locked_cognitive_turn")
    _patched(monkeypatch, *target, raise_it)
    outcome = env.finalize(alice, candidate)
    assert (outcome.status, outcome.value) == (UNAVAILABLE, None)  # jamais ContextChanged ni refus métier
    assert env.full_snapshot() == before


def test_unexpected_integrity_error_in_claim_is_never_a_collision(env, alice, monkeypatch):
    lease_cmd = next_turn()
    _, lease, candidate = env.generate(alice, lease_cmd)
    before = env.full_snapshot()

    def integrity(original):
        def fail(*args, **kwargs):
            raise sa.exc.IntegrityError("INSERT", {}, Exception("contrainte inattendue"))
        return fail

    _patched(monkeypatch, ad, "claim_delivery", integrity)
    assert env.finalize(alice, candidate).status == UNAVAILABLE
    assert env.full_snapshot() == before
    assert env.reservation(lease.reservation_id)["state"] == "reserved"  # jamais bloquée


def test_commit_rejected_by_postgresql_is_unavailable_and_never_replayed(env, alice, monkeypatch):
    _, lease, candidate = env.generate(alice, next_turn())
    with env.observer.begin() as conn:
        conn.execute(sa.text("CREATE FUNCTION c1b_reject() RETURNS trigger LANGUAGE plpgsql AS "
                             "$$ BEGIN RAISE EXCEPTION 'c1b commit refusé'; END $$"))
        conn.execute(sa.text("CREATE CONSTRAINT TRIGGER c1b_reject AFTER UPDATE ON decryptage_turn_reservations "
                             "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION c1b_reject()"))
    calls = []
    entry = bridge.OPERATIONS[bridge.CandidateFinalization]

    def counted(*args):
        calls.append(1)
        return entry(*args)

    monkeypatch.setattr(bridge, "OPERATIONS", {**bridge.OPERATIONS, bridge.CandidateFinalization: counted})
    try:
        before = env.full_snapshot()
        outcome = env.finalize(alice, candidate)
        assert (outcome.status, outcome.value, outcome.principal) == (UNAVAILABLE, None, None)
        assert calls == [1]  # aucune réexécution
        assert env.full_snapshot() == before
    finally:
        with env.observer.begin() as conn:
            conn.execute(sa.text("DROP TRIGGER c1b_reject ON decryptage_turn_reservations"))
            conn.execute(sa.text("DROP FUNCTION c1b_reject()"))


def test_uncertain_commit_is_unavailable_and_replay_is_recognized(env, alice, monkeypatch, caplog):
    _, lease, candidate = env.generate(alice, next_turn())
    armed = []
    dialect = env.engine.dialect
    original = type(dialect).do_commit

    def uncertain_commit(dbapi_connection):
        original(dialect, dbapi_connection)
        if armed:
            armed.clear()
            raise sa.exc.OperationalError("COMMIT", {}, Exception("connexion perdue après COMMIT (simulé)"))

    monkeypatch.setattr(dialect, "do_commit", uncertain_commit)
    outcome = env.finalize(alice, candidate, hook=lambda point: armed.append(1) if point == "executed" else None)
    assert (outcome.status, outcome.value) == (UNAVAILABLE, None)  # ni succès ni annulation prétendus
    [delivery] = env.delivery_rows()  # commité malgré l'erreur : état incertain pour l'appelant
    assert env.reservation(lease.reservation_id)["state"] == "finalized"
    before = env.full_snapshot()
    again = env.finalize(alice, candidate)
    assert again.status == AUTHORIZED and again.value == dfin.AlreadyFinalized(lease.reservation_id, delivery["id"])
    assert env.full_snapshot() == before


# --------------------------------------------------------------------------
# 3. Revalidation B2 (le contexte change entre la génération et la finalisation)
# --------------------------------------------------------------------------

def _context_changed(env, alice, candidate, lease):
    before = env.full_snapshot()
    outcome = env.finalize(alice, candidate)
    assert outcome.status == AUTHORIZED, outcome
    assert outcome.value == dfin.FinalizationContextChanged(lease.reservation_id)
    assert env.full_snapshot() == before  # rien de durable, réservation reserved intacte


def _stable(snapshot):
    """NaN != NaN : valeurs non finies rendues comparables (texte)."""
    def norm(value):
        return repr(value) if isinstance(value, float) and value != value else value
    return {t: [{k: norm(v) for k, v in row.items()} for row in rows] for t, rows in snapshot.items()}


def _refused(env, alice, candidate, lease, reason):
    before = _stable(env.full_snapshot())
    outcome = env.finalize(alice, candidate)
    assert outcome.status == AUTHORIZED, outcome
    assert outcome.value == dfin.FinalizationPreparationRefused(lease.reservation_id, reason)
    assert _stable(env.full_snapshot()) == before


@pytest.mark.parametrize("change", ["level", "level_revision", "fact", "last_fact_null", "statement", "step"])
def test_a_changed_pedagogical_context_is_context_changed(env, alice, change):
    session = started(env, alice)
    env.add_fact(session["id"], "roe", 24.0, date="2027-01-01 00:00:00")
    lease, candidate = pending_contribution(env, alice)
    if change == "level":
        env.execute("UPDATE users SET level = CASE WHEN level = 'debutant' THEN 'avance' ELSE 'debutant' END, "
                    "level_revision = level_revision + 1 WHERE id = :u", u=USER_A)
    elif change == "level_revision":
        env.execute("UPDATE users SET level_revision = level_revision + 1 WHERE id = :u", u=USER_A)
    elif change == "fact":
        env.add_fact(session["id"], "roe", 25.0, date="2027-02-01 00:00:00")
    elif change == "last_fact_null":
        env.add_fact(session["id"], "roe", None, date="2027-02-01 00:00:00")  # la dernière valeur NULL l'emporte
    elif change == "statement":
        env.add_statement(session["id"], "business", "Contribution d'un autre onglet.", date="2027-02-01 00:00:00")
    else:
        env.execute("UPDATE analysis_sessions SET current_step = 'moat' WHERE id = :s", s=session["id"])
    _context_changed(env, alice, candidate, lease)


def test_a_changed_thesis_is_context_changed(env, alice):
    env.add_thesis("Ancienne thèse.")
    _, lease, candidate = env.generate(alice, next_turn())
    assert candidate.preparation.thesis_used is not None
    env.add_thesis("Thèse plus récente.", created_at="2027-01-01 00:00:00")
    _context_changed(env, alice, candidate, lease)


def test_a_new_active_session_changes_the_method_and_is_context_changed(env, alice):
    _, lease, candidate = env.generate(alice, next_turn())  # tour initial : base initial_turn
    assert candidate.preparation.method_basis == dcand.BASIS_INITIAL_TURN
    env.add_session()  # la session apparaît : base active_analysis
    _context_changed(env, alice, candidate, lease)


def test_a_changed_anchor_is_context_changed(env, alice, legacy_route):
    started(env, alice)
    lease, candidate = pending_contribution(env, alice, conv="conv-c1b-other")  # conversation sans ancre
    assert candidate.preparation.anchor.state == dc.ANCHOR_NONE
    delivered = legacy_turn(env, legacy_route, "conv-c1b-other", "business")
    legacy_ack(env, "conv-c1b-other", delivered["assistant_turn_id"])  # ancrée entre-temps
    _context_changed(env, alice, candidate, lease)


def test_multiple_active_sessions_is_a_closed_preparation_refusal(env, alice):
    session = started(env, alice)
    lease, candidate = pending_contribution(env, alice)
    env.add_session(offset=60)
    _refused(env, alice, candidate, lease, dc.MULTIPLE_ACTIVE_SESSIONS)


def test_a_stale_anchor_is_a_closed_preparation_refusal(env, alice, legacy_route):
    started(env, alice)
    first = legacy_turn(env, legacy_route, "conv-c1b-b", "business")
    legacy_ack(env, "conv-c1b-b", first["assistant_turn_id"])  # B ancrée au même event
    lease, candidate = pending_contribution(env, alice)          # A : contribution générée
    moved = legacy_turn(env, legacy_route, "conv-c1b-b", "moat", "Réponse business B.")
    legacy_ack(env, "conv-c1b-b", moved["assistant_turn_id"])     # B avance : event business fermé
    _refused(env, alice, candidate, lease, dc.STALE_CONVERSATION_CONTEXT)


def test_out_of_bounds_content_is_a_closed_preparation_refusal(env, alice):
    session = started(env, alice)
    lease, candidate = pending_contribution(env, alice)
    env.add_statement(session["id"], "business", "x" * (dc.STATEMENT_TEXT_MAX_BYTES + 1), date="2027-01-01 00:00:00")
    _refused(env, alice, candidate, lease, dc.CONTEXT_OUT_OF_BOUNDS)


def test_unrepresentable_value_is_a_closed_preparation_refusal(env, alice):
    session = started(env, alice)
    lease, candidate = pending_contribution(env, alice)
    env.add_fact(session["id"], "roe", "NaN", date="2027-01-01 00:00:00")
    _refused(env, alice, candidate, lease, dc.CONTEXT_VALUE_INVALID)


def test_an_unchanged_context_is_finalized_with_the_prepared_decision(env, alice):
    session = started(env, alice)
    env.add_fact(session["id"], "roe", None, date="2027-01-01 00:00:00")  # dernière valeur NULL, inchangée
    lease, candidate = pending_contribution(env, alice)
    done = finalized(env, alice, candidate)
    link = link_of(env, done.assistant_delivery_id)
    [event] = env.events()
    assert (link["input_action"], link["input_event_id"], link["product_progress_action"]) == (
        "contribution_appended", event["id"], "forward")
    assert [s["step"] for s in statements(env)] == ["business"]


# --------------------------------------------------------------------------
# 4. AlreadyFinalized
# --------------------------------------------------------------------------

def _recognized(env, alice, candidate, lease, delivery_id):
    before = env.full_snapshot()
    outcome = env.finalize(alice, candidate)
    assert outcome.status == AUTHORIZED
    assert outcome.value == dfin.AlreadyFinalized(lease.reservation_id, delivery_id)
    assert {f.name for f in dataclasses.fields(outcome.value)} == {"reservation_id", "assistant_delivery_id"}
    assert env.full_snapshot() == before  # aucune écriture, aucune progression


def test_already_finalized_is_recognized_without_the_erased_token_and_writes_nothing(env, alice):
    _, lease, candidate = env.generate(alice, next_turn())
    done = finalized(env, alice, candidate)
    assert env.reservation(lease.reservation_id)["lease_token_hash"] is None
    _recognized(env, alice, candidate, lease, done.assistant_delivery_id)


def test_level_changed_after_commit_does_not_hide_the_finalization(env, alice):
    _, lease, candidate = env.generate(alice, next_turn())
    done = finalized(env, alice, candidate)
    env.execute("UPDATE users SET level = CASE WHEN level = 'debutant' THEN 'avance' ELSE 'debutant' END, "
                "level_revision = level_revision + 1 WHERE id = :u", u=USER_A)
    _recognized(env, alice, candidate, lease, done.assistant_delivery_id)  # niveau du candidat, pas du profil


def test_another_candidate_text_never_replaces_the_canonical_content(env, alice):
    command = next_turn()
    command, reserved = env.reserve(alice, command)
    first = env.orchestrator().generate(alice.credential(), command, reserved).candidate
    clock = FakeClock()
    other = env.orchestrator(clock, claude=FakeClaude(clock, text="Texte DIFFÉRENT.\n<!--ORYX_STEP:business-->"))
    second = other.generate(alice.credential(), command, reserved).candidate  # même bail, même epoch
    assert second.payload.analysis != first.payload.analysis
    done = finalized(env, alice, first)
    canonical = env.delivery_rows()[0]["payload"]
    _recognized(env, alice, second, reserved.value.lease, done.assistant_delivery_id)
    assert env.delivery_rows()[0]["payload"] == canonical and "DIFFÉRENT" not in canonical


@pytest.mark.parametrize("damage, expected", [
    ("lease_epoch = lease_epoch + 1", REVISION_CONFLICT),
    ("resolved_ticker = 'AI.PA'", REVISION_CONFLICT),
    ("request_fingerprint = repeat('0', 64)", IDEMPOTENCY_CONFLICT),
    ("fingerprint_key_version = 'd9'", UNAVAILABLE),
    ("resolved_ticker = NULL", UNAVAILABLE),
], ids=["wrong_epoch", "other_resolved_ticker", "divergent_intent", "unknown_key", "missing_ticker"])
def test_already_finalized_reservation_mismatches(env, alice, damage, expected):
    _, lease, candidate = env.generate(alice, next_turn())
    finalized(env, alice, candidate)
    env.force(lease.reservation_id, damage)
    before = env.full_snapshot()
    outcome = env.finalize(alice, candidate)
    assert (outcome.status, outcome.value) == (expected, None)
    assert env.full_snapshot() == before


def test_unavailable_old_key_is_never_replaced_by_the_current_key(env, alice):
    _, lease, candidate = env.generate(alice, next_turn())  # clé d1
    finalized(env, alice, candidate)
    rotated = decryptage_keyring({"d2": env.decryptage_keys.key("d2")}, "d2")  # d1 indisponible
    outcome = env.finalize(alice, candidate, decryptage_keys=rotated)
    assert (outcome.status, outcome.value) == (UNAVAILABLE, None)


@pytest.mark.parametrize("damage, expected", [
    ("request_fingerprint = repeat('0', 64)", REVISION_CONFLICT),
    ("delivery_schema_version = 'assistant-delivery-v9'", UNAVAILABLE),
    ("user_id = :b", UNAVAILABLE),
    ("conversation_key = 'conv-autre'", UNAVAILABLE),
], ids=["historical_fingerprint", "schema", "foreign_owner", "other_identity"])
def test_already_finalized_linked_delivery_mismatches(env, alice, bob, damage, expected):
    _, lease, candidate = env.generate(alice, next_turn())
    done = finalized(env, alice, candidate)
    with env.observer.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))  # contourne les FK (base jetable)
        conn.execute(sa.text(f"UPDATE assistant_deliveries SET {damage} WHERE id = :d"),
                     {"d": done.assistant_delivery_id, "b": USER_B})
    before = env.full_snapshot()
    outcome = env.finalize(alice, candidate)
    assert (outcome.status, outcome.value) == (expected, None)
    assert env.full_snapshot() == before


def test_already_finalized_with_an_absent_linked_delivery_is_unavailable(env, alice):
    _, lease, candidate = env.generate(alice, next_turn())
    done = finalized(env, alice, candidate)
    with env.observer.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        conn.execute(sa.text("DELETE FROM decryptage_cognitive_links WHERE assistant_delivery_id = :d"),
                     {"d": done.assistant_delivery_id})
        conn.execute(sa.text("DELETE FROM assistant_deliveries WHERE id = :d"), {"d": done.assistant_delivery_id})
    outcome = env.finalize(alice, candidate)
    assert (outcome.status, outcome.value) == (UNAVAILABLE, None)  # aucune réparation automatique


def test_a_blocked_reservation_returns_only_its_terminal_view(env, alice):
    _, lease, candidate = env.generate(alice, next_turn())
    env.force(lease.reservation_id, "state = 'blocked', blocked_reason = 'attempts_exhausted', "
                                    "lease_token_hash = NULL, lease_expires_at = NULL")
    before = env.full_snapshot()
    outcome = env.finalize(alice, candidate)
    assert outcome.value == dr.ReservationBlocked(lease.reservation_id, "attempts_exhausted")
    assert env.full_snapshot() == before  # aucune réactivation


# --------------------------------------------------------------------------
# 5. Bail et propriétaire
# --------------------------------------------------------------------------

def _with_lease(candidate, **changes):
    lease = dataclasses.replace(candidate.lease, **changes)
    return dataclasses.replace(candidate, lease=lease, prepared_lease_expires_at=lease.lease_expires_at)


@pytest.mark.parametrize("case", ["token", "expired", "released"])
def test_a_stale_capability_never_finalizes(env, alice, case):
    _, lease, candidate = env.generate(alice, next_turn())
    if case == "token":
        candidate = _with_lease(candidate, token=dr.LeaseToken.generate())
    elif case == "expired":
        env.expire(lease.reservation_id)
    else:
        env.force(lease.reservation_id, "lease_token_hash = NULL, lease_expires_at = NULL, "
                                        "retry_not_before_at = clock_timestamp()")
    before = env.full_snapshot()
    log = _statements_log(env.engine)
    assert env.finalize(alice, candidate).status == REVISION_CONFLICT
    assert not [s for s in log if s.startswith("SAVEPOINT")]  # B1 vérifiée AVANT toute mutation de livraison
    assert env.full_snapshot() == before


def test_an_old_epoch_never_finalizes_a_new_attempt(env, alice):
    _, lease, candidate = env.generate(alice, next_turn())
    env.force(lease.reservation_id, "lease_epoch = lease_epoch + 1")  # nouvelle attribution (même jeton haché)
    before = env.full_snapshot()
    assert env.finalize(alice, candidate).status == REVISION_CONFLICT
    assert env.full_snapshot() == before


def test_another_principal_cannot_finalize_a_candidate(env, alice, bob):
    _, lease, candidate = env.generate(alice, next_turn())
    before = env.full_snapshot()
    log = _statements_log(env.engine)
    outcome = env.finalize(bob, candidate)
    assert (outcome.status, outcome.value) == (NOT_FOUND, None)
    assert not [s for s in log if "decryptage_turn_reservations" in s]  # rien n'est lu ni verrouillé
    assert env.full_snapshot() == before


def test_a_divergent_intention_is_an_idempotency_conflict(env, alice):
    _, lease, candidate = env.generate(alice, next_turn())
    env.force(lease.reservation_id, "request_fingerprint = repeat('0', 64)")
    assert env.finalize(alice, candidate).status == IDEMPOTENCY_CONFLICT


# --------------------------------------------------------------------------
# 6. Collisions legacy (séquentielles ; concurrentes : fichier de concurrence)
# --------------------------------------------------------------------------

def _legacy_delivery(env, candidate, fingerprint):
    with Session(env.legacy_engine) as db:
        delivery, created = ad.claim_delivery(
            db, user_id=USER_A, conversation_key=candidate.turn.conversation_key, surface=ad.DECRYPTAGE_SURFACE,
            source_user_turn_id=candidate.turn.client_turn_id, delivery_ordinal=1, request_fingerprint=fingerprint,
            visible_content_fingerprint=ad.visible_content_fingerprint("Réponse historique."),
            response_payload={"analysis": "Réponse historique."}, private_metadata={},
            delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION)
        assert created
        db.commit()
        return delivery.id


@pytest.mark.parametrize("same_fingerprint", [False, True], ids=["different_sha", "identical_sha"])
def test_a_preexisting_legacy_delivery_is_never_adopted_and_blocks(env, alice, same_fingerprint):
    started(env, alice)
    lease, candidate = pending_contribution(env, alice)
    fingerprint = bridge._delivery_request_fingerprint(candidate) if same_fingerprint else "f" * 64
    legacy_id = _legacy_delivery(env, candidate, fingerprint)
    before = env.full_snapshot()
    outcome = env.finalize(alice, candidate)
    assert outcome.status == AUTHORIZED and outcome.value == dfin.LegacyCollisionBlocked(lease.reservation_id)
    after = env.full_snapshot()
    row = env.reservation(lease.reservation_id)
    assert (row["state"], row["blocked_reason"], row["assistant_delivery_id"], row["lease_token_hash"]) == (
        "blocked", "legacy_collision", None, None)  # jamais liée à la livraison legacy
    for table in ("assistant_deliveries", "decryptage_cognitive_links", "cognitive_events", "analysis_sessions",
                  "user_statements", "analysis_facts", "investment_theses"):
        assert after[table] == before[table], table  # aucune capture ni progression F2
    assert [d["id"] for d in after["assistant_deliveries"] if d["id"] == legacy_id]


def test_a_collision_whose_lease_expires_after_a_pre_read_instant_is_never_blocked(env, alice, monkeypatch):
    """reserved -> blocked / legacy_collision : gagnant prouvé, mais bail
    expiré AVANT l'instruction de blocage (un instant lu avant la pause ne
    l'autorise plus) : rien n'est committé, B1 inchangée, la livraison legacy
    n'est ni adoptée ni modifiée."""
    started(env, alice)
    lease, candidate = pending_contribution(env, alice)
    legacy_id = _legacy_delivery(env, candidate, "f" * 64)
    _lease_expiring_soon(env, lease.reservation_id)
    reserved = env.reservation(lease.reservation_id)
    before = env.full_snapshot()
    window = ExpiresBeforeTheWrite(monkeypatch, env, "_BLOCK_PROVEN_COLLISION")
    outcome = env.finalize(alice, candidate)
    assert (outcome.status, outcome.value) == (REVISION_CONFLICT, None)
    window.assert_refused_by_the_real_statement()
    assert "blocked_reason = 'legacy_collision'" in [st for st, _ in window.executed
                                                     if st.startswith("WITH decision AS MATERIALIZED")][0]
    assert env.full_snapshot() == before  # aucune livraison F2, aucun lien, aucune progression
    row = env.reservation(lease.reservation_id)
    assert (row["state"], row["blocked_reason"], row["lease_epoch"], row["attempts_used"], row["lease_token_hash"],
            row["xmin"]) == ("reserved", None, reserved["lease_epoch"], reserved["attempts_used"],
                             reserved["lease_token_hash"], reserved["xmin"])
    assert legacy_id in [d["id"] for d in env.delivery_rows()]  # legacy intacte (empreinte : xmin inchangé)


def test_an_unproven_collision_is_never_committed(env, alice, monkeypatch):
    """Annulation locale legacy_collision sans livraison gagnante visible :
    refus technique (503), aucun blocage."""
    _, lease, candidate = env.generate(alice, next_turn())

    def no_winner(original):
        def claim(*args, **kwargs):
            raise ad.AssistantTurnIdentityCollision("collision simulée sans gagnant")
        return claim

    _patched(monkeypatch, ad, "claim_delivery", no_winner)
    before = env.full_snapshot()
    assert env.finalize(alice, candidate).status == UNAVAILABLE
    assert env.full_snapshot() == before


# --------------------------------------------------------------------------
# 7. Parité de la méthode B2 / C1-B
# --------------------------------------------------------------------------

@pytest.mark.parametrize("basis, question, context, with_session", [
    (dcand.BASIS_ACTIVE_ANALYSIS, "Ils vendent du luxe.", "h", True),
    (dcand.BASIS_INITIAL_TURN, "", "", False),
    (dcand.BASIS_INITIAL_TURN, "Quel est le moat ?", "", False),
    (dcand.BASIS_LOOKUP, "Quel est le moat de LVMH ?", "historique", False),
    (dcand.BASIS_LOOKUP, "", "historique", False),  # repli historique sur le nom de société
    (dcand.BASIS_LOOKUP, "zzz qqq", "historique", False),  # aucune méthode
], ids=["active", "initial_empty", "initial_question", "lookup", "lookup_fallback", "lookup_none"])
def test_method_selection_is_shared_by_generation_and_finalization(env, alice, basis, question, context,
                                                                   with_session):
    if with_session:
        env.add_session()
    _, lease, candidate = env.generate(alice, next_turn(question=question, context=context))
    snapshot = candidate.preparation
    assert snapshot.method_basis == basis
    selection = dms.select_method(has_active_session=with_session, question=question, context=context,
                                  last_method_id=None, market_name=candidate.market.name, ticker=snapshot.ticker)
    assert (selection.method_id, selection.basis) == (snapshot.method_id, snapshot.method_basis)
    outcome = env.finalize(alice, candidate)
    assert outcome.status == AUTHORIZED and type(outcome.value) is dfin.DecryptageFinalized


# --------------------------------------------------------------------------
# 8. Secrets et candidat altéré
# --------------------------------------------------------------------------

def test_no_lease_secret_in_logs_views_or_errors(env, alice, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    _, lease, candidate = env.generate(alice, next_turn())
    secrets = (lease.token.reveal().hex(), lease.token.digest())
    outcomes = [env.finalize(alice, candidate), env.finalize(alice, candidate)]  # finalisé puis reconnu
    _, other_lease, other = env.generate(alice, next_turn())
    monkeypatch.setattr(_adapters, "_FINALIZE_RESERVATION",
                        _adapters._FINALIZE_RESERVATION.replace("WHERE id = :id", "WHERE FALSE AND id = :id"))
    outcomes.append(env.finalize(alice, other))  # échec global après RELEASE
    secrets += (other_lease.token.reveal().hex(), other_lease.token.digest())
    texts = [caplog.text] + [repr(o) for o in outcomes] + [repr(c.FinalizeDecryptageCandidate(candidate)),
                                                            repr(bridge.CandidateFinalization(candidate))]
    for text in texts:
        for secret in secrets:
            assert secret not in text


def test_a_candidate_altered_after_construction_is_refused_before_any_transaction(env, alice):
    """Invariants VÉRIFIABLES revérifiés sur l'objet reçu (empreinte
    financière, empreinte du snapshot, cohérence du payload). Limite connue :
    le TEXTE généré n'a aucune empreinte dans le contrat B2 ; le candidat
    reste une donnée serveur en mémoire, jamais une preuve (signalé)."""
    _, lease, candidate = env.generate(alice, next_turn())
    object.__setattr__(candidate, "market_fingerprint", "0" * 64)
    with pytest.raises(ValueError):
        c.FinalizeDecryptageCandidate(candidate)
    _, _, fresh = env.generate(alice, next_turn())
    object.__setattr__(fresh.preparation, "level_revision", fresh.preparation.level_revision + 1)
    with pytest.raises(ValueError):
        c.FinalizeDecryptageCandidate(fresh)  # empreinte du snapshot incohérente
    _, _, other = env.generate(alice, next_turn())
    object.__setattr__(other.payload, "method_used", "autre_methode")
    with pytest.raises(ValueError):
        c.FinalizeDecryptageCandidate(other)  # payload incohérent avec le snapshot
