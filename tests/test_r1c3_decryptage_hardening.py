"""Tests de R1-C3 : hardening du runtime Décrypter AVANT T3.

R1-C3B — l'AnalysisSession in_progress du ticker courant est la source
autoritaire de progression (chargée que le context soit vide ou non) ;
current_step = étape ACTUELLEMENT OUVERTE ; la règle « commencer par le
Business » ne s'applique jamais avec une analyse en cours.

R1-C3C — progression AnalysisSession monotone (même étape, avancée, saut,
rétrograde sans dégât, swot_final) ; UserStatement / InvestmentThesis
uniquement pour une vraie contribution R1-C2 (contribution_appended).

R1-C3A (conversation_key par onglet) et le contexte filtré par ticker sont
testés côté frontend (tests/web/decryptage_tab_scope.test.js).

R1-C4 : une avancée / terminalisation d'une session existante exige en plus
une vraie contribution (user_contribution) ; la navigation est
context_switched et une reprise sans ancre no_open_event. Les tests
ci-dessous l'expriment explicitement.

1. Tests sans base (toujours exécutés) : prompt de reprise, classement
   des marqueurs, structure de la route, aucune migration, aucun T3+.

2. Tests contre un vrai PostgreSQL : uniquement si ORYX_TEST_DATABASE_URL
   pointe vers une base DÉDIÉE dont le nom contient "test" (sinon SKIPPÉS) :

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_r1c3_test \\
           python -m pytest tests/test_r1c3_decryptage_hardening.py

   Les parcours appellent api.decryptage / api.ack_assistant_delivery
   (fixtures et faux appels externes de tests/test_assistant_delivery.py,
   helpers de tests/test_decryptage_cognitive_runtime.py).
"""
import ast
import json
import logging
import uuid

import pytest
import sqlalchemy as sa

import api
from core import decryptage_progress as dp
from core.decryptage_engine import build_system_prompt
from core.models import AnalysisSession
from core.pedagogie_library import METHODES
from tests.test_assistant_delivery import (  # noqa: F401 — fixtures
    Sessions,
    _count,
    _product,
    _rows,
    engine,
    ext,
)
from tests.test_cognitive_capture import _run_blocked
from tests.test_decryptage_cognitive_runtime import (
    CONV,
    CONV_A,
    CONV_B,
    _ack,
    _event,
    _events,
    _link,
    _no_t3,
    _reply,
    _send,
    _session_id,
    _traces,
    _turn,
)
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture
from tests.test_migration_0005_cognitive_support_traces import USER

API_PATH = REPO_ROOT / "api.py"
PROGRESS_LOGGER = "core.decryptage_progress"
DATA = {"name": "NVIDIA", "currency": "USD"}


def _method():
    return {"method_id": "construction_these", **METHODES["construction_these"]}


def _progress(step, statements=()):
    return {"current_step": step, "statements": [{"step": s, "text": t} for s, t in statements], "facts": {}}


def _resume_block(prompt):
    """Bloc de reprise (analyse en cours), avant les règles générales."""
    start = prompt.index("RAPPEL IMPORTANT — analyse déjà EN COURS")
    return prompt[start:prompt.index("RÈGLES ABSOLUES")]


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_e_resume_prompt_reopens_moat_never_chiffres():
    """(E) current_step = moat : l'étape OUVERTE est le Moat ; la reprise
    demande le Moat, jamais directement les Chiffres."""
    block = _resume_block(build_system_prompt(DATA, _method(), "debutant", None,
                                              _progress("moat", [("business", "GPU")])))
    assert "Étape ACTUELLEMENT OUVERTE : Composante 2 (Moat)" in block
    assert "reprends CETTE étape : Composante 2 (Moat)" in block
    assert "ce n'est PAS une étape terminée" in block
    assert "seulement ensuite, passe à\n  l'étape suivante" in block
    assert "Composante 3" not in block and "Chiffres clés" not in block
    for removed in ("Dernière étape atteinte", "Reprendre DIRECTEMENT"):
        assert removed not in block


