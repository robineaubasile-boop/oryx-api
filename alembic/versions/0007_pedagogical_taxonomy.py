"""T4-A : persistance de la taxonomie pédagogique versionnée et des capacités
noyau Oryx (EXPAND).

Revision ID: 0007_pedagogical_taxonomy
Revises: 0006_observation_layer
Create Date: 2026-09-28

Doctrine : observation = force et profondeur de la preuve ; capacité =
périmètre de la preuve. Une capacité noyau n'est ni une preuve, ni une
sous-compétence utilisateur, ni une case à valider : elle n'a ni niveau,
ni score, ni confiance, ni statut acquis, ni progression. Aucune colonne
de ce type n'existe dans les tables ci-dessous.

Crée uniquement la STRUCTURE (aucune donnée pédagogique, aucune release,
aucune capacité seedée : les quatre tables sont vides après l'upgrade) :

pedagogical_taxonomy_releases : version de l'ensemble du référentiel
(C1-C12, capacités noyau, frontières, mapping guidance).
- version_key (UNIQUE uq_pedagogical_taxonomy_releases_version_key) et
  spec_fingerprint (UNIQUE uq_pedagogical_taxonomy_releases_spec_fingerprint,
  jamais calculé ici) ;
- status : candidate / active / retired (CHECK
  ck_pedagogical_taxonomy_releases_status). Les transitions ne sont pas
  gérées ici ;
- uq_pedagogical_taxonomy_releases_one_active : index UNIQUE PARTIEL sur
  (status) WHERE status = 'active' : au plus une release active, garanti
  par PostgreSQL ; candidate / retired restent multiples ;
- activated_at nullable, aucun server_default.

core_capability_definitions : UNE signification d'une capacité noyau
(même id = même sens ; un changement de sens = nouvelle ligne, même
capability_code, semantic_revision suivante, nouvel UUID).
- capability_code : exactement les 45 codes figés (C1_A..C3_C, C4_A..C12_D),
  CHECK ck_core_capability_definitions_capability_code ;
- competency_code : C1..C12, CHECK ck_core_capability_definitions_competency_code ;
- ck_core_capability_definitions_code_competency :
  split_part(capability_code, '_', 1) = competency_code (C7_A => C7 ;
  C7_A + C8 refusé) ;
- ck_core_capability_definitions_semantic_revision : semantic_revision >= 1 ;
- uq_core_capability_definitions_code_revision :
  UNIQUE(capability_code, semantic_revision) ;
- mapping_guidance : JSONB NOT NULL sans schéma interne en base
  (validation métier en T4-B/T4-C) ; definition : TEXT.
  Aucun trigger d'immutabilité (applicative, T4-B).

capability_taxonomy_memberships : « cette définition précise appartient à
cette release » (jamais une validation, un poids, un ordre ni une
progression). Une définition inchangée est réutilisée par plusieurs
releases. UNIQUE(taxonomy_release_id, capability_definition_id)
(uq_capability_taxonomy_memberships_release_definition).

observation_capabilities : localisation sémantique d'une
pedagogical_observation existante (PK composite (observation_id,
capability_membership_id), sans id artificiel). Une ligne ne crée jamais
de nouvelle preuve ; plusieurs lignes pour une observation restent UNE
observation.

observation_evaluation_runs.pedagogical_taxonomy_release_id (colonne
EXISTANTE de 0006, UUID nullable) reçoit enfin sa FK
observation_evaluation_runs_taxonomy_release_id_fkey ->
pedagogical_taxonomy_releases.id. La colonne n'est ni recréée ni rendue
NOT NULL (runs historiques sans release, T7 non branché, aucun backfill).
Le nom automatique PostgreSQL (<table>_<colonne>_fkey) dépasserait 63
caractères : il est explicite et abrégé.
Garde-fou : la table cible étant créée par cette migration, toute valeur
non NULL préexistante serait orpheline. Elle n'est JAMAIS effacée ni
réécrite : l'upgrade échoue (RuntimeError, ou exception du bloc DO en
mode offline) avant l'ajout de la FK, la transaction Alembic est annulée
et la base reste en 0006. La validation de la FK par PostgreSQL reste la
défense finale (une valeur insérée entre la vérification et l'ALTER fait
échouer l'ALTER).

Index (PostgreSQL n'indexe pas le côté enfant d'une FK) :
- ix_capability_taxonomy_memberships_capability_definition_id : releases
  contenant une définition ;
- ix_observation_capabilities_capability_membership_id : observations
  localisées sur un membership ;
- ix_observation_evaluation_runs_pedagogical_taxonomy_release_id : runs
  évalués sous une release ;
- aucun index séparé sur memberships.taxonomy_release_id ni sur
  observation_capabilities.observation_id : première colonne,
  respectivement, de l'UNIQUE et de la PK composite (déjà indexées).

Invariants volontairement reportés à T4-B (ils traversent plusieurs
tables ; ni trigger ni dénormalisation ici) :
- une release ne contient jamais deux révisions du même capability_code ;
- une observation Cn n'est localisée que sur des capacités Cn_* (une
  interaction démontrant C7 et C8 produit deux observations) ;
- localized => au moins une observation_capabilities ; competency_only =>
  aucune ;
- immutabilité des définitions et transitions candidate -> active ->
  retired.

Conventions (identiques à 0005/0006) : UUID générés par l'application
(aucun server_default), TIMESTAMPTZ sans server_default, VARCHAR + CHECK
(pas d'ENUM), FK sans ON DELETE / ON UPDATE (NO ACTION : aucune cascade),
aucun trigger, aucune fonction. Identifiant de révision de 25 caractères
(alembic_version.version_num = VARCHAR(32)).

Le downgrade retire la FK et l'index ajoutés à observation_evaluation_runs
(la colonne pedagogical_taxonomy_release_id, qui appartient à 0006, est
conservée avec ses valeurs), puis les quatre tables T4 et leurs index dans
l'ordre inverse des dépendances : retour exact à 0006. Il supprime les
données T4 ; les runs, observations, événements et aides T2/T3 ne sont pas
touchés. Ce n'est pas la stratégie normale de rollback production (on
privilégie le rollback applicatif).
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0007_pedagogical_taxonomy"
down_revision: Union[str, Sequence[str], None] = "0006_observation_layer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNS = "observation_evaluation_runs"
RELEASE_COLUMN = "pedagogical_taxonomy_release_id"
RELEASE_FK = "observation_evaluation_runs_taxonomy_release_id_fkey"
RELEASE_INDEX = "ix_observation_evaluation_runs_pedagogical_taxonomy_release_id"
ONE_ACTIVE_INDEX = "uq_pedagogical_taxonomy_releases_one_active"
MEMBERSHIP_DEFINITION_INDEX = "ix_capability_taxonomy_memberships_capability_definition_id"
OBSERVATION_MEMBERSHIP_INDEX = "ix_observation_capabilities_capability_membership_id"
ORPHAN_RELEASE_MESSAGE = (
    "T4-A : observation_evaluation_runs contient des pedagogical_taxonomy_release_id "
    "sans release correspondante, FK non ajoutée. Aucune valeur n'est effacée ni "
    "réécrite : ces runs doivent être examinés avant la migration 0007_pedagogical_taxonomy."
)
ORPHAN_RELEASE_FILTER = (
    f"FROM {RUNS} r WHERE r.{RELEASE_COLUMN} IS NOT NULL "
    f"AND NOT EXISTS (SELECT 1 FROM pedagogical_taxonomy_releases p WHERE p.id = r.{RELEASE_COLUMN})"
)


def upgrade() -> None:
    """Crée les quatre tables T4 et leurs index, puis la FK
    observation_evaluation_runs.pedagogical_taxonomy_release_id (et rien
    d'autre)."""
    op.create_table(
        "pedagogical_taxonomy_releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("spec_fingerprint", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('candidate', 'active', 'retired')",
            name="ck_pedagogical_taxonomy_releases_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "version_key",
            name="uq_pedagogical_taxonomy_releases_version_key",
        ),
        sa.UniqueConstraint(
            "spec_fingerprint",
            name="uq_pedagogical_taxonomy_releases_spec_fingerprint",
        ),
    )
    op.create_index(
        ONE_ACTIVE_INDEX,
        "pedagogical_taxonomy_releases",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "core_capability_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("capability_code", sa.String(), nullable=False),
        sa.Column("semantic_revision", sa.Integer(), nullable=False),
        sa.Column("competency_code", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("mapping_guidance", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # Les 45 codes figés : 3 capacités pour C1-C3, 4 pour C4-C12.
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "competency_code IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', "
            "'C7', 'C8', 'C9', 'C10', 'C11', 'C12')",
            name="ck_core_capability_definitions_competency_code",
        ),
        sa.CheckConstraint(
            "split_part(capability_code, '_', 1) = competency_code",
            name="ck_core_capability_definitions_code_competency",
        ),
        sa.CheckConstraint(
            "semantic_revision >= 1",
            name="ck_core_capability_definitions_semantic_revision",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "capability_code", "semantic_revision",
            name="uq_core_capability_definitions_code_revision",
        ),
    )
    op.create_table(
        "capability_taxonomy_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("taxonomy_release_id", sa.Uuid(), nullable=False),
        sa.Column("capability_definition_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["taxonomy_release_id"], ["pedagogical_taxonomy_releases.id"]),
        sa.ForeignKeyConstraint(["capability_definition_id"], ["core_capability_definitions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "taxonomy_release_id", "capability_definition_id",
            name="uq_capability_taxonomy_memberships_release_definition",
        ),
    )
    op.create_index(
        MEMBERSHIP_DEFINITION_INDEX,
        "capability_taxonomy_memberships",
        ["capability_definition_id"],
    )
    op.create_table(
        "observation_capabilities",
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("capability_membership_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["observation_id"], ["pedagogical_observations.id"]),
        sa.ForeignKeyConstraint(["capability_membership_id"], ["capability_taxonomy_memberships.id"]),
        sa.PrimaryKeyConstraint("observation_id", "capability_membership_id"),
    )
    op.create_index(
        OBSERVATION_MEMBERSHIP_INDEX,
        "observation_capabilities",
        ["capability_membership_id"],
    )

    # Garde-fou : jamais d'effacement ni de faux backfill d'une valeur
    # historique pour faire passer la FK.
    if context.is_offline_mode():
        message = ORPHAN_RELEASE_MESSAGE.replace("'", "''")
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 {ORPHAN_RELEASE_FILTER}) THEN "
            f"RAISE EXCEPTION '{message}'; END IF; END $$"
        )
    else:
        orphans = op.get_bind().execute(sa.text(f"SELECT count(*) {ORPHAN_RELEASE_FILTER}")).scalar_one()
        if orphans:
            raise RuntimeError(f"{ORPHAN_RELEASE_MESSAGE} ({orphans} run(s) concerné(s))")
    op.create_index(RELEASE_INDEX, RUNS, [RELEASE_COLUMN])
    op.create_foreign_key(
        RELEASE_FK,
        RUNS,
        "pedagogical_taxonomy_releases",
        [RELEASE_COLUMN],
        ["id"],
    )


def downgrade() -> None:
    """Retire la FK et l'index de observation_evaluation_runs (colonne
    conservée), puis les quatre tables T4 (retour exact à 0006)."""
    op.drop_constraint(RELEASE_FK, RUNS, type_="foreignkey")
    op.drop_index(RELEASE_INDEX, table_name=RUNS)
    op.drop_index(OBSERVATION_MEMBERSHIP_INDEX, table_name="observation_capabilities")
    op.drop_table("observation_capabilities")
    op.drop_index(MEMBERSHIP_DEFINITION_INDEX, table_name="capability_taxonomy_memberships")
    op.drop_table("capability_taxonomy_memberships")
    op.drop_table("core_capability_definitions")
    op.drop_index(
        ONE_ACTIVE_INDEX,
        table_name="pedagogical_taxonomy_releases",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table("pedagogical_taxonomy_releases")
