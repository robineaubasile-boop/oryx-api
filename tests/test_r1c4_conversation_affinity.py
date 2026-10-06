"""Tests de R1-C4 : affinité conversationnelle des CognitiveEvents Décrypter
(runtime decryptage-cognitive-runtime-v2) en multi-onglets / multi-
conversations.

Vérité de provenance visée : une réponse utilisateur est rattachée à la
tâche réellement vue dans SA conversation, et les aides qui la précèdent sont
exactement celles réellement rendues dans cette conversation.

1. Tests sans base (toujours exécutés) : ancre conversationnelle (fonction
   pure), structure de la route (pré-vérification avant Claude, 409 dédié),
   progression produit sans contribution, frontières (aucun T3+, aucun
   event swot_final, task_kind NULL).

2. Tests contre un vrai PostgreSQL : uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (sinon SKIPPÉS) :

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_r1c4_test \\
           python -m pytest tests/test_r1c4_conversation_affinity.py

   Scénarios A à K de la spec R1-C4 + concurrence réelle (deux connexions,
   pg_blocking_pids(), aucun sleep arbitraire). Les parcours appellent
   api.decryptage / api.ack_assistant_delivery (fixtures et faux appels
   externes de tests/test_assistant_delivery.py).
"""
import ast
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

import api
from core import assistant_delivery as ad
from core import cognitive_capture as cc
from core import decryptage_cognitive_runtime as dcr
from core import decryptage_progress as dp
from tests.test_assistant_delivery import (  # noqa: F401 — fixtures
    FP_A,
    PAYLOAD_A,
    STEPS,
    Sessions,
    _count,
    _product,
    _rows,
    engine,
    ext,
)
from tests.test_cognitive_capture import _backend_pid, _run_blocked, _Statements, _writes
from tests.test_decryptage_cognitive_runtime import (
    T3_AND_BEYOND,
    _ack,
    _delivery,
    _event,
    _events,
    _id,
    _link,
    _links,
    _no_t3,
    _reply,
    _send,
    _session_id,
    _t2_state,
    _traces,
    _turn,
)
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture
from tests.test_migration_0004_drop_company_analyses import _code_tokens
from tests.test_migration_0005_cognitive_support_traces import USER

V1 = "decryptage-cognitive-runtime-v1"
V2 = "decryptage-cognitive-runtime-v2"
CONV_A, CONV_B, CONV_C = "conv-a", "conv-b", "conv-c"
RUNTIME_LOGGER = "core.decryptage_cognitive_runtime"
PROGRESS_LOGGER = "core.decryptage_progress"
STALE_DETAIL = {"error": "stale_conversation_context", "retryable": False}
MODIFIED_MODULES = ("core/decryptage_cognitive_runtime.py", "core/cognitive_capture.py", "core/decryptage_progress.py")


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["opened_event", "continued_event", "continued_without_boundary_signal",
                                    "transitioned_event"])
def test_anchor_of_an_anchoring_response_is_its_event(action):
    event_id = uuid.uuid4()
    assert dcr._anchor_event_id(action, event_id) == event_id
    with pytest.raises(dcr.CognitiveLinkInvariantError):
        dcr._anchor_event_id(action, None)


@pytest.mark.parametrize("action", ["closed_terminal", "no_cognitive_action", "stale_delivery"])
def test_anchor_of_a_non_anchoring_response_is_none(action):
    assert dcr._anchor_event_id(action, None) is None
    with pytest.raises(dcr.CognitiveLinkInvariantError):
        dcr._anchor_event_id(action, uuid.uuid4())


@pytest.mark.parametrize("action", [None, "", "abandoned", "event_closed_context_change"])
def test_anchor_of_an_unknown_action_fails_closed(action):
    with pytest.raises(dcr.CognitiveLinkInvariantError):
        dcr._anchor_event_id(action, uuid.uuid4())


def test_anchor_vocabularies_partition_the_response_actions():
    assert set(dcr.ANCHORING_RESPONSE_ACTIONS) | set(dcr.NON_ANCHORING_RESPONSE_ACTIONS) == set(dcr.RESPONSE_ACTIONS)
    assert not set(dcr.ANCHORING_RESPONSE_ACTIONS) & set(dcr.NON_ANCHORING_RESPONSE_ACTIONS)
    assert dcr.ANCHORED_INPUT_ACTIONS == ("contribution_appended", "no_user_contribution")


def test_new_deliveries_are_v2_and_v1_stays_readable():
    assert dcr.runtime_private_metadata("moat")["cognitive_runtime_version"] == V2
    assert dcr.SUPPORTED_RUNTIME_VERSIONS == (V1, V2)
    assert dcr.EVENT_BUILDER_VERSION == "decryptage-event-builder-v1"  # events partagés V1 / V2


