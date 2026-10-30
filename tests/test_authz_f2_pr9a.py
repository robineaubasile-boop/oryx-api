"""R1 PR 9-A — fondations transactionnelles F2 / Décrypter, contre un VRAI
PostgreSQL (16 et 18 en CI).

Banc d'essai isolé (tests/authz_f2_pr9a_support.py) : VRAI runner
(AuthorizedTransactionRunner, plan DECRYPTAGE_MUTATION), VRAI pont ORM
privé et VRAIS services Décrypter, sous un registre fermé de TEST restauré
après chaque test. Couvre :

- atomicité A à G (livraison, contribution, thèse, ACK partiel, succès
  complet, SAVEPOINT, COMMIT externe en échec ou incertain) ;
- autorisation et propriété (justificatif invalide, expiré, révoqué,
  génération révoquée, livraison non confirmée, livraison / conversation
  étrangères, ressource absente, commande hors registre, sous-classe,
  erreur à T_authorization) : aucune opération métier avant la décision,
  aucune fuite d'existence ;
- primitive propriétaire-centrée lock_owned_delivery et non-régression des
  services historiques extraits ;
- contrat du pont : session gardée, transaction externe jamais possédée,
  aucune autre connexion, rollback_only mesuré.

Chaque écriture durable est observée par une connexion indépendante ;
xmin identique = une seule transaction a écrit.
"""
import json
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import core.db
from core import assistant_delivery as ad
from core import cognitive_capture as cc
from core import decryptage_cognitive_runtime as dcr
from core import decryptage_progress as dp
from core import interaction_identity as ii
from core.authz_f2 import _adapters
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import commands as c
from core.authz_f2.runner import AUTHORIZED, NOT_FOUND, SESSION_INVALID, UNAVAILABLE, UnknownCommand
from core.models import AnalysisFact, InvestmentThesis
from tests.authz_f2_pr9a_support import (  # noqa: F401 — fixtures
    BOB_EMAIL,
    STEPS,
    TICKER,
    USER_A,
    USER_B,
    Background,
    BenchEnv,
    BenchTurn,
    Pause,
    ack,
    bench,
    legacy,
    legacy_ack,
    legacy_turn,
    turn,
    wait_until,
)
from tests.f2_support import prepare_schema
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

CONV, CONV_B = "conv-pr9a-a", "conv-pr9a-b"


class Injected(RuntimeError):
    """Échec injecté par le test (jamais une erreur du service)."""


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def benv(pg_url, schema, bench):  # noqa: F811
    environment = BenchEnv(pg_url)
    before = environment.deadlocks()
    yield environment
    assert environment.deadlocks() == before
    environment.dispose()


@pytest.fixture
def alice(benv):
    return benv.confirmed()


def raising(exc):
    """Fonction qui lève `exc`, quels que soient ses arguments."""
    def fail(*args, **kwargs):
        raise exc
    return fail


def run(benv, issued, command, **kwargs):
    return benv.runner(**kwargs).run(issued.credential(), command)


def finalize(benv, issued, conversation_key, marker, text="", **kwargs):
    outcome = run(benv, issued, turn(conversation_key, marker, text, **kwargs))
    assert outcome.status == AUTHORIZED, outcome
    assert type(outcome.value) is bridge.TurnFinalized
    return outcome.value


def acknowledge(benv, issued, conversation_key, assistant_turn_id):
    outcome = run(benv, issued, ack(conversation_key, assistant_turn_id))
    assert outcome.status == AUTHORIZED, outcome
    assert type(outcome.value) is bridge.DeliveryAcknowledged
    return outcome.value


def walk_to(benv, issued, step, conversation_key=CONV):
    """Tours F2 + ACK jusqu'à ce que `step` soit l'étape ouverte (chaque
    contribution répond à l'étape précédente)."""
    first = finalize(benv, issued, conversation_key, "business")
    acknowledge(benv, issued, conversation_key, first.assistant_turn_id)
    for previous, marker in zip(STEPS, STEPS[1:STEPS.index(step) + 1]):
        done = finalize(benv, issued, conversation_key, marker, f"Ma réponse {previous}.")
        assert done.product_progress_action == "forward" and done.input_action == "contribution_appended"
        acknowledge(benv, issued, conversation_key, done.assistant_turn_id)
    [session] = [s for s in benv.sessions() if s["status"] == "in_progress"]
    assert session["current_step"] == step
    return session


def _count_entries(monkeypatch):
    """Compte les appels des points d'entrée du pont (inventaire inchangé)."""
    calls = []

    def counted(entry):
        def wrapper(*args):
            calls.append(entry.__name__)
            return entry(*args)
        return wrapper

    monkeypatch.setattr(bridge, "OPERATIONS", {op: counted(f) for op, f in bridge.OPERATIONS.items()})
    return calls


def in_transaction(db, sql, **params):
    return db.execute(sa.text(sql), params).scalar_one()


# --------------------------------------------------------------------------
# Succès complet (E) et transaction unique
# --------------------------------------------------------------------------

_WRITTEN = ("SELECT 'assistant_deliveries' AS t, xmin::text AS xmin FROM assistant_deliveries UNION ALL "
            "SELECT 'decryptage_cognitive_links', xmin::text FROM decryptage_cognitive_links UNION ALL "
            "SELECT 'analysis_sessions', xmin::text FROM analysis_sessions UNION ALL "
            "SELECT 'analysis_facts', xmin::text FROM analysis_facts UNION ALL "
            "SELECT 'conversation_identities', xmin::text FROM conversation_identities UNION ALL "
            "SELECT 'cognitive_events', xmin::text FROM cognitive_events UNION ALL "
            "SELECT 'support_traces', xmin::text FROM support_traces ORDER BY 1, 2")