def test_f_resume_prompt_reopens_chiffres():
    """(F) current_step = chiffres : reprise sur les Chiffres, pas la
    Valorisation."""
    block = _resume_block(build_system_prompt(DATA, _method(), "debutant", None, _progress("chiffres")))
    assert "Étape ACTUELLEMENT OUVERTE : Composante 3 (Chiffres clés)" in block
    assert "reprends CETTE étape : Composante 3 (Chiffres clés)" in block
    assert "Composante 4" not in block and "Valorisation" not in block


@pytest.mark.parametrize("step", [s for s in dp.DECRYPTAGE_STEP_MARKERS if s != dp.FINAL_STEP])
def test_resume_prompt_never_computes_a_next_step(step):
    """Aucune « étape suivante » n'est calculée comme cible de reprise :
    seule l'étape ouverte est nommée dans le bloc de reprise."""
    labels = {"business": "Composante 1", "moat": "Composante 2", "chiffres": "Composante 3",
              "valorisation": "Composante 4", "risques": "Composante 5"}
    block = _resume_block(build_system_prompt(DATA, _method(), "debutant", None, _progress(step)))
    named = {s for s, label in labels.items() if label in block}
    # Business n'est nommé que pour interdire d'y revenir.
    assert named - {"business"} <= {step}
    assert labels[step] in block


def test_g_start_with_business_rule_never_applies_with_an_active_session():
    """(G) La règle générique « commencer par le Business » ne s'applique
    jamais si in_progress_analysis existe ; elle subsiste sans analyse en
    cours. La méthode injectée se soumet aussi à l'analyse en cours."""
    active = build_system_prompt(DATA, _method(), "debutant", None, _progress("moat"))
    assert "Toujours commencer par l'Étape 1" not in active
    assert "Aucune analyse en cours n'est fournie" not in active
    assert "son étape actuellement ouverte est AUTORITAIRE" in active
    assert "Ne reviens jamais à l'Étape 1 (Business)" in active
    assert "Tu veux qu'on formule une thèse" not in active
    assert "si une analyse EN COURS est fournie plus haut" in active  # méthode construction_these

    fresh = build_system_prompt(DATA, _method(), "debutant", None, None)
    assert "Aucune analyse en cours n'est fournie : commence par l'Étape 1 (Business)" in fresh
    assert "AUTORITAIRE" not in fresh and "analyse déjà EN COURS" not in fresh
    assert "Tu veux qu'on formule une thèse" in fresh


@pytest.mark.parametrize("current, incoming, action", [
    ("business", "business", dp.PROGRESS_SAME),
    ("moat", "moat", dp.PROGRESS_SAME),
    ("business", "moat", dp.PROGRESS_FORWARD),
    ("moat", "chiffres", dp.PROGRESS_FORWARD),
    ("risques", "swot_final", dp.PROGRESS_FORWARD),
    ("business", "chiffres", dp.PROGRESS_STEP_SKIP),
    ("moat", "swot_final", dp.PROGRESS_STEP_SKIP),
    ("chiffres", "business", dp.PROGRESS_RETROGRADE),
    ("risques", "moat", dp.PROGRESS_RETROGRADE),
    (None, "business", dp.PROGRESS_FORWARD),
    ("inconnu", "moat", dp.PROGRESS_FORWARD),
])
def test_progress_action_follows_the_closed_order(current, incoming, action):
    assert dp._progress_action(current, incoming) == action


def test_closed_step_order():
    assert dp.STEP_ORDER == {"business": 0, "moat": 1, "chiffres": 2, "valorisation": 3, "risques": 4,
                             "swot_final": 5}


def _route_source():
    tree = ast.parse(API_PATH.read_text(encoding="utf-8"))
    (route,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "decryptage"]
    return route, ast.unparse(route)


