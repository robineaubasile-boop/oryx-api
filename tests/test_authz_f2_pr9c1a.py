"""R1 PR 9-C1-A — préparation / application cognitive et SAVEPOINT métier
privé, contre un VRAI PostgreSQL (16 et 18 en CI).

Banc d'essai isolé (tests/authz_f2_pr9c1a_support.py) : VRAI runner, VRAI
pont, VRAIS services, registre et inventaire de TEST restaurés après chaque
test. Aucune réservation B1 n'est lue ni écrite.

1. Prepare : aucune écriture (instructions SELECT seulement, aucune ligne
   cognitive ni pédagogique créée ou modifiée, xmin inchangés) ;
2. Apply : réutilise EXACTEMENT la session, l'ancre et le classement
   préparés (toute re-sélection rendue impossible pendant Apply) ;
   contribution à l'ancre exacte ; answered_step de provenance historique ;
3. parité legacy / F2 : mêmes résultats et même état final, scénario par
   scénario (same, forward, step_skip, retrograde, forward/terminal sans
   contribution, terminal_before_risques, analyse complète jusqu'à la
   thèse, stale_contribution_step, contexte stale, navigation entre
   tickers, swot_final sans session, absence de marqueur) ;
4. plusieurs sessions actives : legacy inchangé (la plus récente), F2 refuse ;
5. décision non réutilisable (après application, après annulation locale,
   dans un autre SAVEPOINT, une autre transaction, un autre principal) ;
6. SAVEPOINT C1 : dans la transaction du runner, aucun COMMIT propre,
   annulation locale des effets provisoires, écritures antérieures
   conservées, écriture en attente refusée, SAVEPOINT des services
   imbriqués, erreur SQL inattendue et perte de transaction : refus global.
"""
import copy
import json
import pickle
import uuid

import pytest
import sqlalchemy as sa

from core import decryptage_cognitive_runtime as dcr
from core import decryptage_progress as dp
from core import interaction_identity as ii
from core.authz_f2 import _decryptage_orm_bridge as bridge
from core.authz_f2 import decryptage_context as dc
from core.authz_f2.runner import AUTHORIZED, UNAVAILABLE
from core.models import AnalysisSession, AssistantDelivery, ConversationIdentity, DecryptageCognitiveLink
from tests.authz_f2_pr9a_support import (  # noqa: F401 — fixtures
    BOB_EMAIL,
    STEPS,
    TICKER,
    USER_A,
    USER_B,
    Background,
    BenchEnv,
    ack,
    bench,
    turn,
    wait_until,
)
from tests.authz_f2_pr9c1a_support import Expected, RehearsalOutcome, c1a, rehearsal  # noqa: F401 — fixture
from tests.f2_support import prepare_schema
from tests.test_migration_0002_analysis_sessions import pg_url  # noqa: F401 — fixture

CONV = "conv-c1a"


class Injected(RuntimeError):
    """Échec injecté par le test."""