def _record_inside(monkeypatch):
    """Relève, DANS la transaction du runner (fin du point d'entrée), son xid
    de premier niveau et les xmin des lignes Décrypter visibles."""
    seen = []

    def recording(entry):
        def wrapper(db, user_id, operation):
            result = entry(db, user_id, operation)
            top = int(db.execute(sa.text("SELECT pg_current_xact_id()::text")).scalar_one())
            seen.append((top, sorted(tuple(r) for r in db.execute(sa.text(_WRITTEN)))))
            return result
        wrapper.__name__ = entry.__name__
        return wrapper

    monkeypatch.setattr(bridge, "OPERATIONS", {op: recording(f) for op, f in bridge.OPERATIONS.items()})
    return seen


def test_e_complete_turn_and_ack_persist_in_one_external_transaction_each(benv, alice, monkeypatch):
    seen = _record_inside(monkeypatch)
    commits, checkouts = [], []
    sa.event.listen(benv.engine, "commit", lambda conn: commits.append(1))
    sa.event.listen(benv.engine.pool, "checkout", lambda *a: checkouts.append(1))
    first = finalize(benv, alice, CONV, "business")
    # Phase A (lecture, COMMIT sans écriture) puis phase B : deux emprunts,
    # deux COMMIT ; aucune autre connexion ni transaction indépendante.
    assert (len(commits), len(checkouts)) == (2, 2)
    assert (first.created, first.delivery_status, first.input_action, first.product_progress_action) == (
        True, "pending", "no_open_event", "new")
    [delivery] = benv.deliveries()
    [session] = benv.sessions()
    assert delivery["analysis_session_id"] == session["id"] == first.analysis_session_id
    # Les lignes commitées sont exactement celles écrites dans la transaction
    # du runner (xmin = son xid ou ceux de ses SAVEPOINT, tous postérieurs).
    [(top, inside)] = seen
    committed = sorted(tuple(r.values()) for r in benv.rows(_WRITTEN))
    assert committed == inside and all(int(x) >= top for _, x in committed)
    assert {t for t, _ in committed} == {"assistant_deliveries", "decryptage_cognitive_links", "analysis_sessions",
                                         "analysis_facts", "conversation_identities"}
    acked = acknowledge(benv, alice, CONV, first.assistant_turn_id)
    assert (acked.delivery_status, acked.response_action) == ("delivered", "opened_event")
    [event] = benv.events()
    [link] = benv.links()
    assert event["status"] == "open" and link["capture_state"] == "captured"
    top, inside = seen[1]
    assert sorted(tuple(r.values()) for r in benv.rows(_WRITTEN)) == inside
    assert int(event["xmin"]) >= top and int(link["xmin"]) >= top
    assert (len(commits), len(checkouts)) == (4, 4)


def test_e_full_analysis_reaches_a_thesis_through_the_bridge(benv, alice):
    walk_to(benv, alice, "risques")
    final = finalize(benv, alice, CONV, "swot_final", "Ma thèse complète.")
    assert final.product_progress_action == "forward"
    [thesis] = benv.rows("SELECT user_id, ticker, thesis_text, analysis_session_id FROM investment_theses")
    assert thesis == {"user_id": USER_A, "ticker": TICKER, "thesis_text": "Ma thèse complète.",
                      "analysis_session_id": final.analysis_session_id}
    acked = acknowledge(benv, alice, CONV, final.assistant_turn_id)
    assert acked.response_action == "closed_terminal"
    assert [e["status"] for e in benv.events()] == ["finalized"] * 5
    assert benv.sessions()[0]["status"] == "completed"


def test_retry_of_a_finalized_turn_returns_the_canonical_delivery_without_new_writes(benv, alice):
    command = turn(CONV, "business")
    first = run(benv, alice, command).value
    before = benv.snapshot()
    again = run(benv, alice, command)
    assert again.status == AUTHORIZED
    assert (again.value.assistant_turn_id, again.value.created, again.value.input_action) == (
        first.assistant_turn_id, False, None)
    assert benv.snapshot() == before
    acknowledge(benv, alice, CONV, first.assistant_turn_id)
    before = benv.snapshot()
    assert acknowledge(benv, alice, CONV, first.assistant_turn_id).response_action == "opened_event"
    assert benv.snapshot() == before  # ACK rejoué : idempotent, aucune écriture


# --------------------------------------------------------------------------
# Atomicité A-D
# --------------------------------------------------------------------------

def test_a_delivery_created_then_failure_before_contribution_capture(benv, alice, monkeypatch):
    before = benv.snapshot()
    seen = []

    def failing_capture(db, *, delivery, ticker, user_text):
        seen.append(in_transaction(db, "SELECT count(*) FROM assistant_deliveries WHERE id = :d", d=delivery.id))
        raise Injected("avant la capture")

    monkeypatch.setattr(dcr, "capture_user_turn", failing_capture)
    assert run(benv, alice, turn(CONV, "business")).status == UNAVAILABLE
    assert seen == [1]  # la livraison existait dans la transaction
    assert benv.snapshot() == before  # ni livraison, ni lien, ni progression, ni conversation


def test_b_contribution_captured_then_failure_before_progress(benv, alice, monkeypatch):
    walk_to(benv, alice, "business")
    before = benv.snapshot()
    seen = []
    original = dp.apply_construction_these_progress

    def failing_progress(db, **kwargs):
        seen.append((in_transaction(db, "SELECT count(*) FROM decryptage_cognitive_links"),
                     in_transaction(db, "SELECT jsonb_array_length(user_work_snapshot) FROM cognitive_events")))
        raise Injected("avant la progression")

    monkeypatch.setattr(dp, "apply_construction_these_progress", failing_progress)
    assert run(benv, alice, turn(CONV, "moat", "Ma réponse business.")).status == UNAVAILABLE
    assert seen == [(2, 1)]  # lien et contribution écrits dans la transaction
    monkeypatch.setattr(dp, "apply_construction_these_progress", original)
    assert benv.snapshot() == before
    [event] = benv.events()
    assert event["user_work_snapshot"] == [] and event["status"] == "open"


