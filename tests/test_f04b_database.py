"""F04-B / B03, B04 — moteur SQLAlchemy F2 et pool borné.

Sans base : normalisation de DATABASE_URL, paramètres figés, aucun moteur
ni core.db à l'import. Avec la base jetable (ORYX_TEST_DATABASE_URL, garde-
fous tests/pg_guard.py ; SKIP sinon) : plafond de 7 connexions, saturation
et attente bornée à 5 s, restitution, isolation READ COMMITTED effective,
fermeture par dispose, aucun retry.
"""
import ast
import json
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

import pytest
import sqlalchemy as sa

from core.f2_runtime import database as d
from tests.pg_guard import assert_disposable_connection
from tests.test_f2_dormancy import F04C_ENTRYPOINT
from tests.test_f2_http_dormancy import _clean_env
from tests.test_migration_0002_analysis_sessions import REPO_ROOT, pg_url  # noqa: F401 — fixture


# --------------------------------------------------------------------------
# Sans base
# --------------------------------------------------------------------------

def test_frozen_pool_parameters():
    assert d.POOL_PARAMETERS == {"pool_size": 5, "max_overflow": 2, "pool_timeout": 5, "pool_pre_ping": True,
                                 "pool_recycle": 1800, "isolation_level": "READ COMMITTED"}


def test_engine_is_built_only_on_request_with_the_frozen_pool():
    engine = d.create_f2_engine(d.normalize_f2_database_url("postgres://u:p@127.0.0.1:1/oryx_never_test"))
    try:
        assert isinstance(engine.pool, sa.pool.QueuePool)
        assert engine.pool.size() == 5 and engine.pool.timeout() == 5
        assert engine.pool.checkedout() == 0
        assert engine.dialect.name == "postgresql" and engine.dialect.driver == "psycopg2"
        assert engine.dialect._on_connect_isolation_level == "READ COMMITTED"
        assert engine.hide_parameters is True
        # Règle de la composition personnelle (isolated.py) : isolation configurée READ COMMITTED.
        configured = (engine.get_execution_options().get("isolation_level")
                      or getattr(engine.dialect, "_on_connect_isolation_level", None))
        assert configured == "READ COMMITTED"
    finally:
        engine.dispose()


@pytest.mark.parametrize("raw", ["", None, b"postgresql://u:p@h/d", "sqlite:///x.db", "postgresql+asyncpg://u:p@h/d",
                                 "postgresql+psycopg://u:p@h/d", "postgresql://u:p@h", "postgresql://h/d",
                                 "postgresql://u@/d", "postgresql://u:p@h:notaport/d"])
def test_database_url_outside_the_contract_is_refused_without_echo(raw):
    with pytest.raises(d.F2DatabaseUrlError) as info:
        d.normalize_f2_database_url(raw)
    assert "p@h" not in str(info.value) and info.value.__cause__ is None


def test_engine_factory_refuses_unnormalized_urls():
    with pytest.raises(d.F2DatabaseUrlError):
        d.create_f2_engine("postgresql://u:p@h/d")
    with pytest.raises(d.F2DatabaseUrlError):
        d.create_f2_engine(sa.engine.make_url("postgresql+asyncpg://u:p@h/d"))


def test_normalization_matches_the_legacy_formats_it_accepts():
    """Mêmes résultats que core.db.normalize_database_url sur les formats
    PostgreSQL psycopg2 (lu sans importer core.db : la fonction est relue
    depuis la source pour ne pas construire le moteur legacy)."""
    namespace = {}
    source = (REPO_ROOT / "core" / "db.py").read_text(encoding="utf-8")
    exec(compile(source.split("DATABASE_URL = ")[0], "core/db.py", "exec"), namespace)  # fonction pure seule
    legacy = namespace["normalize_database_url"]
    for raw in ("postgres://u:p@h:5432/d", "postgresql://u:p@h:5432/d", "postgresql+psycopg2://u:p@h/d?x=1"):
        assert d.normalize_f2_database_url(raw).render_as_string(hide_password=False) == legacy(raw)


IMPORT_PROBE = r'''
import gc, json, socket, sys, importlib
attempts = []
socket.socket.connect = lambda self, address: attempts.append("socket")
socket.create_connection = lambda *a, **k: attempts.append("create_connection")
import psycopg2
psycopg2.connect = lambda *a, **k: attempts.append("psycopg2")
for m in ("core.f2_runtime", "core.f2_runtime.database", "core.f2_runtime.config", "core.f2_runtime.preflight",
          "core.f2_runtime.resources"):
    importlib.import_module(m)
import sqlalchemy as sa
engines = sum(1 for o in gc.get_objects() if isinstance(o, sa.engine.Engine))
loaded = sorted(m for m in ("core.db", "core.models", "api", "fastapi", "starlette", "uvicorn", "yfinance",
                            "core.data_fetcher", "core.authz_f2_http.decryptage", "core.authz_f2.runner") if m in sys.modules)
print(json.dumps({"attempts": attempts, "engines": engines, "loaded": loaded}))
'''