def test_route_always_loads_the_active_session_before_choosing_the_method():
    """R1-C3B : _get_analysis_progress est appelé inconditionnellement (hors
    de tout if), avant le choix de méthode ; il force construction_these."""
    route, source = _route_source()
    top_level_calls = [ast.unparse(n) for n in route.body if isinstance(n, ast.Assign)]
    assert "in_progress_analysis = _get_analysis_progress(user_id, ticker)" in top_level_calls
    assert source.index("_get_analysis_progress(") < source.index("lookup_method(")
    [choice] = [n for n in route.body if isinstance(n, ast.If) and ast.unparse(n.test) == "in_progress_analysis"]
    assert ast.unparse(choice.body) == "method = _force_construction_these_method()"


def test_route_passes_text_to_the_product_only_for_a_real_contribution():
    """R1-C3C : thesis_text = question SEULEMENT si R1-C2 a classé le tour
    contribution_appended ; capture_user_turn reste avant la progression.
    R1-C4 : la même classification autorise seule l'avancée produit."""
    _, source = _route_source()
    assert "cognitive_link = capture_user_turn(" in source
    assert ("user_contribution = cognitive_link is not None and cognitive_link.input_action == "
            "CONTRIBUTION_APPENDED") in source
    assert "progress_thesis_text = question if user_contribution else None" in source
    assert "thesis_text=progress_thesis_text" in source
    assert "user_contribution=user_contribution" in source
    assert "thesis_text=question" not in source
    assert source.index("capture_user_turn(") < source.index("apply_construction_these_progress(")


def test_no_migration_and_no_t3_in_the_hardened_modules():
    """R1-C3 n'a ajouté aucune migration (0013 est celle de R1-C4)."""
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files[-2:] == ["0012_decryptage_cognitive_links.py", "0013_decryptage_conversation_affinity.py"]
    assert len(files) == 13
    for path in ("core/decryptage_progress.py", "core/decryptage_engine.py"):
        tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported |= {node.module or ""} | {a.name for a in node.names}
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
        for forbidden in ("core.observation_service", "core.inference_service", "core.longitudinal_service",
                          "core.taxonomy_service", "core.cognitive_capture", "PedagogicalObservation",
                          "CognitiveEvent", "SupportTrace"):
            assert forbidden not in imported, (path, forbidden)


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL — R1-C3B : AnalysisSession autoritaire
# --------------------------------------------------------------------------

class _PromptSpy:
    """Enregistre les arguments de build_system_prompt (puis délègue) et
    remplace lookup_method par une méthode différente de construction_these
    (pour prouver qu'elle ne peut pas l'emporter sur une session active)."""

    OTHER = {"method_id": "autre_methode", "title": "Autre méthode", "method_content": "Autre.",
             "example_company": None, "keywords_matched": []}

    def __init__(self, monkeypatch):
        self.prompts = []
        self.lookups = []
        real = api.build_system_prompt

        def spy(data, method, level, existing_thesis, in_progress_analysis):
            self.prompts.append({"method": method["method_id"] if method else None,
                                 "existing_thesis": existing_thesis, "in_progress": in_progress_analysis})
            return real(data, method, level, existing_thesis, in_progress_analysis)

        def lookup(*args, **kwargs):
            self.lookups.append((args, kwargs))
            return dict(self.OTHER)

        monkeypatch.setattr(api, "build_system_prompt", spy)
        monkeypatch.setattr(api, "lookup_method", lookup)


def _nvda_at_moat(Sessions, ext, conv=CONV_B):
    """AnalysisSession NVDA current_step = moat, E_NVDA moat open."""
    _turn(Sessions, ext, _reply("N1", "business"), "", context="", conv=conv, ticker="NVDA")
    _turn(Sessions, ext, _reply("N2", "moat"), "NVIDIA vend des GPU.", conv=conv, ticker="NVDA")
    session = _sessions(Sessions, "NVDA")[0]
    assert (session.status, session.current_step) == ("in_progress", "moat")
    return session