def test_c_thesis_created_then_failure_before_the_final_result(benv, alice, monkeypatch):
    walk_to(benv, alice, "risques")
    before = benv.snapshot()
    seen = []
    original = dp.apply_construction_these_progress

    def observed_progress(db, **kwargs):
        outcome = original(db, **kwargs)
        seen.append((db.query(InvestmentThesis).count(), outcome.analysis_session.status, outcome.action))
        return outcome

    def fail(point):
        if point == "executed":
            raise Injected("avant le résultat final")

    monkeypatch.setattr(dp, "apply_construction_these_progress", observed_progress)
    outcome = run(benv, alice, turn(CONV, "swot_final", "Ma thèse."), hook=fail)
    assert outcome.status == UNAVAILABLE and outcome.value is None
    # La progression a réellement satisfait les conditions de la thèse.
    assert seen == [(1, "completed", "forward")]
    assert benv.snapshot() == before
    assert benv.rows("SELECT count(*) AS n FROM investment_theses") == [{"n": 0}]
    assert benv.sessions()[0]["status"] == "in_progress"


def test_d_ack_partially_executed_leaves_the_delivery_pending(benv, alice, monkeypatch):
    walk_to(benv, alice, "business")
    pending = finalize(benv, alice, CONV, "business", "Ma réponse business.", reply="Reformulation.")
    assert pending.product_progress_action == "same"
    before = benv.snapshot()
    seen = []

    def failing_ack(db, *, delivery, user_id):
        seen.append((in_transaction(db, "SELECT capture_state FROM decryptage_cognitive_links "
                                        "WHERE assistant_delivery_id = :d", d=delivery.id),
                     in_transaction(db, "SELECT count(*) FROM support_traces")))
        raise Injected("avant delivered")

    monkeypatch.setattr(ad, "acknowledge_locked_delivery", failing_ack)
    assert run(benv, alice, ack(CONV, pending.assistant_turn_id)).status == UNAVAILABLE
    assert seen == [("captured", 1)]  # capture cognitive faite dans la transaction
    assert benv.snapshot() == before
    delivery = [d for d in benv.deliveries() if d["id"] == pending.assistant_turn_id][0]
    link = [lk for lk in benv.links() if lk["assistant_delivery_id"] == pending.assistant_turn_id][0]
    assert delivery["status"] == "pending" and link["capture_state"] == "awaiting_delivery"
    assert benv.rows("SELECT count(*) AS n FROM support_traces") == [{"n": 0}]


# --------------------------------------------------------------------------
# F — SAVEPOINT
# --------------------------------------------------------------------------

def _statements(engine):
    log = []
    sa.event.listen(engine, "before_cursor_execute", lambda conn, cur, st, *a: log.append(" ".join(st.split())))
    return log


@pytest.mark.parametrize("fail_after", [False, True])
def test_f_conversation_collision_in_a_savepoint_keeps_or_drops_the_outer_writes(benv, alice, fail_after):
    """Collision UNIQUE (PK conversation_identities) dans le SAVEPOINT de
    register_or_resolve_conversation : traitée localement, gagnant relu ;
    les écritures de la transaction extérieure persistent en cas de succès
    et sont TOUTES annulées par un échec ultérieur."""
    holder = benv.Sessions()
    ii.register_or_resolve_conversation(holder, user_id=USER_A, conversation_key=CONV)  # non commitée
    log = _statements(benv.engine)
    def fail_late(point):
        if fail_after and point == "executed":
            raise Injected("après la collision")

    worker = Background(lambda: run(benv, alice, turn(CONV, "business"), hook=fail_late))
    worker.start()
    try:
        wait_until(lambda: benv.waiting() == 1, message="le pont n'attend pas l'index unique")
        holder.commit()
    finally:
        holder.close()
    outcome = worker.outcome()
    assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in log)
    assert len(benv.rows("SELECT * FROM conversation_identities")) == 1
    if fail_after:
        assert outcome.status == UNAVAILABLE
        assert benv.deliveries() == [] and benv.links() == [] and benv.sessions() == []
    else:
        assert outcome.status == AUTHORIZED and outcome.value.created
        assert len(benv.deliveries()) == len(benv.links()) == len(benv.sessions()) == 1


def test_f_delivery_collision_in_the_claim_savepoint_rereads_the_winner(benv, alice):
    """Même tour inséré par une transaction historique concurrente : la
    violation uq_assistant_deliveries_turn_ordinal revient au SAVEPOINT de
    claim_delivery, le gagnant est relu (READ COMMITTED) ; aucune
    duplication, aucune capture par le perdant."""
    command = turn(CONV, "business")
    op = command.operation
    with benv.Sessions() as setup:
        ii.register_or_resolve_conversation(setup, user_id=USER_A, conversation_key=CONV)
        setup.commit()
    holder = benv.Sessions()
    winner, created = ad.claim_delivery(
        holder, user_id=USER_A, conversation_key=CONV, surface="decryptage", source_user_turn_id=op.client_turn_id,
        delivery_ordinal=1, request_fingerprint=op.request_fingerprint,
        visible_content_fingerprint=ad.visible_content_fingerprint("Réponse business."),
        response_payload=json.loads(op.response_payload), private_metadata=dcr.runtime_private_metadata("business"),
        delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION)
    assert created
    winner_id = winner.id
    log = _statements(benv.engine)
    worker = Background(lambda: run(benv, alice, command))
    worker.start()
    try:
        wait_until(lambda: benv.waiting() == 1, message="le pont n'attend pas l'index unique")
        holder.commit()
    finally:
        holder.close()
    outcome = worker.outcome()
    assert outcome.status == AUTHORIZED
    assert (outcome.value.assistant_turn_id, outcome.value.created, outcome.value.input_action) == (
        winner_id, False, None)
    assert [d["id"] for d in benv.deliveries()] == [winner_id]
    assert benv.links() == [] and benv.sessions() == []
    assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in log)


