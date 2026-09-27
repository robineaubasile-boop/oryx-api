"""Tests de T1-C1 : Décrypte ne dépend plus que de analysis_sessions.

T1-B2 avait basculé les nouvelles tentatives sur AnalysisSession en gardant
CompanyAnalysis (fallback legacy + miroir). T1-C1 retire tout usage
applicatif de CompanyAnalysis ; T1-C2 supprime ensuite le modèle et la
table (migration 0004_drop_company_analyses).

Aucune migration dans ce chantier : le schéma testé est celui produit par
`alembic upgrade head` (= 0004_drop_company_analyses depuis T1-C2), ce qui
vérifie que les parcours T1-C1 fonctionnent sans company_analyses.

1. Tests sans base (toujours exécutés) : tête Alembic attendue, aucune
   référence à CompanyAnalysis dans api.py.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T1-A/B1/B2) :
   uniquement si ORYX_TEST_DATABASE_URL pointe vers une base DÉDIÉE dont le
   nom contient "test" (schéma public détruit et recréé). Sinon SKIPPÉS :
   rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t1c1_test \\
           python -m pytest tests/test_t1c1_decryptage_sessions.py

   Les scénarios appellent directement les fonctions des endpoints
   (api.decryptage, api.get_theses, api.delete_thesis) ; seuls la
   récupération des données financières, l'appel Claude et la résolution
   de ticker (réseau) sont remplacés. L'appel Claude factice renvoie le
   marqueur <!--ORYX_STEP:...--> voulu.
"""
import ast
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import api
import core.db
from core.db import Base
from core.models import (
    AnalysisFact,
    AnalysisSession,
    InvestmentThesis,
    User,
    UserStatement,
)
from tests.test_migration_0002_analysis_sessions import (
    BASELINE,
    REPO_ROOT,
    T1A,
    _reset_schema,
    _run_alembic,
    _script_directory,
    pg_engine,  # noqa: F401 — fixture
    pg_url,  # noqa: F401 — fixture
)

T1B1 = "0003_analysis_session_links"
T1C2 = "0004_drop_company_analyses"
USER = "user-t1c1"
TICKER = "MC.PA"
DATA = {
    "name": "LVMH", "current_price": 600.0, "currency": "EUR",
    "operating_margin": 0.26, "roe": 0.24, "net_cash": -1.0e9,
}
SNAPSHOT = {"operating_margin": 0.26, "roe": 0.24, "net_cash": -1.0e9}


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_no_migration_added_by_t1c1_head_is_0004():
    """(1) T1-C1 n'a ajouté aucune migration ; la seule ajoutée depuis est
    0004 (T1-C2), qui est la tête."""
    script = _script_directory()
    assert script.get_heads() == [T1C2]
    assert {rev.revision for rev in script.walk_revisions()} == {BASELINE, T1A, T1B1, T1C2}
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [
        "0001_current_oryx_baseline.py",
        "0002_analysis_sessions.py",
        "0003_analysis_session_links.py",
        "0004_drop_company_analyses.py",
    ]


def test_api_has_no_reference_to_company_analysis():
    """(9) api.py ne référence plus CompanyAnalysis / company_analyses, ni
    l'ancien chemin legacy (filtre created_at, _track_legacy_progress)."""
    source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    for forbidden in ("CompanyAnalysis", "company_analyses", "_track_legacy_progress"):
        assert forbidden not in source, forbidden

    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name)
    assert "CompanyAnalysis" not in names
    assert not hasattr(api, "CompanyAnalysis")
    assert not hasattr(api, "_track_legacy_progress")


def test_company_analysis_model_is_removed():
    """(5) T1-C2 : le modèle CompanyAnalysis n'existe plus."""
    import core.models
    assert "company_analyses" not in Base.metadata.tables
    assert not hasattr(core.models, "CompanyAnalysis")


# --------------------------------------------------------------------------
# 2. Contre un vrai PostgreSQL
# --------------------------------------------------------------------------