@pytest.fixture(scope="module")
def schema(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    prepare_schema(pg_url, engine)
    engine.dispose()


@pytest.fixture
def benv(pg_url, schema, c1a):  # noqa: F811
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


def rehearse(benv, issued, *args, **kwargs) -> RehearsalOutcome:
    value = ok(run(benv, issued, rehearsal(*args, **kwargs)))
    assert type(value) is RehearsalOutcome
    return value


def acknowledge(benv, issued, conversation_key, assistant_turn_id):
    return ok(run(benv, issued, ack(conversation_key, assistant_turn_id)))


def walk_to(benv, issued, step, conversation_key=CONV):
    """Tours (chemin legacy du banc PR 9-A) + ACK jusqu'à ce que `step` soit
    l'étape ouverte, la conversation ancrée à son event."""
    first = ok(run(benv, issued, turn(conversation_key, "business")))
    acknowledge(benv, issued, conversation_key, first.assistant_turn_id)
    for previous, marker in zip(STEPS, STEPS[1:STEPS.index(step) + 1]):
        done = ok(run(benv, issued, turn(conversation_key, marker, f"Ma réponse {previous}.")))
        assert done.product_progress_action == "forward"
        acknowledge(benv, issued, conversation_key, done.assistant_turn_id)
    [session] = [s for s in benv.sessions() if s["status"] == "in_progress"]
    assert session["current_step"] == step
    return session


def open_event(benv):
    [event] = [e for e in benv.events() if e["status"] == "open"]
    return event


def in_tx(db, sql, **params):
    return db.bind.execute(sa.text(sql), params).scalar()


def raising(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"{name} appelé pendant Apply")
    return fail


# --------------------------------------------------------------------------
# 1. Prepare n'écrit rien
# --------------------------------------------------------------------------

def test_prepare_emits_only_reads_and_writes_nothing(benv, alice, c1a):
    session = walk_to(benv, alice, "moat")
    event = open_event(benv)
    observed = {}

    def start(state):
        log = []
        sa.event.listen(state.db.bind, "before_cursor_execute", lambda c, cur, st, *a: log.append(st))
        observed["log"] = log
        observed["before"] = {t: in_tx(state.db, f"SELECT count(*) FROM {t}") for t in (
            "decryptage_cognitive_links", "user_statements", "investment_theses", "cognitive_events",
            "analysis_sessions", "support_traces", "analysis_facts")}

    def check(state):
        db = state.db
        assert not (db.new or db.dirty or db.deleted)
        statements = [" ".join(s.split()) for s in observed["log"]]
        assert statements and all(s.startswith("SELECT ") for s in statements), statements
        assert any("FOR UPDATE" in s for s in statements)
        observed["after"] = {t: in_tx(db, f"SELECT count(*) FROM {t}") for t in observed["before"]}
        observed["session"] = in_tx(db, "SELECT current_step || ':' || status || ':' || xmin::text FROM "
                                        "analysis_sessions WHERE id = :s", s=session["id"])
        observed["event"] = in_tx(db, "SELECT jsonb_array_length(user_work_snapshot::jsonb) || ':' || xmin::text "
                                      "FROM cognitive_events WHERE id = :e", e=event["id"])
        state.skip_apply = True  # Prepare seul, puis RELEASE et COMMIT du runner

    c1a.at("claimed", start)
    c1a.at("prepared", check)
    before = benv.snapshot()
    outcome = rehearse(benv, alice, CONV, "chiffres", "Ma réponse moat.")
    assert outcome.savepoint == "released" and outcome.applied is None
    assert observed["after"] == observed["before"]
    assert observed["session"] == f"moat:in_progress:{session['xmin']}"
    assert observed["event"] == f"0:{event['xmin']}"
    after = benv.snapshot()
    # Seule la livraison revendiquée (phase 4, hors C1-A) est écrite : aucune
    # ligne cognitive ni pédagogique créée ni modifiée.
    for table in ("decryptage_cognitive_links", "cognitive_events", "support_traces", "analysis_sessions",
                  "user_statements", "analysis_facts", "investment_theses", "conversation_identities"):
        assert after[table] == before[table], table
    assert len(after["assistant_deliveries"]) == len(before["assistant_deliveries"]) + 1
    # La décision préparée décrit l'état verrouillé.
    context = outcome.context
    assert (context.analysis_session_id, context.current_step, context.input_action, context.answered_step) == (
        session["id"], "moat", "contribution_appended", "moat")
    assert context.anchor_event_id == context.contribution_event_id == context.response_context_event_id == \
        event["id"]


# --------------------------------------------------------------------------
# 2. Apply : aucune re-sélection, ancre exacte, answered_step historique
# --------------------------------------------------------------------------

def test_apply_reuses_exactly_the_prepared_session_anchor_and_classification(benv, alice, c1a):
    session = walk_to(benv, alice, "moat")
    event = open_event(benv)
    patches = pytest.MonkeyPatch()
    seen = {}

    def forbid_reselection(state):
        for module, name in ((dcr, "_lock_active_analysis_sessions"), (dcr, "_conversation_anchor_event_id"),
                             (dcr, "_lock_turn_events"), (dcr, "_classify_user_turn"), (dcr, "_session_ticker"),
                             (dcr, "prepare_locked_cognitive_turn"), (dp, "find_active_analysis_session"),
                             (dp, "apply_construction_these_progress"), (dcr, "capture_user_turn")):
            patches.setattr(module, name, raising(name))

    def inspect_link(state):
        db = state.db
        link = db.execute(sa.select(DecryptageCognitiveLink).where(
            DecryptageCognitiveLink.assistant_delivery_id == state.delivery.id)).scalar_one()
        seen["link"] = (link.input_action, link.input_event_id, link.input_context_event_id,
                        link.response_context_event_id, link.product_progress_action)
        seen["answered"] = dcr.answered_step(db, link=link)  # dérivation historique, après capture

    c1a.at("prepared", forbid_reselection)
    c1a.at("applied", inspect_link)
    try:
        outcome = rehearse(benv, alice, CONV, "chiffres", "Ma réponse moat.")
    finally:
        patches.undo()
    context, applied = outcome.context, outcome.applied
    assert outcome.savepoint == "released"
    assert seen["link"] == ("contribution_appended", event["id"], event["id"], event["id"], "forward")
    assert seen["answered"] == context.answered_step == "moat"
    assert applied.analysis_session_id == context.analysis_session_id == session["id"]
    assert (applied.input_action, applied.product_progress_action) == ("contribution_appended", "forward")
    # Contribution à l'event EXACT d'ancrage, UserStatement sur l'étape répondue.
    [anchor] = [e for e in benv.events() if e["id"] == event["id"]]
    assert [c["text"] for c in anchor["user_work_snapshot"]] == ["Ma réponse moat."]
    statements = benv.rows("SELECT step, statement_text, analysis_session_id FROM user_statements ORDER BY id")
    assert statements[-1] == {"step": "moat", "statement_text": "Ma réponse moat.",
                              "analysis_session_id": session["id"]}
    [current] = benv.sessions()
    assert current["current_step"] == "chiffres"
    [delivery] = [d for d in benv.deliveries() if d["id"] == outcome.assistant_turn_id]
    assert delivery["analysis_session_id"] == session["id"] and delivery["status"] == "pending"
    # L'ACK historique du pont (même runtime V2) clôt la frontière normalement.
    assert acknowledge(benv, alice, CONV, outcome.assistant_turn_id).response_action == "transitioned_event"


def test_revalidation_accepts_the_prepared_context_and_refuses_another(benv, alice, c1a):
    session = walk_to(benv, alice, "moat")
    event = open_event(benv)
    anchor = dc.PreparationAnchor(dc.ANCHOR_CURRENT, event["id"], session["id"])
    same = Expected(TICKER, anchor, dc.PreparationActiveSession(session["id"], "moat"))
    before = benv.snapshot()
    # Contexte B (autre étape ouverte) : annulation locale, rien n'est écrit.
    other = Expected(TICKER, anchor, dc.PreparationActiveSession(session["id"], "business"))
    refused = rehearse(benv, alice, CONV, "chiffres", "Ma réponse moat.", expected=other)
    assert (refused.savepoint, refused.reason, refused.applied) == ("rolled_back", "context_changed", None)
    assert benv.snapshot() == before
    # Contexte A (celui verrouillé) : appliqué tel quel.
    accepted = rehearse(benv, alice, CONV, "chiffres", "Ma réponse moat.", expected=same)
    assert accepted.savepoint == "released"
    assert accepted.applied.analysis_session_id == session["id"]
    assert accepted.applied.product_progress_action == "forward"


# --------------------------------------------------------------------------
# 3. Parité legacy / F2, scénario par scénario
# --------------------------------------------------------------------------

def T(conv, marker, text="", ticker=TICKER):
    return ("turn", conv, marker, text, ticker)


def A(conv):
    return ("ack", conv)


def _walk(conv, step):
    steps = [T(conv, "business"), A(conv)]
    for previous, marker in zip(STEPS, STEPS[1:STEPS.index(step) + 1]):
        steps += [T(conv, marker, f"Réponse {previous}."), A(conv)]
    return steps


SCENARIOS = {
    "same": [T("c1", "business"), A("c1"), T("c1", "business", "Réponse business."), A("c1")],
    "forward": _walk("c1", "moat"),
    "step_skip": [T("c1", "business"), A("c1"), T("c1", "valorisation", "Réponse business."), A("c1")],
    "retrograde": _walk("c1", "moat") + [T("c1", "business", "Réponse moat."), A("c1")],
    "forward_without_contribution": [T("c1", "business"), A("c1"), T("c1", "moat"), A("c1")],
    "terminal_before_risques": _walk("c1", "moat") + [T("c1", "swot_final", "Ma thèse."), A("c1")],
    "terminal_without_contribution": _walk("c1", "risques") + [T("c1", "swot_final"), A("c1")],
    "full_analysis": _walk("c1", "risques") + [T("c1", "swot_final", "Ma thèse finale."), A("c1")],
    "stale_contribution_step": [T("c1", "business"), A("c1"), T("c2", "business"), A("c2"),
                                T("c1", "moat", "Réponse business 1."), T("c2", "moat", "Réponse business 2."),
                                A("c1"), A("c2")],
    "stale_context": [T("c1", "business"), A("c1"), T("c2", "business"), A("c2"),
                      T("c1", "moat", "Réponse business 1."), A("c1"), T("c2", "moat", "Réponse business 2.")],
    "navigation": [T("c1", "business"), A("c1"), T("c1", "business", "Parlons d'Air Liquide.", "AI.PA"), A("c1"),
                   T("c1", "moat", "Réponse business AI.", "AI.PA"), A("c1")],
    "swot_final_without_session": [T("c1", "swot_final", "Bonjour.")],
    "no_marker": [T("c1", "business"), A("c1"), T("c1", None, "Réponse sans marqueur."), A("c1")],
}


def _play(benv, issued, steps, path, prefix):
    results, last = [], {}
    for step in steps:
        if step[0] == "ack":
            conv = f"{prefix}-{step[1]}"
            results.append(("ack", acknowledge(benv, issued, conv, last[conv]).response_action))
            continue
        _, conv, marker, text, ticker = step
        conv = f"{prefix}-{conv}"
        command = (turn(conv, marker, text, ticker=ticker) if path == "legacy"
                   else rehearsal(conv, marker, text, ticker=ticker))
        outcome = run(benv, issued, command)
        if outcome.status != AUTHORIZED:
            results.append(("refused", outcome.status))
            continue
        value = outcome.value
        if path == "legacy":
            results.append((value.input_action, value.product_progress_action))
        else:
            assert value.savepoint == "released"
            results.append((value.applied.input_action, value.applied.product_progress_action))
        last[conv] = value.assistant_turn_id
    return results


def _state(benv, user_id):
    """État Décrypter d'un utilisateur, identifiants remplacés par leur rang."""
    sessions = benv.rows("SELECT id, ticker, status, current_step, completed_at IS NOT NULL AS done "
                         "FROM analysis_sessions WHERE user_id = :u ORDER BY started_at, id", u=user_id)
    sid = {s["id"]: i for i, s in enumerate(sessions)} | {None: None}
    events = benv.rows("SELECT id, status, analysis_session_id, user_work_snapshot FROM cognitive_events "
                       "WHERE user_id = :u ORDER BY started_at, id", u=user_id)
    eid = {e["id"]: i for i, e in enumerate(events)} | {None: None}
    deliveries = benv.rows("SELECT d.status, d.analysis_session_id, l.input_action, l.input_event_id, "
                           "l.input_context_event_id, l.response_context_event_id, l.capture_state, "
                           "l.response_action, l.response_event_id, l.product_progress_action, "
                           "l.support_trace_id IS NOT NULL AS traced FROM assistant_deliveries d "
                           "LEFT JOIN decryptage_cognitive_links l ON l.assistant_delivery_id = d.id "
                           "WHERE d.user_id = :u ORDER BY d.generated_at, d.id", u=user_id)
    return {
        "sessions": [(s["ticker"], s["status"], s["current_step"], s["done"]) for s in sessions],
        "events": [(e["status"], sid[e["analysis_session_id"]], [c["text"] for c in e["user_work_snapshot"]])
                   for e in events],
        "deliveries": [(d["status"], sid[d["analysis_session_id"]], d["input_action"], eid[d["input_event_id"]],
                        eid[d["input_context_event_id"]], eid[d["response_context_event_id"]], d["capture_state"],
                        d["response_action"], eid[d["response_event_id"]], d["product_progress_action"],
                        d["traced"]) for d in deliveries],
        "statements": [(r["step"], r["statement_text"], sid[r["analysis_session_id"]]) for r in benv.rows(
            "SELECT step, statement_text, analysis_session_id FROM user_statements WHERE user_id = :u ORDER BY id",
            u=user_id)],
        "theses": [(r["thesis_text"], sid[r["analysis_session_id"]]) for r in benv.rows(
            "SELECT thesis_text, analysis_session_id FROM investment_theses WHERE user_id = :u ORDER BY id",
            u=user_id)],
        "facts": [(r["fact_type"], sid[r["analysis_session_id"]]) for r in benv.rows(
            "SELECT fact_type, analysis_session_id FROM analysis_facts WHERE user_id = :u ORDER BY id", u=user_id)],
    }


EXPECTED_LAST = {
    "same": ("contribution_appended", "same"),
    "forward": ("contribution_appended", "forward"),
    "step_skip": ("contribution_appended", "step_skip"),
    "retrograde": ("contribution_appended", "retrograde_marker"),
    "forward_without_contribution": ("no_user_contribution", "forward_without_contribution"),
    "terminal_before_risques": ("contribution_appended", "terminal_before_risques"),
    "terminal_without_contribution": ("no_user_contribution", "terminal_without_contribution"),
    "full_analysis": ("contribution_appended", "forward"),
    "stale_contribution_step": ("contribution_appended", "stale_contribution_step"),
    "stale_context": ("refused", UNAVAILABLE),
    "navigation": ("contribution_appended", "forward"),
    "swot_final_without_session": ("no_open_event", None),
    "no_marker": ("contribution_appended", None),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_prepared_path_matches_the_legacy_rules_scenario_by_scenario(benv, alice, bob, scenario, caplog):
    steps = SCENARIOS[scenario]
    legacy = _play(benv, alice, steps, "legacy", "legacy")
    prepared = _play(benv, bob, steps, "f2", "f2")
    assert prepared == legacy
    turns = [r for r in legacy if r[0] != "ack"]
    if scenario == "stale_contribution_step":
        assert turns[-2:] == [("contribution_appended", "forward"), ("contribution_appended", "stale_contribution_step")]
    else:
        assert turns[-1] == EXPECTED_LAST[scenario]
    assert _state(benv, USER_B) == _state(benv, USER_A)
    state = _state(benv, USER_A)
    if scenario == "full_analysis":
        assert state["theses"] == [("Ma thèse finale.", 0)] and state["sessions"][0][1:] == ("completed", "swot_final",
                                                                                              True)
    if scenario in ("terminal_before_risques", "terminal_without_contribution"):
        assert state["theses"] == [] and state["sessions"][0][1] == "in_progress"
    if scenario == "navigation":
        assert [s[0] for s in state["sessions"]] == [TICKER, "AI.PA"]
        assert ("context_switched", "new") in turns  # ancre MC.PA détachée, jamais fermée ni enrichie
    if scenario == "stale_context":
        assert caplog.text.count("error=StaleConversationContext") == 2


def test_multiple_active_sessions_legacy_keeps_the_most_recent_f2_refuses(benv, alice, caplog):
    session = walk_to(benv, alice, "moat")
    with benv.observer.begin() as conn:  # anomalie : seconde tentative in_progress, plus récente
        newer = conn.execute(sa.text(
            "INSERT INTO analysis_sessions (id, user_id, ticker, status, current_step, started_at, updated_at) "
            "VALUES (gen_random_uuid(), :u, :t, 'in_progress', 'business', now() + interval '1 hour', now()) "
            "RETURNING id"), {"u": USER_A, "t": TICKER}).scalar()
    before = benv.snapshot()
    # F2 : refus fermé, aucune écriture (jamais la plus récente par défaut).
    assert run(benv, alice, rehearsal("conv-other", "business")).status == UNAVAILABLE
    assert "error=AmbiguousActiveAnalysisSessions" in caplog.text
    assert benv.snapshot() == before
    # Legacy : comportement historique inchangé, la plus récente est retenue.
    legacy = ok(run(benv, alice, turn("conv-other", "business")))
    assert legacy.analysis_session_id == newer != session["id"]


# --------------------------------------------------------------------------
# 4. Décision préparée non réutilisable
# --------------------------------------------------------------------------

def _apply(state, scope=None, prepared=None, user_id=None):
    t = state.operation.turn
    return bridge._apply_prepared_turn(state.db, scope or state.scope, user_id or state.user_id,
                                       prepared or state.prepared, method_id=t.method_id, step_marker=t.step_marker,
                                       financial_snapshot=t.financial_snapshot)


def test_prepared_decision_is_single_use_and_private(benv, alice, c1a):
    walk_to(benv, alice, "moat")
    refusals = []

    def misuse_before_apply(state):
        prepared = state.prepared
        for attempt in (lambda: pickle.dumps(prepared), lambda: copy.copy(prepared), lambda: copy.deepcopy(prepared),
                        lambda: pickle.dumps(prepared._decision), lambda: copy.copy(prepared._decision),
                        lambda: copy.deepcopy(prepared._decision), lambda: pickle.dumps(prepared.context)):
            with pytest.raises(TypeError):
                attempt()
        for target in (prepared, prepared._decision):
            with pytest.raises(AttributeError):
                target._state = "prepared"
        assert "Ma réponse" not in repr(prepared) and "Ma réponse" not in repr(prepared._decision)
        with pytest.raises(dcr.PreparedCognitiveTurnUnusable):
            dcr.PreparedCognitiveTurn(object(), state.db, state.delivery, TICKER, "x", None, None)
        with pytest.raises(bridge.PreparedTurnUnusable):
            bridge._PreparedTurn(object(), state.scope, state.user_id, prepared._decision)
        # Autre principal : refusé, et la décision est consommée (jamais réessayée).
        with pytest.raises(bridge.PreparedTurnUnusable):
            _apply(state, user_id=USER_B)
        refusals.append("other_principal")
        with pytest.raises(bridge.PreparedTurnUnusable):
            _apply(state)
        refusals.append("consumed")

    c1a.at("prepared", misuse_before_apply)
    before = benv.snapshot()
    assert run(benv, alice, rehearsal(CONV, "chiffres", "Ma réponse moat.")).status == UNAVAILABLE
    assert refusals == ["other_principal", "consumed"]
    assert benv.snapshot() == before


def test_prepared_decision_cannot_be_applied_twice(benv, alice, c1a):
    walk_to(benv, alice, "moat")
    c1a.at("applied", lambda state: _apply(state))
    before = benv.snapshot()
    assert run(benv, alice, rehearsal(CONV, "chiffres", "Ma réponse moat.")).status == UNAVAILABLE
    assert benv.snapshot() == before


def test_prepared_decision_is_dead_after_a_local_rollback(benv, alice, c1a):
    session = walk_to(benv, alice, "moat")
    refusals = []

    def rollback(state):
        raise bridge._LocalRollback(bridge.CONTEXT_CHANGED)

    def reuse(state):
        db, prepared = state.db, state.prepared
        decision = prepared._decision
        # Objets chargés dans le SAVEPOINT annulé : détachés (expunge_all),
        # jamais relus silencieusement sans verrou.
        assert sa.inspect(decision.analysis_session).session is None
        with pytest.raises(bridge.PreparedTurnUnusable):
            _apply(state)  # SAVEPOINT annulé
        refusals.append("same_scope")
        with pytest.raises(dcr.PreparedCognitiveTurnUnusable):
            dcr.apply_prepared_cognitive_turn(db, prepared=decision)  # défense du runtime : références détachées
        refusals.append("runtime")
        fresh = db.get(AnalysisSession, session["id"])  # rechargée hors de toute décision
        with pytest.raises(dp.PreparedAnalysisSessionMismatch):
            dp.apply_prepared_construction_these_progress(
                db, analysis_session=decision.analysis_session, user_id=state.user_id, ticker=TICKER,
                step="chiffres", thesis_text=None, data=None, user_contribution=False, answered_step=None)
        refusals.append("progress")
        db.expunge(fresh)
        with bridge._business_savepoint(db) as other:
            with pytest.raises(bridge.PreparedTurnUnusable):
                _apply(state, scope=other)  # nouveau SAVEPOINT : jamais la décision de l'ancien
            refusals.append("other_scope")

    c1a.at("prepared", rollback)
    c1a.at("after_savepoint", reuse)
    before = benv.snapshot()
    outcome = rehearse(benv, alice, CONV, "chiffres", "Ma réponse moat.")
    assert (outcome.savepoint, outcome.reason) == ("rolled_back", "context_changed")
    assert refusals == ["same_scope", "runtime", "progress", "other_scope"]
    assert benv.snapshot() == before


def test_prepared_decision_never_crosses_transactions(benv, alice, c1a):
    walk_to(benv, alice, "moat")
    kept = {}

    def keep(state):
        kept["prepared"], kept["decision"] = state.prepared, state.prepared._decision
        raise bridge._LocalRollback(bridge.CONTEXT_CHANGED)

    c1a.at("prepared", keep)
    assert rehearse(benv, alice, CONV, "chiffres", "Ma réponse moat.").savepoint == "rolled_back"
    refusals = []

    def replay(state):
        with pytest.raises(bridge.PreparedTurnUnusable):
            _apply(state, prepared=kept["prepared"])
        refusals.append("bridge")
        with pytest.raises(dcr.PreparedCognitiveTurnUnusable):
            dcr.apply_prepared_cognitive_turn(state.db, prepared=kept["decision"])
        refusals.append("runtime")
        raise bridge._LocalRollback(bridge.CONTEXT_CHANGED)

    c1a.actions.clear()
    c1a.at("claimed", replay)
    before = benv.snapshot()
    assert rehearse(benv, alice, CONV, "chiffres", "Ma réponse moat.").savepoint == "rolled_back"
    assert refusals == ["bridge", "runtime"] and benv.snapshot() == before


def test_a_foreign_delivery_is_never_prepared(benv, alice, bob, c1a):
    foreign = ok(run(benv, bob, turn("conv-bob", "business"))).assistant_turn_id
    refused = []

    def prepare_foreign(state):
        other = state.db.get(AssistantDelivery, foreign)  # chargement direct par identifiant (test)
        with pytest.raises(bridge.ProvenanceIncoherent):
            bridge._prepare_turn(state.db, state.scope, state.user_id, other, ticker=TICKER, user_text="")
        refused.append(foreign)
        raise bridge._LocalRollback(bridge.CONTEXT_CHANGED)

    c1a.at("claimed", prepare_foreign)
    before = benv.snapshot()
    assert rehearse(benv, alice, CONV, "business").savepoint == "rolled_back"
    assert refused == [foreign] and benv.snapshot() == before


# --------------------------------------------------------------------------
# 5. SAVEPOINT C1 dans la transaction du runner
# --------------------------------------------------------------------------

def _statements(engine):
    log = []
    sa.event.listen(engine, "before_cursor_execute", lambda conn, cur, st, *a: log.append(" ".join(st.split())))
    return log


def _commits(engine):
    commits = []
    sa.event.listen(engine, "commit", lambda conn: commits.append(1))
    return commits


def test_savepoint_lives_inside_the_runner_transaction_without_its_own_commit(benv, alice, c1a):
    walk_to(benv, alice, "moat")
    log, commits = _statements(benv.engine), _commits(benv.engine)
    outcome = rehearse(benv, alice, CONV, "chiffres", "Ma réponse moat.")
    assert outcome.savepoint == "released" and outcome.applied.product_progress_action == "forward"
    assert len(commits) == 2  # phase A (lecture) et phase B du runner, rien d'autre
    control = [s for s in log if s.split()[0] in ("SAVEPOINT", "RELEASE", "ROLLBACK", "COMMIT", "BEGIN")]
    names = [s.split()[-1] for s in control]
    assert control[0] == f"SAVEPOINT {names[0]}" and control[-1] == f"RELEASE SAVEPOINT {names[0]}"
    assert names.count(names[0]) == 2 and len(control) > 2  # SAVEPOINT des services imbriqués dedans
    assert not [s for s in control if s.startswith(("ROLLBACK", "COMMIT", "BEGIN"))]
    users = next(i for i, s in enumerate(log) if "FROM users" in s and "FOR NO KEY UPDATE" in s)
    assert users < log.index(control[0])  # SAVEPOINT après l'autorisation, dans la même transaction
    assert all(s.startswith("SELECT") for s in log[log.index(control[-1]) + 1:])  # seule la sonde suit


def test_local_rollback_discards_provisional_effects_and_keeps_prior_writes(benv, alice, c1a):
    session = walk_to(benv, alice, "moat")
    event = open_event(benv)

    def prior_write(state):
        ii.register_or_resolve_conversation(state.db, user_id=state.user_id, conversation_key="conv-prior")
        state.db.flush()

    def provisional_then_rollback(state):
        assert in_tx(state.db, "SELECT count(*) FROM user_statements") == 2  # effets provisoires visibles
        raise bridge._LocalRollback(bridge.CONTEXT_CHANGED)

    c1a.at("before_savepoint", prior_write)
    c1a.at("applied", provisional_then_rollback)
    before = benv.snapshot()
    outcome = rehearse(benv, alice, CONV, "chiffres", "Ma réponse moat.")
    assert (outcome.savepoint, outcome.reason, outcome.applied) == ("rolled_back", "context_changed", None)
    after = benv.snapshot()
    for table in ("assistant_deliveries", "decryptage_cognitive_links", "cognitive_events", "support_traces",
                  "analysis_sessions", "user_statements", "analysis_facts", "investment_theses"):
        assert after[table] == before[table], table  # xmin compris : session et event intacts
    assert {r["conversation_key"] for r in after["conversation_identities"]} == {
        r["conversation_key"] for r in before["conversation_identities"]} | {"conv-prior"}
    assert [s["xmin"] for s in benv.sessions()] == [session["xmin"]] and open_event(benv)["xmin"] == event["xmin"]


def test_pending_write_before_the_savepoint_is_refused(benv, alice, c1a, caplog):
    def pending(state):
        state.db.add(ConversationIdentity(conversation_key="conv-pending", user_id=state.user_id,
                                          created_at=sa.func.now()))

    c1a.at("before_savepoint", pending)
    before = benv.snapshot()
    assert run(benv, alice, rehearsal(CONV, "business")).status == UNAVAILABLE
    assert "alert=decryptage_bridge_pending_writes" in caplog.text
    assert benv.snapshot() == before


@pytest.mark.parametrize("mode", ["raised", "swallowed_then_local_rollback", "swallowed_then_release"])
def test_unexpected_sql_error_in_the_savepoint_refuses_the_whole_operation(benv, alice, c1a, mode):
    walk_to(benv, alice, "moat")

    def prior_write(state):
        ii.register_or_resolve_conversation(state.db, user_id=state.user_id, conversation_key="conv-prior")
        state.db.flush()

    def sql_error(state):
        if mode == "raised":
            state.db.execute(sa.text("SELECT 1 / 0"))
        try:
            state.db.execute(sa.text("SELECT 1 / 0"))
        except sa.exc.DataError:
            pass  # avalée : la sous-transaction PostgreSQL reste avortée
        if mode == "swallowed_then_local_rollback":
            raise bridge._LocalRollback(bridge.CONTEXT_CHANGED)

    c1a.at("before_savepoint", prior_write)
    c1a.at("prepared", sql_error)
    before = benv.snapshot()
    assert run(benv, alice, rehearsal(CONV, "chiffres", "Ma réponse moat.")).status == UNAVAILABLE
    assert benv.snapshot() == before  # écriture antérieure au SAVEPOINT comprise : ROLLBACK du runner


def test_transaction_lost_inside_the_savepoint_is_refused(benv, alice, c1a):
    walk_to(benv, alice, "moat")

    def lose(state):
        state.db.bind.get_transaction().rollback()  # transaction du runner terminée « derrière » le pont

    c1a.at("applied", lose)
    before = benv.snapshot()
    assert run(benv, alice, rehearsal(CONV, "chiffres", "Ma réponse moat.")).status == UNAVAILABLE
    assert benv.snapshot() == before


def test_service_savepoints_nest_inside_the_c1_savepoint_on_a_real_collision(benv, alice, c1a):
    """Collision UNIQUE réelle dans le SAVEPOINT de
    register_or_resolve_conversation, IMBRIQUÉ dans le SAVEPOINT C1 : retour
    au SAVEPOINT du service seulement, gagnant relu, le SAVEPOINT C1
    continue puis est libéré."""
    holder = benv.Sessions()
    ii.register_or_resolve_conversation(holder, user_id=USER_A, conversation_key=CONV)  # non commitée
    log = _statements(benv.engine)
    worker = Background(lambda: run(benv, alice, rehearsal(CONV, "business")))
    worker.start()
    try:
        wait_until(lambda: benv.waiting() == 1, message="le SAVEPOINT du service n'attend pas l'index unique")
        holder.commit()
    finally:
        holder.close()
    outcome = ok(worker.outcome())
    assert outcome.savepoint == "released" and outcome.created
    control = [s for s in log if s.split()[0] in ("SAVEPOINT", "RELEASE", "ROLLBACK")]
    outer = control[0].split()[-1]
    rolled = [s for s in control if s.startswith("ROLLBACK TO SAVEPOINT")]
    assert rolled and all(s.split()[-1] != outer for s in rolled)  # jamais le SAVEPOINT C1
    assert control[-1] == f"RELEASE SAVEPOINT {outer}"
    assert len(benv.rows("SELECT * FROM conversation_identities")) == 1
    assert len(benv.deliveries()) == len(benv.links()) == len(benv.sessions()) == 1


def test_a_lost_claim_skips_preparation_and_releases(benv, alice, c1a):
    """Tour déjà finalisé (rejeu) : created=False, aucune préparation, aucune
    écriture nouvelle ; la livraison canonique est relue."""
    first = ok(run(benv, alice, turn(CONV, "business", turn_id=uuid.UUID(int=7))))
    before = benv.snapshot()
    outcome = rehearse(benv, alice, CONV, "business", turn_id=uuid.UUID(int=7))
    assert (outcome.created, outcome.savepoint, outcome.context, outcome.applied) == (False, "released", None, None)
    assert outcome.assistant_turn_id == first.assistant_turn_id
    assert benv.snapshot() == before


def test_prepared_payload_and_snapshot_are_the_operation_ones(benv, alice, c1a):
    """Nouvelle session (absence préparée) : faits du snapshot financier de
    l'opération, marqueur initial, aucune contribution."""
    outcome = rehearse(benv, alice, CONV, "business")
    assert (outcome.context.analysis_session_id, outcome.context.input_action) == (None, "no_open_event")
    assert outcome.applied.product_progress_action == "new"
    [session] = benv.sessions()
    assert outcome.applied.analysis_session_id == session["id"]
    facts = benv.rows("SELECT fact_type FROM analysis_facts ORDER BY id")
    assert {f["fact_type"] for f in facts} == {"operating_margin", "roe", "net_cash"}
    assert json.loads(benv.rows("SELECT response_payload::text AS p FROM assistant_deliveries")[0]["p"])[
        "analysis"] == "Réponse business."