def test_f_begin_nested_flushes_pending_outer_writes_before_the_savepoint(benv, alice):
    """Mécanique SQLAlchemy mesurée dans la Session du pont : begin_nested()
    flushe les écritures en attente AVANT le SAVEPOINT (autoflush=False) ;
    le retour au SAVEPOINT ne les touche pas ; l'annulation externe les
    efface toutes."""
    from core.models import ConversationIdentity

    with benv.observer.begin() as conn:
        conn.execute(sa.text("INSERT INTO conversation_identities (conversation_key, user_id, created_at) "
                             "VALUES ('taken', :u, now())"), {"u": USER_A})
    log = _statements(benv.engine)
    with pytest.raises(Injected):
        with benv.engine.begin() as conn:
            db = bridge._BridgeSession(conn)
            db.add(ConversationIdentity(conversation_key="outer", user_id=USER_A, created_at=sa.func.now()))
            with pytest.raises(sa.exc.IntegrityError):
                with db.begin_nested():
                    db.add(ConversationIdentity(conversation_key="taken", user_id=USER_A, created_at=sa.func.now()))
                    db.flush()
            assert in_transaction(db, "SELECT count(*) FROM conversation_identities WHERE conversation_key = "
                                      "'outer'") == 1
            db._release()
            raise Injected("annulation externe")
    kinds = [s.split()[0] for s in log if not s.startswith("SELECT")]
    assert kinds[:4] == ["INSERT", "SAVEPOINT", "INSERT", "ROLLBACK"]
    assert [r["conversation_key"] for r in benv.rows("SELECT conversation_key FROM conversation_identities")] == [
        "taken"]


# --------------------------------------------------------------------------
# G — COMMIT externe en échec ou incertain
# --------------------------------------------------------------------------