def _route():
    tree = ast.parse((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    (route,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "decryptage"]
    return ast.unparse(route)


def test_route_prechecks_before_any_external_call_and_rechecks_under_lock():
    source = _route()
    assert (source.index("normalize_ticker(") < source.index("precheck_conversation_context(")
            < source.index("fetch_financial_data(") < source.index("client.messages.create(")
            < source.index("claim_delivery(") < source.index("capture_user_turn("))
    # Pré-vérification dans sa propre transaction courte (aucune transaction
    # ouverte pendant les appels externes).
    precheck = source[source.index("precheck_conversation_context("):source.index("fetch_financial_data(")]
    assert "db.commit()" in precheck and "db.rollback()" in precheck
    assert "except StaleConversationContext" in precheck
    # Vérification finale : capturée AVANT le handler générique (pas de 500).
    claim = source[source.index("claim_delivery("):]
    assert claim.index("except StaleConversationContext") < claim.index("except Exception")
    assert "_stale_conversation_http_error(exc, phase='claim')" in claim


def test_stale_http_error_is_a_non_technical_409():
    exc = api._stale_conversation_http_error(dcr.StaleConversationContext(uuid.uuid4()), phase="claim")
    assert (exc.status_code, exc.detail) == (409, STALE_DETAIL)


def test_progress_requires_an_explicit_contribution_flag_and_answered_step():
    base = {"user_id": USER, "ticker": "X", "step": "moat", "data": None}
    with pytest.raises(TypeError):
        dp.apply_construction_these_progress(None, **base, thesis_text=None)
    with pytest.raises(TypeError):
        dp.apply_construction_these_progress(None, **base, thesis_text=None, user_contribution=False)
    with pytest.raises(TypeError):
        dp.apply_construction_these_progress(None, **base, thesis_text=None, user_contribution=None,
                                             answered_step=None)
    with pytest.raises(ValueError):  # jamais de texte produit sans contribution réelle
        dp.apply_construction_these_progress(None, **base, thesis_text="t", user_contribution=False,
                                             answered_step=None)
    # Blocker 2 : answered_step <=> contribution, et une étape TASK valide.
    for answered in (None, "swot_final", "Moat", "", "quality"):
        with pytest.raises(ValueError):
            dp.apply_construction_these_progress(None, **base, thesis_text="t", user_contribution=True,
                                                 answered_step=answered)
    with pytest.raises(ValueError):
        dp.apply_construction_these_progress(None, **base, thesis_text=None, user_contribution=False,
                                             answered_step="moat")


def test_k_no_t3_and_beyond_in_r1c4_modules():
    for path in MODIFIED_MODULES:
        tokens = _code_tokens((REPO_ROOT / path).read_text(encoding="utf-8"))
        for name in (*T3_AND_BEYOND, "Step6", "step_6", "C1", "C12"):
            assert name not in tokens.split("\n"), (path, name)
    source = (REPO_ROOT / "core" / "decryptage_cognitive_runtime.py").read_text(encoding="utf-8")
    # task_kind reste NULL, aucun event swot_final (TASK_STEPS sans swot_final).
    assert "swot_final" not in dcr.TASK_STEPS
    assert "task_kind=None" in source and "task_kind=" not in source.replace("task_kind=None", "")


def test_capture_primitive_stays_surface_agnostic():
    """La primitive transverse ne connaît ni AssistantDelivery ni lien
    Décrypter : le runtime décide des aides disponibles."""
    tokens = _code_tokens((REPO_ROOT / "core" / "cognitive_capture.py").read_text(encoding="utf-8"))
    for name in ("AssistantDelivery", "DecryptageCognitiveLink", "decryptage"):
        assert name not in tokens, name


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL — helpers
# --------------------------------------------------------------------------

def _stale(Sessions, ext, reply, question, **kwargs):  # noqa: F811
    with pytest.raises(HTTPException) as failure:
        _send(Sessions, ext, reply, question, **kwargs)
    assert (failure.value.status_code, failure.value.detail) == (409, STALE_DETAIL)


def _anchor(Sessions, conv):  # noqa: F811
    with Sessions() as session:
        return dcr._conversation_anchor_event_id(session, user_id=USER, conversation_key=conv)


def _moat_shared_by_a_and_b(Sessions, ext, engine, ticker="MC.PA"):  # noqa: F811
    """A ouvre business puis répond (contribution, marqueur moat) : E_MOAT
    s'ouvre, rendu dans A. B reprend dans une nouvelle conversation (reprise
    vide) : la réponse moat rendue dans B continue E_MOAT. A et B sont
    ancrées à E_MOAT. Retourne E_MOAT."""
    _turn(Sessions, ext, _reply("A-business", "business"), "", context="", conv=CONV_A, ticker=ticker)
    _turn(Sessions, ext, _reply("A-moat", "moat"), "A : ils vendent du luxe.", conv=CONV_A, ticker=ticker)
    _turn(Sessions, ext, _reply("B-moat", "moat"), "", context="", conv=CONV_B, ticker=ticker)
    moat = _events(engine)[-1]
    assert moat["status"] == "open"
    assert _anchor(Sessions, CONV_A) == _anchor(Sessions, CONV_B) == moat["id"]
    return moat


def _claim_v2(session, *, conv, marker=None, analysis_session_id=None, payload=None):
    delivery, created = ad.claim_delivery(
        session, user_id=USER, conversation_key=conv, surface="decryptage", source_user_turn_id=uuid.uuid4(),
        delivery_ordinal=1, request_fingerprint=FP_A, visible_content_fingerprint=FP_A,
        response_payload=payload or PAYLOAD_A, private_metadata=dcr.runtime_private_metadata(marker),
        delivery_schema_version=ad.ASSISTANT_DELIVERY_SCHEMA_VERSION)
    assert created
    if analysis_session_id is not None:
        ad.bind_analysis_session(session, delivery_id=delivery.id, analysis_session_id=analysis_session_id)
    return delivery


def _statements(engine):  # noqa: F811
    return [(s[0], s[1]) for s in _product(engine)["statements"]]


def _session_row(engine, ticker="MC.PA"):  # noqa: F811
    return [s for s in _product(engine)["sessions"] if s[0] == _session_id(engine, ticker)]


# --------------------------------------------------------------------------
# A. Event partagé, context switch
# --------------------------------------------------------------------------

def test_pg_a_context_switch_detaches_a_and_leaves_the_shared_event_to_b(engine, Sessions, ext, caplog):
    """A et B ancrées sur E_NVDA ; A passe à LVMH : détachement seulement.
    E_NVDA reste open et inchangé, l'AnalysisSession NVDA aussi ; B continue
    E_NVDA normalement."""
    _turn(Sessions, ext, _reply("N1", "business"), "", context="", conv=CONV_A, ticker="NVDA")
    _turn(Sessions, ext, _reply("N2", "business"), "", context="", conv=CONV_B, ticker="NVDA")
    [nvda] = _events(engine)
    assert _anchor(Sessions, CONV_A) == _anchor(Sessions, CONV_B) == nvda["id"]
    nvda_session, traces = _session_row(engine, "NVDA"), _traces(engine, nvda["id"])

    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        switch = _send(Sessions, ext, _reply("L1", "business"), "Et LVMH ?", conv=CONV_A, ticker="MC.PA")
    link = _link(engine, switch["assistant_turn_id"])
    assert (link["input_action"], link["input_context_event_id"], link["input_event_id"],
            link["context_exit_event_id"]) == ("context_switched", nvda["id"], None, None)
    assert f"assistant_turn={switch['assistant_turn_id']} input_action=context_switched from_event={nvda['id']}" in (
        caplog.text)
    assert _event(engine, nvda["id"]) == nvda and _traces(engine, nvda["id"]) == traces
    assert _session_row(engine, "NVDA") == nvda_session
    _ack(Sessions, switch, conv=CONV_A)
    assert _event(engine, nvda["id"]) == nvda
    assert _anchor(Sessions, CONV_A) != nvda["id"]  # A ancrée à son nouvel event LVMH

    turn = str(uuid.uuid4())
    answer = _turn(Sessions, ext, _reply("N3", "business"), "B : NVIDIA vend des GPU.", conv=CONV_B,
                   ticker="NVDA", turn=turn)
    link = _link(engine, answer["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"], link["input_context_event_id"]) == (
        "contribution_appended", nvda["id"], nvda["id"])
    [contribution] = _event(engine, nvda["id"])["user_work_snapshot"]
    assert (contribution["contribution_id"], contribution["session_ref"]) == (turn, CONV_B)
    assert _event(engine, nvda["id"])["status"] == "open"
    assert _statements(engine) == [("business", "B : NVIDIA vend des GPU.")]


# --------------------------------------------------------------------------
# B. Contribution stale
# --------------------------------------------------------------------------

def test_pg_b_stale_contribution_is_409_and_writes_nothing(engine, Sessions, ext, caplog):
    """B répond (E_MOAT finalized, E_CHIFFRES open) ; A répond ensuite à son
    ancienne question MOAT : 409 stale_conversation_context dès la
    pré-vérification (Claude n'est pas appelé), aucune écriture."""
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    _turn(Sessions, ext, _reply("B-chiffres", "chiffres"), "B : un moat de marque.", conv=CONV_B)
    old, chiffres = _events(engine)[-2:]
    assert (old["id"], old["status"], chiffres["status"]) == (moat["id"], "finalized", "open")
    state, product, calls = _t2_state(engine), _product(engine), (ext.fetch_calls, ext.claude_calls)

    with caplog.at_level(logging.INFO):
        _stale(Sessions, ext, _reply("A-x", "chiffres"), "A : ma réponse moat.", conv=CONV_A)
    assert (ext.fetch_calls, ext.claude_calls) == calls  # pré-vérification : aucun appel externe
    assert (_t2_state(engine), _product(engine)) == (state, product)
    assert "A : ma réponse moat." not in json.dumps(_events(engine), default=str)
    assert _session_row(engine)[0][2] == "chiffres"
    assert f"stale_conversation_context expected_event={moat['id']}" in caplog.text
    assert "ma réponse moat" not in caplog.text
    # Rejouer le même tour reste stale (jamais de rattachement automatique).
    _stale(Sessions, ext, _reply("A-x", "chiffres"), "A : ma réponse moat.", conv=CONV_A)
    assert (_t2_state(engine), _product(engine)) == (state, product)


@pytest.mark.parametrize("question", ["A : ma réponse moat.", ""], ids=["contribution", "reprise-vide"])
def test_pg_b_race_between_precheck_and_claim_still_fails_closed(engine, Sessions, ext, caplog, question):
    """La pré-vérification passe (E_MOAT encore open) ; pendant les appels
    externes de A, l'ACK de B fait transiter E_MOAT. La vérification finale
    sous verrou refuse le tour : 409, aucune livraison, aucun lien, aucune
    contribution, aucune progression ; Claude a bien été appelé."""
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    pending_b = _send(Sessions, ext, _reply("B-chiffres", "chiffres"), "B : un moat de marque.", conv=CONV_B)
    ext.on_fetch = lambda: _ack(Sessions, pending_b, conv=CONV_B)
    claude_calls = ext.claude_calls
    deliveries = _count(engine, "assistant_deliveries")
    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        _stale(Sessions, ext, _reply("A-x", "chiffres"), question, conv=CONV_A)
    assert ext.claude_calls == claude_calls + 1
    assert _count(engine, "assistant_deliveries") == deliveries
    assert [e["status"] for e in _events(engine)][-2:] == ["finalized", "open"]
    assert [c["session_ref"] for c in _event(engine, moat["id"])["user_work_snapshot"]] == [CONV_B]
    assert _events(engine)[-1]["user_work_snapshot"] == []
    assert f"stale_conversation_context expected_event={moat['id']}" in caplog.text


def test_pg_b_stale_conversation_can_navigate_or_be_resumed(engine, Sessions, ext):
    """Après un 409, l'utilisateur décide : une nouvelle conversation
    (« Reprendre ») reprend l'étape actuelle ; un changement de ticker dans
    la conversation stale reste une navigation valide."""
    _moat_shared_by_a_and_b(Sessions, ext, engine)
    _turn(Sessions, ext, _reply("B-chiffres", "chiffres"), "B : un moat de marque.", conv=CONV_B)
    chiffres = _events(engine)[-1]
    _stale(Sessions, ext, _reply("A-x"), "A : ma réponse moat.", conv=CONV_A)
    resume = _turn(Sessions, ext, _reply("C-chiffres", "chiffres"), "", context="", conv=CONV_C)
    assert _link(engine, resume["assistant_turn_id"])["response_event_id"] == chiffres["id"]
    assert _anchor(Sessions, CONV_C) == chiffres["id"]
    switch = _send(Sessions, ext, _reply("N1"), "Et NVIDIA ?", conv=CONV_A, ticker="NVDA")
    assert _link(engine, switch["assistant_turn_id"])["input_action"] == "context_switched"


# --------------------------------------------------------------------------
# C. ACK tardif
# --------------------------------------------------------------------------

@pytest.mark.parametrize("question, marker", [("", "moat"), ("A : ma réponse moat.", "moat")],
                         ids=["reprise-vide", "contribution"])
def test_pg_c_late_ack_is_delivered_but_stale(engine, Sessions, ext, caplog, question, marker):
    """A (ancrée E_MOAT) produit une livraison pending qui ne fait pas avancer
    la session (reprise, ou contribution dont la réponse reste sur moat) ;
    avant son ACK, B répond et fait réellement avancer E_MOAT -> E_CHIFFRES.
    L'ACK de A : delivered, lien captured stale_delivery, aucun SupportTrace,
    aucun event, E_CHIFFRES intact. (Si A avait elle-même avancé, la
    contribution de B serait périmée et ne pourrait plus créer de frontière :
    voir test_pg_boundary_*.)"""
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    pending_a = _send(Sessions, ext, _reply("A-pending", marker), question, conv=CONV_A)
    assert _link(engine, pending_a["assistant_turn_id"])["input_context_event_id"] == moat["id"]
    _turn(Sessions, ext, _reply("B-chiffres", "chiffres"), "B : un moat de marque.", conv=CONV_B)
    chiffres = _events(engine)[-1]
    assert (_event(engine, moat["id"])["status"], chiffres["status"]) == ("finalized", "open")
    events, traces = _events(engine), _traces(engine)

    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        assert _ack(Sessions, pending_a, conv=CONV_A)["delivery_status"] == "delivered"
    assert _delivery(engine, pending_a["assistant_turn_id"])["status"] == "delivered"
    link = _link(engine, pending_a["assistant_turn_id"])
    assert (link["capture_state"], link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "captured", "stale_delivery", None, None)
    assert link["captured_at"] is not None
    assert (_events(engine), _traces(engine)) == (events, traces)
    assert (f"assistant_turn={pending_a['assistant_turn_id']} action=stale_delivery expected_event={moat['id']} "
            f"current_event={chiffres['id']}") in caplog.text
    assert "A-pending" not in caplog.text
    # stale_delivery : plus d'ancre ; le tour suivant de A n'est pas une
    # contribution (et n'est pas stale).
    assert _anchor(Sessions, CONV_A) is None
    nxt = _send(Sessions, ext, _reply("A-next", "chiffres"), "A : et maintenant ?", conv=CONV_A)
    assert _link(engine, nxt["assistant_turn_id"])["input_action"] == "no_open_event"
    # Retry ACK : no-op.
    assert _ack(Sessions, pending_a, conv=CONV_A)["delivery_status"] == "delivered"
    assert _link(engine, pending_a["assistant_turn_id"]) == link


# --------------------------------------------------------------------------
# D. Provenance des aides par conversation
# --------------------------------------------------------------------------

def test_pg_d_support_refs_before_are_conversation_scoped(engine, Sessions, ext, caplog):
    """E partagé par A et B. S1 rendu dans A, S2 dans B, S3 dans A : B
    n'hérite que de S2, A de S1 + S3 (ordre chronologique) ; aucun
    SupportTrace n'est dupliqué."""
    _turn(Sessions, ext, _reply("A1", "business"), "", context="", conv=CONV_A)
    [event] = _events(engine)
    _turn(Sessions, ext, _reply("S1", "business"), "", context="h", conv=CONV_A)      # S1 dans A
    _turn(Sessions, ext, _reply("S2", "business"), "", context="", conv=CONV_B)       # S2 dans B
    _turn(Sessions, ext, _reply("S3", "business"), "", context="h", conv=CONV_A)      # S3 dans A
    s1, s2, s3 = (str(t["id"]) for t in _traces(engine, event["id"]))
    assert [t["support_payload"]["visible_content"] for t in _traces(engine, event["id"])] == [
        "Réponse S1.", "Réponse S2.", "Réponse S3."]

    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        _send(Sessions, ext, _reply("B-next", "business"), "B : ils vendent du luxe.", conv=CONV_B)
        _send(Sessions, ext, _reply("A-next", "business"), "A : une marque forte.", conv=CONV_A)
    b_work, a_work = _event(engine, event["id"])["user_work_snapshot"]
    assert (b_work["session_ref"], b_work["support_refs_before"]) == (CONV_B, [s2])
    assert (a_work["session_ref"], a_work["support_refs_before"]) == (CONV_A, [s1, s3])
    assert len(_traces(engine, event["id"])) == 3
    assert f"event={event['id']} support_refs conversation={CONV_B} count=1" in caplog.text
    assert f"event={event['id']} support_refs conversation={CONV_A} count=2" in caplog.text


def test_pg_d_aid_rendered_only_in_a_is_never_inherited_by_b(engine, Sessions, ext):
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    [shown_in_b] = [str(t["id"]) for t in _traces(engine, moat["id"])]
    _turn(Sessions, ext, _reply("S-A", "moat"), "", context="h", conv=CONV_A)  # S1 rendu dans A seulement
    s1 = str(_traces(engine, moat["id"])[-1]["id"])
    _send(Sessions, ext, _reply("B-next", "moat"), "B : un moat de marque.", conv=CONV_B)
    [b_work] = _event(engine, moat["id"])["user_work_snapshot"]
    assert b_work["support_refs_before"] == [shown_in_b] and s1 not in b_work["support_refs_before"]
    _send(Sessions, ext, _reply("A-next", "moat"), "A : un réseau de boutiques.", conv=CONV_A)
    a_work = _event(engine, moat["id"])["user_work_snapshot"][-1]
    assert a_work["support_refs_before"] == [s1]


def test_pg_d_pending_or_other_user_aids_are_never_inherited(engine, Sessions, ext):
    """Seules les aides delivered + captured comptent : une réponse rendue
    mais dont l'ACK n'a pas abouti n'est pas une aide disponible."""
    _turn(Sessions, ext, _reply("A1", "business"), "", context="", conv=CONV_A)
    [event] = _events(engine)
    shown = _turn(Sessions, ext, _reply("S1", "business"), "", context="h", conv=CONV_A)
    [trace] = _traces(engine, event["id"])
    with engine.begin() as conn:  # corruption délibérée : livraison repassée pending
        conn.execute(sa.text("UPDATE assistant_deliveries SET status = 'pending', delivered_at = NULL WHERE id = :d"),
                     {"d": _id(shown)})
    with Sessions() as session:
        refs = dcr._conversation_support_refs(session, event_id=event["id"], user_id=USER, conversation_key=CONV_A)
        other = dcr._conversation_support_refs(session, event_id=event["id"], user_id="u2", conversation_key=CONV_A)
    assert refs == () and other == ()
    assert trace["id"]


# --------------------------------------------------------------------------
# E. La dernière livraison captured définit l'ancre
# --------------------------------------------------------------------------

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _captured_delivery(session, conv, action, event_id, *, at, version=V2, delivered=True, captured=True,
                       context=None):
    delivery = _claim_v2(session, conv=conv)
    if version != V2:
        delivery.private_metadata = {**delivery.private_metadata, "cognitive_runtime_version": version}
    trace_id = None
    if action in ("continued_event", "continued_without_boundary_signal"):
        trace_id = cc.add_support_trace(session, event_id=event_id, support_kind="assistant_response",
                                        support_payload={"visible_content": "x"}).id
    response_event = event_id if action in dcr.ANCHORING_RESPONSE_ACTIONS else None
    # Décision de progression cohérente avec l'action (CHECK
    # boundary_causality, V2).
    progress_action = None
    if version == V2 and action in ("transitioned_event", "closed_terminal"):
        progress_action = "forward"
    elif version == V2 and action == "opened_event":
        progress_action = "new"
    # Cible figée cohérente avec l'action (CHECK response_context, V2).
    if version != V2 or action in ("opened_event", "no_cognitive_action", "stale_delivery"):
        response_context = None
    elif action in ("continued_event", "continued_without_boundary_signal"):
        response_context = event_id
    else:
        response_context = context
    session.add(dcr.DecryptageCognitiveLink(
        assistant_delivery_id=delivery.id, capture_version=version, input_action="no_open_event",
        input_event_id=None, input_context_event_id=None, response_context_event_id=response_context,
        product_progress_action=progress_action, context_exit_event_id=None,
        capture_state="captured" if captured else "awaiting_delivery",
        response_action=action if captured else None, response_event_id=response_event if captured else None,
        support_trace_id=trace_id if captured else None, created_at=at, captured_at=at if captured else None))
    if delivered:
        delivery.status, delivery.delivered_at = "delivered", at
    session.flush()


def _bare_events(session, n):
    return [cc.open_event_idempotent(
        session, user_id=USER, conversation_key=CONV_A, source_turn_refs=(uuid.uuid4(),), segmentation_ordinal=1,
        event_origin="decryptage", stimulus_snapshot={"visible_content": "x"},
        event_builder_version=dcr.EVENT_BUILDER_VERSION, admission_version=dcr.ADMISSION_VERSION).id
        for _ in range(n)]


@pytest.mark.parametrize("last, anchored", [
    ("closed_terminal", False), ("stale_delivery", False), ("no_cognitive_action", False),
    ("continued_event", True), ("continued_without_boundary_signal", True), ("opened_event", True),
    ("transitioned_event", True),
])
def test_pg_e_last_captured_delivery_defines_the_anchor(engine, Sessions, ext, last, anchored):
    """delivery 1 : continued_event E1 ; delivery 2 : <last>. Ancre = E2 si
    <last> ancre, sinon AUCUNE : on ne remonte jamais chercher E1."""
    with Sessions() as session:
        e1, e2 = _bare_events(session, 2)
        _captured_delivery(session, CONV_A, "continued_event", e1, at=T0)
        _captured_delivery(session, CONV_A, last, e2, at=T0 + timedelta(seconds=1), context=e1)
        session.commit()
    assert _anchor(Sessions, CONV_A) == (e2 if anchored else None)


def test_pg_e_anchor_ignores_pending_uncaptured_v1_and_other_conversations(engine, Sessions, ext):
    with Sessions() as session:
        e1, e2, e3, e4 = _bare_events(session, 4)
        _captured_delivery(session, CONV_A, "continued_event", e1, at=T0)
        # Plus récentes mais non éligibles : pending, non captured, V1, autre conversation.
        _captured_delivery(session, CONV_A, "continued_event", e2, at=T0 + timedelta(seconds=1), delivered=False)
        _captured_delivery(session, CONV_A, "opened_event", e3, at=T0 + timedelta(seconds=2), captured=False)
        _captured_delivery(session, CONV_A, "closed_terminal", None, at=T0 + timedelta(seconds=3), version=V1)
        _captured_delivery(session, CONV_B, "closed_terminal", None, at=T0 + timedelta(seconds=4), context=e2)
        _captured_delivery(session, CONV_C, "opened_event", e4, at=T0)
        session.commit()
    assert _anchor(Sessions, CONV_A) == e1
    assert _anchor(Sessions, CONV_B) is None
    assert _anchor(Sessions, CONV_C) == e4
    assert _anchor(Sessions, "conv-vierge") is None


def test_pg_e_terminal_closure_leaves_no_anchor_in_a_real_flow(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("business", "business"), conv=CONV_A)
    for previous, step in zip(STEPS, STEPS[1:]):
        last = _turn(Sessions, ext, _reply(step, step), f"réponse {previous}", conv=CONV_A)
    assert _link(engine, last["assistant_turn_id"])["response_action"] == "closed_terminal"
    assert _anchor(Sessions, CONV_A) is None
    after = _send(Sessions, ext, _reply("merci"), "Merci !", conv=CONV_A)
    assert _link(engine, after["assistant_turn_id"])["input_action"] == "no_open_event"
    assert not [e for e in _events(engine) if e["status"] == "open"]


# --------------------------------------------------------------------------
# F. Pas d'avancée produit sans contribution
# --------------------------------------------------------------------------

def _lvmh_at_moat(Sessions, ext, engine, conv=CONV_A):  # noqa: F811
    _turn(Sessions, ext, _reply("business", "business"), "", context="", conv=conv)
    _turn(Sessions, ext, _reply("moat", "moat"), "Ils vendent du luxe.", conv=conv)
    assert _session_row(engine)[0][1:3] == ("in_progress", "moat")
    return _events(engine)[-1]


def test_pg_f_empty_resume_with_a_forward_marker_never_advances(engine, Sessions, ext, caplog):
    moat = _lvmh_at_moat(Sessions, ext, engine)
    with caplog.at_level(logging.INFO):
        resume = _turn(Sessions, ext, _reply("chiffres ?", "chiffres"), "", context="h", conv=CONV_A)
    link = _link(engine, resume["assistant_turn_id"])
    assert (link["input_action"], link["input_context_event_id"]) == ("no_user_contribution", moat["id"])
    assert _session_row(engine)[0][1:4] == ("in_progress", "moat", None)
    assert "anomaly=forward_without_contribution current=moat received=chiffres" in caplog.text
    # Cognitif cohérent avec le produit : aucune frontière.
    assert (link["response_action"], link["response_event_id"]) == ("continued_without_boundary_signal", moat["id"])
    assert _event(engine, moat["id"])["status"] == "open" and _events(engine)[-1]["id"] == moat["id"]
    assert (f"anomaly=boundary_not_applied marker=chiffres current_step=moat "
            f"progress_action=forward_without_contribution event={moat['id']}") in caplog.text


def test_pg_f_context_switch_with_a_forward_marker_never_advances_the_target(engine, Sessions, ext, caplog):
    nvda_moat = _lvmh_at_moat(Sessions, ext, engine, conv=CONV_B)  # session MC.PA à moat (B)
    _turn(Sessions, ext, _reply("N1", "business"), "", context="", conv=CONV_A, ticker="NVDA")
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        switch = _turn(Sessions, ext, _reply("L", "chiffres"), "Revenons à LVMH.", conv=CONV_A)
    assert _link(engine, switch["assistant_turn_id"])["input_action"] == "context_switched"
    assert _session_row(engine)[0][1:3] == ("in_progress", "moat")
    assert "anomaly=forward_without_contribution current=moat received=chiffres" in caplog.text
    assert _event(engine, nvda_moat["id"])["status"] == "open"
    assert _statements(engine) == [("business", "Ils vendent du luxe.")]


def test_pg_f_swot_final_without_contribution_never_completes(engine, Sessions, ext, caplog):
    _turn(Sessions, ext, _reply("business", "business"), conv=CONV_A)
    for previous, step in zip(STEPS[:4], STEPS[1:5]):
        _turn(Sessions, ext, _reply(step, step), f"réponse {previous}", conv=CONV_A)
    risques = _events(engine)[-1]
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        final = _turn(Sessions, ext, _reply("bilan", "swot_final"), "", context="h", conv=CONV_A)
    assert _link(engine, final["assistant_turn_id"])["input_action"] == "no_user_contribution"
    assert _session_row(engine)[0][1:4] == ("in_progress", "risques", None)
    assert _product(engine)["theses"] == []
    assert "anomaly=terminal_without_contribution current=risques received=swot_final" in caplog.text
    assert _event(engine, risques["id"])["status"] == "open"
    assert _link(engine, final["assistant_turn_id"])["response_action"] == "continued_without_boundary_signal"


# --------------------------------------------------------------------------
# G. Avancée avec contribution (R1-C2 / R1-C3 conservés)
# --------------------------------------------------------------------------

def test_pg_g_forward_with_contribution_advances_and_transitions(engine, Sessions, ext):
    moat = _lvmh_at_moat(Sessions, ext, engine)
    answer = _send(Sessions, ext, _reply("chiffres", "chiffres"), "Un moat de marque.", conv=CONV_A)
    link = _link(engine, answer["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"], link["input_context_event_id"]) == (
        "contribution_appended", moat["id"], moat["id"])
    assert _statements(engine)[-1] == ("moat", "Un moat de marque.")
    assert _session_row(engine)[0][1:3] == ("in_progress", "chiffres")
    assert _event(engine, moat["id"])["status"] == "open"  # rien avant l'ACK
    _ack(Sessions, answer, conv=CONV_A)
    old, new = _events(engine)[-2:]
    assert (old["id"], old["status"], new["status"]) == (moat["id"], "finalized", "open")
    assert new["stimulus_snapshot"]["visible_content"] == "Réponse chiffres." and new["task_kind"] is None
    assert _link(engine, answer["assistant_turn_id"])["response_action"] == "transitioned_event"
    assert _anchor(Sessions, CONV_A) == new["id"]


def test_pg_g_full_attempt_completes_with_a_thesis_and_no_swot_event(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("business", "business"), conv=CONV_A)
    for previous, step in zip(STEPS, STEPS[1:]):
        _turn(Sessions, ext, _reply(step, step), f"réponse {previous}", conv=CONV_A)
    [session] = _product(engine)["sessions"]
    assert session[1:3] == ("completed", "swot_final") and session[3] is not None
    assert [t[0] for t in _product(engine)["theses"]] == ["réponse risques"]
    events = _events(engine)
    assert len(events) == 5 and {e["status"] for e in events} == {"finalized"}
    assert all(e["task_kind"] is None for e in events)
    _no_t3(engine)


# --------------------------------------------------------------------------
# H. Navigation + contenu dans le même message
# --------------------------------------------------------------------------

def test_pg_h_navigation_with_content_is_navigation_only(engine, Sessions, ext):
    """« Revenons à LVMH. Pour le moat, je pense que… » change réellement
    d'AnalysisSession : tout le tour est navigation, sans parsing."""
    lvmh_moat = _lvmh_at_moat(Sessions, ext, engine, conv=CONV_B)
    _turn(Sessions, ext, _reply("N1", "business"), "", context="", conv=CONV_A, ticker="NVDA")
    [nvda] = [e for e in _events(engine) if e["analysis_session_id"] == _session_id(engine, "NVDA")]
    statements = _statements(engine)
    message = "Revenons à LVMH. Pour le moat, je pense que c'est la marque."
    switch = _turn(Sessions, ext, _reply("L", "moat"), message, conv=CONV_A)
    link = _link(engine, switch["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"], link["input_context_event_id"]) == (
        "context_switched", None, nvda["id"])
    assert _statements(engine) == statements
    assert message not in json.dumps(_events(engine), default=str)
    assert _event(engine, nvda["id"])["status"] == "open"
    # La réponse rendue ancre A à E_LVMH moat : la vraie production arrive au
    # tour suivant.
    assert _anchor(Sessions, CONV_A) == lvmh_moat["id"]
    nxt = _send(Sessions, ext, _reply("L2", "moat"), "Le moat, c'est la marque.", conv=CONV_A)
    assert _link(engine, nxt["assistant_turn_id"])["input_event_id"] == lvmh_moat["id"]
    assert _statements(engine)[-1] == ("moat", "Le moat, c'est la marque.")


# --------------------------------------------------------------------------
# I. Compatibilité V1
# --------------------------------------------------------------------------

def _v1_pending(Sessions, *, conv, marker, analysis_session_id, input_action="no_user_contribution",
                input_event_id=None):
    """Livraison V1 encore pending au déploiement (lien V1 awaiting)."""
    with Sessions() as session:
        delivery = _claim_v2(session, conv=conv, marker=marker, analysis_session_id=analysis_session_id,
                             payload={**PAYLOAD_A, "analysis": f"V1 {marker}"})
        delivery.private_metadata = {**delivery.private_metadata, "cognitive_runtime_version": V1}
        session.add(dcr.DecryptageCognitiveLink(
            assistant_delivery_id=delivery.id, capture_version=V1, input_action=input_action,
            input_event_id=input_event_id, context_exit_event_id=None, capture_state="awaiting_delivery",
            created_at=T0))
        session.commit()
        return {"assistant_turn_id": str(delivery.id)}


def test_pg_i_v1_pending_delivery_ack_keeps_v1_semantics(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("A1", "business"), "", context="", conv=CONV_A)
    [event] = _events(engine)
    sid = event["analysis_session_id"]
    same = _v1_pending(Sessions, conv="conv-v1", marker="business", analysis_session_id=sid)
    assert _ack(Sessions, same, conv="conv-v1")["delivery_status"] == "delivered"
    link = _link(engine, same["assistant_turn_id"])
    [trace] = _traces(engine, event["id"])
    assert (link["capture_version"], link["input_context_event_id"], link["response_action"],
            link["support_trace_id"]) == (V1, None, "continued_event", trace["id"])
    # Sémantique V1 d'origine : un marqueur suivant est une frontière à l'ACK
    # (sans exigence de contribution, propre à V2).
    forward = _v1_pending(Sessions, conv="conv-v1", marker="moat", analysis_session_id=sid)
    assert _ack(Sessions, forward, conv="conv-v1")["delivery_status"] == "delivered"
    assert _link(engine, forward["assistant_turn_id"])["response_action"] == "transitioned_event"
    assert [e["status"] for e in _events(engine)] == ["abandoned", "open"]
    # Retry ACK V1 : no-op ; aucune conversion V1 -> V2.
    state = _t2_state(engine)
    assert _ack(Sessions, forward, conv="conv-v1")["delivery_status"] == "delivered"
    assert _t2_state(engine) == state
    assert {l["capture_version"] for l in _links(engine) if str(l["assistant_delivery_id"]) in (
        same["assistant_turn_id"], forward["assistant_turn_id"])} == {V1}


def test_pg_i_historic_v1_context_change_rows_stay_readable(engine, Sessions, ext):
    """Lignes V1 event_closed_ / event_abandoned_context_change : lisibles,
    jamais une ancre V2 ; la conversation V1 démarre sans ancre en V2."""
    _turn(Sessions, ext, _reply("A1", "business"), "", context="", conv=CONV_A)
    [event] = _events(engine)
    with Sessions() as session:
        closed = _bare_events(session, 1)[0]
        for action in ("event_closed_context_change", "event_abandoned_context_change"):
            delivery = _claim_v2(session, conv="conv-v1")
            delivery.private_metadata = {**delivery.private_metadata, "cognitive_runtime_version": V1}
            delivery.status, delivery.delivered_at = "delivered", T0
            session.add(dcr.DecryptageCognitiveLink(
                assistant_delivery_id=delivery.id, capture_version=V1, input_action=action, input_event_id=None,
                context_exit_event_id=closed, capture_state="captured", response_action="continued_event",
                response_event_id=event["id"], support_trace_id=cc.add_support_trace(
                    session, event_id=event["id"], support_kind="assistant_response",
                    support_payload={"visible_content": "v1"}).id, created_at=T0, captured_at=T0))
        session.commit()
    assert {l["input_action"] for l in _links(engine) if l["capture_version"] == V1} == {
        "event_closed_context_change", "event_abandoned_context_change"}
    assert _anchor(Sessions, "conv-v1") is None
    turn = _send(Sessions, ext, _reply("V2", "business"), "Je reprends.", conv="conv-v1")
    assert _link(engine, turn["assistant_turn_id"])["input_action"] == "no_open_event"


def test_pg_i_v1_delivery_is_never_captured_by_the_v2_input_path(engine, Sessions, ext):
    with Sessions() as session:
        delivery = _claim_v2(session, conv=CONV_A)
        delivery.private_metadata = {**delivery.private_metadata, "cognitive_runtime_version": V1}
        with pytest.raises(dcr.CognitiveLinkInvariantError):
            dcr.capture_user_turn(session, delivery=delivery, ticker="MC.PA", user_text="x")


# --------------------------------------------------------------------------
# J. Non-régression R1-C3 (compléments ; suites R1-C2 / R1-C3 rejouées)
# --------------------------------------------------------------------------

def test_pg_j_retrograde_marker_never_regresses_and_stays_a_continuation(engine, Sessions, ext):
    moat = _lvmh_at_moat(Sessions, ext, engine)
    back = _turn(Sessions, ext, _reply("retour", "business"), "Je reviens au business.", conv=CONV_A)
    assert _session_row(engine)[0][1:3] == ("in_progress", "moat")
    link = _link(engine, back["assistant_turn_id"])
    assert (link["input_action"], link["response_action"], link["response_event_id"]) == (
        "contribution_appended", "continued_without_boundary_signal", moat["id"])
    assert _statements(engine)[-1] == ("moat", "Je reviens au business.")


def test_pg_j_one_open_event_per_analysis_session_even_with_many_conversations(engine, Sessions, ext):
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    _turn(Sessions, ext, _reply("C", "moat"), "", context="", conv=CONV_C)
    open_events = [e for e in _events(engine) if e["status"] == "open"]
    assert [e["id"] for e in open_events] == [moat["id"]]


# --------------------------------------------------------------------------
# Concurrence réelle
# --------------------------------------------------------------------------

def test_pg_two_tabs_answering_the_same_event_both_contribute_to_it(engine, Sessions, ext):
    """A et B répondent en même temps à E_MOAT : la transaction de A
    (contribution + avancée produit, non commitée) détient l'AnalysisSession ;
    B attend puis voit E_MOAT encore open (A n'est pas acquitté) : B
    contribue au MÊME event, avec sa propre session_ref et ses propres
    aides."""
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    sid = moat["analysis_session_id"]
    with Sessions() as holder, Sessions() as waiter:
        a = _claim_v2(holder, conv=CONV_A, marker="chiffres")
        dcr.capture_user_turn(holder, delivery=a, ticker="MC.PA", user_text="A : moat.")
        assert dcr.answered_step(holder, link=dcr._lock_link(holder, a.id)) == "moat"
        progress = dp.apply_construction_these_progress(holder, user_id=USER, ticker="MC.PA", step="chiffres",
                                                        thesis_text="A : moat.", data=None, user_contribution=True,
                                                        answered_step="moat")
        assert progress.action == "forward"
        dcr.record_turn_progress(holder, link=dcr._lock_link(holder, a.id), progress_action=progress.action)

        def b_turn(session):
            b = _claim_v2(session, conv=CONV_B, marker="chiffres")
            link = dcr.capture_user_turn(session, delivery=b, ticker="MC.PA", user_text="B : moat.")
            return link.input_action, link.input_event_id

        result, error = _run_blocked(engine, holder, waiter, b_turn)
    assert error is None and result == ("contribution_appended", moat["id"])
    work = _event(engine, moat["id"])["user_work_snapshot"]
    assert [(c["session_ref"], c["text"]) for c in work] == [(CONV_A, "A : moat."), (CONV_B, "B : moat.")]
    [b_trace] = _traces(engine, moat["id"])  # rendue dans B seulement
    assert (work[0]["support_refs_before"], work[1]["support_refs_before"]) == ([], [str(b_trace["id"])])
    assert [s[2] for s in _product(engine)["sessions"] if s[0] == sid] == ["chiffres"]


def test_pg_late_ack_racing_a_transition_ack_becomes_stale(engine, Sessions, ext):
    """L'ACK de B (transition E_MOAT -> E_CHIFFRES) détient les verrous ; l'ACK
    de A (même contexte E_MOAT) attend sur l'AnalysisSession puis voit E_MOAT
    fermé : stale_delivery, jamais rattaché à E_CHIFFRES."""
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    pending_a = _send(Sessions, ext, _reply("A-pending", "moat"), "", context="h", conv=CONV_A)
    pending_b = _send(Sessions, ext, _reply("B-chiffres", "chiffres"), "B : moat.", conv=CONV_B)
    request = api.AssistantDeliveryAckRequest(user_id=USER, conversation_key=CONV_A)
    with Sessions() as holder, Sessions() as waiter:
        delivery = ad.lock_delivery_for_ack(holder, assistant_turn_id=_id(pending_b), user_id=USER,
                                            conversation_key=CONV_B)
        dcr.capture_delivered_response(holder, delivery=delivery)
        ad.acknowledge_delivery(holder, assistant_turn_id=_id(pending_b), user_id=USER, conversation_key=CONV_B)
        result, error = _run_blocked(engine, holder, waiter,
                                     lambda s: api.ack_assistant_delivery(_id(pending_a), request, db=s))
    assert error is None and result["delivery_status"] == "delivered"
    chiffres = _events(engine)[-1]
    assert (_event(engine, moat["id"])["status"], chiffres["status"]) == ("finalized", "open")
    link = _link(engine, pending_a["assistant_turn_id"])
    assert (link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "stale_delivery", None, None)
    assert _traces(engine, chiffres["id"]) == []


def test_pg_context_switch_serializes_on_the_shared_event_without_mutating_it(engine, Sessions, ext):
    """A (ancrée E_NVDA) passe à LVMH : le lien référence E_NVDA (FK), donc la
    courte transaction de A verrouille E_NVDA dans l'ordre global (session
    cible -> events par id), sans le muter. B, qui contribue à E_NVDA, attend
    le commit de A puis contribue normalement : E_NVDA reste open."""
    _turn(Sessions, ext, _reply("N1", "business"), "", context="", conv=CONV_A, ticker="NVDA")
    _turn(Sessions, ext, _reply("N2", "business"), "", context="", conv=CONV_B, ticker="NVDA")
    [nvda] = _events(engine)
    with Sessions() as holder, Sessions() as waiter:
        a = _claim_v2(holder, conv=CONV_A)
        assert dcr.capture_user_turn(holder, delivery=a, ticker="MC.PA",
                                     user_text="Et LVMH ?").input_action == "context_switched"

        def b_turn(session):
            b = _claim_v2(session, conv=CONV_B)
            link = dcr.capture_user_turn(session, delivery=b, ticker="NVDA", user_text="B : des GPU.")
            return link.input_action, link.input_event_id

        result, error = _run_blocked(engine, holder, waiter, b_turn)
    assert error is None and result == ("contribution_appended", nvda["id"])
    after = _event(engine, nvda["id"])
    assert after["status"] == "open"
    assert [c["session_ref"] for c in after["user_work_snapshot"]] == [CONV_B]


def test_pg_crossed_ticker_switches_never_deadlock(engine, Sessions, ext):
    """A ancrée E_LVMH passe à NVDA pendant que B ancrée E_NVDA passe à LVMH :
    chacun verrouille sa session cible puis {E_LVMH, E_NVDA} dans le même
    ordre (id) ; le second attend le premier, aucun interblocage, aucun event
    fermé."""
    _turn(Sessions, ext, _reply("L1", "business"), "", context="", conv=CONV_A)
    _turn(Sessions, ext, _reply("N1", "business"), "", context="", conv=CONV_B, ticker="NVDA")
    lvmh, nvda = _events(engine)
    with Sessions() as holder, Sessions() as waiter:
        a = _claim_v2(holder, conv=CONV_A)
        assert dcr.capture_user_turn(holder, delivery=a, ticker="NVDA",
                                     user_text="Et NVIDIA ?").input_action == "context_switched"

        def b_turn(session):
            b = _claim_v2(session, conv=CONV_B)
            link = dcr.capture_user_turn(session, delivery=b, ticker="MC.PA", user_text="Et LVMH ?")
            return link.input_action, link.input_context_event_id

        result, error = _run_blocked(engine, holder, waiter, b_turn)
    assert error is None and result == ("context_switched", nvda["id"])
    assert (_event(engine, lvmh["id"]), _event(engine, nvda["id"])) == (lvmh, nvda)


def test_pg_concurrent_support_trace_is_not_inherited_by_the_other_conversation(engine, Sessions, ext):
    """L'ACK de A (SupportTrace S_A sur E) détient le verrou de E ; la
    contribution de B attend, puis ne référence PAS S_A (rendu dans A)."""
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    [shown_in_b] = [str(t["id"]) for t in _traces(engine, moat["id"])]
    pending_a = _send(Sessions, ext, _reply("A-aide", "moat"), "", context="h", conv=CONV_A)
    with Sessions() as holder, Sessions() as waiter:
        delivery = ad.lock_delivery_for_ack(holder, assistant_turn_id=_id(pending_a), user_id=USER,
                                            conversation_key=CONV_A)
        dcr.capture_delivered_response(holder, delivery=delivery)
        ad.acknowledge_delivery(holder, assistant_turn_id=_id(pending_a), user_id=USER, conversation_key=CONV_A)

        def b_turn(session):
            b = _claim_v2(session, conv=CONV_B, marker="moat")
            return dcr.capture_user_turn(session, delivery=b, ticker="MC.PA", user_text="B : moat.").input_action

        result, error = _run_blocked(engine, holder, waiter, b_turn)
    assert error is None and result == "contribution_appended"
    s_a = str(_traces(engine, moat["id"])[-1]["id"])
    [b_work] = _event(engine, moat["id"])["user_work_snapshot"]
    assert b_work["support_refs_before"] == [shown_in_b] and s_a not in b_work["support_refs_before"]


def test_pg_stale_final_check_under_lock_after_a_concurrent_transition(engine, Sessions, ext):
    """La transition de E_MOAT est commitée pendant que le tour de A attend
    le verrou de l'AnalysisSession : sous verrou, A voit E_MOAT fermé ->
    StaleConversationContext (et rien n'est écrit)."""
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    pending_b = _send(Sessions, ext, _reply("B-chiffres", "chiffres"), "B : moat.", conv=CONV_B)
    with Sessions() as holder, Sessions() as waiter:
        delivery = ad.lock_delivery_for_ack(holder, assistant_turn_id=_id(pending_b), user_id=USER,
                                            conversation_key=CONV_B)
        dcr.capture_delivered_response(holder, delivery=delivery)
        ad.acknowledge_delivery(holder, assistant_turn_id=_id(pending_b), user_id=USER, conversation_key=CONV_B)

        def a_turn(session):
            a = _claim_v2(session, conv=CONV_A, marker="chiffres")
            dcr.capture_user_turn(session, delivery=a, ticker="MC.PA", user_text="A : moat.")

        result, error = _run_blocked(engine, holder, waiter, a_turn)
    assert isinstance(error, dcr.StaleConversationContext) and error.expected_event_id == moat["id"]
    assert [c["session_ref"] for c in _event(engine, moat["id"])["user_work_snapshot"]] == [CONV_B]
    assert _events(engine)[-1]["user_work_snapshot"] == []


def test_pg_full_parallel_flow_writes_t2_but_never_t3(engine, Sessions, ext):
    _moat_shared_by_a_and_b(Sessions, ext, engine)
    _turn(Sessions, ext, _reply("B-chiffres", "chiffres"), "B : moat.", conv=CONV_B)
    _stale(Sessions, ext, _reply("A"), "A : moat.", conv=CONV_A)
    _turn(Sessions, ext, _reply("N1", "business"), "", context="", conv=CONV_A, ticker="NVDA")
    assert all(e["task_kind"] is None for e in _events(engine))
    assert not [r for r in _rows(engine) if r["private_metadata"].get("decryptage_step_marker") == "swot_final"]
    _no_t3(engine)


# --------------------------------------------------------------------------
# Audit PR #216 — blocker 1 : la cible de la réponse est figée à la capture
# --------------------------------------------------------------------------

def _nvda_moat_anchored_in_b(Sessions, ext, engine):  # noqa: F811
    """B : NVDA business puis contribution (marqueur moat) -> E_MOAT NVDA,
    rendu dans B (B ancrée à E_MOAT), session NVDA à moat."""
    _turn(Sessions, ext, _reply("NB", "business"), "", context="", conv=CONV_B, ticker="NVDA")
    _turn(Sessions, ext, _reply("NM", "moat"), "B : des GPU.", conv=CONV_B, ticker="NVDA")
    moat = _events(engine)[-1]
    assert _anchor(Sessions, CONV_B) == moat["id"] and moat["status"] == "open"
    assert _session_row(engine, "NVDA")[0][2] == "moat"
    return moat


def test_pg_blocker1_t1_context_switch_target_closed_before_ack_is_stale(engine, Sessions, ext):
    """A (ancrée LVMH) passe à NVDA : la cible de sa réponse est figée sur
    E_MOAT (event open NVDA à la capture). Avant l'ACK de A, B fait transiter
    E_MOAT -> E_CHIFFRES. L'ACK de A : stale_delivery, jamais E_CHIFFRES."""
    _turn(Sessions, ext, _reply("L1", "business"), "", context="", conv=CONV_A)
    moat = _nvda_moat_anchored_in_b(Sessions, ext, engine)
    switch = _send(Sessions, ext, _reply("A-moat", "moat"), "Et NVIDIA ?", conv=CONV_A, ticker="NVDA")
    link = _link(engine, switch["assistant_turn_id"])
    assert (link["input_action"], link["response_context_event_id"]) == ("context_switched", moat["id"])
    _turn(Sessions, ext, _reply("B-chiffres", "chiffres"), "B : un moat logiciel.", conv=CONV_B, ticker="NVDA")
    chiffres = _events(engine)[-1]
    assert (_event(engine, moat["id"])["status"], chiffres["status"]) == ("finalized", "open")
    events, traces = _events(engine), _traces(engine)
    assert _ack(Sessions, switch, conv=CONV_A)["delivery_status"] == "delivered"
    link = _link(engine, switch["assistant_turn_id"])
    assert (link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "stale_delivery", None, None)
    assert (_events(engine), _traces(engine)) == (events, traces)
    assert _traces(engine, chiffres["id"]) == []


@pytest.mark.parametrize("question", ["", "Je commence."], ids=["reprise-vide", "texte"])
def test_pg_blocker1_t2_event_appearing_after_capture_is_never_the_target(engine, Sessions, ext, question):
    """A capture son tour sans event cible (session créée par B, event pas
    encore ouvert : response_context_event_id NULL). Avant l'ACK de A, B ouvre
    l'event. L'ACK de A ne continue PAS cet event apparu après la capture :
    stale_delivery, l'event de B intact, toujours un seul event open."""
    pending_b = _send(Sessions, ext, _reply("B1", "business"), "", context="", conv=CONV_B)
    pending_a = _send(Sessions, ext, _reply("A1", "business"), question, context="", conv=CONV_A)
    link = _link(engine, pending_a["assistant_turn_id"])
    assert (link["input_action"], link["response_context_event_id"]) == ("no_open_event", None)
    _ack(Sessions, pending_b, conv=CONV_B)
    [event] = _events(engine)
    assert _ack(Sessions, pending_a, conv=CONV_A)["delivery_status"] == "delivered"
    link = _link(engine, pending_a["assistant_turn_id"])
    assert (link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "stale_delivery", None, None)
    assert _events(engine) == [event] and _traces(engine) == []
    assert _anchor(Sessions, CONV_A) is None


def test_pg_blocker1_t2_without_any_event_the_ack_opens_its_own(engine, Sessions, ext):
    """Aucune cible figée et aucun event apparu : l'ACK ouvre l'event si le
    marqueur a été appliqué par le produit (création de session)."""
    first = _send(Sessions, ext, _reply("A1", "business"), "", context="", conv=CONV_A)
    assert _link(engine, first["assistant_turn_id"])["response_context_event_id"] is None
    _ack(Sessions, first, conv=CONV_A)
    [event] = _events(engine)
    link = _link(engine, first["assistant_turn_id"])
    assert (link["response_action"], link["response_event_id"]) == ("opened_event", event["id"])


def test_pg_blocker1_t3_context_switch_to_an_open_target_continues_it(engine, Sessions, ext):
    """context_switched vers une session dont E_TARGET est open : cible
    figée = E_TARGET ; il reste open jusqu'à l'ACK, qui le continue."""
    _turn(Sessions, ext, _reply("L1", "business"), "", context="", conv=CONV_A)
    lvmh = _events(engine)[0]
    target = _nvda_moat_anchored_in_b(Sessions, ext, engine)
    switch = _turn(Sessions, ext, _reply("A-moat", "moat"), "Et NVIDIA ?", conv=CONV_A, ticker="NVDA")
    link = _link(engine, switch["assistant_turn_id"])
    assert (link["input_action"], link["input_context_event_id"], link["response_context_event_id"]) == (
        "context_switched", lvmh["id"], target["id"])
    [trace] = [t for t in _traces(engine, target["id"]) if t["support_payload"]["visible_content"] == "Réponse A-moat."]
    assert (link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "continued_event", target["id"], trace["id"])
    assert _anchor(Sessions, CONV_A) == target["id"]


def test_pg_blocker1_anchored_turns_target_their_anchor(engine, Sessions, ext):
    moat = _lvmh_at_moat(Sessions, ext, engine)
    answer = _send(Sessions, ext, _reply("x", "moat"), "Une marque.", conv=CONV_A)
    empty = None
    _ack(Sessions, answer, conv=CONV_A)
    empty = _send(Sessions, ext, _reply("y", "moat"), "", context="h", conv=CONV_A)
    for turn, action in ((answer, "contribution_appended"), (empty, "no_user_contribution")):
        link = _link(engine, turn["assistant_turn_id"])
        assert (link["input_action"], link["input_context_event_id"], link["response_context_event_id"]) == (
            action, moat["id"], moat["id"])


# --------------------------------------------------------------------------
# Audit PR #216 — blocker 2 : la progression connaît l'étape répondue
# --------------------------------------------------------------------------

@pytest.mark.parametrize("marker", ["chiffres", "valorisation", "moat"])
def test_pg_blocker2_t4_late_answer_to_moat_never_advances_nor_mislabels(engine, Sessions, ext, caplog, marker):
    """A et B ancrées à E_MOAT. A répond (moat) : la session passe à chiffres,
    A n'est pas acquittée (E_MOAT encore open). B répond ensuite à l'ancienne
    question moat : contribution dans E_MOAT, UserStatement step=moat
    (answered_step), et AUCUNE progression quel que soit le marqueur."""
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    _send(Sessions, ext, _reply("A-chiffres", "chiffres"), "A : un moat de marque.", conv=CONV_A)
    assert _session_row(engine)[0][1:3] == ("in_progress", "chiffres")
    assert _event(engine, moat["id"])["status"] == "open"
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        late = _send(Sessions, ext, _reply("B-late", marker), "B : un moat de réseau.", conv=CONV_B)
    link = _link(engine, late["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"]) == ("contribution_appended", moat["id"])
    with Sessions() as session:
        assert dcr.answered_step(session, link=session.get(dcr.DecryptageCognitiveLink, _id(late))) == "moat"
    assert [c["session_ref"] for c in _event(engine, moat["id"])["user_work_snapshot"]] == [CONV_A, CONV_B]
    assert _session_row(engine)[0][1:4] == ("in_progress", "chiffres", None)
    assert _statements(engine)[-2:] == [("moat", "A : un moat de marque."), ("moat", "B : un moat de réseau.")]
    assert not [s for s in _statements(engine) if s[0] == "chiffres"]
    assert f"anomaly=stale_contribution_step answered=moat current=chiffres received={marker}" in caplog.text


def test_pg_blocker2_t5_answered_step_equal_to_current_step_progresses_normally(engine, Sessions, ext):
    moat = _lvmh_at_moat(Sessions, ext, engine)
    answer = _send(Sessions, ext, _reply("chiffres", "chiffres"), "Un moat de marque.", conv=CONV_A)
    with Sessions() as session:
        link = session.get(dcr.DecryptageCognitiveLink, _id(answer))
        assert dcr.answered_step(session, link=link) == "moat"
        assert link.input_event_id == moat["id"]
    assert _session_row(engine)[0][1:3] == ("in_progress", "chiffres")
    assert _statements(engine)[-1] == ("moat", "Un moat de marque.")


def test_pg_blocker2_answered_step_is_none_without_contribution(engine, Sessions, ext):
    _lvmh_at_moat(Sessions, ext, engine)
    empty = _send(Sessions, ext, _reply("x", "moat"), "", context="h", conv=CONV_A)
    with Sessions() as session:
        assert dcr.answered_step(session, link=session.get(dcr.DecryptageCognitiveLink, _id(empty))) is None


def _seed_nvda(Sessions, step, status="in_progress"):  # noqa: F811
    from core.models import AnalysisSession
    with Sessions() as s:
        session = AnalysisSession(user_id=USER, ticker="NVDA", status=status, current_step=step)
        s.add(session)
        s.commit()
        return session.id


def _apply_nvda(Sessions, step, *, answered, thesis="Ma thèse."):  # noqa: F811
    with Sessions() as s:
        result = dp.apply_construction_these_progress(s, user_id=USER, ticker="NVDA", step=step, thesis_text=thesis,
                                                      data=None, user_contribution=True, answered_step=answered)
        result_id = result.analysis_session.id if result is not None else None
        s.commit()
        return result_id


def _nvda_state(engine):  # noqa: F811
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT status, current_step, completed_at IS NOT NULL FROM analysis_sessions WHERE ticker = 'NVDA' "
            "ORDER BY started_at"))]


