"""Précontrôles PostgreSQL F2 en LECTURE SEULE (ACT-L1 / F04-B V0.3, B06) — DORMANT.

run_f2_preflight(engine, keyring) est une opération EXPLICITE (jamais à
l'import, jamais implicite) qui, dans UNE transaction PostgreSQL ouverte en
« SET TRANSACTION READ ONLY » puis toujours annulée (ROLLBACK), vérifie :

  1. le serveur attendu : current_database() et current_user égaux à la
     base et à l'utilisateur de l'URL du moteur ; version majeure qualifiée
     par la CI (QUALIFIED_POSTGRES_MAJORS) ;
  2. l'isolation EFFECTIVE de la transaction : read committed ;
  3. le mode effectif : transaction_read_only = on ;
  4. alembic_version : une seule ligne, égale à l'UNIQUE tête Alembic du code
     (alembic/versions, lue sans exécuter alembic/env.py) ;
  5. la présence des objets F2 nécessaires (F2_REQUIRED_TABLES,
     F2_REQUIRED_COLUMNS), résolus comme les requêtes F2 non qualifiées ;
  6. l'état anti-abus : core.auth_f2.abuse.load_key_state (lecture sans
     verrou ; ligne absente ou versions inconnues du trousseau : refus).

Interdits (vérifiés par tests/test_f04b_preflight.py) : aucun alembic
upgrade ni stamp, aucun initialize_abuse_key_state, aucune migration,
aucune création ni modification de données, aucune rotation de clé, aucune
réparation automatique. Tout état absent, inattendu ou incompatible lève
F2PreflightError(code) : échec FERMÉ, sans détail de valeur ni message
d'origine du pilote.
"""
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from core.auth_f2.abuse import AbuseUnavailable, load_key_state
from core.auth_f2.keys import Keyring
from core.auth_f2.policy import LOCK_TIMEOUT, STATEMENT_TIMEOUT

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
QUALIFIED_POSTGRES_MAJORS = (16, 18)
F2_REQUIRED_TABLES = (
    # Authentification F2 (0017, 0018)
    "auth_identities", "invitations", "auth_operations", "auth_preauth_candidates", "auth_otp_requests",
    "auth_otp_verification_attempts", "auth_abuse_subjects", "auth_abuse_events", "auth_abuse_key_state",
    "oryx_sessions", "oryx_session_generations", "auth_operation_results",
    # Frontière personnelle (0016, 0021) et tables métier qu'elle lit ou écrit
    "portfolio_write_idempotency", "decryptage_turn_reservations", "users", "portfolio_positions",
    "investment_theses", "analysis_sessions", "assistant_deliveries",
)
F2_REQUIRED_COLUMNS = (
    ("users", "level_revision"),                   # 0015
    ("auth_abuse_events", "http_action"),          # 0019
    ("portfolio_positions", "position_revision"),  # 0020
)

UNEXPECTED_SERVER = "unexpected_server"
UNSUPPORTED_SERVER_VERSION = "unsupported_server_version"
CONNECTION_FAILED = "connection_failed"
ISOLATION_NOT_READ_COMMITTED = "isolation_not_read_committed"
TRANSACTION_NOT_READ_ONLY = "transaction_not_read_only"
ALEMBIC_VERSION_MISSING = "alembic_version_missing"
ALEMBIC_HEAD_MISMATCH = "alembic_head_mismatch"
ALEMBIC_HEAD_AMBIGUOUS = "alembic_head_ambiguous"
F2_OBJECTS_MISSING = "f2_objects_missing"
ABUSE_KEY_STATE_UNAVAILABLE = "abuse_key_state_unavailable"
DATABASE_ERROR = "database_error"

_IDENTITY = sa.text(
    "SELECT current_database(), current_user, current_setting('server_version_num')::int, "
    "current_setting('transaction_isolation'), current_setting('transaction_read_only')")
_ALEMBIC_TABLE = sa.text("SELECT to_regclass('alembic_version') IS NOT NULL")
_ALEMBIC_VERSION = sa.text("SELECT version_num FROM alembic_version")
_MISSING_TABLES = sa.text(
    "SELECT name FROM unnest(CAST(:names AS text[])) AS name WHERE to_regclass(name) IS NULL ORDER BY name")