@pytest.fixture
def db(pg_url, pg_engine, monkeypatch):
    """Schéma = alembic upgrade head ; api.py branché sur cette base."""
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", "head")
    factory = sessionmaker(bind=pg_engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(core.db, "SessionLocal", factory)
    with factory() as s:
        s.add_all([User(id=USER), User(id="someone-else")])
        s.commit()
    return factory


class _FakeClaude:
    """Remplace anthropic.Anthropic : renvoie le prochain marqueur voulu et
    mémorise le system prompt reçu."""

    def __init__(self):
        self.next_step = None
        self.system_prompts = []
        self.messages = self

    def __call__(self, *args, **kwargs):
        return self

    def create(self, **kwargs):
        self.system_prompts.append(kwargs["system"])
        text = "Réponse pédagogique."
        if self.next_step:
            text += f"\n<!--ORYX_STEP:{self.next_step}-->"
        block = type("Block", (), {"type": "text", "text": text})()
        return type("Resp", (), {"content": [block], "stop_reason": "end_turn"})()


@pytest.fixture
def claude(db, monkeypatch):
    fake = _FakeClaude()
    monkeypatch.setattr(api.anthropic, "Anthropic", fake)
    monkeypatch.setattr(api, "fetch_financial_data", lambda t: {"success": True, "data": dict(DATA)})
    monkeypatch.setattr(api, "normalize_ticker", lambda t: t)
    return fake


class _Client:
    """Appelle directement les fonctions des endpoints (sans serveur HTTP)."""

    def __init__(self, factory):
        self.factory = factory

    def decryptage(self, payload):
        return api.decryptage(api.DecryptageRequest(**payload))

    def theses(self, user_id=USER):
        with self.factory() as s:
            return api.get_theses(user_id, db=s)

    def delete(self, ticker=TICKER, user_id=USER):
        with self.factory() as s:
            return api.delete_thesis(user_id, ticker, db=s)


@pytest.fixture
def client(db, claude):
    return _Client(db)


def _turn(client, claude, step, question="", context="", ticker=TICKER, user_id=USER):
    """Un message utilisateur sur /decryptage ; Claude répond avec `step`."""
    claude.next_step = step
    r = client.decryptage({
        "ticker": ticker, "question": question, "context": context,
        "last_method_id": "construction_these" if context else None,
        "user_id": user_id,
    })
    assert r["success"], r
    assert "ORYX_STEP" not in r["analysis"]
    return r


def _full_attempt(client, claude, prefix, ticker=TICKER):
    """Tentative complète business → swot_final. Retourne les réponses
    utilisateur enregistrables (hors message déclencheur)."""
    _turn(client, claude, "business", ticker=ticker)
    answers = []
    for step in ("moat", "chiffres", "valorisation", "risques", "swot_final"):
        answer = f"{prefix} réponse avant {step}"
        _turn(client, claude, step, question=answer, context="historique", ticker=ticker)
        answers.append(answer)
    return answers


def _sessions(db, **filters):
    with db() as s:
        q = s.query(AnalysisSession).filter_by(user_id=USER, **filters)
        return q.order_by(AnalysisSession.started_at).all()


def _rows(db, model, **filters):
    with db() as s:
        return s.query(model).filter_by(user_id=USER, **filters).order_by(model.id).all()


def _company_analyses_exists(db):
    with db() as s:
        return "company_analyses" in sa.inspect(s.connection()).get_table_names()


def test_first_attempt_creates_exactly_one_session_and_reuses_it(db, client, claude):
    """(2)(3) Première analyse : une seule session in_progress, même UUID
    aux tours suivants, pas de session par message."""
    assert _sessions(db) == []
    _turn(client, claude, "business")
    [first] = _sessions(db)
    assert first.status == "in_progress"
    assert first.current_step == "business"
    assert first.ticker == TICKER
    assert first.completed_at is None
    assert first.started_at.tzinfo is not None and first.updated_at.tzinfo is not None

    _turn(client, claude, "moat", question="Ils vendent du luxe.", context="h")
    _turn(client, claude, "moat", question="Précision.", context="h")
    _turn(client, claude, "chiffres", question="Marque forte.", context="h")
    # Message sans marqueur : aucun suivi.
    _turn(client, claude, None, question="Question libre.", context="h")

    [same] = _sessions(db)
    assert same.id == first.id
    assert same.status == "in_progress"
    assert same.current_step == "chiffres"
    assert same.updated_at > first.updated_at
    # Plus aucun miroir CompanyAnalysis (la table n'existe plus en 0004).
    assert not _company_analyses_exists(db)


def test_facts_statements_and_thesis_share_the_session_uuid(db, client, claude):
    """(4)(5) Faits, déclarations et thèse finale portent le même UUID ;
    swot_final → completed + completed_at."""
    answers = _full_attempt(client, claude, "A")
    [session] = _sessions(db)

    facts = _rows(db, AnalysisFact)
    assert {f.fact_type: f.fact_value for f in facts} == SNAPSHOT
    assert {f.analysis_session_id for f in facts} == {session.id}

    statements = _rows(db, UserStatement)
    assert [(s.step, s.statement_text) for s in statements] == list(zip(
        ("business", "moat", "chiffres", "valorisation", "risques"), answers,
    ))
    assert {s.analysis_session_id for s in statements} == {session.id}

    [thesis] = _rows(db, InvestmentThesis)
    assert thesis.thesis_text == answers[-1]
    assert thesis.analysis_session_id == session.id

    assert session.status == "completed"
    assert session.current_step == "swot_final"
    assert session.completed_at is not None and session.completed_at.tzinfo is not None
    assert session.updated_at >= session.completed_at
    assert not _company_analyses_exists(db)


def test_second_attempt_gets_new_uuid_and_sessions_are_isolated(db, client, claude):
    """(6)(7)(10) Deuxième analyse du même ticker : nouvel UUID ; la session
    A terminée n'est pas réutilisée ; B ne lit rien de A ; swot_final sans
    session active (conversation libre après le bilan) ne crée rien."""
    _full_attempt(client, claude, "A")
    [a] = _sessions(db)

    _turn(client, claude, "swot_final", question="Merci !", context="h")
    assert len(_sessions(db)) == 1
    assert len(_rows(db, InvestmentThesis)) == 1

    _turn(client, claude, "business")
    _turn(client, claude, "moat", question="B business", context="h")
    a_after, b = _sessions(db)
    assert a_after.id == a.id and a_after.status == "completed"
    assert b.id != a.id and b.status == "in_progress"

    assert api._get_analysis_progress(USER, TICKER) == {
        "current_step": "moat",
        "statements": [{"step": "business", "text": "B business"}],
        "facts": SNAPSHOT,
    }
    assert len(_rows(db, AnalysisFact, analysis_session_id=b.id)) == 3
    assert len(_rows(db, AnalysisFact)) == 6

    for step in ("chiffres", "valorisation", "risques", "swot_final"):
        _turn(client, claude, step, question=f"B {step}", context="h")
    theses = _rows(db, InvestmentThesis)
    assert [t.analysis_session_id for t in theses] == [a.id, b.id]
    assert [s.status for s in _sessions(db)] == ["completed", "completed"]
    a_statements = _rows(db, UserStatement, analysis_session_id=a.id)
    b_statements = _rows(db, UserStatement, analysis_session_id=b.id)
    assert all(s.statement_text.startswith("A ") for s in a_statements)
    assert all(s.statement_text.startswith("B") for s in b_statements)


def test_swot_final_without_any_session_creates_nothing(db, client, claude):
    """(10) swot_final sans session active (et sans aucun historique) :
    aucune session artificielle, aucune thèse."""
    _turn(client, claude, "swot_final", question="Ma thèse ?", context="h")
    assert _sessions(db) == []
    assert _rows(db, InvestmentThesis) == []
    assert _rows(db, UserStatement) == []
    assert _rows(db, AnalysisFact) == []


def test_progress_reads_only_the_active_session(db):
    """(8) _get_analysis_progress ne lit que l'AnalysisSession in_progress et
    ses lignes : ni lignes sans session, ni lignes d'autres sessions, et
    aucun filtre temporel."""
    long_ago = datetime.utcnow() - timedelta(days=30)
    with db() as s:
        s.add(UserStatement(user_id=USER, ticker=TICKER, step="business", statement_text="sans session"))
        s.add(AnalysisFact(user_id=USER, ticker=TICKER, fact_type="roe", fact_value=0.11))
        s.commit()
    assert api._get_analysis_progress(USER, TICKER) is None

    with db() as s:
        done = AnalysisSession(user_id=USER, ticker=TICKER, status="completed", current_step="swot_final",
                               completed_at=datetime.now(timezone.utc))
        active = AnalysisSession(user_id=USER, ticker=TICKER, status="in_progress", current_step="chiffres")
        s.add_all([done, active])
        s.flush()
        s.add(UserStatement(user_id=USER, ticker=TICKER, step="moat", statement_text="autre session",
                            analysis_session_id=done.id))
        s.add(AnalysisFact(user_id=USER, ticker=TICKER, fact_type="roe", fact_value=0.99,
                           analysis_session_id=done.id))
        # Dates antérieures au début de la session : lues quand même.
        s.add(UserStatement(user_id=USER, ticker=TICKER, step="business", statement_text="active",
                            statement_date=long_ago, analysis_session_id=active.id))
        s.add(AnalysisFact(user_id=USER, ticker=TICKER, fact_type="eps", fact_value=12.0,
                           fact_date=long_ago, analysis_session_id=active.id))
        s.commit()

    assert api._get_analysis_progress(USER, TICKER) == {
        "current_step": "chiffres",
        "statements": [{"step": "business", "text": "active"}],
        "facts": {"eps": 12.0},
    }


def test_abandoned_and_completed_sessions_are_never_resumed(db, client, claude):
    """(16) Seules des sessions completed/abandoned : aucune progression,
    et un nouveau tour crée une nouvelle session."""
    with db() as s:
        s.add(AnalysisSession(user_id=USER, ticker=TICKER, status="abandoned", current_step="moat"))
        s.add(AnalysisSession(user_id=USER, ticker=TICKER, status="completed", current_step="swot_final",
                              completed_at=datetime.now(timezone.utc)))
        s.commit()
    old_ids = {s.id for s in _sessions(db)}
    assert api._get_analysis_progress(USER, TICKER) is None

    _turn(client, claude, "moat")
    new = [s for s in _sessions(db) if s.id not in old_ids]
    assert len(new) == 1 and new[0].status == "in_progress" and new[0].current_step == "moat"
    assert sorted(s.status for s in _sessions(db) if s.id in old_ids) == ["abandoned", "completed"]


def test_theses_listing_active_session_wins_over_old_thesis(db, client, claude):
    """(11) Ancienne thèse + nouvelle session en cours : GET /theses affiche
    « En cours » (theses=[]) ; Décrypte reprend la session en cours."""
    _full_attempt(client, claude, "A")
    _turn(client, claude, "business")
    _turn(client, claude, "moat", question="B business", context="h")
    [_, b] = _sessions(db)

    assert client.theses() == [{
        "ticker": TICKER,
        "current_step": "moat",
        "updated_at": b.updated_at.isoformat(),
        "theses": [],
    }]

    claude.system_prompts.clear()
    _turn(client, claude, "chiffres")  # réouverture : context vide
    prompt = claude.system_prompts[-1]
    assert "analyse déjà EN COURS" in prompt
    assert "B business" in prompt
    assert "thèse déjà formulée" not in prompt


def test_theses_listing_finished_state_without_active_session(db, client, claude):
    """(12) Sans session active, avec thèse(s) : état terminé, thèses de la
    plus récente à la plus ancienne. Ticker avec seulement des sessions
    abandoned : absent. Autre utilisateur : isolé."""
    _full_attempt(client, claude, "A")
    _full_attempt(client, claude, "B")
    _turn(client, claude, "business", ticker="AAPL")
    with db() as s:
        s.add(AnalysisSession(user_id=USER, ticker="MSFT", status="abandoned", current_step="moat"))
        s.commit()

    listing = {e["ticker"]: e for e in client.theses()}
    assert set(listing) == {TICKER, "AAPL"}
    done = listing[TICKER]
    assert done["current_step"] == "swot_final"
    assert [t["text"] for t in done["theses"]] == ["B réponse avant swot_final", "A réponse avant swot_final"]
    latest = _rows(db, InvestmentThesis)[-1]
    assert done["updated_at"] == latest.created_at.replace(tzinfo=timezone.utc).isoformat()
    assert listing["AAPL"]["current_step"] == "business" and listing["AAPL"]["theses"] == []

    assert client.theses("someone-else") == []
    # Sans session active, Décrypte reprend la dernière thèse.
    claude.system_prompts.clear()
    _turn(client, claude, None)
    assert "thèse déjà formulée" in claude.system_prompts[-1]
    assert '"B réponse avant swot_final"' in claude.system_prompts[-1]


def test_delete_abandons_in_progress_sessions_and_keeps_history(db, client, claude):
    """(13)(14)(15) DELETE : thèses supprimées, toutes les sessions
    in_progress → abandoned (completed_at NULL), rien d'autre supprimé ;
    une nouvelle analyse crée un nouvel UUID."""
    _full_attempt(client, claude, "A")
    _turn(client, claude, "business")
    _turn(client, claude, "moat", question="B business", context="h")
    # Anomalie : une deuxième session in_progress.
    with db() as s:
        s.add(AnalysisSession(user_id=USER, ticker=TICKER, status="in_progress", current_step="business"))
        s.commit()
    # Autre ticker / autre utilisateur : non touchés.
    _turn(client, claude, "business", ticker="AAPL")
    _turn(client, claude, "business", user_id="someone-else")

    facts_before = len(_rows(db, AnalysisFact))
    statements_before = len(_rows(db, UserStatement))
    before = {s.id: s for s in _sessions(db, ticker=TICKER)}
    assert len(before) == 3
    t0 = datetime.now(timezone.utc)

    assert client.delete() == {"success": True}

    assert _rows(db, InvestmentThesis, ticker=TICKER) == []
    after = {s.id: s for s in _sessions(db, ticker=TICKER)}
    assert set(after) == set(before)
    for sid, s in after.items():
        if before[sid].status == "in_progress":
            assert s.status == "abandoned"
            assert s.completed_at is None
            assert s.updated_at >= t0 and s.updated_at.tzinfo is not None
        else:
            assert s.status == "completed" and s.completed_at is not None
    assert len(_rows(db, AnalysisFact)) == facts_before
    assert len(_rows(db, UserStatement)) == statements_before
    [aapl] = _sessions(db, ticker="AAPL")
    assert aapl.status == "in_progress"
    with db() as s:
        [other] = s.query(AnalysisSession).filter_by(user_id="someone-else").all()
        assert other.status == "in_progress"

    assert api._get_analysis_progress(USER, TICKER) is None
    assert [e["ticker"] for e in client.theses()] == ["AAPL"]

    _turn(client, claude, "business")
    new = [s for s in _sessions(db, ticker=TICKER) if s.id not in before]
    assert len(new) == 1 and new[0].status == "in_progress"
    assert api._get_analysis_progress(USER, TICKER) == {
        "current_step": "business", "statements": [], "facts": SNAPSHOT,
    }


def test_multiple_in_progress_sessions_anomaly_is_deterministic(db, client, claude, capsys):
    """(17) Plusieurs sessions in_progress par anomalie : la plus récente
    est choisie, aucune troisième n'est créée, l'anomalie est journalisée."""
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    with db() as s:
        old = AnalysisSession(user_id=USER, ticker=TICKER, status="in_progress",
                              current_step="moat", started_at=base, updated_at=base)
        recent = AnalysisSession(user_id=USER, ticker=TICKER, status="in_progress",
                                 current_step="chiffres", started_at=base + timedelta(minutes=5),
                                 updated_at=base + timedelta(minutes=5))
        s.add_all([old, recent])
        s.flush()
        s.add(UserStatement(user_id=USER, ticker=TICKER, step="business", statement_text="old",
                            analysis_session_id=old.id))
        s.add(UserStatement(user_id=USER, ticker=TICKER, step="moat", statement_text="recent",
                            analysis_session_id=recent.id))
        s.commit()
        old_id, recent_id = old.id, recent.id

    progress = api._get_analysis_progress(USER, TICKER)
    assert progress["current_step"] == "chiffres"
    assert progress["statements"] == [{"step": "moat", "text": "recent"}]
    [entry] = client.theses()
    assert entry["current_step"] == "chiffres"

    _turn(client, claude, "valorisation", question="suite", context="h")
    sessions = {s.id: s for s in _sessions(db)}
    assert set(sessions) == {old_id, recent_id}
    assert sessions[recent_id].current_step == "valorisation"
    assert sessions[old_id].current_step == "moat"
    [st] = _rows(db, UserStatement, statement_text="suite")
    assert st.analysis_session_id == recent_id and st.step == "chiffres"
    assert "[ANALYSIS-SESSION ANOMALY]" in capsys.readouterr().out


def test_other_ticker_and_other_user_are_isolated(db, client, claude):
    """(7) Une session in_progress n'est réutilisée que pour son user+ticker."""
    _turn(client, claude, "business")
    _turn(client, claude, "business", ticker="AAPL")
    sessions = _sessions(db)
    assert sorted(s.ticker for s in sessions) == ["AAPL", TICKER]
    assert len({s.id for s in sessions}) == 2
    assert api._get_analysis_progress("someone-else", TICKER) is None


def test_schema_unchanged_and_company_analyses_absent(db, client, claude, pg_engine):
    """(18) Après un parcours complet (analyse, GET, DELETE, nouvelle
    analyse) : schéma = 0004, Base.metadata identique au schéma migré,
    company_analyses absente (supprimée par T1-C2)."""
    _full_attempt(client, claude, "A")
    client.theses()
    _turn(client, claude, "business")
    client.delete()
    _turn(client, claude, "business")

    with pg_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == T1C2
        assert "company_analyses" not in sa.inspect(conn).get_table_names()

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    with pg_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []
