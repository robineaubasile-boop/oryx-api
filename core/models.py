import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Float, Integer, DateTime, ForeignKey, Uuid, CheckConstraint
from core.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    level = Column(String, nullable=False, default="debutant")
    created_at = Column(DateTime, default=datetime.utcnow)


class PortfolioPosition(Base):
    __tablename__ = "portfolio_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    ticker = Column(String, nullable=False)
    quantity = Column(Float, nullable=False)
    purchase_price = Column(Float, nullable=True)
    target_percent = Column(Float, nullable=True)
    envelope = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CompanyAnalysis(Base):
    """État courant d'une tentative d'analyse construction_these pour un
    (user_id, ticker). Ne représente PAS une session/version d'analyse
    distincte : une nouvelle tentative sur le même ticker remplace la
    précédente dans cette table (voir DELETE /api/user/{user_id}/theses/{ticker}
    qui supprime cette ligne mais laisse AnalysisFact/UserStatement en place).
    created_at marque le début de la tentative EN COURS et sert de filtre
    transitoire pour exclure les AnalysisFact/UserStatement orphelins d'une
    tentative précédemment supprimée (voir _get_analysis_progress dans
    api.py). Décision explicite de Basile (2026-09) : accepter cette dette
    plutôt que de préempter le vrai versionnement d'analyses que Chemin
    Oryx devra introduire (analysis_id / session_id). À remplacer à ce
    moment-là, pas avant."""
    __tablename__ = "company_analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    ticker = Column(String, nullable=False)
    current_step = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class InvestmentThesis(Base):
    __tablename__ = "investment_theses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    ticker = Column(String, nullable=False)
    thesis_text = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class AnalysisFact(Base):
    __tablename__ = "analysis_facts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    ticker = Column(String, nullable=False)
    fact_date = Column(DateTime, default=datetime.utcnow)
    fact_type = Column(String, nullable=False)
    fact_value = Column(Float, nullable=True)


class UserStatement(Base):
    __tablename__ = "user_statements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    ticker = Column(String, nullable=False)
    step = Column(String, nullable=True)
    statement_date = Column(DateTime, default=datetime.utcnow)
    statement_text = Column(String, nullable=False)


def _utcnow_aware():
    return datetime.now(timezone.utc)


class AnalysisSession(Base):
    """Une tentative distincte d'analyse fondamentale d'une entreprise par
    un utilisateur (T1-A). Plusieurs sessions peuvent exister pour un même
    (user_id, ticker) : pas de UNIQUE(user_id, ticker).

    T1-A (EXPAND) : table créée par la migration 0002_analysis_sessions
    mais encore inutilisée par l'application. CompanyAnalysis reste la
    source de vérité de Décrypte tant que T1-B/T1-C ne sont pas faits.

    Conventions des nouvelles tables : id UUID généré par l'application
    (aucun server_default), timestamps TIMESTAMPTZ. Seul status est
    contraint en SQL ; la cohérence status ↔ completed_at et les valeurs
    de current_step relèvent de la couche applicative."""
    __tablename__ = "analysis_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'abandoned')",
            name="ck_analysis_sessions_status",
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    ticker = Column(String, nullable=False)
    status = Column(String, nullable=False)
    current_step = Column(String, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware, onupdate=_utcnow_aware)
