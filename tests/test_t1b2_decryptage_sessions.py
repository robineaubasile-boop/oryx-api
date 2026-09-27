"""Tests de T1-B2 : bascule applicative de Décrypte vers analysis_sessions.

Aucune migration dans ce chantier : le schéma testé est celui produit par
`alembic upgrade head` (= 0003_analysis_session_links).

1. Tests sans base (toujours exécutés) : aucune migration ajoutée.

2. Tests contre un vrai PostgreSQL (mêmes conditions que T1-A/T1-B1) :
   uniquement si ORYX_TEST_DATABASE_URL pointe vers une base DÉDIÉE dont le
   nom contient "test" (schéma public détruit et recréé). Sinon SKIPPÉS :
   rien n'est simulé avec SQLite.

       ORYX_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/oryx_t1b2_test \\
           python -m pytest tests/test_t1b2_decryptage_sessions.py

   Les scénarios appellent directement la fonction de l'endpoint
   /decryptage (api.decryptage) ; seuls la récupération des données financières, l'appel
   Claude et la résolution de ticker (réseau) sont remplacés. L'appel
   Claude factice renvoie le marqueur <!--ORYX_STEP:...--> voulu.
"""
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import api
import core.db
from core.models import (
    AnalysisFact,
    AnalysisSession,
    CompanyAnalysis,
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
USER = "user-t1b2"
TICKER = "MC.PA"
DATA = {
    "name": "LVMH", "current_price": 600.0, "currency": "EUR",
    "operating_margin": 0.26, "roe": 0.24, "net_cash": -1.0e9,
}


# --------------------------------------------------------------------------
# 1. Sans base
# --------------------------------------------------------------------------

def test_no_migration_added_head_is_still_0003():
    """(18) Aucun changement de schéma : la tête Alembic reste 0003 et
    aucun nouveau fichier de migration n'existe."""
    script = _script_directory()
    assert script.get_heads() == [T1B1]
    assert {rev.revision for rev in script.walk_revisions()} == {BASELINE, T1A, T1B1}
    files = sorted(p.name for p in (REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert files == [
        "0001_current_oryx_baseline.py",
        "0002_analysis_sessions.py",
        "0003_analysis_session_links.py",
    ]


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
        s.add(User(id=USER))
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

    def theses(self, user_id):
        with self.factory() as s:
            return api.get_theses(user_id, db=s)


@pytest.fixture
def client(db, claude):
    return _Client(db)


def _turn(client, claude, step, question="", context="", ticker=TICKER):
    """Un message utilisateur sur /decryptage ; Claude répond avec `step`."""
    claude.next_step = step
    r = client.decryptage({
        "ticker": ticker, "question": question, "context": context,
        "last_method_id": "construction_these" if context else None,
        "user_id": USER,
    })
    assert r["success"], r
    assert "ORYX_STEP" not in r["analysis"]
    return r


def _full_attempt(client, claude, prefix):
    """Tentative complète business → swot_final. Retourne les réponses
    utilisateur enregistrables (hors message déclencheur)."""
    _turn(client, claude, "business")
    answers = []
    for step in ("moat", "chiffres", "valorisation", "risques", "swot_final"):
        answer = f"{prefix} réponse avant {step}"
        _turn(client, claude, step, question=answer, context="historique")
        answers.append(answer)
    return answers


def _sessions(db, **filters):
    with db() as s:
        q = s.query(AnalysisSession).filter_by(user_id=USER, **filters)
        return q.order_by(AnalysisSession.started_at).all()


def _rows(db, model, **filters):
    with db() as s:
        return s.query(model).filter_by(user_id=USER, **filters).order_by(model.id).all()


def _company_analysis(db):
    with db() as s:
        return s.query(CompanyAnalysis).filter_by(user_id=USER, ticker=TICKER).one()


def test_first_attempt_creates_exactly_one_session_and_reuses_it(db, client, claude):
    """(1)(2)(3) Première tentative : une seule session in_progress, même
    UUID aux tours suivants, pas de session par message."""
    assert _sessions(db) == []
    _turn(client, claude, "business")
    [first] = _sessions(db)
    assert first.status == "in_progress"
    assert first.current_step == "business"
    assert first.ticker == TICKER
    assert first.completed_at is None
    assert first.started_at.tzinfo is not None

    _turn(client, claude, "moat", question="Ils vendent du luxe.", context="h")
    _turn(client, claude, "moat", question="Précision.", context="h")
    _turn(client, claude, "chiffres", question="Marque forte.", context="h")
    # Message sans marqueur : aucun suivi.
    _turn(client, claude, None, question="Question libre.", context="h")

    [same] = _sessions(db)
    assert same.id == first.id
    assert same.status == "in_progress"
    assert same.current_step == "chiffres"


def test_facts_statements_and_thesis_share_the_session_uuid(db, client, claude):
    """(4)(5)(6)(7) Faits, déclarations et thèse finale portent le même
    UUID ; swot_final → completed + completed_at."""
    answers = _full_attempt(client, claude, "A")
    [session] = _sessions(db)

    facts = _rows(db, AnalysisFact)
    assert {f.fact_type: f.fact_value for f in facts} == {
        "operating_margin": 0.26, "roe": 0.24, "net_cash": -1.0e9,
    }
    assert {f.analysis_session_id for f in facts} == {session.id}
    assert all(f.ticker == TICKER and f.fact_date is not None for f in facts)

    statements = _rows(db, UserStatement)
    assert [(s.step, s.statement_text) for s in statements] == list(zip(
        ("business", "moat", "chiffres", "valorisation", "risques"), answers,
    ))
    assert {s.analysis_session_id for s in statements} == {session.id}
    assert all(s.statement_date is not None for s in statements)

    [thesis] = _rows(db, InvestmentThesis)
    assert thesis.thesis_text == answers[-1]
    assert thesis.analysis_session_id == session.id
    assert thesis.created_at is not None

    assert session.status == "completed"
    assert session.current_step == "swot_final"
    assert session.completed_at is not None and session.completed_at.tzinfo is not None
    assert session.updated_at >= session.completed_at


def test_completed_session_is_never_reused_second_attempt_gets_new_uuid(db, client, claude):
    """(8)(9)(10) Deuxième analyse du même ticker : nouvel UUID ; la session
    A terminée n'est pas réutilisée ; la progression de B ne lit rien de A."""
    _full_attempt(client, claude, "A")
    [a] = _sessions(db)

    # Conversation libre après le bilan : aucune nouvelle session.
    _turn(client, claude, "swot_final", question="Merci !", context="h")
    assert len(_sessions(db)) == 1

    _turn(client, claude, "business")
    _turn(client, claude, "moat", question="B business", context="h")
    a_after, b = _sessions(db)
    assert a_after.id == a.id and a_after.status == "completed"
    assert b.id != a.id and b.status == "in_progress"

    progress = api._get_analysis_progress(USER, TICKER)
    assert progress == {
        "current_step": "moat",
        "statements": [{"step": "business", "text": "B business"}],
        "facts": {"operating_margin": 0.26, "roe": 0.24, "net_cash": -1.0e9},
    }
    b_facts = _rows(db, AnalysisFact, analysis_session_id=b.id)
    assert len(b_facts) == 3  # snapshot propre à B, distinct de celui de A
    assert len(_rows(db, AnalysisFact)) == 6

    # B se termine : sa thèse porte son propre UUID.
    for step in ("chiffres", "valorisation", "risques", "swot_final"):
        _turn(client, claude, step, question=f"B {step}", context="h")
    theses = _rows(db, InvestmentThesis)
    assert [t.analysis_session_id for t in theses] == [a.id, b.id]
    assert [s.status for s in _sessions(db)] == ["completed", "completed"]


def test_active_session_has_priority_over_finished_thesis(db, client, claude):
    """(11) Ancienne thèse LVMH terminée + nouvelle session LVMH en cours :
    Décrypte reprend la session en cours."""
    _full_attempt(client, claude, "A")
    _turn(client, claude, "business")
    _turn(client, claude, "moat", question="B business", context="h")

    claude.system_prompts.clear()
    _turn(client, claude, "chiffres")  # réouverture : context vide
    prompt = claude.system_prompts[-1]
    assert "analyse déjà EN COURS" in prompt
    assert "B business" in prompt
    assert "thèse déjà formulée" not in prompt

    # Sans session en cours, la dernière thèse reprend la priorité.
    for step in ("valorisation", "risques", "swot_final"):
        _turn(client, claude, step, question=f"B {step}", context="h")
    claude.system_prompts.clear()
    _turn(client, claude, None)
    prompt = claude.system_prompts[-1]
    assert "thèse déjà formulée" in prompt
    assert '"B swot_final"' in prompt
    assert "analyse déjà EN COURS" not in prompt


def _seed_legacy_attempt(db, step="moat", started_minutes_ago=10):
    """Tentative commencée avant T1-B2 : CompanyAnalysis sans session,
    données avec analysis_session_id NULL."""
    start = datetime.utcnow() - timedelta(minutes=started_minutes_ago)
    with db() as s:
        s.add(CompanyAnalysis(user_id=USER, ticker=TICKER, current_step=step, created_at=start))
        s.add(AnalysisFact(user_id=USER, ticker=TICKER, fact_type="roe", fact_value=0.11, fact_date=start))
        s.add(UserStatement(user_id=USER, ticker=TICKER, step="business", statement_text="legacy business",
                            statement_date=start + timedelta(minutes=1)))
        # Orphelin d'une tentative supprimée avant : exclu par created_at.
        s.add(UserStatement(user_id=USER, ticker=TICKER, step="business", statement_text="orphelin",
                            statement_date=start - timedelta(days=1)))
        s.commit()


def test_legacy_attempt_stays_legacy_until_its_end(db, client, claude):
    """(12)(13) Tentative legacy en cours : aucune AnalysisSession créée,
    aucun backfill, elle termine sur l'ancien chemin."""
    _seed_legacy_attempt(db)

    claude.system_prompts.clear()
    _turn(client, claude, "moat")  # réouverture
    assert "legacy business" in claude.system_prompts[-1]
    assert "orphelin" not in claude.system_prompts[-1]

    for step in ("chiffres", "valorisation", "risques", "swot_final"):
        _turn(client, claude, step, question=f"L {step}", context="h")

    assert _sessions(db) == []
    assert _company_analysis(db).current_step == "swot_final"
    assert all(s.analysis_session_id is None for s in _rows(db, UserStatement))
    assert all(f.analysis_session_id is None for f in _rows(db, AnalysisFact))
    [thesis] = _rows(db, InvestmentThesis)
    assert thesis.thesis_text == "L swot_final" and thesis.analysis_session_id is None

    # Tentative suivante après la fin du legacy = nouveau système.
    _turn(client, claude, "business")
    [new] = _sessions(db)
    assert new.status == "in_progress"


def test_legacy_fallback_only_reads_rows_without_session(db):
    """(14) Le fallback legacy ne lit que analysis_session_id IS NULL, même
    si des lignes du nouveau système sont postérieures à created_at."""
    _seed_legacy_attempt(db)
    now = datetime.utcnow()
    with db() as s:
        foreign = AnalysisSession(user_id=USER, ticker=TICKER, status="completed",
                                  current_step="swot_final", completed_at=datetime.now(timezone.utc))
        s.add(foreign)
        s.flush()
        s.add(UserStatement(user_id=USER, ticker=TICKER, step="moat", statement_text="nouveau système",
                            statement_date=now, analysis_session_id=foreign.id))
        s.add(AnalysisFact(user_id=USER, ticker=TICKER, fact_type="roe", fact_value=0.99,
                           fact_date=now, analysis_session_id=foreign.id))
        s.commit()

    assert api._get_analysis_progress(USER, TICKER) == {
        "current_step": "moat",
        "statements": [{"step": "business", "text": "legacy business"}],
        "facts": {"roe": 0.11},
    }


def test_company_analysis_mirror_is_maintained(db, client, claude):
    """(15) CompanyAnalysis reste maintenu pour les nouvelles sessions."""
    _turn(client, claude, "business")
    ca = _company_analysis(db)
    [session] = _sessions(db)
    assert ca.current_step == "business"
    first_created_at = ca.created_at
    assert first_created_at is not None

    _turn(client, claude, "moat", question="x", context="h")
    assert _company_analysis(db).current_step == "moat"
    for step in ("chiffres", "valorisation", "risques", "swot_final"):
        _turn(client, claude, step, question=step, context="h")
    assert _company_analysis(db).current_step == "swot_final"

    # Nouvelle tentative : même ligne miroir, recalée sur la nouvelle session.
    _turn(client, claude, "business")
    ca = _company_analysis(db)
    assert ca.current_step == "business"
    assert ca.created_at > first_created_at
    with db() as s:
        assert s.query(CompanyAnalysis).count() == 1

    # GET /theses (lit CompanyAnalysis) reste fonctionnel.
    listing = client.theses(USER)
    assert listing[0]["ticker"] == TICKER and listing[0]["current_step"] == "business"
    assert len(listing[0]["theses"]) == 1


def _legacy_v0003_progress(factory, user_id, ticker):
    """Copie de la logique de lecture de _get_analysis_progress telle que
    sur main avant T1-B2 (ea48c66) : ce que ferait l'ancien code après un
    rollback applicatif sur le schéma 0003."""
    session = factory()
    try:
        analysis = session.query(CompanyAnalysis).filter(
            CompanyAnalysis.user_id == user_id, CompanyAnalysis.ticker == ticker
        ).first()
        if not analysis or not analysis.current_step or analysis.current_step == "swot_final":
            return None
        since = analysis.created_at
        statements_q = session.query(UserStatement).filter(
            UserStatement.user_id == user_id, UserStatement.ticker == ticker
        )
        facts_q = session.query(AnalysisFact).filter(
            AnalysisFact.user_id == user_id, AnalysisFact.ticker == ticker
        )
        if since:
            statements_q = statements_q.filter(UserStatement.statement_date >= since)
            facts_q = facts_q.filter(AnalysisFact.fact_date >= since)
        statements = statements_q.order_by(UserStatement.statement_date.asc()).all()
        facts = facts_q.order_by(AnalysisFact.fact_date.asc()).all()
        return {
            "current_step": analysis.current_step,
            "statements": [{"step": s.step, "text": s.statement_text} for s in statements if s.step],
            "facts": {f.fact_type: f.fact_value for f in facts},
        }
    finally:
        session.close()


def test_rollback_compatible_old_code_sees_only_the_current_attempt(db, client, claude):
    """(16) Après une tentative A terminée puis une tentative B en cours,
    l'ancien code (lecture via CompanyAnalysis + created_at) retrouve
    exactement l'état de B, sans rien de A."""
    _full_attempt(client, claude, "A")
    _turn(client, claude, "business")
    _turn(client, claude, "moat", question="B business", context="h")

    assert _legacy_v0003_progress(db, USER, TICKER) == api._get_analysis_progress(USER, TICKER)
    assert _legacy_v0003_progress(db, USER, TICKER) == {
        "current_step": "moat",
        "statements": [{"step": "business", "text": "B business"}],
        "facts": {"operating_margin": 0.26, "roe": 0.24, "net_cash": -1.0e9},
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

    _turn(client, claude, "valorisation", question="suite", context="h")
    sessions = {s.id: s for s in _sessions(db)}
    assert set(sessions) == {old_id, recent_id}
    assert sessions[recent_id].current_step == "valorisation"
    assert sessions[old_id].current_step == "moat"
    [st] = _rows(db, UserStatement, statement_text="suite")
    assert st.analysis_session_id == recent_id and st.step == "chiffres"
    assert "[ANALYSIS-SESSION ANOMALY]" in capsys.readouterr().out


def test_other_ticker_and_other_user_are_isolated(db, client, claude):
    """Une session in_progress n'est réutilisée que pour son user+ticker."""
    _turn(client, claude, "business")
    _turn(client, claude, "business", ticker="AAPL")
    sessions = _sessions(db)
    assert sorted(s.ticker for s in sessions) == ["AAPL", TICKER]
    assert len({s.id for s in sessions}) == 2
    assert api._get_analysis_progress("someone-else", TICKER) is None
