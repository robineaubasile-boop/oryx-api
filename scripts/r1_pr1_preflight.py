"""Préflight de production R1 PR 1 (0015 à 0017) — READ-ONLY.

But
---
Avant de merger la PR qui apporte 0015_users_level_revision,
0016_portfolio_idempotency et 0017_oryx_auth_foundations (le pre-deploy
Railway exécute `alembic upgrade head` dès le merge), établir par une
preuve lue en base, et non par hypothèse :

  - la version de PostgreSQL (majeure.mineure ; >= 11 exigé par 0015) et le
    rôle du serveur (primaire, pas une réplique) ;
  - la révision Alembic réellement installée (exactement
    0014_evaluation_run_leases) ;
  - l'absence de tout objet que 0015 à 0017 vont créer (pas d'application
    partielle ou manuelle antérieure) ;
  - le volume de users (estimation du catalogue) et donc la durée attendue
    de validation du CHECK de 0015 sous verrou ;
  - l'activité concurrente au moment du contrôle (transactions longues,
    verrous sur users) : une fenêtre calme évite l'échec par lock_timeout ;
  - les délais par défaut du rôle connecté (informatif).

Ce que ce script ne peut PAS prouver (voir alembic/README, section R1
PR 1) : l'existence d'une sauvegarde restaurable, la fenêtre de déploiement,
la configuration Railway (variables, proxys, Pre-deploy Timeout).

Garanties READ-ONLY
--------------------
- Une seule instruction SELECT (PREFLIGHT_SQL) sur des fonctions et
  catalogues système, plus alembic_version. Aucune ligne métier n'est lue
  (users n'est jamais parcourue : son volume vient de pg_class.reltuples).
- Connexion `default_transaction_read_only=on`, `SET TRANSACTION READ
  ONLY` confirmé par `SHOW transaction_read_only`, `lock_timeout` et
  `statement_timeout` courts : le préflight ne peut ni écrire ni rester
  bloqué. Jamais de commit.
- DATABASE_URL n'est jamais affiché, ni dans les messages d'erreur.

Autonomie
---------
Le script n'importe que la bibliothèque standard et SQLAlchemy (ni core/,
ni scripts/) : il peut être exécuté dans le conteneur oryx-api actuellement
déployé, qui ne le contient pas encore, en le transmettant par l'entrée
standard. `--print-sql` affiche la requête seule, sans connexion, pour une
exécution par psql.

Sortie
------
- GO TECHNIQUE (avertissements éventuels listés), exit code 0 ;
- NO-GO (au moins un FAIL), exit code 1 ;
- erreur opérationnelle (DATABASE_URL absent, connexion impossible,
  read-only non confirmé...), message sans secret, exit code 2.

Usage
-----
    DATABASE_URL=postgresql://... python scripts/r1_pr1_preflight.py
    python scripts/r1_pr1_preflight.py --print-sql
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

EXPECTED_REVISION = "0014_evaluation_run_leases"
PR1_REVISIONS = ("0015_users_level_revision", "0016_portfolio_idempotency", "0017_oryx_auth_foundations")
MIN_SERVER_VERSION_NUM = 110000  # ADD COLUMN ... DEFAULT sans réécriture (0015)
PR1_CHECK = "ck_users_level_revision_non_negative"
PR1_TABLES = (
    "portfolio_write_idempotency",
    "auth_identities",
    "invitations",
    "auth_operations",
    "auth_preauth_candidates",
    "auth_otp_requests",
    "auth_otp_verification_attempts",
    "auth_abuse_subjects",
    "auth_abuse_events",
    "oryx_sessions",
    "oryx_session_generations",
    "auth_operation_results",
)
# Garde-fous de 0015 (lock_timeout, statement_timeout) et débit de
# validation du CHECK mesuré sur PostgreSQL 16 et 18 (~50 ms par million de
# lignes) : au-delà de ces seuils, le préflight avertit.
LONG_TRANSACTION_SECONDS = 5
CHECK_SCAN_MS_PER_MILLION_ROWS = 50
CHECK_SCAN_WARN_MS = 5000

_TABLE_LIST = ", ".join(f"'{name}'" for name in PR1_TABLES)

# Une seule instruction SELECT, sans point-virgule final : elle reste valide
# si un outil de requête y ajoute « LIMIT n ».
PREFLIGHT_SQL = f"""SELECT
  current_setting('server_version') AS server_version,
  current_setting('server_version_num')::int AS server_version_num,
  pg_is_in_recovery() AS in_recovery,
  current_setting('transaction_read_only') AS transaction_read_only,
  (SELECT string_agg(version_num, ',' ORDER BY version_num) FROM public.alembic_version) AS alembic_revisions,
  EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'public.users'::regclass
          AND attname = 'level_revision' AND NOT attisdropped) AS level_revision_present,
  EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.users'::regclass
          AND conname = '{PR1_CHECK}') AS level_revision_check_present,
  ARRAY(SELECT c.relname::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname IN ({_TABLE_LIST}) ORDER BY 1) AS pr1_tables_present,
  (SELECT reltuples::bigint FROM pg_class WHERE oid = 'public.users'::regclass) AS users_estimated_rows,
  pg_size_pretty(pg_total_relation_size('public.users')) AS users_total_size,
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()
          AND xact_start < now() - interval '{LONG_TRANSACTION_SECONDS} seconds')::int AS long_transactions,
  (SELECT COALESCE(max(EXTRACT(EPOCH FROM now() - xact_start)), 0)::int FROM pg_stat_activity
          WHERE datname = current_database() AND pid <> pg_backend_pid()) AS oldest_transaction_seconds,
  (SELECT count(DISTINCT pid) FROM pg_locks WHERE relation = 'public.users'::regclass
          AND pid <> pg_backend_pid())::int AS users_lock_holders,
  (SELECT reset_val || COALESCE(unit, '') FROM pg_settings WHERE name = 'lock_timeout') AS role_lock_timeout,
  (SELECT reset_val || COALESCE(unit, '') FROM pg_settings WHERE name = 'statement_timeout') AS role_statement_timeout,
  (SELECT reset_val || COALESCE(unit, '') FROM pg_settings
          WHERE name = 'idle_in_transaction_session_timeout') AS role_idle_in_transaction_session_timeout"""

# Délais propres au préflight : il ne doit jamais attendre un verrou ni
# durer (SET LOCAL, autorisé en transaction read-only).
PREFLIGHT_GUARDS = ("SET LOCAL lock_timeout = '2s'", "SET LOCAL statement_timeout = '10s'")

READONLY_CONNECT_ARGS = {"options": "-c default_transaction_read_only=on"}


class PreflightError(RuntimeError):
    """Erreur opérationnelle (message sans secret)."""


@dataclass
class Finding:
    level: str  # FAIL, WARN, INFO
    message: str


@dataclass
class PreflightReport:
    findings: List[Finding] = field(default_factory=list)

    @property
    def go(self) -> bool:
        return not any(f.level == "FAIL" for f in self.findings)


def evaluate(facts: dict) -> PreflightReport:
    """Règles pures : faits lus par PREFLIGHT_SQL -> constats."""
    report = PreflightReport()
    add = lambda level, message: report.findings.append(Finding(level, message))  # noqa: E731

    version_num = int(facts["server_version_num"])
    major, minor = version_num // 10000, version_num % 10000
    if version_num < MIN_SERVER_VERSION_NUM:
        add("FAIL", f"PostgreSQL {facts['server_version']} : 0015 exige PostgreSQL >= 11")
    else:
        add("INFO", f"PostgreSQL {major}.{minor} (server_version = {facts['server_version']})")
    if facts["in_recovery"]:
        add("FAIL", "le serveur est une réplique (pg_is_in_recovery) : migration impossible ici")
    if str(facts["transaction_read_only"]).lower() != "on":
        add("FAIL", "transaction du préflight non read-only")

    revisions = facts["alembic_revisions"]
    if revisions == EXPECTED_REVISION:
        add("INFO", f"révision Alembic installée : {revisions}")
    elif revisions and any(rev in revisions.split(",") for rev in PR1_REVISIONS):
        add("FAIL", f"révision Alembic {revisions} : une migration de R1 PR 1 est déjà appliquée ; "
                    "ne rien relancer, analyser d'abord")
    else:
        add("FAIL", f"révision Alembic {revisions!r} au lieu de {EXPECTED_REVISION}")

    if facts["level_revision_present"]:
        add("FAIL", "users.level_revision existe déjà")
    if facts["level_revision_check_present"]:
        add("FAIL", f"{PR1_CHECK} existe déjà")
    if facts["pr1_tables_present"]:
        add("FAIL", "tables R1 PR 1 déjà présentes : " + ", ".join(facts["pr1_tables_present"]))

    rows = max(int(facts["users_estimated_rows"]), 0)  # -1 = jamais analysée
    scan_ms = rows * CHECK_SCAN_MS_PER_MILLION_ROWS // 1_000_000
    estimate = f"users : ~{rows} lignes (estimation pg_class), {facts['users_total_size']}, " \
               f"validation du CHECK estimée à ~{scan_ms} ms sous verrou"
    add("WARN" if scan_ms > CHECK_SCAN_WARN_MS else "INFO", estimate)

    if facts["long_transactions"] or facts["users_lock_holders"]:
        add("WARN", f"activité concurrente : {facts['long_transactions']} transaction(s) ouverte(s) depuis plus de "
                    f"{LONG_TRANSACTION_SECONDS} s (la plus ancienne : {facts['oldest_transaction_seconds']} s), "
                    f"{facts['users_lock_holders']} session(s) tenant un verrou sur users ; l'upgrade échouerait "
                    "par lock_timeout (5 s) si elles persistent : choisir une fenêtre calme")
    else:
        add("INFO", "aucune transaction longue ni verrou concurrent sur users au moment du contrôle")

    add("INFO", "délais par défaut du rôle : lock_timeout={}, statement_timeout={}, "
                "idle_in_transaction_session_timeout={} (0015 pose les siens par SET LOCAL)".format(
                    facts["role_lock_timeout"], facts["role_statement_timeout"],
                    facts["role_idle_in_transaction_session_timeout"]))
    return report


def collect_facts(database_url: str) -> dict:
    """Exécute PREFLIGHT_SQL en lecture seule confirmée ; jamais de commit."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.pool import NullPool

    # Même normalisation que core/db.py (recopiée : script autonome) :
    # SQLAlchemy 2.x choisirait sinon psycopg (v3), absent de l'image.
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    try:
        engine = create_engine(database_url, poolclass=NullPool, connect_args=READONLY_CONNECT_ARGS)
    except Exception as exc:  # noqa: BLE001 — jamais l'URL dans le message
        raise PreflightError(f"DATABASE_URL inutilisable ({type(exc).__name__})") from exc
    try:
        with engine.connect() as connection:
            if connection.dialect.name != "postgresql":
                raise PreflightError("la base n'est pas PostgreSQL")
            connection.execute(text("SET TRANSACTION READ ONLY"))
            if str(connection.execute(text("SHOW transaction_read_only")).scalar()).lower() != "on":
                raise PreflightError("PostgreSQL n'a pas confirmé le mode read-only : arrêt")
            for guard in PREFLIGHT_GUARDS:
                connection.execute(text(guard))
            row = connection.execute(text(PREFLIGHT_SQL)).mappings().one()
            connection.rollback()
            return dict(row)
    except PreflightError:
        raise
    except SQLAlchemyError as exc:
        raise PreflightError(f"erreur PostgreSQL ({type(exc).__name__})") from exc
    finally:
        engine.dispose()


def format_report(report: PreflightReport) -> str:
    lines = [f"{f.level:<4} {f.message}" for f in report.findings]
    warnings = sum(f.level == "WARN" for f in report.findings)
    if report.go:
        lines.append(f"GO TECHNIQUE R1 PR 1 ({warnings} avertissement(s)) — restent à confirmer hors script : "
                     "sauvegarde restaurable, fenêtre de déploiement, configuration Railway (alembic/README)")
    else:
        lines.append("NO-GO R1 PR 1 — ne pas merger")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--print-sql"]:
        print(PREFLIGHT_SQL)
        return 0
    if args:
        print("usage : r1_pr1_preflight.py [--print-sql]", file=sys.stderr)
        return 2
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        print("PREFLIGHT ERROR: DATABASE_URL n'est pas défini", file=sys.stderr)
        return 2
    try:
        report = evaluate(collect_facts(database_url))
    except PreflightError as exc:
        print(f"PREFLIGHT ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — garde-fou générique, jamais de secret
        print(f"PREFLIGHT ERROR: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(format_report(report))
    return 0 if report.go else 1


if __name__ == "__main__":
    sys.exit(main())