def test_g_commit_rejected_by_postgresql_is_unavailable_and_never_replayed(benv, alice, monkeypatch):
    with benv.observer.begin() as conn:
        conn.execute(sa.text(
            "CREATE FUNCTION pr9a_reject() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'pr9a commit refusé'; END $$"))
        conn.execute(sa.text(
            "CREATE CONSTRAINT TRIGGER pr9a_reject AFTER INSERT ON assistant_deliveries "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION pr9a_reject()"))
    calls = _count_entries(monkeypatch)
    try:
        before = benv.snapshot()
        outcome = run(benv, alice, turn(CONV, "business"))
        assert (outcome.status, outcome.value, outcome.principal) == (UNAVAILABLE, None, None)
        assert calls == ["_finalize_turn"]  # aucune réexécution aveugle
        assert benv.snapshot() == before
    finally:
        with benv.observer.begin() as conn:
            conn.execute(sa.text("DROP TRIGGER pr9a_reject ON assistant_deliveries"))
            conn.execute(sa.text("DROP FUNCTION pr9a_reject()"))


def test_g_uncertain_commit_is_reported_unavailable_without_claiming_or_repairing(benv, alice, monkeypatch, caplog):
    """Le COMMIT aboutit côté serveur mais le client reçoit une erreur
    (perte de connexion simulée APRÈS le COMMIT) : état INCERTAIN pour le
    runner. Il rend UNAVAILABLE, ne prétend ni succès ni annulation, ne
    rejoue rien ; l'état réel (ici : commité) ne se constate que par une
    lecture ultérieure, et un rejeu du même tour est idempotent."""
    armed = []
    dialect = benv.engine.dialect
    original = type(dialect).do_commit

    def uncertain_commit(dbapi_connection):
        original(dialect, dbapi_connection)
        if armed:
            armed.clear()
            raise sa.exc.OperationalError("COMMIT", {}, Exception("connexion perdue après COMMIT (simulé)"))

    monkeypatch.setattr(dialect, "do_commit", uncertain_commit)
    command = turn(CONV, "business")
    outcome = run(benv, alice, command, hook=lambda point: armed.append(1) if point == "executed" else None)
    assert (outcome.status, outcome.value) == (UNAVAILABLE, None)
    assert "status=unavailable error=OperationalError" in caplog.text
    [delivery] = benv.deliveries()  # commité malgré l'erreur : état incertain pour l'appelant
    again = run(benv, alice, command)
    assert again.status == AUTHORIZED and not again.value.created
    assert again.value.assistant_turn_id == delivery["id"]
    assert len(benv.deliveries()) == 1 and len(benv.links()) == 1


# --------------------------------------------------------------------------
# Autorisation et propriété
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["garbage", "expired", "revoked", "generation_revoked", "pending_delivery",
                                  "authorization_error"])
def test_no_business_operation_before_the_final_decision(benv, monkeypatch, case):
    issued = benv.sign_in() if case == "pending_delivery" else benv.confirmed()
    credential = issued.credential()
    if case == "garbage":
        from core.auth_f2.service import SessionCredential
        credential = SessionCredential(uuid.uuid4(), credential.secret)
    elif case == "expired":
        benv.expire_at(issued.session_id, "clock_timestamp() - interval '1 second'")
    elif case == "revoked":
        benv.service.revoke_session(issued.session_id, "security")
    elif case == "generation_revoked":
        with benv.observer.begin() as conn:
            conn.execute(sa.text("UPDATE oryx_session_generations SET status = 'revoked', revoked_at = now(), "
                                 "revocation_reason = 'security' WHERE id = :g"), {"g": issued.generation_id})
    calls = _count_entries(monkeypatch)
    before = benv.snapshot()
    kwargs = {}
    if case == "authorization_error":
        kwargs["clock"] = raising(Injected("horloge PostgreSQL indisponible"))
    outcome = benv.runner(**kwargs).run(credential, turn(CONV, "business"))
    expected = UNAVAILABLE if case == "authorization_error" else SESSION_INVALID
    assert outcome.status == expected and outcome.value is None and outcome.principal is None
    assert calls == []
    assert benv.snapshot() == before


def test_foreign_absent_and_other_conversation_deliveries_are_indistinguishable(benv, alice):
    bob = benv.confirmed(BOB_EMAIL, USER_B)
    mine = finalize(benv, alice, CONV, "business")
    theirs = finalize(benv, bob, CONV_B, "business")
    before = benv.snapshot()
    for conversation_key, turn_id in ((CONV_B, theirs.assistant_turn_id), (CONV, theirs.assistant_turn_id),
                                      (CONV, uuid.uuid4()), ("conv-autre", mine.assistant_turn_id),
                                      (CONV_B, mine.assistant_turn_id)):
        outcome = run(benv, alice, ack(conversation_key, turn_id))
        assert (outcome.status, outcome.value, outcome.principal) == (NOT_FOUND, None, None)
    assert benv.snapshot() == before


def test_foreign_conversation_is_refused_without_any_write(benv, alice):
    bob = benv.confirmed(BOB_EMAIL, USER_B)
    finalize(benv, bob, CONV_B, "business")
    before = benv.snapshot()
    outcome = run(benv, alice, turn(CONV_B, "business"))
    assert (outcome.status, outcome.value) == (NOT_FOUND, None)
    assert benv.snapshot() == before


def test_foreign_cognitive_provenance_is_never_locked(benv, alice):
    """Donnée incohérente (lien pointant vers un event d'autrui) : refus
    fermé AVANT tout verrou de capture, rien n'est écrit."""
    bob = benv.confirmed(BOB_EMAIL, USER_B)
    theirs = finalize(benv, bob, CONV_B, "business")
    acknowledge(benv, bob, CONV_B, theirs.assistant_turn_id)
    [foreign_event] = benv.events()
    mine = finalize(benv, alice, CONV, "business")
    with benv.observer.begin() as conn:
        conn.execute(sa.text("UPDATE decryptage_cognitive_links SET response_context_event_id = :e "
                             "WHERE assistant_delivery_id = :d"), {"e": foreign_event["id"], "d": mine.assistant_turn_id})
    before = benv.snapshot()
    outcome = run(benv, alice, ack(CONV, mine.assistant_turn_id))
    assert outcome.status == UNAVAILABLE
    assert benv.snapshot() == before


# --- Propriété transitive COMPLÈTE (correctif d'audit, V2.2-01) -------------

V1 = "decryptage-cognitive-runtime-v1"


def _foreign_material(benv):
    """Event, SupportTrace et AnalysisSession de Bob (réels, via le pont)."""
    bob = benv.confirmed(BOB_EMAIL, USER_B)
    opened = finalize(benv, bob, CONV_B, "business")
    acknowledge(benv, bob, CONV_B, opened.assistant_turn_id)
    again = finalize(benv, bob, CONV_B, "business", "Réponse de Bob.", reply="Suite.")
    assert acknowledge(benv, bob, CONV_B, again.assistant_turn_id).response_action == "continued_event"
    [trace] = benv.rows("SELECT id, cognitive_event_id FROM support_traces")
    # Event d'autrui qu'aucun lien n'ouvre (l'index unique d'ouverture
    # interdit de réutiliser un event ouvert par un lien) : service réel.
    with benv.Sessions() as db:
        unopened = cc.open_event_idempotent(
            db, user_id=USER_B, conversation_key=CONV_B, source_turn_refs=(uuid.uuid4(),), segmentation_ordinal=1,
            event_origin="education", stimulus_snapshot={}, event_builder_version=cc.EVENT_BUILDER_VERSION,
            admission_version=cc.ADMISSION_VERSION).id
        db.commit()
    return {"event": trace["cognitive_event_id"], "trace": trace["id"], "session": opened.analysis_session_id,
            "unopened_event": unopened}


def _captured(benv, alice, action):
    """Livraison d'Alice déjà ACK (delivered + captured) : continued_event
    (réponse = event, trace d'aide) ou transitioned_event (réponse = nouvel
    event, distinct de la cible figée)."""
    first = finalize(benv, alice, CONV, "business")
    acknowledge(benv, alice, CONV, first.assistant_turn_id)
    if action == "continued_event":
        done = finalize(benv, alice, CONV, "business", "Ma réponse.", reply="Reformulation.")
    else:
        done = finalize(benv, alice, CONV, "moat", "Ma réponse business.")
    assert acknowledge(benv, alice, CONV, done.assistant_turn_id).response_action == action
    return done.assistant_turn_id


def _refused_without_lock(benv, alice, turn_id, foreign_table, foreign_id, caplog):
    """ACK (premier ou rejoué) refusé AVANT tout verrou de capture : la ligne
    d'autrui est tenue FOR UPDATE par un tiers pendant l'appel et le refus
    est la relecture de propriété (alerte), jamais un délai de verrou ;
    ROLLBACK intégral (état inchangé, xmin compris)."""
    before = benv.snapshot()
    blocker = benv.observer.connect()
    tx = blocker.begin()
    blocker.execute(sa.text(f"SELECT 1 FROM {foreign_table} WHERE id = :i FOR UPDATE"), {"i": foreign_id})
    try:
        outcome = run(benv, alice, ack(CONV, turn_id))
    finally:
        tx.rollback()
        blocker.close()
    assert (outcome.status, outcome.value, outcome.principal) == (UNAVAILABLE, None, None)
    assert "alert=decryptage_bridge_provenance_incoherent" in caplog.text
    assert "OperationalError" not in caplog.text
    assert benv.snapshot() == before


def _set(benv, table, key, value, **columns):
    assignments = ", ".join(f"{name} = :{name}" for name in columns)
    with benv.observer.begin() as conn:
        conn.execute(sa.text(f"UPDATE {table} SET {assignments} WHERE {key} = :k"), {"k": value, **columns})


def test_replayed_ack_with_a_foreign_response_event_is_refused(benv, alice, caplog):
    """response_event_id (distinct de la cible figée pour une transition)
    n'était pas relu avant le correctif."""
    foreign = _foreign_material(benv)
    turn_id = _captured(benv, alice, "transitioned_event")
    _set(benv, "decryptage_cognitive_links", "assistant_delivery_id", turn_id,
         response_event_id=foreign["unopened_event"])
    _refused_without_lock(benv, alice, turn_id, "cognitive_events", foreign["unopened_event"], caplog)


def test_replayed_ack_with_a_foreign_support_trace_is_refused(benv, alice, caplog):
    foreign = _foreign_material(benv)
    turn_id = _captured(benv, alice, "continued_event")
    _set(benv, "decryptage_cognitive_links", "assistant_delivery_id", turn_id, support_trace_id=foreign["trace"])
    _refused_without_lock(benv, alice, turn_id, "support_traces", foreign["trace"], caplog)


def test_replayed_ack_whose_support_trace_has_a_foreign_parent_is_refused(benv, alice, caplog):
    foreign = _foreign_material(benv)
    turn_id = _captured(benv, alice, "continued_event")
    [link] = benv.rows("SELECT support_trace_id FROM decryptage_cognitive_links WHERE assistant_delivery_id = :d",
                       d=turn_id)
    _set(benv, "support_traces", "id", link["support_trace_id"], cognitive_event_id=foreign["event"],
         sequence_no=99)
    _refused_without_lock(benv, alice, turn_id, "cognitive_events", foreign["event"], caplog)


def test_replayed_ack_whose_event_belongs_to_a_foreign_analysis_session_is_refused(benv, alice, caplog):
    foreign = _foreign_material(benv)
    turn_id = _captured(benv, alice, "transitioned_event")
    [link] = benv.rows("SELECT response_event_id FROM decryptage_cognitive_links WHERE assistant_delivery_id = :d",
                       d=turn_id)
    _set(benv, "cognitive_events", "id", link["response_event_id"], analysis_session_id=foreign["session"])
    _refused_without_lock(benv, alice, turn_id, "analysis_sessions", foreign["session"], caplog)


@pytest.mark.parametrize("action", ["continued_event", "transitioned_event"])
def test_coherent_replayed_ack_is_still_idempotent(benv, alice, action):
    turn_id = _captured(benv, alice, action)
    before = benv.snapshot()
    assert acknowledge(benv, alice, CONV, turn_id).response_action == action
    assert benv.snapshot() == before


def _v1_pending(benv, alice):
    """Livraison V1 encore pending (données historiques decryptage-cognitive-
    runtime-v1, aucune conversion par le code) : runtime et lien réécrits
    selon le contrat de 0013 (pas de contexte figé ni de progression)."""
    first = finalize(benv, alice, CONV, "business")
    acknowledge(benv, alice, CONV, first.assistant_turn_id)
    pending = finalize(benv, alice, "conv-pr9a-v1", "business")
    with benv.observer.begin() as conn:
        conn.execute(sa.text("UPDATE assistant_deliveries SET private_metadata = jsonb_set(private_metadata, "
                             "'{cognitive_runtime_version}', to_jsonb(CAST(:v AS text))) WHERE id = :d"),
                     {"v": V1, "d": pending.assistant_turn_id})
        conn.execute(sa.text("UPDATE decryptage_cognitive_links SET capture_version = :v, "
                             "response_context_event_id = NULL, product_progress_action = NULL "
                             "WHERE assistant_delivery_id = :d"), {"v": V1, "d": pending.assistant_turn_id})
    [own_event] = benv.rows("SELECT id FROM cognitive_events WHERE user_id = :u", u=USER_A)
    return pending.assistant_turn_id, own_event["id"]


def test_v1_ack_with_an_owned_context_exit_event_is_captured(benv, alice):
    turn_id, own_event = _v1_pending(benv, alice)
    _set(benv, "decryptage_cognitive_links", "assistant_delivery_id", turn_id,
         input_action="event_closed_context_change", context_exit_event_id=own_event)
    outcome = run(benv, alice, ack("conv-pr9a-v1", turn_id))
    assert outcome.status == AUTHORIZED
    assert outcome.value.delivery_status == "delivered"  # sémantique R1-C2 (V1) d'origine


def test_v1_ack_with_a_foreign_context_exit_event_is_refused(benv, alice, caplog):
    """context_exit_event_id (V1 seulement) n'était pas relu avant le
    correctif."""
    foreign = _foreign_material(benv)
    turn_id, _ = _v1_pending(benv, alice)
    _set(benv, "decryptage_cognitive_links", "assistant_delivery_id", turn_id,
         input_action="event_closed_context_change", context_exit_event_id=foreign["event"])
    before = benv.snapshot()
    outcome = run(benv, alice, ack("conv-pr9a-v1", turn_id))
    assert outcome.status == UNAVAILABLE
    assert "alert=decryptage_bridge_provenance_incoherent" in caplog.text
    assert benv.snapshot() == before
    [delivery] = [d for d in benv.deliveries() if d["id"] == turn_id]
    assert delivery["status"] == "pending"


def test_unknown_schema_version_is_refused_closed(benv, alice):
    mine = finalize(benv, alice, CONV, "business")
    with benv.observer.begin() as conn:
        conn.execute(sa.text("UPDATE assistant_deliveries SET delivery_schema_version = 'v9' WHERE id = :d"),
                     {"d": mine.assistant_turn_id})
    before = benv.snapshot()
    assert run(benv, alice, ack(CONV, mine.assistant_turn_id)).status == UNAVAILABLE
    assert benv.snapshot() == before


def test_unregistered_command_and_subclass_are_refused_before_any_transaction(benv, alice):
    @__import__("dataclasses").dataclass(frozen=True, slots=True)
    class SubTurn(BenchTurn):
        pass

    command = turn(CONV, "business")
    with pytest.raises(UnknownCommand):
        run(benv, alice, SubTurn(command.operation))
    with pytest.raises(UnknownCommand):
        run(benv, alice, command.operation)  # opération brute : jamais une commande
    assert benv.hook_calls == []


def test_bridge_refuses_operations_outside_its_closed_inventory():
    class Sub(bridge.TurnFinalization):
        __slots__ = ()

    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError("connexion touchée")

    op = turn(CONV, "business").operation
    for operation in (Sub(*(getattr(op, f) for f in op.__dataclass_fields__)), object(), "TurnFinalization"):
        with pytest.raises(bridge.UnknownOperation):
            bridge._execute(Untouchable(), None, operation)


# --------------------------------------------------------------------------
# Contrat du pont : transaction jamais possédée, aucune autre connexion
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["commit", "rollback", "close", "reset", "invalidate", "begin", "connection"])
def test_service_trying_to_drive_the_transaction_is_refused_and_rolled_back(benv, alice, monkeypatch, method):
    before = benv.snapshot()
    original = dcr.capture_user_turn

    def rogue(db, **kwargs):
        link = original(db, **kwargs)
        getattr(db, method)()
        return link

    monkeypatch.setattr(dcr, "capture_user_turn", rogue)
    assert run(benv, alice, turn(CONV, "business")).status == UNAVAILABLE
    assert benv.snapshot() == before