@pytest.mark.parametrize("database_url", [None, "postgresql://u:p@127.0.0.1:1/oryx_never_test"])
def test_importing_f04b_has_no_external_effect_and_builds_no_engine(database_url):
    extra = {} if database_url is None else {"DATABASE_URL": database_url}
    out = subprocess.run([sys.executable, "-c", IMPORT_PROBE], cwd=REPO_ROOT, capture_output=True, text=True,
                         env={**_clean_env({}), **extra}, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result == {"attempts": [], "engines": 0, "loaded": []}


# ACT-L1 / F04-C (arbitrage E-1) : le point d'entrée dormant est le SEUL module
# de core/f2_runtime autorisé à lire l'environnement, et seulement par cette
# lecture-copie au démarrage (freeze F04-C § 6.2 : load_f2_config(copie de
# os.environ), aucune mutation, rien à l'import).
F04C_ENVIRONMENT_READ = "source = os.environ if self._environ is None else self._environ"
F04C_ENVIRONMENT_COPY = "config = load_f2_config(dict(source))"
F04C_ENVIRONMENT_FUNCTION = "_startup"


def _statement_and_function(node, parents):
    statement, function = node, None
    while statement in parents and not isinstance(statement, ast.stmt):
        statement = parents[statement]
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = current.name
            break
    return statement, function


def environment_read_only_at_startup(tree) -> list:
    """Problèmes d'usage de l'environnement du point d'entrée F04-C : seul
    « import os » ; os n'est utilisé que pour os.environ ; exactement UNE
    lecture, dans _startup, sous la forme F04C_ENVIRONMENT_READ ; la valeur lue
    n'est utilisée que copiée (F04C_ENVIRONMENT_COPY) ; aucune écriture."""
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    problems = []
    reads = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "os":
            attribute = parents.get(node)
            if not (isinstance(attribute, ast.Attribute) and attribute.attr == "environ"):
                problems.append(f"usage de os hors os.environ : {ast.unparse(parents.get(node, node))}")
                continue
            if not isinstance(attribute.ctx, ast.Load) or not isinstance(node.ctx, ast.Load):
                problems.append("écriture de os.environ")
            statement, function = _statement_and_function(attribute, parents)
            if ast.unparse(statement) != F04C_ENVIRONMENT_READ or function != F04C_ENVIRONMENT_FUNCTION:
                problems.append(f"lecture hors de la copie de démarrage : {ast.unparse(statement)} ({function})")
            reads += 1
        elif isinstance(node, ast.Attribute) and node.attr == "environ" and not (
                isinstance(node.value, ast.Name) and node.value.id == "os"):
            problems.append(f"environ hors os.environ : {ast.unparse(node)}")
    if reads != 1:
        problems.append(f"{reads} lecture(s) de os.environ (une seule attendue)")
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "source"]
    loads = [n for n in uses if isinstance(n.ctx, ast.Load)]
    if len(loads) != 1 or ast.unparse(_statement_and_function(loads[0], parents)[0]) != F04C_ENVIRONMENT_COPY:
        problems.append("la valeur lue doit être utilisée seulement copiée : " + F04C_ENVIRONMENT_COPY)
    return problems