def _sessions(Sessions, ticker):
    with Sessions() as s:
        return s.query(AnalysisSession).filter_by(user_id=USER, ticker=ticker).order_by(
            AnalysisSession.started_at).all()


@pytest.mark.parametrize("context, last_method_id", [("historique NVDA", None), ("h", "construction_these"),
                                                     ("[USER] Et la dette ?", "autre_methode")])
def test_pg_a_non_empty_context_still_loads_the_active_session(engine, Sessions, ext, monkeypatch, context,
                                                               last_method_id):
    """(A) context non vide + AnalysisSession NVDA moat : session chargée,
    construction_these forcée, in_progress transmis au prompt ; lookup_method
    (et last_method_id) ne peuvent pas basculer vers une autre méthode."""
    _nvda_at_moat(Sessions, ext)
    spy = _PromptSpy(monkeypatch)
    ext.texts = [_reply("N3", "moat")]
    response = api_call(Sessions, ext, question="Et la dette ?", context=context, last_method_id=last_method_id)
    assert response["method_used"] == "construction_these"
    [prompt] = spy.prompts
    assert prompt["method"] == "construction_these"
    assert prompt["in_progress"]["current_step"] == "moat"
    assert prompt["in_progress"]["statements"] == [{"step": "business", "text": "NVIDIA vend des GPU."}]
    assert prompt["existing_thesis"] is None
    assert spy.lookups == []


def test_pg_b_empty_context_with_active_session_is_equally_authoritative(engine, Sessions, ext, monkeypatch):
    """(B) context vide + AnalysisSession active : même logique (reprise)."""
    _nvda_at_moat(Sessions, ext)
    spy = _PromptSpy(monkeypatch)
    ext.texts = [_reply("N3", "moat")]
    response = api_call(Sessions, ext, question="", context="", conversation_key=CONV_A)
    assert response["method_used"] == "construction_these"
    [prompt] = spy.prompts
    assert (prompt["method"], prompt["in_progress"]["current_step"], prompt["existing_thesis"]) == (
        "construction_these", "moat", None)
    assert spy.lookups == []


def test_pg_c_no_active_session_and_context_keeps_lookup_method(engine, Sessions, ext, monkeypatch):
    """(C) Pas d'AnalysisSession active + context non vide : lookup_method
    historique (avec last_method_id) décide, aucun in_progress."""
    spy = _PromptSpy(monkeypatch)
    ext.texts = [_reply("X", "moat")]
    response = api_call(Sessions, ext, question="Et la dette ?", context="h", last_method_id="construction_these")
    assert response["method_used"] == "autre_methode"
    [(args, kwargs)] = spy.lookups
    assert (args, kwargs) == (("Et la dette ?",), {"context": "h", "last_method_id": "construction_these"})
    assert spy.prompts == [{"method": "autre_methode", "existing_thesis": None, "in_progress": None}]
    # Marqueur hors construction_these : aucune progression produit.
    assert _sessions(Sessions, "NVDA") == []


def test_pg_d_no_active_session_and_empty_context_starts_construction_these(engine, Sessions, ext, monkeypatch):
    """(D) Pas d'AnalysisSession active + context vide : nouvelle
    construction_these comme aujourd'hui (nouvelle session business)."""
    spy = _PromptSpy(monkeypatch)
    ext.texts = [_reply("X", "business")]
    response = api_call(Sessions, ext, question="", context="")
    assert response["method_used"] == "construction_these"
    assert spy.prompts == [{"method": "construction_these", "existing_thesis": None, "in_progress": None}]
    assert spy.lookups == []
    [session] = _sessions(Sessions, "NVDA")
    assert (session.status, session.current_step) == ("in_progress", "business")


