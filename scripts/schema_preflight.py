"""Préflight de schéma — vérification READ-ONLY de la base PostgreSQL
existante par rapport à la baseline Alembic `0001_current_oryx_baseline`.

But
---
Avant tout `alembic stamp` sur la base Railway de production (T0-D), cet
outil vérifie que le schéma réellement présent en base correspond
exactement au schéma historique d'Oryx tel que figé dans
`alembic/versions/0001_current_oryx_baseline.py`.

Ce script est volontairement autonome : il ne importe ni `core.models`
ni `core.db`, pour ne dépendre d'aucun effet de bord applicatif (pas de
`Base.metadata`, pas de `create_all`, pas de moteur global). Le manifeste
attendu ci-dessous (EXPECTED_MANIFEST) est une recopie figée et explicite
de la baseline — il n'est PAS dérivé dynamiquement d'un futur état de
core/models.py.

Garanties READ-ONLY
--------------------
- Connexion PostgreSQL avec `default_transaction_read_only=on` (options
  libpq passées à psycopg2) ET `SET TRANSACTION READ ONLY` explicite sur
  la transaction, puis vérification effective via `SHOW
  transaction_read_only`. Le script refuse de continuer si PostgreSQL ne
  confirme pas ce mode.
- `NullPool` : aucune connexion n'est gardée ouverte au-delà de l'appel.
- Aucune ligne métier n'est jamais lue : uniquement des métadonnées via
  `sqlalchemy.inspect()` (catalogues système PostgreSQL). Aucun SELECT,
  COUNT, INSERT, UPDATE, DELETE, ALTER ou CREATE sur les tables métier.
- La transaction n'est jamais committée (rollback implicite à la
  fermeture de la connexion) : même une commande qui violerait le mode
  read-only serait annulée.
- `DATABASE_URL` n'est jamais affiché ni loggé, sous aucune forme (y
  compris dans les messages d'erreur).

Ce script ne fait JAMAIS de `alembic upgrade` / `stamp` / `downgrade`. Il
ne modifie jamais la base. En cas de MISMATCH, il ne corrige rien : il
liste l'écart et s'arrête.

Sortie
------
- MATCH  : "SCHEMA MATCH — production compatible avec
  0001_current_oryx_baseline", exit code 0.
- MISMATCH : "SCHEMA MISMATCH" suivi de la liste déterministe des écarts,
  exit code 1.
- Erreur opérationnelle (connexion impossible, mode read-only non
  confirmé, DATABASE_URL absent, dialecte non PostgreSQL, etc.) : message
  clair sans secret, exit code 2.

Usage
-----
    DATABASE_URL=postgresql://... python scripts/schema_preflight.py

Voir alembic/README, section T0-C, pour la méthode d'exécution recommandée
contre Railway (via `railway ssh`).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from sqlalchemy import types as sa_types

# --------------------------------------------------------------------------
# Types sémantiques (comparaison de types indépendante de la chaîne exacte)
# --------------------------------------------------------------------------

STRING = "STRING"
INTEGER = "INTEGER"
FLOAT = "FLOAT"
DATETIME_NAIVE = "DATETIME_NAIVE"
DATETIME_TZ = "DATETIME_TZ"

EXPECTED_SCHEMA = "public"

# Tables qui ne doivent JAMAIS être présentes avant le stamp T0-D.
FORBIDDEN_TABLES = {"alembic_version"}


# Comparaison par NOM DE CLASSE EXACT (pas isinstance) : Integer est la
# classe de base commune à INTEGER/BIGINT/SMALLINT, Float à
# FLOAT/DOUBLE_PRECISION/REAL, String à VARCHAR/CHAR/TEXT — un isinstance()
# accepterait donc silencieusement des types PostgreSQL réellement
# différents de ceux de la baseline. La baseline (sa.String()/sa.Integer()/
# sa.Float()) est créée SANS longueur/précision explicite ; sur PostgreSQL
# cela produit respectivement VARCHAR (sans longueur), INTEGER et DOUBLE
# PRECISION — jamais TEXT, CHAR, BIGINT, SMALLINT ou REAL, qui doivent donc
# être rejetés comme un type différent, pas acceptés comme "compatibles".
_STRING_TYPE_NAMES = {"String", "VARCHAR"}
_INTEGER_TYPE_NAMES = {"Integer", "INTEGER"}
_FLOAT_TYPE_NAMES = {"Float", "DOUBLE_PRECISION"}


def semantic_type(type_obj: object) -> str:
    """Traduit un type SQLAlchemy/PostgreSQL introspecté en catégorie
    sémantique stable, plutôt que de comparer des chaînes au caractère
    près (VARCHAR(255) vs character varying, etc.) — mais sans élargir la
    comparaison à des types PostgreSQL distincts (TEXT, CHAR, BIGINT,
    SMALLINT, REAL) même s'ils héritent de la même classe de base
    SQLAlchemy. Un type non reconnu retombe sur une catégorie UNKNOWN:*,
    qui ne correspond jamais à un type attendu de la baseline."""
    if isinstance(type_obj, sa_types.DateTime):
        return DATETIME_TZ if getattr(type_obj, "timezone", False) else DATETIME_NAIVE
    type_name = type(type_obj).__name__
    if type_name in _FLOAT_TYPE_NAMES:
        return FLOAT
    if type_name in _INTEGER_TYPE_NAMES:
        return INTEGER
    if type_name in _STRING_TYPE_NAMES:
        return STRING
    return f"UNKNOWN:{type_name}"


def column_is_autoincrement(column_info: dict) -> bool:
    """Détermine si une colonne réflétée a une sémantique auto-incrément
    PostgreSQL valide, sans comparer textuellement un DEFAULT nextval(...)
    au caractère près. Accepte toute représentation équivalente :
    - le flag 'autoincrement' calculé par le dialecte SQLAlchemy ;
    - une colonne IDENTITY (GENERATED ... AS IDENTITY) ;
    - un DEFAULT basé sur une séquence (nextval(...)), quel que soit le
      nom exact de la séquence.
    """
    if column_info.get("autoincrement") is True:
        return True
    if column_info.get("identity"):
        return True
    default = column_info.get("default")
    if isinstance(default, str) and "nextval(" in default:
        return True
    return False


def column_has_unexpected_server_default(column_info: dict) -> bool:
    """La baseline 0001 ne déclare aucun server_default métier (les
    default=/onupdate=datetime.utcnow de core/models.py sont des defaults
    applicatifs Python, jamais des DEFAULT SQL). Un DEFAULT SQL n'est donc
    légitime que s'il fait partie du mécanisme d'auto-incrément d'un id
    (séquence nextval(...) ou IDENTITY, cf. column_is_autoincrement) : tout
    autre DEFAULT (littéral, now(), ...), sur n'importe quelle colonne, est
    un écart avec la baseline.
    """
    default = column_info.get("default")
    if default is None:
        return False
    if isinstance(default, str) and "nextval(" in default:
        return False
    return True


# --------------------------------------------------------------------------
# Manifeste attendu — recopie figée de 0001_current_oryx_baseline
# --------------------------------------------------------------------------

def _fk(
    columns: Sequence[str],
    ref_table: str,
    ref_columns: Sequence[str],
    ref_schema: str = EXPECTED_SCHEMA,
) -> dict:
    return {
        "columns": list(columns),
        "ref_schema": ref_schema,
        "ref_table": ref_table,
        "ref_columns": list(ref_columns),
    }


EXPECTED_MANIFEST: Dict[str, dict] = {
    "users": {
        "columns": {
            "id": {"type": STRING, "nullable": False},
            "level": {"type": STRING, "nullable": False},
            "created_at": {"type": DATETIME_NAIVE, "nullable": True},
        },
        "primary_key": ["id"],
        "foreign_keys": [],
        "autoincrement": [],
    },
    "portfolio_positions": {
        "columns": {
            "id": {"type": INTEGER, "nullable": False},
            "user_id": {"type": STRING, "nullable": False},
            "ticker": {"type": STRING, "nullable": False},
            "quantity": {"type": FLOAT, "nullable": False},
            "purchase_price": {"type": FLOAT, "nullable": True},
            "target_percent": {"type": FLOAT, "nullable": True},
            "envelope": {"type": STRING, "nullable": True},
            "created_at": {"type": DATETIME_NAIVE, "nullable": True},
            "updated_at": {"type": DATETIME_NAIVE, "nullable": True},
        },
        "primary_key": ["id"],
        "foreign_keys": [_fk(["user_id"], "users", ["id"])],
        "autoincrement": ["id"],
    },
    "company_analyses": {
        "columns": {
            "id": {"type": INTEGER, "nullable": False},
            "user_id": {"type": STRING, "nullable": False},
            "ticker": {"type": STRING, "nullable": False},
            "current_step": {"type": STRING, "nullable": False},
            "created_at": {"type": DATETIME_NAIVE, "nullable": True},
            "updated_at": {"type": DATETIME_NAIVE, "nullable": True},
        },
        "primary_key": ["id"],
        "foreign_keys": [_fk(["user_id"], "users", ["id"])],
        "autoincrement": ["id"],
    },
    "investment_theses": {
        "columns": {
            "id": {"type": INTEGER, "nullable": False},
            "user_id": {"type": STRING, "nullable": False},
            "ticker": {"type": STRING, "nullable": False},
            "thesis_text": {"type": STRING, "nullable": False},
            "created_at": {"type": DATETIME_NAIVE, "nullable": True},
        },
        "primary_key": ["id"],
        "foreign_keys": [_fk(["user_id"], "users", ["id"])],
        "autoincrement": ["id"],
    },
    "analysis_facts": {
        "columns": {
            "id": {"type": INTEGER, "nullable": False},
            "user_id": {"type": STRING, "nullable": False},
            "ticker": {"type": STRING, "nullable": False},
            "fact_date": {"type": DATETIME_NAIVE, "nullable": True},
            "fact_type": {"type": STRING, "nullable": False},
            "fact_value": {"type": FLOAT, "nullable": True},
        },
        "primary_key": ["id"],
        "foreign_keys": [_fk(["user_id"], "users", ["id"])],
        "autoincrement": ["id"],
    },
    "user_statements": {
        "columns": {
            "id": {"type": INTEGER, "nullable": False},
            "user_id": {"type": STRING, "nullable": False},
            "ticker": {"type": STRING, "nullable": False},
            "step": {"type": STRING, "nullable": True},
            "statement_date": {"type": DATETIME_NAIVE, "nullable": True},
            "statement_text": {"type": STRING, "nullable": False},
        },
        "primary_key": ["id"],
        "foreign_keys": [_fk(["user_id"], "users", ["id"])],
        "autoincrement": ["id"],
    },
}


# --------------------------------------------------------------------------
# Erreurs
# --------------------------------------------------------------------------

class PreflightError(Exception):
    """Erreur opérationnelle empêchant d'effectuer le contrôle (connexion,
    mode read-only non confirmé, dialecte inattendu, ...). Ne doit jamais
    contenir DATABASE_URL ni aucun secret."""


# --------------------------------------------------------------------------
# Étape 1 : collecte brute (introspection pure via un "inspector")
# --------------------------------------------------------------------------

def collect_raw_snapshot(inspector, schema: str = EXPECTED_SCHEMA) -> dict:
    """Construit un instantané brut du schéma à partir d'un objet
    "inspector" (sqlalchemy.engine.reflection.Inspector, ou tout
    faux-objet de test exposant la même API en lecture). Aucune donnée
    métier n'est lue : uniquement get_table_names/get_columns/
    get_pk_constraint/get_foreign_keys/get_unique_constraints/
    get_check_constraints/get_indexes, qui interrogent les catalogues
    système PostgreSQL.
    """
    table_names = set(inspector.get_table_names(schema=schema))
    tables: Dict[str, dict] = {}
    for name in table_names:
        tables[name] = {
            "columns": inspector.get_columns(name, schema=schema),
            "pk": inspector.get_pk_constraint(name, schema=schema),
            "foreign_keys": inspector.get_foreign_keys(name, schema=schema),
            "unique_constraints": inspector.get_unique_constraints(name, schema=schema),
            "check_constraints": inspector.get_check_constraints(name, schema=schema),
            "indexes": inspector.get_indexes(name, schema=schema),
        }
    return {"schema": schema, "tables": tables}


# --------------------------------------------------------------------------
# Étape 2 : normalisation (pure, aucune dépendance à une connexion)
# --------------------------------------------------------------------------

def _normalize_ref_schema(referred_schema: Optional[str]) -> str:
    """SQLAlchemy renvoie parfois referred_schema=None quand la table
    référencée est dans le même schéma que la table inspectée (ici
    toujours `public`, cf. EXPECTED_SCHEMA) : None et "public" désignent
    donc le même schéma courant et sont traités comme équivalents. Toute
    autre valeur (un schéma réellement différent) est conservée telle
    quelle."""
    if referred_schema in (None, ""):
        return EXPECTED_SCHEMA
    return referred_schema


def normalize_snapshot(raw: dict) -> dict:
    """Convertit l'instantané brut en une structure normalisée,
    déterministe (listes triées) et comparable sémantiquement, sans
    aucune connexion à la base."""
    tables: Dict[str, dict] = {}
    for table_name, info in raw["tables"].items():
        columns: Dict[str, dict] = {}
        autoincrement_cols: List[str] = []
        for col in info["columns"]:
            columns[col["name"]] = {
                "type": semantic_type(col["type"]),
                "nullable": bool(col.get("nullable", True)),
                "has_unexpected_server_default": column_has_unexpected_server_default(col),
            }
            if column_is_autoincrement(col):
                autoincrement_cols.append(col["name"])

        pk_cols = sorted(info["pk"].get("constrained_columns") or [])

        foreign_keys = sorted(
            (
                {
                    "columns": sorted(fk.get("constrained_columns") or []),
                    "ref_schema": _normalize_ref_schema(fk.get("referred_schema")),
                    "ref_table": fk.get("referred_table"),
                    "ref_columns": sorted(fk.get("referred_columns") or []),
                }
                for fk in info["foreign_keys"]
            ),
            key=lambda fk: (fk["columns"], fk["ref_schema"], fk["ref_table"] or "", fk["ref_columns"]),
        )

        unique_constraints = sorted(
            sorted(u.get("column_names") or []) for u in info["unique_constraints"]
        )

        check_constraints = sorted(
            (c.get("sqltext") or c.get("name") or "?") for c in info["check_constraints"]
        )

        indexes = sorted(
            (
                {
                    "name": idx.get("name") or "?",
                    "columns": sorted(idx.get("column_names") or []),
                    "unique": bool(idx.get("unique")),
                }
                for idx in info["indexes"]
            ),
            key=lambda idx: (idx["name"], idx["columns"]),
        )

        tables[table_name] = {
            "columns": columns,
            "primary_key": pk_cols,
            "foreign_keys": foreign_keys,
            "autoincrement": sorted(autoincrement_cols),
            "unique_constraints": unique_constraints,
            "check_constraints": check_constraints,
            "indexes": indexes,
        }
    return {"schema": raw["schema"], "tables": tables}


# --------------------------------------------------------------------------
# Étape 3 : comparaison (pure, déterministe)
# --------------------------------------------------------------------------

def compare_snapshot(
    normalized: dict,
    expected: Dict[str, dict],
    dialect: Optional[str] = None,
    forbidden_tables: Optional[set] = None,
) -> List[str]:
    """Compare l'instantané normalisé au manifeste attendu et retourne la
    liste déterministe (triée) des écarts. Liste vide == MATCH."""
    forbidden_tables = FORBIDDEN_TABLES if forbidden_tables is None else forbidden_tables
    mismatches: List[str] = []

    if dialect is not None and dialect != "postgresql":
        mismatches.append(f"wrong dialect: expected postgresql, got '{dialect}'")

    actual_tables = normalized["tables"]
    expected_table_names = set(expected.keys())
    actual_table_names = set(actual_tables.keys())

    for forbidden in sorted(forbidden_tables & actual_table_names):
        mismatches.append(
            f"unexpected table: {forbidden} "
            "(doit être absente avant stamp — un état de migration existe déjà)"
        )

    for t in sorted(expected_table_names - actual_table_names):
        mismatches.append(f"missing table: {t}")

    for t in sorted(actual_table_names - expected_table_names - forbidden_tables):
        mismatches.append(f"unexpected table: {t}")

    for table_name in sorted(expected_table_names & actual_table_names):
        mismatches.extend(
            _compare_table(table_name, actual_tables[table_name], expected[table_name])
        )

    return mismatches


def _compare_table(table_name: str, actual: dict, expected: dict) -> List[str]:
    out: List[str] = []
    actual_cols = actual["columns"]
    expected_cols = expected["columns"]

    for c in sorted(set(expected_cols) - set(actual_cols)):
        out.append(f"missing column: {table_name}.{c}")

    for c in sorted(set(actual_cols) - set(expected_cols)):
        out.append(f"unexpected column: {table_name}.{c}")

    for c in sorted(set(expected_cols) & set(actual_cols)):
        exp_col = expected_cols[c]
        act_col = actual_cols[c]
        if act_col["type"] != exp_col["type"]:
            out.append(
                f"wrong type: {table_name}.{c} "
                f"(expected {exp_col['type']}, got {act_col['type']})"
            )
        if act_col["nullable"] != exp_col["nullable"]:
            out.append(
                f"wrong nullable: {table_name}.{c} "
                f"(expected nullable={exp_col['nullable']}, got {act_col['nullable']})"
            )
        if act_col["has_unexpected_server_default"]:
            out.append(f"unexpected server default: {table_name}.{c}")

    expected_pk = sorted(expected["primary_key"])
    if actual["primary_key"] != expected_pk:
        out.append(
            f"wrong pk: {table_name} (expected {expected_pk}, got {actual['primary_key']})"
        )

    expected_fks = expected["foreign_keys"]
    for exp_fk in expected_fks:
        exp_cols = sorted(exp_fk["columns"])
        exp_ref_schema = _normalize_ref_schema(exp_fk.get("ref_schema"))
        exp_ref_cols = sorted(exp_fk["ref_columns"])
        found = any(
            act_fk["columns"] == exp_cols
            and act_fk["ref_schema"] == exp_ref_schema
            and act_fk["ref_table"] == exp_fk["ref_table"]
            and act_fk["ref_columns"] == exp_ref_cols
            for act_fk in actual["foreign_keys"]
        )
        if not found:
            out.append(
                f"missing FK: {table_name}.{','.join(exp_cols)} -> "
                f"{exp_ref_schema}.{exp_fk['ref_table']}.{','.join(exp_ref_cols)}"
            )

    # Comparaison sur les 4 composantes (colonnes liées, schéma référencé
    # normalisé, table référencée, colonnes référencées) : une FK qui
    # pointerait vers le bon (table, colonnes) mais un schéma différent —
    # ou vice versa — doit être détectée comme inattendue, pas confondue
    # avec la FK attendue.
    expected_fk_keys = {
        (
            tuple(sorted(fk["columns"])),
            _normalize_ref_schema(fk.get("ref_schema")),
            fk["ref_table"],
            tuple(sorted(fk["ref_columns"])),
        )
        for fk in expected_fks
    }
    for act_fk in actual["foreign_keys"]:
        key = (
            tuple(act_fk["columns"]),
            act_fk["ref_schema"],
            act_fk["ref_table"],
            tuple(act_fk["ref_columns"]),
        )
        if key not in expected_fk_keys:
            out.append(
                f"unexpected FK: {table_name}.{','.join(act_fk['columns'])} -> "
                f"{act_fk['ref_schema']}.{act_fk['ref_table']}.{','.join(act_fk['ref_columns'])}"
            )

    for unique_cols in actual["unique_constraints"]:
        out.append(f"unexpected unique: {table_name}({','.join(unique_cols)})")

    for check in actual["check_constraints"]:
        out.append(f"unexpected check: {table_name}: {check}")

    for idx in actual["indexes"]:
        out.append(f"unexpected index: {table_name}.{idx['name']} ({','.join(idx['columns'])})")

    expected_autoincrement = set(expected.get("autoincrement", []))
    actual_autoincrement = set(actual["autoincrement"])
    for c in sorted(expected_autoincrement - actual_autoincrement):
        out.append(f"missing autoincrement: {table_name}.{c}")

    return out


# --------------------------------------------------------------------------
# Étape 4 : connexion READ-ONLY et orchestration (seule partie qui touche
# réellement une base de données)
# --------------------------------------------------------------------------

def normalize_database_url(raw_url: str) -> str:
    """Même normalisation que core/db.py (postgres:// -> postgresql://),
    réimplémentée ici pour que ce script reste autonome et n'importe
    jamais core.db ni core.models."""
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql://", 1)
    return raw_url


# Force une session read-only côté serveur dès la connexion (psycopg2).
READONLY_CONNECT_ARGS = {"options": "-c default_transaction_read_only=on"}


def build_readonly_engine(database_url: str):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    return create_engine(
        database_url,
        poolclass=NullPool,
        connect_args=READONLY_CONNECT_ARGS,
    )


def assert_transaction_read_only(connection) -> None:
    """Renforce et vérifie le mode read-only côté PostgreSQL. Lève
    PreflightError si PostgreSQL ne le confirme pas — le script refuse
    alors de continuer."""
    from sqlalchemy import text

    connection.execute(text("SET TRANSACTION READ ONLY"))
    value = connection.execute(text("SHOW transaction_read_only")).scalar()
    if str(value).strip().lower() != "on":
        raise PreflightError(
            "PostgreSQL n'a pas confirmé le mode read-only "
            f"(SHOW transaction_read_only = {value!r}). Arrêt par sécurité, "
            "aucune introspection effectuée."
        )


@dataclass
class PreflightResult:
    match: bool
    mismatches: List[str] = field(default_factory=list)
    dialect: str = ""


def run_preflight(database_url: Optional[str] = None) -> PreflightResult:
    """Point d'entrée qui se connecte réellement à PostgreSQL, en lecture
    seule, inspecte le schéma `public`, et compare au manifeste figé de
    la baseline. N'écrit jamais rien."""
    from sqlalchemy import inspect
    from sqlalchemy.exc import SQLAlchemyError

    url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")
    if not url:
        raise PreflightError(
            "DATABASE_URL n'est pas défini : impossible d'effectuer le préflight."
        )
    url = normalize_database_url(url)

    engine = build_readonly_engine(url)
    try:
        try:
            with engine.connect() as connection:
                assert_transaction_read_only(connection)
                dialect_name = connection.dialect.name
                inspector = inspect(connection)
                raw_snapshot = collect_raw_snapshot(inspector, schema=EXPECTED_SCHEMA)
                # Ne JAMAIS committer : la connexion se ferme ici sans
                # commit, donc tout est annulé (rollback implicite) même
                # si une instruction avait échappé au contrôle read-only.
        except PreflightError:
            raise
        except SQLAlchemyError as exc:
            # Le message d'une SQLAlchemyError peut parfois inclure des
            # fragments de la chaîne de connexion : on ne remonte que le
            # type d'erreur, jamais son texte complet.
            raise PreflightError(
                f"Erreur de connexion ou d'introspection PostgreSQL ({type(exc).__name__})."
            ) from exc
    finally:
        engine.dispose()

    normalized = normalize_snapshot(raw_snapshot)
    mismatches = compare_snapshot(normalized, EXPECTED_MANIFEST, dialect=dialect_name)
    return PreflightResult(match=not mismatches, mismatches=mismatches, dialect=dialect_name)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def format_report(result: PreflightResult) -> str:
    if result.match:
        return "SCHEMA MATCH — production compatible avec 0001_current_oryx_baseline"
    lines = ["SCHEMA MISMATCH"]
    for mismatch in result.mismatches:
        lines.append(f"  - {mismatch}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    del argv  # aucune option de ligne de commande : configuration via DATABASE_URL uniquement
    try:
        result = run_preflight()
    except PreflightError as exc:
        print(f"PREFLIGHT ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # garde-fou générique : jamais de fuite de secret
        print(f"PREFLIGHT ERROR: {type(exc).__name__}", file=sys.stderr)
        return 2

    print(format_report(result))
    return 0 if result.match else 1


if __name__ == "__main__":
    sys.exit(main())