def test_swallowed_flush_failure_is_never_reported_as_success(benv, alice, monkeypatch, caplog):
    """Mesuré : une erreur de flush hors SAVEPOINT termine la transaction
    externe (rollback_only), puis le bloc du runner sortirait SANS erreur et
    sans COMMIT. Un service qui avalerait l'erreur est détecté : 503."""
    before = benv.snapshot()
    original = dcr.capture_user_turn

    def swallowing(db, **kwargs):
        link = original(db, **kwargs)
        db.add(AnalysisFact(user_id="inconnu", ticker=TICKER, fact_type="roe", fact_value=1.0))  # FK violée
        try:
            db.flush()
        except sa.exc.IntegrityError:
            pass  # erreur avalée : la transaction externe est déjà annulée
        return link

    monkeypatch.setattr(dcr, "capture_user_turn", swallowing)
    outcome = run(benv, alice, turn(CONV, "business"))
    assert outcome.status == UNAVAILABLE
    assert benv.snapshot() == before
    # Défense mesurée qui l'arrête : le flush final explicite du pont
    # (Session à annuler) ; à défaut, _require_live (test suivant).
    assert "error=PendingRollbackError" in caplog.text


def test_require_live_detects_an_external_transaction_ended_behind_the_runner(benv):
    """Mesuré : une Session rollback_only ordinaire qui annule termine la
    transaction externe ; le contrôle du pont le constate (TransactionLost),
    jamais un succès."""
    with pytest.raises(bridge.TransactionLost):
        with benv.engine.begin() as conn:
            outer = conn.get_transaction()
            bridge._require_live(conn, outer)
            plain = Session(bind=conn, autoflush=False, join_transaction_mode="rollback_only")
            plain.execute(sa.text("SELECT 1"))
            plain.rollback()
            plain.close()
            assert not outer.is_active
            bridge._require_live(conn, outer)