def api_call(Sessions, ext, **overrides):
    """Un tour /decryptage sur NVDA (client_turn_id neuf)."""
    from tests.test_assistant_delivery import _call
    payload = {"ticker": "NVDA", "client_turn_id": str(uuid.uuid4()), "conversation_key": CONV, **overrides}
    return _call(Sessions, ext, **payload)


# --------------------------------------------------------------------------
# 3. Contre un vrai PostgreSQL — R1-C3C : progression monotone
# --------------------------------------------------------------------------

def _seed(Sessions, step, ticker="NVDA", status="in_progress"):
    with Sessions() as s:
        session = AnalysisSession(user_id=USER, ticker=ticker, status=status, current_step=step)
        s.add(session)
        s.commit()
        return session.id


def _apply(Sessions, step, thesis_text=None, ticker="NVDA", user_contribution=True, answered_step=None):
    """Progression d'un tour. Par défaut une vraie contribution qui répond à
    l'étape actuellement ouverte (R1-C4 : seule une contribution fait avancer
    une session existante, et seulement si answered_step == current_step) ;
    answered_step explicite pour les autres cas."""
    with Sessions() as s:
        if user_contribution and answered_step is None:
            active = dp.find_active_analysis_session(s, USER, ticker)
            answered_step = active.current_step if active is not None else None
        result = dp.apply_construction_these_progress(s, user_id=USER, ticker=ticker, step=step,
                                                      thesis_text=thesis_text, data=None,
                                                      user_contribution=user_contribution,
                                                      answered_step=answered_step)
        result_id = result.id if result is not None else None
        s.commit()
        return result_id


def _state(Sessions, ticker="NVDA"):
    return [(s.id, s.status, s.current_step, s.completed_at is not None) for s in _sessions(Sessions, ticker)]


@pytest.mark.parametrize("current, incoming, expected, log", [
    ("business", "business", "business", "action=same step=business"),
    ("business", "moat", "moat", "action=forward from=business to=moat"),
    ("moat", "chiffres", "chiffres", "action=forward from=moat to=chiffres"),
    ("business", "chiffres", "chiffres", "anomaly=step_skip from=business to=chiffres"),
])
def test_pg_same_forward_and_skip(engine, Sessions, ext, caplog, current, incoming, expected, log):
    sid = _seed(Sessions, current)
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        assert _apply(Sessions, incoming) == sid
    assert _state(Sessions) == [(sid, "in_progress", expected, False)]
    assert f"[DECRYPTAGE-PROGRESS] session={sid} {log}" in caplog.text


@pytest.mark.parametrize("current, incoming", [("chiffres", "business"), ("risques", "moat"),
                                               ("moat", "business"), ("valorisation", "chiffres")])
def test_pg_retrograde_marker_never_damages_the_session(engine, Sessions, ext, caplog, current, incoming):
    """Rétrograde : current_step et status inchangés, même session
    retournée, aucune nouvelle session, anomalie journalisée."""
    sid = _seed(Sessions, current)
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        assert _apply(Sessions, incoming) == sid
    assert _state(Sessions) == [(sid, "in_progress", current, False)]
    assert (f"[DECRYPTAGE-PROGRESS] session={sid} anomaly=retrograde_marker current={current} "
            f"received={incoming}") in caplog.text


def test_pg_risques_to_swot_final_completes(engine, Sessions, ext):
    sid = _seed(Sessions, "risques")
    assert _apply(Sessions, "swot_final", thesis_text="Ma thèse en 3 phrases.") == sid
    assert _state(Sessions) == [(sid, "completed", "swot_final", True)]
    assert [t[0] for t in _product(engine)["theses"]] == ["Ma thèse en 3 phrases."]


def test_pg_swot_final_without_a_session_is_none_as_before(engine, Sessions, ext):
    assert _apply(Sessions, "swot_final", user_contribution=False) is None
    assert _state(Sessions) == []
    # R1-C4 : une contribution sans AnalysisSession active est incohérente
    # (une contribution répond à un event d'une session active) : fail closed.
    with pytest.raises(ValueError):
        _apply(Sessions, "swot_final", thesis_text="Merci !", answered_step="risques")
    assert _state(Sessions) == []
    assert _product(engine) == {"sessions": [], "statements": [], "facts": [], "theses": []}


