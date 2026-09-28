"""T6-A : persistance de l'inférence de l'état C1-C12 du Niveau 6 (EXPAND).

Revision ID: 0009_competency_inference_state
Revises: 0008_longitudinal_relations
Create Date: 2026-09-28

Doctrine : le Niveau 6 produit une INTERPRÉTATION VERSIONNÉE du dossier
longitudinal ; il ne crée aucune preuve. Chaîne : PedagogicalObservation
-> périmètre T4 -> dossier / relations T5 -> inférence T6. Un run
d'inférence, une stage claim, une tension, une basis ref ou le cache
user_competency_states sont des couches DÉRIVÉES : aucune ne devient une
nouvelle preuve utilisateur. Aucun score de compétence, aucune confiance
numérique, aucun stade numérique, aucun niveau global, aucun plan de
validation actif persistant (validation_needs = besoin latent dérivé ;
l'utilisation concrète d'une interaction pour observer appartient à
l'adaptation future). users.level n'est pas touché.

Crée uniquement la STRUCTURE (les six tables sont vides après l'upgrade ;
aucune donnée migrée, aucun backfill, aucun seed, aucun état généré pour
les utilisateurs existants) :

competency_inference_runs : interprétation versionnée du dossier d'UNE
compétence (C1..C12) d'UN utilisateur, adossée à exactement UN
longitudinal_assessment_run (snapshot T5 interprété ; la release de
taxonomie est la sienne, non dupliquée). predecessor_inference_run_id :
self-FK nullable, jamais le run lui-même
(ck_competency_inference_runs_no_self_predecessor).
- execution_status running / completed / failed, interpretation_status
  candidate / active / superseded / obsolete ; previous_stage /
  current_stage (non_etabli / discovery / comprehension / application /
  mastery), transition (maintained / upgraded / revised_down),
  transition_cause (new_user_evidence / evidence_integrity_change /
  pedagogical_reinterpretation), tension_state (none / open) : VARCHAR +
  CHECK nommés ck_competency_inference_runs_<colonne> ; trigger :
  vocabulaire ouvert, sans CHECK ;
- sorties NULLABLE : un run running est une inférence en cours et
  non_etabli (conclusion pédagogique réelle) n'est JAMAIS un placeholder ;
  aucun CHECK croisé de lifecycle (complétion, transitions : T6-B) ;
- inference_dedup_key : UNIQUE uq_competency_inference_runs_dedup_key ;
- uq_competency_inference_runs_one_active_user_competency : index UNIQUE
  PARTIEL sur (user_id, competency_code) WHERE interpretation_status =
  'active' (même philosophie que T3 / T5).

competency_stage_claims : prétention POSITIVE sur un stade (discovery /
comprehension / application / mastery ; non_etabli n'est PAS une claim),
UNIQUE(inference_run_id, stage). ck_competency_stage_claims_basis_status_mode :
not_established => basis_mode none ; established => direct ou
implied_by_higher_claim. confidence_profile et mastery_assessment : JSONB
nullable sans schéma interne (T6-C), jamais un score. Aucun
highest_positive_stage persisté (dérivé des claims).

competency_inference_tensions : prétention positive défendable fragilisée
sur un périmètre (fragilized_stage, scope_mode whole_competency / localized
/ competency_only, revision_status unresolved / revalidation_needed).
Tension != stade != pénalité.

competency_inference_tension_capabilities : périmètre d'une tension (PK
composite (tension_id, capability_membership_id), rien d'autre).

competency_inference_basis_refs : provenance explicite (jamais une preuve
supplémentaire) vers exactement UNE source à FK explicite : observation,
dépendance, transfert ou revalidation (ck_competency_inference_basis_refs_source_xor,
aucune FK polymorphique opaque). Rattachement structurel par ref_role
(ck_competency_inference_basis_refs_<role>_ref) : positive_basis = claim +
observation uniquement ; confidence = claim + confidence_dimension
(diagnosticity / coverage / independence / consistency /
temporal_validation) ; mastery = claim ; tension = tension ; transition /
validation = run-level.

user_competency_states : read-model / cache du run T6 active (PK
(user_id, competency_code), active_inference_run_id NOT NULL et UNIQUE).
Pas la source de vérité ; absence de ligne = aucune inférence, ligne
non_etabli = dossier interprété sans base positive pour Discovery.
state_generation (BIGINT) : compteur technique du cache, jamais XP ni
progression. Seul T6-B l'écrira, à l'activation atomique d'un run.

Invariants volontairement reportés à T6-B / T6-C (ils traversent plusieurs
tables ; ni trigger, ni fonction, ni dénormalisation ici) : run T5 parent
completed / active, même user et même compétence ; predecessor du même
user, de la même compétence et réellement précédent ; input_fingerprint
cohérent ; complétion (sorties renseignées, exactement quatre claims) avant
activation ; règles de transition (première inférence, maintained,
upgraded, revised_down) ; profil de confiance réservé aux claims
established ; ref mastery vers la claim mastery ; claim / tension de refs
appartenant au même run ; sources des refs appartenant au dossier T5
consommé ; localized => >= 1 ligne de périmètre, whole_competency /
competency_only => aucune ; memberships de la release du run T5, de la
compétence du run ; mutation de state_generation et écriture du cache.

Index (PostgreSQL n'indexe pas le côté enfant d'une FK) : un index par FK
enfant, sauf lorsque la colonne est la première d'une PK composite ou d'un
UNIQUE (competency_stage_claims.inference_run_id via
uq_competency_stage_claims_run_stage ; user_competency_states.user_id via
la PK ; active_inference_run_id via son UNIQUE ;
competency_inference_tension_capabilities.tension_id via la PK). L'index
unique partiel ne couvre que les runs active : user_id a son index dédié.

Nom explicite lorsque le nom automatique dépasserait 63 caractères :
competency_inference_tension_caps_membership_id_fkey et son index.

Conventions (identiques à 0005-0008) : UUID générés par l'application
(aucun server_default), TIMESTAMPTZ sans server_default, VARCHAR + CHECK
(pas d'ENUM), FK sans ON DELETE / ON UPDATE (NO ACTION : aucune cascade ;
les inférences forment un audit trail dérivé), aucun trigger, aucune
fonction. Aucune table T0-T5 n'est modifiée. Identifiant de révision de 31
caractères (alembic_version.version_num = VARCHAR(32)).

Le downgrade supprime les six tables et leurs index dans l'ordre inverse
des dépendances : retour exact à 0008, sans toucher aux tables T0-T5. Il
supprime les données T6 ; ce n'est pas la stratégie normale de rollback
production (on privilégie le rollback applicatif).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0009_competency_inference_state"
down_revision: Union[str, Sequence[str], None] = "0008_longitudinal_relations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNS = "competency_inference_runs"
CLAIMS = "competency_stage_claims"
TENSIONS = "competency_inference_tensions"
TENSION_CAPS = "competency_inference_tension_capabilities"
REFS = "competency_inference_basis_refs"
STATES = "user_competency_states"

ONE_ACTIVE_INDEX = "uq_competency_inference_runs_one_active_user_competency"
ACTIVE_WHERE = "interpretation_status = 'active'"
COMPETENCY_CODE_SQL = ("competency_code IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', "
                       "'C7', 'C8', 'C9', 'C10', 'C11', 'C12')")
# non_etabli : conclusion pédagogique réelle, jamais une stage claim.
CURRENT_STAGES = "('non_etabli', 'discovery', 'comprehension', 'application', 'mastery')"
CLAIM_STAGES = "('discovery', 'comprehension', 'application', 'mastery')"

# Index de FK côté enfant, par table, dans l'ordre de création.
INDEXES = {
    RUNS: {
        "ix_competency_inference_runs_user_id": "user_id",
        "ix_competency_inference_runs_longitudinal_assessment_run_id": "longitudinal_assessment_run_id",
        "ix_competency_inference_runs_predecessor_inference_run_id": "predecessor_inference_run_id",
    },
    CLAIMS: {},
    TENSIONS: {"ix_competency_inference_tensions_inference_run_id": "inference_run_id"},
    TENSION_CAPS: {"ix_competency_inference_tension_caps_membership_id": "capability_membership_id"},
    REFS: {
        "ix_competency_inference_basis_refs_inference_run_id": "inference_run_id",
        "ix_competency_inference_basis_refs_stage_claim_id": "stage_claim_id",
        "ix_competency_inference_basis_refs_tension_id": "tension_id",
        "ix_competency_inference_basis_refs_source_observation_id": "source_observation_id",
        "ix_competency_inference_basis_refs_source_dependency_id": "source_dependency_id",
        "ix_competency_inference_basis_refs_source_transfer_id": "source_transfer_id",
        "ix_competency_inference_basis_refs_source_revalidation_id": "source_revalidation_id",
    },
    STATES: {},
}


def _create_indexes(table: str) -> None:
    for name, column in INDEXES[table].items():
        op.create_index(name, table, [column])


def _drop_indexes(table: str) -> None:
    for name in reversed(list(INDEXES[table])):
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    """Crée les six tables T6-A et leurs index (et rien d'autre)."""
    # 1. Interprétation versionnée du dossier Cx d'un utilisateur.
    op.create_table(
        RUNS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("competency_code", sa.String(), nullable=False),
        sa.Column("longitudinal_assessment_run_id", sa.Uuid(), nullable=False),
        sa.Column("predecessor_inference_run_id", sa.Uuid(), nullable=True),
        sa.Column("execution_status", sa.String(), nullable=False),
        sa.Column("interpretation_status", sa.String(), nullable=False),
        # Vocabulaire ouvert : volontairement sans CHECK.
        sa.Column("trigger", sa.String(), nullable=False),
        # Sorties NULLABLE : un run running est une inférence en cours
        # (non_etabli n'est jamais un placeholder).
        sa.Column("previous_stage", sa.String(), nullable=True),
        sa.Column("current_stage", sa.String(), nullable=True),
        sa.Column("transition", sa.String(), nullable=True),
        sa.Column("transition_cause", sa.String(), nullable=True),
        sa.Column("tension_state", sa.String(), nullable=True),
        sa.Column("unresolved_revision_context", postgresql.JSONB(), nullable=True),
        sa.Column("validation_needs", postgresql.JSONB(), nullable=True),
        sa.Column("state_decision_summary", sa.Text(), nullable=True),
        sa.Column("positive_basis_version", sa.String(), nullable=False),
        sa.Column("confidence_profile_version", sa.String(), nullable=False),
        sa.Column("state_decision_version", sa.String(), nullable=False),
        sa.Column("validation_version", sa.String(), nullable=False),
        sa.Column("inference_schema_version", sa.String(), nullable=False),
        sa.Column("evaluator_version", sa.String(), nullable=False),
        # Provenance facultative uniquement : aucun appel de modèle ici.
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("prompt_spec_version", sa.String(), nullable=True),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("output_fingerprint", sa.String(), nullable=True),
        sa.Column("inference_dedup_key", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.CheckConstraint(COMPETENCY_CODE_SQL, name="ck_competency_inference_runs_competency_code"),
        sa.CheckConstraint(
            "execution_status IN ('running', 'completed', 'failed')",
            name="ck_competency_inference_runs_execution_status",
        ),
        sa.CheckConstraint(
            "interpretation_status IN ('candidate', 'active', 'superseded', 'obsolete')",
            name="ck_competency_inference_runs_interpretation_status",
        ),
        sa.CheckConstraint(f"previous_stage IN {CURRENT_STAGES}",
                           name="ck_competency_inference_runs_previous_stage"),
        sa.CheckConstraint(f"current_stage IN {CURRENT_STAGES}",
                           name="ck_competency_inference_runs_current_stage"),
        sa.CheckConstraint(
            "transition IN ('maintained', 'upgraded', 'revised_down')",
            name="ck_competency_inference_runs_transition",
        ),
        sa.CheckConstraint(
            "transition_cause IN ('new_user_evidence', 'evidence_integrity_change', "
            "'pedagogical_reinterpretation')",
            name="ck_competency_inference_runs_transition_cause",
        ),
        sa.CheckConstraint(
            "tension_state IN ('none', 'open')",
            name="ck_competency_inference_runs_tension_state",
        ),
        sa.CheckConstraint(
            "predecessor_inference_run_id IS NULL OR predecessor_inference_run_id <> id",
            name="ck_competency_inference_runs_no_self_predecessor",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["longitudinal_assessment_run_id"], ["longitudinal_assessment_runs.id"]),
        sa.ForeignKeyConstraint(["predecessor_inference_run_id"], [f"{RUNS}.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inference_dedup_key", name="uq_competency_inference_runs_dedup_key"),
    )
    op.create_index(
        ONE_ACTIVE_INDEX,
        RUNS,
        ["user_id", "competency_code"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_WHERE),
    )
    _create_indexes(RUNS)

    # 2. Prétentions positives par stade (non_etabli n'en est pas une).
    op.create_table(
        CLAIMS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("inference_run_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("positive_basis_status", sa.String(), nullable=False),
        sa.Column("basis_mode", sa.String(), nullable=False),
        sa.Column("basis_summary", sa.Text(), nullable=True),
        sa.Column("scope_summary", sa.Text(), nullable=True),
        sa.Column("confidence_profile", postgresql.JSONB(), nullable=True),
        sa.Column("mastery_assessment", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"stage IN {CLAIM_STAGES}", name="ck_competency_stage_claims_stage"),
        sa.CheckConstraint(
            "positive_basis_status IN ('established', 'not_established')",
            name="ck_competency_stage_claims_positive_basis_status",
        ),
        sa.CheckConstraint(
            "basis_mode IN ('direct', 'implied_by_higher_claim', 'none')",
            name="ck_competency_stage_claims_basis_mode",
        ),
        sa.CheckConstraint(
            "(positive_basis_status = 'not_established' AND basis_mode = 'none') "
            "OR (positive_basis_status = 'established' "
            "AND basis_mode IN ('direct', 'implied_by_higher_claim'))",
            name="ck_competency_stage_claims_basis_status_mode",
        ),
        sa.ForeignKeyConstraint(["inference_run_id"], [f"{RUNS}.id"]),
        sa.PrimaryKeyConstraint("id"),
        # Couvre aussi l'index de la FK inference_run_id (première colonne).
        sa.UniqueConstraint("inference_run_id", "stage", name="uq_competency_stage_claims_run_stage"),
    )

    # 3. Tensions : prétention positive fragilisée (ni stade, ni pénalité).
    op.create_table(
        TENSIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("inference_run_id", sa.Uuid(), nullable=False),
        sa.Column("fragilized_stage", sa.String(), nullable=False),
        sa.Column("scope_mode", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("revision_status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"fragilized_stage IN {CLAIM_STAGES}",
                           name="ck_competency_inference_tensions_fragilized_stage"),
        sa.CheckConstraint(
            "scope_mode IN ('whole_competency', 'localized', 'competency_only')",
            name="ck_competency_inference_tensions_scope_mode",
        ),
        sa.CheckConstraint(
            "revision_status IN ('unresolved', 'revalidation_needed')",
            name="ck_competency_inference_tensions_revision_status",
        ),
        sa.ForeignKeyConstraint(["inference_run_id"], [f"{RUNS}.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(TENSIONS)

    # 4. Périmètre des tensions.
    op.create_table(
        TENSION_CAPS,
        sa.Column("tension_id", sa.Uuid(), nullable=False),
        sa.Column("capability_membership_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["tension_id"], [f"{TENSIONS}.id"]),
        # Nom automatique (<table>_<colonne>_fkey) : 71 caractères > 63.
        sa.ForeignKeyConstraint(
            ["capability_membership_id"], ["capability_taxonomy_memberships.id"],
            name="competency_inference_tension_caps_membership_id_fkey",
        ),
        sa.PrimaryKeyConstraint("tension_id", "capability_membership_id"),
    )
    _create_indexes(TENSION_CAPS)

    # 5. Provenance (jamais une preuve supplémentaire) : quatre FK explicites.
    op.create_table(
        REFS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("inference_run_id", sa.Uuid(), nullable=False),
        sa.Column("stage_claim_id", sa.Uuid(), nullable=True),
        sa.Column("tension_id", sa.Uuid(), nullable=True),
        sa.Column("ref_role", sa.String(), nullable=False),
        sa.Column("confidence_dimension", sa.String(), nullable=True),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("source_observation_id", sa.Uuid(), nullable=True),
        sa.Column("source_dependency_id", sa.Uuid(), nullable=True),
        sa.Column("source_transfer_id", sa.Uuid(), nullable=True),
        sa.Column("source_revalidation_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ref_role IN ('positive_basis', 'confidence', 'tension', 'transition', 'validation', 'mastery')",
            name="ck_competency_inference_basis_refs_ref_role",
        ),
        sa.CheckConstraint(
            "confidence_dimension IN ('diagnosticity', 'coverage', 'independence', 'consistency', "
            "'temporal_validation')",
            name="ck_competency_inference_basis_refs_confidence_dimension",
        ),
        sa.CheckConstraint(
            "source_kind IN ('observation', 'dependency', 'transfer', 'revalidation')",
            name="ck_competency_inference_basis_refs_source_kind",
        ),
        sa.CheckConstraint(
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
        # Seule une PedagogicalObservation soutient directement une positive
        # claim : les relations T5 ne valent jamais « preuve +1 ».
        sa.CheckConstraint(
            "ref_role <> 'positive_basis' OR (stage_claim_id IS NOT NULL AND tension_id IS NULL "
            "AND confidence_dimension IS NULL AND source_kind = 'observation')",
            name="ck_competency_inference_basis_refs_positive_basis_ref",
        ),
        sa.CheckConstraint(
            "ref_role <> 'confidence' OR (stage_claim_id IS NOT NULL AND tension_id IS NULL "
            "AND confidence_dimension IS NOT NULL)",
            name="ck_competency_inference_basis_refs_confidence_ref",
        ),
        sa.CheckConstraint(
            "ref_role <> 'mastery' OR (stage_claim_id IS NOT NULL AND tension_id IS NULL "
            "AND confidence_dimension IS NULL)",
            name="ck_competency_inference_basis_refs_mastery_ref",
        ),
        sa.CheckConstraint(
            "ref_role <> 'tension' OR (stage_claim_id IS NULL AND tension_id IS NOT NULL "
            "AND confidence_dimension IS NULL)",
            name="ck_competency_inference_basis_refs_tension_ref",
        ),
        sa.CheckConstraint(
            "ref_role <> 'transition' OR (stage_claim_id IS NULL AND tension_id IS NULL "
            "AND confidence_dimension IS NULL)",
            name="ck_competency_inference_basis_refs_transition_ref",
        ),
        sa.CheckConstraint(
            "ref_role <> 'validation' OR (stage_claim_id IS NULL AND tension_id IS NULL "
            "AND confidence_dimension IS NULL)",
            name="ck_competency_inference_basis_refs_validation_ref",
        ),
        sa.ForeignKeyConstraint(["inference_run_id"], [f"{RUNS}.id"]),
        sa.ForeignKeyConstraint(["stage_claim_id"], [f"{CLAIMS}.id"]),
        sa.ForeignKeyConstraint(["tension_id"], [f"{TENSIONS}.id"]),
        sa.ForeignKeyConstraint(["source_observation_id"], ["pedagogical_observations.id"]),
        sa.ForeignKeyConstraint(["source_dependency_id"], ["observation_dependencies.id"]),
        sa.ForeignKeyConstraint(["source_transfer_id"], ["observation_transfers.id"]),
        sa.ForeignKeyConstraint(["source_revalidation_id"], ["observation_revalidations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(REFS)

    # 6. Cache du run T6 active (jamais source de vérité, aucune ligne créée).
    op.create_table(
        STATES,
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("competency_code", sa.String(), nullable=False),
        sa.Column("active_inference_run_id", sa.Uuid(), nullable=False),
        sa.Column("current_stage", sa.String(), nullable=False),
        sa.Column("tension_state", sa.String(), nullable=False),
        # Compteur technique de génération du cache (jamais XP / progression).
        sa.Column("state_generation", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(COMPETENCY_CODE_SQL, name="ck_user_competency_states_competency_code"),
        sa.CheckConstraint(f"current_stage IN {CURRENT_STAGES}",
                           name="ck_user_competency_states_current_stage"),
        sa.CheckConstraint(
            "tension_state IN ('none', 'open')",
            name="ck_user_competency_states_tension_state",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["active_inference_run_id"], [f"{RUNS}.id"]),
        sa.PrimaryKeyConstraint("user_id", "competency_code"),
        sa.UniqueConstraint("active_inference_run_id",
                            name="uq_user_competency_states_active_inference_run_id"),
    )


def downgrade() -> None:
    """Supprime les six tables T6-A et leurs index dans l'ordre inverse des
    dépendances (retour exact à 0008)."""
    for table in (STATES, REFS, TENSION_CAPS, TENSIONS, CLAIMS):
        _drop_indexes(table)
        op.drop_table(table)
    _drop_indexes(RUNS)
    op.drop_index(ONE_ACTIVE_INDEX, table_name=RUNS, postgresql_where=sa.text(ACTIVE_WHERE))
    op.drop_table(RUNS)
