"""T5-A : persistance des relations longitudinales du Niveau 5 (EXPAND).

Revision ID: 0008_longitudinal_relations
Revises: 0007_pedagogical_taxonomy
Create Date: 2026-09-28

Doctrine : OBSERVATION = unité de preuve ; CAPABILITY = périmètre de la
preuve ; RELATION T5 = structure historique ENTRE preuves. Une relation T5
ne crée JAMAIS de nouvelle preuve : elle ne vaut ni +1, ni 0.5 preuve, ni
coefficient, ni poids. Le Niveau 5 ne décide pas du stade utilisateur, ne
calcule ni score, ni confiance, ni progression, ne conclut aucune maîtrise
et ne déclenche aucune revalidation future (tout cela appartient à T6).
Les profils coverage / variety / independence / consistency / freshness /
durability seront RECONSTRUITS à partir de ces tables : aucune table ni
colonne ne les stocke.

Crée uniquement la STRUCTURE (les huit tables sont vides après l'upgrade ;
aucune donnée migrée, aucun backfill, aucun seed, aucune taxonomie
modifiée) :

longitudinal_assessment_runs : une version précise du dossier d'UNE
compétence (competency_code) d'UN utilisateur (user_id), examinée sous une
release de taxonomie (pedagogical_taxonomy_release_id) et des versions
explicites (dependency_version, transfer_version, revalidation_version,
relation_schema_version ; rien d'autre : ni modèle, ni prompt, ni
évaluateur, ni output_fingerprint, ni run prédécesseur).
- competency_code C1..C12, execution_status running / completed / failed,
  interpretation_status candidate / active / superseded / obsolete :
  VARCHAR + CHECK nommés ck_longitudinal_assessment_runs_<colonne> ;
  trigger : vocabulaire ouvert, VARCHAR sans CHECK ;
- assessment_dedup_key : UNIQUE uq_longitudinal_assessment_runs_dedup_key ;
- uq_longitudinal_assessment_runs_one_active_user_competency : index UNIQUE
  PARTIEL sur (user_id, competency_code) WHERE interpretation_status =
  'active' : au plus une interprétation longitudinale active par
  compétence utilisateur, garanti par PostgreSQL y compris sous
  concurrence ; candidate / superseded / obsolete restent multiples ;
- input_fingerprint : stocké seulement (algorithme défini en T5-B) ;
- aucun CHECK croisé status <-> completed_at (lifecycle applicatif, T5-B).

longitudinal_assessment_inputs : snapshot EXPLICITE des observations
réellement examinées par un run (PK composite (run_id, observation_id),
sans id, poids, score ni horodatage). Un run peut n'avoir aucune entrée
(dossier vide : représentable, distinct de « aucun run »). L'absence
ultérieure de relation ne signifie jamais indépendance.

observation_dependencies : dépendance cognitive INTER-épisodes d'une
observation cible envers une source (source_kind observation /
support_trace). ck_observation_dependencies_source_xor impose exactement
une source cohérente avec source_kind ;
ck_observation_dependencies_no_self_dependency interdit qu'une observation
dépende d'elle-même. dependency_type : dependent / partially_dependent
uniquement. « independent » n'existe VOLONTAIREMENT pas : l'absence d'arête
dans un dossier examiné signifie seulement qu'aucune dépendance n'a été
identifiée ; l'indépendance sera reconstruite. dependency_basis (JSONB,
sans schéma en base) dit POURQUOI, jamais combien.

observation_transfers : transfert directionnel (source -> cible), local et
non transitif, effectivement démontré ;
ck_observation_transfers_distinct_observations (source <> cible).

observation_revalidations : fait HISTORIQUE qu'une nouvelle démonstration
supportive réexamine un mécanisme auparavant fragilisé par une observation
contradictory (jamais « il faudra revalider » : décision T6) ;
ck_observation_revalidations_distinct_observations. La contradiction reste
conservée.

dependency_capabilities / transfer_capabilities / revalidation_capabilities :
périmètre sémantique d'une arête (PK composite (<relation>_id,
capability_membership_id), sans id, score, poids, stade ni horodatage : la
provenance temporelle appartient à l'arête parente).

scope_mode (whole_observation / localized / competency_only) : CHECK nommé
ck_<table>_scope_mode sur les trois tables d'arêtes. scope_fingerprint et
les colonnes *_basis sont stockés seulement (calcul et validation en
T5-B). Aucune closure transitive (A -> B et B -> C ne génèrent jamais
A -> C).

Invariants volontairement reportés à T5-B (ils traversent plusieurs
tables ; ni trigger, ni fonction, ni dénormalisation d'event_id ici) :
entrée du snapshot appartenant au user et à la compétence du run, issue
d'un run T3 active completed, integrity_status = valid et compatible avec
la release ; source / cible dans le snapshot, même user / compétence ;
dépendance inter-événements et support_trace du bon contexte causal ;
transfert supportive -> supportive, cible au moins Application, variation
cognitive réelle (changer ticker, date, surface ou société ne suffit
jamais), adaptation attribuable à l'utilisateur ; revalidation
contradictory -> supportive, inter-événements, même mécanisme, pas une
répétition immédiate après correction ; localized => >= 1 ligne de scope,
competency_only => aucune ; memberships de la release du run ; scope de
dépendance ⊆ cible, scope de transfert / revalidation ⊆ intersection
source-cible ; dépendance directe et transfert autonome incompatibles sur
un même scope ; input_fingerprint encore courant à l'activation ;
transitions candidate / active / superseded / obsolete.

Index (PostgreSQL n'indexe pas le côté enfant d'une FK) : un index par FK
enfant, sauf lorsque la colonne est la première d'une PK composite
(longitudinal_assessment_inputs.run_id, <relation>_capabilities.<relation>_id).
L'index unique partiel ne couvre que les runs active : user_id a son index
dédié.

Noms explicites lorsque le nom automatique dépasserait 63 caractères (ou
l'atteindrait) : longitudinal_assessment_runs_taxonomy_release_id_fkey,
observation_revalidations_source_contradiction_fkey,
observation_revalidations_target_supportive_fkey et les index associés.

Conventions (identiques à 0005/0006/0007) : UUID générés par l'application
(aucun server_default), TIMESTAMPTZ sans server_default, VARCHAR + CHECK
(pas d'ENUM), FK sans ON DELETE / ON UPDATE (NO ACTION : aucune cascade ;
invalidation, supersession et versioning, jamais suppression destructive),
aucun trigger, aucune fonction. Aucune table T0-T4 n'est modifiée.
Identifiant de révision de 27 caractères (alembic_version.version_num =
VARCHAR(32)).

Le downgrade supprime les huit tables et leurs index dans l'ordre inverse
des dépendances : retour exact à 0007, sans toucher aux tables T0-T4. Il
supprime les données T5 ; ce n'est pas la stratégie normale de rollback
production (on privilégie le rollback applicatif).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0008_longitudinal_relations"
down_revision: Union[str, Sequence[str], None] = "0007_pedagogical_taxonomy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNS = "longitudinal_assessment_runs"
INPUTS = "longitudinal_assessment_inputs"
DEPENDENCIES = "observation_dependencies"
DEPENDENCY_CAPS = "dependency_capabilities"
TRANSFERS = "observation_transfers"
TRANSFER_CAPS = "transfer_capabilities"
REVALIDATIONS = "observation_revalidations"
REVALIDATION_CAPS = "revalidation_capabilities"

ONE_ACTIVE_INDEX = "uq_longitudinal_assessment_runs_one_active_user_competency"
ACTIVE_WHERE = "interpretation_status = 'active'"
SCOPE_MODE_SQL = "scope_mode IN ('whole_observation', 'localized', 'competency_only')"

# Index de FK côté enfant, par table, dans l'ordre de création.
INDEXES = {
    RUNS: {
        "ix_longitudinal_assessment_runs_user_id": "user_id",
        "ix_longitudinal_assessment_runs_taxonomy_release_id": "pedagogical_taxonomy_release_id",
    },
    INPUTS: {"ix_longitudinal_assessment_inputs_observation_id": "observation_id"},
    DEPENDENCIES: {
        "ix_observation_dependencies_run_id": "run_id",
        "ix_observation_dependencies_target_observation_id": "target_observation_id",
        "ix_observation_dependencies_source_observation_id": "source_observation_id",
        "ix_observation_dependencies_source_support_trace_id": "source_support_trace_id",
    },
    DEPENDENCY_CAPS: {"ix_dependency_capabilities_capability_membership_id": "capability_membership_id"},
    TRANSFERS: {
        "ix_observation_transfers_run_id": "run_id",
        "ix_observation_transfers_source_observation_id": "source_observation_id",
        "ix_observation_transfers_target_observation_id": "target_observation_id",
    },
    TRANSFER_CAPS: {"ix_transfer_capabilities_capability_membership_id": "capability_membership_id"},
    REVALIDATIONS: {
        "ix_observation_revalidations_run_id": "run_id",
        "ix_observation_revalidations_source_contradiction": "source_contradiction_observation_id",
        "ix_observation_revalidations_target_supportive": "target_supportive_observation_id",
    },
    REVALIDATION_CAPS: {"ix_revalidation_capabilities_capability_membership_id": "capability_membership_id"},
}


def _create_indexes(table: str) -> None:
    for name, column in INDEXES[table].items():
        op.create_index(name, table, [column])


def _drop_indexes(table: str) -> None:
    for name in reversed(list(INDEXES[table])):
        op.drop_index(name, table_name=table)


def _capability_scope_table(table: str, relation_column: str, relation_table: str) -> None:
    """Table de périmètre sémantique d'une arête : PK composite, rien
    d'autre (ni id, ni score, ni poids, ni stade, ni horodatage)."""
    op.create_table(
        table,
        sa.Column(relation_column, sa.Uuid(), nullable=False),
        sa.Column("capability_membership_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint([relation_column], [f"{relation_table}.id"]),
        sa.ForeignKeyConstraint(["capability_membership_id"], ["capability_taxonomy_memberships.id"]),
        sa.PrimaryKeyConstraint(relation_column, "capability_membership_id"),
    )
    _create_indexes(table)


def upgrade() -> None:
    """Crée les huit tables T5-A et leurs index (et rien d'autre)."""
    # 1. Version précise du dossier Cx d'un utilisateur.
    op.create_table(
        RUNS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("competency_code", sa.String(), nullable=False),
        sa.Column("execution_status", sa.String(), nullable=False),
        sa.Column("interpretation_status", sa.String(), nullable=False),
        # Vocabulaire ouvert : volontairement sans CHECK.
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("pedagogical_taxonomy_release_id", sa.Uuid(), nullable=False),
        sa.Column("dependency_version", sa.String(), nullable=False),
        sa.Column("transfer_version", sa.String(), nullable=False),
        sa.Column("revalidation_version", sa.String(), nullable=False),
        sa.Column("relation_schema_version", sa.String(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("assessment_dedup_key", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.CheckConstraint(
            "competency_code IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', "
            "'C7', 'C8', 'C9', 'C10', 'C11', 'C12')",
            name="ck_longitudinal_assessment_runs_competency_code",
        ),
        sa.CheckConstraint(
            "execution_status IN ('running', 'completed', 'failed')",
            name="ck_longitudinal_assessment_runs_execution_status",
        ),
        sa.CheckConstraint(
            "interpretation_status IN ('candidate', 'active', 'superseded', 'obsolete')",
            name="ck_longitudinal_assessment_runs_interpretation_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        # Nom automatique (<table>_<colonne>_fkey) : 65 caractères > 63.
        sa.ForeignKeyConstraint(
            ["pedagogical_taxonomy_release_id"], ["pedagogical_taxonomy_releases.id"],
            name="longitudinal_assessment_runs_taxonomy_release_id_fkey",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assessment_dedup_key",
            name="uq_longitudinal_assessment_runs_dedup_key",
        ),
    )
    op.create_index(
        ONE_ACTIVE_INDEX,
        RUNS,
        ["user_id", "competency_code"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_WHERE),
    )
    _create_indexes(RUNS)

    # 2. Snapshot explicite des observations examinées par le run.
    op.create_table(
        INPUTS,
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], [f"{RUNS}.id"]),
        sa.ForeignKeyConstraint(["observation_id"], ["pedagogical_observations.id"]),
        sa.PrimaryKeyConstraint("run_id", "observation_id"),
    )
    _create_indexes(INPUTS)

    # 3. Dépendances inter-épisodes (jamais « independent »).
    op.create_table(
        DEPENDENCIES,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("target_observation_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("source_observation_id", sa.Uuid(), nullable=True),
        sa.Column("source_support_trace_id", sa.Uuid(), nullable=True),
        sa.Column("dependency_type", sa.String(), nullable=False),
        sa.Column("scope_mode", sa.String(), nullable=False),
        sa.Column("dependency_basis", postgresql.JSONB(), nullable=False),
        sa.Column("scope_fingerprint", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('observation', 'support_trace')",
            name="ck_observation_dependencies_source_kind",
        ),
        sa.CheckConstraint(
            "(source_kind = 'observation' AND source_observation_id IS NOT NULL "
            "AND source_support_trace_id IS NULL) "
            "OR (source_kind = 'support_trace' AND source_observation_id IS NULL "
            "AND source_support_trace_id IS NOT NULL)",
            name="ck_observation_dependencies_source_xor",
        ),
        sa.CheckConstraint(
            "source_observation_id IS NULL OR source_observation_id <> target_observation_id",
            name="ck_observation_dependencies_no_self_dependency",
        ),
        # « independent » volontairement ABSENT : l'absence d'arête ne
        # prouve jamais l'indépendance.
        sa.CheckConstraint(
            "dependency_type IN ('dependent', 'partially_dependent')",
            name="ck_observation_dependencies_dependency_type",
        ),
        sa.CheckConstraint(SCOPE_MODE_SQL, name="ck_observation_dependencies_scope_mode"),
        sa.ForeignKeyConstraint(["run_id"], [f"{RUNS}.id"]),
        sa.ForeignKeyConstraint(["target_observation_id"], ["pedagogical_observations.id"]),
        sa.ForeignKeyConstraint(["source_observation_id"], ["pedagogical_observations.id"]),
        sa.ForeignKeyConstraint(["source_support_trace_id"], ["support_traces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(DEPENDENCIES)

    # 4. Périmètre des dépendances.
    _capability_scope_table(DEPENDENCY_CAPS, "dependency_id", DEPENDENCIES)

    # 5. Transferts directionnels et non transitifs.
    op.create_table(
        TRANSFERS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_observation_id", sa.Uuid(), nullable=False),
        sa.Column("target_observation_id", sa.Uuid(), nullable=False),
        sa.Column("scope_mode", sa.String(), nullable=False),
        sa.Column("transfer_basis", postgresql.JSONB(), nullable=False),
        sa.Column("scope_fingerprint", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_observation_id <> target_observation_id",
            name="ck_observation_transfers_distinct_observations",
        ),
        sa.CheckConstraint(SCOPE_MODE_SQL, name="ck_observation_transfers_scope_mode"),
        sa.ForeignKeyConstraint(["run_id"], [f"{RUNS}.id"]),
        sa.ForeignKeyConstraint(["source_observation_id"], ["pedagogical_observations.id"]),
        sa.ForeignKeyConstraint(["target_observation_id"], ["pedagogical_observations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(TRANSFERS)

    # 6. Périmètre des transferts.
    _capability_scope_table(TRANSFER_CAPS, "transfer_id", TRANSFERS)

    # 7. Revalidations historiques effectivement observées.
    op.create_table(
        REVALIDATIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_contradiction_observation_id", sa.Uuid(), nullable=False),
        sa.Column("target_supportive_observation_id", sa.Uuid(), nullable=False),
        sa.Column("scope_mode", sa.String(), nullable=False),
        sa.Column("revalidation_basis", postgresql.JSONB(), nullable=False),
        sa.Column("scope_fingerprint", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_contradiction_observation_id <> target_supportive_observation_id",
            name="ck_observation_revalidations_distinct_observations",
        ),
        sa.CheckConstraint(SCOPE_MODE_SQL, name="ck_observation_revalidations_scope_mode"),
        sa.ForeignKeyConstraint(["run_id"], [f"{RUNS}.id"]),
        # Noms automatiques : 66 et 63 caractères (limite PostgreSQL).
        sa.ForeignKeyConstraint(
            ["source_contradiction_observation_id"], ["pedagogical_observations.id"],
            name="observation_revalidations_source_contradiction_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["target_supportive_observation_id"], ["pedagogical_observations.id"],
            name="observation_revalidations_target_supportive_fkey",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_indexes(REVALIDATIONS)

    # 8. Périmètre des revalidations.
    _capability_scope_table(REVALIDATION_CAPS, "revalidation_id", REVALIDATIONS)


def downgrade() -> None:
    """Supprime les huit tables T5-A et leurs index dans l'ordre inverse des
    dépendances (retour exact à 0007)."""
    for table in (REVALIDATION_CAPS, REVALIDATIONS, TRANSFER_CAPS, TRANSFERS,
                  DEPENDENCY_CAPS, DEPENDENCIES, INPUTS):
        _drop_indexes(table)
        op.drop_table(table)
    _drop_indexes(RUNS)
    op.drop_index(ONE_ACTIVE_INDEX, table_name=RUNS, postgresql_where=sa.text(ACTIVE_WHERE))
    op.drop_table(RUNS)