def test_pg_blocker2_t6_completed_session_never_gets_a_thesis(engine, Sessions, ext):
    """answered_step=risques mais la session est déjà completed : aucune
    session active, contribution incohérente -> fail closed, aucune thèse."""
    _seed_nvda(Sessions, "swot_final", status="completed")
    with pytest.raises(ValueError):
        _apply_nvda(Sessions, "swot_final", answered="risques")
    assert _nvda_state(engine) == [("completed", "swot_final", False)]
    assert _product(engine)["theses"] == [] and _product(engine)["statements"] == []


@pytest.mark.parametrize("current, answered, anomaly", [
    ("swot_final", "risques", "stale_contribution_step"),   # état anormal : jamais de thèse
    ("valorisation", "risques", "stale_contribution_step"),
    ("moat", "moat", "terminal_before_risques"),             # saut vers swot_final
])
def test_pg_blocker2_swot_final_requires_answered_eq_current_eq_risques(engine, Sessions, ext, caplog, current,
                                                                       answered, anomaly):
    _seed_nvda(Sessions, current)
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        _apply_nvda(Sessions, "swot_final", answered=answered)
    assert _nvda_state(engine) == [("in_progress", current, False)]
    assert _product(engine)["theses"] == []
    assert [s[0] for s in _product(engine)["statements"]] == [answered]
    assert f"anomaly={anomaly}" in caplog.text