def test_swallowed_statement_error_is_never_reported_as_success(benv, alice, monkeypatch, caplog):
    """Mesuré : une erreur d'INSTRUCTION avalée laisse la transaction
    PostgreSQL avortée (SQLAlchemy la croit active) ; son COMMIT deviendrait
    un ROLLBACK silencieux. La sonde du pont la refuse : 503, rien n'est
    écrit."""
    before = benv.snapshot()
    original = dcr.capture_user_turn

    def swallowing(db, **kwargs):
        link = original(db, **kwargs)
        try:
            db.execute(sa.text("SELECT 1 / 0"))
        except sa.exc.DBAPIError:
            pass
        return link

    monkeypatch.setattr(dcr, "capture_user_turn", swallowing)
    outcome = run(benv, alice, turn(CONV, "business"))
    assert outcome.status == UNAVAILABLE
    assert "error=InternalError" in caplog.text  # InFailedSqlTransaction de la sonde
    assert benv.snapshot() == before


def test_bridge_never_uses_sessionlocal_nor_another_connection(benv, alice, monkeypatch):
    """Entre T_authorization et la fin de l'exécution : aucun emprunt ni
    aucune connexion d'aucun pool, aucun usage de core.db (SessionLocal,
    engine) ; une seule connexion serveur (pg_backend_pid) pour la phase B."""
    class Forbidden:
        def __call__(self, *a, **k):
            raise AssertionError("SessionLocal / moteur applicatif utilisé")

        def __getattr__(self, name):
            raise AssertionError("SessionLocal / moteur applicatif utilisé")

    monkeypatch.setattr(core.db, "SessionLocal", Forbidden())
    monkeypatch.setattr(core.db, "engine", Forbidden())
    window, during = [], []

    def on_pool_event(*args):
        if window:
            during.append(args)

    sa.event.listen(sa.pool.Pool, "checkout", on_pool_event)
    sa.event.listen(sa.pool.Pool, "connect", on_pool_event)
    benv.hooks.update({"authorized": lambda: window.append(1), "executed": window.clear})
    try:
        walk_to(benv, alice, "moat")
    finally:
        sa.event.remove(sa.pool.Pool, "checkout", on_pool_event)
        sa.event.remove(sa.pool.Pool, "connect", on_pool_event)
    assert during == []
    assert benv.hook_calls.count("executed") == 4


