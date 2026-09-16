from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, DateTime, ForeignKey
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