def test_pg_blocker2_swot_final_on_risques_completes_with_a_thesis(engine, Sessions, ext):
    sid = _seed_nvda(Sessions, "risques")
    assert _apply_nvda(Sessions, "swot_final", answered="risques", thesis="Thèse finale.") == sid
    assert _nvda_state(engine) == [("completed", "swot_final", True)]
    assert [t[0] for t in _product(engine)["theses"]] == ["Thèse finale."]
    assert [(s[0], s[1]) for s in _product(engine)["statements"]] == [("risques", "Thèse finale.")]


# --------------------------------------------------------------------------
# Audit PR #216 — blocker 3 : même ticker + ancienne tentative = stale
# --------------------------------------------------------------------------

def _nvda_to_risques(Sessions, ext, conv):  # noqa: F811
    _turn(Sessions, ext, _reply("business", "business"), "", context="", conv=conv, ticker="NVDA")
    for previous, step in zip(STEPS[:4], STEPS[1:5]):
        _turn(Sessions, ext, _reply(step, step), f"réponse {previous}", conv=conv, ticker="NVDA")


def _final_check_is_stale(Sessions, conv, ticker, text="x"):  # noqa: F811
    """Vérification qui fait foi, sous verrou (sans la pré-vérification)."""
    with Sessions() as session:
        delivery = _claim_v2(session, conv=conv)
        with pytest.raises(dcr.StaleConversationContext):
            dcr.capture_user_turn(session, delivery=delivery, ticker=ticker, user_text=text)
        session.rollback()


def test_pg_blocker3_t7_answer_after_another_tab_completed_the_attempt_is_stale(engine, Sessions, ext):
    """A ancrée à E_RISQUES NVDA ; B (ancrée au même event) termine NVDA
    (swot_final : session completed, E_RISQUES fermé). A répond sur NVDA :
    409 stale_conversation_context, PAS context_switched."""
    _nvda_to_risques(Sessions, ext, CONV_A)
    risques = _events(engine)[-1]
    _turn(Sessions, ext, _reply("B-risques", "risques"), "", context="", conv=CONV_B, ticker="NVDA")
    assert _anchor(Sessions, CONV_A) == _anchor(Sessions, CONV_B) == risques["id"]
    _turn(Sessions, ext, _reply("bilan", "swot_final"), "B : ma thèse.", conv=CONV_B, ticker="NVDA")
    assert _nvda_state(engine)[-1][:2] == ("completed", "swot_final")
    assert _event(engine, risques["id"])["status"] == "finalized"
    state, product = _t2_state(engine), _product(engine)
    _stale(Sessions, ext, _reply("A"), "A : mes risques.", conv=CONV_A, ticker="NVDA")
    _final_check_is_stale(Sessions, CONV_A, "NVDA")
    assert (_t2_state(engine), _product(engine)) == (state, product)


