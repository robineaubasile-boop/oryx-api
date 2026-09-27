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
    """OBSOLÈTE — non utilisé par l'application depuis T1-C1.

    Ancien état courant (une ligne par user_id+ticker) d'une tentative
    construction_these, remplacé par AnalysisSession. Plus aucun code
    applicatif ne lit ni n'écrit ce modèle.

    Conservé uniquement parce que la table company_analyses existe encore
    physiquement dans le schéma Alembic 0003 : Base.metadata doit y rester
    fidèle. À supprimer avec la table en T1-C2 (DROP TABLE)."""
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
    # T1-B1 : rattachement à une AnalysisSession. NULL = provenance de
    # session non établie (lignes historiques, jamais backfillées).
    analysis_session_id = Column(Uuid, ForeignKey("analysis_sessions.id"), nullable=True)


class AnalysisFact(Base):
    __tablename__ = "analysis_facts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    ticker = Column(String, nullable=False)
    fact_date = Column(DateTime, default=datetime.utcnow)
    fact_type = Column(String, nullable=False)
    fact_value = Column(Float, nullable=True)
    # T1-B1 : rattachement à une AnalysisSession. NULL = provenance de
    # session non établie (lignes historiques, jamais backfillées).
    analysis_session_id = Column(Uuid, ForeignKey("analysis_sessions.id"), nullable=True)


class UserStatement(Base):
    __tablename__ = "user_statements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    ticker = Column(String, nullable=False)
    step = Column(String, nullable=True)
    statement_date = Column(DateTime, default=datetime.utcnow)
    statement_text = Column(String, nullable=False)
    # T1-B1 : rattachement à une AnalysisSession. NULL = provenance de
    # session non établie (lignes historiques, jamais backfillées).
    analysis_session_id = Column(Uuid, ForeignKey("analysis_sessions.id"), nullable=True)


def _utcnow_aware():
    return datetime.now(timezone.utc)


class AnalysisSession(Base):
    """Une tentative distincte d'analyse fondamentale d'une entreprise par
    un utilisateur (T1-A). Plusieurs sessions peuvent exister pour un même
    (user_id, ticker) : pas de UNIQUE(user_id, ticker).

    T1-A (EXPAND) : table créée par la migration 0002_analysis_sessions.
    T1-B2 : identité réelle de toute nouvelle tentative construction_these
    (voir _track_construction_these_progress dans api.py).
    T1-C1 : unique identité d'une tentative ; CompanyAnalysis n'est plus
    utilisé par l'application.

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
