"""Contraintes NOT NULL du catalogue PostgreSQL 18 dans les tests de structure.

PostgreSQL 18 enregistre chaque NOT NULL de colonne comme une ligne de
pg_constraint (contype = 'n', nom « <table>_<colonne>_not_null », définition
« NOT NULL <colonne> ») ; PostgreSQL 16 ne le fait pas (seul
pg_attribute.attnotnull porte l'information). Les tests historiques qui
comparent pg_constraint à une liste exacte de PK / FK / UNIQUE / CHECK
passent par without_verified_not_null(), qui :

1. sur toutes les versions, vérifie que les colonnes NOT NULL de la table
   (pg_attribute.attnotnull) sont exactement celles attendues par le test ;
2. sur PostgreSQL >= 18, vérifie que les lignes 'n' reçues sont exactement
   une par colonne attendue, avec le nom et la définition que PostgreSQL
   leur donne, validées, locales et héritables, et qu'aucune autre ligne 'n'
   n'existe ;
3. avant PostgreSQL 18, vérifie qu'aucune ligne 'n' n'est présente ;

puis retourne les autres contraintes, dans leur ordre d'origine, pour la
comparaison exacte historique. Une contrainte NOT NULL manquante,
supplémentaire, renommée ou mal définie fait échouer le test : rien n'est
ignoré silencieusement.
"""
import sqlalchemy as sa

NAMEDATALEN = 64
NOT_NULL_LABEL = "not_null"


def not_null_constraint_name(table: str, column: str) -> str:
    """Nom automatique d'une contrainte NOT NULL (makeObjectName de
    PostgreSQL : on raccourcit d'abord le plus long des deux noms pour tenir
    dans NAMEDATALEN - 1 octets ; identifiants ASCII)."""
    overhead = 1 + len(NOT_NULL_LABEL) + 1
    available = NAMEDATALEN - 1 - overhead
    table_chars, column_chars = len(table), len(column)
    while table_chars + column_chars > available:
        if table_chars > column_chars:
            table_chars -= 1
        else:
            column_chars -= 1
    return f"{table[:table_chars]}_{column[:column_chars]}_{NOT_NULL_LABEL}"


def not_null_columns(catalog_columns) -> list:
    """Colonnes NOT NULL d'une liste attendue (nom, type, is_nullable, défaut)
    telle que les tests la comparent à information_schema.columns."""
    return [name for name, _type, is_nullable, *_ in catalog_columns if is_nullable == "NO"]


def expected_not_null_rows(table: str, columns) -> list:
    """Lignes (conname, contype, définition, confdeltype, confupdtype)
    attendues sur PostgreSQL >= 18 pour ces colonnes NOT NULL."""
    return [(not_null_constraint_name(table, column), "n", f"NOT NULL {column}", " ", " ") for column in columns]


def _connection(bind):
    return bind.connect() if isinstance(bind, sa.engine.Engine) else _Borrowed(bind)


class _Borrowed:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def _require(condition, message: str) -> None:
    # Pas d'instruction assert : ce module n'est pas réécrit par pytest et
    # doit rester actif même sous python -O.
    if not condition:
        raise AssertionError(message)


def without_verified_not_null(bind, table: str, constraints, expected_not_null) -> list:
    """Vérifie les NOT NULL de `table` puis retourne `constraints` sans ses
    lignes 'n'. `constraints` : lignes (conname, contype, définition,
    confdeltype, confupdtype) lues dans pg_constraint pour cette table ;
    `expected_not_null` : colonnes NOT NULL attendues par le test."""
    constraints = [tuple(row) for row in constraints]
    expected_not_null = list(expected_not_null)
    _require(len(set(expected_not_null)) == len(expected_not_null), f"{table} : colonne attendue en double")
    with _connection(bind) as conn:
        version = int(conn.execute(sa.text("SHOW server_version_num")).scalar_one())
        attnotnull = conn.execute(sa.text(
            "SELECT attname FROM pg_attribute WHERE attrelid = CAST(:t AS regclass) "
            "AND attnum > 0 AND NOT attisdropped AND attnotnull ORDER BY attnum"
        ), {"t": f"public.{table}"}).scalars().all()
        catalog_rows = conn.execute(sa.text(
            "SELECT c.conname, a.attname, c.convalidated, c.conislocal, c.connoinherit, "
            "cardinality(c.conkey) FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "WHERE c.conrelid = CAST(:t AS regclass) AND c.contype = 'n' ORDER BY c.conname"
        ), {"t": f"public.{table}"}).all()

    _require(sorted(attnotnull) == sorted(expected_not_null),
             f"{table} : colonnes NOT NULL {sorted(attnotnull)} != attendues {sorted(expected_not_null)}")

    received = sorted(row for row in constraints if row[1] == "n")
    if version >= 180000:
        expected_rows = sorted(expected_not_null_rows(table, expected_not_null))
        _require(received == expected_rows, f"{table} : contraintes NOT NULL {received} != attendues {expected_rows}")
        expected_catalog = sorted(
            (not_null_constraint_name(table, column), column, True, True, False, 1) for column in expected_not_null)
        _require(sorted(tuple(row) for row in catalog_rows) == expected_catalog,
                 f"{table} : catalogue NOT NULL inattendu {catalog_rows}")
    else:
        _require(received == [] and catalog_rows == [], f"{table} : contrainte 'n' avant PostgreSQL 18")
    return [row for row in constraints if row[1] != "n"]