def test_pg_swot_final_without_real_contribution_neither_completes_nor_creates_a_thesis(engine, Sessions, ext,
                                                                                        caplog):
    """R1-C4 : swot_final sans contribution réelle ne termine pas une session
    existante (status, current_step, completed_at inchangés) et ne crée
    jamais d'InvestmentThesis ; anomalie terminal_without_contribution."""
    sid = _seed(Sessions, "risques")
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        assert _apply(Sessions, "swot_final", thesis_text=None, user_contribution=False) == sid
    assert _state(Sessions) == [(sid, "in_progress", "risques", False)]
    assert _product(engine)["theses"] == [] and _product(engine)["statements"] == []
    assert (f"[DECRYPTAGE-PROGRESS] session={sid} anomaly=terminal_without_contribution current=risques "
            "received=swot_final") in caplog.text


def test_pg_retrograde_never_touches_a_completed_session(engine, Sessions, ext):
    """Une session completed n'est jamais reprise : un marqueur ultérieur
    ouvre une NOUVELLE tentative (règle T1-C1 inchangée)."""
    done = _seed(Sessions, "swot_final", status="completed")
    new = _apply(Sessions, "business", user_contribution=False)
    assert new != done
    assert [(s[1], s[2]) for s in _state(Sessions)] == [("completed", "swot_final"), ("in_progress", "business")]


def test_pg_concurrent_mutation_never_regresses_under_a_stale_snapshot(engine, Sessions, ext):
    """Deux onglets, même ticker, session à moat. L'onglet 1 avance jusqu'à
    valorisation (verrou détenu, non commité). L'onglet 2 applique
    « chiffres » : sans relecture verrouillée il lirait moat (snapshot
    stale), verrait une avancée et écraserait valorisation par chiffres. Le
    verrou le fait attendre puis relire valorisation : rétrograde, la BDD
    reste à valorisation (R1-C4 : sa contribution répondait au moat, qui
    n'est plus l'étape ouverte : aucune progression non plus)."""
    sid = _seed(Sessions, "moat")
    with Sessions() as holder, Sessions() as waiter:
        assert dp.apply_construction_these_progress(holder, user_id=USER, ticker="NVDA", step="valorisation",
                                                    thesis_text=None, data=None, user_contribution=True,
                                                    answered_step="moat").id == sid
        result, error = _run_blocked(engine, holder, waiter, lambda s: dp.apply_construction_these_progress(
            s, user_id=USER, ticker="NVDA", step="chiffres", thesis_text=None, data=None,
            user_contribution=True, answered_step="moat").current_step)
    assert error is None
    assert result == "valorisation"
    assert _state(Sessions) == [(sid, "in_progress", "valorisation", False)]


# --------------------------------------------------------------------------
# 4. Contre un vrai PostgreSQL — UserStatement = contribution réelle
# --------------------------------------------------------------------------

def _statements(engine):
    return [(s[0], s[1]) for s in _product(engine)["statements"]]


def test_pg_a_contribution_appended_creates_the_statement(engine, Sessions, ext):
    """(A) Vraie réponse -> contribution R1-C2 -> UserStatement sur l'étape
    qui était ouverte."""
    _turn(Sessions, ext, _reply("1", "business"))
    second = _turn(Sessions, ext, _reply("2", "moat"), "Ils vendent du luxe.")
    assert _link(engine, second["assistant_turn_id"])["input_action"] == "contribution_appended"
    assert _statements(engine) == [("business", "Ils vendent du luxe.")]


