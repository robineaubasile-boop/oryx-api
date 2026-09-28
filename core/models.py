import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Float, Integer, SmallInteger, Text, DateTime, ForeignKey, Uuid,
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