def f04b_source_offences(relative: str, text: str) -> list:
    """Règles historiques F04-B pour tout module de core/f2_runtime ; seule
    exception NOMINATIVE : `os` / `environ` pour le point d'entrée F04-C, sous
    la forme exacte vérifiée par environment_read_only_at_startup."""
    tree = ast.parse(text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
    nominative = relative == F04C_ENTRYPOINT
    problems = []
    roots = ("dotenv", "api") if nominative else ("os", "dotenv", "api")
    banned = {m for m in imported if m.split(".")[0] in roots}
    if nominative and {m for m in imported if m.split(".")[0] == "os"} != {"os"}:
        banned |= {m for m in imported if m.split(".")[0] == "os"}
    if banned:
        problems.append(f"imports interdits : {sorted(banned)}")
    legacy = {m for m in imported if m == "core.db" or m.startswith("core.db.") or m == "core.models"}
    if legacy:
        problems.append(f"imports legacy : {sorted(legacy)}")
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    forbidden = {"environ", "getenv", "SessionLocal", "get_db", "putenv"} - ({"environ"} if nominative else set())
    if names & forbidden:
        problems.append(f"noms interdits : {sorted(names & forbidden)}")
    if nominative:
        problems += environment_read_only_at_startup(tree)
    return problems


def test_f04b_sources_never_reference_the_legacy_engine_nor_the_environment():
    for path in sorted((REPO_ROOT / "core" / "f2_runtime").glob("*.py")):
        relative = str(path.relative_to(REPO_ROOT))
        assert f04b_source_offences(relative, path.read_text(encoding="utf-8")) == [], path.name


ENTRYPOINT_LIKE = """import os


class Envelope:
    def _startup(self):
        source = os.environ if self._environ is None else self._environ
        config = load_f2_config(dict(source))
"""


@pytest.mark.parametrize("variant", [
    ENTRYPOINT_LIKE + "\nSECRET = os.environ['ORYX_F2_AUTH_KEYS_JSON']\n",          # lecture à l'import
    ENTRYPOINT_LIKE + "\n\ndef later():\n    return os.environ.get('X')\n",         # autre lecture
    ENTRYPOINT_LIKE.replace("dict(source)", "source"),                              # pas de copie
    ENTRYPOINT_LIKE + "\nos.environ['DATABASE_URL'] = ''\n",                       # mutation
    ENTRYPOINT_LIKE + "\nos.environ.pop('DATABASE_URL')\n",                        # mutation
    ENTRYPOINT_LIKE + "\nos.getenv('X')\n",                                         # autre accès
    ENTRYPOINT_LIKE.replace("import os", "from os import environ"),                 # import détourné
    ENTRYPOINT_LIKE + "\nfrom core.db import SessionLocal\n",                       # legacy
    ENTRYPOINT_LIKE + "\nimport api\n",                                             # point d'entrée servi
])
def test_the_environment_exception_refuses_any_other_use(variant):
    """Négatif : toute lecture à l'import, seconde lecture, absence de copie,
    mutation, autre accès à os, import détourné ou legacy est refusé, même dans
    le point d'entrée F04-C ; la forme exacte est admise."""
    assert f04b_source_offences(F04C_ENTRYPOINT, ENTRYPOINT_LIKE) == []
    assert f04b_source_offences(F04C_ENTRYPOINT, variant) != []


@pytest.mark.parametrize("relative", ["core/f2_runtime/config.py", "core/f2_runtime/work_registry.py",
                                      "core/f2_runtime/entrypoint_copy.py"])
def test_no_other_f2_runtime_module_may_read_the_environment(relative):
    """Négatif : la même forme exacte reste refusée hors du point d'entrée."""
    assert f04b_source_offences(relative, ENTRYPOINT_LIKE) != []


# --------------------------------------------------------------------------
# Base jetable
# --------------------------------------------------------------------------

@pytest.fixture
def tagged(pg_url):  # noqa: F811
    """URL de la base jetable marquée d'un application_name unique."""
    name = f"oryx_f04b_{uuid.uuid4().hex[:12]}"
    url = d.normalize_f2_database_url(pg_url).update_query_dict({"application_name": name})
    return url, name


@pytest.fixture
def observer(pg_url):  # noqa: F811
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    yield engine
    engine.dispose()


def _server_connections(observer, name) -> int:
    with observer.connect() as conn:
        return conn.execute(sa.text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :n"),
                            {"n": name}).scalar_one()


def test_pool_is_capped_at_seven_and_waits_at_most_five_seconds(tagged, observer):
    url, name = tagged
    engine = d.create_f2_engine(url)
    held = []
    try:
        for _ in range(7):
            held.append(engine.connect())
            held[-1].execute(sa.text("SELECT 1"))
        assert engine.pool.checkedout() == 7
        assert _server_connections(observer, name) == 7
        started = time.monotonic()
        with pytest.raises(sa.exc.TimeoutError):
            engine.connect()
        elapsed = time.monotonic() - started
        assert 4.5 <= elapsed < 7.5, elapsed  # attente bornée, aucun retry
        assert _server_connections(observer, name) == 7  # aucune huitième connexion serveur
    finally:
        for conn in held:
            conn.close()
    assert engine.pool.checkedout() == 0
    # Restitution : 5 connexions conservées au repos, le débordement est fermé.
    assert _server_connections(observer, name) == 5
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT 1")).scalar_one() == 1
    engine.dispose()
    assert _server_connections(observer, name) == 0


def test_saturation_under_concurrency_never_exceeds_the_cap(tagged, observer):
    url, name = tagged
    engine = d.create_f2_engine(url)
    peak, errors, lock = [0], [], threading.Lock()
    start = threading.Barrier(12)

    def work():
        start.wait()
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT pg_sleep(0.6)"))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=work) for _ in range(11)]
    for t in threads:
        t.start()
    start.wait()
    deadline = time.monotonic() + 20
    while any(t.is_alive() for t in threads) and time.monotonic() < deadline:
        peak[0] = max(peak[0], _server_connections(observer, name), engine.pool.checkedout())
        time.sleep(0.05)
    for t in threads:
        t.join(timeout=20)
    try:
        assert errors == []  # 11 travaux de 0,6 s : les 4 en attente obtiennent une connexion en moins de 5 s
        assert 1 <= peak[0] <= 7, peak
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()
    assert _server_connections(observer, name) == 0


def test_isolation_is_read_committed_even_if_the_session_default_differs(tagged):
    url, _ = tagged
    hostile = url.update_query_dict({"options": "-c default_transaction_isolation=serializable"})
    engine = d.create_f2_engine(hostile)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SHOW transaction_isolation")).scalar_one() == "read committed"
            conn.rollback()
        with engine.begin() as conn:
            assert conn.execute(sa.text("SHOW transaction_isolation")).scalar_one() == "read committed"
        # Témoin : sans isolation explicite, la même URL serait SERIALIZABLE.
        bare = sa.create_engine(hostile, poolclass=sa.pool.NullPool)
        with bare.connect() as conn:
            assert conn.execute(sa.text("SHOW transaction_isolation")).scalar_one() == "serializable"
        bare.dispose()
    finally:
        engine.dispose()


def test_connections_are_returned_after_errors_and_rollbacks(tagged, observer):
    url, name = tagged
    engine = d.create_f2_engine(url)
    try:
        for _ in range(10):
            with pytest.raises(sa.exc.ProgrammingError):
                with engine.begin() as conn:
                    conn.execute(sa.text("SELECT * FROM f04b_table_that_does_not_exist"))
        assert engine.pool.checkedout() == 0
        assert _server_connections(observer, name) <= 5
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_errors_hide_bound_parameters(tagged):
    url, _ = tagged
    engine = d.create_f2_engine(url)
    try:
        with pytest.raises(sa.exc.DBAPIError) as info:
            with engine.connect() as conn:
                # Division par zéro : l'erreur serveur ne cite pas la valeur ; seul
                # SQLAlchemy la reproduirait, sans hide_parameters.
                conn.execute(sa.text("SELECT length(CAST(:secret AS text)) / 0"), {"secret": "synthetic-bound-secret"})
        assert "synthetic-bound-secret" not in str(info.value)
        assert "SQL parameters hidden" in str(info.value)
    finally:
        engine.dispose()


def test_unreachable_server_fails_once_without_retry():
    engine = d.create_f2_engine(d.normalize_f2_database_url("postgresql://u:p@127.0.0.1:1/oryx_never_test"))
    attempts = []
    sa.event.listen(engine, "do_connect", lambda *a, **k: attempts.append(1))
    try:
        with pytest.raises(sa.exc.OperationalError):
            engine.connect()
        assert attempts == [1]
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# Freeze F04-B V0.3 § 4 : la normalisation n'altère pas les mots de passe encodés
# --------------------------------------------------------------------------

ENCODED = [
    ("postgres://u:p%40ss%2Fw%3A%25@h:5432/d", "p@ss/w:%"),
    ("postgresql://u:a%2Bb%20c%23%3F%26@h/d?sslmode=require", "a+b c#?&"),
    ("postgresql+psycopg2://u:%C3%A9t%C3%A9@h/d", "été"),
]


@pytest.mark.parametrize("raw, password", ENCODED)
def test_normalization_keeps_encoded_passwords_exactly(raw, password):
    url = d.normalize_f2_database_url(raw)
    assert url.password == password
    engine = d.create_f2_engine(url)
    try:
        _, kwargs = engine.dialect.create_connect_args(engine.url)
        assert kwargs["password"] == password  # valeur transmise telle quelle au pilote
    finally:
        engine.dispose()


def test_an_encoded_password_really_authenticates(pg_url):  # noqa: F811
    """Rôle temporaire de la base jetable dont le mot de passe contient des
    caractères réservés ; connexion par une URL postgres:// encodée."""
    admin = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    role = f"oryx_f04b_pw_{uuid.uuid4().hex[:10]}"
    password = "p@ss/w:%#?& +é"
    encoded = urllib.parse.quote(password, safe="")
    base = sa.engine.make_url(pg_url)
    raw = f"postgres://{role}:{encoded}@{base.host}:{base.port}/{base.database}"
    with admin.begin() as conn:
        assert_disposable_connection(conn)
        conn.execute(sa.text(f"CREATE ROLE {role} LOGIN PASSWORD :p"), {"p": password})
    engine = d.create_f2_engine(d.normalize_f2_database_url(raw))
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT current_user")).scalar_one() == role
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.text(f"DROP ROLE {role}"))
        admin.dispose()
