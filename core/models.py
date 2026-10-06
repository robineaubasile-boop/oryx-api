import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Float, Integer, SmallInteger, BigInteger, Text, DateTime, ForeignKey, Uuid,
    CheckConstraint, UniqueConstraint, Index, text,
)
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
    (voir apply_construction_these_progress dans core/decryptage_progress.py,
    ex-_track_construction_these_progress de api.py).
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
    NULL SQL, refusé par NOT NULL).

    R1-B (migration 0010_r1b_event_idempotence) : identité idempotente.
    event_dedup_key (UNIQUE uq_cognitive_events_event_dedup_key) est un
    SHA-256 calculé côté serveur à partir de l'identité de segmentation
    (jamais du contenu) ; event_builder_version, admission_version et
    event_schema_version versionnent la politique qui a produit l'événement
    et le format de user_work_snapshot. Les quatre colonnes sont NULL
    uniquement pour les lignes legacy pré-R1-B (jamais backfillées) ; le
    service R1-B les renseigne toujours. task_kind est NULL quand la
    démonstration ne répond à aucune tâche imposée (aucune valeur factice)."""
    __tablename__ = "cognitive_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'finalized', 'abandoned')",
            name="ck_cognitive_events_status",
        ),
        UniqueConstraint("event_dedup_key", name="uq_cognitive_events_event_dedup_key"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    analysis_session_id = Column(Uuid, ForeignKey("analysis_sessions.id"), nullable=True)
    event_origin = Column(String, nullable=False)
    task_kind = Column(String, nullable=True)
    status = Column(String, nullable=False)
    conversation_key = Column(String, nullable=True)
    stimulus_snapshot = Column(JSONB(none_as_null=True), nullable=False, default=dict)
    user_work_snapshot = Column(JSONB(none_as_null=True), nullable=False, default=list)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware, onupdate=_utcnow_aware)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    event_dedup_key = Column(String, nullable=True)
    event_builder_version = Column(String, nullable=True)
    admission_version = Column(String, nullable=True)
    event_schema_version = Column(String, nullable=True)


class ConversationIdentity(Base):
    """Registre minimal d'appartenance d'une conversation produit (R1-B) :
    conversation_key -> exactement un user_id, jamais réattribué. La PK est
    la défense finale sous concurrence (core/interaction_identity.py).

    Ce n'est PAS un système de conversation persistant : ni messages,
    historique, surface, titre, résumé, route, niveau ni lifecycle. Une
    nouvelle conversation n'est jamais un reset pédagogique ; l'historique
    texte conversationnel n'est pas une provenance pédagogique.

    Table créée par la migration 0010_r1b_event_idempotence ; created_at
    généré par l'application (aucun server_default)."""
    __tablename__ = "conversation_identities"

    conversation_key = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


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


