"""Contrôles Alembic indépendants des tests (jobs ci / migrations-16 et -18).

    python ci/check_migrations.py --leg migrations-16 --commit "$GITHUB_SHA" \\
        --offline-database oryx_ci_offline_16_test --report reports/result.json

Lancé sous `env -i` : seul ORYX_TEST_DATABASE_URL (base jetable vérifiée par
ci/verify_test_database.py) est présent. DATABASE_URL n'est injectée que dans
les sous-processus Alembic, avec cette même URL jetable (ou une URL factice
pour le mode offline, sans connexion).

Contrôles statiques (sans base) :
  1. une seule base (0001_current_oryx_baseline), une seule tête, chaîne
     linéaire, aucune révision manquante, aucun fichier hors chaîne ;
  2. identifiants : NNNN_[a-z0-9_]+, au plus 32 caractères
     (alembic_version.version_num VARCHAR(32)), nom de fichier = révision,
     préfixes 0001..N contigus dans l'ordre de la chaîne ;
  3. migrations historiques figées : sha256 identiques à
     ci/migration_history.sha256 (0001 à 0014 du main de référence) ;
  4. SQL offline : upgrade base -> head et downgrade head -> 0001 générés.

Contrôles PostgreSQL (base vide du job) :
  5. upgrade d'une base vide jusqu'à head en une fois ; révision finale ;
  6. invariants : contraintes NOT NULL de PostgreSQL 18 (contype 'n') en
     bijection exacte avec les colonnes attnotnull, aucune avant 18 ; aucun
     ENUM, trigger, fonction, ni extension autre que plpgsql ; FK
     NO ACTION / NO ACTION non différables ; modèles SQLAlchemy et schéma
     de head identiques (compare_metadata vide, types et server defaults
     compris) ;
  7. SQL offline appliqué à une seconde base jetable : schéma identique ;
  8. montée révision par révision depuis une base vide, avec des données
     historiques insérées en 0001 : données conservées à chaque étape ;
     schéma final identique à la montée directe ;
  9. descente révision par révision jusqu'à 0001 (jamais en dessous) :
     chaque downgrade restaure exactement le schéma de la révision
     précédente, données conservées ; puis remontée jusqu'à head.

Le rapport JSON liste chaque contrôle ; le processus sort en 1 au premier
échec (message sans URL ni secret).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"
MANIFEST = REPO_ROOT / "ci" / "migration_history.sha256"
BASELINE = "0001_current_oryx_baseline"
REVISION_ID = re.compile(r"^(?P<number>[0-9]{4})_[a-z0-9_]+$")
MAX_REVISION_LENGTH = 32
TEST_DATABASE_NAME = re.compile(r"^oryx_[a-z0-9_]*_test$")
DISPOSABLE_COMMENT = "oryx-disposable-test-db"
OFFLINE_URL = "postgresql://offline@localhost/offline"

# Données historiques insérées en 0001 (aucune ligne dans company_analyses,
# que 0004 exige vide). Clés : colonnes de la baseline, ordre de la PK.
SEED = {
    "users": [
        {"id": "ci-user-1", "level": "debutant", "created_at": "2026-01-02 03:04:05.123456"},
        {"id": "ci-user-2", "level": "confirme", "created_at": None},
    ],
    "portfolio_positions": [
        {"id": 1, "user_id": "ci-user-1", "ticker": "AIR.PA", "quantity": 12.5, "purchase_price": 101.25,
         "target_percent": 7.5, "envelope": "PEA", "created_at": "2026-01-03 00:00:00",
         "updated_at": "2026-01-04 00:00:00"},
        {"id": 2, "user_id": "ci-user-2", "ticker": "MSFT", "quantity": 3.0, "purchase_price": None,
         "target_percent": None, "envelope": None, "created_at": None, "updated_at": None},
    ],
    "investment_theses": [
        {"id": 1, "user_id": "ci-user-1", "ticker": "AIR.PA", "thesis_text": "Thèse : carnet de commandes",
         "created_at": "2026-01-05 10:00:00"},
    ],
    "analysis_facts": [
        {"id": 1, "user_id": "ci-user-1", "ticker": "AIR.PA", "fact_date": "2026-01-06 00:00:00",
         "fact_type": "roe", "fact_value": 15.25},
    ],
    "user_statements": [
        {"id": 1, "user_id": "ci-user-2", "ticker": "MSFT", "step": "business",
         "statement_date": None, "statement_text": "Énoncé « historique »"},
    ],
}


class CheckFailed(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Alembic
# --------------------------------------------------------------------------

def run_alembic(database_url: str, *args: str) -> str:
    env = dict(os.environ, DATABASE_URL=database_url)
    proc = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise CheckFailed(f"alembic {' '.join(args)} a échoué (code {proc.returncode}) :\n{tail}")
    return proc.stdout


def script_directory():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))


def revision_chain() -> list:
    """Révisions de la base à la tête, après les contrôles de structure."""
    script = script_directory()
    bases = script.get_bases()
    heads = script.get_heads()
    if bases != [BASELINE]:
        raise CheckFailed(f"base(s) inattendue(s) : {bases}")
    if len(heads) != 1:
        raise CheckFailed(f"tête(s) multiple(s) : {heads}")
    revisions = list(script.walk_revisions())  # tête -> base ; lève si une révision manque
    for rev in revisions:
        if isinstance(rev.down_revision, (tuple, list)) or rev.is_merge_point or rev.is_branch_point:
            raise CheckFailed(f"chaîne non linéaire en {rev.revision}")
        if rev.branch_labels or rev.dependencies:
            raise CheckFailed(f"branch_labels / depends_on inattendus en {rev.revision}")
    chain = [rev.revision for rev in reversed(revisions)]
    if chain[0] != BASELINE or chain[-1] != heads[0]:
        raise CheckFailed("la chaîne ne va pas de la baseline à la tête")
    return chain


def check_revision_files(chain: list) -> None:
    """Un fichier par révision de la chaîne, rien d'autre. Le nom de fichier
    peut être plus long que l'identifiant (0013_decryptage_conversation_affinity.py
    porte 0013_decryptage_conv_affinity, limité à 32 caractères) mais garde
    son préfixe numérique."""
    files = sorted(p.name for p in VERSIONS_DIR.iterdir() if p.is_file())
    python_files = [name for name in files if name.endswith(".py")]
    others = [name for name in files if not name.endswith(".py") and name != ".gitkeep"]
    if others:
        raise CheckFailed(f"fichiers inattendus dans alembic/versions : {others}")
    script = script_directory()
    paths = {rev: Path(script.get_revision(rev).path).name for rev in chain}
    if sorted(paths.values()) != python_files:
        raise CheckFailed("les fichiers de alembic/versions ne correspondent pas exactement à la chaîne")
    for position, rev in enumerate(chain, start=1):
        match = REVISION_ID.match(rev)
        if not match:
            raise CheckFailed(f"identifiant de révision invalide : {rev}")
        if len(rev) > MAX_REVISION_LENGTH:
            raise CheckFailed(f"identifiant de plus de {MAX_REVISION_LENGTH} caractères : {rev}")
        if int(match.group("number")) != position:
            raise CheckFailed(f"préfixe non contigu : {rev} en position {position}")
        if not REVISION_ID.match(Path(paths[rev]).stem) or not paths[rev].startswith(match.group("number") + "_"):
            raise CheckFailed(f"fichier {paths[rev]} sans le préfixe de sa révision {rev}")


def check_frozen_history(chain: list) -> list:
    entries = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        digest, _, path = line.partition("  ")
        entries.append((digest, path))
    script = script_directory()
    chain_paths = [Path(script.get_revision(rev).path).relative_to(REPO_ROOT).as_posix() for rev in chain]
    if [path for _, path in entries] != chain_paths[:len(entries)]:
        raise CheckFailed("le manifeste figé n'est pas un préfixe exact de la chaîne")
    frozen = chain[:len(entries)]
    for digest, path in entries:
        actual = hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()
        if actual != digest:
            raise CheckFailed(f"migration historique modifiée : {path}")
    return frozen


def check_offline_sql(chain: list) -> str:
    upgrade = run_alembic(OFFLINE_URL, "upgrade", "head", "--sql")
    positions = []
    for rev in chain:
        marker = f"'{rev}'"
        if marker not in upgrade:
            raise CheckFailed(f"SQL offline : version {rev} jamais écrite")
        positions.append(upgrade.index(marker))
    if positions != sorted(positions):
        raise CheckFailed("SQL offline : versions écrites dans le désordre")
    if f"UPDATE alembic_version SET version_num='{chain[-1]}'" not in upgrade:
        raise CheckFailed("SQL offline : la tête n'est pas la dernière version écrite")
    if len(chain) > 1:
        run_alembic(OFFLINE_URL, "downgrade", f"{chain[-1]}:{BASELINE}", "--sql")
    return upgrade


# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------

def disposable_engine(url: sa.engine.URL) -> sa.engine.Engine:
    if not TEST_DATABASE_NAME.match(url.database or ""):
        raise CheckFailed("nom de base non jetable")
    return sa.create_engine(url.set(drivername="postgresql+psycopg2"), poolclass=sa.pool.NullPool)


def assert_disposable(conn) -> None:
    database, server_addr, comment = conn.execute(sa.text(
        "SELECT current_database(), host(inet_server_addr()), "
        "shobj_description((SELECT oid FROM pg_database WHERE datname = current_database()), 'pg_database')"
    )).one()
    if not TEST_DATABASE_NAME.match(database or ""):
        raise CheckFailed("current_database() non jetable")
    if server_addr not in ("127.0.0.1", "::1"):
        raise CheckFailed("serveur PostgreSQL non local")
    if comment != DISPOSABLE_COMMENT:
        raise CheckFailed("commentaire sentinelle absent")


def reset_schema(engine) -> None:
    with engine.begin() as conn:
        assert_disposable(conn)
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))


def assert_empty(engine) -> None:
    with engine.connect() as conn:
        assert_disposable(conn)
        count = conn.execute(sa.text(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace")).scalar_one()
    if count:
        raise CheckFailed("la base du job n'est pas vide au départ")


SNAPSHOT_QUERIES = {
    "relations": (
        "SELECT relname, relkind::text FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace ORDER BY 1"
    ),
    "columns": (
        "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
        "pg_get_expr(d.adbin, d.adrelid), a.attidentity::text, a.attgenerated::text, "
        "rank() OVER (PARTITION BY c.relname ORDER BY a.attnum) "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm') "
        "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1, 8"
    ),
    "constraints": (
        "SELECT conrelid::regclass::text, conname, contype::text, pg_get_constraintdef(oid), "
        "convalidated, condeferrable, condeferred, confdeltype::text, confupdtype::text "
        "FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
    ),
    "indexes": (
        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1, 2"
    ),
    "types": (
        "SELECT typname, typtype::text FROM pg_type WHERE typnamespace = 'public'::regnamespace "
        "AND typtype NOT IN ('b') AND typrelid = 0 ORDER BY 1"
    ),
    "functions": (
        "SELECT proname, pg_get_function_identity_arguments(oid) FROM pg_proc "
        "WHERE pronamespace = 'public'::regnamespace ORDER BY 1, 2"
    ),
    "triggers": (
        "SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1, 2"
    ),
    "extensions": "SELECT extname FROM pg_extension ORDER BY 1",
}


def snapshot(engine) -> dict:
    with engine.connect() as conn:
        return {name: [list(row) for row in conn.execute(sa.text(query)).all()]
                for name, query in SNAPSHOT_QUERIES.items()}


def schema_difference(left: dict, right: dict) -> list:
    problems = []
    for key in SNAPSHOT_QUERIES:
        missing = [row for row in left[key] if row not in right[key]]
        extra = [row for row in right[key] if row not in left[key]]
        if missing or extra:
            problems.append(f"{key}: absents {missing[:5]} / en trop {extra[:5]}")
    return problems


def server_version_num(engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.text("SHOW server_version_num")).scalar_one())


def check_invariants(engine, snap: dict) -> None:
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT c.relname, a.attname, a.attnotnull, "
            "(SELECT count(*) FROM pg_constraint n WHERE n.contype = 'n' AND n.conrelid = a.attrelid "
            " AND n.conkey = ARRAY[a.attnum]) "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        )).all()
        orphans = conn.execute(sa.text(
            "SELECT count(*) FROM pg_constraint n JOIN pg_attribute a ON a.attrelid = n.conrelid "
            "AND a.attnum = ANY(n.conkey) WHERE n.contype = 'n' "
            "AND n.connamespace = 'public'::regnamespace AND NOT a.attnotnull"
        )).scalar_one()
    with_constraints = server_version_num(engine) >= 180000
    for table, column, notnull, count in rows:
        expected = 1 if (with_constraints and notnull) else 0
        if count != expected:
            raise CheckFailed(f"NOT NULL {table}.{column} : {count} contrainte(s) 'n', {expected} attendue(s)")
    if orphans:
        raise CheckFailed("contrainte 'n' sur une colonne nullable")
    for _, name, contype, definition, validated, deferrable, deferred, deltype, updtype in snap["constraints"]:
        if not validated:
            raise CheckFailed(f"contrainte non validée : {name}")
        if deferrable or deferred:
            raise CheckFailed(f"contrainte différable : {name}")
        if contype == "f" and (deltype, updtype) != ("a", "a"):
            raise CheckFailed(f"FK avec action ON DELETE / ON UPDATE : {name}")
    if snap["types"]:
        raise CheckFailed(f"types définis dans public (ENUM...) : {snap['types']}")
    if snap["functions"]:
        raise CheckFailed(f"fonctions dans public : {snap['functions']}")
    if snap["triggers"]:
        raise CheckFailed(f"triggers : {snap['triggers']}")
    if snap["extensions"] != [["plpgsql"]]:
        raise CheckFailed(f"extensions inattendues : {snap['extensions']}")


def check_models(engine) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    sys.path.insert(0, str(REPO_ROOT))
    from core.db import Base
    from core import models  # noqa: F401 — enregistre les modèles

    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True})
        diff = compare_metadata(context, Base.metadata)
    if diff:
        raise CheckFailed(f"modèles SQLAlchemy et schéma de head divergent : {diff[:10]}")


def current_version(engine) -> str:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(rows) != 1:
        raise CheckFailed(f"alembic_version contient {len(rows)} ligne(s)")
    return rows[0]


def seed_history(engine) -> None:
    with engine.begin() as conn:
        for table, rows in SEED.items():
            for row in rows:
                columns = ", ".join(row)
                values = ", ".join(f":{name}" for name in row)
                conn.execute(sa.text(f"INSERT INTO {table} ({columns}) VALUES ({values})"), row)


def history_rows(engine) -> dict:
    with engine.connect() as conn:
        result = {}
        for table, rows in SEED.items():
            columns = ", ".join(rows[0])
            result[table] = [list(map(str, row)) for row in conn.execute(
                sa.text(f"SELECT {columns} FROM {table} ORDER BY id")).all()]
        return result


def run_checks(offline_database: str, report: dict) -> None:
    def record(name: str, detail=None) -> None:
        report["checks"].append({"check": name, "status": "passed", "detail": detail})
        print(f"[ok] {name}" + (f" : {detail}" if detail else ""), flush=True)

    chain = revision_chain()
    report["chain"] = chain
    record("base unique, tête unique, chaîne linéaire complète", f"{chain[0]} -> {chain[-1]} ({len(chain)})")
    check_revision_files(chain)
    record("identifiants et fichiers de révision")
    frozen = check_frozen_history(chain)
    record("migrations historiques identiques au manifeste figé", f"{frozen[0]} .. {frozen[-1]}")
    offline_sql = check_offline_sql(chain)
    record("SQL offline upgrade / downgrade")

    url = sa.engine.make_url(os.environ["ORYX_TEST_DATABASE_URL"])
    engine = disposable_engine(url)
    offline_engine = disposable_engine(url.set(database=offline_database))
    pg_url = url.render_as_string(hide_password=False)
    head = chain[-1]

    assert_empty(engine)
    run_alembic(pg_url, "upgrade", "head")
    if current_version(engine) != head:
        raise CheckFailed("révision finale inattendue après upgrade head")
    if run_alembic(pg_url, "current").split()[0] != head:
        raise CheckFailed("alembic current ne désigne pas la tête")
    direct = snapshot(engine)
    record("upgrade d'une base vide jusqu'à head", head)
    check_invariants(engine, direct)
    record("invariants du schéma (NOT NULL, FK, types, fonctions, triggers, extensions)",
           f"server_version_num={server_version_num(engine)}")
    check_models(engine)
    record("modèles SQLAlchemy == schéma de head (compare_metadata vide)")

    assert_empty(offline_engine)
    with offline_engine.connect() as conn:
        assert_disposable(conn)
        conn.rollback()
        raw = conn.connection.dbapi_connection
        raw.autocommit = True  # le script porte ses propres BEGIN / COMMIT
        with raw.cursor() as cursor:
            cursor.execute(offline_sql)  # sans paramètres : aucun formatage de '%'
    problems = schema_difference(direct, snapshot(offline_engine))
    if problems:
        raise CheckFailed("SQL offline appliqué != upgrade en ligne : " + "; ".join(problems))
    record("SQL offline appliqué == upgrade en ligne")

    reset_schema(engine)
    run_alembic(pg_url, "upgrade", BASELINE)
    seed_history(engine)
    expected_rows = history_rows(engine)
    snapshots = {BASELINE: snapshot(engine)}
    for rev in chain[1:]:
        run_alembic(pg_url, "upgrade", rev)
        if current_version(engine) != rev:
            raise CheckFailed(f"version inattendue après upgrade {rev}")
        if history_rows(engine) != expected_rows:
            raise CheckFailed(f"données historiques modifiées par l'upgrade {rev}")
        snapshots[rev] = snapshot(engine)
    problems = schema_difference(direct, snapshots[head])
    if problems:
        raise CheckFailed("montée pas à pas != montée directe : " + "; ".join(problems))
    record("montée révision par révision avec données, identique à la montée directe", f"{len(chain) - 1} étapes")

    for position in range(len(chain) - 1, 0, -1):
        previous = chain[position - 1]
        run_alembic(pg_url, "downgrade", previous)
        if current_version(engine) != previous:
            raise CheckFailed(f"version inattendue après downgrade {previous}")
        problems = schema_difference(snapshots[previous], snapshot(engine))
        if problems:
            raise CheckFailed(f"downgrade vers {previous} inexact : " + "; ".join(problems))
        if history_rows(engine) != expected_rows:
            raise CheckFailed(f"données historiques modifiées par le downgrade vers {previous}")
    record("descente révision par révision jusqu'à 0001 (jamais en dessous), exacte", f"{len(chain) - 1} étapes")

    run_alembic(pg_url, "upgrade", "head")
    problems = schema_difference(direct, snapshot(engine))
    if problems or current_version(engine) != head or history_rows(engine) != expected_rows:
        raise CheckFailed("remontée finale vers head inexacte : " + "; ".join(problems))
    record("remontée finale vers head", head)
    report["head"] = head
    report["server_version_num"] = server_version_num(engine)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--leg", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--offline-database", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    report = {"leg": args.leg, "kind": "migrations", "commit": args.commit, "checks": [], "status": "failure"}
    exit_code = 1
    try:
        if not TEST_DATABASE_NAME.match(args.offline_database):
            raise CheckFailed("nom de base offline non jetable")
        run_checks(args.offline_database, report)
        report["status"] = "success"
        exit_code = 0
    except CheckFailed as exc:
        report["error"] = str(exc)
        print(f"ÉCHEC : {exc}", file=sys.stderr)
    finally:
        os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, ensure_ascii=False)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