def test_pg_blocker3_t8_new_attempt_of_the_same_ticker_elsewhere_makes_a_stale(engine, Sessions, ext):
    """A ancrée à l'ancienne tentative NVDA (event encore open) ; une nouvelle
    AnalysisSession NVDA est active (créée ailleurs). A répond NVDA : 409."""
    _turn(Sessions, ext, _reply("N1", "business"), "", context="", conv=CONV_A, ticker="NVDA")
    [old] = _events(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE analysis_sessions SET status = 'abandoned' WHERE id = :s"),
                     {"s": old["analysis_session_id"]})
    _turn(Sessions, ext, _reply("C1", "business"), "", context="", conv=CONV_C, ticker="NVDA")
    assert len([s for s in _nvda_state(engine) if s[0] == "in_progress"]) == 1
    state, product = _t2_state(engine), _product(engine)
    _stale(Sessions, ext, _reply("A"), "A : des GPU.", conv=CONV_A, ticker="NVDA")
    _final_check_is_stale(Sessions, CONV_A, "NVDA")
    assert (_t2_state(engine), _product(engine)) == (state, product)
    assert _event(engine, old["id"]) == old


def test_pg_blocker3_same_ticker_without_any_active_attempt_is_stale(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("N1", "business"), "", context="", conv=CONV_A, ticker="NVDA")
    [old] = _events(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE analysis_sessions SET status = 'abandoned' WHERE id = :s"),
                     {"s": old["analysis_session_id"]})
    _stale(Sessions, ext, _reply("A"), "A : des GPU.", conv=CONV_A, ticker="NVDA")
    _final_check_is_stale(Sessions, CONV_A, "NVDA")