def test_runner_rolls_back_when_the_bridge_raises_and_the_session_is_released(benv, alice, monkeypatch):
    released = []
    original = bridge._BridgeSession._release
    monkeypatch.setattr(bridge._BridgeSession, "_release", lambda self: released.append(1) or original(self))
    monkeypatch.setattr(dp, "apply_construction_these_progress",
                        raising(Injected("métier")))
    assert run(benv, alice, turn(CONV, "business")).status == UNAVAILABLE
    assert released == [1]
    assert benv.deliveries() == []


def test_cleanup_failure_never_masks_the_original_error(benv, alice, monkeypatch, caplog):
    monkeypatch.setattr(bridge._BridgeSession, "_release",
                        raising(RuntimeError("nettoyage")))
    monkeypatch.setattr(dp, "apply_construction_these_progress",
                        raising(Injected("métier")))
    assert run(benv, alice, turn(CONV, "business")).status == UNAVAILABLE
    assert "error=Injected" in caplog.text and "cleanup_failed error=RuntimeError" in caplog.text


def test_views_are_immutable_and_carry_no_orm_instance(benv, alice):
    done = finalize(benv, alice, CONV, "business")
    acked = acknowledge(benv, alice, CONV, done.assistant_turn_id)
    for view in (done, acked):
        with pytest.raises(AttributeError):
            view.delivery_status = "x"
        for field in view.__dataclass_fields__:
            assert type(getattr(view, field)) in (uuid.UUID, bool, str, type(None)), field


# --------------------------------------------------------------------------
# Primitive propriétaire-centrée (V2.2-01) et non-régression historique
# --------------------------------------------------------------------------

def test_lock_owned_delivery_filters_owner_and_conversation_inside_the_for_update(benv, alice):
    bob = benv.confirmed(BOB_EMAIL, USER_B)
    mine = finalize(benv, alice, CONV, "business")
    theirs = finalize(benv, bob, CONV_B, "business")
    log = _statements(benv.legacy_engine)
    with benv.Sessions() as db:
        assert ad.lock_owned_delivery(db, assistant_turn_id=mine.assistant_turn_id, user_id=USER_A,
                                      conversation_key=CONV).id == mine.assistant_turn_id
        for turn_id, user, key in ((theirs.assistant_turn_id, USER_A, CONV_B), (theirs.assistant_turn_id, USER_A, CONV),
                                   (mine.assistant_turn_id, USER_A, CONV_B), (uuid.uuid4(), USER_A, CONV)):
            with pytest.raises(ad.AssistantDeliveryNotFound) as refused:
                ad.lock_owned_delivery(db, assistant_turn_id=turn_id, user_id=user, conversation_key=key)
            assert str(turn_id) not in str(refused.value)
        db.rollback()
    [locking] = {s for s in log if "FOR UPDATE" in s}
    assert ("WHERE assistant_deliveries.id = %(id_1)s::UUID AND assistant_deliveries.user_id = %(user_id_1)s "
            "AND assistant_deliveries.conversation_key = %(conversation_key_1)s FOR UPDATE") in locking


def test_extracted_locked_mutations_refuse_another_owner(benv, alice):
    mine = finalize(benv, alice, CONV, "business")
    with benv.Sessions() as db:
        delivery = ad.lock_owned_delivery(db, assistant_turn_id=mine.assistant_turn_id, user_id=USER_A,
                                          conversation_key=CONV)
        with pytest.raises(ad.AssistantDeliveryOwnershipConflict):
            ad.acknowledge_locked_delivery(db, delivery=delivery, user_id=USER_B)
        with pytest.raises(ad.AssistantDeliveryOwnershipConflict):
            ad.bind_locked_analysis_session(db, delivery=delivery, user_id=USER_B,
                                            analysis_session_id=mine.analysis_session_id)
        with pytest.raises(ad.InvalidAssistantDeliveryInput):
            ad.acknowledge_locked_delivery(db, delivery=object(), user_id=USER_A)
        db.rollback()


def test_legacy_decryptage_and_ack_keep_their_behaviour(benv, alice, legacy):
    """Routes historiques inchangées (statuts, erreurs, idempotence),
    après l'extraction de _mark_delivered / _bind_locked."""
    first = legacy_turn(benv, legacy, CONV, "business")
    assert first["delivery_status"] == "pending"
    assert legacy_ack(benv, CONV, first["assistant_turn_id"])["delivery_status"] == "delivered"
    assert legacy_ack(benv, CONV, first["assistant_turn_id"])["delivery_status"] == "delivered"
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as foreign:
        legacy_ack(benv, CONV, first["assistant_turn_id"], user_id=USER_B)
    assert foreign.value.status_code == 403  # comportement historique conservé (distinct de F2)
    with pytest.raises(HTTPException) as absent:
        legacy_ack(benv, CONV, uuid.uuid4())
    assert absent.value.status_code == 404
    [event] = benv.events()
    assert event["status"] == "open"


def test_bridge_constants_match_the_historical_route():
    import api

    assert bridge.DELIVERY_ORDINAL == api.DECRYPTAGE_DELIVERY_ORDINAL
    assert bridge.CONSTRUCTION_THESE == "construction_these"
    assert c.DECRYPTAGE_MUTATION.users_lock == "FOR NO KEY UPDATE"


def test_isolation_other_than_read_committed_is_refused_with_the_decryptage_alert(benv, alice, pg_url, caplog):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool, isolation_level="REPEATABLE READ")
    try:
        before = benv.snapshot()
        outcome = benv.runner(engine=engine).run(alice.credential(), turn(CONV, "business"))
        assert outcome.status == UNAVAILABLE
        assert "alert=decryptage_isolation_refused" in caplog.text
        assert benv.snapshot() == before
    finally:
        engine.dispose()


def test_session_is_the_normative_configuration(benv):
    with benv.engine.begin() as conn:
        db = bridge._BridgeSession(conn)
        assert isinstance(db, Session) and db.autoflush is False
        assert db.join_transaction_mode == "rollback_only" and db.bind is conn
        db._release()
        assert conn.in_transaction()
