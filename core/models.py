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
    __tablename__ = "company_analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    ticker = Column(String, nullable=False)
    current_step = Column(String, nullable=False)
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