def test_pg_blocker3_t9_other_ticker_is_a_context_switch(engine, Sessions, ext):
    """A ancrée NVDA demande LVMH : context_switched, event NVDA inchangé —
    même si la tentative NVDA de l'ancre est terminée ou abandonnée."""
    _turn(Sessions, ext, _reply("N1", "business"), "", context="", conv=CONV_A, ticker="NVDA")
    [nvda] = _events(engine)
    with engine.begin() as conn:  # même une tentative NVDA abandonnée n'est pas un motif de stale pour LVMH
        conn.execute(sa.text("UPDATE analysis_sessions SET status = 'abandoned' WHERE id = :s"),
                     {"s": nvda["analysis_session_id"]})
    switch = _send(Sessions, ext, _reply("L1", "business"), "Et LVMH ?", conv=CONV_A, ticker="MC.PA")
    link = _link(engine, switch["assistant_turn_id"])
    assert (link["input_action"], link["input_context_event_id"], link["input_event_id"]) == (
        "context_switched", nvda["id"], None)
    assert _event(engine, nvda["id"]) == nvda


# --------------------------------------------------------------------------
# Audit PR #216 — causalité des frontières : seule la progression DE CE TOUR
# (product_progress_action) autorise transition / fermeture terminale
# --------------------------------------------------------------------------

def _full_turn_tx(session, *, conv, marker, text, ticker="MC.PA"):
    """Un tour /decryptage complet dans la transaction `session` (comme la
    route, sans les appels externes) : claim, capture cognitive, progression
    produit, liaison de session, traçage de la décision de CE tour."""
    delivery = _claim_v2(session, conv=conv, marker=marker,
                         payload={**PAYLOAD_A, "analysis": f"Réponse {conv} {marker}."})
    link = dcr.capture_user_turn(session, delivery=delivery, ticker=ticker, user_text=text)
    contribution = link.input_action == "contribution_appended"
    progress = dp.apply_construction_these_progress(
        session, user_id=USER, ticker=ticker, step=marker, thesis_text=text if contribution else None, data=None,
        user_contribution=contribution, answered_step=dcr.answered_step(session, link=link))
    ad.bind_analysis_session(session, delivery_id=delivery.id, analysis_session_id=progress.analysis_session.id)
    dcr.record_turn_progress(session, link=link, progress_action=progress.action)
    return {"assistant_turn_id": str(delivery.id), "action": progress.action}


