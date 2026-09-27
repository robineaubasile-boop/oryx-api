import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Float, Integer, DateTime, ForeignKey, Uuid, CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
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
    T1-C2 : modèle CompanyAnalysis et table company_analyses supprimés
    (migration 0004_drop_company_analyses).

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


class CognitiveEvent(Base):
    """Capsule cohérente d'un problème cognitif présenté à un utilisateur
    (T2-A, niveau 2 du futur système pédagogique). Un événement peut
    couvrir plusieurs messages/tours : événement != message.

    Couche descriptive/historique : conserve ce que l'utilisateur a
    réellement vu (stimulus_snapshot, jamais reconstruit a posteriori ;
    une donnée source erronée n'invalide pas l'événement) et ce qu'il a
    réellement produit, dans l'ordre (user_work_snapshot, liste de
    productions). Aucune évaluation : ni compréhension, niveau,
    compétence, stage, score, confiance ou progression.

    Lifecycle conceptuel : open -> finalized, ou open -> abandoned. Seuls
    les événements finalized seront évaluables plus tard (T3) ; un
    événement open ne produira jamais d'observation persistante et un
    événement abandoned n'est pas une preuve pédagogique. Après
    finalized/abandoned, l'immutabilité relèvera du service T2-B (aucun
    trigger en base).

    T2-A : table créée par la migration 0005_cognitive_support_traces ;
    ni lue ni écrite par l'application. Seul status est contraint en SQL.
    event_origin (ex. education, coach, decryptage, academy, rallye,
    portfolio) et task_kind (ex. explain_concept, interpret_metric,
    compare_options, apply_formula, identify_risk, build_thesis) sont des
    vocabulaires extensibles, volontairement sans CHECK. Aucun schéma JSON
    interne n'est imposé en base ; JSONB(none_as_null=True) : un None
    Python n'est jamais stocké comme le JSON null (colonne sans défaut :
    NULL SQL, refusé par NOT NULL)."""
    __tablename__ = "cognitive_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'finalized', 'abandoned')",
            name="ck_cognitive_events_status",
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    analysis_session_id = Column(Uuid, ForeignKey("analysis_sessions.id"), nullable=True)
    event_origin = Column(String, nullable=False)
    task_kind = Column(String, nullable=False)
    status = Column(String, nullable=False)
    conversation_key = Column(String, nullable=True)
    stimulus_snapshot = Column(JSONB(none_as_null=True), nullable=False, default=dict)
    user_work_snapshot = Column(JSONB(none_as_null=True), nullable=False, default=list)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware, onupdate=_utcnow_aware)
    closed_at = Column(DateTime(timezone=True), nullable=True)


class SupportTrace(Base):
    """Aide réellement visible fournie par Oryx à l'utilisateur pendant un
    CognitiveEvent (T2-A). SupportTrace != preuve utilisateur : une aide
    reçue ne démontre jamais une compétence.

    support_payload ne contient que ce qui a été effectivement montré à
    l'utilisateur : jamais de chain-of-thought, de raisonnement interne du
    modèle, de prompt système ni de diagnostic non montré. sequence_no
    reconstruit l'ordre des aides au sein d'un événement
    (UNIQUE(cognitive_event_id, sequence_no)). support_kind (ex. rephrase,
    hint, formula, worked_example, explanation, feedback, answer_reveal,
    data_provided) est extensible, volontairement sans CHECK."""
    __tablename__ = "support_traces"
    __table_args__ = (
        UniqueConstraint(
            "cognitive_event_id", "sequence_no",
            name="uq_support_traces_event_sequence",
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    cognitive_event_id = Column(Uuid, ForeignKey("cognitive_events.id"), nullable=False)
    sequence_no = Column(Integer, nullable=False)
    support_kind = Column(String, nullable=False)
    support_payload = Column(JSONB(none_as_null=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