_COLUMN = sa.text(
    "SELECT count(*) FROM pg_catalog.pg_attribute WHERE attrelid = to_regclass(:table) AND attname = :column "
    "AND attnum > 0 AND NOT attisdropped")


class F2PreflightError(RuntimeError):
    """Précontrôle refusé : code stable, aucun détail de valeur."""

    def __init__(self, code: str):
        super().__init__(f"précontrôle F2 refusé : {code}")
        self.code = code


@dataclass(frozen=True)
class PreflightReport:
    server_major: int
    alembic_head: str
    abuse_versions: tuple


def expected_alembic_head(ini_path=ALEMBIC_INI) -> str:
    """UNIQUE tête du répertoire de migrations du code (aucune connexion,
    alembic/env.py jamais exécuté)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(Config(str(ini_path))).get_heads()
    if len(heads) != 1:
        raise F2PreflightError(ALEMBIC_HEAD_AMBIGUOUS)
    return heads[0]


def run_f2_preflight(engine, keyring, *, expected_head=None) -> PreflightReport:
    """Précontrôles en lecture seule (voir le module) ; F2PreflightError sinon."""
    if not isinstance(engine, sa.engine.Engine) or engine.dialect.name != "postgresql":
        raise TypeError("moteur PostgreSQL attendu")
    if type(keyring) is not Keyring:
        raise TypeError("trousseau F2 attendu")
    head = expected_alembic_head() if expected_head is None else expected_head
    if type(head) is not str or not head:
        raise TypeError("tête Alembic attendue")
    try:
        conn = engine.connect()
    except sa.exc.SQLAlchemyError:
        raise F2PreflightError(CONNECTION_FAILED) from None
    try:
        with conn:
            transaction = conn.begin()
            try:
                return _checks(conn, engine.url, keyring, head)
            finally:
                transaction.rollback()  # jamais de COMMIT, même si tout est conforme
    except F2PreflightError:
        raise
    except sa.exc.SQLAlchemyError:
        raise F2PreflightError(DATABASE_ERROR) from None


def _checks(conn, url, keyring: Keyring, head: str) -> PreflightReport:
    conn.execute(sa.text("SET TRANSACTION READ ONLY"))
    conn.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    conn.execute(sa.text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    database, user, version_num, isolation, read_only = conn.execute(_IDENTITY).one()
    if database != url.database or user != url.username:
        raise F2PreflightError(UNEXPECTED_SERVER)
    major = version_num // 10000
    if major not in QUALIFIED_POSTGRES_MAJORS:
        raise F2PreflightError(UNSUPPORTED_SERVER_VERSION)
    if isolation != "read committed":
        raise F2PreflightError(ISOLATION_NOT_READ_COMMITTED)
    if read_only != "on":
        raise F2PreflightError(TRANSACTION_NOT_READ_ONLY)
    if not conn.execute(_ALEMBIC_TABLE).scalar_one():
        raise F2PreflightError(ALEMBIC_VERSION_MISSING)
    versions = conn.execute(_ALEMBIC_VERSION).scalars().all()
    if not versions:
        raise F2PreflightError(ALEMBIC_VERSION_MISSING)
    if versions != [head]:
        raise F2PreflightError(ALEMBIC_HEAD_MISMATCH)
    if conn.execute(_MISSING_TABLES, {"names": list(F2_REQUIRED_TABLES)}).scalars().all():
        raise F2PreflightError(F2_OBJECTS_MISSING)
    for table, column in F2_REQUIRED_COLUMNS:
        if conn.execute(_COLUMN, {"table": table, "column": column}).scalar_one() != 1:
            raise F2PreflightError(F2_OBJECTS_MISSING)
    try:
        state = load_key_state(conn, keyring)
    except AbuseUnavailable:
        raise F2PreflightError(ABUSE_KEY_STATE_UNAVAILABLE) from None
    return PreflightReport(major, head, state.versions())