def test_pg_boundary_race_stale_contribution_never_transitions_even_if_session_matches(engine, Sessions, ext,
                                                                                      caplog):
    """Course réelle (deux connexions) : A et B ancrées à E_MOAT. La
    transaction de A (contribution moat, marqueur chiffres : moat -> chiffres)
    détient l'AnalysisSession ; celle de B (contribution moat, marqueur
    chiffres) attend, puis voit current_step = chiffres : contribution
    stale_contribution_step, AUCUNE progression. À l'ACK de B, la session est
    bien à chiffres et la réponse de B porte chiffres : B ne ferme PAS E_MOAT
    et ne crée aucune transition (continuation sans frontière). Miroir : l'ACK
    de A, qui a réellement appliqué moat -> chiffres, produit la transition.
    Retries d'ACK : no-op strict."""
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    with Sessions() as holder, Sessions() as waiter:
        turn_a = _full_turn_tx(holder, conv=CONV_A, marker="chiffres", text="A : un moat de marque.")
        assert turn_a["action"] == "forward"
        result, error = _run_blocked(engine, holder, waiter, lambda s: _full_turn_tx(
            s, conv=CONV_B, marker="chiffres", text="B : un moat de réseau."))
    assert error is None and result["action"] == "stale_contribution_step"
    turn_b = result
    assert _session_row(engine)[0][1:3] == ("in_progress", "chiffres")
    assert _link(engine, turn_a["assistant_turn_id"])["product_progress_action"] == "forward"
    assert _link(engine, turn_b["assistant_turn_id"])["product_progress_action"] == "stale_contribution_step"
    assert [c["session_ref"] for c in _event(engine, moat["id"])["user_work_snapshot"]] == [CONV_A, CONV_B]

    # ACK de B d'abord : état global compatible (chiffres == chiffres), mais
    # CE tour n'a rien appliqué.
    events_before = _events(engine)
    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        assert _ack(Sessions, turn_b, conv=CONV_B)["delivery_status"] == "delivered"
    link_b = _link(engine, turn_b["assistant_turn_id"])
    [trace_b] = [t for t in _traces(engine, moat["id"]) if t["id"] == link_b["support_trace_id"]]
    assert (link_b["response_action"], link_b["response_event_id"]) == (
        "continued_without_boundary_signal", moat["id"])
    assert trace_b["support_payload"] == {"visible_content": "Réponse conv-b chiffres."}
    assert _event(engine, moat["id"])["status"] == "open"
    assert [e["id"] for e in _events(engine)] == [e["id"] for e in events_before]  # aucun nouvel event
    assert ("anomaly=boundary_not_applied marker=chiffres current_step=chiffres "
            "progress_action=stale_contribution_step") in caplog.text

    # Miroir positif : l'ACK de A (progression réellement appliquée par CE tour).
    assert _ack(Sessions, turn_a, conv=CONV_A)["delivery_status"] == "delivered"
    link_a = _link(engine, turn_a["assistant_turn_id"])
    old, new = _events(engine)[-2:]
    assert (old["id"], old["status"], new["status"]) == (moat["id"], "finalized", "open")
    assert (link_a["response_action"], link_a["response_event_id"]) == ("transitioned_event", new["id"])
    assert new["stimulus_snapshot"]["visible_content"] == "Réponse conv-a chiffres."

    # Idempotence : retries d'ACK sans aucune écriture.
    state = _t2_state(engine)
    with _Statements(engine) as statements:
        for turn, conv in ((turn_b, CONV_B), (turn_a, CONV_A)):
            assert _ack(Sessions, turn, conv=conv)["delivery_status"] == "delivered"
    assert _writes(statements) == [] and _t2_state(engine) == state


@pytest.mark.parametrize("order", ["stale-first", "advancing-first"])
def test_pg_boundary_api_flow_only_the_advancing_turn_transitions(engine, Sessions, ext, order):
    """Même course via la route /decryptage : quel que soit l'ordre des ACK,
    seul le tour qui a fait avancer la session crée la frontière ; la réponse
    du tour périmé ne transite jamais (continuation si E_MOAT est encore
    open, stale_delivery s'il a déjà été fermé par l'ACK légitime)."""
    moat = _moat_shared_by_a_and_b(Sessions, ext, engine)
    turn_a = _send(Sessions, ext, _reply("A-chiffres", "chiffres"), "A : un moat de marque.", conv=CONV_A)
    turn_b = _send(Sessions, ext, _reply("B-chiffres", "chiffres"), "B : un moat de réseau.", conv=CONV_B)
    assert (_link(engine, turn_a["assistant_turn_id"])["product_progress_action"],
            _link(engine, turn_b["assistant_turn_id"])["product_progress_action"]) == (
        "forward", "stale_contribution_step")
    acks = [(turn_b, CONV_B), (turn_a, CONV_A)] if order == "stale-first" else [(turn_a, CONV_A), (turn_b, CONV_B)]
    for turn, conv in acks:
        _ack(Sessions, turn, conv=conv)
    link_a = _link(engine, turn_a["assistant_turn_id"])
    link_b = _link(engine, turn_b["assistant_turn_id"])
    assert link_a["response_action"] == "transitioned_event"
    expected_b = "continued_without_boundary_signal" if order == "stale-first" else "stale_delivery"
    assert link_b["response_action"] == expected_b
    assert [e["status"] for e in _events(engine)][-2:] == ["finalized", "open"]
    assert len([e for e in _events(engine) if e["status"] == "open"]) == 1
    assert _event(engine, moat["id"])["status"] == "finalized"


def test_pg_boundary_terminal_requires_this_turn_to_complete_the_session(engine, Sessions, ext, caplog):
    """A et B ancrées à E_RISQUES. B (reprise sans texte) reçoit une réponse
    swot_final : progression refusée (terminal_without_contribution). A
    contribue et termine réellement la session (swot_final). À l'ACK de B, la
    session est completed / current_step = swot_final : B ne peut JAMAIS
    provoquer closed_terminal (continuation sans frontière). L'ACK de A, qui
    a réellement terminé la session, ferme E_RISQUES (aucun event
    swot_final)."""
    _nvda_to_risques(Sessions, ext, CONV_A)
    risques = _events(engine)[-1]
    _turn(Sessions, ext, _reply("B-risques", "risques"), "", context="", conv=CONV_B, ticker="NVDA")
    turn_b = _send(Sessions, ext, _reply("B-bilan", "swot_final"), "", context="h", conv=CONV_B, ticker="NVDA")
    turn_a = _send(Sessions, ext, _reply("A-bilan", "swot_final"), "A : ma thèse.", conv=CONV_A, ticker="NVDA")
    assert (_link(engine, turn_b["assistant_turn_id"])["product_progress_action"],
            _link(engine, turn_a["assistant_turn_id"])["product_progress_action"]) == (
        "terminal_without_contribution", "forward")
    assert _nvda_state(engine)[-1][:2] == ("completed", "swot_final")

    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        _ack(Sessions, turn_b, conv=CONV_B)
    link_b = _link(engine, turn_b["assistant_turn_id"])
    assert (link_b["response_action"], link_b["response_event_id"]) == (
        "continued_without_boundary_signal", risques["id"])
    assert _event(engine, risques["id"])["status"] == "open"
    assert "progress_action=terminal_without_contribution" in caplog.text

    _ack(Sessions, turn_a, conv=CONV_A)
    link_a = _link(engine, turn_a["assistant_turn_id"])
    assert (link_a["response_action"], link_a["response_event_id"]) == ("closed_terminal", None)
    assert _event(engine, risques["id"])["status"] == "finalized"
    assert not [e for e in _events(engine) if e["status"] == "open"]
    assert [t[0] for t in _product(engine)["theses"]] == ["A : ma thèse."]


def test_pg_boundary_opening_never_from_a_refused_progression(engine, Sessions, ext, caplog):
    """Aucun event ouvert, aucune cible figée : un tour dont la progression a
    été REFUSÉE (forward_without_contribution) n'ouvre jamais d'event, même
    si l'état global de la session a été amené sur son marqueur entre-temps
    (simulé ici) ; seule une création / confirmation de CE tour (new / same)
    le peut."""
    first = _send(Sessions, ext, _reply("A1", "business"), "", context="", conv=CONV_A)  # new (pending)
    refused = _send(Sessions, ext, _reply("B1", "moat"), "", context="", conv=CONV_B)
    assert _link(engine, refused["assistant_turn_id"])["product_progress_action"] == "forward_without_contribution"
    with engine.begin() as conn:  # un autre tour aurait amené la session sur « moat »
        conn.execute(sa.text("UPDATE analysis_sessions SET current_step = 'moat' WHERE ticker = 'MC.PA' "
                             "AND status = 'in_progress'"))
    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        _ack(Sessions, refused, conv=CONV_B)
    assert _link(engine, refused["assistant_turn_id"])["response_action"] == "no_cognitive_action"
    assert _events(engine) == []
    assert "progress_action=forward_without_contribution" in caplog.text
    assert _link(engine, first["assistant_turn_id"])["product_progress_action"] == "new"


def test_pg_record_turn_progress_invariants(engine, Sessions, ext):
    with Sessions() as session:
        delivery = _claim_v2(session, conv=CONV_A, marker="business")
        link = dcr.capture_user_turn(session, delivery=delivery, ticker="MC.PA", user_text="")
        with pytest.raises(dcr.CognitiveLinkInvariantError):
            dcr.record_turn_progress(session, link=link, progress_action="applied")  # hors vocabulaire
        dcr.record_turn_progress(session, link=link, progress_action="new")
        with pytest.raises(dcr.CognitiveLinkInvariantError):  # une seule fois
            dcr.record_turn_progress(session, link=link, progress_action="forward")
        session.rollback()


def test_pg_positive_mirror_simple_flow_records_forward_and_transitions(engine, Sessions, ext):
    moat = _lvmh_at_moat(Sessions, ext, engine)
    answer = _turn(Sessions, ext, _reply("chiffres", "chiffres"), "Un moat de marque.", conv=CONV_A)
    link = _link(engine, answer["assistant_turn_id"])
    assert (link["product_progress_action"], link["response_action"]) == ("forward", "transitioned_event")
    assert _event(engine, moat["id"])["status"] == "finalized"