class ObservationEvaluationRun(Base):
    """Interprétation versionnée complète d'un CognitiveEvent (T3-A,
    niveau 3 du futur système pédagogique) : une évaluation de l'événement
    sous une combinaison donnée de versions (normalisation, stages locaux,
    taxonomie, mapping de capacités, schéma d'évaluation, évaluateur,
    modèle, prompt). Seuls les événements finalized seront évalués ; cette
    règle relève du service T3-B (aucun trigger en base).

    Le run distingue les cas que l'absence d'observation ne permet pas de
    distinguer seule : aucun run = jamais évalué ; execution_status
    running = évaluation en cours ; completed sans aucune
    PedagogicalObservation = évaluation réussie n'ayant rien observé
    (jamais de fausse observation « vide ») ; completed avec 1..n
    observations ; failed = échec technique (failure_code facultatif).

    La supersession se fait au niveau du run (interpretation_status :
    candidate / active / superseded / obsolete), jamais au niveau de
    l'observation. Un seul run active par event_id, garanti par PostgreSQL
    (index unique partiel uq_observation_evaluation_runs_one_active_event) ;
    candidate, superseded et obsolete peuvent être multiples.
    re_evaluates_run_id est NULL pour un premier run.

    pedagogical_taxonomy_release_id : release de taxonomie T4 sous laquelle
    le run a été évalué. Colonne créée nullable et sans FK par 0006 ; T4-A
    (migration 0007_pedagogical_taxonomy) ajoute la FK vers
    pedagogical_taxonomy_releases.id (NO ACTION) et son index, sans la
    rendre NOT NULL : des runs peuvent exister sans release (historique,
    T7 non branché), jamais backfillés.

    T3-A : table créée par la migration 0006_observation_layer ; ni lue ni
    écrite par l'application (lifecycle et écriture en T3-B). trigger est
    un vocabulaire extensible, volontairement sans CHECK."""
    __tablename__ = "observation_evaluation_runs"
    __table_args__ = (
        CheckConstraint(
            "execution_status IN ('running', 'completed', 'failed')",
            name="ck_observation_evaluation_runs_execution_status",
        ),
        CheckConstraint(
            "interpretation_status IN ('candidate', 'active', 'superseded', 'obsolete')",
            name="ck_observation_evaluation_runs_interpretation_status",
        ),
        UniqueConstraint(
            "evaluation_dedup_key",
            name="uq_observation_evaluation_runs_dedup_key",
        ),
        Index(
            "uq_observation_evaluation_runs_one_active_event",
            "event_id",
            unique=True,
            postgresql_where=text("interpretation_status = 'active'"),
        ),
        Index(
            "ix_observation_evaluation_runs_pedagogical_taxonomy_release_id",
            "pedagogical_taxonomy_release_id",
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id = Column(Uuid, ForeignKey("cognitive_events.id"), nullable=False)
    execution_status = Column(String, nullable=False)
    interpretation_status = Column(String, nullable=False)
    trigger = Column(String, nullable=False)
    re_evaluates_run_id = Column(Uuid, ForeignKey("observation_evaluation_runs.id"), nullable=True)
    evaluation_dedup_key = Column(String, nullable=False)
    normalization_version = Column(String, nullable=False)
    local_stage_version = Column(String, nullable=False)
    # FK ajoutée par T4-A (0007) ; nom explicite : le nom automatique
    # PostgreSQL dépasserait 63 caractères. Reste nullable (voir docstring).
    pedagogical_taxonomy_release_id = Column(
        Uuid,
        ForeignKey("pedagogical_taxonomy_releases.id",
                   name="observation_evaluation_runs_taxonomy_release_id_fkey"),
        nullable=True,
    )
    capability_mapping_version = Column(String, nullable=False)
    evaluation_schema_version = Column(String, nullable=False)
    evaluator_version = Column(String, nullable=False)
    model_id = Column(String, nullable=True)
    prompt_spec_version = Column(String, nullable=True)
    input_fingerprint = Column(String, nullable=False)
    output_fingerprint = Column(String, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    failure_code = Column(String, nullable=True)


class PedagogicalObservation(Base):
    """Unité probante locale et atomique produite par un
    ObservationEvaluationRun (T3-A) : ce que l'utilisateur a effectivement
    démontré sur UNE compétence (competency_code), dans le contexte précis
    du CognitiveEvent évalué par le run. La provenance (event_id) est
    portée par le run, non dupliquée ici.

    Une observation ne représente jamais l'état global de l'utilisateur :
    le niveau longitudinal sera inféré plus tard à partir de nombreuses
    observations. evidence_strength mesure la qualité diagnostique du
    signal, pas un niveau ; une preuve strong peut être supportive ou
    contradictory. Pas de polarité « mixed » : une réponse mixte est
    décomposée en observations atomiques. Plusieurs observations, y
    compris sur la même compétence, peuvent appartenir au même run
    (aucune unicité sur competency_code) ; ordinal ne sert qu'à les
    ordonner (UNIQUE(evaluation_run_id, ordinal) ; valeur de départ et
    absence de trou relèvent du service T3-B).

    local_stage : none / discovery / comprehension / application.
    mastery est volontairement absent : il exige répétition, transfert et
    durée, jamais conclus d'une observation locale. supportive => local_stage
    renseigné et contradiction_scope NULL ; contradictory => local_stage
    NULL et contradiction_scope renseigné
    (ck_pedagogical_observations_polarity_stage_scope).

    support_level décrit l'aide reçue ; ce n'est ni un coefficient ni un
    plafond. Le détail des aides reste dans SupportTrace (non dupliqué).
    residual_cognitive_work décrit ce qui restait réellement à construire
    par l'utilisateur. Colonnes JSONB sans schéma interne imposé en base
    (validation métier en T3-B) ; JSONB(none_as_null=True), sans défaut.

    integrity_status (valid / invalidated) = validité intrinsèque de
    l'observation uniquement ; la supersession est héritée du run (une
    observation d'un run superseded peut rester valid). valid =>
    invalidated_at et invalidation_reason NULL ; invalidated => les deux
    renseignés (ck_pedagogical_observations_integrity_invalidation).

    Aucun score, XP, confiance ni progression. T3-A : table créée par la
    migration 0006_observation_layer ; ni lue ni écrite par l'application.
    task_kind est extensible, volontairement sans CHECK."""
    __tablename__ = "pedagogical_observations"
    __table_args__ = (
        CheckConstraint(
            "competency_code IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', "
            "'C7', 'C8', 'C9', 'C10', 'C11', 'C12')",
            name="ck_pedagogical_observations_competency_code",
        ),
        CheckConstraint(
            "observation_role IN ('primary', 'secondary')",
            name="ck_pedagogical_observations_observation_role",
        ),
        CheckConstraint(
            "elicitation_mode IN ('prompted', 'spontaneous')",
            name="ck_pedagogical_observations_elicitation_mode",
        ),
        CheckConstraint(
            "support_level IN ('none', 'hinted', 'guided', 'answer_given')",
            name="ck_pedagogical_observations_support_level",
        ),
        CheckConstraint(
            "polarity IN ('supportive', 'contradictory')",
            name="ck_pedagogical_observations_polarity",
        ),
        CheckConstraint(
            "evidence_strength IN ('weak', 'medium', 'strong')",
            name="ck_pedagogical_observations_evidence_strength",
        ),
        CheckConstraint(
            "local_stage IN ('none', 'discovery', 'comprehension', 'application')",
            name="ck_pedagogical_observations_local_stage",
        ),
        CheckConstraint(
            "contradiction_scope IN ('recognition', 'comprehension', 'application', 'undetermined')",
            name="ck_pedagogical_observations_contradiction_scope",
        ),
        CheckConstraint(
            "error_type IN ('conceptual', 'procedural', 'execution', 'factual_premise')",
            name="ck_pedagogical_observations_error_type",
        ),
        CheckConstraint(
            "capability_localization IN ('localized', 'competency_only')",
            name="ck_pedagogical_observations_capability_localization",
        ),
        CheckConstraint(
            "integrity_status IN ('valid', 'invalidated')",
            name="ck_pedagogical_observations_integrity_status",
        ),
        CheckConstraint(
            "(polarity = 'supportive' AND local_stage IS NOT NULL AND contradiction_scope IS NULL) "
            "OR (polarity = 'contradictory' AND local_stage IS NULL AND contradiction_scope IS NOT NULL)",
            name="ck_pedagogical_observations_polarity_stage_scope",
        ),
        CheckConstraint(
            "(integrity_status = 'valid' AND invalidated_at IS NULL AND invalidation_reason IS NULL) "
            "OR (integrity_status = 'invalidated' AND invalidated_at IS NOT NULL "
            "AND invalidation_reason IS NOT NULL)",
            name="ck_pedagogical_observations_integrity_invalidation",
        ),
        UniqueConstraint(
            "evaluation_run_id", "ordinal",
            name="uq_pedagogical_observations_run_ordinal",
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    evaluation_run_id = Column(Uuid, ForeignKey("observation_evaluation_runs.id"), nullable=False)
    ordinal = Column(SmallInteger, nullable=False)
    competency_code = Column(String, nullable=False)
    observation_role = Column(String, nullable=False)
    task_kind = Column(String, nullable=True)
    primary_user_action = Column(JSONB(none_as_null=True), nullable=False)
    contributive_user_actions = Column(JSONB(none_as_null=True), nullable=False)
    elicitation_mode = Column(String, nullable=False)
    support_level = Column(String, nullable=False)
    source_contribution_refs = Column(JSONB(none_as_null=True), nullable=False)
    residual_cognitive_work = Column(JSONB(none_as_null=True), nullable=False)
    polarity = Column(String, nullable=False)
    evidence_strength = Column(String, nullable=False)
    local_stage = Column(String, nullable=True)
    contradiction_scope = Column(String, nullable=True)
    error_type = Column(String, nullable=True)
    observation_text = Column(Text, nullable=False)
    capability_localization = Column(String, nullable=False)
    integrity_status = Column(String, nullable=False)
    invalidated_at = Column(DateTime(timezone=True), nullable=True)
    invalidation_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class PedagogicalTaxonomyRelease(Base):
    """Version de la taxonomie pédagogique Oryx ENTIÈRE (T4-A) : définitions
    C1-C12, capacités noyau, frontières et mapping guidance. version_key
    est l'identité stable de la release ; spec_fingerprint identifie son
    contenu canonique (calculé plus tard, T4-B/T4-C, jamais ici).

    status : candidate / active / retired. Au plus une release active,
    garanti par PostgreSQL (index unique partiel
    uq_pedagogical_taxonomy_releases_one_active) ; les transitions
    relèvent de T4-B. Aucune release n'est créée par la migration.

    T4-A : table créée par la migration 0007_pedagogical_taxonomy ; ni lue
    ni écrite par l'application."""
    __tablename__ = "pedagogical_taxonomy_releases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate', 'active', 'retired')",
            name="ck_pedagogical_taxonomy_releases_status",
        ),
        UniqueConstraint(
            "version_key",
            name="uq_pedagogical_taxonomy_releases_version_key",
        ),
        UniqueConstraint(
            "spec_fingerprint",
            name="uq_pedagogical_taxonomy_releases_spec_fingerprint",
        ),
        Index(
            "uq_pedagogical_taxonomy_releases_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    version_key = Column(String, nullable=False)
    status = Column(String, nullable=False)
    spec_fingerprint = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    activated_at = Column(DateTime(timezone=True), nullable=True)


class CoreCapabilityDefinition(Base):
    """UNE signification d'une capacité noyau (T4-A) : dimension interne
    d'une compétence C1-C12 qui localise le PÉRIMÈTRE d'une preuve T3.
    Observation = force et profondeur de la preuve ; capacité = périmètre.
    Une capacité n'est ni une preuve, ni une sous-compétence utilisateur :
    ni niveau, ni score, ni confiance, ni statut acquis, ni progression.

    Même id = même sens. Un changement de sens crée une nouvelle ligne
    (même capability_code, semantic_revision suivante, nouvel UUID) ; une
    définition n'est jamais réécrite (immutabilité applicative en T4-B,
    aucun trigger). UNIQUE(capability_code, semantic_revision),
    semantic_revision >= 1.

    capability_code : exactement les 45 codes figés ; competency_code :
    C1..C12 ; cohérence code / compétence garantie en base
    (ck_core_capability_definitions_code_competency : C7_A => C7).
    mapping_guidance : JSONB sans schéma interne imposé en base (validation
    métier en T4-B/T4-C) ; JSONB(none_as_null=True), sans défaut.

    T4-A : table créée par la migration 0007_pedagogical_taxonomy ; aucune
    capacité seedée ; ni lue ni écrite par l'application."""
    __tablename__ = "core_capability_definitions"
    __table_args__ = (
        CheckConstraint(
            "capability_code IN ("
            "'C1_A', 'C1_B', 'C1_C', "
            "'C2_A', 'C2_B', 'C2_C', "
            "'C3_A', 'C3_B', 'C3_C', "
            "'C4_A', 'C4_B', 'C4_C', 'C4_D', "
            "'C5_A', 'C5_B', 'C5_C', 'C5_D', "
            "'C6_A', 'C6_B', 'C6_C', 'C6_D', "
            "'C7_A', 'C7_B', 'C7_C', 'C7_D', "
            "'C8_A', 'C8_B', 'C8_C', 'C8_D', "
            "'C9_A', 'C9_B', 'C9_C', 'C9_D', "
            "'C10_A', 'C10_B', 'C10_C', 'C10_D', "
            "'C11_A', 'C11_B', 'C11_C', 'C11_D', "
            "'C12_A', 'C12_B', 'C12_C', 'C12_D')",
            name="ck_core_capability_definitions_capability_code",
        ),
        CheckConstraint(
            "competency_code IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', "
            "'C7', 'C8', 'C9', 'C10', 'C11', 'C12')",
            name="ck_core_capability_definitions_competency_code",
        ),
        CheckConstraint(
            "split_part(capability_code, '_', 1) = competency_code",
            name="ck_core_capability_definitions_code_competency",
        ),
        CheckConstraint(
            "semantic_revision >= 1",
            name="ck_core_capability_definitions_semantic_revision",
        ),
        UniqueConstraint(
            "capability_code", "semantic_revision",
            name="uq_core_capability_definitions_code_revision",
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    capability_code = Column(String, nullable=False)
    semantic_revision = Column(Integer, nullable=False)
    competency_code = Column(String, nullable=False)
    label = Column(String, nullable=False)
    definition = Column(Text, nullable=False)
    mapping_guidance = Column(JSONB(none_as_null=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class CapabilityTaxonomyMembership(Base):
    """« Cette définition sémantique précise appartient à cette release »
    (T4-A), et rien d'autre : ni validation utilisateur, ni progression,
    ni poids, ni ordre, ni priorité. Une définition inchangée est réutilisée
    par plusieurs releases (même capability_definition_id).
    UNIQUE(taxonomy_release_id, capability_definition_id).

    Limite volontaire : « jamais deux révisions du même capability_code
    dans une release » traverse deux tables ; il n'est garanti ni par
    trigger ni par dénormalisation, mais par T4-B (transactionnel).

    T4-A : table créée par la migration 0007_pedagogical_taxonomy ; ni lue
    ni écrite par l'application."""
    __tablename__ = "capability_taxonomy_memberships"
    __table_args__ = (
        UniqueConstraint(
            "taxonomy_release_id", "capability_definition_id",
            name="uq_capability_taxonomy_memberships_release_definition",
        ),
        Index(
            "ix_capability_taxonomy_memberships_capability_definition_id",
            "capability_definition_id",
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    taxonomy_release_id = Column(Uuid, ForeignKey("pedagogical_taxonomy_releases.id"), nullable=False)
    capability_definition_id = Column(Uuid, ForeignKey("core_capability_definitions.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class ObservationCapability(Base):
    """Localisation sémantique d'une PedagogicalObservation existante sur
    une capacité d'une release (T4-A). Une ligne ne crée JAMAIS une
    nouvelle preuve : plusieurs lignes pour une observation (C7_A et C7_B)
    restent UNE observation. Aucun stade, score, poids, confiance, force
    de preuve, polarité ni statut : tout cela appartient à l'observation
    ou n'existe pas. PK composite (observation_id,
    capability_membership_id), sans id artificiel.

    Limites volontaires (garanties par T4-B, ni trigger ni
    dénormalisation) : une observation Cn n'est localisée que sur des
    capacités Cn_* ; localized => au moins une ligne, competency_only =>
    aucune.

    T4-A : table créée par la migration 0007_pedagogical_taxonomy ; ni lue
    ni écrite par l'application."""
    __tablename__ = "observation_capabilities"
    __table_args__ = (
        Index(
            "ix_observation_capabilities_capability_membership_id",
            "capability_membership_id",
        ),
    )

    observation_id = Column(Uuid, ForeignKey("pedagogical_observations.id"), primary_key=True)
    capability_membership_id = Column(
        Uuid, ForeignKey("capability_taxonomy_memberships.id"), primary_key=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class LongitudinalAssessmentRun(Base):
    """Version précise du dossier d'UNE compétence (competency_code) d'UN
    utilisateur (user_id) (T5-A, niveau 5 : relations longitudinales) : ce
    run dit quelle version du dossier Cx a été examinée, sous quelle
    release de taxonomie (pedagogical_taxonomy_release_id, NOT NULL) et
    quelles versions de règles (dependency_version, transfer_version,
    revalidation_version, relation_schema_version ; aucun modèle, prompt ni
    évaluateur, aucun output_fingerprint, aucun run prédécesseur).

    OBSERVATION = unité de preuve ; CAPABILITY = périmètre de la preuve ;
    RELATION T5 = structure historique entre preuves. Ce run ne crée
    aucune preuve et ne porte ni stade, ni score, ni confiance, ni
    progression, ni maîtrise, ni besoin de revalidation (T6). Les profils
    coverage / variety / independence / consistency / freshness /
    durability sont reconstruits, jamais stockés.

    Les observations examinées sont listées explicitement par
    LongitudinalAssessmentInput. Aucun run = dossier jamais examiné ; un
    run sans entrée = dossier vide examiné (représentable).

    execution_status : running / completed / failed ;
    interpretation_status : candidate / active / superseded / obsolete. Au
    plus un run active par (user_id, competency_code), garanti par
    PostgreSQL (index unique partiel
    uq_longitudinal_assessment_runs_one_active_user_competency) ; les
    autres statuts restent multiples. Les transitions, la cohérence
    status <-> completed_at et la re-vérification de input_fingerprint à
    l'activation relèvent de T5-B (aucun trigger, aucun CHECK croisé).
    input_fingerprint est stocké seulement (algorithme défini en T5-B).
    trigger est un vocabulaire ouvert, volontairement sans CHECK.

    T5-A : table créée par la migration 0008_longitudinal_relations ; ni
    lue ni écrite par l'application (aucun service, aucune route)."""
    __tablename__ = "longitudinal_assessment_runs"
    __table_args__ = (
        CheckConstraint(
            "competency_code IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', "
            "'C7', 'C8', 'C9', 'C10', 'C11', 'C12')",
            name="ck_longitudinal_assessment_runs_competency_code",
        ),
        CheckConstraint(
            "execution_status IN ('running', 'completed', 'failed')",
            name="ck_longitudinal_assessment_runs_execution_status",
        ),
        CheckConstraint(
            "interpretation_status IN ('candidate', 'active', 'superseded', 'obsolete')",
            name="ck_longitudinal_assessment_runs_interpretation_status",
        ),
        UniqueConstraint(
            "assessment_dedup_key",
            name="uq_longitudinal_assessment_runs_dedup_key",
        ),
        Index(
            "uq_longitudinal_assessment_runs_one_active_user_competency",
            "user_id", "competency_code",
            unique=True,
            postgresql_where=text("interpretation_status = 'active'"),
        ),
        Index("ix_longitudinal_assessment_runs_user_id", "user_id"),
        Index("ix_longitudinal_assessment_runs_taxonomy_release_id", "pedagogical_taxonomy_release_id"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    competency_code = Column(String, nullable=False)
    execution_status = Column(String, nullable=False)
    interpretation_status = Column(String, nullable=False)
    trigger = Column(String, nullable=False)
    # Nom explicite : le nom automatique PostgreSQL dépasserait 63 caractères.
    pedagogical_taxonomy_release_id = Column(
        Uuid,
        ForeignKey("pedagogical_taxonomy_releases.id",
                   name="longitudinal_assessment_runs_taxonomy_release_id_fkey"),
        nullable=False,
    )
    dependency_version = Column(String, nullable=False)
    transfer_version = Column(String, nullable=False)
    revalidation_version = Column(String, nullable=False)
    relation_schema_version = Column(String, nullable=False)
    input_fingerprint = Column(String, nullable=False)
    assessment_dedup_key = Column(String, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    failure_code = Column(String, nullable=True)


class LongitudinalAssessmentInput(Base):
    """Snapshot EXPLICITE des observations réellement examinées par UN
    LongitudinalAssessmentRun (T5-A). Une ligne = « cette observation
    faisait partie du dossier examiné par ce run », rien d'autre : ni
    poids, ni score, ni horodatage, ni nouvelle entité pédagogique. PK
    composite (run_id, observation_id), sans id artificiel ; une même
    observation peut entrer dans plusieurs runs.

    L'absence ultérieure de relation entre deux observations du snapshot
    ne signifie jamais automatiquement leur indépendance. Aucun minimum
    d'entrées n'est imposé (dossier vide représentable). Pas de table
    active_history : l'historique actif est reconstruit (T2/T3/T4 + ce
    snapshot), jamais dupliqué.

    Limites volontaires (T5-B, ni trigger ni dénormalisation) : que
    l'observation appartienne au user et à la compétence du run, provienne
    d'un run T3 active completed, soit valid et compatible avec la release
    du run.

    T5-A : table créée par la migration 0008_longitudinal_relations ; ni
    lue ni écrite par l'application."""
    __tablename__ = "longitudinal_assessment_inputs"
    __table_args__ = (
        Index("ix_longitudinal_assessment_inputs_observation_id", "observation_id"),
    )

    run_id = Column(Uuid, ForeignKey("longitudinal_assessment_runs.id"), primary_key=True)
    observation_id = Column(Uuid, ForeignKey("pedagogical_observations.id"), primary_key=True)


class ObservationDependency(Base):
    """Dépendance cognitive INTER-épisodes identifiée par un run
    longitudinal (T5-A) : l'observation cible (target_observation_id)
    dépend d'une source antérieure, soit une autre observation
    (source_kind = 'observation'), soit une aide réellement montrée
    (source_kind = 'support_trace'). Exactement une source, cohérente avec
    source_kind (ck_observation_dependencies_source_xor) ; une observation
    ne dépend jamais d'elle-même
    (ck_observation_dependencies_no_self_dependency).

    Une dépendance n'est JAMAIS une preuve supplémentaire, ni un
    coefficient : pas de score, de poids ni de score d'indépendance.
    dependency_type : dependent / partially_dependent uniquement.
    « independent » n'existe volontairement pas : l'absence d'arête dans un
    dossier examiné signifie seulement qu'aucune dépendance n'a été
    identifiée ; l'indépendance sera reconstruite (snapshot, événements,
    aides, relations, contexte). dependency_basis (JSONB sans schéma en
    base, validé en T5-B) dit POURQUOI la dépendance a été identifiée.
    scope_mode (whole_observation / localized / competency_only) et
    DependencyCapability décrivent le périmètre ; scope_fingerprint est
    stocké seulement (calcul en T5-B). Aucune transitivité (A -> B et
    B -> C ne génèrent jamais A -> C).

    Le support immédiat du MÊME CognitiveEvent reste décrit par
    SupportTrace, support_level et residual_cognitive_work (non doublé).
    Limites volontaires (T5-B) : dépendance inter-événements, source et
    cible dans le snapshot, support_trace du bon user / contexte causal,
    périmètre ⊆ cible, memberships de la release du run, incompatibilité
    avec un transfert autonome sur le même scope. event_id n'est pas
    dupliqué ici (provenance : observation -> run T3 -> événement).

    T5-A : table créée par la migration 0008_longitudinal_relations ; ni
    lue ni écrite par l'application."""
    __tablename__ = "observation_dependencies"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('observation', 'support_trace')",
            name="ck_observation_dependencies_source_kind",
        ),
        CheckConstraint(
            "(source_kind = 'observation' AND source_observation_id IS NOT NULL "
            "AND source_support_trace_id IS NULL) "
            "OR (source_kind = 'support_trace' AND source_observation_id IS NULL "
            "AND source_support_trace_id IS NOT NULL)",
            name="ck_observation_dependencies_source_xor",
        ),
        CheckConstraint(
            "source_observation_id IS NULL OR source_observation_id <> target_observation_id",
            name="ck_observation_dependencies_no_self_dependency",
        ),
        CheckConstraint(
            "dependency_type IN ('dependent', 'partially_dependent')",
            name="ck_observation_dependencies_dependency_type",
        ),
        CheckConstraint(
            "scope_mode IN ('whole_observation', 'localized', 'competency_only')",
            name="ck_observation_dependencies_scope_mode",
        ),
        Index("ix_observation_dependencies_run_id", "run_id"),
        Index("ix_observation_dependencies_target_observation_id", "target_observation_id"),
        Index("ix_observation_dependencies_source_observation_id", "source_observation_id"),
        Index("ix_observation_dependencies_source_support_trace_id", "source_support_trace_id"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id = Column(Uuid, ForeignKey("longitudinal_assessment_runs.id"), nullable=False)
    target_observation_id = Column(Uuid, ForeignKey("pedagogical_observations.id"), nullable=False)
    source_kind = Column(String, nullable=False)
    source_observation_id = Column(Uuid, ForeignKey("pedagogical_observations.id"), nullable=True)
    source_support_trace_id = Column(Uuid, ForeignKey("support_traces.id"), nullable=True)
    dependency_type = Column(String, nullable=False)
    scope_mode = Column(String, nullable=False)
    dependency_basis = Column(JSONB(none_as_null=True), nullable=False)
    scope_fingerprint = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class DependencyCapability(Base):
    """Périmètre sémantique d'une ObservationDependency (T5-A), et rien
    d'autre : une ligne localise l'arête sur un membership de taxonomie ;
    elle ne crée aucune preuve et ne porte ni score, ni poids, ni stade,
    ni horodatage (la provenance temporelle appartient à l'arête parente).
    PK composite (dependency_id, capability_membership_id).

    Limites volontaires (T5-B) : membership de la release du run, périmètre
    ⊆ celui de l'observation cible, localized => au moins une ligne,
    competency_only => aucune.

    T5-A : table créée par la migration 0008_longitudinal_relations ; ni
    lue ni écrite par l'application."""
    __tablename__ = "dependency_capabilities"
    __table_args__ = (
        Index("ix_dependency_capabilities_capability_membership_id", "capability_membership_id"),
    )

    dependency_id = Column(Uuid, ForeignKey("observation_dependencies.id"), primary_key=True)
    capability_membership_id = Column(
        Uuid, ForeignKey("capability_taxonomy_memberships.id"), primary_key=True,
    )


class ObservationTransfer(Base):
    """Transfert effectivement démontré, identifié par un run longitudinal
    (T5-A) : DIRECTIONNEL (source_observation_id -> target_observation_id),
    local et NON TRANSITIF (A -> B et B -> C ne génèrent jamais A -> C ;
    aucune closure). source <> cible
    (ck_observation_transfers_distinct_observations). Un transfert n'est
    jamais une preuve supplémentaire ni un poids.

    Sémantique (validée en T5-B, jamais par trigger) : événements
    cognitifs distincts ; source = démonstration supportive ; cible
    supportive, au moins Application sur le scope concerné ; contexte
    cognitivement différent ; adaptation attribuable à l'utilisateur.
    Changer uniquement le ticker, la date, la surface ou le nom de société
    ne suffit jamais. Une dépendance directe forte et un transfert
    autonome sont incompatibles sur un même scope (T5-B).

    transfer_basis (JSONB sans schéma en base) dit pourquoi ; scope_mode,
    TransferCapability et scope_fingerprint décrivent le périmètre (⊆
    intersection source-cible, memberships de la release du run : T5-B).

    T5-A : table créée par la migration 0008_longitudinal_relations ; ni
    lue ni écrite par l'application."""
    __tablename__ = "observation_transfers"
    __table_args__ = (
        CheckConstraint(
            "source_observation_id <> target_observation_id",
            name="ck_observation_transfers_distinct_observations",
        ),
        CheckConstraint(
            "scope_mode IN ('whole_observation', 'localized', 'competency_only')",
            name="ck_observation_transfers_scope_mode",
        ),
        Index("ix_observation_transfers_run_id", "run_id"),
        Index("ix_observation_transfers_source_observation_id", "source_observation_id"),
        Index("ix_observation_transfers_target_observation_id", "target_observation_id"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id = Column(Uuid, ForeignKey("longitudinal_assessment_runs.id"), nullable=False)
    source_observation_id = Column(Uuid, ForeignKey("pedagogical_observations.id"), nullable=False)
    target_observation_id = Column(Uuid, ForeignKey("pedagogical_observations.id"), nullable=False)
    scope_mode = Column(String, nullable=False)
    transfer_basis = Column(JSONB(none_as_null=True), nullable=False)
    scope_fingerprint = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class TransferCapability(Base):
    """Périmètre sémantique d'un ObservationTransfer (T5-A), et rien
    d'autre : ni preuve, ni score, ni poids, ni stade, ni horodatage. PK
    composite (transfer_id, capability_membership_id).

    Limites volontaires (T5-B) : périmètre ⊆ intersection(source, cible),
    membership de la release du run, localized => au moins une ligne,
    competency_only => aucune.

    T5-A : table créée par la migration 0008_longitudinal_relations ; ni
    lue ni écrite par l'application."""
    __tablename__ = "transfer_capabilities"
    __table_args__ = (
        Index("ix_transfer_capabilities_capability_membership_id", "capability_membership_id"),
    )

    transfer_id = Column(Uuid, ForeignKey("observation_transfers.id"), primary_key=True)
    capability_membership_id = Column(
        Uuid, ForeignKey("capability_taxonomy_memberships.id"), primary_key=True,
    )


class ObservationRevalidation(Base):
    """Fait HISTORIQUE identifié par un run longitudinal (T5-A) : une
    nouvelle démonstration supportive (target_supportive_observation_id)
    réexamine réellement un mécanisme auparavant fragilisé par une
    observation contradictory (source_contradiction_observation_id).
    Directionnelle, non transitive ; source <> cible
    (ck_observation_revalidations_distinct_observations).

    Ce n'est PAS « il faudra revalider dans le futur » : ce besoin est une
    décision T6. La contradiction historique reste conservée ; la
    revalidation ne crée aucune preuve supplémentaire.

    Sémantique (validée en T5-B, jamais par trigger) : source polarity =
    contradictory, cible polarity = supportive, événements distincts,
    nouvelle démonstration réellement diagnostique du même mécanisme ; une
    répétition immédiate après correction Oryx n'est PAS une revalidation.
    revalidation_basis (JSONB sans schéma en base) dit pourquoi ;
    scope_mode, RevalidationCapability et scope_fingerprint décrivent le
    périmètre (⊆ intersection source-cible : T5-B).

    Noms explicites des deux FK d'observation : les noms automatiques
    PostgreSQL atteindraient ou dépasseraient 63 caractères.

    T5-A : table créée par la migration 0008_longitudinal_relations ; ni
    lue ni écrite par l'application."""
    __tablename__ = "observation_revalidations"
    __table_args__ = (
        CheckConstraint(
            "source_contradiction_observation_id <> target_supportive_observation_id",
            name="ck_observation_revalidations_distinct_observations",
        ),
        CheckConstraint(
            "scope_mode IN ('whole_observation', 'localized', 'competency_only')",
            name="ck_observation_revalidations_scope_mode",
        ),
        Index("ix_observation_revalidations_run_id", "run_id"),
        Index("ix_observation_revalidations_source_contradiction", "source_contradiction_observation_id"),
        Index("ix_observation_revalidations_target_supportive", "target_supportive_observation_id"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id = Column(Uuid, ForeignKey("longitudinal_assessment_runs.id"), nullable=False)
    source_contradiction_observation_id = Column(
        Uuid,
        ForeignKey("pedagogical_observations.id", name="observation_revalidations_source_contradiction_fkey"),
        nullable=False,
    )
    target_supportive_observation_id = Column(
        Uuid,
        ForeignKey("pedagogical_observations.id", name="observation_revalidations_target_supportive_fkey"),
        nullable=False,
    )
    scope_mode = Column(String, nullable=False)
    revalidation_basis = Column(JSONB(none_as_null=True), nullable=False)
    scope_fingerprint = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class RevalidationCapability(Base):
    """Périmètre sémantique d'une ObservationRevalidation (T5-A), et rien
    d'autre : ni preuve, ni score, ni poids, ni stade, ni horodatage. PK
    composite (revalidation_id, capability_membership_id).

    Limites volontaires (T5-B) : périmètre ⊆ intersection(source
    contradiction, cible supportive), membership de la release du run,
    localized => au moins une ligne, competency_only => aucune.

    T5-A : table créée par la migration 0008_longitudinal_relations ; ni
    lue ni écrite par l'application."""
    __tablename__ = "revalidation_capabilities"
    __table_args__ = (
        Index("ix_revalidation_capabilities_capability_membership_id", "capability_membership_id"),
    )

    revalidation_id = Column(Uuid, ForeignKey("observation_revalidations.id"), primary_key=True)
    capability_membership_id = Column(
        Uuid, ForeignKey("capability_taxonomy_memberships.id"), primary_key=True,
    )


# --- Niveau 6 : inférence de l'état C1-C12 (T6-A) -----------------------------

_COMPETENCY_CODES_SQL = ("('C1', 'C2', 'C3', 'C4', 'C5', 'C6', "
                         "'C7', 'C8', 'C9', 'C10', 'C11', 'C12')")
# non_etabli = conclusion pédagogique réelle (« dossier interprété sans
# prétention positive établie »), jamais un placeholder ni une stage claim.
_CURRENT_STAGES_SQL = "('non_etabli', 'discovery', 'comprehension', 'application', 'mastery')"
_CLAIM_STAGES_SQL = "('discovery', 'comprehension', 'application', 'mastery')"


class CompetencyInferenceRun(Base):
    """Interprétation VERSIONNÉE du dossier longitudinal d'UNE compétence
    (competency_code) d'UN utilisateur (user_id) (T6-A, niveau 6 :
    inférence de l'état C1-C12). État DÉRIVÉ, jamais preuve : ni le run, ni
    ses stage claims, tensions ou basis refs, ni le cache
    UserCompetencyState ne deviennent une nouvelle preuve utilisateur ; une
    inférence ancienne reste auditable mais ne remonte jamais dans la
    chaîne comme preuve.

    Chaîne : PedagogicalObservation -> périmètre T4 -> dossier / relations
    T5 -> inférence T6. Le run référence exactement UN
    longitudinal_assessment_run_id (le snapshot T5 réellement interprété) ;
    la release de taxonomie est celle de ce run T5 (non dupliquée ici).
    predecessor_inference_run_id : run T6 précédent (NULL pour une première
    inférence), jamais le run lui-même
    (ck_competency_inference_runs_no_self_predecessor).

    Sorties (previous_stage, current_stage, transition, transition_cause,
    tension_state, unresolved_revision_context, validation_needs,
    state_decision_summary, output_fingerprint) : NULLABLE, car un run
    running représente une inférence en cours ; non_etabli n'est JAMAIS un
    placeholder de calcul. Stage et tension sont orthogonaux (pas de faux
    stade « application_uncertain »). Aucun ordre numérique de stade, aucun
    score, aucune confiance numérique, aucun niveau global utilisateur, aucun
    active_validation_plan (validation_needs = besoin latent dérivé).

    Au plus un run active par (user_id, competency_code), garanti par
    PostgreSQL (index unique partiel
    uq_competency_inference_runs_one_active_user_competency). Invariants de
    complétion, transitions, cohérence du predecessor (même user, même
    compétence, bon run précédent) et du run T5 parent : T6-B (aucun
    trigger, aucun CHECK croisé de lifecycle). trigger est un vocabulaire
    ouvert, volontairement sans CHECK.

    T6-A : table créée par la migration 0009_competency_inference_state ;
    ni lue ni écrite par l'application (aucun service, aucune route)."""
    __tablename__ = "competency_inference_runs"
    __table_args__ = (
        CheckConstraint(
            f"competency_code IN {_COMPETENCY_CODES_SQL}",
            name="ck_competency_inference_runs_competency_code",
        ),
        CheckConstraint(
            "execution_status IN ('running', 'completed', 'failed')",
            name="ck_competency_inference_runs_execution_status",
        ),
        CheckConstraint(
            "interpretation_status IN ('candidate', 'active', 'superseded', 'obsolete')",
            name="ck_competency_inference_runs_interpretation_status",
        ),
        CheckConstraint(
            f"previous_stage IN {_CURRENT_STAGES_SQL}",
            name="ck_competency_inference_runs_previous_stage",
        ),
        CheckConstraint(
            f"current_stage IN {_CURRENT_STAGES_SQL}",
            name="ck_competency_inference_runs_current_stage",
        ),
        CheckConstraint(
            "transition IN ('maintained', 'upgraded', 'revised_down')",
            name="ck_competency_inference_runs_transition",
        ),
        CheckConstraint(
            "transition_cause IN ('new_user_evidence', 'evidence_integrity_change', "
            "'pedagogical_reinterpretation')",
            name="ck_competency_inference_runs_transition_cause",
        ),
        CheckConstraint(
            "tension_state IN ('none', 'open')",
            name="ck_competency_inference_runs_tension_state",
        ),
        CheckConstraint(
            "predecessor_inference_run_id IS NULL OR predecessor_inference_run_id <> id",
            name="ck_competency_inference_runs_no_self_predecessor",
        ),
        UniqueConstraint(
            "inference_dedup_key",
            name="uq_competency_inference_runs_dedup_key",
        ),
        Index(
            "uq_competency_inference_runs_one_active_user_competency",
            "user_id", "competency_code",
            unique=True,
            postgresql_where=text("interpretation_status = 'active'"),
        ),
        Index("ix_competency_inference_runs_user_id", "user_id"),
        Index("ix_competency_inference_runs_longitudinal_assessment_run_id", "longitudinal_assessment_run_id"),
        Index("ix_competency_inference_runs_predecessor_inference_run_id", "predecessor_inference_run_id"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    competency_code = Column(String, nullable=False)
    longitudinal_assessment_run_id = Column(Uuid, ForeignKey("longitudinal_assessment_runs.id"), nullable=False)
    predecessor_inference_run_id = Column(Uuid, ForeignKey("competency_inference_runs.id"), nullable=True)
    execution_status = Column(String, nullable=False)
    interpretation_status = Column(String, nullable=False)
    trigger = Column(String, nullable=False)
    previous_stage = Column(String, nullable=True)
    current_stage = Column(String, nullable=True)
    transition = Column(String, nullable=True)
    transition_cause = Column(String, nullable=True)
    tension_state = Column(String, nullable=True)
    unresolved_revision_context = Column(JSONB(none_as_null=True), nullable=True)
    validation_needs = Column(JSONB(none_as_null=True), nullable=True)
    state_decision_summary = Column(Text, nullable=True)
    positive_basis_version = Column(String, nullable=False)
    confidence_profile_version = Column(String, nullable=False)
    state_decision_version = Column(String, nullable=False)
    validation_version = Column(String, nullable=False)
    inference_schema_version = Column(String, nullable=False)
    evaluator_version = Column(String, nullable=False)
    model_id = Column(String, nullable=True)
    prompt_spec_version = Column(String, nullable=True)
    input_fingerprint = Column(String, nullable=False)
    output_fingerprint = Column(String, nullable=True)
    inference_dedup_key = Column(String, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)
    failure_code = Column(String, nullable=True)


class CompetencyStageClaim(Base):
    """Prétention POSITIVE sur UN stade (discovery / comprehension /
    application / mastery) au sein d'un CompetencyInferenceRun (T6-A). Une
    stage claim est distincte du current_stage du run : le current_stage est
    une décision d'état ; la claim dit seulement si une base positive est
    établie pour ce stade. non_etabli n'est PAS une stage claim (absence de
    prétention positive établie). UNIQUE(inference_run_id, stage).

    positive_basis_status (established / not_established) et basis_mode
    (direct / implied_by_higher_claim / none) restent structurellement
    cohérents (ck_competency_stage_claims_basis_status_mode) :
    not_established => none ; established => direct ou
    implied_by_higher_claim (Application directe peut impliquer
    Compréhension et Discovery sans recopier les preuves). La base ne
    décide jamais quand un mode s'applique.

    Un run complet contiendra exactement quatre claims ; cette complétude,
    le plus haut stade positivement établi (DÉRIVÉ, jamais persisté) et
    l'absence de profil pour une claim not_established relèvent de
    T6-B/T6-C (aucun trigger). confidence_profile et mastery_assessment :
    JSONB nullable sans schéma interne en base (T6-C) ; jamais un score, un
    niveau low / medium / high, un pourcentage, un x/5 ni un booléen global.

    T6-A : table créée par la migration 0009_competency_inference_state ;
    ni lue ni écrite par l'application."""
    __tablename__ = "competency_stage_claims"
    __table_args__ = (
        CheckConstraint(
            f"stage IN {_CLAIM_STAGES_SQL}",
            name="ck_competency_stage_claims_stage",
        ),
        CheckConstraint(
            "positive_basis_status IN ('established', 'not_established')",
            name="ck_competency_stage_claims_positive_basis_status",
        ),
        CheckConstraint(
            "basis_mode IN ('direct', 'implied_by_higher_claim', 'none')",
            name="ck_competency_stage_claims_basis_mode",
        ),
        CheckConstraint(
            "(positive_basis_status = 'not_established' AND basis_mode = 'none') "
            "OR (positive_basis_status = 'established' "
            "AND basis_mode IN ('direct', 'implied_by_higher_claim'))",
            name="ck_competency_stage_claims_basis_status_mode",
        ),
        UniqueConstraint(
            "inference_run_id", "stage",
            name="uq_competency_stage_claims_run_stage",
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    inference_run_id = Column(Uuid, ForeignKey("competency_inference_runs.id"), nullable=False)
    stage = Column(String, nullable=False)
    positive_basis_status = Column(String, nullable=False)
    basis_mode = Column(String, nullable=False)
    basis_summary = Column(Text, nullable=True)
    scope_summary = Column(Text, nullable=True)
    confidence_profile = Column(JSONB(none_as_null=True), nullable=True)
    mastery_assessment = Column(JSONB(none_as_null=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class CompetencyInferenceTension(Base):
    """Tension d'un CompetencyInferenceRun (T6-A) : une prétention positive
    défendable (fragilized_stage) est fragilisée sur un périmètre donné.
    Une tension n'est ni un stade, ni une pénalité, ni un score : stage et
    tension sont orthogonaux. Une contradiction seule, sans claim positive
    préexistante, ne crée pas nécessairement de tension (le current_stage
    peut rester non_etabli, jamais un pseudo-stade négatif).

    scope_mode : whole_competency / localized / competency_only ; périmètre
    localisé par CompetencyInferenceTensionCapability. revision_status :
    unresolved / revalidation_needed. La base stocke une tension déjà
    décidée (T6-C) ; cohérence avec les claims et le périmètre : T6-B.

    T6-A : table créée par la migration 0009_competency_inference_state ;
    ni lue ni écrite par l'application."""
    __tablename__ = "competency_inference_tensions"
    __table_args__ = (
        CheckConstraint(
            f"fragilized_stage IN {_CLAIM_STAGES_SQL}",
            name="ck_competency_inference_tensions_fragilized_stage",
        ),
        CheckConstraint(
            "scope_mode IN ('whole_competency', 'localized', 'competency_only')",
            name="ck_competency_inference_tensions_scope_mode",
        ),
        CheckConstraint(
            "revision_status IN ('unresolved', 'revalidation_needed')",
            name="ck_competency_inference_tensions_revision_status",
        ),
        Index("ix_competency_inference_tensions_inference_run_id", "inference_run_id"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    inference_run_id = Column(Uuid, ForeignKey("competency_inference_runs.id"), nullable=False)
    fragilized_stage = Column(String, nullable=False)
    scope_mode = Column(String, nullable=False)
    summary = Column(Text, nullable=False)
    revision_status = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class CompetencyInferenceTensionCapability(Base):
    """Périmètre sémantique d'une CompetencyInferenceTension (T6-A), et
    rien d'autre : ni preuve, ni score, ni poids, ni stade, ni horodatage.
    PK composite (tension_id, capability_membership_id).

    Limites volontaires (T6-B, ni trigger ni dénormalisation) : localized
    => au moins une ligne ; whole_competency et competency_only => aucune ;
    membership de la release du run T5 parent, de la compétence du run et
    du périmètre réellement fragilisé.

    Nom explicite de la FK membership : le nom automatique PostgreSQL
    dépasserait 63 caractères.

    T6-A : table créée par la migration 0009_competency_inference_state ;
    ni lue ni écrite par l'application."""
    __tablename__ = "competency_inference_tension_capabilities"
    __table_args__ = (
        Index("ix_competency_inference_tension_caps_membership_id", "capability_membership_id"),
    )

    tension_id = Column(Uuid, ForeignKey("competency_inference_tensions.id"), primary_key=True)
    capability_membership_id = Column(
        Uuid,
        ForeignKey("capability_taxonomy_memberships.id",
                   name="competency_inference_tension_caps_membership_id_fkey"),
        primary_key=True,
    )


class CompetencyInferenceBasisRef(Base):
    """Provenance explicite d'une décision T6 (T6-A) : QUELLE source du
    dossier (observation, dépendance, transfert ou revalidation T5)
    documente une claim, une dimension de confiance, une tension, une
    transition, un besoin de validation ou une évaluation de maîtrise.
    Provenance seulement : une ref n'est JAMAIS une preuve supplémentaire
    (ni +1, ni poids, ni coefficient).

    Quatre FK explicites (pas de FK polymorphique opaque) ; exactement une
    source, cohérente avec source_kind
    (ck_competency_inference_basis_refs_source_xor). Rattachement structurel
    par ref_role :
    - positive_basis : stage_claim_id requis, tension_id et
      confidence_dimension absents, source_kind = observation (seules les
      PedagogicalObservation soutiennent directement une positive claim ;
      les relations T5 n'y valent jamais « preuve +1 ») ;
    - confidence : stage_claim_id et confidence_dimension requis
      (diagnosticity / coverage / independence / consistency /
      temporal_validation), tension_id absent ;
    - mastery : stage_claim_id requis, tension_id et confidence_dimension
      absents ;
    - tension : tension_id requis, stage_claim_id et confidence_dimension
      absents ;
    - transition / validation : run-level, stage_claim_id, tension_id et
      confidence_dimension absents.

    Limites volontaires (T6-B, ni trigger ni dénormalisation) : la source
    appartient au snapshot / au run T5 consommé par le run T6 ; la claim ou
    la tension appartient au même run ; une ref mastery vise la claim
    mastery.

    T6-A : table créée par la migration 0009_competency_inference_state ;
    ni lue ni écrite par l'application."""
    __tablename__ = "competency_inference_basis_refs"
    __table_args__ = (
        CheckConstraint(
            "ref_role IN ('positive_basis', 'confidence', 'tension', 'transition', 'validation', 'mastery')",
            name="ck_competency_inference_basis_refs_ref_role",
        ),
        CheckConstraint(
            "confidence_dimension IN ('diagnosticity', 'coverage', 'independence', 'consistency', "
            "'temporal_validation')",
            name="ck_competency_inference_basis_refs_confidence_dimension",
        ),
        CheckConstraint(
            "source_kind IN ('observation', 'dependency', 'transfer', 'revalidation')",
            name="ck_competency_inference_basis_refs_source_kind",
        ),
        CheckConstraint(
            "(source_kind = 'observation' AND source_observation_id IS NOT NULL "
            "AND source_dependency_id IS NULL AND source_transfer_id IS NULL "
            "AND source_revalidation_id IS NULL) "
            "OR (source_kind = 'dependency' AND source_observation_id IS NULL "
            "AND source_dependency_id IS NOT NULL AND source_transfer_id IS NULL "
            "AND source_revalidation_id IS NULL) "
            "OR (source_kind = 'transfer' AND source_observation_id IS NULL "
            "AND source_dependency_id IS NULL AND source_transfer_id IS NOT NULL "
            "AND source_revalidation_id IS NULL) "
            "OR (source_kind = 'revalidation' AND source_observation_id IS NULL "
            "AND source_dependency_id IS NULL AND source_transfer_id IS NULL "
            "AND source_revalidation_id IS NOT NULL)",
            name="ck_competency_inference_basis_refs_source_xor",
        ),
        CheckConstraint(
            "ref_role <> 'positive_basis' OR (stage_claim_id IS NOT NULL AND tension_id IS NULL "
            "AND confidence_dimension IS NULL AND source_kind = 'observation')",
            name="ck_competency_inference_basis_refs_positive_basis_ref",
        ),
        CheckConstraint(
            "ref_role <> 'confidence' OR (stage_claim_id IS NOT NULL AND tension_id IS NULL "
            "AND confidence_dimension IS NOT NULL)",
            name="ck_competency_inference_basis_refs_confidence_ref",
        ),
        CheckConstraint(
            "ref_role <> 'mastery' OR (stage_claim_id IS NOT NULL AND tension_id IS NULL "
            "AND confidence_dimension IS NULL)",
            name="ck_competency_inference_basis_refs_mastery_ref",
        ),
        CheckConstraint(
            "ref_role <> 'tension' OR (stage_claim_id IS NULL AND tension_id IS NOT NULL "
            "AND confidence_dimension IS NULL)",
            name="ck_competency_inference_basis_refs_tension_ref",
        ),
        CheckConstraint(
            "ref_role <> 'transition' OR (stage_claim_id IS NULL AND tension_id IS NULL "
            "AND confidence_dimension IS NULL)",
            name="ck_competency_inference_basis_refs_transition_ref",
        ),
        CheckConstraint(
            "ref_role <> 'validation' OR (stage_claim_id IS NULL AND tension_id IS NULL "
            "AND confidence_dimension IS NULL)",
            name="ck_competency_inference_basis_refs_validation_ref",
        ),
        Index("ix_competency_inference_basis_refs_inference_run_id", "inference_run_id"),
        Index("ix_competency_inference_basis_refs_stage_claim_id", "stage_claim_id"),
        Index("ix_competency_inference_basis_refs_tension_id", "tension_id"),
        Index("ix_competency_inference_basis_refs_source_observation_id", "source_observation_id"),
        Index("ix_competency_inference_basis_refs_source_dependency_id", "source_dependency_id"),
        Index("ix_competency_inference_basis_refs_source_transfer_id", "source_transfer_id"),
        Index("ix_competency_inference_basis_refs_source_revalidation_id", "source_revalidation_id"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    inference_run_id = Column(Uuid, ForeignKey("competency_inference_runs.id"), nullable=False)
    stage_claim_id = Column(Uuid, ForeignKey("competency_stage_claims.id"), nullable=True)
    tension_id = Column(Uuid, ForeignKey("competency_inference_tensions.id"), nullable=True)
    ref_role = Column(String, nullable=False)
    confidence_dimension = Column(String, nullable=True)
    source_kind = Column(String, nullable=False)
    source_observation_id = Column(Uuid, ForeignKey("pedagogical_observations.id"), nullable=True)
    source_dependency_id = Column(Uuid, ForeignKey("observation_dependencies.id"), nullable=True)
    source_transfer_id = Column(Uuid, ForeignKey("observation_transfers.id"), nullable=True)
    source_revalidation_id = Column(Uuid, ForeignKey("observation_revalidations.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class UserCompetencyState(Base):
    """Read-model / cache de l'état courant d'UNE compétence d'UN
    utilisateur (T6-A), PAS la source de vérité pédagogique : la source de
    vérité est le CompetencyInferenceRun active (active_inference_run_id,
    UNIQUE) et sa chaîne parentale (run T5, observations). PK composite
    (user_id, competency_code).

    Absence de ligne = aucune inférence encore produite ; ligne
    current_stage = non_etabli = dossier réellement interprété sans
    prétention positive suffisante pour Discovery. Les deux restent
    distincts : la migration ne crée aucune ligne (aucun backfill) et aucun
    trigger ne l'alimente. Seul T6-B, lors de l'activation atomique d'un
    run, écrira cette table.

    state_generation : compteur TECHNIQUE de génération du cache (mutation
    définie en T6-B) ; jamais XP, progression, score de stade, confiance ni
    nombre de preuves. Aucun niveau global : l'état reste
    compétence-spécifique (C1..C12) ; users.level reste distinct du moteur
    pédagogique.

    T6-A : table créée par la migration 0009_competency_inference_state ;
    ni lue ni écrite par l'application."""
    __tablename__ = "user_competency_states"
    __table_args__ = (
        CheckConstraint(
            f"competency_code IN {_COMPETENCY_CODES_SQL}",
            name="ck_user_competency_states_competency_code",
        ),
        CheckConstraint(
            f"current_stage IN {_CURRENT_STAGES_SQL}",
            name="ck_user_competency_states_current_stage",
        ),
        CheckConstraint(
            "tension_state IN ('none', 'open')",
            name="ck_user_competency_states_tension_state",
        ),
        UniqueConstraint(
            "active_inference_run_id",
            name="uq_user_competency_states_active_inference_run_id",
        ),
    )

    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    competency_code = Column(String, primary_key=True)
    active_inference_run_id = Column(Uuid, ForeignKey("competency_inference_runs.id"), nullable=False)
    current_stage = Column(String, nullable=False)
    tension_state = Column(String, nullable=False)
    state_generation = Column(BigInteger, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow_aware)


class AssistantDelivery(Base):
    """Réponse Oryx canonique produite pour UN tour utilisateur sur une
    surface produit, et son statut de livraison (R1-C1).

    Infrastructure runtime uniquement : AssistantDelivery n'est ni un
    SupportTrace, ni un CognitiveEvent, ni une observation, ni une preuve
    utilisateur, ni un mouvement pédagogique. Elle établit seulement
    l'identité d'un assistant turn (id, exposé sous le nom
    assistant_turn_id), la réponse canonique rejouée à l'identique sur
    retry, et le statut :

    - pending : contenu canonique persisté AVANT la réponse HTTP ;
    - delivered : le frontend first-party Oryx a confirmé avoir inséré ce
      contenu dans l'interface. delivered != lu, compris, regardé,
      mémorisé ni utilisé cognitivement.

    Identité logique (UNIQUE uq_assistant_deliveries_turn_ordinal, défense
    finale sous concurrence) : (user_id, conversation_key, surface,
    source_user_turn_id, delivery_ordinal). id est aléatoire et ne sert pas
    à la déduplication.

    response_payload = payload public canonique (jamais assistant_turn_id,
    delivery_status ni private_metadata) ; private_metadata n'est jamais
    renvoyé au frontend ; visible_content_fingerprint est une empreinte
    d'intégrité du texte canonique affiché, pas une preuve de lecture.
    analysis_session_id NULL = aucun suivi produit applicable (aucune
    session artificielle).

    Seul point d'écriture : core/assistant_delivery.py. Une fois créée,
    seuls status / delivered_at (ACK) et analysis_session_id (liaison) sont
    mutés. Table créée par la migration 0011_assistant_deliveries ; id et
    timestamps générés par l'application (aucun server_default)."""
    __tablename__ = "assistant_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivered')",
            name="ck_assistant_deliveries_status",
        ),
        CheckConstraint(
            "delivery_ordinal >= 1",
            name="ck_assistant_deliveries_delivery_ordinal",
        ),
        UniqueConstraint(
            "user_id", "conversation_key", "surface", "source_user_turn_id", "delivery_ordinal",
            name="uq_assistant_deliveries_turn_ordinal",
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    conversation_key = Column(String, ForeignKey("conversation_identities.conversation_key"), nullable=False)
    surface = Column(String, nullable=False)
    source_user_turn_id = Column(Uuid, nullable=False)
    delivery_ordinal = Column(Integer, nullable=False)
    analysis_session_id = Column(Uuid, ForeignKey("analysis_sessions.id"), nullable=True)
    request_fingerprint = Column(String, nullable=False)
    visible_content_fingerprint = Column(String, nullable=False)
    response_payload = Column(JSONB(none_as_null=True), nullable=False)
    private_metadata = Column(JSONB(none_as_null=True), nullable=False)
    status = Column(String, nullable=False)
    delivery_schema_version = Column(String, nullable=False)
    generated_at = Column(DateTime(timezone=True), nullable=False)
    delivered_at = Column(DateTime(timezone=True), nullable=True)


class DecryptageCognitiveLink(Base):
    """Rattachement cognitif T2 d'UN tour Décrypter (R1-C2) : relie, pour
    une AssistantDelivery R1-C2, ce que la production utilisateur du tour
    est devenue (côté entrée) et ce que la réponse assistant réellement
    delivered est devenue (côté réponse).

    Ce n'est PAS une preuve, ni une observation, ni une évaluation : une
    ligne de provenance runtime spécifique Décrypter. Un tour peut relier la
    production utilisateur à un ancien CognitiveEvent (input_event_id) ET la
    réponse assistant à un nouveau (response_event_id) : jamais un seul
    cognitive_event_id par livraison. CognitiveEvent != message ; le
    marqueur d'étape produit n'est ni task_kind ni compétence ; delivered
    != lu / compris.

    - assistant_delivery_id (PK) : au plus une ligne par AssistantDelivery
      (idempotence de la capture) ;
    - input_action / input_event_id : sort de la production utilisateur du
      tour, fixé dans la transaction /decryptage du worker gagnant ;
    - context_exit_event_id : (historique V1 uniquement) event QUITTÉ
      (fermé) par ce tour sur rupture de contexte, qu'il ait été finalized
      (travail utilisateur présent, input_action event_closed_context_change)
      ou abandoned (event vide, event_abandoned_context_change). Toujours
      NULL en V2 (R1-C4) : une navigation ne ferme jamais un event ;
    - input_context_event_id (R1-C4, V2 uniquement) : event auquel la
      conversation du tour était ANCRÉE quand le tour est arrivé (dernière
      livraison V2 delivered + captured de cette conversation_key). Référence
      de contexte de la livraison du tour à l'ACK (stale_delivery si cet
      event a été fermé entre-temps). NULL si aucune ancre (no_open_event) ;
      context_switched : l'ancre quittée, laissée intacte ;
    - capture_state awaiting_delivery -> captured, atomiquement avec
      l'ACK pending -> delivered ; response_action / response_event_id /
      support_trace_id / captured_at sont remplis à ce moment.

    Aucune duplication de user_id, conversation_key, surface, tour source,
    analysis_session_id, ticker ni marqueur : ils viennent de
    AssistantDelivery. Seul point d'écriture :
    core/decryptage_cognitive_runtime.py. Table créée par la migration
    0012_decryptage_cognitive_links ; timestamps générés par l'application
    (aucun server_default), CHECK plutôt qu'ENUM, FK sans cascade.
    Étendue (sans backfill) par 0013_decryptage_conv_affinity (R1-C4) :
    input_context_event_id, context_switched, stale_delivery, CHECK de
    cohérence par version de runtime."""
    __tablename__ = "decryptage_cognitive_links"
    __table_args__ = (
        CheckConstraint(
            "capture_state IN ('awaiting_delivery', 'captured')",
            name="ck_decryptage_cognitive_links_capture_state",
        ),
        CheckConstraint(
            "input_action IN ('no_open_event', 'no_user_contribution', 'contribution_appended', "
            "'event_closed_context_change', 'event_abandoned_context_change', 'context_switched')",
            name="ck_decryptage_cognitive_links_input_action",
        ),
        CheckConstraint(
            "response_action IS NULL OR response_action IN ('opened_event', 'continued_event', "
            "'continued_without_boundary_signal', 'transitioned_event', 'closed_terminal', "
            "'no_cognitive_action', 'stale_delivery')",
            name="ck_decryptage_cognitive_links_response_action",
        ),
        CheckConstraint(
            "(capture_state = 'awaiting_delivery' AND response_action IS NULL AND response_event_id IS NULL "
            "AND support_trace_id IS NULL AND captured_at IS NULL) OR (capture_state = 'captured' "
            "AND response_action IS NOT NULL AND captured_at IS NOT NULL)",
            name="ck_decryptage_cognitive_links_capture_state_fields",
        ),
        CheckConstraint(
            "(input_action = 'contribution_appended') = (input_event_id IS NOT NULL)",
            name="ck_decryptage_cognitive_links_input_event",
        ),
        CheckConstraint(
            "response_action IS NULL OR ((response_action IN ('opened_event', 'continued_event', "
            "'continued_without_boundary_signal', 'transitioned_event')) = (response_event_id IS NOT NULL) "
            "AND (response_action IN ('continued_event', 'continued_without_boundary_signal')) "
            "= (support_trace_id IS NOT NULL))",
            name="ck_decryptage_cognitive_links_response_refs",
        ),
        CheckConstraint(
            "input_action <> 'context_switched' OR (input_event_id IS NULL AND input_context_event_id IS NOT NULL "
            "AND context_exit_event_id IS NULL)",
            name="ck_decryptage_cognitive_links_context_switched",
        ),
        CheckConstraint(
            "response_action IS NULL OR response_action <> 'stale_delivery' OR (response_event_id IS NULL "
            "AND support_trace_id IS NULL)",
            name="ck_decryptage_cognitive_links_stale_delivery",
        ),
        CheckConstraint(
            "(capture_version = 'decryptage-cognitive-runtime-v1' AND input_context_event_id IS NULL "
            "AND input_action <> 'context_switched' "
            "AND (response_action IS NULL OR response_action <> 'stale_delivery')) "
            "OR (capture_version = 'decryptage-cognitive-runtime-v2' AND context_exit_event_id IS NULL "
            "AND input_action NOT IN ('event_closed_context_change', 'event_abandoned_context_change') "
            "AND (input_action = 'no_open_event') = (input_context_event_id IS NULL) "
            "AND (input_action <> 'contribution_appended' OR input_event_id = input_context_event_id))",
            name="ck_decryptage_cognitive_links_runtime_version",
        ),
        Index(
            "uq_decryptage_cognitive_links_opening_event", "response_event_id", unique=True,
            postgresql_where=text("response_action IN ('opened_event', 'transitioned_event')"),
        ),
    )

    assistant_delivery_id = Column(Uuid, ForeignKey("assistant_deliveries.id"), primary_key=True)
    capture_version = Column(String, nullable=False)
    input_action = Column(String, nullable=False)
    input_event_id = Column(Uuid, ForeignKey("cognitive_events.id"), nullable=True)
    context_exit_event_id = Column(Uuid, ForeignKey("cognitive_events.id"), nullable=True)
    capture_state = Column(String, nullable=False)
    response_action = Column(String, nullable=True)
    response_event_id = Column(Uuid, ForeignKey("cognitive_events.id"), nullable=True)
    support_trace_id = Column(Uuid, ForeignKey("support_traces.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=True)
    # R1-C4 (0013) : ajoutée en fin de table par ALTER TABLE.
    input_context_event_id = Column(Uuid, ForeignKey("cognitive_events.id"), nullable=True)