def test_pg_b_no_open_event_creates_no_statement(engine, Sessions, ext):
    """(B) Réponse précédente jamais acquittée (aucune ancre) : le texte n'est
    pas une contribution R1-C2, donc aucun UserStatement, et (R1-C4) la
    progression produit n'avance PAS sur le seul marqueur de Claude."""
    _send(Sessions, ext, _reply("1", "business"))
    second = _send(Sessions, ext, _reply("2", "moat"), "Ils vendent du luxe.")
    assert _link(engine, second["assistant_turn_id"])["input_action"] == "no_open_event"
    assert _statements(engine) == []
    [(_, status, step, _)] = _product(engine)["sessions"]
    assert (status, step) == ("in_progress", "business")


def test_pg_c_no_user_contribution_creates_no_statement(engine, Sessions, ext):
    _turn(Sessions, ext, _reply("1", "business"))
    empty = _turn(Sessions, ext, _reply("2", "business"), "", context="h")
    assert _link(engine, empty["assistant_turn_id"])["input_action"] == "no_user_contribution"
    assert _statements(engine) == []


def _lvmh_in_a_and_nvda_in_b(Sessions, ext, *, lvmh_work=True):
    _turn(Sessions, ext, _reply("L1", "business"), "", context="", conv=CONV_A)
    if lvmh_work:  # même marqueur : E_LVMH business continue, avec travail
        _turn(Sessions, ext, _reply("L2", "business"), "LVMH vend du luxe.", conv=CONV_A)
    _nvda_at_moat(Sessions, ext, conv=CONV_B)


@pytest.mark.parametrize("lvmh_work", [True, False], ids=["travail", "vide"])
def test_pg_d_e_f_navigation_never_becomes_a_statement(engine, Sessions, ext, lvmh_work):
    """(D)(E)(F) « Passons à NVIDIA » dans A (R1-C4 : context_switched) :
    aucun UserStatement, ni dans l'ancienne (LVMH) ni dans la nouvelle
    (NVDA) AnalysisSession."""
    _lvmh_in_a_and_nvda_in_b(Sessions, ext, lvmh_work=lvmh_work)
    before = _statements(engine)
    switch = _turn(Sessions, ext, _reply("N3", "moat"), "Passons à NVIDIA", conv=CONV_A, ticker="NVDA",
                   context="")
    assert _link(engine, switch["assistant_turn_id"])["input_action"] == "context_switched"
    assert _statements(engine) == before
    assert "Passons à NVIDIA" not in json.dumps(_product(engine), default=str)


def test_pg_g_empty_resume_creates_no_statement(engine, Sessions, ext):
    """(G) Reprise vide dans une nouvelle conversation (aucune ancre) : aucun
    UserStatement."""
    _nvda_at_moat(Sessions, ext, conv=CONV_B)
    before = _statements(engine)
    resume = _turn(Sessions, ext, _reply("R", "moat"), "", context="", conv=CONV_A, ticker="NVDA")
    assert _link(engine, resume["assistant_turn_id"])["input_action"] == "no_open_event"
    assert _statements(engine) == before


def test_pg_h_next_real_answer_creates_the_statement_on_the_open_step(engine, Sessions, ext):
    """(H) Après navigation, le tour suivant est une vraie réponse :
    UserStatement sur l'étape réellement ouverte (moat)."""
    _lvmh_in_a_and_nvda_in_b(Sessions, ext)
    _turn(Sessions, ext, _reply("N3", "moat"), "Passons à NVIDIA", conv=CONV_A, ticker="NVDA", context="")
    answer = _turn(Sessions, ext, _reply("N4", "chiffres"), "Leur moat, c'est CUDA.", conv=CONV_A, ticker="NVDA")
    assert _link(engine, answer["assistant_turn_id"])["input_action"] == "contribution_appended"
    nvda = _session_id(engine, "NVDA")
    assert [(s[0], s[1]) for s in _product(engine)["statements"] if s[2] == nvda] == [
        ("business", "NVIDIA vend des GPU."), ("moat", "Leur moat, c'est CUDA.")]


# --------------------------------------------------------------------------
# 5. Scénario croisé R1-C2 + R1-C3 (production)
# --------------------------------------------------------------------------

def test_pg_cross_switch_to_nvda_resumes_moat_and_survives_a_retrograde_marker(engine, Sessions, ext, monkeypatch,
                                                                                 caplog):
    """NVDA current_step = moat, E_NVDA moat open (conversation B) ;
    conversation A contient LVMH. Dans A, l'utilisateur bascule vers NVDA
    (context NVDA vide : le frontend ne transmet pas l'historique LVMH) ;
    Claude répond par erreur avec le marqueur business."""
    _lvmh_in_a_and_nvda_in_b(Sessions, ext)
    nvda_session = _session_id(engine, "NVDA")
    [e_nvda] = [e for e in _events(engine) if e["analysis_session_id"] == nvda_session and e["status"] == "open"]
    spy = _PromptSpy(monkeypatch)

    with caplog.at_level(logging.INFO):
        switch = _send(Sessions, ext, _reply("N3", "business"), "Passons à NVIDIA", conv=CONV_A, ticker="NVDA",
                       context="")
    # Session NVDA chargée, construction_these forcée, prompt de reprise moat.
    [prompt] = spy.prompts
    assert (prompt["method"], prompt["in_progress"]["current_step"]) == ("construction_these", "moat")
    assert switch["method_used"] == "construction_these"
    # Navigation : non-contribution R1-C2, aucun UserStatement.
    link = _link(engine, switch["assistant_turn_id"])
    assert (link["input_action"], link["input_event_id"]) == ("context_switched", None)
    assert "Passons à NVIDIA" not in json.dumps(_product(engine), default=str)
    # Marqueur business accidentel : la session reste moat et la livraison y
    # est rattachée.
    assert [s for s in _state(Sessions) if s[0] == nvda_session] == [(nvda_session, "in_progress", "moat", False)]
    assert [r["analysis_session_id"] for r in _rows(engine) if str(r["id"]) == switch["assistant_turn_id"]] == [
        nvda_session]
    assert "anomaly=retrograde_marker current=moat received=business" in caplog.text

    # ACK : R1-C2 => continued_without_boundary_signal, E_NVDA moat reste open.
    _ack(Sessions, switch, conv=CONV_A)
    link = _link(engine, switch["assistant_turn_id"])
    [trace] = [t for t in _traces(engine, e_nvda["id"]) if t["support_payload"] == {"visible_content": "Réponse N3."}]
    assert (link["response_action"], link["response_event_id"], link["support_trace_id"]) == (
        "continued_without_boundary_signal", e_nvda["id"], trace["id"])
    assert _event(engine, e_nvda["id"])["status"] == "open"

    # Tour suivant : vraie contribution -> UserStatement moat, support_refs_before.
    turn = str(uuid.uuid4())
    answer = _turn(Sessions, ext, _reply("N4", "chiffres"), "Leur moat, c'est CUDA.", conv=CONV_A, ticker="NVDA",
                   turn=turn)
    assert _link(engine, answer["assistant_turn_id"])["input_action"] == "contribution_appended"
    work = _event(engine, e_nvda["id"])["user_work_snapshot"]
    assert (work[-1]["text"], work[-1]["contribution_id"], work[-1]["session_ref"]) == (
        "Leur moat, c'est CUDA.", turn, CONV_A)
    assert str(trace["id"]) in work[-1]["support_refs_before"]
    assert [(s[0], s[1]) for s in _product(engine)["statements"] if s[2] == nvda_session] == [
        ("business", "NVIDIA vend des GPU."), ("moat", "Leur moat, c'est CUDA.")]
    assert [s for s in _state(Sessions) if s[0] == nvda_session] == [(nvda_session, "in_progress", "chiffres", False)]
    _no_t3(engine)
    assert _count(engine, "cognitive_events") == len(_events(engine))
    assert not [e for e in _events(engine) if e["task_kind"] is not None]
